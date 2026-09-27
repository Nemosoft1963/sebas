"""Read-only evidence graph: vehicle × month × category → original page/row.

Nodes (cell / record / source / allocation) are assembled lazily inside
`trace`. Nothing is prebuilt, cached, or written to input.json / DB /
artifacts. Gate and C12 are unchanged; this module only diagnoses.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from itertools import product

from app.vehicle_workflow import (
    CATEGORIES,
    applicable,
    digest,
    load_input,
    sources,
)

NOT_APPLICABLE_REASON = "車両損益以外はサンプリング対象(未実装)"
DEFAULT_SUMMARY_LIMIT = 50
SOURCE_PREFIX = "context:"

STATUS_TRACED = "traced"
STATUS_UNTRACEABLE = "untraceable"
STATUS_STALE = "stale"
STATUS_NO_DATA = "no_data"
STATUS_NOT_APPLICABLE = "not_applicable"

_KIND_CELL = "cell"
_KIND_RECORD = "record"
_KIND_SOURCE = "source"
_KIND_ALLOCATION = "allocation"


def build_full_graph(manager, project_id):
    """Full-graph construction is intentionally unsupported.

    Evidence nodes are generated only for the cell requested by `trace`.
    """
    raise RuntimeError("evidence graph is lazy; full build is not supported")


def _not_applicable():
    return {
        "applicable": False,
        "status": STATUS_NOT_APPLICABLE,
        "reason": NOT_APPLICABLE_REASON,
    }


def _mission(manager, project_id):
    return manager.memory.get_mission(project_id)


def _json_amount(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return str(value)
    if isinstance(value, int):
        return value
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if number == number.to_integral_value():
        return int(number)
    return str(number)


def _context_id(source_ref):
    text = str(source_ref or "")
    if text.startswith(SOURCE_PREFIX):
        return text[len(SOURCE_PREFIX):]
    return None


def _snapshot_by_id(snapshot):
    return {item["id"]: item for item in (snapshot or []) if item.get("id")}


def _record_vehicle_ids(record):
    ids = []
    if record.get("vehicle_id"):
        ids.append(str(record["vehicle_id"]))
    for item in record.get("allocations") or []:
        vid = item.get("vehicle_id")
        if vid:
            ids.append(str(vid))
    return list(dict.fromkeys(ids))


def _is_missing_quality(record):
    return record.get("quality") == "missing"


def record_matches_cell(record, vehicle_id, month, category):
    if _is_missing_quality(record):
        return False
    if record.get("month") != month or record.get("category") != category:
        return False
    return str(vehicle_id) in _record_vehicle_ids(record)


def contributed_amount(record, vehicle_id):
    """Amount this record contributes to a vehicle cell.

    Uses only the record's own `amount` and `allocations`. Does not invent
    ratios, rounding policy, or company-level allocation rules.
    """
    if not record_matches_cell(record, vehicle_id, record.get("month"), record.get("category")):
        return None
    raw = record.get("amount")
    if raw is None:
        return None
    try:
        amount = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    matching = [
        item for item in (record.get("allocations") or [])
        if item.get("vehicle_id") == vehicle_id
    ]
    if matching:
        total = Decimal("0")
        for item in matching:
            try:
                ratio = Decimal(str(item.get("ratio")))
            except (InvalidOperation, TypeError, ValueError):
                return None
            total += amount * ratio
        return total
    if record.get("vehicle_id") == vehicle_id:
        return amount
    return None


def cell_amount(records, vehicle_id):
    total = Decimal("0")
    any_amount = False
    for record in records:
        part = contributed_amount(record, vehicle_id)
        if part is None:
            continue
        total += part
        any_amount = True
    if not any_amount:
        return None
    return _json_amount(total)


def company_totals_from_input(data):
    """Sum vehicle-cell amounts from input.json only. No new allocation rules."""
    vehicles = [v.get("id") for v in (data or {}).get("vehicles") or [] if v.get("id")]
    months = list((data or {}).get("months") or [])
    categories = list((data or {}).get("required_categories") or CATEGORIES)
    records = list((data or {}).get("records") or [])
    totals = {}
    for month, category in product(months, categories):
        company_sum = Decimal("0")
        any_amount = False
        per_vehicle = {}
        for vid in vehicles:
            matched = [r for r in records if record_matches_cell(r, vid, month, category)]
            amount = cell_amount(matched, vid)
            per_vehicle[vid] = amount
            if amount is not None:
                company_sum += Decimal(str(amount))
                any_amount = True
        totals[(month, category)] = {
            "vehicles": per_vehicle,
            "company_total": _json_amount(company_sum) if any_amount else None,
        }
    return totals


def _blank(value):
    return not str(value or "").strip()


def _source_issue(record_id, field, reason, message):
    return {
        "record_id": record_id,
        "field": field,
        "reason": reason,
        "message": message,
    }


def _classify_records(records, *, current_by_id, stored_by_id, envelope_source_hash, current_digest):
    """Classify a cell from its records without building graph nodes."""
    if not records:
        return STATUS_NO_DATA, []
    issues = []
    untraceable = False
    stale = False
    global_stale = bool(envelope_source_hash) and envelope_source_hash != current_digest
    missing_hash = not envelope_source_hash
    if missing_hash:
        untraceable = True
        issues.append(_source_issue(
            "", "source_hash", "missing_source_hash",
            "入力に source_hash がありません",
        ))
    for record in records:
        rid = str(record.get("id") or "")
        source_ref = record.get("source_ref")
        locator = record.get("source_locator")
        if _blank(source_ref):
            untraceable = True
            issues.append(_source_issue(
                rid, "source_ref", "missing_source_ref",
                f"{rid}: source_ref がありません",
            ))
        if _blank(locator):
            untraceable = True
            issues.append(_source_issue(
                rid, "source_locator", "missing_source_locator",
                f"{rid}: source_locator がありません",
            ))
        if _blank(source_ref):
            continue
        cid = _context_id(source_ref)
        if not cid:
            untraceable = True
            issues.append(_source_issue(
                rid, "source", "source_not_context",
                f"{rid}: 原本参照が登録原本ではありません ({source_ref})",
            ))
            continue
        current = current_by_id.get(cid)
        if not current:
            untraceable = True
            issues.append(_source_issue(
                rid, "source", "source_missing",
                f"{rid}: 原本 {source_ref} が現存しません",
            ))
            continue
        stored = stored_by_id.get(cid)
        current_sha = current.get("sha256")
        stored_sha = (stored or {}).get("sha256")
        if stored_sha and current_sha and stored_sha != current_sha:
            stale = True
            issues.append(_source_issue(
                rid, "source_hash", "source_hash_mismatch",
                f"{rid}: 原本ハッシュが入力作成時と一致しません",
            ))
        elif global_stale:
            stale = True
            issues.append(_source_issue(
                rid, "source_hash", "source_hash_mismatch",
                f"{rid}: 登録原本スナップショットが入力作成後に変わっています",
            ))
    if untraceable:
        return STATUS_UNTRACEABLE, issues
    if stale:
        return STATUS_STALE, issues
    return STATUS_TRACED, issues


def _allocation_node(record):
    allocations = list(record.get("allocations") or [])
    reused = record.get("allocation_reused")
    extra_keys = (
        "allocation_rule_id", "decision_id", "applied_rule_id",
        "reuse_of", "allocation_source_id",
    )
    extras = {key: record[key] for key in extra_keys if record.get(key) not in (None, "")}
    if not allocations and reused in (None, False) and not extras:
        return None
    node = {
        "kind": _KIND_ALLOCATION,
        "record_id": record.get("id"),
    }
    if allocations:
        node["allocations"] = allocations
    if reused not in (None, False, ""):
        node["allocation_reused"] = reused
    node.update(extras)
    return node


def _cell_universe(data):
    vehicles = [v.get("id") for v in (data or {}).get("vehicles") or [] if v.get("id")]
    months = list((data or {}).get("months") or [])
    categories = list((data or {}).get("required_categories") or CATEGORIES)
    return vehicles, months, categories


def _stored_snapshot(data):
    return list((data or {}).get("source_snapshot") or [])


def trace(manager, project_id, vehicle_id, month, category):
    """Assemble nodes for one vehicle × month × category cell. Read-only."""
    mission = _mission(manager, project_id)
    if not applicable(mission):
        return _not_applicable()
    envelope = load_input(manager, project_id)
    snapshot = sources(manager, project_id)
    current_digest = digest(snapshot)
    data = (envelope or {}).get("data") if envelope else None
    records = [
        record for record in (data or {}).get("records") or []
        if record_matches_cell(record, vehicle_id, month, category)
    ]
    current_by_id = _snapshot_by_id(snapshot)
    stored_by_id = _snapshot_by_id(_stored_snapshot(data))
    envelope_hash = (envelope or {}).get("source_hash") or ""
    status, issues = _classify_records(
        records,
        current_by_id=current_by_id,
        stored_by_id=stored_by_id,
        envelope_source_hash=envelope_hash,
        current_digest=current_digest,
    )
    amount = None if status == STATUS_NO_DATA else cell_amount(records, vehicle_id)
    record_nodes = []
    source_nodes = []
    seen_sources = set()
    allocation_nodes = []
    for record in records:
        contrib = contributed_amount(record, vehicle_id)
        source_ref = str(record.get("source_ref") or "").strip()
        locator = str(record.get("source_locator") or "").strip()
        record_nodes.append({
            "kind": _KIND_RECORD,
            "id": record.get("id"),
            "vehicle_id": vehicle_id,
            "month": record.get("month"),
            "category": record.get("category"),
            "amount": _json_amount(record.get("amount")),
            "contributed_amount": _json_amount(contrib),
            "source_ref": source_ref,
            "source_locator": locator,
            "allocations": list(record.get("allocations") or []),
        })
        key = (source_ref, locator)
        if source_ref or locator:
            if key not in seen_sources:
                seen_sources.add(key)
                cid = _context_id(source_ref)
                current = current_by_id.get(cid) if cid else None
                source_nodes.append({
                    "kind": _KIND_SOURCE,
                    "source_ref": source_ref,
                    "locator": locator,
                    "source_hash": (current or {}).get("sha256"),
                    "exists": bool(current),
                })
        node = _allocation_node(record)
        if node is not None:
            allocation_nodes.append(node)
    if amount is not None:
        contributed_sum = cell_amount(records, vehicle_id)
        if contributed_sum != amount:
            issues.append(_source_issue(
                "", "amount", "amount_mismatch",
                "セル金額が明細合計と一致しません",
            ))
            if status == STATUS_TRACED:
                status = STATUS_UNTRACEABLE
    return {
        "applicable": True,
        "vehicle_id": vehicle_id,
        "month": month,
        "category": category,
        "amount": amount,
        "status": status,
        "cell": {
            "kind": _KIND_CELL,
            "vehicle_id": vehicle_id,
            "month": month,
            "category": category,
            "amount": amount,
        },
        "records": record_nodes,
        "sources": source_nodes,
        "allocations": allocation_nodes,
        "issues": issues,
    }


def summary(manager, project_id, limit=DEFAULT_SUMMARY_LIMIT):
    """Count cell statuses from one input.json read. No node expansion."""
    mission = _mission(manager, project_id)
    if not applicable(mission):
        return _not_applicable()
    try:
        cap = int(limit)
    except (TypeError, ValueError):
        cap = DEFAULT_SUMMARY_LIMIT
    if cap < 0:
        cap = 0
    envelope = load_input(manager, project_id)
    snapshot = sources(manager, project_id)
    current_digest = digest(snapshot)
    data = (envelope or {}).get("data") if envelope else None
    vehicles, months, categories = _cell_universe(data)
    all_records = list((data or {}).get("records") or [])
    current_by_id = _snapshot_by_id(snapshot)
    stored_by_id = _snapshot_by_id(_stored_snapshot(data))
    envelope_hash = (envelope or {}).get("source_hash") or ""
    counts = {
        STATUS_TRACED: 0,
        STATUS_UNTRACEABLE: 0,
        STATUS_STALE: 0,
        STATUS_NO_DATA: 0,
    }
    untraceable = []
    stale = []
    untraceable_total = 0
    stale_total = 0
    for vid, month, category in product(vehicles, months, categories):
        matched = [
            record for record in all_records
            if record_matches_cell(record, vid, month, category)
        ]
        status, issues = _classify_records(
            matched,
            current_by_id=current_by_id,
            stored_by_id=stored_by_id,
            envelope_source_hash=envelope_hash,
            current_digest=current_digest,
        )
        counts[status] = counts.get(status, 0) + 1
        entry = {
            "vehicle_id": vid,
            "month": month,
            "category": category,
            "status": status,
            "issues": issues,
        }
        if status == STATUS_UNTRACEABLE:
            untraceable_total += 1
            if len(untraceable) < cap:
                untraceable.append(entry)
        elif status == STATUS_STALE:
            stale_total += 1
            if len(stale) < cap:
                stale.append(entry)
    return {
        "applicable": True,
        "counts": counts,
        "untraceable": untraceable,
        "stale": stale,
        "limit": cap,
        "untraceable_total": untraceable_total,
        "stale_total": stale_total,
        "untraceable_omitted": max(0, untraceable_total - len(untraceable)),
        "stale_omitted": max(0, stale_total - len(stale)),
        "cell_total": len(vehicles) * len(months) * len(categories),
    }
