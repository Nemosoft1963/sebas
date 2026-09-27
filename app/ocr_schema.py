"""ocr-fallback/v1 の出力契約とエラーコード（Phase 1: スキーマと後続動作表のみ）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "ocr-fallback/v1"

STATUS_VALUES = frozenset({"passed", "needs_review", "failed", "skipped"})
TRIGGER_VALUES = frozenset({
    "garbled", "unreadable", "low_text_density", "table_required", "manual",
})
BLOCK_LABELS = frozenset({"table", "text", "title", "image"})
FIELD_VALUE_STATES = frozenset({"blank", "zero", "unreadable", "value"})

REQUIRED_TOP_KEYS = (
    "schema_version", "status", "trigger", "source", "engine",
    "pages", "fields", "checks", "review",
)
REQUIRED_SOURCE_KEYS = ("context_file_id", "filename", "sha256", "page_count")
REQUIRED_ENGINE_KEYS = (
    "pipeline", "vlm_model", "layout_model", "image_digest", "parameters_hash",
)
REQUIRED_PAGE_KEYS = ("page", "width", "height", "blocks")
REQUIRED_BLOCK_KEYS = ("block_id", "label", "bbox", "text", "confidence")
REQUIRED_FIELD_KEYS = ("name", "value", "type", "page", "block_id", "value_state")
REQUIRED_REVIEW_KEYS = ("required", "decision", "reviewer", "reviewed_at")


class OcrErrorCode(str, Enum):
    OCR_NOT_REQUIRED = "OCR_NOT_REQUIRED"
    OCR_SERVICE_UNAVAILABLE = "OCR_SERVICE_UNAVAILABLE"
    OCR_TIMEOUT = "OCR_TIMEOUT"
    OCR_SOURCE_HASH_MISMATCH = "OCR_SOURCE_HASH_MISMATCH"
    OCR_PAGE_RENDER_FAILED = "OCR_PAGE_RENDER_FAILED"
    OCR_LAYOUT_FAILED = "OCR_LAYOUT_FAILED"
    OCR_REPETITION_DETECTED = "OCR_REPETITION_DETECTED"
    OCR_SCHEMA_INVALID = "OCR_SCHEMA_INVALID"
    OCR_TOTAL_MISMATCH = "OCR_TOTAL_MISMATCH"
    OCR_FIELD_AMBIGUOUS = "OCR_FIELD_AMBIGUOUS"
    OCR_ARTIFACT_TAMPERED = "OCR_ARTIFACT_TAMPERED"
    OCR_REVIEW_REQUIRED = "OCR_REVIEW_REQUIRED"


class FollowUpKind(str, Enum):
    SKIP = "skip"
    RETRY = "retry"
    NEEDS_REVIEW = "needs_review"
    FORCE_STOP = "force_stop"
    REJECT_RESULT = "reject_result"
    FORBID_PUBLISH = "forbid_publish"
    STOP_DOWNSTREAM = "stop_downstream"


# 仕様書 第12章の後続動作。OCR_NOT_REQUIRED 以外も機械的に引ける。
FOLLOW_UP_KIND: dict[str, FollowUpKind] = {
    OcrErrorCode.OCR_NOT_REQUIRED.value: FollowUpKind.SKIP,
    OcrErrorCode.OCR_SERVICE_UNAVAILABLE.value: FollowUpKind.RETRY,
    OcrErrorCode.OCR_TIMEOUT.value: FollowUpKind.NEEDS_REVIEW,
    OcrErrorCode.OCR_SOURCE_HASH_MISMATCH.value: FollowUpKind.FORCE_STOP,
    OcrErrorCode.OCR_PAGE_RENDER_FAILED.value: FollowUpKind.FORCE_STOP,
    OcrErrorCode.OCR_LAYOUT_FAILED.value: FollowUpKind.NEEDS_REVIEW,
    OcrErrorCode.OCR_REPETITION_DETECTED.value: FollowUpKind.REJECT_RESULT,
    OcrErrorCode.OCR_SCHEMA_INVALID.value: FollowUpKind.REJECT_RESULT,
    OcrErrorCode.OCR_TOTAL_MISMATCH.value: FollowUpKind.STOP_DOWNSTREAM,
    OcrErrorCode.OCR_FIELD_AMBIGUOUS.value: FollowUpKind.NEEDS_REVIEW,
    OcrErrorCode.OCR_ARTIFACT_TAMPERED.value: FollowUpKind.FORCE_STOP,
    OcrErrorCode.OCR_REVIEW_REQUIRED.value: FollowUpKind.FORBID_PUBLISH,
}

FOLLOW_UP_LABEL: dict[str, str] = {
    OcrErrorCode.OCR_NOT_REQUIRED.value: "スキップ",
    OcrErrorCode.OCR_SERVICE_UNAVAILABLE.value: "再試行または要確認",
    OcrErrorCode.OCR_TIMEOUT.value: "要確認",
    OcrErrorCode.OCR_SOURCE_HASH_MISMATCH.value: "強制停止",
    OcrErrorCode.OCR_PAGE_RENDER_FAILED.value: "強制停止",
    OcrErrorCode.OCR_LAYOUT_FAILED.value: "要確認",
    OcrErrorCode.OCR_REPETITION_DETECTED.value: "結果不採用",
    OcrErrorCode.OCR_SCHEMA_INVALID.value: "結果不採用",
    OcrErrorCode.OCR_TOTAL_MISMATCH.value: "後続計算停止",
    OcrErrorCode.OCR_FIELD_AMBIGUOUS.value: "人間確認",
    OcrErrorCode.OCR_ARTIFACT_TAMPERED.value: "強制停止",
    OcrErrorCode.OCR_REVIEW_REQUIRED.value: "公開禁止",
}

OCR_ERROR_CODES: tuple[str, ...] = tuple(item.value for item in OcrErrorCode)


def follow_up_kind(code: str | OcrErrorCode) -> FollowUpKind:
    key = code.value if isinstance(code, OcrErrorCode) else str(code)
    return FOLLOW_UP_KIND.get(key, FollowUpKind.FORCE_STOP)


def follow_up_label(code: str | OcrErrorCode) -> str:
    key = code.value if isinstance(code, OcrErrorCode) else str(code)
    return FOLLOW_UP_LABEL.get(key, "強制停止")


def follow_up_for(code: str | OcrErrorCode) -> dict[str, str]:
    key = code.value if isinstance(code, OcrErrorCode) else str(code)
    return {
        "code": key,
        "kind": follow_up_kind(key).value,
        "label": follow_up_label(key),
    }


@dataclass(frozen=True, slots=True)
class OcrIssue:
    code: str
    message: str = ""
    path: str = ""

    @property
    def follow_up(self) -> str:
        return follow_up_kind(self.code).value

    @property
    def follow_up_label(self) -> str:
        return follow_up_label(self.code)


@dataclass(frozen=True, slots=True)
class OcrOutcome:
    """例外の代わりに返す型付き結果。呼び出し側は既存抽出を保持できる。"""
    ok: bool
    error_code: str | None = None
    message: str = ""
    data: Any = None
    retried: bool = False
    status_code: int | None = None
    issues: tuple[OcrIssue, ...] = ()

    @property
    def follow_up(self) -> str | None:
        if not self.error_code:
            return None
        return follow_up_kind(self.error_code).value

    @property
    def follow_up_label(self) -> str | None:
        if not self.error_code:
            return None
        return follow_up_label(self.error_code)


@dataclass(slots=True)
class OcrSource:
    context_file_id: str
    filename: str
    sha256: str
    page_count: int


@dataclass(slots=True)
class OcrEngine:
    pipeline: str
    vlm_model: str
    layout_model: str
    image_digest: str
    parameters_hash: str


@dataclass(slots=True)
class OcrBlock:
    block_id: str
    label: str
    bbox: list[float]
    text: str
    confidence: float


@dataclass(slots=True)
class OcrPage:
    page: int
    width: float
    height: float
    blocks: list[OcrBlock] = field(default_factory=list)


@dataclass(slots=True)
class OcrField:
    """値は文字列。空欄・0・読取不能は value_state で区別し、読取不能を 0 にしない。"""
    name: str
    value: str
    type: str
    page: int
    block_id: str
    value_state: str
    evidence_text: str = ""
    validation: str = ""


@dataclass(slots=True)
class OcrReview:
    required: bool
    decision: str | None = None
    reviewer: str | None = None
    reviewed_at: str | None = None


@dataclass(slots=True)
class OcrResult:
    schema_version: str
    status: str
    trigger: str
    source: OcrSource
    engine: OcrEngine
    pages: list[OcrPage] = field(default_factory=list)
    fields: list[OcrField] = field(default_factory=list)
    checks: list[Any] = field(default_factory=list)
    review: OcrReview = field(default_factory=lambda: OcrReview(required=True))


_ZERO_STRINGS = frozenset({"0", "0.0", "0.00", "0.000", "+0", "-0"})


def classify_field_state(raw: str | None, *, unreadable: bool = False) -> str:
    """VAL-12: blank / zero / unreadable / value。読取不能を zero にしない。"""
    if unreadable:
        return "unreadable"
    if raw is None or str(raw) == "":
        return "blank"
    text = str(raw).strip()
    if text in _ZERO_STRINGS:
        return "zero"
    return "value"


def make_ocr_field(
    name: str,
    raw: str | None,
    *,
    field_type: str = "decimal",
    page: int,
    block_id: str,
    unreadable: bool = False,
    evidence_text: str = "",
    validation: str = "",
) -> OcrField:
    """読取不能を 0 に変換する経路を持たないフィールド工場。"""
    state = classify_field_state(raw, unreadable=unreadable)
    if state == "unreadable":
        value = "" if raw is None else str(raw)
        if value.strip() in _ZERO_STRINGS:
            value = ""
        return OcrField(
            name=name, value=value, type=field_type, page=page, block_id=block_id,
            value_state="unreadable", evidence_text=evidence_text, validation=validation,
        )
    if state == "blank":
        return OcrField(
            name=name, value="", type=field_type, page=page, block_id=block_id,
            value_state="blank", evidence_text=evidence_text, validation=validation,
        )
    return OcrField(
        name=name, value=str(raw), type=field_type, page=page, block_id=block_id,
        value_state=state, evidence_text=evidence_text, validation=validation,
    )


def field_as_zero_amount(field: OcrField | Mapping[str, Any]) -> bool:
    """後続計算が 0 円とみなしてよいか。読取不能・空欄は False。"""
    state = field.value_state if isinstance(field, OcrField) else field.get("value_state")
    if state == "zero":
        return True
    return False


def _issue(path: str, message: str) -> OcrIssue:
    return OcrIssue(code=OcrErrorCode.OCR_SCHEMA_INVALID.value, message=message, path=path)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_required(obj: Any, keys: Iterable[str], path: str, errors: list[OcrIssue]) -> bool:
    if not isinstance(obj, dict):
        errors.append(_issue(path, "object である必要があります"))
        return False
    ok = True
    for key in keys:
        if key not in obj:
            errors.append(_issue(f"{path}.{key}" if path else key, "必須キーが欠落しています"))
            ok = False
    return ok


def _validate_bbox(bbox: Any, path: str, errors: list[OcrIssue]) -> None:
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        errors.append(_issue(path, "bbox は4要素の数値配列である必要があります"))
        return
    if not all(_is_number(item) for item in bbox):
        errors.append(_issue(path, "bbox の要素は数値である必要があります"))


def _validate_confidence(value: Any, path: str, errors: list[OcrIssue]) -> None:
    if not _is_number(value) or isinstance(value, bool):
        errors.append(_issue(path, "confidence は 0〜1 の数値である必要があります"))
        return
    if value < 0 or value > 1:
        errors.append(_issue(path, "confidence は 0〜1 の範囲である必要があります"))


def _validate_block(block: Any, path: str, errors: list[OcrIssue]) -> None:
    if not _check_required(block, REQUIRED_BLOCK_KEYS, path, errors):
        if isinstance(block, dict):
            if "bbox" in block:
                _validate_bbox(block.get("bbox"), f"{path}.bbox", errors)
            if "confidence" in block:
                _validate_confidence(block.get("confidence"), f"{path}.confidence", errors)
        return
    if not isinstance(block.get("block_id"), str) or not block["block_id"]:
        errors.append(_issue(f"{path}.block_id", "block_id は空でない文字列である必要があります"))
    if block.get("label") not in BLOCK_LABELS:
        errors.append(_issue(f"{path}.label", "label が許容値ではありません"))
    _validate_bbox(block.get("bbox"), f"{path}.bbox", errors)
    if not isinstance(block.get("text"), str):
        errors.append(_issue(f"{path}.text", "text は文字列である必要があります"))
    _validate_confidence(block.get("confidence"), f"{path}.confidence", errors)


def _validate_page(page: Any, path: str, errors: list[OcrIssue]) -> None:
    if not _check_required(page, REQUIRED_PAGE_KEYS, path, errors):
        if isinstance(page, dict) and isinstance(page.get("blocks"), list):
            for index, block in enumerate(page["blocks"]):
                _validate_block(block, f"{path}.blocks[{index}]", errors)
        return
    if not isinstance(page.get("page"), int) or isinstance(page.get("page"), bool) or page["page"] < 1:
        errors.append(_issue(f"{path}.page", "page は 1 以上の整数である必要があります"))
    if not _is_number(page.get("width")) or not _is_number(page.get("height")):
        errors.append(_issue(f"{path}.width", "width/height は数値である必要があります"))
    blocks = page.get("blocks")
    if not isinstance(blocks, list):
        errors.append(_issue(f"{path}.blocks", "blocks は配列である必要があります"))
        return
    for index, block in enumerate(blocks):
        _validate_block(block, f"{path}.blocks[{index}]", errors)


def _validate_field(item: Any, path: str, errors: list[OcrIssue]) -> None:
    if not _check_required(item, REQUIRED_FIELD_KEYS, path, errors):
        return
    if not isinstance(item.get("name"), str) or not item["name"]:
        errors.append(_issue(f"{path}.name", "name は空でない文字列である必要があります"))
    if not isinstance(item.get("value"), str):
        errors.append(_issue(f"{path}.value", "value は文字列で保持する必要があります"))
    if not isinstance(item.get("type"), str) or not item["type"]:
        errors.append(_issue(f"{path}.type", "type は空でない文字列である必要があります"))
    if not isinstance(item.get("page"), int) or isinstance(item.get("page"), bool) or item["page"] < 1:
        errors.append(_issue(f"{path}.page", "page は 1 以上の整数である必要があります"))
    if not isinstance(item.get("block_id"), str) or not item["block_id"]:
        errors.append(_issue(f"{path}.block_id", "block_id は必須の空でない文字列です"))
    state = item.get("value_state")
    if state not in FIELD_VALUE_STATES:
        errors.append(_issue(f"{path}.value_state", "value_state が許容値ではありません"))
    if state == "unreadable" and str(item.get("value", "")).strip() in _ZERO_STRINGS:
        errors.append(_issue(
            f"{path}.value",
            "読取不能を 0 として保持してはなりません",
        ))


def validate_ocr_result(payload: dict) -> list[OcrIssue]:
    """ocr-fallback/v1 を検査し、違反は OCR_SCHEMA_INVALID として返す。"""
    errors: list[OcrIssue] = []
    if not isinstance(payload, dict):
        return [_issue("", "OCR結果は object である必要があります")]
    _check_required(payload, REQUIRED_TOP_KEYS, "", errors)
    if "schema_version" in payload and payload.get("schema_version") != SCHEMA_VERSION:
        errors.append(_issue("schema_version", "schema_version が ocr-fallback/v1 ではありません"))
    if "status" in payload and payload.get("status") not in STATUS_VALUES:
        errors.append(_issue("status", "status が許容値ではありません"))
    if "trigger" in payload and payload.get("trigger") not in TRIGGER_VALUES:
        errors.append(_issue("trigger", "trigger が許容値ではありません"))
    source = payload.get("source")
    if "source" in payload:
        if _check_required(source, REQUIRED_SOURCE_KEYS, "source", errors) and isinstance(source, dict):
            if not isinstance(source.get("page_count"), int) or isinstance(source.get("page_count"), bool) or source["page_count"] < 1:
                errors.append(_issue("source.page_count", "page_count は 1 以上の整数である必要があります"))
    engine = payload.get("engine")
    if "engine" in payload:
        _check_required(engine, REQUIRED_ENGINE_KEYS, "engine", errors)
    pages = payload.get("pages")
    if "pages" in payload:
        if not isinstance(pages, list):
            errors.append(_issue("pages", "pages は配列である必要があります"))
        else:
            for index, page in enumerate(pages):
                _validate_page(page, f"pages[{index}]", errors)
    fields = payload.get("fields")
    if "fields" in payload:
        if not isinstance(fields, list):
            errors.append(_issue("fields", "fields は配列である必要があります"))
        else:
            for index, item in enumerate(fields):
                _validate_field(item, f"fields[{index}]", errors)
    if "checks" in payload and not isinstance(payload.get("checks"), list):
        errors.append(_issue("checks", "checks は配列である必要があります"))
    if "review" in payload:
        _check_required(payload.get("review"), REQUIRED_REVIEW_KEYS, "review", errors)
    return errors
