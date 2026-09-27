"""OCR-3 Phase 3: 請求・入金・当月買上を分離する帳票アダプター。

仕様 第6章・第7章・10.1・13.3。前月請求・入金・繰越を当月費用へ配賦しない。
読取不能を 0 円や assumed_zero にしない。業務未確定値は推測しない。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from app.ocr_schema import OcrErrorCode, classify_field_state, field_as_zero_amount
from app.vehicle_auto import (
    AdapterResult,
    InvoiceDocument,
    MoneyParseResult,
    company,
    document_role_of,
    extract_invoice_number,
    month,
    parse_money,
    source_vendor,
)

SCHEMA_VERSION = "ocr-fallback/v1"

# 仕様 第6章。別名は既存 Phase 2 のフィールド名と揃える。
CHAPTER6_FIELDS: tuple[str, ...] = (
    "previous_invoice_amount",
    "payment_amount",
    "carryover_amount",
    "current_purchase_amount",
    "current_invoice_amount",
    "tax_amount",
    "discount_amount",
    "purchase_date",
    "payment_date",
    "vehicle_number",
    "product",
    "quantity",
    "unit_price",
    "line_amount",
    "receipt_number",
)

CANONICAL_ALIASES: dict[str, str] = {
    "previous_invoice_amount": "previous_invoice_amount",
    "previous_balance": "previous_invoice_amount",
    "前月ご請求額": "previous_invoice_amount",
    "前月請求額": "previous_invoice_amount",
    "前月ご請求": "previous_invoice_amount",
    "前月残": "previous_invoice_amount",
    "payment_amount": "payment_amount",
    "deposit_amount": "payment_amount",
    "当月ご入金額": "payment_amount",
    "当月入金額": "payment_amount",
    "ご入金額": "payment_amount",
    "入金額": "payment_amount",
    "carryover_amount": "carryover_amount",
    "差引繰越額": "carryover_amount",
    "繰越額": "carryover_amount",
    "差引繰越": "carryover_amount",
    "current_purchase_amount": "current_purchase_amount",
    "当月お買上額": "current_purchase_amount",
    "当月買上額": "current_purchase_amount",
    "お買上額": "current_purchase_amount",
    "current_invoice_amount": "current_invoice_amount",
    "当月ご請求額": "current_invoice_amount",
    "当月請求額": "current_invoice_amount",
    "tax_amount": "tax_amount",
    "税額": "tax_amount",
    "消費税": "tax_amount",
    "参考消費税": "tax_amount",
    "discount_amount": "discount_amount",
    "adjustment_amount": "discount_amount",
    "値引": "discount_amount",
    "値引き": "discount_amount",
    "調整額": "discount_amount",
    "purchase_date": "purchase_date",
    "購入日": "purchase_date",
    "ご利用日": "purchase_date",
    "利用日": "purchase_date",
    "payment_date": "payment_date",
    "支払日": "payment_date",
    "入金日": "payment_date",
    "vehicle_number": "vehicle_number",
    "vehicle_id": "vehicle_number",
    "車両番号": "vehicle_number",
    "車番": "vehicle_number",
    "product": "product",
    "商品": "product",
    "品目": "product",
    "quantity": "quantity",
    "数量": "quantity",
    "unit_price": "unit_price",
    "単価": "unit_price",
    "line_amount": "line_amount",
    "detail_amount": "line_amount",
    "明細金額": "line_amount",
    "receipt_number": "receipt_number",
    "レシート番号": "receipt_number",
    "fuel_usage": "fuel_usage",
    "当月燃料使用量": "fuel_usage",
    "燃料使用量": "fuel_usage",
}

# 最長一致。短い「ご請求額」「金額」は当月/前月を特定できない。
HEADING_PATTERNS: tuple[tuple[str, str], ...] = tuple(sorted(
    ((alias, canon) for alias, canon in CANONICAL_ALIASES.items() if not alias.isascii() or alias != canon),
    key=lambda item: len(item[0]),
    reverse=True,
))

NOT_CURRENT_MONTH_FIELDS = frozenset({
    "previous_invoice_amount",
    "payment_amount",
    "carryover_amount",
})
CURRENT_MONTH_CHARGE_FIELDS = frozenset({
    "current_purchase_amount",
    "current_invoice_amount",
    "line_amount",
})
AMOUNT_FIELDS = frozenset({
    "previous_invoice_amount", "payment_amount", "carryover_amount",
    "current_purchase_amount", "current_invoice_amount", "tax_amount",
    "discount_amount", "unit_price", "line_amount", "quantity",
})
AMBIGUOUS_NAMES = frozenset({
    "amount", "total", "金額", "合計", "ご請求額", "請求額", "請求金額", "合計金額",
})
UNDETERMINED_BUSINESS_FIELDS = frozenset({"tax_amount", "discount_amount"})

NOT_CURRENT_MONTH_REASONS = {
    "previous_invoice_amount": "前月請求額は当月費用へ配賦しない",
    "payment_amount": "当月入金額は前月請求への入金であり、当月の車両別費用へ配賦しない",
    "carryover_amount": "差引繰越額は当月費用へ配賦しない",
}

# OCR value_state → parse_money.status。読取不能を value/0 にしない。
OCR_STATE_TO_PARSE_MONEY: dict[str, str] = {
    "value": "value",
    "zero": "value",
    "blank": "blank",
    "unreadable": "invalid",
}


@dataclass(slots=True)
class AdaptedField:
    name: str
    value: str
    state: str
    page: int
    block_id: str
    bbox: list[float] | None = None
    evidence_text: str = ""
    error_code: str = ""
    row_key: str = ""
    used_for_vehicle_cost: bool = False

    def as_dict(self) -> dict[str, Any]:
        row = {
            "name": self.name,
            "value": self.value,
            "value_state": self.state,
            "state": self.state,
            "page": self.page,
            "block_id": self.block_id,
            "evidence_text": self.evidence_text,
        }
        if self.bbox is not None:
            row["bbox"] = list(self.bbox)
        if self.error_code:
            row["error_code"] = self.error_code
        if self.row_key:
            row["row_key"] = self.row_key
        if self.used_for_vehicle_cost:
            row["used_for_vehicle_cost"] = True
        return row


@dataclass(slots=True)
class VehicleLine:
    vehicle_number: AdaptedField | None = None
    product: AdaptedField | None = None
    quantity: AdaptedField | None = None
    unit_price: AdaptedField | None = None
    line_amount: AdaptedField | None = None
    receipt_number: AdaptedField | None = None
    purchase_date: AdaptedField | None = None
    unallocated: bool = True
    used_for_vehicle_cost: bool = False
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        def dump(item: AdaptedField | None) -> dict[str, Any] | None:
            return item.as_dict() if item is not None else None
        return {
            "vehicle_number": dump(self.vehicle_number),
            "product": dump(self.product),
            "quantity": dump(self.quantity),
            "unit_price": dump(self.unit_price),
            "line_amount": dump(self.line_amount),
            "receipt_number": dump(self.receipt_number),
            "purchase_date": dump(self.purchase_date),
            "unallocated": self.unallocated,
            "used_for_vehicle_cost": self.used_for_vehicle_cost,
            "reason": self.reason,
        }


@dataclass(slots=True)
class AdaptedInvoice:
    fields: dict[str, AdaptedField] = field(default_factory=dict)
    extra_fields: list[AdaptedField] = field(default_factory=list)
    current_month_charges: list[AdaptedField] = field(default_factory=list)
    not_current_month: list[dict[str, Any]] = field(default_factory=list)
    vehicle_lines: list[VehicleLine] = field(default_factory=list)
    unallocated_lines: list[VehicleLine] = field(default_factory=list)
    ambiguous: list[AdaptedField] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    current_month_new_fuel_amount: int | None = None
    file_absent: bool = False
    roles_ambiguous: bool = False
    force_stop_downstream: bool = False
    source: dict[str, Any] = field(default_factory=dict)
    run_id: str = ""
    status: str = ""

    def field(self, name: str) -> AdaptedField | None:
        return self.fields.get(name)


@dataclass(frozen=True, slots=True)
class OcrGateResult:
    blocked: bool
    noop: bool = False
    issues: tuple[dict[str, Any], ...] = ()
    allowed_run_ids: tuple[str, ...] = ()
    downstream_values: Any = None
    reason: str = ""

    @property
    def downstream_allowed(self) -> bool:
        return (not self.blocked) and self.downstream_values is not None


def ocr_state_to_parse_money(item: AdaptedField | Mapping[str, Any]) -> MoneyParseResult:
    """OCR の空欄・0・読取不能・値を parse_money の状態へ対応付ける。

    unreadable → invalid (reason=unreadable)。0 にも assumed_zero にもしない。
    blank → blank。zero → value / amount=0。
    """
    if isinstance(item, AdaptedField):
        state, raw, raw_type = item.state, item.value, "ocr"
    else:
        state = str(item.get("value_state") or item.get("state") or "")
        raw = item.get("value")
        raw_type = "ocr"
    if state == "unreadable":
        return MoneyParseResult("invalid", None, str(raw or ""), raw_type, "unreadable")
    if state == "blank":
        return MoneyParseResult("blank", None, str(raw or ""), raw_type, "blank")
    if state == "zero":
        return MoneyParseResult("value", 0, str(raw if raw not in (None, "") else "0"), raw_type)
    parsed = parse_money(raw)
    if parsed.status == "value":
        return parsed
    if parsed.status == "blank":
        return MoneyParseResult("blank", None, parsed.raw_text, raw_type, "blank")
    return MoneyParseResult(parsed.status, None, parsed.raw_text, raw_type, parsed.reason or "non_numeric")


def _blocks_by_id(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for page in payload.get("pages") or []:
        if not isinstance(page, Mapping):
            continue
        for block in page.get("blocks") or []:
            if isinstance(block, Mapping) and block.get("block_id"):
                row = dict(block)
                row["_page"] = page.get("page")
                found[str(block["block_id"])] = row
    return found


def _canonical_name(raw_name: str, evidence: str = "") -> str | None:
    key = str(raw_name or "").strip()
    if key in CANONICAL_ALIASES:
        return CANONICAL_ALIASES[key]
    blob = f"{key} {evidence or ''}"
    for heading, canon in HEADING_PATTERNS:
        if heading and heading in blob:
            return canon
    return None


def _is_ambiguous_name(name: str) -> bool:
    packed = str(name or "").strip()
    if packed in AMBIGUOUS_NAMES:
        return True
    if packed in {"amount", "total"}:
        return True
    return False


def _bbox_of(raw: Mapping[str, Any], blocks: Mapping[str, Mapping[str, Any]]) -> list[float] | None:
    bbox = raw.get("bbox")
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        try:
            return [float(x) for x in bbox]
        except (TypeError, ValueError):
            return None
    block = blocks.get(str(raw.get("block_id") or ""))
    if not block:
        return None
    bbox = block.get("bbox")
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        try:
            return [float(x) for x in bbox]
        except (TypeError, ValueError):
            return None
    return None


def _adapted_from_raw(raw: Mapping[str, Any], blocks: Mapping[str, Mapping[str, Any]], name: str) -> AdaptedField:
    state = str(raw.get("value_state") or "")
    if state not in {"blank", "zero", "unreadable", "value"}:
        state = classify_field_state(raw.get("value"), unreadable=bool(raw.get("unreadable")))
    page = raw.get("page")
    try:
        page_no = int(page) if page is not None else 1
    except (TypeError, ValueError):
        page_no = 1
    if page_no < 1:
        page_no = 1
    block_id = str(raw.get("block_id") or "")
    return AdaptedField(
        name=name,
        value="" if raw.get("value") is None else str(raw.get("value")),
        state=state,
        page=page_no,
        block_id=block_id,
        bbox=_bbox_of(raw, blocks),
        evidence_text=str(raw.get("evidence_text") or ""),
        row_key=str(raw.get("row_key") or ""),
        used_for_vehicle_cost=bool(raw.get("used_for_vehicle_cost")),
    )


def _issue(code: str, message: str, **extra: Any) -> dict[str, Any]:
    item = {"code": code, "message": message, "kind": "ocr"}
    item.update(extra)
    return item


def _yen(field: AdaptedField | None) -> int | None:
    if field is None:
        return None
    parsed = ocr_state_to_parse_money(field)
    if parsed.status != "value":
        return None
    return parsed.amount


def _group_vehicle_lines(line_fields: Sequence[AdaptedField]) -> list[VehicleLine]:
    grouped: dict[str, dict[str, AdaptedField]] = {}
    order: list[str] = []
    for item in line_fields:
        key = item.row_key or f"{item.page}:{item.block_id}"
        if key not in grouped:
            grouped[key] = {}
            order.append(key)
        grouped[key][item.name] = item
    lines: list[VehicleLine] = []
    for key in order:
        parts = grouped[key]
        number = parts.get("vehicle_number")
        amount = parts.get("line_amount")
        has_number = bool(number and number.state == "value" and str(number.value or "").strip())
        line = VehicleLine(
            vehicle_number=number,
            product=parts.get("product"),
            quantity=parts.get("quantity"),
            unit_price=parts.get("unit_price"),
            line_amount=amount,
            receipt_number=parts.get("receipt_number"),
            purchase_date=parts.get("purchase_date"),
            unallocated=not has_number,
            used_for_vehicle_cost=has_number,
            reason="" if has_number else "車両番号が存在しない行は車両原価明細に変換しない",
        )
        lines.append(line)
    return lines


def adapt_ocr_invoice(payload: Mapping[str, Any] | None) -> AdaptedInvoice:
    """ocr-fallback/v1 の fields/blocks から第6章項目を別フィールドとして構造化する。"""
    result = AdaptedInvoice()
    if not payload or not isinstance(payload, Mapping):
        result.issues.append(_issue(OcrErrorCode.OCR_SCHEMA_INVALID.value, "OCR結果がありません"))
        result.force_stop_downstream = True
        return result
    result.status = str(payload.get("status") or "")
    result.run_id = str(payload.get("run_id") or "")
    result.file_absent = bool(payload.get("file_absent"))
    source = payload.get("source") if isinstance(payload.get("source"), Mapping) else {}
    result.source = dict(source)
    if result.file_absent:
        result.issues.append(_issue(
            "file_absent",
            "原本ファイルがありません。読取不能ファイルとは区別します",
        ))
        result.force_stop_downstream = True
        return result

    blocks = _blocks_by_id(payload)
    named: dict[str, AdaptedField] = {}
    extras: list[AdaptedField] = []
    ambiguous: list[AdaptedField] = []
    line_fields: list[AdaptedField] = []

    for raw in payload.get("fields") or []:
        if not isinstance(raw, Mapping):
            continue
        raw_name = str(raw.get("name") or "")
        evidence = str(raw.get("evidence_text") or "")
        if _is_ambiguous_name(raw_name) and not _canonical_name(raw_name, evidence):
            item = _adapted_from_raw(raw, blocks, raw_name or "amount")
            item.error_code = OcrErrorCode.OCR_FIELD_AMBIGUOUS.value
            ambiguous.append(item)
            continue
        canon = _canonical_name(raw_name, evidence)
        if canon is None:
            if raw.get("type") in {"decimal", "amount", "money", "yen"} or str(raw_name).endswith("_amount"):
                item = _adapted_from_raw(raw, blocks, raw_name or "amount")
                item.error_code = OcrErrorCode.OCR_FIELD_AMBIGUOUS.value
                ambiguous.append(item)
            else:
                extras.append(_adapted_from_raw(raw, blocks, raw_name or "unknown"))
            continue
        item = _adapted_from_raw(raw, blocks, canon)
        if canon in {"vehicle_number", "product", "quantity", "unit_price", "line_amount", "receipt_number"} and item.row_key:
            line_fields.append(item)
        if canon in named and canon not in {"vehicle_number", "product", "quantity", "unit_price", "line_amount", "receipt_number"}:
            # 同一見出しの重複は区別不能
            item.error_code = OcrErrorCode.OCR_FIELD_AMBIGUOUS.value
            ambiguous.append(item)
            continue
        if canon not in named:
            named[canon] = item
        elif item.row_key:
            line_fields.append(item)

    result.fields = named
    result.extra_fields = extras
    result.ambiguous = ambiguous

    for name, item in named.items():
        if name in NOT_CURRENT_MONTH_FIELDS:
            result.not_current_month.append({
                "name": name,
                "field": item.as_dict(),
                "reason": NOT_CURRENT_MONTH_REASONS.get(name, "当月費用へ配賦しない"),
                "amount": _yen(item),
            })
        elif name in CURRENT_MONTH_CHARGE_FIELDS:
            result.current_month_charges.append(item)
        elif name in UNDETERMINED_BUSINESS_FIELDS:
            result.issues.append(_issue(
                OcrErrorCode.OCR_REVIEW_REQUIRED.value,
                "税・値引の当月費用化は業務仕様が未確定のため配賦せず要確認にします",
                field=name, page=item.page, block_id=item.block_id,
            ))

    vehicle_lines = _group_vehicle_lines(line_fields)
    # 車両番号フィールドだけが単独である（明細なし）場合は原価行にしない
    if not vehicle_lines and named.get("vehicle_number"):
        number = named["vehicle_number"]
        has_number = number.state == "value" and str(number.value or "").strip()
        vehicle_lines.append(VehicleLine(
            vehicle_number=number,
            unallocated=not has_number,
            used_for_vehicle_cost=False,
            reason="車両購入明細なし" if has_number else "車両番号が存在しない行は車両原価明細に変換しない",
        ))
    for line in vehicle_lines:
        if line.used_for_vehicle_cost:
            result.vehicle_lines.append(line)
            if line.line_amount is not None:
                result.current_month_charges.append(line.line_amount)
        else:
            result.unallocated_lines.append(line)

    if ambiguous:
        result.roles_ambiguous = True
        result.issues.append(_issue(
            OcrErrorCode.OCR_FIELD_AMBIGUOUS.value,
            "見出しから項目の意味が特定できない金額は割り当てず人間確認にします",
            count=len(ambiguous),
        ))
        labeled_roles = {name for name in named if name in NOT_CURRENT_MONTH_FIELDS | CURRENT_MONTH_CHARGE_FIELDS}
        if len(labeled_roles) < 4:
            result.force_stop_downstream = True
            result.issues.append(_issue(
                OcrErrorCode.OCR_FIELD_AMBIGUOUS.value,
                "当月額と前月額・入金額の区別ができないため後続計算を停止します",
            ))

    purchase = named.get("current_purchase_amount")
    if any(line.line_amount and line.line_amount.state == "unreadable" for line in result.vehicle_lines):
        result.current_month_new_fuel_amount = None
        result.issues.append(_issue(
            "unreadable",
            "読取不能の明細金額を0円として確定しません",
            kind="read",
        ))
    elif result.vehicle_lines:
        total = 0
        ok = True
        for line in result.vehicle_lines:
            yen = _yen(line.line_amount)
            if yen is None and line.line_amount is not None and line.line_amount.state not in {"blank"}:
                ok = False
                break
            total += yen or 0
        result.current_month_new_fuel_amount = total if ok else None
    elif purchase is not None:
        if purchase.state == "unreadable":
            result.current_month_new_fuel_amount = None
            result.issues.append(_issue(
                "unreadable",
                "当月買上額が読取不能です。0円として確定しません",
                kind="read", page=purchase.page, block_id=purchase.block_id,
            ))
        elif purchase.state == "blank":
            result.current_month_new_fuel_amount = None
            result.issues.append(_issue(
                "blank_purchase",
                "当月買上額が空欄です。空欄を0円として扱いません",
                kind="read", page=purchase.page, block_id=purchase.block_id,
            ))
        else:
            result.current_month_new_fuel_amount = _yen(purchase)
    else:
        # 当月買上フィールドも車両明細もない。0円と仮定しない。
        result.current_month_new_fuel_amount = None

    return result


def printed_total_from_adapted(adapted: AdaptedInvoice) -> int | None:
    """printed_total は当月請求額のみ。前月請求額・入金額は使わない。"""
    current = adapted.fields.get("current_invoice_amount")
    if current is None:
        return None
    if current.state in {"blank", "unreadable"}:
        return None
    return _yen(current)


def to_invoice_document(
    adapted: AdaptedInvoice,
    source: Mapping[str, Any],
    content: str = "",
    *,
    fingerprints: Sequence[str] | None = None,
) -> InvoiceDocument:
    src = dict(source)
    filename = str(src.get("filename") or adapted.source.get("filename") or "")
    printed = printed_total_from_adapted(adapted)
    issues = list(adapted.issues)
    complete = (
        not adapted.force_stop_downstream
        and not adapted.roles_ambiguous
        and not adapted.file_absent
        and printed is not None
        and not any(item.get("code") == "unreadable" for item in adapted.issues)
    )
    return InvoiceDocument(
        source_ref="context:" + str(src.get("id") or adapted.source.get("context_file_id") or ""),
        vendor=source_vendor(src, content),
        company=company(filename) or company(content),
        billing_month=month(filename) or "",
        invoice_number=extract_invoice_number(content, filename),
        invoice_date=None,
        printed_total=printed,
        detail_count_printed=len(adapted.vehicle_lines) or None,
        document_role=document_role_of(src),
        detail_fingerprints=list(fingerprints or []),
        extraction_complete=complete,
        extraction_issues=issues,
    )


def to_adapter_result(adapted: AdaptedInvoice) -> AdapterResult:
    printed = printed_total_from_adapted(adapted)
    parsed_total = 0
    parsed_count = 0
    for line in adapted.vehicle_lines:
        yen = _yen(line.line_amount)
        if yen is None:
            continue
        parsed_total += yen
        parsed_count += 1
    if parsed_count == 0 and adapted.current_month_new_fuel_amount is not None:
        parsed_total = adapted.current_month_new_fuel_amount
    complete = (
        not adapted.force_stop_downstream
        and not adapted.roles_ambiguous
        and printed is not None
        and not adapted.file_absent
    )
    return AdapterResult(
        matched=not adapted.file_absent,
        records_added=parsed_count,
        parsed_total=parsed_total,
        printed_total=printed,
        expected_detail_count=len(adapted.vehicle_lines) or 0,
        parsed_detail_count=parsed_count,
        complete=complete,
        issues=list(adapted.issues),
    )


def current_month_source_control(
    adapted: AdaptedInvoice,
    source: Mapping[str, Any],
    *,
    mon: str,
    category: str = "fuel",
) -> dict[str, Any] | None:
    """独立原本照合用。当月請求額だけを source_total にする。"""
    printed = printed_total_from_adapted(adapted)
    current = adapted.fields.get("current_invoice_amount")
    if printed is None or current is None:
        return None
    return {
        "source_ref": "context:" + str(source.get("id") or adapted.source.get("context_file_id") or ""),
        "company": company(str(source.get("filename") or "")) or "未特定",
        "month": mon,
        "category": category,
        "tax_basis": "inclusive",
        "invoice_id": None,
        "control_kind": "invoice_total",
        "origin": "source_total",
        "amount": printed,
        "source_locator": f"ocr:{adapted.run_id}:p{current.page}:{current.block_id}",
        "extraction_method": "ocr_invoice_adapter:v1",
        "status": "available",
    }


def is_ocr_adoptable(
    run: Mapping[str, Any] | None,
    review: Mapping[str, Any] | None = None,
) -> bool:
    """passed、または人間承認済み(ocr_reviews.decision='approved')だけ採用する。"""
    if not run:
        return False
    if str(run.get("status") or "") == "passed":
        return True
    decision = str(review.get("decision") or "") if review else ""
    if decision in {"approved", "correct_and_approve"}:
        return True
    return False


def payload_from_ocr_bundle(bundle: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not bundle or not bundle.get("run"):
        return None
    run = bundle["run"]
    pages = []
    for row in bundle.get("pages") or []:
        page_no = row.get("page_no")
        blocks = [
            {
                "block_id": block.get("block_id") or "",
                "label": block.get("label") or "text",
                "bbox": _json_list(block.get("bbox_json")),
                "text": block.get("text") or "",
                "confidence": block.get("confidence") if block.get("confidence") is not None else 1.0,
            }
            for block in bundle.get("blocks") or []
            if block.get("page_no") == page_no
        ]
        pages.append({
            "page": page_no,
            "width": row.get("width") or 0,
            "height": row.get("height") or 0,
            "blocks": blocks,
        })
    fields = []
    for item in bundle.get("fields") or []:
        fields.append({
            "name": item.get("name") or "",
            "value": item.get("value_text") or "",
            "type": item.get("value_type") or "string",
            "page": item.get("page_no") or 1,
            "block_id": item.get("block_id") or "",
            "value_state": item.get("validation_status") or "value",
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "status": str(run.get("status") or ""),
        "trigger": str(run.get("trigger") or "manual"),
        "run_id": str(run.get("run_id") or ""),
        "source": {
            "context_file_id": str(run.get("context_file_id") or ""),
            "filename": "",
            "sha256": str(run.get("source_sha256") or ""),
            "page_count": max(1, len(pages) or 1),
        },
        "engine": {},
        "pages": pages,
        "fields": fields,
        "checks": [
            {
                "check_id": item.get("check_id"),
                "status": item.get("status"),
                "expected": item.get("expected") or "",
                "actual": item.get("actual") or "",
                "detail": item.get("detail") or "",
            }
            for item in bundle.get("validations") or []
        ],
        "review": {
            "required": str(run.get("status") or "") == "needs_review",
            "decision": (bundle.get("review") or {}).get("decision") if bundle.get("review") else None,
            "reviewer": (bundle.get("review") or {}).get("reviewer") if bundle.get("review") else None,
            "reviewed_at": (bundle.get("review") or {}).get("reviewed_at") if bundle.get("review") else None,
        },
        "file_absent": str(run.get("error_code") or "") == "source_missing",
        "error_code": str(run.get("error_code") or ""),
    }


def _json_list(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw:
        import json
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return [0, 0, 0, 0]
        return value if isinstance(value, list) else [0, 0, 0, 0]
    return [0, 0, 0, 0]


def latest_ocr_run(store: Any, project_id: str, context_file_id: str) -> dict[str, Any] | None:
    if store is None:
        return None
    with store.connect() as db:
        row = db.execute(
            """SELECT * FROM ocr_runs
               WHERE project_id=? AND context_file_id=?
               ORDER BY started_at DESC, run_id DESC
               LIMIT 1""",
            (project_id, context_file_id),
        ).fetchone()
    return dict(row) if row else None


def _source_requires_ocr(source: Mapping[str, Any]) -> bool:
    filename = str(source.get("filename") or "").lower()
    if not filename.endswith(".pdf"):
        return False
    if source.get("requires_ocr") is True:
        return True
    quality = source.get("quality")
    if quality in {"garbled", "unreadable"}:
        return True
    codes = source.get("ocr_reason_codes") or []
    return bool(codes)


def evaluate_ocr_resolution_gate(
    sources: Sequence[Mapping[str, Any]] | None,
    *,
    project_id: str,
    store: Any = None,
    feature_enabled: bool | None = None,
) -> OcrGateResult:
    """SC00 と vehicle_extract の間の OCR 解決ゲート。

    flag 無効(既定)では no-op。requires_ocr な PDF は passed または
    ocr_reviews.decision='approved' のときだけ通過する。
    """
    from app.capability_registry import ocr_feature_enabled
    enabled = ocr_feature_enabled() if feature_enabled is None else bool(feature_enabled)
    if not enabled:
        return OcrGateResult(blocked=False, noop=True, reason="feature_disabled")

    issues: list[dict[str, Any]] = []
    allowed: list[str] = []
    for source in sources or []:
        if not _source_requires_ocr(source):
            continue
        file_id = str(source.get("id") or source.get("context_file_id") or "")
        filename = str(source.get("filename") or "")
        original_available = source.get("original_available")
        if original_available is False:
            issues.append(_issue(
                "file_absent",
                f"{filename or file_id}: 原本ファイルがありません（読取不能とは区別します）",
                source_ref="context:" + file_id,
                context_file_id=file_id,
            ))
            continue
        run = latest_ocr_run(store, project_id, file_id) if store is not None else None
        review = store.get_review(run["run_id"]) if store is not None and run else None
        if run is None:
            issues.append(_issue(
                "ocr_not_run",
                f"{filename or file_id}: OCR未実行のため後続計算へ渡しません",
                source_ref="context:" + file_id,
                context_file_id=file_id,
            ))
            continue
        status = str(run.get("status") or "")
        if is_ocr_adoptable(run, review):
            allowed.append(str(run["run_id"]))
            continue
        if status == "failed":
            code = str(run.get("error_code") or "ocr_failed")
            issues.append(_issue(
                code,
                f"{filename or file_id}: OCRが failed のため後続計算へ渡しません",
                source_ref="context:" + file_id,
                context_file_id=file_id,
                run_id=run.get("run_id"),
                ocr_status=status,
            ))
            continue
        if status == "needs_review":
            issues.append(_issue(
                OcrErrorCode.OCR_REVIEW_REQUIRED.value,
                f"{filename or file_id}: OCRが needs_review で未承認のため後続計算へ渡しません",
                source_ref="context:" + file_id,
                context_file_id=file_id,
                run_id=run.get("run_id"),
                ocr_status=status,
            ))
            continue
        if status in {"skipped", "queued", "running", ""}:
            issues.append(_issue(
                "ocr_unresolved",
                f"{filename or file_id}: OCRが未完了({status or 'unknown'})のため後続計算へ渡しません",
                source_ref="context:" + file_id,
                context_file_id=file_id,
                run_id=run.get("run_id"),
                ocr_status=status,
            ))
            continue
        issues.append(_issue(
            "ocr_unapproved",
            f"{filename or file_id}: OCRが未承認のため後続計算へ渡しません",
            source_ref="context:" + file_id,
            context_file_id=file_id,
            run_id=run.get("run_id"),
            ocr_status=status,
        ))
    if issues:
        reason = " / ".join(str(item.get("message") or "") for item in issues[:8])
        return OcrGateResult(
            blocked=True,
            issues=tuple(issues),
            allowed_run_ids=tuple(allowed),
            downstream_values=None,
            reason=reason,
        )
    return OcrGateResult(
        blocked=False,
        issues=(),
        allowed_run_ids=tuple(allowed),
        downstream_values={"allowed_run_ids": list(allowed)},
        reason="",
    )


def adoptable_charge_dicts(adapted: AdaptedInvoice) -> list[dict[str, Any]]:
    """正規化入力へ入れてよい当月費用だけ。前月請求・入金・繰越・曖昧額は含めない。"""
    if adapted.force_stop_downstream or adapted.file_absent:
        return []
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for item in adapted.current_month_charges:
        if item.name in NOT_CURRENT_MONTH_FIELDS:
            continue
        if item.error_code == OcrErrorCode.OCR_FIELD_AMBIGUOUS.value:
            continue
        if item.state == "unreadable":
            continue
        key = (item.name, item.page, item.block_id)
        if key in seen:
            continue
        seen.add(key)
        rows.append(item.as_dict())
    return rows


def field_as_zero_amount_safe(item: AdaptedField | Mapping[str, Any]) -> bool:
    """読取不能・空欄は 0 円とみなさない。"""
    if isinstance(item, AdaptedField):
        return item.state == "zero" and field_as_zero_amount({
            "value_state": item.state, "value": item.value,
        })
    return field_as_zero_amount(item)
