"""G3-a: 承認済みpacketを外部AIへ1回だけ送り、応答を信用しない下書きとして保管する.

外部通信・LLM呼出は引数のランナー経由のみ。モジュール内で ReviewStore を作らない。
返答をミッション・計画に書かない。承認・実行開始につなげない。
ローカルでの統合(compile_task等)はG3-bで行うので作らない。
送信本文・応答本文・例外メッセージ・秘密をログ・例外・監査・ジョブ記録に残さない。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from datetime import datetime, timezone

from app import planning_packet
from app import planning_settings
from app.planning_privacy import check_outbound_text

KIND = "external_plan_job"
JOB_STATUSES = ("sending", "external_generated", "rejected_response",
                "failed", "cancelled", "outcome_unknown")
MAX_JOBS_PER_DAY = 3
SEND_TIMEOUT_SECONDS = 120
MAX_TOKENS = 3000
PURPOSE = "plan_generation"
BOOT_ID = uuid.uuid4().hex

PLAN_SYSTEM_PROMPT = (
    "あなたは公開情報だけから計画の下書きを作る支援者です。"
    "送られた公開情報だけを使い、JSONだけを返してください。説明文やコードフェンスは付けないでください。"
    "使えるキーは tasks / required_outputs / unverified_facts / external_operations_pending のみです。"
    "tasks の各要素は title / description / outputs / depends_on のみです。"
    "outputs は相対パスだけを入れ、絶対パスや .. を入れないでください。"
    "depends_on には他の工程の title だけを入れてください。"
    "事実を補ったり推測で埋めたりしないでください。不明な点は unverified_facts に書いてください。"
    "実施済み・達成の主張をしないでください。"
)

_ALLOWED_TOP_KEYS = frozenset({
    "tasks", "required_outputs", "unverified_facts",
    "external_operations_pending",
})
_ALLOWED_TASK_KEYS = frozenset({"title", "description", "outputs", "depends_on"})
_SENSITIVE_FAIL_CODES = frozenset({
    "secret", "email", "abs_path", "url_query", "hash_like", "encoded_blob",
})
_REFERENCE_RE = re.compile(r"\[(?:顧客|取引先|担当者|案件)[^\[\]\r\n]{0,40}\]")
_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```")
_CURL_PIPE_RE = re.compile(r"curl[\s\S]{0,200}\|\s*(sh|bash)")
_FORBIDDEN_MARKERS = (
    "rm -rf",
    "del /s",
    "invoke-expression",
    "powershell -enc",
    "-encodedcommand",
    "drop table",
)
_INJECTION_MARKERS_ASCII = (
    "ignore previous",
    "disregard previous",
    "system prompt",
)
_INJECTION_MARKERS_RAW = (
    "以前の指示を無視",
    "以前の指示は無視",
    "システムプロンプト",
    "あなたはもう",
)
_WEAKEN_MARKERS_RAW = (
    "検証を省略",
    "承認不要",
    "外部確認不要",
    "レビュー不要",
    "ゲートを無効",
    "承認を省略",
    "確認を省略",
)
_WEAKEN_MARKERS_ASCII = (
    "skip verification",
    "skip review",
    "bypass approval",
)
_CLAIM_MARKERS_RAW = ("実施済み", "完了済み", "達成済み", "確認済み")
_CLAIM_MARKERS_ASCII = ("completed", "achieved", "verified")
_SENTENCE_SPLIT_RE = re.compile(r"\n+|(?<=[。！？.!?])\s*")


def idempotency_key_of(pid, plan_signature, packet_sha256, provider,
                       purpose) -> str:
    """曖昧さのない連結の SHA-256 16進(小文字)。1つでも変わると値が変わる。"""
    parts = [pid, plan_signature, packet_sha256, provider, purpose]
    blob = b""
    for part in parts:
        raw = str(part if part is not None else "").encode("utf-8")
        blob += str(len(raw)).encode("ascii") + b":" + raw + b";"
    return hashlib.sha256(blob).hexdigest()


def enabled_for_project(env_value: str, pid: str) -> bool:
    """環境変数の値(カンマ区切りのPJ ID)に pid が完全一致で含まれるときだけ True。"""
    if not isinstance(env_value, str) or not env_value.strip():
        return False
    if not isinstance(pid, str) or pid == "":
        return False
    return any(token == pid for token in
               (item.strip() for item in env_value.split(",")) if token)


def _utc_date_of(ts: float):
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).date()


def _today_utc():
    return datetime.now(timezone.utc).date()


def recover_interrupted(store, pid) -> list[str]:
    """前プロセスの取り残し(sending かつ別 boot_id)を outcome_unknown にする。再送しない。"""
    recovered: list[str] = []
    try:
        rows = store.list(pid, KIND)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise
    for signature, row in rows or []:
        if not isinstance(row, dict):
            continue
        if row.get("status") != "sending":
            continue
        if row.get("boot_id") == BOOT_ID:
            continue
        updated = dict(row)
        updated["status"] = "outcome_unknown"
        updated["result_class"] = "outcome_unknown"
        updated["codes"] = ["interrupted"]
        updated["updated_at"] = time.time()
        store.put(pid, KIND, signature, updated)
        recovered.append(signature)
    return recovered


def get_job(store, pid, job_id):
    """recover を先に行ってから返す。無ければ None。"""
    recover_interrupted(store, pid)
    try:
        saved = store.get(pid, KIND, job_id)
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    if saved is None or not isinstance(saved, dict):
        return None
    return dict(saved)


def list_jobs(store, pid) -> list:
    """PJのジョブを作成時刻順で返す。"""
    try:
        rows = store.list(pid, KIND)
    except (ValueError, KeyError, TypeError, AttributeError):
        return []
    items = [dict(row) for _, row in rows or [] if isinstance(row, dict)]
    items.sort(key=lambda row: float(row.get("created_at") or 0))
    return items


def cancel_job(store, pid, job_id):
    """sending なら cancelled にする。無ければ job_not_found、sending以外は job_not_cancellable。"""
    try:
        saved = store.get(pid, KIND, job_id)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ValueError("job_not_found")
    if saved is None or not isinstance(saved, dict):
        raise ValueError("job_not_found")
    if saved.get("status") != "sending":
        raise ValueError("job_not_cancellable")
    updated = dict(saved)
    updated["status"] = "cancelled"
    updated["result_class"] = "cancelled"
    updated["updated_at"] = time.time()
    store.put(pid, KIND, job_id, updated)
    return dict(updated)


def prepare_job(store, pid, *, packet_id, provider, idempotency_key,
                current_plan_signature, enabled, approver, blocked_terms,
                source_texts, now=None) -> dict:
    """fail-closed の同期関数。順序どおりに検査し、許可を1回の送信にだけ使う。"""
    if not enabled:
        raise ValueError("feature_disabled")
    settings = planning_settings.get_settings(store, pid)
    if not isinstance(settings, dict) or settings.get("method") != "external_guarded":
        raise ValueError("method_not_external")
    if (not isinstance(provider, str) or provider == ""
            or provider != settings.get("generator_provider")):
        raise ValueError("provider_mismatch")
    packet = planning_packet.get_packet(store, pid, packet_id)
    if packet is None:
        raise ValueError("packet_not_found")
    if packet.get("provider") != provider:
        raise ValueError("provider_mismatch")
    if packet.get("purpose") != PURPOSE:
        raise ValueError("purpose_mismatch")
    packet_sha = packet.get("sha256") if isinstance(packet.get("sha256"), str) else ""
    expected_key = idempotency_key_of(pid, current_plan_signature, packet_sha,
                                      provider, PURPOSE)
    if not isinstance(idempotency_key, str) or idempotency_key != expected_key:
        raise ValueError("idempotency_key_mismatch")
    # 中断の取り残しを先に確定させる(次の prepare でも outcome_unknown になる)。
    recover_interrupted(store, pid)
    job_id = idempotency_key[:32]
    try:
        existing = store.get(pid, KIND, job_id)
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
        raise ValueError("check_error") from exc
    if isinstance(existing, dict):
        return {"job": dict(existing), "created": False}
    ts = float(now) if now is not None else time.time()
    try:
        rows = store.list(pid, KIND)
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
        raise ValueError("check_error") from exc
    today = _utc_date_of(ts)
    count = 0
    for _, row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            created = float(row.get("created_at") or 0)
        except (TypeError, ValueError):
            continue
        if created and _utc_date_of(created) == today:
            count += 1
    if count >= MAX_JOBS_PER_DAY:
        raise ValueError("quota_exceeded")
    verified = planning_packet.verify_for_send(
        store, pid, packet_id,
        current_plan_signature=current_plan_signature)
    if not isinstance(verified, dict) or not verified.get("allowed"):
        reason = ""
        if isinstance(verified, dict):
            reason = str(verified.get("reason") or "")
        raise ValueError(reason or "check_error")
    text = verified.get("text")
    if not isinstance(text, str) or text == "":
        raise ValueError("check_error")
    # G3-a2: 送信直前の再検査。承認後に原本が変わっても漏らさない。
    # 既存ジョブの早期復帰より後、ジョブ保存・許可消費より前。
    if (not isinstance(blocked_terms, (list, tuple))
            or not isinstance(source_texts, (list, tuple))):
        planning_packet._audit(
            store, pid, packet_id, "send_denied",
            str(packet.get("purpose") or PURPOSE),
            str(packet.get("provider") or provider),
            packet_sha,
            int(packet.get("chars")) if isinstance(packet.get("chars"), int) else 0,
            "", "privacy_recheck_failed", ["check_error"])
        raise ValueError("privacy_recheck_failed")
    try:
        rechecked = check_outbound_text(
            text, blocked_terms=list(blocked_terms),
            source_texts=list(source_texts))
    except (ValueError, KeyError, TypeError, AttributeError):
        planning_packet._audit(
            store, pid, packet_id, "send_denied",
            str(packet.get("purpose") or PURPOSE),
            str(packet.get("provider") or provider),
            packet_sha,
            int(packet.get("chars")) if isinstance(packet.get("chars"), int) else 0,
            "", "privacy_recheck_failed", ["check_error"])
        raise ValueError("privacy_recheck_failed")
    if not isinstance(rechecked, dict) or not rechecked.get("ok"):
        codes: list[str] = []
        if isinstance(rechecked, dict):
            for code in rechecked.get("codes") or []:
                if isinstance(code, str) and code and code not in codes:
                    codes.append(code)
        if not codes:
            codes = ["check_error"]
        planning_packet._audit(
            store, pid, packet_id, "send_denied",
            str(packet.get("purpose") or PURPOSE),
            str(packet.get("provider") or provider),
            packet_sha,
            int(packet.get("chars")) if isinstance(packet.get("chars"), int) else 0,
            "", "privacy_recheck_failed", list(codes))
        raise ValueError("privacy_recheck_failed")
    job = {
        "id": job_id,
        "pid": pid,
        "packet_id": packet_id,
        "provider": provider,
        "purpose": PURPOSE,
        "plan_signature": current_plan_signature,
        "packet_sha256": packet_sha,
        "idempotency_key": idempotency_key,
        "status": "sending",
        "boot_id": BOOT_ID,
        "send_started": False,
        "created_at": ts,
        "updated_at": ts,
        "model": "",
        "usage": {},
        "result_class": "",
        "codes": [],
        "external_draft": None,
        "unverified_claims": [],
        "error_class": "",
    }
    store.put(pid, KIND, job_id, dict(job))
    planning_packet.revoke_packet(store, pid, packet_id,
                                  "system:external_plan_job")
    chars = packet.get("chars")
    planning_packet._audit(
        store, pid, packet_id, "job_created",
        str(packet.get("purpose") or PURPOSE), provider, packet_sha,
        int(chars) if isinstance(chars, int) else 0,
        approver if isinstance(approver, str) else "", "", [])
    return {"job": dict(job), "created": True, "text": text}


def _audit_finished(store, pid, job, status, codes, model, usage) -> None:
    row = {
        "packet_id": str(job.get("packet_id") or ""),
        "job_id": str(job.get("id") or ""),
        "action": "job_finished",
        "purpose": str(job.get("purpose") or PURPOSE),
        "provider": str(job.get("provider") or ""),
        "sha256": str(job.get("packet_sha256") or ""),
        "packet_sha256": str(job.get("packet_sha256") or ""),
        "status": str(status or ""),
        "codes": list(codes or []),
        "model": str(model or ""),
        "usage": dict(usage or {}),
        "chars": 0,
        "approver": "",
        "reason": str(status or ""),
        "at": time.time(),
    }
    store.put(pid, "packet_audit", uuid.uuid4().hex, dict(row))


def _extract_fence(raw_text: str) -> str:
    text = raw_text.strip()
    match = _FENCE_RE.search(text)
    if match:
        inner = match.group(1).strip()
        if inner:
            return inner
    return text


def _iter_strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _iter_strings(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _iter_strings(value)


def _collect_unverified_claims(draft: dict) -> list:
    claims: list[str] = []
    for text in _iter_strings(draft):
        for sentence in _SENTENCE_SPLIT_RE.split(text):
            cleaned = sentence.strip()
            if not cleaned:
                continue
            lowered = cleaned.lower()
            if any(mark in cleaned for mark in _CLAIM_MARKERS_RAW):
                claims.append(cleaned)
            elif any(mark in lowered for mark in _CLAIM_MARKERS_ASCII):
                claims.append(cleaned)
    unique: list[str] = []
    for claim in claims:
        if claim not in unique:
            unique.append(claim)
    return unique


def _shape_draft(obj: dict) -> dict:
    tasks = []
    for item in obj.get("tasks") or []:
        tasks.append({
            "title": str(item.get("title")).strip(),
            "description": str(item.get("description")).strip(),
            "outputs": [str(name).strip() for name in item.get("outputs") or []],
            "depends_on": [str(name).strip()
                            for name in item.get("depends_on") or []],
        })
    required = [str(name).strip() for name in obj.get("required_outputs") or []]
    facts = [str(name).strip() for name in obj.get("unverified_facts") or []]
    pending = []
    for entry in obj.get("external_operations_pending") or []:
        if isinstance(entry, str):
            pending.append(entry.strip())
        elif isinstance(entry, dict):
            shaped: dict = {}
            for key, value in entry.items():
                if isinstance(key, str) and isinstance(value, str):
                    shaped[key.strip()] = value.strip()
                elif isinstance(key, str):
                    shaped[key.strip()] = value
            pending.append(shaped)
    return {
        "tasks": tasks,
        "required_outputs": required,
        "unverified_facts": facts,
        "external_operations_pending": pending,
    }


def validate_response(raw_text: str) -> dict:
    """純粋関数。応答を検査し、固定コード・整形済み下書き・隔離文を返す。"""
    if not isinstance(raw_text, str):
        return {"ok": False, "codes": ["not_json"], "draft": None,
                "unverified_claims": []}
    codes: list[str] = []
    if len(raw_text) > 60000:
        codes.append("too_large")
    try:
        obj = json.loads(_extract_fence(raw_text))
    except (ValueError, TypeError, AttributeError):
        obj = None
        codes.append("not_json")
    if obj is not None and not isinstance(obj, dict):
        codes.append("not_object")
        obj = None
    if isinstance(obj, dict):
        for key in obj:
            if key not in _ALLOWED_TOP_KEYS and "unknown_key" not in codes:
                codes.append("unknown_key")
        tasks = obj.get("tasks")
        if "tasks" not in obj or not isinstance(tasks, list):
            if "bad_type" not in codes:
                codes.append("bad_type")
            tasks = None
        else:
            if len(tasks) == 0:
                codes.append("no_tasks")
            if len(tasks) >= 31:
                codes.append("too_many_tasks")
            titles: set[str] = set()
            for item in tasks:
                if isinstance(item, dict) and isinstance(item.get("title"), str):
                    titles.add(item.get("title"))
            for item in tasks:
                if not isinstance(item, dict):
                    if "bad_type" not in codes:
                        codes.append("bad_type")
                    continue
                for key in item:
                    if key not in _ALLOWED_TASK_KEYS and "unknown_key" not in codes:
                        codes.append("unknown_key")
                title = item.get("title")
                description = item.get("description")
                outputs = item.get("outputs")
                depends = item.get("depends_on")
                if (not isinstance(title, str) or not isinstance(description, str)
                        or not isinstance(outputs, list)
                        or not isinstance(depends, list)):
                    if "bad_type" not in codes:
                        codes.append("bad_type")
                    continue
                if any(not isinstance(name, str) for name in outputs):
                    if "bad_type" not in codes:
                        codes.append("bad_type")
                if any(not isinstance(name, str) for name in depends):
                    if "bad_type" not in codes:
                        codes.append("bad_type")
                if len(title) > 120 or len(description) > 1000 or any(
                        isinstance(name, str) and len(name) > 200
                        for name in outputs):
                    if "too_long" not in codes:
                        codes.append("too_long")
                for name in outputs:
                    if not isinstance(name, str):
                        continue
                    if (re.match(r"^[A-Za-z]:[\\/]", name)
                            or name.startswith("/")
                            or name.startswith("\\")
                            or ".." in name):
                        if "unsafe_path" not in codes:
                            codes.append("unsafe_path")
                        break
                for name in depends:
                    if isinstance(name, str) and name not in titles:
                        if "bad_dependency" not in codes:
                            codes.append("bad_dependency")
                        break
        for key in ("required_outputs", "unverified_facts"):
            if key in obj:
                values = obj.get(key)
                if not isinstance(values, list) or any(
                        not isinstance(entry, str) for entry in values):
                    if "bad_type" not in codes:
                        codes.append("bad_type")
        if "external_operations_pending" in obj:
            pending = obj.get("external_operations_pending")
            if not isinstance(pending, list) or any(
                    not isinstance(entry, (dict, str)) for entry in pending):
                if "bad_type" not in codes:
                    codes.append("bad_type")
    lowered = raw_text.lower()
    if ("format " in lowered or "rm -rf" in lowered or "del /s" in lowered
            or "invoke-expression" in lowered or "powershell -enc" in lowered
            or "-encodedcommand" in lowered or "drop table" in lowered
            or _CURL_PIPE_RE.search(lowered)):
        codes.append("forbidden_command")
    try:
        checked = check_outbound_text(raw_text)
    except (ValueError, KeyError, TypeError, AttributeError):
        checked = {"codes": ["check_error"]}
    found = set()
    if isinstance(checked, dict):
        for code in checked.get("codes") or []:
            if isinstance(code, str):
                found.add(code)
    if found & _SENSITIVE_FAIL_CODES:
        codes.append("sensitive_in_response")
    if found & {"check_error", "unusable_input"}:
        codes.append("check_error")
    if any(mark in lowered for mark in _INJECTION_MARKERS_ASCII) or any(
            mark in raw_text for mark in _INJECTION_MARKERS_RAW):
        codes.append("injection_suspect")
    if _REFERENCE_RE.search(raw_text):
        codes.append("unknown_reference")
    if any(mark in raw_text for mark in _WEAKEN_MARKERS_RAW) or any(
            mark in lowered for mark in _WEAKEN_MARKERS_ASCII):
        codes.append("weakens_safety")
    if codes:
        return {"ok": False, "codes": codes, "draft": None,
                "unverified_claims": []}
    draft = _shape_draft(obj)
    return {"ok": True, "codes": [], "draft": draft,
            "unverified_claims": _collect_unverified_claims(draft)}


async def run_job(store, pid, job_id, text, runner, *,
                  timeout=SEND_TIMEOUT_SECONDS) -> dict:
    """承認済み本文をランナー経由で1回だけ送り、下書きとして保管する。"""
    try:
        job = store.get(pid, KIND, job_id)
    except (ValueError, KeyError, TypeError, AttributeError):
        return {"job": None}
    if not isinstance(job, dict):
        return {"job": None}
    if job.get("status") != "sending":
        return {"job": dict(job)}
    provider = job.get("provider")
    packet_sha = job.get("packet_sha256")
    sending = dict(job)
    sending["send_started"] = True
    sending["updated_at"] = time.time()
    store.put(pid, KIND, job_id, sending)
    try:
        raw = await asyncio.wait_for(
            runner(provider, text, PLAN_SYSTEM_PROMPT, MAX_TOKENS), timeout)
    except asyncio.TimeoutError:
        try:
            fresh = store.get(pid, KIND, job_id)
        except (ValueError, KeyError, TypeError, AttributeError):
            fresh = None
        if isinstance(fresh, dict) and fresh.get("status") == "cancelled":
            return {"job": dict(fresh)}
        base = dict(fresh) if isinstance(fresh, dict) else dict(sending)
        base["status"] = "outcome_unknown"
        base["result_class"] = "outcome_unknown"
        base["codes"] = ["timeout"]
        base["model"] = ""
        base["usage"] = {}
        base["external_draft"] = None
        base["unverified_claims"] = []
        base["error_class"] = ""
        base["updated_at"] = time.time()
        store.put(pid, KIND, job_id, base)
        _audit_finished(store, pid, base, "outcome_unknown", ["timeout"], "", {})
        return {"job": dict(base)}
    except Exception as exc:
        try:
            fresh = store.get(pid, KIND, job_id)
        except (ValueError, KeyError, TypeError, AttributeError):
            fresh = None
        base = dict(fresh) if isinstance(fresh, dict) else dict(sending)
        base["status"] = "failed"
        base["result_class"] = "failed"
        base["codes"] = []
        base["model"] = ""
        base["usage"] = {}
        base["external_draft"] = None
        base["unverified_claims"] = []
        base["error_class"] = type(exc).__name__
        base["updated_at"] = time.time()
        store.put(pid, KIND, job_id, base)
        _audit_finished(store, pid, base, "failed", [], "", {})
        return {"job": dict(base)}
    try:
        fresh = store.get(pid, KIND, job_id)
    except (ValueError, KeyError, TypeError, AttributeError):
        fresh = None
    if isinstance(fresh, dict) and fresh.get("status") != "sending":
        return {"job": dict(fresh)}
    base = dict(fresh) if isinstance(fresh, dict) else dict(sending)
    response_text = raw.get("text") if isinstance(raw, dict) else ""
    model = raw.get("model") if isinstance(raw, dict) else ""
    usage = raw.get("usage") if isinstance(raw, dict) else {}
    if not isinstance(model, str):
        model = ""
    if not isinstance(usage, dict):
        usage = {}
    checked = validate_response(response_text)
    if not checked.get("ok"):
        result_codes = list(checked.get("codes") or [])
        base["status"] = "rejected_response"
        base["result_class"] = "rejected_response"
        base["codes"] = result_codes
        base["model"] = ""
        base["usage"] = {}
        base["external_draft"] = None
        base["unverified_claims"] = []
        base["error_class"] = ""
        base["updated_at"] = time.time()
        store.put(pid, KIND, job_id, base)
        _audit_finished(store, pid, base, "rejected_response", result_codes,
                        "", {})
        return {"job": dict(base)}
    base["status"] = "external_generated"
    base["result_class"] = "external_generated"
    base["codes"] = []
    base["model"] = model
    base["usage"] = dict(usage)
    base["external_draft"] = checked.get("draft")
    base["unverified_claims"] = list(checked.get("unverified_claims") or [])
    base["error_class"] = ""
    base["updated_at"] = time.time()
    store.put(pid, KIND, job_id, base)
    _audit_finished(store, pid, base, "external_generated", [], model, usage)
    return {"job": dict(base)}


def public_view(job) -> dict:
    """ジョブのレスポンス用。text(送信本文)は含めない。"""
    source = dict(job) if isinstance(job, dict) else {}
    return {
        "id": source.get("id"),
        "pid": source.get("pid"),
        "packet_id": source.get("packet_id"),
        "provider": source.get("provider"),
        "purpose": source.get("purpose"),
        "plan_signature": source.get("plan_signature"),
        "packet_sha256": source.get("packet_sha256"),
        "idempotency_key": source.get("idempotency_key"),
        "status": source.get("status"),
        "boot_id": source.get("boot_id"),
        "send_started": bool(source.get("send_started")),
        "created_at": source.get("created_at"),
        "updated_at": source.get("updated_at"),
        "model": source.get("model", ""),
        "usage": dict(source.get("usage") or {}),
        "result_class": source.get("result_class", ""),
        "codes": list(source.get("codes") or []),
        "external_draft": source.get("external_draft"),
        "unverified_claims": list(source.get("unverified_claims") or []),
        "error_class": source.get("error_class", ""),
        "request_template": PLAN_SYSTEM_PROMPT,
    }
