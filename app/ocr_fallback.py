"""OCR-2 Phase 2: 起動判定・検算・強制停止・ジョブ化フォールバック（帳票割当・API/UIは対象外）。"""
from __future__ import annotations

import html
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.capability_registry import ocr_feature_enabled
from app.ocr_artifacts import (
    Renderer,
    generate_manifest,
    make_execution_key,
    render_pdf_page,
    sha256_hex,
    verify_manifest,
    verify_source_hash,
    write_artifact,
)
from app.ocr_client import DOCUMENT_TIMEOUT_SECONDS, MAX_CONCURRENT_OCR, OcrClient
from app.ocr_schema import (
    SCHEMA_VERSION,
    OcrErrorCode,
    classify_field_state,
    field_as_zero_amount,
    follow_up_kind,
    make_ocr_field,
    validate_ocr_result,
)
from app.ocr_store import OcrStore, PAGE_DONE_STATUSES, TERMINAL_STATUSES

DEFAULT_ENGINE = {
    "pipeline": "PaddleOCR-VL-1.6",
    "vlm_model": "PaddleOCR-VL-1.6-0.9B",
    "layout_model": "PP-DocLayoutV3",
    "image_digest": "",
    "parameters_hash": "",
}
DEFAULT_DPI = 200
MIN_DPI = 200
MAX_DPI = 300
MAX_PAGES = 500
REPLACEMENT_CONTROL_THRESHOLD = 0.005
MIN_CHARS_PER_PAGE = 50
IMAGE_AREA_RATIO_THRESHOLD = 0.80
TOTAL_MISMATCH_YEN = Decimal("1")
REPETITION_LIMIT = 10
SHORT_PHRASE_MAX_LEN = 40
CONTROL_KEEP = frozenset("\n\r\t")
AMOUNT_TYPES = frozenset({"decimal", "amount", "money", "yen"})
INVOICE_FIELD_NAMES = {
    "previous_invoice_amount": "previous",
    "previous_balance": "previous",
    "payment_amount": "payment",
    "deposit_amount": "payment",
    "current_purchase_amount": "purchase",
    "current_invoice_amount": "current",
}
DATE_ROLES = frozenset({"target_month", "transaction_date", "closing_date"})
IMPORTANT_AMOUNT_NAMES = frozenset({
    "current_invoice_amount", "previous_invoice_amount", "payment_amount",
    "current_purchase_amount", "tax_amount", "line_amount", "unit_price",
    "previous_balance", "deposit_amount",
})
TRIGGER_PRIORITY = (
    "unreadable", "garbled", "table_required", "low_text_density", "manual",
)
TRG_TRIGGER = {
    "TRG-01": "unreadable",
    "TRG-02": "garbled",
    "TRG-03": "garbled",
    "TRG-04": "low_text_density",
    "TRG-05": "unreadable",
    "TRG-06": "table_required",
    "TRG-07": "table_required",
    "TRG-08": "garbled",
    "TRG-09": "manual",
}
FORCE_FAIL_CODES = frozenset({
    OcrErrorCode.OCR_SOURCE_HASH_MISMATCH.value,
    OcrErrorCode.OCR_PAGE_RENDER_FAILED.value,
    OcrErrorCode.OCR_SCHEMA_INVALID.value,
    OcrErrorCode.OCR_ARTIFACT_TAMPERED.value,
    OcrErrorCode.OCR_REPETITION_DETECTED.value,
    OcrErrorCode.OCR_TOTAL_MISMATCH.value,
    OcrErrorCode.OCR_SERVICE_UNAVAILABLE.value,
})
FORCE_REVIEW_CODES = frozenset({
    OcrErrorCode.OCR_TIMEOUT.value,
    OcrErrorCode.OCR_LAYOUT_FAILED.value,
    OcrErrorCode.OCR_FIELD_AMBIGUOUS.value,
    OcrErrorCode.OCR_REVIEW_REQUIRED.value,
})

_OCR_JOB_LOCK = threading.BoundedSemaphore(MAX_CONCURRENT_OCR)
_OCR_ACTIVE = 0
_OCR_ACTIVE_GUARD = threading.Lock()


@dataclass(frozen=True, slots=True)
class OcrTriggerThresholds:
    replacement_control_rate: float = REPLACEMENT_CONTROL_THRESHOLD
    min_chars_per_page: int = MIN_CHARS_PER_PAGE
    image_area_ratio: float = IMAGE_AREA_RATIO_THRESHOLD
    total_mismatch_yen: Decimal = TOTAL_MISMATCH_YEN


DEFAULT_THRESHOLDS = OcrTriggerThresholds()


@dataclass(frozen=True, slots=True)
class TriggerHit:
    page: int
    code: str
    trigger: str
    detail: str = ""


@dataclass(slots=True)
class TriggerDecision:
    pages: list[int]
    reasons: list[TriggerHit]
    trigger: str | None
    skipped: bool
    skip_reason: str = ""

    @property
    def reason_codes(self) -> list[str]:
        return [item.code for item in self.reasons]


def replacement_control_rate(text: str) -> float:
    if not text:
        return 0.0
    bad = 0
    for char in text:
        code = ord(char)
        if char == "\ufffd":
            bad += 1
        elif code < 32 and char not in CONTROL_KEEP:
            bad += 1
        elif 0x80 <= code <= 0x9F:
            bad += 1
    return bad / len(text)


def _page_no(page: Mapping[str, Any], index: int) -> int:
    raw = page.get("page", page.get("page_no", index + 1))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = index + 1
    return value if value >= 1 else index + 1


def _page_text(page: Mapping[str, Any]) -> str:
    return str(page.get("text") or page.get("extracted_text") or "")


def _page_quality(page: Mapping[str, Any]) -> str | None:
    quality = page.get("quality") or page.get("extraction_quality")
    if quality in {"readable", "garbled", "unreadable"}:
        return quality
    return None


def _char_count(page: Mapping[str, Any]) -> int:
    if page.get("char_count") is not None:
        try:
            return int(page["char_count"])
        except (TypeError, ValueError):
            pass
    body = re.sub(r"\[Page \d+\]", "", _page_text(page))
    return len(body)


def _ratio(page: Mapping[str, Any], key: str) -> float | None:
    raw = page.get(key)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return None
    text = str(value).strip().replace(",", "").replace("¥", "").replace("￥", "").replace("円", "")
    if not text:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    return number


def _amount_unmapped(page: Mapping[str, Any]) -> bool:
    if page.get("amount_candidates_unmapped") is True:
        return True
    if page.get("amount_candidates") and not page.get("amount_field_mapping"):
        return True
    return False


def _has_table_structure(page: Mapping[str, Any]) -> bool | None:
    if "has_table_structure" in page:
        return bool(page.get("has_table_structure"))
    if "table_structure" in page:
        return bool(page.get("table_structure"))
    return None


def evaluate_page_triggers(
    page: Mapping[str, Any],
    *,
    index: int = 0,
    purpose: str = "context",
    user_requested_original_match: bool = False,
    known_total: Any = None,
    extracted_total: Any = None,
    thresholds: OcrTriggerThresholds = DEFAULT_THRESHOLDS,
) -> list[TriggerHit]:
    """仕様 第4章 TRG-01〜TRG-09 を1ページについて判定する。"""
    page_no = _page_no(page, index)
    hits: list[TriggerHit] = []
    quality = _page_quality(page)
    text = _page_text(page)
    if quality == "unreadable":
        hits.append(TriggerHit(page_no, "TRG-01", "unreadable", "extraction_quality=unreadable"))
    if quality == "garbled":
        hits.append(TriggerHit(page_no, "TRG-02", "garbled", "extraction_quality=garbled"))
    rate = _ratio(page, "replacement_control_rate")
    if rate is None:
        rate = replacement_control_rate(text)
    if rate >= thresholds.replacement_control_rate:
        hits.append(TriggerHit(
            page_no, "TRG-03", "garbled",
            f"replacement_control_rate={rate}",
        ))
    chars = _char_count(page)
    if chars < thresholds.min_chars_per_page:
        hits.append(TriggerHit(
            page_no, "TRG-04", "low_text_density",
            f"char_count={chars}",
        ))
    image_ratio = _ratio(page, "image_area_ratio")
    if image_ratio is not None and image_ratio >= thresholds.image_area_ratio:
        hits.append(TriggerHit(
            page_no, "TRG-05", "unreadable",
            f"image_area_ratio={image_ratio}",
        ))
    if purpose == "table":
        structure = _has_table_structure(page)
        if structure is not True:
            hits.append(TriggerHit(
                page_no, "TRG-06", "table_required",
                "table request without matrix structure",
            ))
    if _amount_unmapped(page):
        hits.append(TriggerHit(
            page_no, "TRG-07", "table_required",
            "amount candidates are not mapped to field names",
        ))
    page_known = page.get("known_total", known_total)
    page_extracted = page.get("extracted_total", extracted_total)
    known = _decimal(page_known)
    extracted = _decimal(page_extracted)
    if known is not None and extracted is not None:
        if abs(known - extracted) >= thresholds.total_mismatch_yen:
            hits.append(TriggerHit(
                page_no, "TRG-08", "garbled",
                f"extracted_total={extracted} known_total={known}",
            ))
    requested = user_requested_original_match or bool(page.get("user_requested_original_match"))
    if requested:
        hits.append(TriggerHit(page_no, "TRG-09", "manual", "user requested original matching"))
    return hits


def _select_trigger(hits: Sequence[TriggerHit]) -> str | None:
    found = {item.trigger for item in hits}
    for name in TRIGGER_PRIORITY:
        if name in found:
            return name
    return None


def evaluate_ocr_triggers(
    pages: Sequence[Mapping[str, Any]] | None,
    *,
    purpose: str = "context",
    user_requested_original_match: bool = False,
    known_total: Any = None,
    extracted_total: Any = None,
    thresholds: OcrTriggerThresholds | None = None,
    page_count: int | None = None,
) -> TriggerDecision:
    """ページ単位で OCR 起動ページと理由コードを返す。正常文字PDFは skipped。"""
    used = thresholds or DEFAULT_THRESHOLDS
    rows = list(pages or [])
    if not rows:
        if page_count and int(page_count) >= 1:
            # ページ数は分かるが抽出情報が空 = 通常抽出が読めなかった。OCR不要にしない。
            rows = [
                {"page": index, "text": "", "quality": "unreadable", "char_count": 0}
                for index in range(1, int(page_count) + 1)
            ]
        else:
            # 判定材料がゼロ。読めなかったものを問題なしにしてはならない。
            return TriggerDecision(
                pages=[],
                reasons=[TriggerHit(0, "TRG-01", "unreadable", "pages and page_count unavailable")],
                trigger="unreadable",
                skipped=False,
                skip_reason="OCR_QUALITY_UNKNOWN",
            )
    all_hits: list[TriggerHit] = []
    for index, page in enumerate(rows):
        all_hits.extend(evaluate_page_triggers(
            page, index=index, purpose=purpose,
            user_requested_original_match=user_requested_original_match,
            known_total=known_total, extracted_total=extracted_total,
            thresholds=used,
        ))
    page_nos = sorted({hit.page for hit in all_hits})
    if not page_nos:
        return TriggerDecision(
            pages=[], reasons=[], trigger=None, skipped=True,
            skip_reason=OcrErrorCode.OCR_NOT_REQUIRED.value,
        )
    return TriggerDecision(
        pages=page_nos,
        reasons=all_hits,
        trigger=_select_trigger(all_hits),
        skipped=False,
    )


def _check(
    check_id: str,
    status: str,
    *,
    expected: Any = "",
    actual: Any = "",
    detail: str = "",
) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "status": status,
        "expected": "" if expected is None else str(expected),
        "actual": "" if actual is None else str(actual),
        "detail": detail,
    }


class _VisibleTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._current_text: list[str] = []

    def handle_data(self, data: str) -> None:
        if data:
            self._current_text.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._flush()

    def handle_endtag(self, tag: str) -> None:
        self._flush()

    def _flush(self) -> None:
        if self._current_text:
            text = "".join(self._current_text).strip()
            if text:
                self._parts.append(text)
            self._current_text.clear()

    def get_text(self) -> str:
        self._flush()
        return " ".join(self._parts)


def extract_visible_text(text: str) -> str:
    """HTMLタグ・属性値を除去し可視テキスト（セル区切りは空白、空セル無視、エンティティ復号）を抽出する。"""
    if not text or ("<" not in text and ">" not in text):
        return text
    try:
        extractor = _VisibleTextExtractor()
        extractor.feed(text)
        extractor.close()
        return extractor.get_text()
    except Exception:
        clean = re.sub(r"<[^>]+>", " ", text)
        return " ".join(html.unescape(clean).split())


def _blocks_by_id(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for page in payload.get("pages") or []:
        if not isinstance(page, Mapping):
            continue
        for block in page.get("blocks") or []:
            if isinstance(block, Mapping) and block.get("block_id"):
                found[str(block["block_id"])] = dict(block)
                found[str(block["block_id"])]["_page"] = page.get("page")
    return found


def _all_text(payload: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for page in payload.get("pages") or []:
        if not isinstance(page, Mapping):
            continue
        for block in page.get("blocks") or []:
            if isinstance(block, Mapping):
                parts.append(str(block.get("text") or ""))
        if page.get("text"):
            parts.append(str(page.get("text")))
    for item in payload.get("fields") or []:
        if isinstance(item, Mapping):
            parts.append(str(item.get("value") or ""))
            parts.append(str(item.get("evidence_text") or ""))
    return "\n".join(parts)


def _all_visible_text(payload: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for page in payload.get("pages") or []:
        if not isinstance(page, Mapping):
            continue
        for block in page.get("blocks") or []:
            if isinstance(block, Mapping):
                parts.append(extract_visible_text(str(block.get("text") or "")))
        if page.get("text"):
            parts.append(extract_visible_text(str(page.get("text"))))
    for item in payload.get("fields") or []:
        if isinstance(item, Mapping):
            parts.append(extract_visible_text(str(item.get("value") or "")))
            parts.append(extract_visible_text(str(item.get("evidence_text") or "")))
    return "\n".join(parts)


def max_consecutive_short_repeats(text: str) -> int:
    if not text:
        return 0
    visible = extract_visible_text(text)
    if not visible:
        return 0
    parts = [part for part in re.split(r"\s+", visible) if part]
    longest = 1 if parts else 0
    run = 1
    for index in range(1, len(parts)):
        prev, current = parts[index - 1], parts[index]
        if current == prev and 1 <= len(current) <= SHORT_PHRASE_MAX_LEN:
            run += 1
            if run > longest:
                longest = run
        else:
            run = 1
    compact = re.sub(r"\s+", "", visible)
    for size in range(2, min(SHORT_PHRASE_MAX_LEN, max(len(compact), 2)) + 1):
        if size * REPETITION_LIMIT > len(compact):
            break
        index = 0
        while index + size * REPETITION_LIMIT <= len(compact):
            chunk = compact[index:index + size]
            count = 1
            cursor = index + size
            while compact[cursor:cursor + size] == chunk:
                count += 1
                cursor += size
            if count > longest:
                longest = count
            index += 1 if count == 1 else size * count
    return longest


def _amount_fields(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for item in payload.get("fields") or []:
        if not isinstance(item, Mapping):
            continue
        field_type = str(item.get("type") or "")
        name = str(item.get("name") or "")
        if field_type in AMOUNT_TYPES or name in IMPORTANT_AMOUNT_NAMES or name.endswith("_amount"):
            rows.append(dict(item))
    return rows


def _lossless_decimal(raw: str) -> tuple[bool, str]:
    text = str(raw).strip()
    parsed = _decimal(text)
    if parsed is None:
        return False, text
    normalized = str(text).strip().replace(",", "").replace("¥", "").replace("￥", "").replace("円", "")
    try:
        again = Decimal(normalized)
    except InvalidOperation:
        return False, text
    if again != parsed:
        return False, str(parsed)
    # 文字列が表す値と Decimal が一致し、float 経由の変換を使わない
    if "e" in normalized.lower():
        return False, normalized
    return True, str(parsed)


def validate_ocr_checks(
    payload: Mapping[str, Any],
    *,
    original_bytes: bytes | None = None,
    expected_sha256: str | None = None,
    target_pages: Sequence[int] | None = None,
    tax_rounding_rule: str | None = None,
    expected_invoice_formula: Mapping[str, Any] | None = None,
    vehicle_master: Sequence[str] | None = None,
    vehicle_rows_require_number: bool = False,
    reviewed_vehicle_numbers: Sequence[str] | None = None,
    regular_extraction: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """仕様 7.1 VAL-01〜VAL-12。未確定の業務規則は needs_review。"""
    checks: list[dict[str, Any]] = []
    source = payload.get("source") if isinstance(payload.get("source"), Mapping) else {}
    sha = str((expected_sha256 or source.get("sha256") or "")).strip()
    if original_bytes is not None and sha:
        issues = verify_source_hash(original_bytes, sha)
        checks.append(_check(
            "VAL-01", "failed" if issues else "passed",
            expected=sha.lower().replace("sha256:", ""),
            actual=sha256_hex(original_bytes),
            detail="" if not issues else issues[0].message,
        ))
    else:
        stored = str(source.get("sha256") or "")
        checks.append(_check(
            "VAL-01", "passed" if stored and stored == sha else "failed",
            expected=sha, actual=stored,
            detail="原本ハッシュを照合できません" if not stored or stored != sha else "",
        ))

    pages = [item for item in (payload.get("pages") or []) if isinstance(item, Mapping)]
    present = {int(item["page"]) for item in pages if isinstance(item.get("page"), int)}
    expected_pages = set(int(p) for p in (target_pages or present))
    source_count = source.get("page_count")
    if isinstance(source_count, int) and source_count >= 1 and target_pages is None:
        expected_pages = set(range(1, source_count + 1)) if not expected_pages else expected_pages
    coverage = expected_pages <= present and (not expected_pages or present >= expected_pages)
    missing = sorted(expected_pages - present)
    checks.append(_check(
        "VAL-02", "passed" if coverage and not missing else "failed",
        expected="100%",
        actual=f"{len(present & expected_pages)}/{len(expected_pages) if expected_pages else 0}",
        detail="" if not missing else f"missing_pages={missing}",
    ))

    blocks = _blocks_by_id(payload)
    val3_ok = True
    val3_detail = ""
    for index, item in enumerate(payload.get("fields") or []):
        if not isinstance(item, Mapping):
            val3_ok = False
            val3_detail = f"fields[{index}] is not object"
            break
        page = item.get("page")
        block_id = item.get("block_id")
        if not isinstance(page, int) or not block_id:
            val3_ok = False
            val3_detail = f"fields[{index}] missing page/block_id"
            break
        block = blocks.get(str(block_id))
        bbox = item.get("bbox") if item.get("bbox") is not None else (block or {}).get("bbox")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            val3_ok = False
            val3_detail = f"fields[{index}] missing bbox"
            break
    checks.append(_check("VAL-03", "passed" if val3_ok else "failed", detail=val3_detail))

    val4_ok = True
    val4_detail = ""
    for item in _amount_fields(payload):
        state = item.get("value_state") or classify_field_state(item.get("value"))
        if state in {"blank", "unreadable"}:
            continue
        if state == "zero":
            continue
        ok, actual = _lossless_decimal(str(item.get("value") or ""))
        if not ok:
            val4_ok = False
            val4_detail = f"{item.get('name')}: {actual}"
            break
    checks.append(_check("VAL-04", "passed" if val4_ok else "failed", detail=val4_detail))

    line_values: list[Decimal] = []
    totals: list[Decimal] = []
    has_unreadable_amount = False
    for item in _amount_fields(payload):
        state = item.get("value_state") or classify_field_state(item.get("value"))
        name = str(item.get("name") or "")
        if state == "unreadable":
            has_unreadable_amount = True
            continue
        if state == "blank":
            continue
        parsed = Decimal("0") if state == "zero" else _decimal(item.get("value"))
        if parsed is None:
            continue
        if name in {"line_amount", "detail_amount"} or name.startswith("line_"):
            line_values.append(parsed)
        if name in IMPORTANT_AMOUNT_NAMES and name.endswith("invoice_amount") or name in {
            "current_invoice_amount", "current_purchase_amount",
        }:
            if name == "current_invoice_amount":
                totals.append(parsed)
    if has_unreadable_amount and (line_values or totals):
        checks.append(_check(
            "VAL-05", "needs_review",
            detail="読取不能の金額があり合計検算を確定できない",
        ))
    elif line_values and totals:
        actual_sum = sum(line_values, Decimal("0"))
        expected_total = totals[0]
        if tax_rounding_rule:
            checks.append(_check(
                "VAL-05", "needs_review",
                expected=str(expected_total), actual=str(actual_sum),
                detail="税丸め規則は推測せず要確認",
            ))
        elif actual_sum == expected_total:
            checks.append(_check("VAL-05", "passed", expected=str(expected_total), actual=str(actual_sum)))
        else:
            checks.append(_check(
                "VAL-05", "failed",
                expected=str(expected_total), actual=str(actual_sum),
                detail="明細合計の許容差は0円",
            ))
    elif line_values and not totals:
        checks.append(_check("VAL-05", "needs_review", detail="合計項目がなく明細合計を確定できない"))
    else:
        checks.append(_check("VAL-05", "passed", detail="照合する明細合計がない"))

    roles: dict[str, Decimal] = {}
    ambiguous_amounts = False
    for item in payload.get("fields") or []:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "")
        role = INVOICE_FIELD_NAMES.get(name)
        state = item.get("value_state") or classify_field_state(item.get("value"))
        if role and state == "unreadable":
            ambiguous_amounts = True
            continue
        parsed = _decimal(item.get("value")) if state not in {"blank", "unreadable"} else None
        if state == "zero":
            parsed = Decimal("0")
        if role and parsed is not None:
            if role in roles:
                ambiguous_amounts = True
            roles[role] = parsed
        if name in {"amount", "total"} and not role:
            ambiguous_amounts = True
    if ambiguous_amounts and len(roles) < 4:
        checks.append(_check("VAL-06", "needs_review", detail="当月額と前月額・入金額の区別不能"))
    elif expected_invoice_formula is None and not roles:
        checks.append(_check("VAL-06", "passed", detail="請求式の対象項目がない"))
    elif expected_invoice_formula is None and len(roles) < 4:
        checks.append(_check(
            "VAL-06", "needs_review",
            detail="当月額と前月額・入金額の区別ができない、または帳票の請求式が未確定",
        ))
    else:
        previous = roles.get("previous", Decimal("0"))
        payment = roles.get("payment", Decimal("0"))
        purchase = roles.get("purchase", Decimal("0"))
        current = roles.get("current")
        actual = previous - payment + purchase
        if current is None:
            checks.append(_check("VAL-06", "needs_review", detail="当月請求額が無い"))
        elif actual == current:
            checks.append(_check("VAL-06", "passed", expected=str(current), actual=str(actual)))
        else:
            checks.append(_check(
                "VAL-06", "failed",
                expected=str(current), actual=str(actual),
                detail="前月請求-入金+当月買上=当月請求 が不一致",
            ))

    repeats = max_consecutive_short_repeats(_all_visible_text(payload))
    checks.append(_check(
        "VAL-07", "passed" if repeats < REPETITION_LIMIT else "failed",
        expected=f"<{REPETITION_LIMIT}",
        actual=str(repeats),
        detail="" if repeats < REPETITION_LIMIT else "同一短文の連続反復が10回以上",
    ))

    rate = replacement_control_rate(_all_text(payload))
    checks.append(_check(
        "VAL-08", "passed" if rate < REPLACEMENT_CONTROL_THRESHOLD else "failed",
        expected=f"<{REPLACEMENT_CONTROL_THRESHOLD}",
        actual=str(rate),
        detail="" if rate < REPLACEMENT_CONTROL_THRESHOLD else "置換・制御文字率が0.5%以上",
    ))

    master = set(vehicle_master or [])
    reviewed = set(reviewed_vehicle_numbers or [])
    vehicle_status = "passed"
    vehicle_detail = "車両原価行がない"
    found_vehicle_row = False
    for item in payload.get("fields") or []:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "")
        used = bool(item.get("used_for_vehicle_cost"))
        if name not in {"vehicle_number", "vehicle_id"} and not used:
            continue
        found_vehicle_row = True
        state = item.get("value_state") or classify_field_state(item.get("value"))
        number = str(item.get("value") or "").strip()
        if state in {"blank", "unreadable"} or not number:
            vehicle_status = "failed"
            vehicle_detail = "車両番号が必要な明細で番号不明"
            break
        if not master and not reviewed:
            vehicle_status = "needs_review"
            vehicle_detail = "車両マスター未提示のため一致を確定できない"
            break
        if number not in master and number not in reviewed:
            vehicle_status = "needs_review"
            vehicle_detail = "車両マスター不一致かつ未レビュー"
            break
        vehicle_status = "passed"
        vehicle_detail = "車両マスター一致またはレビュー済み"
    if not found_vehicle_row:
        vehicle_status = "passed"
        vehicle_detail = "車両原価へ使う行がない"
    checks.append(_check("VAL-09", vehicle_status, detail=vehicle_detail))

    date_fields = [
        item for item in (payload.get("fields") or [])
        if isinstance(item, Mapping) and (
            str(item.get("type") or "") in {"date", "month"}
            or str(item.get("name") or "") in DATE_ROLES
            or str(item.get("name") or "").endswith("_date")
            or str(item.get("name") or "").endswith("_month")
        )
    ]
    if not date_fields:
        checks.append(_check("VAL-10", "passed", detail="日付項目がない"))
    else:
        roles_found = set()
        unlabeled = 0
        for item in date_fields:
            role = item.get("role") or item.get("name")
            if role in DATE_ROLES:
                roles_found.add(role)
            else:
                unlabeled += 1
        if unlabeled:
            checks.append(_check(
                "VAL-10", "needs_review",
                detail="対象月・取引日・締日の役割を区別できない",
            ))
        else:
            checks.append(_check("VAL-10", "passed", actual=",".join(sorted(roles_found))))

    seen_keys: set[tuple[str, int, str]] = set()
    duplicates = 0
    source_sha = str(source.get("sha256") or sha or "")
    for item in payload.get("fields") or []:
        if not isinstance(item, Mapping):
            continue
        page = item.get("page") if isinstance(item.get("page"), int) else 0
        row_key = str(item.get("row_key") or "")
        if not row_key:
            continue
        key = (source_sha, page, row_key)
        if key in seen_keys:
            duplicates += 1
        else:
            seen_keys.add(key)
    checks.append(_check(
        "VAL-11", "passed" if duplicates == 0 else "failed",
        expected="0", actual=str(duplicates),
        detail="" if duplicates == 0 else "同一原本SHA・ページ・行キーの重複",
    ))

    val12_ok = True
    val12_detail = ""
    for item in payload.get("fields") or []:
        if not isinstance(item, Mapping):
            continue
        state = item.get("value_state")
        if state not in {"blank", "zero", "unreadable", "value"}:
            val12_ok = False
            val12_detail = "value_state が空欄/0/読取不能/値に分離されていない"
            break
        if state == "unreadable" and field_as_zero_amount(item):
            val12_ok = False
            val12_detail = "読取不能を0円として保持している"
            break
        if state == "unreadable" and str(item.get("value") or "").strip() in {"0", "0.0", "0.00"}:
            val12_ok = False
            val12_detail = "読取不能を0として保持している"
            break
    checks.append(_check("VAL-12", "passed" if val12_ok else "failed", detail=val12_detail))
    return checks


def _important_amount_ocr_only(
    payload: Mapping[str, Any],
    regular_extraction: Mapping[str, Any] | None,
) -> bool:
    if regular_extraction:
        return False
    important = [
        item for item in (payload.get("fields") or [])
        if isinstance(item, Mapping) and str(item.get("name") or "") in IMPORTANT_AMOUNT_NAMES
        and (item.get("value_state") or "value") == "value"
    ]
    return len(important) == 1


def collect_stop_codes(
    payload: Mapping[str, Any],
    checks: Sequence[Mapping[str, Any]],
    *,
    extra_codes: Sequence[str] = (),
    regular_extraction: Mapping[str, Any] | None = None,
) -> list[str]:
    codes = [str(code) for code in extra_codes if code]
    by_id = {str(item.get("check_id")): item for item in checks}
    if by_id.get("VAL-01", {}).get("status") == "failed":
        codes.append(OcrErrorCode.OCR_SOURCE_HASH_MISMATCH.value)
    if by_id.get("VAL-02", {}).get("status") == "failed":
        codes.append(OcrErrorCode.OCR_PAGE_RENDER_FAILED.value)
    if by_id.get("VAL-07", {}).get("status") == "failed":
        codes.append(OcrErrorCode.OCR_REPETITION_DETECTED.value)
    if by_id.get("VAL-05", {}).get("status") == "failed":
        codes.append(OcrErrorCode.OCR_TOTAL_MISMATCH.value)
    if by_id.get("VAL-06", {}).get("status") in {"failed", "needs_review"} and "区別" in str(
        by_id.get("VAL-06", {}).get("detail") or ""
    ):
        codes.append(OcrErrorCode.OCR_FIELD_AMBIGUOUS.value)
    if by_id.get("VAL-06", {}).get("status") == "failed":
        codes.append(OcrErrorCode.OCR_TOTAL_MISMATCH.value)
    if by_id.get("VAL-09", {}).get("status") == "failed":
        codes.append(OcrErrorCode.OCR_FIELD_AMBIGUOUS.value)
    if by_id.get("VAL-09", {}).get("status") == "needs_review":
        codes.append(OcrErrorCode.OCR_REVIEW_REQUIRED.value)
    if by_id.get("VAL-10", {}).get("status") == "needs_review":
        codes.append(OcrErrorCode.OCR_FIELD_AMBIGUOUS.value)
    if _important_amount_ocr_only(payload, regular_extraction):
        codes.append(OcrErrorCode.OCR_REVIEW_REQUIRED.value)
    if by_id.get("VAL-03", {}).get("status") == "failed":
        codes.append(OcrErrorCode.OCR_SCHEMA_INVALID.value)
    if by_id.get("VAL-12", {}).get("status") == "failed":
        codes.append(OcrErrorCode.OCR_SCHEMA_INVALID.value)
    if by_id.get("VAL-08", {}).get("status") == "failed":
        codes.append(OcrErrorCode.OCR_FIELD_AMBIGUOUS.value)
    return list(dict.fromkeys(codes))


def decide_final_status(
    checks: Sequence[Mapping[str, Any]],
    *,
    extra_codes: Sequence[str] = (),
    skipped: bool = False,
) -> tuple[str, str]:
    """passed|needs_review|failed|skipped。強制停止が1つでもあれば passed にしない。"""
    if skipped:
        return "skipped", extra_codes[0] if extra_codes else OcrErrorCode.OCR_NOT_REQUIRED.value
    codes = [str(code) for code in extra_codes if code]
    for item in checks:
        status = item.get("status")
        check_id = item.get("check_id")
        if status == "failed" and check_id == "VAL-01":
            codes.append(OcrErrorCode.OCR_SOURCE_HASH_MISMATCH.value)
        elif status == "failed" and check_id == "VAL-02":
            codes.append(OcrErrorCode.OCR_PAGE_RENDER_FAILED.value)
        elif status == "failed" and check_id == "VAL-07":
            codes.append(OcrErrorCode.OCR_REPETITION_DETECTED.value)
        elif status == "failed" and check_id in {"VAL-05", "VAL-06"}:
            codes.append(OcrErrorCode.OCR_TOTAL_MISMATCH.value)
        elif status == "failed" and check_id == "VAL-09":
            codes.append(OcrErrorCode.OCR_FIELD_AMBIGUOUS.value)
        elif status == "failed" and check_id in {"VAL-03", "VAL-12", "VAL-11"}:
            codes.append(OcrErrorCode.OCR_SCHEMA_INVALID.value)
        elif status == "failed" and check_id == "VAL-04":
            codes.append(OcrErrorCode.OCR_FIELD_AMBIGUOUS.value)
        elif status == "needs_review":
            codes.append(OcrErrorCode.OCR_REVIEW_REQUIRED.value)
    codes = list(dict.fromkeys(codes))
    if any(code in FORCE_FAIL_CODES for code in codes):
        fail_code = next(code for code in codes if code in FORCE_FAIL_CODES)
        return "failed", fail_code
    if any(code in FORCE_REVIEW_CODES for code in codes):
        review_code = next(code for code in codes if code in FORCE_REVIEW_CODES)
        return "needs_review", review_code
    if any(follow_up_kind(code).value in {"force_stop", "reject_result", "stop_downstream"} for code in codes):
        return "failed", codes[0]
    if any(item.get("status") == "failed" for item in checks):
        return "failed", codes[0] if codes else OcrErrorCode.OCR_REVIEW_REQUIRED.value
    if any(item.get("status") == "needs_review" for item in checks) or codes:
        return "needs_review", (codes[0] if codes else OcrErrorCode.OCR_REVIEW_REQUIRED.value)
    return "passed", ""


def ocr_active_count() -> int:
    with _OCR_ACTIVE_GUARD:
        return _OCR_ACTIVE


def downstream_allowed(status: str) -> bool:
    """failed/needs_review/skipped は後続計算へ渡さない。0円へ変換しない。"""
    return status == "passed"


def amounts_for_downstream(payload: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    if not downstream_allowed(str(payload.get("status") or "")):
        return None
    rows = []
    for item in payload.get("fields") or []:
        if not isinstance(item, Mapping):
            continue
        if not field_as_zero_amount(item) and (item.get("value_state") or "") != "value":
            continue
        rows.append(dict(item))
    return rows


def _parameters_hash(parameters: Mapping[str, Any] | None) -> str:
    payload = json.dumps(parameters or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_hex(payload.encode("utf-8"))


def _empty_review(required: bool) -> dict[str, Any]:
    return {"required": required, "decision": None, "reviewer": None, "reviewed_at": None}


def _base_payload(
    *,
    status: str,
    trigger: str,
    context_file_id: str,
    filename: str,
    sha256: str,
    page_count: int,
    engine: Mapping[str, Any],
    pages: list[Any] | None = None,
    fields: list[Any] | None = None,
    checks: list[Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "trigger": trigger if trigger in {
            "garbled", "unreadable", "low_text_density", "table_required", "manual",
        } else "manual",
        "source": {
            "context_file_id": context_file_id,
            "filename": filename,
            "sha256": sha256,
            "page_count": max(1, int(page_count) if page_count else 1),
        },
        "engine": dict(engine),
        "pages": list(pages or []),
        "fields": list(fields or []),
        "checks": list(checks or []),
        "review": _empty_review(status == "needs_review"),
    }


def _normalize_block(raw: Mapping[str, Any], page_no: int, index: int) -> dict[str, Any]:
    block_id = str(raw.get("block_id") or f"p{page_no}-b{index:03d}")
    label = raw.get("label") if raw.get("label") in {"table", "text", "title", "image"} else "text"
    bbox = raw.get("bbox") if isinstance(raw.get("bbox"), (list, tuple)) and len(raw.get("bbox")) == 4 else [0, 0, 0, 0]
    confidence = raw.get("confidence")
    try:
        conf = float(confidence) if confidence is not None else 1.0
    except (TypeError, ValueError):
        conf = 0.0
    conf = min(1.0, max(0.0, conf))
    block = {
        "block_id": block_id,
        "label": label,
        "bbox": [float(x) for x in bbox],
        "text": str(raw.get("text") or ""),
        "confidence": conf,
    }
    if isinstance(raw.get("fields"), list):
        block["fields"] = list(raw["fields"])
    return block


def normalize_recognition(
    data: Any,
    *,
    page_no: int,
    default_width: float = 1653,
    default_height: float = 2339,
) -> dict[str, Any]:
    payload = data if isinstance(data, Mapping) else {"text": str(data or "")}
    blocks_raw = payload.get("blocks") if isinstance(payload.get("blocks"), list) else []
    if not blocks_raw and payload.get("text"):
        blocks_raw = [{"text": payload.get("text"), "label": "text", "bbox": [0, 0, 1, 1], "confidence": 1.0}]
    blocks = [_normalize_block(item, page_no, index) for index, item in enumerate(blocks_raw, 1) if isinstance(item, Mapping)]
    try:
        width = float(payload.get("width") or default_width)
        height = float(payload.get("height") or default_height)
    except (TypeError, ValueError):
        width, height = default_width, default_height
    return {
        "page": page_no,
        "width": width,
        "height": height,
        "blocks": blocks,
    }


def _fields_from_pages(pages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Phase 2 は正規化まで。帳票割当はしない。ブロックテキストを field 化しない金額推測もしない。"""
    fields: list[dict[str, Any]] = []
    for page in pages:
        for block in page.get("blocks") or []:
            if not isinstance(block, Mapping):
                continue
            supplied = block.get("fields")
            if not isinstance(supplied, list):
                continue
            for raw in supplied:
                if not isinstance(raw, Mapping) or not raw.get("name"):
                    continue
                unreadable = bool(raw.get("unreadable") or raw.get("value_state") == "unreadable")
                created = make_ocr_field(
                    str(raw["name"]),
                    None if unreadable and raw.get("value") in {None, "0", "0.0"} else raw.get("value"),
                    field_type=str(raw.get("type") or "string"),
                    page=int(page["page"]),
                    block_id=str(block["block_id"]),
                    unreadable=unreadable,
                    evidence_text=str(raw.get("evidence_text") or block.get("text") or ""),
                    validation=str(raw.get("validation") or ""),
                )
                item = {
                    "name": created.name,
                    "value": created.value,
                    "type": created.type,
                    "page": created.page,
                    "block_id": created.block_id,
                    "value_state": created.value_state,
                    "evidence_text": created.evidence_text,
                    "validation": created.validation,
                }
                if raw.get("row_key"):
                    item["row_key"] = str(raw["row_key"])
                if raw.get("role"):
                    item["role"] = str(raw["role"])
                if raw.get("used_for_vehicle_cost"):
                    item["used_for_vehicle_cost"] = True
                if raw.get("bbox"):
                    item["bbox"] = list(raw["bbox"])
                fields.append(item)
    return fields


def _write_artifacts(
    root: Path,
    payload: Mapping[str, Any],
    page_images: Mapping[int, bytes],
) -> None:
    write_artifact(root, "ocr_result.json", json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    lines = [f"# OCR result ({payload.get('status')})", ""]
    for page in payload.get("pages") or []:
        lines.append(f"## Page {page.get('page')}")
        for block in page.get("blocks") or []:
            lines.append(f"- {block.get('block_id')}: {block.get('text')}")
        lines.append("")
    write_artifact(root, "ocr_result.md", "\n".join(lines).encode("utf-8"))
    write_artifact(
        root, "validation.json",
        json.dumps({"checks": payload.get("checks") or []}, ensure_ascii=False, indent=2).encode("utf-8"),
    )
    for page_no, png in page_images.items():
        write_artifact(root, f"page-{page_no}.png", png)
        write_artifact(root, f"page-{page_no}-layout.png", png)
    generate_manifest(root)


def _persist_result(store: OcrStore, run_id: str, payload: Mapping[str, Any]) -> None:
    for page in payload.get("pages") or []:
        if not isinstance(page, Mapping):
            continue
        store.save_page(
            run_id, int(page.get("page") or 0),
            width=page.get("width"), height=page.get("height"),
            status="done",
        )
        for block in page.get("blocks") or []:
            if not isinstance(block, Mapping):
                continue
            store.save_block(
                run_id, int(page.get("page") or 0), str(block.get("block_id")),
                label=str(block.get("label") or "text"),
                bbox=block.get("bbox"),
                text=str(block.get("text") or ""),
                confidence=block.get("confidence"),
            )
    for index, item in enumerate(payload.get("fields") or [], 1):
        if not isinstance(item, Mapping):
            continue
        store.save_field(
            run_id, str(item.get("field_id") or f"f{index:03d}"),
            name=str(item.get("name") or ""),
            value_text=str(item.get("value") or ""),
            value_type=str(item.get("type") or "string"),
            page_no=item.get("page"),
            block_id=str(item.get("block_id") or ""),
            validation_status=str(item.get("validation") or item.get("value_state") or ""),
        )
    for item in payload.get("checks") or []:
        if not isinstance(item, Mapping):
            continue
        store.save_validation(
            run_id, str(item.get("check_id") or ""),
            status=str(item.get("status") or ""),
            expected=str(item.get("expected") or ""),
            actual=str(item.get("actual") or ""),
            detail=str(item.get("detail") or ""),
        )


def _finish_payload(
    payload: dict[str, Any],
    *,
    run_id: str,
    skip_reason: str = "",
    error_code: str = "",
    duplicate: bool = False,
    regular_extraction: Any = None,
    resumed: bool = False,
    processed_pages: Sequence[int] | None = None,
) -> dict[str, Any]:
    payload = dict(payload)
    payload["run_id"] = run_id
    payload["skip_reason"] = skip_reason
    payload["error_code"] = error_code
    payload["duplicate"] = duplicate
    payload["resumed"] = resumed
    payload["processed_pages"] = list(processed_pages or [])
    payload["downstream_allowed"] = downstream_allowed(str(payload.get("status") or ""))
    if regular_extraction is not None:
        payload["regular_extraction"] = regular_extraction
    payload["review"] = _empty_review(payload.get("status") == "needs_review")
    return payload


def run_ocr_fallback(
    *,
    original_bytes: bytes | None,
    expected_sha256: str,
    project_id: str,
    context_file_id: str,
    filename: str = "document.pdf",
    page_count: int = 1,
    pages_quality: Sequence[Mapping[str, Any]] | None = None,
    purpose: str = "context",
    known_total: Any = None,
    extracted_total: Any = None,
    user_requested_original_match: bool = False,
    store: OcrStore | None = None,
    client: OcrClient | None = None,
    renderer: Renderer | None = None,
    artifact_root: Path | None = None,
    engine_id: str = "PaddleOCR-VL-1.6",
    parameters: Mapping[str, Any] | None = None,
    regular_extraction: Mapping[str, Any] | None = None,
    vehicle_master: Sequence[str] | None = None,
    tax_rounding_rule: str | None = None,
    expected_invoice_formula: Mapping[str, Any] | None = None,
    feature_enabled: bool | None = None,
    thresholds: OcrTriggerThresholds | None = None,
    resume: bool = False,
) -> dict[str, Any]:
    """第5章 1〜10 のオーケストレーション。手順9の帳票割当と11以降は行わない。"""
    enabled = ocr_feature_enabled() if feature_enabled is None else bool(feature_enabled)
    params = dict(parameters or {})
    dpi = int(params.get("dpi") or DEFAULT_DPI)
    if dpi < MIN_DPI or dpi > MAX_DPI:
        dpi = DEFAULT_DPI
        params["dpi"] = dpi
    else:
        params["dpi"] = dpi
    parameters_hash = _parameters_hash(params)
    engine = dict(DEFAULT_ENGINE)
    engine["parameters_hash"] = parameters_hash
    sha = (expected_sha256 or "").strip().lower().replace("sha256:", "")
    pages_count = max(1, min(int(page_count or 1), MAX_PAGES))
    execution_key = make_execution_key(sha or "empty", engine_id, parameters_hash)
    def persist_run(**kwargs: Any) -> tuple[dict[str, Any], bool]:
        if store is None:
            return {"run_id": uuid.uuid4().hex, "status": kwargs.get("status", "queued")}, False
        return store.create_run(
            project_id=project_id,
            context_file_id=context_file_id,
            source_sha256=sha,
            execution_key=execution_key,
            trigger=str(kwargs.get("trigger") or ""),
            engine=engine,
            parameters_hash=parameters_hash,
            status=str(kwargs.get("status") or "queued"),
            error_code=str(kwargs.get("error_code") or ""),
        )

    if not enabled:
        run, duplicate = persist_run(status="skipped", error_code="feature_disabled")
        payload = _base_payload(
            status="skipped", trigger="manual", context_file_id=context_file_id,
            filename=filename, sha256=sha or ("0" * 64), page_count=pages_count, engine=engine,
        )
        if store is not None:
            store.update_run(run["run_id"], status="skipped", error_code="feature_disabled", finished=True)
        return _finish_payload(
            payload, run_id=run["run_id"], skip_reason="feature_disabled",
            error_code="feature_disabled", duplicate=duplicate,
            regular_extraction=regular_extraction,
        )

    if original_bytes is None or original_bytes == b"":
        run, duplicate = persist_run(status="failed", error_code="source_missing")
        payload = _base_payload(
            status="failed", trigger="unreadable", context_file_id=context_file_id,
            filename=filename, sha256=sha or ("0" * 64), page_count=pages_count, engine=engine,
        )
        payload["file_absent"] = True
        if store is not None:
            store.update_run(run["run_id"], status="failed", error_code="source_missing", finished=True)
        return _finish_payload(
            payload, run_id=run["run_id"], skip_reason="source_missing",
            error_code="source_missing", duplicate=duplicate,
            regular_extraction=regular_extraction,
        )

    hash_issues = verify_source_hash(original_bytes, sha)
    if hash_issues:
        run, duplicate = persist_run(
            status="failed", error_code=OcrErrorCode.OCR_SOURCE_HASH_MISMATCH.value,
            trigger="unreadable",
        )
        payload = _base_payload(
            status="failed", trigger="unreadable", context_file_id=context_file_id,
            filename=filename, sha256=sha, page_count=pages_count, engine=engine,
            checks=[_check("VAL-01", "failed", expected=sha, actual=sha256_hex(original_bytes))],
        )
        if store is not None:
            store.update_run(
                run["run_id"], status="failed",
                error_code=OcrErrorCode.OCR_SOURCE_HASH_MISMATCH.value, finished=True,
            )
        return _finish_payload(
            payload, run_id=run["run_id"],
            error_code=OcrErrorCode.OCR_SOURCE_HASH_MISMATCH.value, duplicate=duplicate,
            regular_extraction=regular_extraction,
        )

    decision = evaluate_ocr_triggers(
        pages_quality, purpose=purpose,
        user_requested_original_match=user_requested_original_match,
        known_total=known_total, extracted_total=extracted_total,
        thresholds=thresholds, page_count=pages_count,
    )
    trigger = decision.trigger or "manual"

    if store is not None and resume:
        incomplete = store.get_incomplete_run(project_id, context_file_id)
        if incomplete and incomplete.get("source_sha256") and incomplete["source_sha256"] != sha:
            store.update_run(
                incomplete["run_id"], status="failed",
                error_code=OcrErrorCode.OCR_SOURCE_HASH_MISMATCH.value, finished=True,
            )
            payload = _base_payload(
                status="failed", trigger=trigger, context_file_id=context_file_id,
                filename=filename, sha256=sha, page_count=pages_count, engine=engine,
            )
            return _finish_payload(
                payload, run_id=incomplete["run_id"],
                error_code=OcrErrorCode.OCR_SOURCE_HASH_MISMATCH.value,
                duplicate=True, regular_extraction=regular_extraction, resumed=True,
            )

    if store is not None:
        existing = store.get_run_by_execution_key(execution_key)
        if existing:
            if existing.get("source_sha256") and existing["source_sha256"] != sha:
                store.update_run(
                    existing["run_id"], status="failed",
                    error_code=OcrErrorCode.OCR_SOURCE_HASH_MISMATCH.value, finished=True,
                )
                payload = _base_payload(
                    status="failed", trigger=trigger, context_file_id=context_file_id,
                    filename=filename, sha256=sha, page_count=pages_count, engine=engine,
                )
                return _finish_payload(
                    payload, run_id=existing["run_id"],
                    error_code=OcrErrorCode.OCR_SOURCE_HASH_MISMATCH.value,
                    duplicate=True, regular_extraction=regular_extraction, resumed=True,
                )
            if existing.get("status") in TERMINAL_STATUSES and not resume:
                payload = _base_payload(
                    status=str(existing["status"]), trigger=str(existing.get("trigger") or trigger),
                    context_file_id=context_file_id, filename=filename, sha256=sha,
                    page_count=pages_count, engine=engine,
                )
                bundle = store.get_bundle(existing["run_id"]) or {}
                payload["pages"] = [
                    {
                        "page": row["page_no"],
                        "width": row.get("width") or 0,
                        "height": row.get("height") or 0,
                        "blocks": [
                            {
                                "block_id": block["block_id"],
                                "label": block.get("label") or "text",
                                "bbox": json.loads(block["bbox_json"]) if block.get("bbox_json") else [0, 0, 0, 0],
                                "text": block.get("text") or "",
                                "confidence": block.get("confidence") if block.get("confidence") is not None else 1.0,
                            }
                            for block in bundle.get("blocks") or []
                            if block.get("page_no") == row["page_no"]
                        ],
                    }
                    for row in bundle.get("pages") or []
                ]
                payload["fields"] = [
                    {
                        "name": item["name"],
                        "value": item.get("value_text") or "",
                        "type": item.get("value_type") or "string",
                        "page": item.get("page_no") or 1,
                        "block_id": item.get("block_id") or "p1-b001",
                        "value_state": item.get("validation_status") or "value",
                    }
                    for item in bundle.get("fields") or []
                ]
                payload["checks"] = [
                    {
                        "check_id": item["check_id"],
                        "status": item["status"],
                        "expected": item.get("expected") or "",
                        "actual": item.get("actual") or "",
                        "detail": item.get("detail") or "",
                    }
                    for item in bundle.get("validations") or []
                ]
                if payload["status"] == "skipped" and not payload["pages"]:
                    payload["pages"] = []
                    payload["fields"] = []
                return _finish_payload(
                    payload, run_id=existing["run_id"],
                    skip_reason=str(existing.get("error_code") or ""),
                    error_code=str(existing.get("error_code") or ""),
                    duplicate=True, regular_extraction=regular_extraction,
                )

    if decision.skipped:
        run, duplicate = persist_run(
            status="skipped", error_code=OcrErrorCode.OCR_NOT_REQUIRED.value, trigger=trigger,
        )
        payload = _base_payload(
            status="skipped", trigger=trigger, context_file_id=context_file_id,
            filename=filename, sha256=sha, page_count=pages_count, engine=engine,
        )
        if store is not None:
            store.update_run(
                run["run_id"], status="skipped", trigger=trigger,
                error_code=OcrErrorCode.OCR_NOT_REQUIRED.value, finished=True,
            )
        return _finish_payload(
            payload, run_id=run["run_id"],
            skip_reason=OcrErrorCode.OCR_NOT_REQUIRED.value,
            error_code=OcrErrorCode.OCR_NOT_REQUIRED.value,
            duplicate=duplicate, regular_extraction=regular_extraction,
        )

    run, duplicate = persist_run(status="queued", trigger=trigger)
    run_id = run["run_id"]
    if duplicate and run.get("status") in TERMINAL_STATUSES and not resume:
        payload = _base_payload(
            status=str(run["status"]), trigger=str(run.get("trigger") or trigger),
            context_file_id=context_file_id, filename=filename, sha256=sha,
            page_count=pages_count, engine=engine,
        )
        return _finish_payload(
            payload, run_id=run_id, error_code=str(run.get("error_code") or ""),
            duplicate=True, regular_extraction=regular_extraction,
        )

    acquired = _OCR_JOB_LOCK.acquire(timeout=DOCUMENT_TIMEOUT_SECONDS)
    if not acquired:
        if store is not None:
            store.update_run(run_id, status="failed", error_code=OcrErrorCode.OCR_TIMEOUT.value, finished=True)
        payload = _base_payload(
            status="needs_review", trigger=trigger, context_file_id=context_file_id,
            filename=filename, sha256=sha, page_count=pages_count, engine=engine,
        )
        return _finish_payload(
            payload, run_id=run_id, error_code=OcrErrorCode.OCR_TIMEOUT.value,
            duplicate=duplicate, regular_extraction=regular_extraction,
        )

    global _OCR_ACTIVE
    with _OCR_ACTIVE_GUARD:
        _OCR_ACTIVE += 1
        active_now = _OCR_ACTIVE
    if active_now > MAX_CONCURRENT_OCR:
        with _OCR_ACTIVE_GUARD:
            _OCR_ACTIVE -= 1
        _OCR_JOB_LOCK.release()
        raise RuntimeError("OCR concurrent limit exceeded")

    started = time.perf_counter()
    extra_codes: list[str] = []
    pages_out: list[dict[str, Any]] = []
    page_images: dict[int, bytes] = {}
    processed: list[int] = []
    resumed = False
    try:
        if store is not None:
            store.update_run(run_id, status="running", trigger=trigger)
            if store.get_run(run_id) and store.get_run(run_id).get("source_sha256") != sha:
                extra_codes.append(OcrErrorCode.OCR_SOURCE_HASH_MISMATCH.value)
                payload = _base_payload(
                    status="failed", trigger=trigger, context_file_id=context_file_id,
                    filename=filename, sha256=sha, page_count=pages_count, engine=engine,
                )
                store.update_run(
                    run_id, status="failed",
                    error_code=OcrErrorCode.OCR_SOURCE_HASH_MISMATCH.value, finished=True,
                )
                return _finish_payload(
                    payload, run_id=run_id,
                    error_code=OcrErrorCode.OCR_SOURCE_HASH_MISMATCH.value,
                    duplicate=duplicate, regular_extraction=regular_extraction, resumed=True,
                )
            done = store.processed_page_nos(run_id)
            if done:
                resumed = True
                for row in store.list_pages(run_id):
                    if row.get("status") in PAGE_DONE_STATUSES:
                        blocks = store.list_blocks(run_id, row["page_no"])
                        pages_out.append({
                            "page": row["page_no"],
                            "width": row.get("width") or 0,
                            "height": row.get("height") or 0,
                            "blocks": [
                                {
                                    "block_id": block["block_id"],
                                    "label": block.get("label") or "text",
                                    "bbox": json.loads(block["bbox_json"]) if block.get("bbox_json") else [0, 0, 0, 0],
                                    "text": block.get("text") or "",
                                    "confidence": block.get("confidence") if block.get("confidence") is not None else 1.0,
                                }
                                for block in blocks
                            ],
                        })
                        processed.append(int(row["page_no"]))

        ocr_client = client
        owns_client = False
        if ocr_client is None:
            extra_codes.append(OcrErrorCode.OCR_SERVICE_UNAVAILABLE.value)
        else:
            health = ocr_client.health()
            if not health.ok:
                extra_codes.append(health.error_code or OcrErrorCode.OCR_SERVICE_UNAVAILABLE.value)

        target_pages = [page for page in decision.pages if page not in set(processed)]
        if extra_codes and extra_codes[0] in {
            OcrErrorCode.OCR_SERVICE_UNAVAILABLE.value, OcrErrorCode.OCR_TIMEOUT.value,
        }:
            status, code = decide_final_status([], extra_codes=extra_codes)
            payload = _base_payload(
                status=status, trigger=trigger, context_file_id=context_file_id,
                filename=filename, sha256=sha, page_count=pages_count, engine=engine,
            )
            elapsed = int((time.perf_counter() - started) * 1000)
            if store is not None:
                store.update_run(run_id, status=status, error_code=code, finished=True, elapsed_ms=elapsed)
            return _finish_payload(
                payload, run_id=run_id, error_code=code, duplicate=duplicate,
                regular_extraction=regular_extraction, resumed=resumed,
                processed_pages=processed,
            )

        assert ocr_client is not None
        for page_no in target_pages:
            rendered = render_pdf_page(original_bytes, page_no, renderer=renderer)
            if not rendered.ok:
                extra_codes.append(rendered.error_code or OcrErrorCode.OCR_PAGE_RENDER_FAILED.value)
                if store is not None:
                    store.save_page(run_id, page_no, status="failed")
                break
            png = rendered.data
            page_images[page_no] = png
            recognized = ocr_client.recognize_page(png, page=page_no)
            if not recognized.ok:
                extra_codes.append(recognized.error_code or OcrErrorCode.OCR_SERVICE_UNAVAILABLE.value)
                if store is not None:
                    store.save_page(run_id, page_no, status="failed")
                break
            page_payload = normalize_recognition(recognized.data, page_no=page_no)
            if not page_payload["blocks"]:
                extra_codes.append(OcrErrorCode.OCR_LAYOUT_FAILED.value)
            pages_out.append(page_payload)
            processed.append(page_no)
            if store is not None:
                store.save_page(
                    run_id, page_no,
                    image_sha256=sha256_hex(png),
                    width=page_payload["width"], height=page_payload["height"],
                    status="done",
                )
                for block in page_payload["blocks"]:
                    store.save_block(
                        run_id, page_no, block["block_id"],
                        label=block["label"], bbox=block["bbox"],
                        text=block["text"], confidence=block["confidence"],
                    )

        pages_out.sort(key=lambda item: int(item.get("page") or 0))
        fields = _fields_from_pages(pages_out)
        payload = _base_payload(
            status="needs_review", trigger=trigger, context_file_id=context_file_id,
            filename=filename, sha256=sha, page_count=pages_count, engine=engine,
            pages=pages_out, fields=fields,
        )
        checks = validate_ocr_checks(
            payload, original_bytes=original_bytes, expected_sha256=sha,
            target_pages=decision.pages, tax_rounding_rule=tax_rounding_rule,
            expected_invoice_formula=expected_invoice_formula,
            vehicle_master=vehicle_master,
            regular_extraction=regular_extraction,
        )
        payload["checks"] = checks
        stop_codes = collect_stop_codes(
            payload, checks, extra_codes=extra_codes,
            regular_extraction=regular_extraction,
        )
        if artifact_root is not None and page_images:
            try:
                _write_artifacts(Path(artifact_root), payload, page_images)
                tamper = verify_manifest(Path(artifact_root))
                if tamper:
                    stop_codes.append(OcrErrorCode.OCR_ARTIFACT_TAMPERED.value)
            except (OSError, ValueError, FileNotFoundError):
                stop_codes.append(OcrErrorCode.OCR_ARTIFACT_TAMPERED.value)
        payload["status"] = "needs_review"
        schema_issues = validate_ocr_result(payload)
        if schema_issues:
            stop_codes.append(OcrErrorCode.OCR_SCHEMA_INVALID.value)
        status, code = decide_final_status(checks, extra_codes=stop_codes)
        payload["status"] = status
        payload["review"] = _empty_review(status == "needs_review")
        elapsed = int((time.perf_counter() - started) * 1000)
        if store is not None:
            store.update_run(run_id, status=status, trigger=trigger, error_code=code, finished=True, elapsed_ms=elapsed)
            _persist_result(store, run_id, payload)
        return _finish_payload(
            payload, run_id=run_id, error_code=code, duplicate=duplicate,
            regular_extraction=regular_extraction, resumed=resumed,
            processed_pages=processed,
        )
    finally:
        with _OCR_ACTIVE_GUARD:
            _OCR_ACTIVE -= 1
        _OCR_JOB_LOCK.release()
        if "owns_client" in locals() and owns_client and ocr_client is not None:
            ocr_client.close()
