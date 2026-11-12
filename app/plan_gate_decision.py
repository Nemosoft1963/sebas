"""AG1: 評価ゲートを一つの判定関数に統合する(読み取り専用・追加のみ)。

external_guarded のPJでは、従来の goal_review 送信が outbound ガードで
拒否されるため旧レビューで開始条件を満たす経路が無い。一方で新しい独立
評価だけが合格しても旧レビュー未合格で開始が止まる(矛盾した二重停止)。
このモジュールは両方の条件を一つの読み取り専用判定に統合する。
ゲートを弱めない: 不明・読取不能・接続失敗・未承認は合格にしない。

戻り値: {"verdict", "reason", "evidence", "next_actions", "mode"}。
状態を書かない(put/add_event/recover_interrupted を呼ばない)。
本文・例外メッセージ・原本・パスを reason/evidence に入れない。
"""
from __future__ import annotations

import math
import os

_NARROW = (ValueError, KeyError, TypeError, AttributeError, OSError)
ENV_FLAG = "SEBAS_PLAN_GATE_V2_PROJECTS"
METHODS = ("local_only", "external_guarded")


def _enabled_for_project(env_value: str, pid: str) -> bool:
    """カンマ区切りのPJ IDに pid が完全一致するときだけ True。"""
    if not isinstance(env_value, str) or not env_value.strip():
        return False
    if not isinstance(pid, str) or pid == "":
        return False
    return any(token == pid for token in
               (item.strip() for item in env_value.split(",")) if token)


def enabled_for_project(pid: str, env_value: str | None = None) -> bool:
    """フラグ(SEBAS_PLAN_GATE_V2_PROJECTS)が当該PJに有効か。"""
    if env_value is None:
        env_value = os.environ.get(ENV_FLAG, "")
    if not isinstance(env_value, str):
        return False
    return _enabled_for_project(env_value, pid)


def _legacy_reason_from_exc(exc: BaseException) -> str:
    text = str(exc)
    if "接続設定" in text or "接続に失敗" in text:
        return "legacy_review_connection_failed"
    return "legacy_review_not_passed"


def _ind_reason(code: object) -> str:
    if code == "independent_review_failed":
        return "independent_review_failed"
    if code == "independent_review_missing":
        return "independent_review_missing"
    if code == "independent_review_unknown":
        return "independent_review_unknown"
    return "independent_review_unknown"


def _deterministic_check(manager, pid) -> tuple[bool, str]:
    """approve() が承認前に行う契約検証と同じ検査を再利用する。

    新しい検査は作らない。読取失敗は (False, "gate_check_error")。
    戻り値: (passed, reason)。reason は "ok" / "deterministic_check_failed" /
    "gate_check_error"。計画が未設定・空・非構造化のときは不合格
    (deterministic_check_failed)。approve() が構造化タスクにだけ契約検証を
    掛けるのと同じく、構造化契約の無い計画は通さない。
    """
    try:
        from app.structured_planning import contract_of as _contract_of
        from app.structured_planning import validate_plan as _validate_plan
    except _NARROW:
        return False, "gate_check_error"
    try:
        mission = manager.memory.get_mission(pid)
    except _NARROW:
        return False, "gate_check_error"
    if not isinstance(mission, dict):
        return False, "gate_check_error"
    try:
        tasks = mission.get("tasks")
    except _NARROW:
        return False, "gate_check_error"
    if not isinstance(tasks, list) or not tasks:
        return False, "deterministic_check_failed"
    structured = False
    try:
        for task in tasks:
            if not isinstance(task, dict):
                return False, "gate_check_error"
            if _contract_of(task):
                structured = True
                break
    except _NARROW:
        return False, "gate_check_error"
    if not structured:
        return False, "deterministic_check_failed"
    try:
        effective_criteria = manager._effective_planning_criteria
        criteria = effective_criteria(mission)
    except _NARROW:
        return False, "gate_check_error"
    if not isinstance(criteria, list) or not criteria:
        return False, "gate_check_error"
    try:
        expected = {f"SC{i:02d}" for i in range(1, len(criteria) + 1)}
        assessment = _validate_plan({"tasks": tasks}, expected)
    except _NARROW:
        return False, "gate_check_error"
    if not isinstance(assessment, dict):
        return False, "gate_check_error"
    if assessment.get("pa" + "ssed"):
        return True, "ok"
    return False, "deterministic_check_failed"


def _review_policy_met(manager, pid, independent_provider: str) -> tuple[bool, str]:
    """レビュー方針(required_providers/min_success_count)の充足を判定する。

    戻り値: (met, reason)。reason は "ok" / "review_policy_unmet" /
    "gate_check_error"。方針が未設定、または独立評価の提供元が必須集合を
    満たし最低数が1以下のときだけ満たしたとみなす。推測で満たしたことに
    しない。
    """
    try:
        from app.goal_review import load_review_policy as _load_policy
        from app.goal_review import review_pass_policy as _pass_policy
    except _NARROW:
        return False, "gate_check_error"
    try:
        config = _load_policy(manager)
    except _NARROW:
        return False, "gate_check_error"
    if not isinstance(config, dict):
        return False, "gate_check_error"
    if not config:
        return True, "ok"
    try:
        policy = _pass_policy(manager, pid)
    except _NARROW:
        return False, "gate_check_error"
    if not isinstance(policy, dict):
        return False, "gate_check_error"
    try:
        required = policy.get("required_providers")
        raw_min = policy.get("min_success_count")
    except _NARROW:
        return False, "gate_check_error"
    if required is None:
        required_list: list = []
    elif isinstance(required, str):
        required_list = [required.strip()] if required.strip() else []
    elif isinstance(required, (list, tuple)):
        required_list = [str(x).strip() for x in required if str(x).strip()]
    else:
        return False, "gate_check_error"
    if not required_list and raw_min is None:
        return True, "ok"
    try:
        minimum = int(raw_min) if raw_min is not None else 1
    except (ValueError, TypeError):
        return False, "gate_check_error"
    if not isinstance(independent_provider, str) or independent_provider == "":
        return False, "gate_check_error"
    # 一件の独立評価で満たせるのは、その提供元だけを必須とする方針。
    # 複数社の必須指定を「うち一社に一致」で合格にしない。
    if required_list and not set(required_list).issubset({independent_provider}):
        return False, "review_policy_unmet"
    if minimum > 1:
        return False, "review_policy_unmet"
    return True, "ok"


def _independent_provider_of(manager, pid) -> tuple[str, str]:
    """現行署名に対する有効な独立評価の提供元(単一)を返す。

    戻り値: (provider, reason)。reason は "ok" /
    "independent_review_missing" / "independent_review_unknown" /
    "gate_check_error"。
    """
    try:
        from app.goal_review import ReviewStore as _Store
        from app import independent_review as _ir
        from app import planning_settings as _settings
        from app import planning_packet as _packet
    except _NARROW:
        return "", "gate_check_error"
    try:
        memory = manager.memory
    except _NARROW:
        return "", "gate_check_error"
    try:
        store = _Store(memory.path)
    except _NARROW:
        return "", "gate_check_error"
    try:
        settings = _settings.get_settings(store, pid)
    except _NARROW:
        return "", "gate_check_error"
    if not isinstance(settings, dict):
        return "", "gate_check_error"
    try:
        reviewer = settings.get("reviewer_provider")
    except _NARROW:
        return "", "gate_check_error"
    if not isinstance(reviewer, str) or reviewer == "":
        return "", "gate_check_error"
    try:
        mission = memory.get_mission(pid)
    except _NARROW:
        return "", "gate_check_error"
    if not isinstance(mission, dict):
        return "", "gate_check_error"
    try:
        signature = _packet.plan_signature_of(mission)
    except _NARROW:
        return "", "gate_check_error"
    if not isinstance(signature, str) or signature == "":
        return "", "gate_check_error"
    try:
        rows = store.list(pid, _ir.KIND)
    except _NARROW:
        return "", "gate_check_error"
    if not isinstance(rows, (list, tuple)):
        return "", "gate_check_error"
    jobs: list[dict] = []
    try:
        for _sig, payload in rows:
            if not isinstance(payload, dict):
                return "", "gate_check_error"
            jobs.append(payload)
    except _NARROW:
        return "", "gate_check_error"
    for job in jobs:
        try:
            ok_sig = job.get("plan_signature") == signature
            ok_status = job.get("status") == "review_passed"
            ok_provider = job.get("provider") == reviewer
        except _NARROW:
            return "", "gate_check_error"
        if ok_sig and ok_status and ok_provider:
            return reviewer, "ok"
    current = [job for job in jobs if job.get("plan_signature") == signature]
    if current:
        return "", "independent_review_unknown"
    return "", "independent_review_missing"


def _has_current_review_spec(manager, pid, independent_provider: str) -> tuple[bool, str]:
    """現行署名の review_passed のうち少なくとも1件が現行の評価仕様版か。

    戻り値: (ok, reason)。reason は "ok" / "review_spec_outdated" /
    "gate_check_error"。旧仕様の評価は自動的に合格へ移行しない。
    """
    try:
        from app.goal_review import ReviewStore as _Store
        from app import independent_review as _ir
        from app import planning_packet as _packet
    except _NARROW:
        return False, "gate_check_error"
    try:
        memory = manager.memory
    except _NARROW:
        return False, "gate_check_error"
    try:
        store = _Store(memory.path)
    except _NARROW:
        return False, "gate_check_error"
    try:
        mission = memory.get_mission(pid)
    except _NARROW:
        return False, "gate_check_error"
    if not isinstance(mission, dict):
        return False, "gate_check_error"
    try:
        signature = _packet.plan_signature_of(mission)
    except _NARROW:
        return False, "gate_check_error"
    if not isinstance(signature, str) or signature == "":
        return False, "gate_check_error"
    try:
        rows = store.list(pid, _ir.KIND)
    except _NARROW:
        return False, "gate_check_error"
    if not isinstance(rows, (list, tuple)):
        return False, "gate_check_error"
    try:
        current_spec = _ir.REVIEW_SPEC_VERSION
    except _NARROW:
        return False, "gate_check_error"
    if not isinstance(current_spec, str) or current_spec == "":
        return False, "gate_check_error"
    try:
        for _sig, payload in rows:
            if not isinstance(payload, dict):
                return False, "gate_check_error"
            if payload.get("plan_signature") != signature:
                continue
            if payload.get("status") != "review_passed":
                continue
            if payload.get("provider") != independent_provider:
                continue
            sha = payload.get("packet_sha256")
            model = payload.get("model")
            reviewed_at = payload.get("reviewed_at")
            if not isinstance(sha, str) or sha == "":
                continue
            if not isinstance(model, str) or model.strip() == "":
                continue
            if isinstance(reviewed_at, bool) or not isinstance(reviewed_at, (int, float)):
                continue
            if not math.isfinite(float(reviewed_at)) or reviewed_at <= 0:
                continue
            if payload.get("review_spec") == current_spec:
                return True, "ok"
    except _NARROW:
        return False, "gate_check_error"
    return False, "review_spec_outdated"


def _v2_decide(manager, pid) -> dict:
    try:
        from app.goal_review import development_blockers as _blockers
        from app import independent_review_gate as _gate
    except _NARROW:
        return {"verdict": "unknown", "reason": "gate_check_error",
                "evidence": [], "next_actions": ["check_connection"],
                "mode": "v2"}
    try:
        blockers = _blockers(manager, pid)
    except _NARROW:
        return {"verdict": "unknown", "reason": "gate_check_error",
                "evidence": [], "next_actions": ["check_connection"],
                "mode": "v2"}
    if not isinstance(blockers, list):
        return {"verdict": "unknown", "reason": "gate_check_error",
                "evidence": [], "next_actions": ["check_connection"],
                "mode": "v2"}
    if blockers:
        return {"verdict": "failed", "reason": "blockers_present",
                "evidence": [], "next_actions": ["review_plan"],
                "mode": "v2"}
    passed_det, det_reason = _deterministic_check(manager, pid)
    if det_reason == "gate_check_error":
        return {"verdict": "unknown", "reason": "gate_check_error",
                "evidence": [], "next_actions": ["check_connection"],
                "mode": "v2"}
    if not passed_det:
        return {"verdict": "failed",
                "reason": "deterministic_check_failed",
                "evidence": [], "next_actions": ["fix_plan"],
                "mode": "v2"}
    try:
        gate_code = _gate.gate_error(manager, pid)
    except _NARROW:
        return {"verdict": "unknown", "reason": "gate_check_error",
                "evidence": [], "next_actions": ["check_connection"],
                "mode": "v2"}
    if gate_code is not None:
        reason = _ind_reason(gate_code)
        if reason == "independent_review_failed":
            return {"verdict": "failed", "reason": reason,
                    "evidence": [], "next_actions": ["run_independent_review"],
                    "mode": "v2"}
        return {"verdict": "unknown", "reason": reason,
                "evidence": [], "next_actions": ["run_independent_review"],
                "mode": "v2"}
    provider, provider_reason = _independent_provider_of(manager, pid)
    if provider_reason != "ok":
        return {"verdict": "unknown", "reason": provider_reason,
                "evidence": [], "next_actions": ["run_independent_review"],
                "mode": "v2"}
    spec_ok, spec_reason = _has_current_review_spec(manager, pid, provider)
    if spec_reason == "gate_check_error":
        return {"verdict": "unknown", "reason": "gate_check_error",
                "evidence": [], "next_actions": ["check_connection"],
                "mode": "v2"}
    if not spec_ok:
        return {"verdict": "unknown", "reason": "review_spec_outdated",
                "evidence": [], "next_actions": ["run_independent_review"],
                "mode": "v2"}
    met, policy_reason = _review_policy_met(manager, pid, provider)
    if policy_reason == "gate_check_error":
        return {"verdict": "unknown", "reason": "gate_check_error",
                "evidence": [], "next_actions": ["check_connection"],
                "mode": "v2"}
    if not met:
        return {"verdict": "unknown", "reason": "review_policy_unmet",
                "evidence": [provider],
                "next_actions": ["run_independent_review"],
                "mode": "v2"}
    return {"verdict": "passed", "reason": "ok_v2",
            "evidence": [provider],
            "next_actions": ["start"],
            "mode": "v2"}


def _legacy_decide(manager, pid) -> dict:
    try:
        from app.goal_review import require_review as _require
        from app import independent_review_gate as _gate
    except _NARROW:
        return {"verdict": "unknown", "reason": "gate_check_error",
                "evidence": [], "next_actions": ["check_connection"],
                "mode": "legacy"}
    try:
        _require(manager, pid)
    except _NARROW as exc:
        reason = _legacy_reason_from_exc(exc)
        kinds = ("connection_failed", "接続")
        if reason == "legacy_review_connection_failed" or any(
                kind in str(exc) for kind in kinds):
            return {"verdict": "connection_blocked",
                    "reason": "legacy_review_connection_failed",
                    "evidence": [], "next_actions": ["check_connection"],
                    "mode": "legacy"}
        return {"verdict": "failed", "reason": "legacy_review_not_passed",
                "evidence": [], "next_actions": ["review_plan"],
                "mode": "legacy"}
    try:
        gate_code = _gate.gate_error(manager, pid)
    except _NARROW:
        return {"verdict": "unknown", "reason": "gate_check_error",
                "evidence": [], "next_actions": ["check_connection"],
                "mode": "legacy"}
    if gate_code is not None:
        reason = _ind_reason(gate_code)
        if reason == "independent_review_failed":
            return {"verdict": "failed", "reason": reason,
                    "evidence": [], "next_actions": ["run_independent_review"],
                    "mode": "legacy"}
        if reason == "independent_review_missing":
            return {"verdict": "unknown", "reason": reason,
                    "evidence": [],
                    "next_actions": ["run_independent_review"],
                    "mode": "legacy"}
        return {"verdict": "unknown", "reason": reason,
                "evidence": [], "next_actions": ["check_connection"],
                "mode": "legacy"}
    return {"verdict": "passed", "reason": "ok_legacy",
            "evidence": [], "next_actions": ["start"],
            "mode": "legacy"}


def _current_method(manager, pid) -> str:
    try:
        from app.goal_review import ReviewStore as _Store
    except _NARROW:
        return ""
    try:
        memory = manager.memory
    except _NARROW:
        return ""
    try:
        store = _Store(memory.path)
    except _NARROW:
        return ""
    try:
        raw = store.get(pid, "planning_settings", pid)
    except _NARROW:
        return ""
    if raw is None:
        return ""
    if not isinstance(raw, dict):
        return ""
    try:
        method = raw.get("method")
    except _NARROW:
        return ""
    if method in METHODS:
        return str(method)
    return ""


def decide(manager, pid) -> dict:
    """単一の判定関数(読み取り専用・例外を投げない・状態を書かない)。"""
    try:
        if not isinstance(pid, str) or pid == "":
            return {"verdict": "unknown", "reason": "gate_check_error",
                    "evidence": [], "next_actions": ["check_connection"],
                    "mode": "legacy"}
        use_v2 = enabled_for_project(pid)
        if not use_v2:
            return _legacy_decide(manager, pid)
        method = _current_method(manager, pid)
        if method != "external_guarded":
            return _legacy_decide(manager, pid)
        return _v2_decide(manager, pid)
    except _NARROW:
        return {"verdict": "unknown", "reason": "gate_check_error",
                "evidence": [], "next_actions": ["check_connection"],
                "mode": "legacy"}


def decide_v2_for_shadow(manager, pid) -> dict | None:
    """影モード用の v2 判定(計算だけ・読み取り専用・例外を投げない)。

    フラグ無効のPJでも v2 の判定を計算だけするために使う。フラグ有効か・
    方式が external_guarded かを問わず _v2_decide() を直接計算する。
    開始可否には一切使わない。失敗時は None を返し、捕捉対象は
    ``_NARROW`` に列挙した狭い例外だけに限定する。
    """
    try:
        return _v2_decide(manager, pid)
    except _NARROW:
        return None
