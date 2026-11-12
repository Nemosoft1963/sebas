"""G2-2: 送信用packetの生成・プレビュー・承認・監査.

外部通信・LLM呼出なし。ストア・検査に使う原本情報は引数で受ける。
モジュール内で ReviewStore を作らない。
本文・検出値・秘密・伏せた内容をログ・例外・監査に残さない。
承認済みpacketの text 保存はこの単位の目的のため可。blockedでは保存しない。
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid

from app.planning_privacy import check_outbound_text
from app import planning_settings as _settings

PUBLIC_FIELDS = ("goal", "success_criteria", "constraints")
PACKET_STATUSES = ("previewed", "blocked", "approved", "revoked")

_PACKET_KIND = "plan_packet"
_AUDIT_KIND = "packet_audit"

_HEADINGS = {
    "goal": "目標",
    "success_criteria": "達成条件",
    "constraints": "制約",
}

_FIXED_CODES = frozenset({
    "unknown_purpose",
    "provider_required",
    "invalid_binding",
    "unknown_field",
    "invalid_item",
    "no_public_items",
    "packet_not_found",
    "packet_not_previewed",
    "packet_not_approved",
    "plan_changed",
    "packet_tampered",
    "privacy_recheck_failed",
    "approver_required",
    "check_error",
})


def plan_signature_of(mission: dict) -> str:
    """計画署名。goal・達成条件・制約・工程から決定的に作る."""
    data = mission if isinstance(mission, dict) else {}
    goal = data.get("goal")
    success = data.get("success_criteria")
    constraints = data.get("constraints_text")
    if not isinstance(goal, str):
        goal = ""
    if not isinstance(success, str):
        success = ""
    if not isinstance(constraints, str):
        constraints = ""
    raw_tasks = data.get("tasks")
    tasks: list[dict] = []
    if isinstance(raw_tasks, list):
        for item in raw_tasks:
            if not isinstance(item, dict):
                tasks.append({"id": "", "title": "", "description": ""})
                continue
            task_id = item.get("id")
            title = item.get("title")
            description = item.get("description")
            tasks.append({
                "id": task_id if isinstance(task_id, str) else "",
                "title": title if isinstance(title, str) else "",
                "description": description if isinstance(description, str) else "",
            })
    canonical = json.dumps(
        {
            "goal": goal,
            "success_criteria": success,
            "constraints_text": constraints,
            "tasks": tasks,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _audit(store, pid, packet_id, action, purpose, provider, sha256, chars,
           approver, reason, codes) -> dict:
    row = {
        "packet_id": str(packet_id or ""),
        "action": str(action or ""),
        "purpose": str(purpose or ""),
        "provider": str(provider or ""),
        "sha256": str(sha256 or ""),
        "chars": int(chars or 0),
        "approver": str(approver or ""),
        "reason": str(reason or ""),
        "codes": list(codes or []),
        "at": time.time(),
    }
    store.put(pid, _AUDIT_KIND, uuid.uuid4().hex, dict(row))
    return row


def build_packet(store, pid, *, purpose, provider, public_items,
                 plan_signature, blocked_terms, source_texts,
                 withheld_fields, structure_text=None) -> dict:
    """公開可の項目だけから送信本文を作り保存する。blockedは本文を保存しない."""
    if purpose not in _settings.PURPOSES:
        raise ValueError("unknown_purpose")
    if not isinstance(provider, str) or provider == "":
        raise ValueError("provider_required")
    if not isinstance(plan_signature, str) or plan_signature == "":
        raise ValueError("invalid_binding")
    if not isinstance(public_items, dict):
        raise ValueError("invalid_item")
    for name in public_items:
        if name not in PUBLIC_FIELDS:
            raise ValueError("unknown_field")
    for name, value in public_items.items():
        if not isinstance(value, str):
            raise ValueError("invalid_item")
    included = [
        name for name in PUBLIC_FIELDS
        if isinstance(public_items.get(name), str)
        and public_items.get(name).strip() != ""
    ]
    if not included:
        raise ValueError("no_public_items")
    if withheld_fields is None:
        withheld: list[str] = []
    else:
        if not isinstance(withheld_fields, (list, tuple)):
            raise ValueError("unknown_field")
        withheld = []
        for name in withheld_fields:
            if name not in PUBLIC_FIELDS:
                raise ValueError("unknown_field")
            if name not in withheld:
                withheld.append(name)
        withheld = [name for name in PUBLIC_FIELDS if name in withheld]
    parts = []
    for name in included:
        parts.append(f"【{_HEADINGS[name]}】\n{public_items[name].strip()}")
    text = "\n\n".join(parts)
    has_structure = False
    if purpose == "plan_review" and isinstance(structure_text, str) \
            and structure_text.strip() != "":
        text = text + "\n\n【計画の構造(実行前)】\n" + structure_text.strip()
        has_structure = True
    try:
        result = check_outbound_text(
            text, blocked_terms=blocked_terms, source_texts=source_texts)
    except (ValueError, KeyError, TypeError, AttributeError):
        result = {"ok": False, "codes": ["check_error"], "details": {}}
    if not isinstance(result, dict) or not result.get("ok"):
        codes: list[str] = []
        if isinstance(result, dict):
            for code in result.get("codes") or []:
                if isinstance(code, str) and code and code not in codes:
                    codes.append(code)
        if not codes:
            codes = ["check_error"]
        status = "blocked"
        privacy = {"ok": False, "codes": list(codes)}
        stored_text = ""
        stored_sha = ""
        stored_chars = 0
        has_structure = False
    else:
        status = "previewed"
        privacy = {"ok": True, "codes": []}
        stored_text = text
        stored_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        stored_chars = len(text)
    packet_id = uuid.uuid4().hex
    packet = {
        "id": packet_id,
        "pid": pid,
        "purpose": purpose,
        "provider": provider,
        "plan_signature": plan_signature,
        "text": stored_text,
        "sha256": stored_sha,
        "chars": stored_chars,
        "included_fields": list(included),
        "withheld_fields": list(withheld),
        "withheld_count": len(withheld),
        "privacy": dict(privacy),
        "status": status,
        "has_structure": bool(has_structure),
        "created_at": time.time(),
    }
    store.put(pid, _PACKET_KIND, packet_id, dict(packet))
    _audit(store, pid, packet_id,
           "blocked" if status == "blocked" else "previewed",
           purpose, provider, stored_sha, stored_chars, "",
           "", list(privacy.get("codes") or []))
    return dict(packet)


def get_packet(store, pid, packet_id):
    """保存済みpacketを返す。無ければ None."""
    try:
        saved = store.get(pid, _PACKET_KIND, packet_id)
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    if saved is None:
        return None
    if not isinstance(saved, dict):
        return None
    return dict(saved)


def approve_packet(store, pid, packet_id, approver, *,
                   current_plan_signature, blocked_terms,
                   source_texts) -> dict:
    """fail-closedで承認する。固定コード以外で失敗させない."""
    try:
        packet = store.get(pid, _PACKET_KIND, packet_id)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ValueError("packet_not_found")
    if packet is None or not isinstance(packet, dict):
        raise ValueError("packet_not_found")
    packet = dict(packet)
    if packet.get("status") != "previewed":
        raise ValueError("packet_not_previewed")
    if (not isinstance(current_plan_signature, str)
            or current_plan_signature != packet.get("plan_signature")):
        raise ValueError("plan_changed")
    text = packet.get("text")
    saved_sha = packet.get("sha256")
    if (not isinstance(text, str) or text == ""
            or not isinstance(saved_sha, str) or saved_sha == ""
            or hashlib.sha256(text.encode("utf-8")).hexdigest() != saved_sha):
        raise ValueError("packet_tampered")
    try:
        result = check_outbound_text(
            text, blocked_terms=blocked_terms, source_texts=source_texts)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ValueError("privacy_recheck_failed")
    if not isinstance(result, dict) or not result.get("ok"):
        raise ValueError("privacy_recheck_failed")
    if not isinstance(approver, str) or approver == "":
        raise ValueError("approver_required")
    binding = {
        "plan_signature": str(packet.get("plan_signature") or ""),
        "packet_sha256": str(packet.get("sha256") or ""),
        "provider": str(packet.get("provider") or ""),
        "purpose": str(packet.get("purpose") or ""),
    }
    _settings.grant_permission(store, pid, str(packet.get("purpose") or ""),
                               dict(binding), approver)
    try:
        rows = store.list(pid, _PACKET_KIND)
    except (ValueError, KeyError, TypeError, AttributeError):
        rows = []
    for signature, row in rows or []:
        if not isinstance(row, dict):
            continue
        if signature == packet.get("id"):
            continue
        if row.get("purpose") != packet.get("purpose"):
            continue
        if row.get("status") != "approved":
            continue
        updated = dict(row)
        updated["status"] = "revoked"
        store.put(pid, _PACKET_KIND, signature, updated)
    packet["status"] = "approved"
    store.put(pid, _PACKET_KIND, packet.get("id"), dict(packet))
    _audit(store, pid, packet.get("id"), "approved",
           str(packet.get("purpose") or ""),
           str(packet.get("provider") or ""),
           str(packet.get("sha256") or ""),
           int(packet.get("chars") or 0),
           approver if isinstance(approver, str) else "",
           "", [])
    return dict(packet)


def verify_for_send(store, pid, packet_id, *,
                    current_plan_signature) -> dict:
    """G3が送信直前に呼ぶ関門。許可のときだけ text を返す."""
    def _deny(reason: str, probe: dict | None = None) -> dict:
        target = probe if isinstance(probe, dict) else {}
        _audit(store, pid, packet_id, "send_denied",
               str(target.get("purpose") or ""),
               str(target.get("provider") or ""),
               str(target.get("sha256") or ""),
               int(target.get("chars") or 0),
               "", str(reason or ""), [])
        return {"allowed": False, "reason": str(reason or ""), "text": ""}

    try:
        packet = store.get(pid, _PACKET_KIND, packet_id)
        if packet is None or not isinstance(packet, dict):
            return _deny("packet_not_found")
        packet = dict(packet)
        if packet.get("status") != "approved":
            return _deny("packet_not_approved", packet)
        text = packet.get("text")
        saved_sha = packet.get("sha256")
        if (not isinstance(text, str) or text == ""
                or not isinstance(saved_sha, str) or saved_sha == ""
                or hashlib.sha256(text.encode("utf-8")).hexdigest() != saved_sha):
            return _deny("packet_tampered", packet)
        if (not isinstance(current_plan_signature, str)
                or current_plan_signature != packet.get("plan_signature")):
            return _deny("plan_changed", packet)
        binding = {
            "plan_signature": str(packet.get("plan_signature") or ""),
            "packet_sha256": str(packet.get("sha256") or ""),
            "provider": str(packet.get("provider") or ""),
            "purpose": str(packet.get("purpose") or ""),
        }
        checked = _settings.check_permission(
            store, pid, str(packet.get("purpose") or ""), dict(binding))
        if not isinstance(checked, dict) or not checked.get("allowed"):
            reason = "permission_denied"
            detail = ""
            if isinstance(checked, dict):
                detail = str(checked.get("reason") or "")
            if detail:
                reason = f"permission_denied:{detail}"
            return _deny(reason, packet)
    except (ValueError, KeyError, TypeError, AttributeError):
        return _deny("check_error", None)
    _audit(store, pid, packet_id, "send_verified",
           str(packet.get("purpose") or ""),
           str(packet.get("provider") or ""),
           str(packet.get("sha256") or ""),
           int(packet.get("chars") or 0),
           "", "ok", [])
    return {"allowed": True, "reason": "ok", "text": text}


def revoke_packet(store, pid, packet_id, approver):
    """承認済みだけ revoked にし、同じ用途の許可を失効させる."""
    try:
        packet = store.get(pid, _PACKET_KIND, packet_id)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ValueError("packet_not_approved")
    if packet is None or not isinstance(packet, dict):
        raise ValueError("packet_not_approved")
    packet = dict(packet)
    if packet.get("status") != "approved":
        raise ValueError("packet_not_approved")
    packet["status"] = "revoked"
    store.put(pid, _PACKET_KIND, packet.get("id"), dict(packet))
    try:
        _settings.revoke_permission(store, pid, str(packet.get("purpose") or ""))
    except ValueError:
        raise
    _audit(store, pid, packet.get("id"), "revoked",
           str(packet.get("purpose") or ""),
           str(packet.get("provider") or ""),
           str(packet.get("sha256") or ""),
           int(packet.get("chars") or 0),
           approver if isinstance(approver, str) else "",
           "", [])
    return dict(packet)


def list_audit(store, pid, packet_id=None) -> list:
    """監査行を時系列(at昇順)で返す。本文・検出値・伏せた内容は含まない."""
    try:
        rows = store.list(pid, _AUDIT_KIND)
    except (ValueError, KeyError, TypeError, AttributeError):
        return []
    items: list[dict] = []
    for _signature, row in rows or []:
        if not isinstance(row, dict):
            continue
        if packet_id is not None and row.get("packet_id") != packet_id:
            continue
        items.append(dict(row))
    items.sort(key=lambda row: float(row.get("at") or 0))
    return items
