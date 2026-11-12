"""G4-c: 実行開始の独立評価ゲート(読み取り専用・追加のみ).

`gate_error(manager, pid)` は実行開始を止める固定コードを返す。
通してよければ `None`。本文・原本・例外メッセージを残さない。
状態を書かない(`put` しない、`recover_interrupted` を呼ばない)。
"""
from __future__ import annotations

import math

from app import independent_review as _ir
from app import planning_packet as _packet
from app import planning_settings as _settings
from app.goal_review import ReviewStore

_METHODS = ("local_only", "external_guarded")
_SETTINGS_KIND = "planning_settings"
_NARROW = (ValueError, KeyError, TypeError, AttributeError, OSError)


def _norm_model(value) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


def _valid_reviewed_at(value) -> bool:
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    number = float(value)
    if not math.isfinite(number):
        return False
    return number > 0


def gate_error(manager, pid) -> str | None:
    """実行開始を止める固定コード、または `None`(通してよい)."""
    try:
        memory = manager.memory
    except _NARROW:
        return "independent_review_check_error"
    try:
        path = memory.path
    except _NARROW:
        return "independent_review_check_error"
    try:
        store = ReviewStore(path)
    except _NARROW:
        return "independent_review_check_error"
    try:
        raw = store.get(pid, _SETTINGS_KIND, pid)
    except _NARROW:
        return "independent_review_check_error"
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return "independent_review_check_error"
    raw_method = raw.get("method")
    if raw_method not in _METHODS:
        return "independent_review_check_error"
    try:
        settings = _settings.get_settings(store, pid)
    except _NARROW:
        return "independent_review_check_error"
    if not isinstance(settings, dict):
        return "independent_review_check_error"
    method = settings.get("method")
    if not isinstance(method, str):
        return "independent_review_check_error"
    if raw_method == "local_only":
        return None
    if method != "external_guarded":
        return "independent_review_check_error"
    try:
        mission = memory.get_mission(pid)
    except _NARROW:
        return "independent_review_check_error"
    if not isinstance(mission, dict):
        return "independent_review_check_error"
    try:
        signature = _packet.plan_signature_of(mission)
    except _NARROW:
        return "independent_review_check_error"
    if not isinstance(signature, str) or signature == "":
        return "independent_review_check_error"
    try:
        rows = store.list(pid, _ir.KIND)
    except _NARROW:
        return "independent_review_check_error"
    if not isinstance(rows, (list, tuple)):
        return "independent_review_check_error"
    jobs: list[dict] = []
    try:
        for entry in rows:
            _entry_signature, payload = entry
            if not isinstance(payload, dict):
                return "independent_review_check_error"
            jobs.append(payload)
    except _NARROW:
        return "independent_review_check_error"
    current = [job for job in jobs if job.get("plan_signature") == signature]
    for job in current:
        if job.get("status") == "review_failed":
            return "independent_review_failed"
    independent = settings.get("independent_review")
    reviewer = settings.get("reviewer_provider")
    generator = settings.get("generator_provider")
    for job in current:
        if job.get("status") != "review_passed":
            continue
        sha = job.get("packet_sha256")
        provider = job.get("provider")
        if not isinstance(sha, str) or sha == "":
            continue
        if not isinstance(provider, str) or provider == "":
            continue
        if _norm_model(job.get("model")) == "":
            continue
        if not _valid_reviewed_at(job.get("reviewed_at")):
            continue
        if not bool(independent):
            continue
        if not isinstance(reviewer, str) or provider != reviewer:
            continue
        if isinstance(generator, str) and generator != "" and provider == generator:
            continue
        return None
    if current:
        return "independent_review_unknown"
    return "independent_review_missing"
