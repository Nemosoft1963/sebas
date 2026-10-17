"""Stage 4-A: execution funnel measurement (read-only).

Evidence-only funnel per project:
plan_passed -> plan_approved -> task_executed -> evidence_verified -> criterion_achieved.

Read-only: never approves, executes, sends externally, registers RAG, or applies
plans. No new tables (computed from existing data on each call).
Results are deterministic and errors are never swallowed
(unreadable -> unknown, never pass).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

STAGES = (
    "plan_passed",
    "plan_approved",
    "task_executed",
    "evidence_verified",
    "criterion_achieved",
)

STAGE_JA = {
    "plan_passed": "計画合格",
    "plan_approved": "計画承認",
    "task_executed": "工程実行",
    "evidence_verified": "証拠検証",
    "criterion_achieved": "達成条件",
}

_APPROVED_MISSION = {"ready", "paused", "running", "completed"}


def _truncate(value: object, length: int = 64) -> str:
    return str(value or "")[: max(1, int(length))]


def _short_hash(value: object) -> str:
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = str(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _file_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except (OSError, ValueError):
        return ""


def _ro_connect(path: Path):
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=15)


def _read_review_row(memory_path, project_id: str, kind: str, signature: str):
    """Read goal_reviews without creating files. Returns (row|None, error)."""
    mem = Path(memory_path)
    db_path = mem.parent / "goal_reviews.sqlite3"
    if not db_path.is_file():
        return None, "goal_reviews DBなし"
    try:
        db = _ro_connect(db_path)
    except sqlite3.Error as exc:
        return None, f"goal_reviews接続失敗:{type(exc).__name__}"
    try:
        try:
            names = {r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        except sqlite3.Error as exc:
            return None, f"reviews表確認失敗:{type(exc).__name__}"
        if "reviews" not in names:
            return None, "reviews表なし"
        try:
            row = db.execute(
                "SELECT payload FROM reviews WHERE project=? AND kind=? AND signature=?",
                (project_id, kind, signature)).fetchone()
        except sqlite3.Error as exc:
            return None, f"reviews読取失敗:{type(exc).__name__}"
        if not row:
            return None, ""
        try:
            payload = json.loads(row[0]) if isinstance(row[0], str) else {}
        except (ValueError, TypeError):
            return None, "reviews形式不正"
        if not isinstance(payload, dict):
            return None, "reviews形式不正"
        return payload, ""
    finally:
        try:
            db.close()
        except sqlite3.Error:
            pass


def _safe_mission(manager, project_id: str):
    try:
        mission = manager.memory.get_mission(project_id)
    except Exception as exc:
        return None, f"mission読取失敗:{type(exc).__name__}"
    if not isinstance(mission, dict):
        return None, "mission形式不正"
    return mission, ""


def _safe_signature(manager, project_id: str):
    try:
        from app.goal_review import plan_snapshot

        _snap, signature = plan_snapshot(manager, project_id)
        return str(signature or ""), ""
    except Exception as exc:
        return "", f"plan_snapshot失敗:{type(exc).__name__}"


def _safe_contract(manager, project_id: str, mission: dict):
    """Read contract without creating goal_completion DB when missing."""
    mem = Path(manager.memory.path)
    db_path = mem.parent / "goal_completion.sqlite3"
    if not db_path.is_file():
        try:
            from app.goal_contract import from_mission

            return from_mission(mission, manager, project_id), "goal_completion DBなしのためmissionから導出"
        except Exception as exc:
            return None, f"contract導出失敗:{type(exc).__name__}"
    try:
        from app.goal_contract import preview

        body = preview(manager, project_id)
    except Exception as exc:
        return None, f"contract読取失敗:{type(exc).__name__}"
    if not isinstance(body, dict):
        return None, "contract形式不正"
    return body, ""


def _plan_passed_raw(manager, mission: dict, signature: str, plan_row):
    """Return (passed, claimed, reason). Structural-only never counts."""
    if not signature:
        return False, False, "署名なし"
    if not isinstance(plan_row, dict) or not plan_row:
        return False, False, "現行署名の計画評価なし"
    status = str(plan_row.get("status") or "")
    if status != "passed":
        return False, False, f"plan status={status or 'なし'}"
    try:
        from app.goal_review import evaluate_external_review_status, review_pass_policy

        parsed = plan_row.get("reviews") or []
        if not isinstance(parsed, list):
            return False, False, "reviews形式不正"
        providers = list(dict.fromkeys(
            list(mission.get("external_providers") or [])
            + [str(x.get("provider") or "") for x in parsed
               if isinstance(x, dict) and x.get("provider")]))
        providers = [p for p in providers if p]
        if not providers:
            return False, True, "検証AIの記録なし(合格の証拠なし)"
        try:
            policy = review_pass_policy(manager, mission.get("project_id") or "", len(providers))
        except Exception as exc:
            return False, False, f"合格方針不能:{type(exc).__name__}"
        try:
            judged = evaluate_external_review_status(parsed, providers, policy)
        except Exception as exc:
            return False, False, f"外部再評価不能:{type(exc).__name__}"
        if str(judged.get("status") or "") != "passed":
            return False, True, f"外部再評価={judged.get('status') or 'なし'}"
    except Exception as exc:
        return False, False, f"計画合否不能:{type(exc).__name__}"
    return True, False, ""


def _plan_approved_raw(mission: dict, plan_passed: bool):
    status = str(mission.get("status") or "")
    if status not in _APPROVED_MISSION:
        return False, False, f"mission={status or 'なし'}"
    if not plan_passed:
        return False, True, "承認状態だが現行署名の合格証拠なし"
    return True, False, ""


def _coverage_of(mission: dict, contract: dict):
    try:
        from app.plan_coverage import build

        cov = build(mission, contract)
        if not isinstance(cov, dict):
            return None, "coverage形式不正"
        return cov, ""
    except Exception as exc:
        return None, f"coverage失敗:{type(exc).__name__}"


def _external_operations(manager, project_id: str):
    try:
        from app.generic_goal_checks import collect_external_evidence

        ev = collect_external_evidence(manager, project_id)
        if not isinstance(ev, dict):
            return None, "外部証拠形式不正"
        return ev, ""
    except Exception as exc:
        return None, f"外部証拠読取失敗:{type(exc).__name__}"


def _resolve_artifact(manager, project_id: str, relative: str):
    project = manager.memory.get_project(project_id) or {}
    workspace = getattr(manager, "workspace", None)
    if workspace is None:
        raise FileNotFoundError(relative)
    return workspace.resolve_file(
        project.get("workspace_path") or "", project_id, relative, must_exist=True)[2]


def _requires_external(tasks: list, cid: str, contract: dict, manager=None) -> bool:
    # F2b fail-closed: 外部要件の判定失敗は「要外部証拠あり(True)」に倒す。
    # 判定不能を「不要(False)」にして外部証拠スキップさせない。
    try:
        from app.project_manager import ProjectOrchestrator as _PM

        for task in tasks:
            try:
                if _PM._task_requires_external_evidence(task):
                    return True
            except (ValueError, AttributeError, TypeError, KeyError):
                # 当該タスクの要否が確認できない=要求あり扱い(停止側)。
                return True
    except (ImportError, AttributeError, TypeError, ValueError):
        # 判定器自体が使えない=要求あり扱い(停止側)。
        return True
    try:
        from app.generic_goal_checks import _needs_external

        item = next((c for c in (contract.get("criteria") or [])
                     if isinstance(c, dict) and c.get("criterion_id") == cid), {})
        if _needs_external(item if isinstance(item, dict) else {}, tasks):
            return True
    except (ImportError, AttributeError, TypeError, ValueError, KeyError):
        return True
    return False


def _external_satisfied(tasks: list, evidence: dict) -> bool:
    try:
        from app.project_manager import ProjectOrchestrator as _PM
        from app.structured_planning import contract_of
    except Exception:
        raise
    ops = evidence.get("operations") if isinstance(evidence, dict) else []
    if not isinstance(ops, list):
        return False
    norm = [dict(item, kind="external_action")
            if isinstance(item, dict) and item.get("kind") == "approved_external_action"
            else item for item in ops]
    for task in tasks:
        try:
            needs = _PM._task_requires_external_evidence(task)
        except Exception:
            raise
        if not needs:
            continue
        try:
            if not _PM._task_external_evidence_satisfied(task, norm):
                return False
        except Exception:
            raise
    kinds: list[str] = []
    try:
        for task in tasks:
            for req in (contract_of(task) or {}).get("action_requirements") or []:
                if isinstance(req, dict) and req.get("kind"):
                    kinds.append(str(req["kind"]))
    except Exception:
        raise
    kinds = list(dict.fromkeys(kinds))
    if kinds:
        present: dict[str, int] = {}
        for item in ops:
            if not isinstance(item, dict):
                continue
            key = str(item.get("kind") or "")
            ident = str(item.get("id") or "")
            present[key + "\x00" + ident] = 1
        by_kind: dict[str, int] = {}
        for key in present:
            kind = key.split("\x00")[0]
            by_kind[kind] = by_kind.get(kind, 0) + 1
        for kind in kinds:
            need: int | None = None
            for task in tasks:
                try:
                    reqs = (contract_of(task) or {}).get("action_requirements") or []
                except Exception:
                    raise
                for req in reqs:
                    if isinstance(req, dict) and str(req.get("kind") or "") == kind:
                        # F2b fail-closed: minimum_executed の欠落・不正を 1 で
                        # 補完しない。欠落=不明として呼び出し側が unknown で
                        # 止まる(件数判定をずらさない)。厳密な値だけ読む。
                        raw_need = req.get("minimum_executed")
                        if raw_need is None or (isinstance(raw_need, str)
                                               and not raw_need.strip()):
                            raise ValueError(f"minimum_executed missing for kind={kind}")
                        try:
                            parsed = int(raw_need)
                        except (TypeError, ValueError) as exc:
                            raise ValueError(
                                f"minimum_executed invalid for kind={kind}") from exc
                        if parsed <= 0:
                            raise ValueError(
                                f"minimum_executed invalid for kind={kind}")
                        need = parsed if need is None else max(need, parsed)
            if need is None:
                # 要求種別の件数が一件も確定できない=判定不能として未達側に倒す。
                return False
            have = by_kind.get(kind, 0)
            if kind == "approved_external_action":
                have += by_kind.get("external_action", 0)
            if have < need:
                return False
    return True


def _task_executed_for(manager, project_id: str, mission: dict, contract: dict,
                       coverage: dict, evidence: dict, cid: str):
    """Return (raw, claimed, unknown, reason, refs)."""
    rows = {r.get("criterion_id"): r for r in (coverage.get("rows") or [])
            if isinstance(r, dict)}
    row = rows.get(cid)
    if not row:
        return False, False, False, "被覆行なし", []
    exec_keys = list(row.get("exec_task_keys") or [])
    if not exec_keys or str(row.get("status") or "") == "uncovered":
        return False, False, False, "被覆なし", []
    by_key = {t.get("task_key"): t for t in (mission.get("tasks") or []) if t.get("task_key")}
    tasks = [by_key[k] for k in exec_keys if k in by_key]
    if not tasks:
        return False, False, False, "実行タスクなし", []
    incomplete = [t for t in tasks if str(t.get("status") or "") != "completed"]
    if incomplete:
        return False, False, False, "実行タスク未完了", []
    try:
        from app.structured_planning import contract_of, verify_outputs
    except Exception as exc:
        return False, False, True, f"契約読取不能:{type(exc).__name__}", []
    refs = []
    for task in tasks:
        try:
            task_contract = contract_of(task) or {}
        except Exception as exc:
            return False, False, True, f"契約形式不正:{type(exc).__name__}", []
        outputs = [o for o in (task_contract.get("outputs") or [])
                   if isinstance(o, dict) and o.get("path")]
        if not outputs:
            return False, True, False, "成果物契約なし(完了主張あり)", []
        for output in outputs:
            rel = str(output.get("path") or "")
            try:
                target = _resolve_artifact(manager, project_id, rel)
            except FileNotFoundError:
                return False, True, False, f"成果物なし:{rel}", []
            except (OSError, ValueError) as exc:
                return False, False, True, f"成果物読取不能:{type(exc).__name__}", []
            except Exception as exc:
                return False, False, True, f"成果物確認不能:{type(exc).__name__}", []
            try:
                if not target.is_file() or target.stat().st_size <= 0:
                    return False, True, False, f"成果物空:{rel}", []
            except OSError as exc:
                return False, False, True, f"成果物stat失敗:{type(exc).__name__}", []
            refs.append({"criterion_id": cid, "task_key": str(task.get("task_key") or ""),
                         "path": rel, "hash": _file_hash(target)})
        try:
            failures = verify_outputs(task, lambda rel: _resolve_artifact(manager, project_id, rel))
        except Exception as exc:
            return False, False, True, f"形式検査不能:{type(exc).__name__}", []
        if failures:
            return False, True, False, "形式条件未達", []
    try:
        requires_external = _requires_external(tasks, cid, contract, manager)
    except Exception as exc:
        return False, False, True, f"外部要否不能:{type(exc).__name__}", []
    if requires_external:
        try:
            ok = _external_satisfied(tasks, evidence)
        except Exception as exc:
            return False, False, True, f"外部証拠判定不能:{type(exc).__name__}", []
        if not ok:
            return False, True, False, "外部証拠なし", []
    return True, False, "", "", refs


def _final_verdicts(manager, project_id: str, mission: dict):
    """Return (verdicts|None, error, path). None + error => unknown."""
    try:
        from app.structured_planning import (
            contract_of, is_final_verification_task, parse_criterion_verdicts)
    except Exception as exc:
        return None, f"検証関数不能:{type(exc).__name__}", ""
    try:
        verify_tasks = [t for t in (mission.get("tasks") or [])
                        if is_final_verification_task(t)]
    except Exception as exc:
        return None, f"最終検証探索不能:{type(exc).__name__}", ""
    if not verify_tasks:
        return {}, "", ""
    task = verify_tasks[0]
    try:
        outputs = (contract_of(task) or {}).get("outputs") or []
    except Exception as exc:
        return None, f"最終検証契約不能:{type(exc).__name__}", ""
    path = str((outputs[0] or {}).get("path") or "result/final_verification.md") \
        if outputs else "result/final_verification.md"
    if str(task.get("status") or "") != "completed":
        return {}, "", path
    try:
        target = _resolve_artifact(manager, project_id, path)
        content = target.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}, "", path
    except (OSError, ValueError) as exc:
        return None, f"最終検証読取不能:{type(exc).__name__}", path
    except Exception as exc:
        return None, f"最終検証確認不能:{type(exc).__name__}", path
    try:
        return parse_criterion_verdicts(content), "", path
    except Exception as exc:
        return None, f"最終検証形式不正:{type(exc).__name__}", path


def _gate_rows(manager, project_id: str):
    mem = Path(manager.memory.path)
    if not (mem.parent / "goal_reviews.sqlite3").is_file():
        return None, "goal_reviews DBなし"
    if not (mem.parent / "goal_completion.sqlite3").is_file():
        return None, "goal_completion DBなし"
    try:
        from app.completion_gate import evaluate

        gate = evaluate(manager, project_id, persist=False)
    except Exception as exc:
        return None, f"完了ゲート失敗:{type(exc).__name__}"
    if not isinstance(gate, dict):
        return None, "完了ゲート形式不正"
    return gate, ""


def _classify_for(inputs: dict):
    try:
        from app.stop_classifier import classify_stop

        out = classify_stop(dict(inputs))
        if not isinstance(out, dict) or not out.get("stop_class"):
            return "unknown", "分類不能"
        return str(out.get("stop_class")), str(out.get("stop_code") or "")
    except Exception as exc:
        return "unknown", f"分類不能:{type(exc).__name__}"


def _stage_value(count: int, total: int, claimed: int, unknown: int, refs: list) -> dict:
    return {"count": int(count), "total": int(total),
            "claimed_without_evidence": int(claimed), "unknown": int(unknown),
            "evidence_refs": list(refs)}


def _empty_funnel(pid: str, signature: str, contract_hash: str, reason: str,
                  unknown_total: int) -> dict:
    empty = _stage_value(0, 0, 0, unknown_total,
                         [{"id": "note", "reason": reason[:200]}])
    return {"project_id": pid, "read_only": True, "external_sends": 0,
            "plan_signature": _truncate(signature, 128),
            "contract_hash": _truncate(contract_hash, 64),
            "plan_passed": dict(empty), "plan_approved": dict(empty),
            "task_executed": dict(empty), "evidence_verified": dict(empty),
            "criterion_achieved": dict(empty),
            "per_criterion": [], "stage_stop_summary": {"unknown": 1},
            "unknown_reason": reason[:300]}


def project_funnel(manager, project_id: str) -> dict:
    """Read-only funnel for one project. Never writes DB/files or calls externally."""
    pid = str(project_id or "")
    mission, mission_err = _safe_mission(manager, pid)
    if mission is None:
        return _empty_funnel(pid, "", "", mission_err, 1)
    try:
        mission["project_id"] = pid
    except (TypeError, AttributeError):
        # F2b fail-closed: mission への project_id 付与の失敗は想定内の型のみ
        # 許容し、以降の判定は pid 変数で継続(成功偽装なし)。
        pass
    signature, sig_err = _safe_signature(manager, pid)
    contract, contract_err = _safe_contract(manager, pid, mission)
    criteria: list[str] = []
    if isinstance(contract, dict):
        for item in (contract.get("criteria") or []):
            if isinstance(item, dict) and item.get("criterion_id"):
                criteria.append(str(item["criterion_id"]))
    criteria = sorted(set(criteria))
    contract_hash = str((contract or {}).get("content_hash") or "")
    total = len(criteria)
    if not criteria:
        return _empty_funnel(pid, signature, contract_hash,
                             contract_err or "達成条件なし", 1)

    if signature:
        plan_row, plan_row_err = _read_review_row(
            manager.memory.path, pid, "plan", signature)
    else:
        plan_row, plan_row_err = None, (sig_err or "署名なし")
    plan_unknown = False
    plan_unknown_reason = ""
    if plan_row_err:
        # Empty string means the row is missing at the current signature
        # ("not reached", not unknown). Only a genuine read failure is unknown.
        if plan_row_err != "":
            plan_unknown = True
            plan_unknown_reason = plan_row_err
    if sig_err and not signature:
        plan_unknown = True
        plan_unknown_reason = sig_err
    if plan_unknown:
        r1 = c1 = False
        u1 = True
        r1_reason = plan_unknown_reason
        r2 = c2 = False
        u2 = True
        r2_reason = plan_unknown_reason
    else:
        try:
            r1, c1, r1_reason = _plan_passed_raw(manager, mission, signature, plan_row)
            u1 = False
        except Exception as exc:
            r1, c1, r1_reason = False, False, f"判定不能:{type(exc).__name__}"
            u1 = True
            plan_unknown = True
            plan_unknown_reason = r1_reason
        try:
            r2, c2, r2_reason = _plan_approved_raw(mission, bool(r1) and not u1)
            u2 = bool(plan_unknown or u1)
        except Exception as exc:
            r2, c2, r2_reason = False, False, f"判定不能:{type(exc).__name__}"
            u2 = True

    coverage, coverage_err = _coverage_of(mission, contract or {})
    coverage_unknown = coverage is None
    evidence, evidence_err = _external_operations(manager, pid)
    evidence_unknown = evidence is None
    verdicts, verdict_err, fv_path = _final_verdicts(manager, pid, mission)
    verdicts_unknown = verdicts is None
    gate, gate_err = _gate_rows(manager, pid)
    gate_rows = {r.get("criterion_id"): r for r in ((gate or {}).get("criteria") or [])
                 if isinstance(r, dict)} if isinstance(gate, dict) else {}
    gate_unknown = gate is None
    mission_completed = str(mission.get("status") or "") == "completed"

    counts = [0, 0, 0, 0, 0]
    claimed = [0, 0, 0, 0, 0]
    unknowns = [0, 0, 0, 0, 0]
    refs_per_stage: list[list] = [[], [], [], [], []]
    if not plan_unknown:
        refs_per_stage[0].append({"id": "plan_signature", "hash": _truncate(signature, 64)})
        refs_per_stage[0].append({"id": "contract_hash", "hash": _truncate(contract_hash, 64)})
        refs_per_stage[1].append({"id": "plan_signature", "hash": _truncate(signature, 64)})
    else:
        refs_per_stage[0].append({"id": "plan_signature",
                                  "reason": (plan_unknown_reason or "不明")[:200]})
        refs_per_stage[1].append({"id": "plan_signature",
                                  "reason": (plan_unknown_reason or "不明")[:200]})

    per: list[dict] = []
    stop_summary: dict[str, int] = {}

    for cid in criteria:
        if coverage_unknown:
            r3, c3, u3, reason3, task_refs = False, False, True, coverage_err, []
        elif evidence_unknown:
            r3, c3, u3, reason3, task_refs = False, False, True, evidence_err, []
        else:
            try:
                r3, c3, u3, reason3, task_refs = _task_executed_for(
                    manager, pid, mission, contract or {}, coverage or {},
                    evidence or {}, cid)
            except Exception as exc:
                r3, c3, u3, reason3, task_refs = (
                    False, False, True, f"実行判定不能:{type(exc).__name__}", [])
        if not isinstance(task_refs, list):
            task_refs = []
        external_ok = True
        if coverage_unknown or evidence_unknown:
            external_ok = False
        elif r3:
            try:
                by_key = {t.get("task_key"): t for t in (mission.get("tasks") or [])
                          if t.get("task_key")}
                cov_row = next((x for x in (coverage.get("rows") or [])
                                if isinstance(x, dict) and x.get("criterion_id") == cid), {})
                exec_tasks = [by_key[k] for k in (cov_row.get("exec_task_keys") or [])
                              if k in by_key]
                external_ok = ((not _requires_external(exec_tasks, cid, contract or {}))
                               or _external_satisfied(exec_tasks, evidence or {}))
            except Exception:
                # Unreadable here means evidence stage unknown, not pass.
                external_ok = False
                if r3:
                    r3, c3, u3 = False, False, True
                    reason3 = "外部証拠再確認不能"
        else:
            external_ok = False
        if verdicts_unknown:
            r4, c4, u4, reason4 = False, False, True, verdict_err
        elif coverage_unknown or evidence_unknown:
            r4, c4, u4, reason4 = False, False, True, coverage_err or evidence_err
        elif u3 and not r3:
            r4, c4, u4, reason4 = False, False, True, "実行段不明のため判定不能"
        elif not r3:
            if c3:
                r4, c4, u4, reason4 = False, True, False, "実行証拠なし"
            else:
                r4, c4, u4, reason4 = False, False, False, "実行証拠なし"
        elif not external_ok:
            r4, c4, u4, reason4 = False, True, False, "外部証拠なし"
        else:
            # evidence_verified は現行判定での検証済み: 完了ゲートの行が
            # PASS/FAIL のいずれかで確定していること。最終検証の明示評価が
            # 無い場合は証拠未確認 (claimed) として扱う。
            grow_ev = gate_rows.get(cid) if isinstance(gate, dict) else None
            if gate_unknown or not isinstance(grow_ev, dict):
                r4, c4, u4, reason4 = False, False, True, gate_err or "完了ゲート行なし"
            elif str(grow_ev.get("status") or "") in {"PASS", "FAIL"}:
                if cid in (verdicts or {}):
                    r4, c4, u4, reason4 = True, False, "", ""
                else:
                    r4, c4, u4, reason4 = False, True, False, "最終検証の明示評価なし"
            elif str(grow_ev.get("status") or "") in {"BLOCKED", "UNTESTABLE"}:
                r4, c4, u4, reason4 = False, True, False, (
                    f"現行判定で未確定:{grow_ev.get('reason_code') or grow_ev.get('status')}")
            else:
                r4, c4, u4, reason4 = False, False, False, (
                    f"現行判定で未確定:{grow_ev.get('reason_code') or grow_ev.get('status')}")
        if gate_unknown:
            r5, c5, u5, reason5 = False, False, True, gate_err
        else:
            grow = gate_rows.get(cid)
            if not isinstance(grow, dict):
                r5, c5, u5, reason5 = False, False, False, "完了ゲート行なし"
            elif str(grow.get("status") or "") == "PASS" and bool((gate or {}).get("achieved")):
                # 達成は完了ゲート全体の achieved が真のときだけ数える。
                # 成果物の存在・行単位PASSだけでは達成にしない。
                r5, c5, u5, reason5 = True, False, "", ""
            elif mission_completed and str(grow.get("status") or "") == "PASS":
                r5, c5, u5, reason5 = (
                    False, True, False,
                    f"未達成:{grow.get('reason_code') or '全体未達成'}")
            elif mission_completed:
                r5, c5, u5, reason5 = (
                    False, True, False,
                    f"未達成:{grow.get('reason_code') or grow.get('status')}")
            else:
                r5, c5, u5, reason5 = (
                    False, False, False,
                    f"未達成:{grow.get('reason_code') or grow.get('status')}")

        raw = [bool(r1) and not u1, bool(r2) and not u2,
               bool(r3), bool(r4), bool(r5)]
        unk = [bool(u1), bool(u2), bool(u3), bool(u4), bool(u5)]
        clm = [bool(c1) and not u1, bool(c2) and not u2,
               bool(c3), bool(c4), bool(c5)]
        reasons = [r1_reason if not u1 else plan_unknown_reason,
                   r2_reason if not u2 else plan_unknown_reason,
                   reason3, reason4, reason5]

        counted = [False, False, False, False, False]
        for i in range(5):
            if unk[i] or not raw[i]:
                continue
            if i > 0 and not counted[i - 1]:
                clm[i] = True
                reasons[i] = ((reasons[i] + ";前段未達のため計上せず") if reasons[i]
                              else "前段未達のため計上せず")[:300]
                continue
            counted[i] = True

        for i in range(5):
            if unk[i]:
                unknowns[i] += 1
                refs_per_stage[i].append({"id": cid, "reason": str(reasons[i] or "確認不能")[:200]})
            elif counted[i]:
                counts[i] += 1
                if i == 2:
                    refs_per_stage[i].extend(task_refs)
                elif i in (0, 1):
                    pass
                elif i == 3:
                    refs_per_stage[i].append({"id": cid, "hash": _short_hash([cid, fv_path])})
                else:
                    grow = gate_rows.get(cid) if isinstance(gate, dict) else None
                    refs_per_stage[i].append({"id": cid, "hash": _short_hash(
                        [cid, (grow or {}).get("reason_code"),
                         (grow or {}).get("evidence_path")])})
            elif clm[i]:
                claimed[i] += 1
                refs_per_stage[i].append({"id": cid, "reason": str(reasons[i] or "証拠未確認")[:200]})

        last_idx = -1
        for i in range(5):
            if counted[i]:
                last_idx = i
        last_stage = STAGES[last_idx] if last_idx >= 0 else ""
        if last_idx >= 4:
            stopping_stage = ""
            stop_class = ""
            stop_reason = ""
        else:
            stopping_stage = STAGES[last_idx + 1]
            gate_row = gate_rows.get(cid) if isinstance(gate, dict) else None
            inputs: dict = {
                "plan_signature": signature,
                "gate": gate_row if isinstance(gate_row, dict) else {},
                "gate_reason": str((gate_row or {}).get("reason_code") or "")
                if isinstance(gate_row, dict) else "",
            }
            if coverage_unknown:
                inputs["coverage_incomplete"] = True
            else:
                cov_row = next((x for x in (coverage.get("rows") or [])
                                if isinstance(x, dict) and x.get("criterion_id") == cid), None)
                if isinstance(cov_row, dict) and cov_row.get("status") == "uncovered":
                    inputs["coverage_incomplete"] = True
            stop_class, stop_code = _classify_for(inputs)
            stop_unknown_idx = last_idx + 1
            if unk[stop_unknown_idx]:
                stop_class = "unknown"
                stop_reason = str(reasons[stop_unknown_idx] or "確認できず")[:300]
            else:
                stop_reason = str(reasons[stop_unknown_idx] or stop_code or "未達")[:300]
            stop_summary[stop_class] = stop_summary.get(stop_class, 0) + 1
        per.append({"criterion_id": cid, "last_stage": last_stage,
                    "stopping_stage": stopping_stage, "stop_class": stop_class,
                    "stop_reason": stop_reason})

    if u1:
        unknowns[0] = total
        counts[0] = 0
        claimed[0] = 0
    if u2:
        unknowns[1] = total
        counts[1] = 0
        claimed[1] = 0
    for i in range(1, 5):
        if counts[i] > counts[i - 1] and unknowns[i - 1] == 0 and unknowns[i] == 0:
            excess = counts[i] - counts[i - 1]
            counts[i] = counts[i - 1]
            claimed[i] += excess

    stages_out = {}
    for i, name in enumerate(STAGES):
        refs = sorted(refs_per_stage[i],
                      key=lambda x: json.dumps(x, ensure_ascii=False, sort_keys=True))[:50]
        stages_out[name] = _stage_value(counts[i], total, claimed[i], unknowns[i], refs)
    return {"project_id": pid, "read_only": True, "external_sends": 0,
            "plan_signature": _truncate(signature, 128),
            "contract_hash": _truncate(contract_hash, 64),
            **stages_out, "per_criterion": per,
            "stage_stop_summary": dict(sorted(stop_summary.items()))}


def portfolio_funnel(manager) -> dict:
    """Aggregate all non-deleted projects. No project is special-cased."""
    try:
        projects = manager.memory.list_projects()
    except Exception as exc:
        return {"read_only": True, "external_sends": 0,
                "error": f"一覧失敗:{type(exc).__name__}",
                "rows": [], "total": _stage_value(0, 0, 0, 1, [])}
    rows = []
    sums = {name: {"count": 0, "total": 0, "claimed_without_evidence": 0,
                   "unknown": 0} for name in STAGES}
    for project in projects:
        if not isinstance(project, dict) or not project.get("id"):
            continue
        pid = str(project.get("id"))
        from app.project_delete import is_deleted_or_unknown as _l3_blocked

        try:
            if _l3_blocked(manager.memory.path, pid):
                # 削除状態が読めなければ、当該PJの情報を集計へ出さない。
                # (is_deleted_or_unknown 自体が unknown→True の fail-closed)
                continue
        except (OSError, ValueError, AttributeError, TypeError, KeyError):
            # F2b fail-closed: 削除確認不能のPJは集計へ出さない(除外=停止側)。
            continue
        try:
            funnel = project_funnel(manager, pid)
        except Exception as exc:
            funnel = {"project_id": pid, "error": type(exc).__name__}
        row = {"project_id": pid}
        for name in STAGES:
            stage = funnel.get(name) if isinstance(funnel, dict) else None
            if not isinstance(stage, dict):
                stage = _stage_value(0, 0, 0, 1, [])
            row[name] = stage
            for key in ("count", "total", "claimed_without_evidence", "unknown"):
                try:
                    sums[name][key] += int(stage.get(key) or 0)
                except (TypeError, ValueError):
                    continue
        rows.append(row)
    rows.sort(key=lambda x: x.get("project_id") or "")
    total_out = {name: _stage_value(sums[name]["count"], sums[name]["total"],
                                   sums[name]["claimed_without_evidence"],
                                   sums[name]["unknown"], [])
                 for name in STAGES}
    return {"read_only": True, "external_sends": 0, "rows": rows, "total": total_out}
