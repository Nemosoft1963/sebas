"""Safety-first Next Action Controller."""
from __future__ import annotations

import inspect
import threading
import time
from typing import Callable

from app.goal_completion_flag import enabled
from app.goal_completion_store import GoalCompletionStore
from app.goal_state_machine import read_goal_state
from app.pending_ledger import decide_action
from app.recovery_policy import RecoveryBudget, RecoveryStopped
from app.workflow_readiness import UNIMPLEMENTED_ACTIONS, build_readiness


class NextActionRefused(RuntimeError):
    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or code)


EXECUTORS: dict[str, Callable] = {}
_BUDGETS: dict[tuple[str, str, str], RecoveryBudget] = {}
_BUDGET_LOCK = threading.Lock()
# After RecoveryBudget.recovery_seconds (300) elapses, wait this long before
# replacing the budget. Count limits are never cleared by time.
BUDGET_TIME_COOLDOWN_SECONDS = 600.0
UNIMPLEMENTED_ACTION_IDS = {ident for ident, _ in UNIMPLEMENTED_ACTIONS}
UNIMPLEMENTED_BLOCKED_ACTIONS = [
    {"id": ident, "reason": reason} for ident, reason in UNIMPLEMENTED_ACTIONS
]


def _store(manager) -> GoalCompletionStore:
    return GoalCompletionStore(manager.memory.path)


def _criterion(readiness: dict) -> str | None:
    failed = list((readiness.get("gate") or {}).get("failed_criteria") or [])
    if not failed:
        return None
    first = failed[0]
    if isinstance(first, dict):
        return str(first.get("id") or first.get("criterion_id") or "") or None
    return str(first) or None


def _allowed(readiness: dict, action_id: str) -> tuple[bool, str]:
    row = (readiness.get("allowed_actions") or {}).get(action_id)
    if isinstance(row, dict):
        return bool(row.get("allowed")), str(row.get("reason") or "")
    if isinstance(row, bool):
        return row, ""
    return False, "action is not allowed"


def _prior_answers_available(manager, project_id: str) -> bool:
    """Read-only: True only when at least one prior answer would apply exactly."""
    try:
        from app.vehicle_service import preview_prior_answers
        from app.vehicle_workflow import load_input

        envelope = load_input(manager, project_id)
        if not envelope:
            return False
        preview = preview_prior_answers(envelope)
        return bool(preview.get("applied"))
    except Exception:
        return False


def _compute_from(manager, project_id: str, readiness: dict) -> dict:
    action = readiness.get("next_action") or {}
    action_id = str(action.get("id") or "idle")
    action_class = str(action.get("class") or "development")
    auto = bool(action.get("auto_executable"))
    label = action.get("label") or ""
    endpoint = action.get("endpoint")
    allowed_actions = readiness.get("allowed_actions") or {}
    if action_id == "confirm_allocation" and _prior_answers_available(manager, project_id):
        action_id = "apply_prior_answers"
        action_class = "local_safe"
        auto = True
        # Keep the existing gate: prior answers do not override a blocked action.
        allowed_actions = dict(allowed_actions)
        allowed_actions[action_id] = allowed_actions.get(action_id, {"allowed": False, "reason": "確認事項の適用は許可されていません"})
        label = "既回答を条件一致の確認事項へ適用する"
        endpoint = None

    decision = decide_action(action_id, action_class, auto, allowed_actions,
                             str(readiness.get("stop_reason") or ""),
                             unimplemented_ids=UNIMPLEMENTED_ACTION_IDS)

    state_view = read_goal_state(manager, project_id)
    return {
        "action_id": action_id,
        "label": label,
        "endpoint": endpoint,
        "action_class": action_class,
        "auto_executable": decision["auto_executable"],
        "manual_executable": decision["manual_executable"],
        "executable": decision["executable"],
        "allowed": decision["allowed"],
        "reason": decision["reason"],
        "blocked": decision["blocked"],
        "navigable": decision["navigable"],
        "criterion": _criterion(readiness),
        "state": state_view.get("state") or "",
        "blocked_actions": list(UNIMPLEMENTED_BLOCKED_ACTIONS),
    }


def compute(manager, project_id: str) -> dict:
    """Compute only; this function never initializes or writes the NAC ledger."""
    return _compute_from(manager, project_id, build_readiness(manager, project_id))


def _require_enabled(manager, project_id: str) -> None:
    if not enabled(manager.memory.path, project_id):
        raise NextActionRefused("FLAG_OFF", "goal completion flag is off")


def _invoke(executor: Callable, manager, project_id: str, key: str, action: dict):
    result = executor(manager, project_id, key, action)
    if inspect.isawaitable(result):
        raise NextActionRefused("ASYNC_EXECUTOR", "async executor requires the web adapter")
    return result


def _one(manager, project_id: str, key: str) -> dict:
    store = _store(manager)
    previous = store.get_nac_execution(project_id, key)
    if previous:
        return previous["result"]
    before = build_readiness(manager, project_id)
    action = _compute_from(manager, project_id, before)
    action_id = action["action_id"]
    if action_id in UNIMPLEMENTED_ACTION_IDS:
        raise NextActionRefused("ACTION_REFUSED", action["reason"])
    if not action["auto_executable"]:
        raise NextActionRefused("ACTION_REFUSED", action["reason"] or "automatic execution is not permitted")
    executor = EXECUTORS.get(action_id)
    if executor is None:
        raise NextActionRefused("EXECUTOR_NOT_REGISTERED", action_id)
    try:
        value = _invoke(executor, manager, project_id, key, action)
        after = build_readiness(manager, project_id)
        result = {"status": "succeeded", "action": action, "execution_result": value,
                  "before": before, "next_action": _compute_from(manager, project_id, after)}
        store.append_nac_execution(project_id, key, action_id, "succeeded", result)
        return result
    except Exception as exc:
        result = {"status": "failed", "action": action,
                  "error": f"{type(exc).__name__}: {exc}", "needs_approval": True}
        store.append_nac_execution(project_id, key, action_id, "failed", result)
        return result


def execute(manager, project_id: str, idempotency_key: str, *, chain: bool = False) -> dict:
    key = str(idempotency_key or "").strip()
    if not key:
        raise NextActionRefused("EMPTY_IDEMPOTENCY_KEY", "idempotency_key is required")
    _require_enabled(manager, project_id)
    if chain:
        return run_chain(manager, project_id, key)
    return _one(manager, project_id, key)


def _count_limit_exceeded(budget: RecoveryBudget) -> bool:
    return (
        budget.tool_calls >= budget.tool_limit
        or budget.cycles >= budget.cycle_limit
        or budget.llm_calls >= budget.llm_limit
    )


def _time_budget_replaceable(budget: RecoveryBudget) -> bool:
    """Replace only after time budget + cooldown. Never clear count limits by time."""
    if budget.started is None:
        return False
    if _count_limit_exceeded(budget):
        return False
    if budget.remaining() > 0:
        return False
    elapsed = time.monotonic() - budget.started
    return elapsed >= (budget.recovery_seconds + BUDGET_TIME_COOLDOWN_SECONDS)


def _budget(manager, project_id: str, criterion: str | None) -> RecoveryBudget:
    key = (str(manager.memory.path), project_id, criterion or "__unknown__")
    with _BUDGET_LOCK:
        budget = _BUDGETS.get(key)
        if budget is None:
            budget = RecoveryBudget()
            _BUDGETS[key] = budget
            return budget
        if _time_budget_replaceable(budget):
            budget = RecoveryBudget()
            _BUDGETS[key] = budget
        return budget


def reset_budget(manager, project_id: str, criterion: str | None = None) -> None:
    key = (str(manager.memory.path), project_id, criterion or "__unknown__")
    with _BUDGET_LOCK:
        _BUDGETS.pop(key, None)


def run_chain(manager, project_id: str, idempotency_key: str, *, max_steps: int = 5) -> dict:
    key = str(idempotency_key or "").strip()
    if not key:
        raise NextActionRefused("EMPTY_IDEMPOTENCY_KEY", "idempotency_key is required")
    _require_enabled(manager, project_id)
    steps, previous_id = [], None
    stopped = "idle"
    needs_approval = False
    for index in range(min(max(1, int(max_steps)), 5)):
        action = compute(manager, project_id)
        action_id, action_class = action["action_id"], action["action_class"]
        if action_id in {"idle", "wait"}:
            stopped = "idle"
            break
        if action_id in UNIMPLEMENTED_ACTION_IDS or not action["auto_executable"]:
            stopped = "external" if action_class == "external" else "human_required"
            needs_approval = True
            break
        if previous_id == action_id:
            stopped, needs_approval = "repeat", True
            break
        budget = _budget(manager, project_id, action.get("criterion"))
        try:
            budget.consume("tool")
            budget.recover("chain_step", f"{project_id}:{action_id}:{index}", "execute")
        except RecoveryStopped:
            stopped, needs_approval = "budget", True
            break
        result = _one(manager, project_id, f"{key}:{index + 1}")
        steps.append(result)
        previous_id = action_id
        if result.get("status") != "succeeded":
            stopped, needs_approval = "budget", True
            break
    else:
        stopped = "max_steps"
        needs_approval = True
    return {"status": "stopped", "stopped_reason": stopped, "steps": steps,
            "needs_approval": needs_approval, "next_action": compute(manager, project_id)}


def execute_prepare(manager, project_id: str, key: str, action: dict):
    from app.vehicle_service import run_prepare
    return run_prepare(manager, project_id, idempotency_key=key)


def execute_apply_prior_answers(manager, project_id: str, key: str, action: dict):
    from app.vehicle_service import apply_prior_answers
    return apply_prior_answers(manager, project_id)


def register_default_executors(*, overwrite: bool = False) -> None:
    """Register built-in local_safe executors. Tests may overwrite EXECUTORS."""
    defaults = {
        "prepare": execute_prepare,
        "apply_prior_answers": execute_apply_prior_answers,
    }
    for action_id, fn in defaults.items():
        if overwrite or action_id not in EXECUTORS:
            EXECUTORS[action_id] = fn


register_default_executors()
