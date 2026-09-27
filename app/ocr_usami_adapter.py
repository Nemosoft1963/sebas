"""宇佐美御買上明細向け OCR アダプター。

OCR の table ブロック text は HTML 表である。標準ライブラリ html.parser で
行(tr)×セル(td/th) に変換し、各セルを空白・改行でトークン化した平坦列を走査する。
HTML 文字列へ正規表現を直接当てない。

人間承認済みかつ恒等式成立のブロックだけを、文字層で欠落した車番ブロックの
補完に使う。読取不能を 0 円や assumed_zero にしない。推測しない。
passed だけでは採用しない。明細の数量・単価・金額は使わない。
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Iterator, Mapping, Sequence

from app.vehicle_auto import MoneyParseResult, compact, parse_money, text

MIN_USAMI_CONFIDENCE = 0.5
# 参考消費税は小計の10%。四捨五入・切り捨て・切り上げの差は最大1円とみなし、
# abs(tax - floor(subtotal * 0.10)) <= 1 を整合とする。浮動小数は使わず整数除算する。
TAX_RATE_TOLERANCE_YEN = 1
FOUR_DIGITS_RE = re.compile(r"^\d{4}$")
TRAILING_FOUR_DIGITS_RE = re.compile(r"(\d{4})\s*$")
TOKEN_SPLIT_RE = re.compile(r"[\s\u3000]+")
CONCAT_NUMBER_RE = re.compile(r"\d+")
# ラベルは空白除去・NFKC後の接頭で判定する(全角半角・「軽 油引取税」揺れ)。
LABEL_PREFIXES: tuple[tuple[str, str], ...] = (
    ("軽油引取税", "diesel"),
    ("参考消費税", "tax"),
    ("小計", "subtotal"),
    ("品名", "product"),
    ("車番", "plate"),
    ("合計", "total"),
)


class _TableRowParser(HTMLParser):
    """table の tr × td/th をリストにする。属性値は捨て、セル可視テキストだけ残す。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.lower()
        if name == "tr":
            self._row = []
        elif name in {"td", "th"}:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name in {"td", "th"} and self._row is not None and self._cell is not None:
            self._row.append("".join(self._cell))
            self._cell = None
        elif name == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


@dataclass(slots=True)
class _Tok:
    text: str
    kind: str | None
    number: int | None
    cell_id: int


@dataclass(slots=True)
class UsamiOcrBlock:
    plate4: str = ""
    subtotal: int | None = None
    diesel_tax: int | None = None
    tax: int | None = None
    printed_total: int | None = None
    identity_ok: bool = False
    adopted: bool = False
    reason_code: str = ""
    reasons: list[str] = field(default_factory=list)
    confidence: float | None = None
    page: int = 1
    block_id: str = ""
    run_id: str = ""
    source_locator: str = ""
    evidence_text: str = ""
    labeled_plate4: str = ""
    source_block_ids: list[str] = field(default_factory=list)
    tax_row_present: bool = False
    diesel_label_present: bool = False
    diesel_candidates: list[int] = field(default_factory=list)
    tokens: list[_Tok] = field(default_factory=list)

    @property
    def charged_amount(self) -> int | None:
        """通常経路と同じ: 小計 + 軽油引取税 + 参考消費税。検証済み税が無いときは None(0で埋めない)。"""
        if self.subtotal is None or self.diesel_tax is None or self.tax is None:
            return None
        return self.subtotal + self.diesel_tax + self.tax


def parse_table_rows(html_text: str) -> list[list[str]]:
    """HTML 表を行×セルへ。パース不能でも空リスト(捏造しない)。"""
    parser = _TableRowParser()
    try:
        parser.feed(html_text or "")
        parser.close()
    except Exception:
        return []
    return parser.rows


def nonempty_cells(row: Sequence[str]) -> list[str]:
    return [text(cell) for cell in row if text(cell)]


def _numeric_cell(value: str) -> MoneyParseResult:
    stripped = text(value).replace("(", "").replace(")", "").replace("（", "").replace("）", "")
    return parse_money(stripped)


def _is_four_digits(value: str) -> bool:
    return bool(FOUR_DIGITS_RE.fullmatch(text(value)))


def _is_detail_row(cells: Sequence[str]) -> bool:
    return len(cells) >= 2 and _is_four_digits(cells[0]) and _is_four_digits(cells[1])


def _label_kind(token_text: str) -> str | None:
    packed = compact(token_text)
    if not packed:
        return None
    for prefix, kind in LABEL_PREFIXES:
        if packed == prefix or packed.startswith(prefix):
            return kind
    return None


def _token_number(token_text: str) -> int | None:
    parsed = _numeric_cell(token_text)
    return parsed.amount if parsed.status == "value" else None


def _merge_split_labels(toks: list[_Tok]) -> list[_Tok]:
    """同一セル内で空白分割されたラベル(小 計 / 軽 油引取税)を結合する。既にラベルのトークンは結合しない。"""
    if len(toks) < 2:
        return toks
    out: list[_Tok] = []
    index = 0
    while index < len(toks):
        if toks[index].kind:
            out.append(toks[index])
            index += 1
            continue
        merged: _Tok | None = None
        acc = toks[index].text
        for follow in range(index + 1, len(toks)):
            if toks[follow].cell_id != toks[index].cell_id:
                break
            acc += toks[follow].text
            kind = _label_kind(acc)
            if kind:
                merged = _Tok(text(acc), kind, _token_number(acc), toks[index].cell_id)
                index = follow + 1
                break
        if merged is not None:
            out.append(merged)
        else:
            out.append(toks[index])
            index += 1
    return out


def _split_cell_tokens(cell: str, cell_id: int) -> list[_Tok]:
    parts = TOKEN_SPLIT_RE.split(cell.replace("\r", "\n"))
    out: list[_Tok] = []
    for raw in parts:
        piece = text(raw)
        if not piece:
            continue
        out.append(_Tok(piece, _label_kind(piece), _token_number(piece), cell_id))
    return _merge_split_labels(out)


def _window_after(tokens: Sequence[_Tok], index: int) -> list[_Tok]:
    """ラベル直後から、別セルの次ラベルまで。同一セル内の後続ラベルでは切らない。"""
    origin = tokens[index].cell_id
    out: list[_Tok] = []
    for item in tokens[index + 1:]:
        if item.kind and item.cell_id != origin:
            break
        if item.kind:
            continue
        out.append(item)
    return out


def _first_number_after(tokens: Sequence[_Tok], index: int) -> tuple[int | None, str]:
    """括弧を除いた最初の数値トークン。同一セルの後続ラベルは飛ばす。混在文字列は連結しない。"""
    origin = tokens[index].cell_id
    for item in tokens[index + 1:]:
        if item.kind and item.cell_id != origin:
            break
        if item.kind:
            continue
        if item.number is not None:
            return item.number, "ok"
        stripped = text(item.text).replace("(", "").replace(")", "").replace("（", "").replace("）", "")
        if not stripped:
            continue
        parsed = parse_money(stripped)
        if parsed.status == "value" and parsed.amount is not None:
            return parsed.amount, "ok"
    return None, "empty"


def _first_concat_number(window: Sequence[_Tok]) -> tuple[int | None, str]:
    """括弧を除いて連結した最初の数値。無い・複数で曖昧なら不採用理由。"""
    blob = "".join(item.text for item in window)
    blob = blob.replace("(", "").replace(")", "").replace("（", "").replace("）", "")
    found = CONCAT_NUMBER_RE.findall(blob)
    if not found:
        return None, "empty"
    values: list[int] = []
    for raw in found:
        parsed = parse_money(raw)
        if parsed.status == "value" and parsed.amount is not None:
            values.append(parsed.amount)
    if not values:
        return None, "empty"
    if len(set(values)) > 1:
        return values[0], "ambiguous"
    return values[0], "ok"


def _window_numbers(window: Sequence[_Tok]) -> list[int]:
    found: list[int] = []
    seen: set[int] = set()
    for item in window:
        if item.number is None:
            continue
        if item.number not in seen:
            seen.add(item.number)
            found.append(item.number)
    concat, status = _first_concat_number(window)
    if concat is not None and concat not in seen and status in {"ok", "ambiguous"}:
        found.append(concat)
    return found


def _trailing_plate4(label: _Tok, window: Sequence[_Tok]) -> str | None:
    blob = compact(label.text)
    found = TRAILING_FOUR_DIGITS_RE.search(blob)
    if found:
        return found.group(1)
    for item in window:
        blob += compact(item.text)
        found = TRAILING_FOUR_DIGITS_RE.search(blob)
        if found:
            return found.group(1)
    return None


def _confidence_of(block: Mapping[str, Any]) -> float | None:
    raw = block.get("confidence")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _iter_table_blocks(
    payload: Mapping[str, Any] | None,
) -> Iterator[tuple[int, str, float | None, list[list[str]], str]]:
    if not payload:
        return
    pages = payload.get("pages") or []
    for page in pages:
        if not isinstance(page, Mapping):
            continue
        try:
            page_no = int(page.get("page") or 1)
        except (TypeError, ValueError):
            page_no = 1
        for block in page.get("blocks") or []:
            if not isinstance(block, Mapping):
                continue
            raw = str(block.get("text") or "")
            label = str(block.get("label") or "")
            if label != "table" and "<table" not in raw.lower():
                continue
            rows = parse_table_rows(raw)
            if not rows:
                continue
            yield page_no, str(block.get("block_id") or ""), _confidence_of(block), rows, raw


def _new_open_block(plate4: str, run_id: str) -> UsamiOcrBlock:
    return UsamiOcrBlock(plate4=plate4, run_id=run_id)


def _touch_source(item: UsamiOcrBlock, page: int, block_id: str, confidence: float | None) -> None:
    if block_id and block_id not in item.source_block_ids:
        item.source_block_ids.append(block_id)
    if item.block_id == "":
        item.page = page
        item.block_id = block_id
    if confidence is not None:
        if item.confidence is None or confidence < item.confidence:
            item.confidence = confidence


def _parse_tokens(item: UsamiOcrBlock) -> None:
    tokens = item.tokens
    seen_product = False
    diesel_numbers: list[int] = []
    for index, tok in enumerate(tokens):
        if tok.kind == "product":
            seen_product = True
        if tok.kind == "subtotal" and item.subtotal is None:
            nxt = tokens[index + 1] if index + 1 < len(tokens) else None
            if nxt is None or nxt.number is None or nxt.kind is not None:
                if "小計が数値として読めない" not in item.reasons:
                    item.reasons.append("小計が数値として読めない")
                if not item.reason_code:
                    item.reason_code = "non_numeric"
            else:
                item.subtotal = nxt.number
        elif tok.kind == "tax":
            item.tax_row_present = True
            value, status = _first_number_after(tokens, index)
            if value is None or status == "empty":
                item.tax = None
                if "参考消費税が数値として読めない" not in item.reasons:
                    item.reasons.append("参考消費税が数値として読めない")
                if not item.reason_code:
                    item.reason_code = "tax_unreadable"
            else:
                item.tax = value
                if item.subtotal is not None and len(str(abs(value))) > len(str(abs(item.subtotal))):
                    item.tax = None
                    if "参考消費税の桁が異常" not in item.reasons:
                        item.reasons.append("参考消費税の桁が異常")
                    if not item.reason_code:
                        item.reason_code = "tax_unreadable"
        elif tok.kind == "diesel":
            item.diesel_label_present = True
            diesel_numbers.extend(_window_numbers(_window_after(tokens, index)))
        elif tok.kind == "plate" and not item.labeled_plate4:
            digits = _trailing_plate4(tok, _window_after(tokens, index))
            if digits:
                item.labeled_plate4 = digits
        elif seen_product and tok.kind == "total" and item.printed_total is None:
            nxt = tokens[index + 1] if index + 1 < len(tokens) else None
            if nxt is not None and nxt.number is not None and nxt.kind is None:
                item.printed_total = nxt.number
    item.diesel_candidates = list(dict.fromkeys(diesel_numbers))


def _has_printed_close(tokens: Sequence[_Tok]) -> bool:
    seen_product = False
    for index, tok in enumerate(tokens):
        if tok.kind == "product":
            seen_product = True
        if seen_product and tok.kind == "total":
            nxt = tokens[index + 1] if index + 1 < len(tokens) else None
            if nxt is not None and nxt.number is not None and nxt.kind is None:
                return True
    return False


def _finalize(item: UsamiOcrBlock) -> UsamiOcrBlock:
    _parse_tokens(item)
    reasons: list[str] = list(item.reasons)
    reason_code = item.reason_code
    diesel_label = item.diesel_label_present
    diesel_numbers = list(item.diesel_candidates)

    def note(code: str, message: str) -> None:
        nonlocal reason_code
        if message not in reasons:
            reasons.append(message)
        if not reason_code:
            reason_code = code

    if item.confidence is not None and item.confidence < MIN_USAMI_CONFIDENCE:
        note("low_confidence", "OCR信頼度が低い")

    if item.labeled_plate4 and item.plate4 and item.labeled_plate4 != item.plate4:
        note("plate_mismatch", "車番ラベルと明細行の車番が不一致")

    if item.subtotal is None:
        note("missing_subtotal", "小計が欠落")
    if item.printed_total is None:
        note("missing_printed", "印字合計が欠落")

    if item.subtotal is not None and item.printed_total is not None:
        expected = item.printed_total - item.subtotal
        if expected < 0:
            note("identity", "小計+軽油引取税と印字合計が一致しない")
            item.diesel_tax = None
        elif diesel_label:
            if expected in diesel_numbers:
                item.diesel_tax = expected
            else:
                note("identity", "小計+軽油引取税と印字合計が一致しない")
                item.diesel_tax = None
        elif expected == 0:
            item.diesel_tax = 0
        else:
            note("missing_diesel", "軽油引取税が欠落")
            item.diesel_tax = None
    elif item.diesel_tax is None and "小計が欠落" not in reasons and "印字合計が欠落" not in reasons:
        note("missing_diesel", "軽油引取税が欠落")

    if not item.tax_row_present:
        note("tax_missing", "参考消費税の行が無い")
        item.tax = None
    elif item.tax is None:
        note("tax_unreadable", "参考消費税が数値として読めない")
    elif item.subtotal is not None:
        expected_tax = item.subtotal // 10  # floor(subtotal * 0.10)。非負の円金額。
        if abs(item.tax - expected_tax) > TAX_RATE_TOLERANCE_YEN:
            note("tax_inconsistent", "参考消費税が小計の10%と整合しない")
            item.tax = None

    if item.subtotal is not None and item.diesel_tax is not None and item.printed_total is not None:
        item.identity_ok = (item.subtotal + item.diesel_tax) == item.printed_total
        if not item.identity_ok:
            note("identity", "小計+軽油引取税と印字合計が一致しない")
    else:
        item.identity_ok = False

    item.reasons = reasons
    item.reason_code = reason_code
    item.adopted = not reasons and item.identity_ok and bool(item.plate4)
    if item.run_id:
        item.source_locator = f"ocr:{item.run_id}:p{item.page}:{item.block_id}"
    else:
        item.source_locator = f"ocr:p{item.page}:{item.block_id}"
    return item


def extract_usami_blocks(
    payload: Mapping[str, Any] | None,
    *,
    run_id: str = "",
) -> list[UsamiOcrBlock]:
    """OCR 結果から宇佐美車番ブロックを構造化する。採用可否はブロックごとに reason を残す。"""
    used_run = run_id or str((payload or {}).get("run_id") or "")
    closed: list[UsamiOcrBlock] = []
    current: UsamiOcrBlock | None = None
    cell_id = 0

    for page, block_id, confidence, rows, raw in _iter_table_blocks(payload):
        for row in rows:
            cells = nonempty_cells(row)
            if not cells:
                continue
            if _is_detail_row(cells):
                plate4 = cells[1]
                if current is not None and current.plate4 != plate4:
                    closed.append(_finalize(current))
                    current = None
                if current is None:
                    current = _new_open_block(plate4, used_run)
                _touch_source(current, page, block_id, confidence)
                if raw and raw not in current.evidence_text:
                    current.evidence_text = (current.evidence_text + "\n" + raw).strip()
                continue
            if current is None:
                continue
            _touch_source(current, page, block_id, confidence)
            if raw and raw not in current.evidence_text:
                current.evidence_text = (current.evidence_text + "\n" + raw).strip()
            batch: list[_Tok] = []
            for cell in cells:
                cell_id += 1
                batch.extend(_split_cell_tokens(cell, cell_id))
            current.tokens.extend(batch)
            if _has_printed_close(current.tokens):
                closed.append(_finalize(current))
                current = None

    if current is not None:
        closed.append(_finalize(current))

    counts = Counter(item.plate4 for item in closed if item.plate4)
    for item in closed:
        if item.plate4 and counts[item.plate4] > 1:
            item.adopted = False
            if "同じ車番の重複" not in item.reasons:
                item.reasons.append("同じ車番の重複")
            item.reason_code = "duplicate_plate"
    return closed


def is_usami_ocr_approved(run: Mapping[str, Any] | None, review: Mapping[str, Any] | None) -> bool:
    """宇佐美補完は人間承認(approve / correct_and_approve)だけ。passed だけでは不可。"""
    if not run or not review:
        return False
    if str(run.get("status") or "") == "failed":
        return False
    from app.ocr_review import decision_is_approved
    return decision_is_approved(review.get("decision"))


def adoptable_usami_blocks(
    payload: Mapping[str, Any] | None,
    run: Mapping[str, Any] | None,
    review: Mapping[str, Any] | None,
) -> dict[str, UsamiOcrBlock]:
    """人間承認済みかつ adopted な車番ブロックだけ。passed のみ・failed・未承認は空。"""
    if not is_usami_ocr_approved(run, review):
        return {}
    found: dict[str, UsamiOcrBlock] = {}
    rid = str((run or {}).get("run_id") or (payload or {}).get("run_id") or "")
    for item in extract_usami_blocks(payload, run_id=rid):
        if item.adopted and item.plate4 and item.plate4 not in found:
            found[item.plate4] = item
    return found


def usami_adoptions_from_ocr(ocr_adoptions: Mapping[str, Any] | None) -> dict[str, dict[str, UsamiOcrBlock]]:
    """extract() に渡す source_id → {plate4: UsamiOcrBlock}。承認されていない採用は入れない。"""
    out: dict[str, dict[str, UsamiOcrBlock]] = {}
    for source_id, adoption in (ocr_adoptions or {}).items():
        if not isinstance(adoption, Mapping):
            continue
        payload = adoption.get("payload") if isinstance(adoption.get("payload"), Mapping) else {}
        review = adoption.get("review") if isinstance(adoption.get("review"), Mapping) else None
        run_id = str(adoption.get("run_id") or payload.get("run_id") or "")
        run = {"status": payload.get("status"), "run_id": run_id}
        blocks = adoptable_usami_blocks(payload, run, review)
        if blocks:
            out[str(source_id)] = blocks
    return out
