"""Optional hooks used by plan generate/approve when goal_completion_v1 is on."""
from __future__ import annotations

import logging

from app.goal_completion_flag import enabled
from app.goal_contract import activate, from_mission, put_draft
from app.plan_coverage import ensure_approvable, save as save_coverage, build as build_coverage
from app.requirement_retention import ensure_plannable


LOGGER = logging.getLogger(__name__)
_BUSINESS_REJECTIONS = ("RETENTION_FAILED", "COVERAGE_INCOMPLETE")


def _is_business_rejection(exc: ValueError) -> bool:
    return any(code in str(exc) for code in _BUSINESS_REJECTIONS)


def before_generate(manager, project_id: str) -> dict | None:
    if not enabled(manager.memory.path, project_id):
        return None
    try:
        contract = put_draft(manager, project_id, from_mission(manager.memory.get_mission(project_id)))
        ensure_plannable(manager, project_id, contract)
        return contract
    except ValueError as exc:
        if _is_business_rejection(exc):
            raise
        LOGGER.warning("goal_completion: before_generate failed: %s", exc, exc_info=True)
    except Exception as exc:
        LOGGER.warning("goal_completion: before_generate failed: %s", exc, exc_info=True)
    return None


def after_generate(manager, project_id: str) -> dict | None:
    # Preview APIs calculate from the mission, so the disabled path needs no ledger write.
    if not enabled(manager.memory.path, project_id):
        return None
    try:
        mission = manager.memory.get_mission(project_id)
        contract = activate(manager, project_id, from_mission(mission))
        coverage = save_coverage(manager, project_id, build_coverage(mission, contract))
        return {"contract": contract, "coverage": coverage}
    except Exception as exc:
        LOGGER.warning("goal_completion: after_generate failed: %s", exc, exc_info=True)
        return None


def before_approve(manager, project_id: str) -> dict | None:
    if not enabled(manager.memory.path, project_id):
        return None
    try:
        from app.goal_contract import preview

        contract = preview(manager, project_id)
        return ensure_approvable(manager, project_id, contract)
    except ValueError as exc:
        if _is_business_rejection(exc):
            raise
        LOGGER.warning("goal_completion: before_approve failed: %s", exc, exc_info=True)
    except Exception as exc:
        LOGGER.warning("goal_completion: before_approve failed: %s", exc, exc_info=True)
    return None
