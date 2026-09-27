"""原本統制値の独立照合（P0-2）。純粋関数のみ。アダプターや workflow には接続しない。"""
from __future__ import annotations

from typing import Iterable, Literal, TypedDict

ReconciliationStatus = Literal['matched', 'mismatched', 'unavailable', 'incomplete']
ControlOrigin = Literal['source_total', 'sum_of_lines', 'none']
ControlAvailability = Literal['available', 'unavailable']
ControlKind = Literal['invoice_total', 'sheet_subtotal', 'category_total']

# 照合キー: 原本別・会社別・月別・費目別・税区分。請求単位がある費目は invoice_id も含む。
KEY_FIELDS = ('source_ref', 'company', 'month', 'category', 'tax_basis', 'invoice_id')


class SourceControl(TypedDict, total=False):
    """原本統制値（設計書 第2.1節）。

    取得できた場合の例::

        {
          "source_ref": "context:...",
          "company": "関東",
          "month": "2026-01",
          "category": "fuel",
          "tax_basis": "inclusive",
          "invoice_id": None,
          "control_kind": "invoice_total",
          "origin": "source_total",
          "amount": 123456,
          "source_locator": "請求書(鑑)!B20",
          "extraction_method": "invoice_header_adapter:v1",
          "status": "available"
        }

    取得できない場合も行を残す::

        {
          "source_ref": "context:...",
          "company": "関東",
          "month": "2026-01",
          "category": "fuel",
          "status": "unavailable",
          "origin": "none",
          "reason_code": "no_independent_total",
          "reason": "明細のみで請求総額欄がない"
        }

    拘束:
    - origin は 'source_total' のみ独立照合に使う。
    - 明細加算の origin='sum_of_lines' は独立合計として使わない（合格にしない）。
    - 取得不能は origin='none'。
    - 明細を足した値を origin='source_total' として保存してはならない。
    - 同一キーの重複 control は禁止。
    - カテゴリ不明なら category='unknown'（null にしない）。
    """

    source_ref: str
    company: str
    month: str
    category: str
    tax_basis: str
    invoice_id: str | None
    control_kind: ControlKind
    origin: ControlOrigin
    amount: int | None
    source_locator: str
    extraction_method: str
    status: ControlAvailability
    reason_code: str
    reason: str


class ReconciliationRow(TypedDict, total=False):
    """reconcile_sources の1キー分の結果（設計書 第2.2節）。"""

    source_ref: str
    company: str
    month: str
    category: str
    tax_basis: str
    invoice_id: str | None
    source_total: int | None
    included_total: int
    excluded_total: int
    unallocated_total: int
    unknown_components: int
    difference: int | None
    status: ReconciliationStatus
    origin: ControlOrigin
    duplicate_identities: int
    ungrounded_exclusions: int
    duplicate_controls: int


def _norm_invoice_id(value) -> str | None:
    if value is None or value == '':
        return None
    return value


def item_key(item: dict) -> tuple:
    """照合キー。source_ref / invoice_id が違えば会社・月・費目が同じでも混同しない。"""
    return (
        item.get('source_ref'),
        item.get('company'),
        item.get('month'),
        item.get('category'),
        item.get('tax_basis'),
        _norm_invoice_id(item.get('invoice_id')),
    )


def _as_int(value) -> int:
    if value is None or value == '':
        return 0
    return int(value)


def _is_policy_record(record: dict) -> bool:
    if record.get('quality') == 'assumed_zero':
        return True
    ref = str(record.get('source_ref') or '')
    return ref.startswith('policy:')


def _is_unallocated(record: dict) -> bool:
    allocations = record.get('allocations') or []
    return not any(a.get('vehicle_id') and a.get('ratio') not in (None, 0, '0') for a in allocations)


def _record_identity(record: dict) -> tuple:
    return (
        record.get('source_ref'),
        record.get('source_locator') or record.get('locator'),
        record.get('month'),
        record.get('category'),
    )


def _is_grounded_exclusion(item: dict) -> bool:
    """除外は金額・原本位置・理由・承認主体を持つ場合のみ差引可能。"""
    if item.get('amount') is None:
        return False
    locator = item.get('source_locator') or item.get('locator')
    reason = item.get('reason') or item.get('evidence') or item.get('reason_code')
    approver = (
        item.get('approver')
        or item.get('approved_by')
        or item.get('approval_subject')
        or item.get('created_by')
    )
    return bool(locator) and bool(reason) and bool(approver)


def _independent_control(control: dict) -> bool:
    return control.get('origin') == 'source_total' and control.get('status') == 'available'


def _issue_applies(issue: dict, key: tuple) -> bool:
    specified = False
    for field, value in zip(KEY_FIELDS, key):
        raw = issue.get(field)
        if field == 'invoice_id':
            raw = _norm_invoice_id(raw)
        if raw in (None, ''):
            continue
        specified = True
        if raw != value:
            return False
    return specified


def _collect_keys(*groups: Iterable[dict]) -> list[tuple]:
    seen: dict[tuple, None] = {}
    for group in groups:
        for item in group:
            seen.setdefault(item_key(item), None)
    return list(seen)


def reconcile_sources(
    records: list[dict],
    controls: list[dict],
    dispositions: list[dict],
    excluded_records: list[dict],
    read_issues: list[dict],
) -> list[dict]:
    """各キーの原本統制額と取込・除外を照合する。

    source_total は origin='source_total' かつ status='available' の control のみ。
    origin='sum_of_lines' は独立合計として使わない。
    読取失敗が混ざるキーは差額0でも incomplete にする。
    """
    excluded_sources = {
        d.get('source_ref') for d in dispositions if d.get('status') == 'excluded'
    }
    keys = _collect_keys(records, controls, excluded_records, read_issues)
    rows: list[dict] = []
    for key in keys:
        source_ref, company, month, category, tax_basis, invoice_id = key
        key_records = [
            r for r in records
            if item_key(r) == key and not _is_policy_record(r) and r.get('source_ref') not in excluded_sources
        ]
        key_controls = [c for c in controls if item_key(c) == key]
        key_excluded = [e for e in excluded_records if item_key(e) == key]
        key_issues = [i for i in read_issues if _issue_applies(i, key)]

        included_total = sum(_as_int(r.get('amount')) for r in key_records)
        unallocated_total = sum(_as_int(r.get('amount')) for r in key_records if _is_unallocated(r))

        grounded = [e for e in key_excluded if _is_grounded_exclusion(e)]
        ungrounded = [e for e in key_excluded if not _is_grounded_exclusion(e)]
        excluded_total = sum(_as_int(e.get('amount')) for e in grounded)

        identities = [_record_identity(r) for r in key_records]
        duplicate_identities = len(identities) - len(set(identities))

        independent = [c for c in key_controls if _independent_control(c)]
        unavailable_controls = [
            c for c in key_controls
            if c.get('status') == 'unavailable' or c.get('origin') == 'none'
        ]
        sum_of_lines_only = (
            bool(key_controls)
            and not independent
            and all(c.get('origin') == 'sum_of_lines' for c in key_controls)
        )

        origin: ControlOrigin = 'none'
        source_total: int | None = None
        duplicate_controls = 0
        if independent:
            origin = 'source_total'
            duplicate_controls = max(0, len(independent) - 1)
            source_total = _as_int(independent[0].get('amount'))
        elif any(c.get('origin') == 'sum_of_lines' for c in key_controls):
            origin = 'sum_of_lines'

        unknown_components = len(key_issues)
        if ungrounded:
            unknown_components += len(ungrounded)
        if duplicate_controls:
            unknown_components += duplicate_controls

        if source_total is None:
            difference: int | None = None
        else:
            difference = source_total - included_total - excluded_total

        status: ReconciliationStatus
        if key_issues:
            # 読取失敗が混ざるキーは差額0でも matched にしない。
            status = 'incomplete'
        elif ungrounded:
            status = 'incomplete'
        elif duplicate_controls:
            status = 'incomplete'
        elif source_total is None:
            # 取得不能、空、origin='sum_of_lines' のみ。合格にしない。
            status = 'unavailable'
        elif difference != 0 or duplicate_identities:
            status = 'mismatched'
        else:
            status = 'matched'

        row: ReconciliationRow = {
            'source_ref': source_ref,
            'company': company,
            'month': month,
            'category': category,
            'tax_basis': tax_basis,
            'invoice_id': invoice_id,
            'source_total': source_total,
            'included_total': included_total,
            'excluded_total': excluded_total,
            'unallocated_total': unallocated_total,
            'unknown_components': unknown_components,
            'difference': difference,
            'status': status,
            'origin': origin,
            'duplicate_identities': duplicate_identities,
            'ungrounded_exclusions': len(ungrounded),
            'duplicate_controls': duplicate_controls,
        }
        if unavailable_controls:
            row['reason_code'] = unavailable_controls[0].get('reason_code') or 'no_independent_total'
        elif sum_of_lines_only:
            row['reason_code'] = 'sum_of_lines_not_independent'
        rows.append(dict(row))
    return rows
