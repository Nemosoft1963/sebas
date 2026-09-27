"""Read-only, bounded human-question view for prepared vehicle projects."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.vehicle_service import state
from app.vehicle_workflow import applicable


def _subject(issue: dict) -> dict:
    return {
        "company": str(issue.get("company") or ""),
        "employee": str(issue.get("allocation_subject") or ""),
    }


def fact_key_for(subject_key: dict, months: list[str] | None, kind: str = "allocation") -> str:
    subject_key = subject_key or {}
    period = ",".join(str(x) for x in (months or []) if x)
    return ":".join((kind or "allocation", str(subject_key.get("company") or ""),
                     str(subject_key.get("employee") or ""), period))


def _confirmed(issue: dict, latest: dict[str, dict]) -> list[dict]:
    month = str(issue.get("month") or "")
    employee = str(issue.get("allocation_subject") or "")
    matches = []
    for fact in latest.values():
        value = fact.get("value") or {}
        subject = value.get("subject_key") or {}
        months = value.get("months") or []
        if employee and str(subject.get("employee") or "") != employee:
            continue
        if month and month not in months:
            continue
        matches.append(fact)
    return matches


def _candidate_rows(issue: dict, choices: list[dict]) -> list[dict]:
    seen, result = set(), []
    raw = list(issue.get("candidates") or []) + list(choices or [])
    for item in raw:
        candidate = dict(item) if isinstance(item, dict) else {"value": item}
        key = repr(sorted(candidate.items()))
        if key in seen:
            continue
        seen.add(key)
        result.append({
            "candidate": candidate,
            "impact": {
                "count": issue.get("count") if issue.get("count") is not None else None,
                "month": issue.get("month") or None,
                "amount": issue.get("amount") if issue.get("amount") is not None else None,
            },
        })
    return result


def _latest_facts(memory_path, project_id: str) -> dict[str, dict]:
    path = Path(memory_path).parent / "goal_completion.sqlite3"
    if not path.exists():
        return {}
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "goal_facts" not in names:
            return {}
        rows = db.execute(
            """SELECT f.* FROM goal_facts f JOIN (
                   SELECT fact_key, MAX(version) AS version FROM goal_facts
                   WHERE project_id=? GROUP BY fact_key
               ) latest ON latest.fact_key=f.fact_key AND latest.version=f.version
               WHERE f.project_id=? ORDER BY f.fact_key""",
            (project_id, project_id),
        ).fetchall()
        result = {}
        for row in rows:
            item = dict(row)
            item["value"] = json.loads(item.pop("value_json"))
            result[item["fact_key"]] = item
        return result
    except (sqlite3.OperationalError, ValueError):
        return {}
    finally:
        db.close()


def build_questions(manager, project_id, limit=5) -> dict:
    """Build a question view without modifying vehicle input or either database."""
    mission = manager.memory.get_mission(project_id)
    base = {
        "applicable": False, "prepared": False, "stale": False,
        "total_open": 0, "omitted": 0, "questions": [],
        "input_hash": None, "plan_version": mission.get("plan_version"),
        "vehicle_choices": [],
    }
    if not applicable(mission):
        return base
    base["applicable"] = True
    current = state(manager, project_id)
    if not current.get("prepared"):
        return base
    base.update(prepared=True, stale=bool(current.get("stale")),
                input_hash=current.get("input_hash"),
                vehicle_choices=list(current.get("vehicle_choices") or []))
    issues = [x for x in current.get("questions") or [] if x.get("kind") != "read"]
    issues.sort(key=lambda x: -(float(x.get("amount") or 0)))
    cap = min(5, max(0, int(limit)))
    latest = _latest_facts(manager.memory.path, project_id)
    cards = []
    for issue in issues[:cap]:
        count = issue.get("count")
        amount = issue.get("amount")
        stop = ""
        if count is not None and amount is not None:
            stop = f"未配賦 {count}件・金額¥{amount} が残るため損益を確定できません"
        elif count is not None:
            stop = f"未解決 {count}件が残るため損益を確定できません"
        month = issue.get("month") or None
        rerun = None
        if month and count is not None:
            rerun = {"months": [month], "count": count, "stages": ["calculate", "verify"]}
        unknown = {
            "message": issue.get("message") or "",
            "source_ref": issue.get("source_ref") or "",
            "locator": issue.get("locator") or "",
        }
        cards.append({
            "id": issue.get("id"), "kind": issue.get("kind"),
            "unknown": unknown, "stop_condition": stop,
            "confirmed_facts": _confirmed(issue, latest),
            "candidates": _candidate_rows(issue, current.get("vehicle_choices") or []),
            "rerun_scope": rerun,
            "answerable": not base["stale"],
            "unanswerable_reason": "原本・要求・抽出版が変わったため再抽出が必要です" if base["stale"] else "",
        })
    base.update(total_open=len(issues), omitted=max(0, len(issues) - len(cards)), questions=cards)
    return base
