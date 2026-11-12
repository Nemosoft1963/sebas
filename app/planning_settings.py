"""G1: PJ別の計画方式・作成AI/評価AI・用途別許可の保存と検査。

純粋な関数群。モジュール内で ReviewStore を作らない。
ストアは get(pid, kind, signature) / put(pid, kind, signature, payload) を持つ。
保存は store.put(pid, "planning_settings", pid, {...}) のみ。
missions テーブルは触らない。allow_external_ai を許可として流用しない。
外部通信・LLM呼出はしない。
"""
from __future__ import annotations

import re
import time

METHODS = ("local_only", "external_guarded")
PURPOSES = (
    "plan_generation",
    "plan_review",
    "capability_review",
    "failover_review",
    "research",
)

_KIND = "planning_settings"
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
# plan_review は評価AI、それ以外は生成AIに束縛する。
_GENERATOR_PURPOSES = frozenset(
    {"plan_generation", "capability_review", "failover_review", "research"}
)


def default_settings() -> dict:
    return {
        "method": "local_only",
        "generator_provider": "",
        "reviewer_provider": "",
        "permissions": {},
        "settings_version": 0,
    }


def _independent_of(settings: dict) -> bool:
    reviewer = settings.get("reviewer_provider") if isinstance(settings, dict) else ""
    generator = settings.get("generator_provider") if isinstance(settings, dict) else ""
    return bool(
        isinstance(reviewer, str)
        and reviewer != ""
        and isinstance(generator, str)
        and reviewer != generator
    )


def _clean_settings(raw: dict | None) -> dict:
    base = default_settings()
    if not isinstance(raw, dict):
        return base
    cleaned = dict(base)
    if raw.get("method") in METHODS:
        cleaned["method"] = raw.get("method")
    for key in ("generator_provider", "reviewer_provider"):
        value = raw.get(key)
        if isinstance(value, str):
            cleaned[key] = value
    permissions = raw.get("permissions")
    if isinstance(permissions, dict):
        cleaned["permissions"] = {
            str(k): v for k, v in permissions.items() if isinstance(v, dict)
        }
    try:
        cleaned["settings_version"] = int(raw.get("settings_version"))
    except (TypeError, ValueError):
        cleaned["settings_version"] = 0
    if cleaned["settings_version"] < 0:
        cleaned["settings_version"] = 0
    return cleaned


def get_settings(store, pid) -> dict:
    saved = store.get(pid, _KIND, pid)
    settings = _clean_settings(saved) if saved is not None else default_settings()
    # 保存済みが壊れていても読むだけで保存しない。旧PJの設定は変えない。
    result = dict(settings)
    result["independent_review"] = _independent_of(settings)
    return result


def _stored_settings(store, pid) -> dict:
    saved = store.get(pid, _KIND, pid)
    if saved is None:
        return default_settings()
    return _clean_settings(saved)


def update_settings(store, pid, changes, known_providers) -> dict:
    if not isinstance(changes, dict):
        raise ValueError("unknown_field")
    allowed_keys = {"method", "generator_provider", "reviewer_provider"}
    for key in changes:
        if key not in allowed_keys:
            raise ValueError("unknown_field")
    known = set(known_providers) if known_providers is not None else set()
    try:
        known = set(known)
    except TypeError:
        raise ValueError("unknown_provider")

    old = _stored_settings(store, pid)

    if "method" in changes:
        method = changes["method"]
        if method not in METHODS:
            raise ValueError("invalid_method")
        new_method = method
    else:
        new_method = old["method"]

    new_generator = old["generator_provider"]
    new_reviewer = old["reviewer_provider"]
    if "generator_provider" in changes:
        value = changes["generator_provider"]
        if not isinstance(value, str) or (value != "" and value not in known):
            raise ValueError("unknown_provider")
        new_generator = value
    if "reviewer_provider" in changes:
        value = changes["reviewer_provider"]
        if not isinstance(value, str) or (value != "" and value not in known):
            raise ValueError("unknown_provider")
        new_reviewer = value

    # 最終形でも接続済み以外を保存しない(fail-closed)。
    for value in (new_generator, new_reviewer):
        if not isinstance(value, str) or (value != "" and value not in known):
            raise ValueError("unknown_provider")

    if new_method == "external_guarded" and new_generator == "":
        raise ValueError("provider_required")

    changed = (
        new_method != old["method"]
        or new_generator != old["generator_provider"]
        or new_reviewer != old["reviewer_provider"]
    )
    if changed:
        revoked = sorted(old.get("permissions", {}).keys())
        new_permissions: dict = {}
        new_version = int(old.get("settings_version", 0)) + 1
    else:
        revoked = []
        new_permissions = dict(old.get("permissions", {}))
        new_version = int(old.get("settings_version", 0))

    new_settings = {
        "method": new_method,
        "generator_provider": new_generator,
        "reviewer_provider": new_reviewer,
        "permissions": new_permissions,
        "settings_version": new_version,
    }
    store.put(pid, _KIND, pid, dict(new_settings))
    return {
        "settings": dict(new_settings),
        "revoked": list(revoked),
        "independent_review": _independent_of(new_settings),
    }


def _is_valid_binding(binding) -> bool:
    if not isinstance(binding, dict):
        return False
    plan_signature = binding.get("plan_signature")
    packet_sha256 = binding.get("packet_sha256")
    provider = binding.get("provider")
    purpose = binding.get("purpose")
    if not isinstance(plan_signature, str) or plan_signature == "":
        return False
    if not isinstance(packet_sha256, str) or _SHA_RE.fullmatch(packet_sha256) is None:
        return False
    if not isinstance(provider, str) or provider == "":
        return False
    if not isinstance(purpose, str) or purpose == "":
        return False
    return True


def _expected_provider(settings: dict, purpose: str) -> str:
    if purpose == "plan_review":
        value = settings.get("reviewer_provider")
    else:
        value = settings.get("generator_provider")
    return value if isinstance(value, str) else ""


def grant_permission(store, pid, purpose, binding, approver) -> dict:
    if purpose not in PURPOSES:
        raise ValueError("unknown_purpose")
    current = _stored_settings(store, pid)
    if current.get("method") != "external_guarded":
        raise ValueError("method_not_external")
    if not _is_valid_binding(binding):
        raise ValueError("invalid_binding")
    if binding.get("purpose") != purpose:
        raise ValueError("invalid_binding")
    expected = _expected_provider(current, purpose)
    if not isinstance(expected, str) or expected == "" or binding.get("provider") != expected:
        raise ValueError("provider_mismatch")
    if not isinstance(approver, str) or approver == "":
        raise ValueError("approver_required")
    permissions = dict(current.get("permissions", {}))
    saved_binding = dict(binding)
    permissions[purpose] = {
        "binding": saved_binding,
        "settings_version": int(current.get("settings_version", 0)),
        "approver": approver,
        "granted_at": time.time(),
    }
    new_settings = dict(current)
    new_settings["permissions"] = permissions
    store.put(pid, _KIND, pid, dict(new_settings))
    return dict(permissions[purpose])


def check_permission(store, pid, purpose, current_binding) -> dict:
    if purpose not in PURPOSES:
        return {"allowed": False, "reason": "unknown_purpose"}
    if not _is_valid_binding(current_binding):
        return {"allowed": False, "reason": "invalid_binding"}
    try:
        saved = store.get(pid, _KIND, pid)
        settings = _clean_settings(saved) if saved is not None else default_settings()
        if settings.get("method") != "external_guarded":
            return {"allowed": False, "reason": "method_not_external"}
        permissions = settings.get("permissions")
        if not isinstance(permissions, dict):
            return {"allowed": False, "reason": "check_error"}
        entry = permissions.get(purpose)
        if entry is None:
            return {"allowed": False, "reason": "no_permission"}
        if not isinstance(entry, dict):
            return {"allowed": False, "reason": "check_error"}
        try:
            saved_version = int(entry.get("settings_version"))
            current_version = int(settings.get("settings_version"))
        except (TypeError, ValueError):
            return {"allowed": False, "reason": "check_error"}
        if saved_version != current_version:
            return {"allowed": False, "reason": "settings_changed"}
        saved_binding = entry.get("binding")
        if not isinstance(saved_binding, dict):
            return {"allowed": False, "reason": "check_error"}
        for field in ("plan_signature", "packet_sha256", "provider", "purpose"):
            if saved_binding.get(field) != current_binding.get(field):
                return {"allowed": False, "reason": f"binding_mismatch:{field}"}
        return {"allowed": True, "reason": "ok"}
    except (ValueError, KeyError, TypeError, AttributeError):
        return {"allowed": False, "reason": "check_error"}


def revoke_permission(store, pid, purpose) -> list:
    if purpose not in PURPOSES:
        raise ValueError("unknown_purpose")
    current = _stored_settings(store, pid)
    permissions = dict(current.get("permissions", {}))
    if purpose in permissions:
        del permissions[purpose]
        revoked = [purpose]
    else:
        revoked = []
    new_settings = dict(current)
    new_settings["permissions"] = permissions
    store.put(pid, _KIND, pid, dict(new_settings))
    return revoked


def revoke_all(store, pid) -> list:
    current = _stored_settings(store, pid)
    revoked = sorted(current.get("permissions", {}).keys())
    new_settings = dict(current)
    new_settings["permissions"] = {}
    store.put(pid, _KIND, pid, dict(new_settings))
    return revoked
