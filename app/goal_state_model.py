"""B-1: 進捗モデル(読み取り専用)とPJ概要の「自走の現在地」材料。

一般機構。特定PJ・件数・IDに依存しない。読み取り専用で、承認・実行・
外部送信・RAG登録・計画適用・自走の実行を一切しない。外部呼出0・
DB/ファイルへの書込0。判定不能を0件・達成と表示しない(fail-closed)。
"""
from __future__ import annotations


STATES = (
    "achieved",
    "capability_gap",
    "evidence_gap",
    "human_decision",
    "unknown",
)

STATE_JA = {
    "achieved": "達成",
    "capability_gap": "機能不足",
    "evidence_gap": "証拠不足",
    "human_decision": "人の判断待ち",
    "unknown": "確認できず",
}

NEXT_KINDS = (
    "autonomous",
    "human_confirm",
    "capability_dev",
    "evidence_confirm",
    "blocked_unknown",
)

SCHEMA = "sebas.goal-state/v1"
QUOTE_LIMIT = 80

_APPROVED_MISSION = ("ready", "paused", "running", "completed")
_OPEN_PROPOSAL = (
    "draft",
    "needs_evidence",
    "reviewed",
    "export_ready",
    "submitted",
    "delivered",
)


def _safe_short_80(text: object) -> str:
    try:
        from app.capability_gap import _safe_short as _cap_short

        return _safe_short(_cap_short(text, QUOTE_LIMIT), QUOTE_LIMIT)
    except Exception:
        blob = str(text if text is not None else "")
        return " ".join(blob.split())[:QUOTE_LIMIT]


def _safe_short(text: object, limit: int) -> str:
    blob = str(text if text is not None else "")
    return " ".join(blob.split())[: max(1, int(limit))]


def _truncate_sig(value: object, length: int = 128) -> str:
    return str(value if value is not None else "")[: max(1, int(length))]


def _type_name(exc: BaseException) -> str:
    try:
        return str(type(exc).__name__ or "Error")
    except Exception:
        return "Error"


def _read_signature(manager, project_id: str) -> tuple[str, str]:
    pid = str(project_id or "")
    try:
        from app.goal_review import plan_snapshot

        _snap, sig = plan_snapshot(manager, pid)
        text = str(sig if sig is not None else "")
        if text == "":
            return "", "empty_signature"
        return text[:128], ""
    except Exception as exc:
        return "", _type_name(exc)


def _read_contract(manager, project_id: str) -> tuple[dict | None, str]:
    pid = str(project_id or "")
    try:
        from app.goal_contract import preview as preview_contract

        body = preview_contract(manager, pid)
    except Exception as exc:
        return None, _type_name(exc)
    if not isinstance(body, dict):
        return None, "TypeError"
    return body, ""


def _read_gate(manager, project_id: str) -> tuple[dict | None, str]:
    pid = str(project_id or "")
    try:
        from app.completion_gate import evaluate as evaluate_gate

        gate = evaluate_gate(manager, pid, persist=False)
    except Exception as exc:
        return None, _type_name(exc)
    if not isinstance(gate, dict):
        return None, "TypeError"
    return gate, ""


def _read_funnel(manager, project_id: str) -> tuple[dict | None, str]:
    pid = str(project_id or "")
    try:
        from app.execution_funnel import project_funnel as funnel_one

        funnel = funnel_one(manager, pid)
    except Exception as exc:
        return None, _type_name(exc)
    if not isinstance(funnel, dict):
        return None, "TypeError"
    return funnel, ""


def _read_preflight(manager, project_id: str) -> tuple[dict | None, str]:
    pid = str(project_id or "")
    try:
        from app import capability_gap as gap

        preflight = gap.capability_preflight(manager, pid)
    except Exception as exc:
        return None, _type_name(exc)
    if not isinstance(preflight, dict):
        return None, "TypeError"
    return preflight, ""


def _read_proposals(manager, project_id: str,
                    signature: str) -> tuple[list | None, str]:
    pid = str(project_id or "")
    sig = str(signature or "")
    if sig == "":
        return None, "empty_signature"
    try:
        from app import capability_gap as gap

        rows = gap.list_proposals(manager, pid, plan_signature=sig)
    except Exception as exc:
        return None, _type_name(exc)
    if not isinstance(rows, list):
        return None, "TypeError"
    return rows, ""


def _read_stop(manager, project_id: str) -> tuple[dict | None, str, dict | None]:
    pid = str(project_id or "")
    try:
        from app.workflow_readiness import build_readiness, build_stop_class

        built = build_readiness(manager, pid)
    except Exception as exc:
        return None, _type_name(exc), None
    if not isinstance(built, dict):
        return None, "TypeError", None
    try:
        from app.workflow_readiness import build_stop_class as _build_stop

        record = _build_stop(manager, pid, built)
    except Exception as exc:
        return None, _type_name(exc), built
    if not isinstance(record, dict):
        return None, "TypeError", built
    if not record.get("stop_class"):
        return None, "ValueError", built
    return record, "", built


def _read_mission_status(manager, project_id: str) -> tuple[str | None, str]:
    pid = str(project_id or "")
    try:
        mission = manager.memory.get_mission(pid)
    except Exception as exc:
        return None, _type_name(exc)
    if not isinstance(mission, dict):
        return None, "TypeError"
    raw = mission.get("status")
    if raw is None:
        return None, "KeyError"
    return str(raw), ""


def _read_autopilot(manager, project_id: str) -> tuple[dict, list | None, str]:
    pid = str(project_id or "")
    try:
        from app import autopilot as _auto

        config = _auto.get_config(manager, pid)
    except Exception as exc:
        return {}, None, _type_name(exc)
    if not isinstance(config, dict):
        return {}, None, "TypeError"
    try:
        from app import autopilot as _auto2

        ticks = _auto2.list_ticks(manager, pid)
    except Exception as exc:
        return config, None, _type_name(exc)
    if not isinstance(ticks, list):
        return config, None, "TypeError"
    return config, ticks, ""


def _criterion_ids(contract: dict | None) -> tuple[list[str], str]:
    if not isinstance(contract, dict):
        return [], "TypeError"
    raw = contract.get("criteria")
    if raw is None:
        return [], "KeyError"
    if not isinstance(raw, list):
        return [], "TypeError"
    ids: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            return [], "TypeError"
        cid = item.get("criterion_id")
        if cid is None:
            return [], "KeyError"
        text = str(cid)
        if text == "":
            return [], "ValueError"
        if text not in ids:
            ids.append(text)
    ids.sort()
    return ids, ""


def _statement_of(contract: dict | None, cid: str) -> tuple[str, str]:
    if not isinstance(contract, dict):
        return "", "TypeError"
    raw = contract.get("criteria")
    if not isinstance(raw, list):
        return "", "TypeError"
    for item in raw:
        if isinstance(item, dict) and str(item.get("criterion_id") or "") == cid:
            stmt = item.get("statement")
            if stmt is None:
                return "", "KeyError"
            return str(stmt), ""
    return "", "KeyError"


def _gate_row_of(gate: dict | None, cid: str) -> tuple[dict | None, str]:
    if not isinstance(gate, dict):
        return None, "TypeError"
    raw = gate.get("criteria")
    if not isinstance(raw, list):
        return None, "TypeError"
    for row in raw:
        if isinstance(row, dict) and str(row.get("criterion_id") or "") == cid:
            return row, ""
    return None, "KeyError"


def _funnel_per_of(funnel: dict | None, cid: str) -> tuple[dict | None, str]:
    if not isinstance(funnel, dict):
        return None, "TypeError"
    raw = funnel.get("per_criterion")
    if not isinstance(raw, list):
        return None, "TypeError"
    for row in raw:
        if isinstance(row, dict) and str(row.get("criterion_id") or "") == cid:
            return row, ""
    return None, "KeyError"


def _preflight_gap_of(preflight: dict | None, cid: str) -> tuple[str | None, str]:
    if not isinstance(preflight, dict):
        return None, "TypeError"
    rows = preflight.get("criteria")
    if not isinstance(rows, list):
        return None, "TypeError"
    for row in rows:
        if isinstance(row, dict) and str(row.get("criterion_id") or "") == cid:
            code = row.get("gap_code")
            if code is None:
                return None, ""
            return str(code), ""
    # 行自体が無い場合は対応付け不能。
    return None, "KeyError"


def _has_missing_capability(preflight: dict | None, cid: str) -> tuple[bool, str]:
    if not isinstance(preflight, dict):
        return False, "TypeError"
    raw = preflight.get("gaps")
    if not isinstance(raw, list):
        return False, "TypeError"
    for gap in raw:
        if not isinstance(gap, dict):
            return False, "TypeError"
        if str(gap.get("criterion_id") or "") != cid:
            continue
        if str(gap.get("gap_code") or "") == "missing_capability":
            return True, ""
    # missing_capability の一覧とも突き合わせる。形式不正は不明に倒す。
    raw_missing = preflight.get("missing_capability")
    if raw_missing is None:
        return False, "KeyError"
    if not isinstance(raw_missing, list):
        return False, "TypeError"
    for gap in raw_missing:
        if not isinstance(gap, dict):
            return False, "TypeError"
        if str(gap.get("criterion_id") or "") == cid:
            return True, ""
    return False, ""


def _open_proposal_for(proposals: list | None, cid: str) -> tuple[bool, str]:
    if proposals is None:
        return False, "TypeError"
    if not isinstance(proposals, list):
        return False, "TypeError"
    for row in proposals:
        if not isinstance(row, dict):
            return False, "TypeError"
        if str(row.get("gap_code") or "") != "missing_capability":
            continue
        hit = False
        if str(row.get("criterion_id") or "") == cid:
            hit = True
        ids_raw = row.get("criterion_ids")
        if isinstance(ids_raw, list):
            for entry in ids_raw:
                if str(entry) == cid:
                    hit = True
        if not hit:
            continue
        status = row.get("status")
        if status is None:
            return False, "KeyError"
        if str(status) in _OPEN_PROPOSAL:
            return True, ""
    return False, ""


def _classify_criterion(*, cid: str, gate: dict | None, gate_err: str,
                        funnel: dict | None, funnel_err: str,
                        preflight: dict | None, preflight_err: str,
                        proposals: list | None, proposals_err: str,
                        mission_status: str | None, mission_err: str,
                        gate_overall_achieved: bool) -> dict:
    refs: dict = {}
    blockers: list[str] = []
    # 読取不能は該当条件を unknown にし、達成・0件に見せない。
    if gate is None:
        return {"criterion_id": cid, "state": "unknown",
                "reason": "完了ゲートを確認できません:" + str(gate_err or "Error"),
                "evidence_refs": {}, "blockers": []}
    if funnel is None:
        return {"criterion_id": cid, "state": "unknown",
                "reason": "漏斗を確認できません:" + str(funnel_err or "Error"),
                "evidence_refs": {}, "blockers": []}
    if preflight is None:
        return {"criterion_id": cid, "state": "unknown",
                "reason": "事前照合を確認できません:" + str(preflight_err or "Error"),
                "evidence_refs": {}, "blockers": []}
    if proposals is None:
        return {"criterion_id": cid, "state": "unknown",
                "reason": "提案を確認できません:" + str(proposals_err or "Error"),
                "evidence_refs": {}, "blockers": []}
    if mission_status is None:
        return {"criterion_id": cid, "state": "unknown",
                "reason": "計画状態を確認できません:" + str(mission_err or "Error"),
                "evidence_refs": {}, "blockers": []}
    gate_row, gate_row_err = _gate_row_of(gate, cid)
    if gate_row is None:
        return {"criterion_id": cid, "state": "unknown",
                "reason": "完了ゲートの行を確認できません:" + str(gate_row_err or "Error"),
                "evidence_refs": {}, "blockers": []}
    funnel_per, funnel_per_err = _funnel_per_of(funnel, cid)
    if funnel_per is None:
        return {"criterion_id": cid, "state": "unknown",
                "reason": "漏斗の条件行を確認できません:" + str(funnel_per_err or "Error"),
                "evidence_refs": {}, "blockers": []}
    pre_gap, pre_gap_err = _preflight_gap_of(preflight, cid)
    if pre_gap_err != "":
        return {"criterion_id": cid, "state": "unknown",
                "reason": "事前照合の条件行を確認できません:" + str(pre_gap_err or "Error"),
                "evidence_refs": {}, "blockers": []}
    # 形式の検査。迷ったら unknown。
    gate_status = gate_row.get("status")
    if gate_status is None:
        return {"criterion_id": cid, "state": "unknown",
                "reason": "完了ゲートの状態を確認できません:KeyError",
                "evidence_refs": {}, "blockers": []}
    gate_status_text = str(gate_status)
    if gate_status_text not in ("PASS", "FAIL", "BLOCKED", "UNTESTABLE"):
        return {"criterion_id": cid, "state": "unknown",
                "reason": "完了ゲートの状態を確認できません:ValueError",
                "evidence_refs": {}, "blockers": []}
    per_last = funnel_per.get("last_stage")
    per_stop = funnel_per.get("stop_class")
    if per_last is None or per_stop is None:
        return {"criterion_id": cid, "state": "unknown",
                "reason": "漏斗の停止を確認できません:KeyError",
                "evidence_refs": {}, "blockers": []}
    per_last_text = str(per_last)
    per_stop_text = str(per_stop)
    if per_stop_text == "unknown":
        detail = str(funnel_per.get("stop_reason") or "確認できず")
        return {"criterion_id": cid, "state": "unknown",
                "reason": "漏斗で確認できず:" + detail[:120],
                "evidence_refs": {"last_stage": per_last_text},
                "blockers": []}
    # 証拠参照はID・ハッシュ・行の所在だけ。本文・秘密は入れない。
    gate_path = gate_row.get("evidence_path")
    if gate_path is None:
        gate_path_text = ""
    else:
        gate_path_text = str(gate_path)[:128]
    refs = {"gate_status": gate_status_text,
            "last_stage": per_last_text,
            "stop_class": per_stop_text}
    if gate_path_text != "":
        refs["evidence_path"] = gate_path_text
    gate_reason = gate_row.get("reason_code")
    gate_reason_text = "" if gate_reason is None else str(gate_reason)[:120]
    if gate_reason_text != "":
        refs["gate_reason"] = gate_reason_text
    # achieved は完了ゲートの行PASSかつ全体達成かつ漏斗の達成段計上の三点が揃ったときだけ。
    # 候補・TRIZ・RAG・パック適合・テスト通過・成果物の存在だけでは付けない。
    if (gate_status_text == "PASS" and bool(gate_overall_achieved)
            and per_last_text == "criterion_achieved"):
        return {"criterion_id": cid, "state": "achieved", "reason": "",
                "evidence_refs": refs, "blockers": []}
    # 矛盾は unknown。行PASSなのに漏斗の達成段にいない場合は証拠不足ではなく
    # 確認不能に倒す場合がある。ここでは PASS + 全体未達成は未達として扱い、
    # 行PASS + 漏斗達成の不一致のうち、漏斗が unknown でないものは証拠不足に回す。
    # 真の矛盾 (漏斗が達成なのに行がPASSでない) は unknown。
    if per_last_text == "criterion_achieved" and gate_status_text != "PASS":
        return {"criterion_id": cid, "state": "unknown",
                "reason": "完了ゲートと漏斗が矛盾するため確認できません:ValueError",
                "evidence_refs": refs, "blockers": []}
    # 機能不足は事前照合の missing_capability が残るときだけ。
    has_cap, has_cap_err = _has_missing_capability(preflight, cid)
    if has_cap_err != "":
        return {"criterion_id": cid, "state": "unknown",
                "reason": "不足の有無を確認できません:" + str(has_cap_err or "Error"),
                "evidence_refs": refs, "blockers": []}
    if bool(has_cap):
        reason_code = "missing_capability"
        blockers.append("必要な操作を実行する機能が無いため停止しています")
        refs["gap_code"] = "missing_capability"
        return {"criterion_id": cid, "state": "capability_gap",
                "reason": reason_code, "evidence_refs": refs,
                "blockers": blockers}
    # 人の判断待ち: 計画承認・指摘の採否・業務事実の確認・提案の確認など。
    human_reasons: list[str] = []
    if str(mission_status) not in _APPROVED_MISSION:
        human_reasons.append("計画承認待ちのため人の確認が必要です")
    if gate_status_text == "BLOCKED":
        human_reasons.append("完了ゲートが入力待ちのため人の確認が必要です")
    if gate_reason_text in ("HUMAN_ACCEPTANCE_MISSING",
                            "HUMAN_ACCEPTANCE_HASH_DRIFT"):
        human_reasons.append("人による結果確認待ちのため人の確認が必要です")
    if pre_gap in ("missing_input", "missing_binding", "policy_block"):
        if pre_gap == "missing_input":
            human_reasons.append("業務事実の確認が必要なため人の確認が必要です")
        elif pre_gap == "missing_binding":
            human_reasons.append("指摘の採否・対応付けの確定に人の確認が必要です")
        else:
            human_reasons.append("安全境界のため人の確認が必要です(迂回しません)")
    if per_stop_text == "missing_decision":
        human_reasons.append("人の判断待ちのため人の確認が必要です")
    has_open, has_open_err = _open_proposal_for(proposals, cid)
    if has_open_err != "":
        return {"criterion_id": cid, "state": "unknown",
                "reason": "提案の対応を確認できません:" + str(has_open_err or "Error"),
                "evidence_refs": refs, "blockers": []}
    if bool(has_open):
        human_reasons.append("不足機能の提案の確認が必要なため人の確認が必要です")
    if human_reasons:
        refs["human"] = human_reasons[0][:200]
        return {"criterion_id": cid, "state": "human_decision",
                "reason": human_reasons[0][:200],
                "evidence_refs": refs, "blockers": human_reasons[:3]}
    # 証拠不足: 実行済みだが証拠未確認・外部作用で外部証拠が無いなど。
    # 完了ゲートの generic は all_pass + human_ok で達成になるため、
    # 行PASS + 全体未達成は人待ちとして上で拾っているはず。残りは証拠不足。
    if gate_status_text in ("FAIL", "UNTESTABLE", "BLOCKED"):
        detail = gate_reason_text or gate_status_text
        blockers.append("証拠が不足しているため確定できません:" + detail[:120])
        refs["gap_code"] = "missing_evidence"
        return {"criterion_id": cid, "state": "evidence_gap",
                "reason": detail[:200], "evidence_refs": refs,
                "blockers": blockers}
    return {"criterion_id": cid, "state": "unknown",
            "reason": "判定材料が不足するため確認できません:ValueError",
            "evidence_refs": refs, "blockers": []}


def _summarize_states(items: list[dict], total: int) -> dict:
    counts = {"achieved": 0, "capability_gap": 0, "evidence_gap": 0,
              "human_decision": 0, "unknown": 0}
    for entry in items:
        if not isinstance(entry, dict):
            continue
        state = str(entry.get("state") or "")
        if state in counts:
            counts[state] = int(counts[state]) + 1
    parts = [int(counts[key]) for key in STATES]
    summed = 0
    for value in parts:
        summed = int(summed) + int(value)
    consistent = bool(int(summed) == int(total))
    remaining: list[str] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("state") or "") != "achieved":
            cid = str(entry.get("criterion_id") or "")
            if cid != "" and cid not in remaining:
                remaining.append(cid)
    remaining.sort()
    goal_achieved = bool(int(total) > 0 and int(counts["achieved"]) == int(total)
                         and consistent)
    return {"counts": counts, "total": int(total), "summed": int(summed),
            "consistent": bool(consistent), "goal_achieved": bool(goal_achieved),
            "goal_remaining": remaining}


def _proposals_view(rows: list | None, rows_err: str) -> dict:
    if rows is None:
        return {"readable": False, "reason": str(rows_err or "Error"),
                "by_status": {}, "ids": [], "total": None}
    if not isinstance(rows, list):
        return {"readable": False, "reason": "TypeError",
                "by_status": {}, "ids": [], "total": None}
    by_status: dict[str, int] = {}
    ids: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            return {"readable": False, "reason": "TypeError",
                    "by_status": {}, "ids": [], "total": None}
        if str(row.get("gap_code") or "") != "missing_capability":
            continue
        status = row.get("status")
        pid_val = row.get("proposal_id")
        if status is None or pid_val is None:
            return {"readable": False, "reason": "KeyError",
                    "by_status": {}, "ids": [], "total": None}
        status_text = str(status)
        pid_text = str(pid_val)
        if status_text == "" or pid_text == "":
            return {"readable": False, "reason": "ValueError",
                    "by_status": {}, "ids": [], "total": None}
        by_status[status_text] = int(by_status.get(status_text, 0)) + 1
        if pid_text not in ids:
            ids.append(pid_text)
    ids.sort()
    trimmed = ids[:20]
    total = 0
    for key in sorted(by_status.keys()):
        total = int(total) + int(by_status[key])
    return {"readable": True, "reason": "", "by_status": by_status,
            "ids": trimmed, "total": int(total)}


def _autopilot_view(config: dict, ticks: list | None, ticks_err: str) -> dict:
    enabled = False
    if isinstance(config, dict):
        raw_enabled = config.get("enabled")
        if isinstance(raw_enabled, bool):
            enabled = bool(raw_enabled)
    latest: dict | None = None
    tick_count = 0
    if ticks is None:
        return {"readable": False, "reason": str(ticks_err or "Error"),
                "enabled": enabled, "tick_count": None, "latest": None}
    if not isinstance(ticks, list):
        return {"readable": False, "reason": "TypeError",
                "enabled": enabled, "tick_count": None, "latest": None}
    tick_count = int(len(ticks))
    if ticks:
        last = ticks[len(ticks) - 1]
        if not isinstance(last, dict):
            return {"readable": False, "reason": "TypeError",
                    "enabled": enabled, "tick_count": tick_count,
                    "latest": None}
        gate_status = last.get("gate_status")
        gate_text = "" if gate_status is None else str(gate_status)[:64]
        created = last.get("created")
        try:
            created_num = float(created)
        except (TypeError, ValueError):
            created_num = 0.0
        latest = {"status": str(last.get("status") or "")[:64],
                  "stop_class": str(last.get("stop_class") or "")[:64],
                  "stop_code": str(last.get("stop_code") or "")[:128],
                  "action_name": str(last.get("action_name") or "")[:64],
                  "gate_status": gate_text,
                  "tick_index": last.get("tick_index"),
                  "plan_signature": _truncate_sig(last.get("plan_signature"), 128),
                  "created": created_num}
    return {"readable": True, "reason": "", "enabled": bool(enabled),
            "tick_count": int(tick_count), "latest": latest}


def _next_step_view(*, manager, project_id: str, signature: str,
                    signature_err: str, stop: dict | None, stop_err: str,
                    gate: dict | None, funnel: dict | None,
                    preflight: dict | None, proposals: list | None,
                    summary: dict, autopilot_enabled: bool) -> dict:
    pid = str(project_id or "")
    sig_short = _truncate_sig(signature, 12)
    stop_class = ""
    stop_code = ""
    stop_next = "none"
    if isinstance(stop, dict):
        stop_class = str(stop.get("stop_class") or "")
        stop_code = str(stop.get("stop_code") or "")
        stop_next = str(stop.get("next_action") or "none")
    # 全体が読めないときは判定不能。再確認の方法だけ返す。
    if signature_err != "" or stop is None or gate is None or funnel is None \
            or preflight is None or proposals is None:
        basis = ("状態を確認できないため次の一手を確定できません。"
                 "計画署名=" + (sig_short if sig_short != "" else "確認できず")
                 + " 停止=" + (stop_class if stop_class != "" else "確認できず")
                 + "。材料(事前照合・完了ゲート・漏斗・停止分類)を再読込してください")
        return {"kind": "blocked_unknown", "title_ja": "確認できず",
                "detail_ja": "材料を再読込してください",
                "action": "none", "screen": "",
                "basis": basis[:500],
                "dry_run_action": "none", "gate_status": "preflight_unknown"}
    if not bool(summary.get("consistent", False)):
        basis = ("集計が一致しないため全体を確認できず扱いにします。"
                 "計画署名=" + sig_short + " 停止=" + stop_class
                 + "。材料を再読込してください")
        return {"kind": "blocked_unknown", "title_ja": "確認できず",
                "detail_ja": "集計不一致のため材料を再読込してください",
                "action": "none", "screen": "",
                "basis": basis[:500],
                "dry_run_action": "none", "gate_status": "preflight_unknown"}
    # 自走の許可アクションの選択は dry_run と同じ preview_planned_action で呼ぶ。
    # ティックの実装は変えない。読取不能は通過扱いにしない。
    preview = None
    preview_error = ""
    try:
        from app.autopilot import preview_planned_action as _preview

        preview = _preview(manager, pid, stop, signature)
    except Exception as exc:
        preview_error = _type_name(exc)
    if preview_error != "":
        basis = ("自走の一手を確認できないため進めません:"
                 + preview_error
                 + " 計画署名=" + sig_short + " 停止=" + stop_class)
        return {"kind": "blocked_unknown", "title_ja": "確認できず",
                "detail_ja": "自走の一手を確認できません",
                "action": "none", "screen": "",
                "basis": basis[:500],
                "dry_run_action": "none", "gate_status": "preflight_unknown"}
    if not isinstance(preview, dict):
        basis = ("自走の一手を確認できないため進めません:TypeError"
                 " 計画署名=" + sig_short + " 停止=" + stop_class)
        return {"kind": "blocked_unknown", "title_ja": "確認できず",
                "detail_ja": "自走の一手を確認できません",
                "action": "none", "screen": "",
                "basis": basis[:500],
                "dry_run_action": "none", "gate_status": "preflight_unknown"}
    planned_raw = preview.get("planned_action")
    if planned_raw is None:
        planned = "none"
    else:
        planned = str(planned_raw)
        if planned == "":
            planned = "none"
    gate_raw = preview.get("gate_status")
    if gate_raw is None:
        gate_status = "preflight_unknown"
    else:
        gate_status = str(gate_raw)
        if gate_status == "":
            gate_status = "preflight_unknown"
    reason_raw = preview.get("reason_ja")
    if reason_raw is None:
        preview_reason = ""
    else:
        preview_reason = str(reason_raw)[:200]
    if planned == "none" or gate_status in ("preflight_unknown", "stale"):
        reason = preview_reason if preview_reason != "" else "事前照合を確認できません"
        basis = ("事前照合を確認できないため自走は進めません。"
                 "計画署名=" + sig_short + " 停止=" + stop_class
                 + " 優先順位=安全側で停止"
                 + (" 理由=" + reason if reason != "" else ""))
        return {"kind": "blocked_unknown", "title_ja": "確認できず",
                "detail_ja": reason,
                "action": "none", "screen": "",
                "basis": basis[:500],
                "dry_run_action": "none", "gate_status": gate_status}
    # 全条件達成のときは達成を匂わせないよう人の最終確認に回す。
    if bool(summary.get("goal_achieved", False)):
        basis = ("全条件が証拠つきで達成のため人の最終確認に回します。"
                 "計画署名=" + sig_short + " 停止=" + stop_class
                 + " 優先順位=達成の確定は人が行う")
        return {"kind": "human_confirm", "title_ja": "人の確認が必要",
                "detail_ja": "全条件が達成のため人の最終確認を成果物の画面で行ってください",
                "action": "approve_result", "screen": "成果物",
                "basis": basis[:500],
                "dry_run_action": planned,
                "gate_status": gate_status}
    if stop_class == "missing_capability":
        outcome = str(preview.get("capability_outcome") or "error")
        state_text = str(preview.get("proposal_state") or "確認できず")[:120]
        if planned == "record_stop_class":
            basis = ("機能不足のため自走は進めません。"
                     "計画署名=" + sig_short + " 停止=" + stop_class
                     + " 優先順位=機能開発が先"
                     + " 理由=" + preview_reason)
            return {"kind": "capability_dev", "title_ja": "機能開発が必要",
                    "detail_ja": "不足機能の提案を不足機能の画面で確認してください",
                    "action": "resolve_development", "screen": "不足機能",
                    "basis": basis[:500],
                    "dry_run_action": planned,
                    "gate_status": gate_status}
        if outcome == "created" and bool(autopilot_enabled) \
                and planned == "capability_preflight":
            basis = ("自走が下書きを用意できるため自走が進めます。"
                     "計画署名=" + sig_short + " 停止=" + stop_class
                     + " 優先順位=下書きまで自走"
                     + " 見通し=" + state_text)
            return {"kind": "autonomous", "title_ja": "自走が進めます",
                    "detail_ja": "不足機能の下書きを自走が用意します(" + state_text + ")",
                    "action": "capability_preflight", "screen": "",
                    "basis": basis[:500],
                    "dry_run_action": planned,
                    "gate_status": gate_status}
        if outcome in ("existing", "created", "skipped"):
            detail = preview_reason if preview_reason != "" else (
                "不足機能の提案を不足機能の画面で確認してください")
            basis = ("機能不足のため人の操作が必要です。"
                     "計画署名=" + sig_short + " 停止=" + stop_class
                     + " 優先順位=提案の状態に応じた人の操作"
                     + " 見通し=" + state_text)
            return {"kind": "capability_dev", "title_ja": "機能開発が必要",
                    "detail_ja": detail[:300],
                    "action": stop_next if stop_next != "" else "resolve_development",
                    "screen": "不足機能",
                    "basis": basis[:500],
                    "dry_run_action": planned,
                    "gate_status": gate_status}
        basis = ("提案を確認できないため進めません。"
                 "計画署名=" + sig_short + " 停止=" + stop_class
                 + " 見通し=" + state_text)
        return {"kind": "blocked_unknown", "title_ja": "確認できず",
                "detail_ja": "提案を確認できないため進めません",
                "action": "none", "screen": "",
                "basis": basis[:500],
                "dry_run_action": "none", "gate_status": gate_status}
    if stop_class in ("missing_binding", "missing_coverage"):
        if planned == "":
            basis = ("自走の一手を確認できないため進めません:ValueError"
                     " 計画署名=" + sig_short + " 停止=" + stop_class)
            return {"kind": "blocked_unknown", "title_ja": "確認できず",
                    "detail_ja": "自走の一手を確認できません",
                    "action": "none", "screen": "",
                    "basis": basis[:500],
                    "dry_run_action": "none", "gate_status": gate_status}
        if bool(autopilot_enabled) and planned != "none" \
                and planned != "record_stop_class":
            basis = ("自走が可逆な作業を進められるため自走が進めます。"
                     "計画署名=" + sig_short + " 停止=" + stop_class
                     + " 優先順位=可逆作業だけ"
                     + " 一手=" + planned)
            return {"kind": "autonomous", "title_ja": "自走が進めます",
                    "detail_ja": "自走が " + planned + " を進めます",
                    "action": planned, "screen": "",
                    "basis": basis[:500],
                    "dry_run_action": planned, "gate_status": gate_status}
        basis = ("自走が無効または条件を満たさないため人の確認が必要です。"
                 "計画署名=" + sig_short + " 停止=" + stop_class
                 + " 優先順位=人の確定が先"
                 + " 一手=" + planned)
        return {"kind": "human_confirm", "title_ja": "人の確認が必要",
                "detail_ja": "保存済み材料の対応付け・候補の確認を計画内容の画面で行ってください",
                "action": planned, "screen": "計画内容",
                "basis": basis[:500],
                "dry_run_action": planned, "gate_status": gate_status}
    if stop_class == "missing_evidence":
        basis = ("証拠が不足しているため自走は進めません。"
                 "計画署名=" + sig_short + " 停止=" + stop_class
                 + " 優先順位=証拠の確認が先")
        return {"kind": "evidence_confirm", "title_ja": "証拠の確認が必要",
                "detail_ja": "実行証拠・成果物・外部証拠を成果物の画面で確認してください",
                "action": stop_next if stop_next != "" else "review_artifacts",
                "screen": "成果物",
                "basis": basis[:500],
                "dry_run_action": "record_stop_class",
                "gate_status": "ok"}
    if stop_class in ("missing_decision", "policy_block"):
        if stop_class == "policy_block":
            detail = "安全境界のため迂回せず承認・権限の制御に従ってください"
            screen = "計画内容"
        else:
            detail = "計画承認・指摘の採否・業務事実の確認を計画内容・実行の画面で行ってください"
            screen = "計画内容"
        basis = ("人の判断が必要なため自走は進めません。"
                 "計画署名=" + sig_short + " 停止=" + stop_class
                 + " 優先順位=人の判断が先")
        return {"kind": "human_confirm", "title_ja": "人の確認が必要",
                "detail_ja": detail,
                "action": stop_next if stop_next != "" else "none",
                "screen": screen,
                "basis": basis[:500],
                "dry_run_action": "record_stop_class",
                "gate_status": "ok"}
    basis = ("停止を確認できないため次の一手を確定できません。"
             "計画署名=" + sig_short + " 停止=" + stop_class
             + "。材料を再読込してください")
    return {"kind": "blocked_unknown", "title_ja": "確認できず",
            "detail_ja": "材料を再読込してください",
            "action": "none", "screen": "",
            "basis": basis[:500],
            "dry_run_action": "none", "gate_status": "ok"}


def build_goal_state(manager, project_id: str) -> dict:
    """進捗モデル(読み取り専用)。外部呼出0・DB/ファイルへの書込0。

    完了ゲート・漏斗・事前照合・停止分類・自走の設定を読み取るだけ。
    判定不能は unknown にし、達成・0件・不足なしに見せない。
    """
    pid = str(project_id or "")
    signature, signature_err = _read_signature(manager, pid)
    contract, contract_err = _read_contract(manager, pid)
    gate, gate_err = _read_gate(manager, pid)
    funnel, funnel_err = _read_funnel(manager, pid)
    preflight, preflight_err = _read_preflight(manager, pid)
    proposals, proposals_err = _read_proposals(manager, pid, signature)
    stop, stop_err, _built = _read_stop(manager, pid)
    mission_status, mission_err = _read_mission_status(manager, pid)
    auto_config, auto_ticks, auto_err = _read_autopilot(manager, pid)

    if isinstance(preflight, dict):
        preflight_status = "ok"
        preflight_reason = ""
    else:
        preflight_status = "unknown"
        preflight_reason = str(preflight_err or "Error")

    cids, cids_err = _criterion_ids(contract)
    if cids_err != "":
        cids = []
        contract_readable = False
        contract_reason = str(cids_err or "Error")
    else:
        contract_readable = True
        contract_reason = str(contract_err or "")

    gate_overall = False
    if isinstance(gate, dict):
        raw_achieved = gate.get("achieved")
        if isinstance(raw_achieved, bool):
            gate_overall = bool(raw_achieved)

    criteria: list[dict] = []
    for cid in cids:
        statement, statement_err = _statement_of(contract, cid)
        if statement_err != "":
            criteria.append({
                "criterion_id": cid,
                "statement_ref": "",
                "state": "unknown",
                "state_label_ja": STATE_JA["unknown"],
                "evidence_refs": {},
                "blockers": [],
                "reason": "達成条件の文面を確認できません:" + str(statement_err or "Error"),
            })
            continue
        classified = _classify_criterion(
            cid=cid, gate=gate, gate_err=gate_err,
            funnel=funnel, funnel_err=funnel_err,
            preflight=preflight, preflight_err=preflight_err,
            proposals=proposals, proposals_err=proposals_err,
            mission_status=mission_status, mission_err=mission_err,
            gate_overall_achieved=gate_overall)
        state = str(classified.get("state") or "unknown")
        if state not in STATE_JA:
            state = "unknown"
        criteria.append({
            "criterion_id": cid,
            "statement_ref": _safe_short_80(statement),
            "state": state,
            "state_label_ja": STATE_JA[state],
            "evidence_refs": classified.get("evidence_refs")
            if isinstance(classified.get("evidence_refs"), dict) else {},
            "blockers": list(classified.get("blockers") or [])
            if isinstance(classified.get("blockers"), list) else [],
            "reason": str(classified.get("reason") or "")[:300],
        })

    total = int(len(cids))
    summary_inner = _summarize_states(criteria, total)
    # 契約自体が読めないときは全体を unknown 扱いにする。
    if not bool(contract_readable):
        summary_inner = {"counts": {"achieved": 0, "capability_gap": 0,
                                    "evidence_gap": 0, "human_decision": 0,
                                    "unknown": int(total)},
                         "total": int(total), "summed": int(total),
                         "consistent": bool(int(total) == 0),
                         "goal_achieved": False,
                         "goal_remaining": list(cids)}
        if int(total) > 0:
            summary_inner["consistent"] = False
    consistent = bool(summary_inner.get("consistent", False))
    # 合計が総数と一致しなければ全体を unknown 扱いにする。
    if not bool(consistent):
        unknown_criteria: list[dict] = []
        for entry in criteria:
            cid = str(entry.get("criterion_id") or "")
            unknown_criteria.append({
                "criterion_id": cid,
                "statement_ref": str(entry.get("statement_ref") or "")[:80],
                "state": "unknown",
                "state_label_ja": STATE_JA["unknown"],
                "evidence_refs": {},
                "blockers": [],
                "reason": "集計不一致のため全体を確認できず扱いにします",
            })
        criteria = unknown_criteria
        summary_inner = {"counts": {"achieved": 0, "capability_gap": 0,
                                    "evidence_gap": 0, "human_decision": 0,
                                    "unknown": int(total)},
                         "total": int(total), "summed": int(total),
                         "consistent": False, "goal_achieved": False,
                         "goal_remaining": list(cids)}

    summary = {"achieved": int(summary_inner["counts"]["achieved"]),
               "capability_gap": int(summary_inner["counts"]["capability_gap"]),
               "evidence_gap": int(summary_inner["counts"]["evidence_gap"]),
               "human_decision": int(summary_inner["counts"]["human_decision"]),
               "unknown": int(summary_inner["counts"]["unknown"]),
               "total": int(summary_inner["total"]),
               "consistent": bool(summary_inner["consistent"]),
               "goal_achieved": bool(summary_inner["goal_achieved"]),
               "goal_remaining": list(summary_inner["goal_remaining"])}

    proposals_view = _proposals_view(proposals, proposals_err)

    autopilot_view = _autopilot_view(auto_config, auto_ticks, auto_err)
    autopilot_enabled = False
    if isinstance(autopilot_view, dict):
        raw_flag = autopilot_view.get("enabled")
        if isinstance(raw_flag, bool):
            autopilot_enabled = bool(raw_flag)

    if isinstance(stop, dict):
        also_raw = stop.get("also")
        also_list = list(also_raw) if isinstance(also_raw, list) else []
        also_clean: list[dict] = []
        for item in also_list:
            if not isinstance(item, dict):
                continue
            also_clean.append({
                "stop_class": str(item.get("stop_class") or "")[:64],
                "stop_code": str(item.get("stop_code") or "")[:128],
                "next_action": str(item.get("next_action") or "none")[:64],
                "reason_ja": str(item.get("reason_ja") or "")[:300],
            })
        also_counts: dict[str, int] = {}
        for item in also_clean:
            key = str(item.get("stop_class") or "")
            if key == "":
                continue
            also_counts[key] = int(also_counts.get(key, 0)) + 1
        stop_view = {"stop_class": str(stop.get("stop_class") or "")[:64],
                     "stop_code": str(stop.get("stop_code") or "")[:128],
                     "reason_ja": str(stop.get("reason_ja") or "")[:500],
                     "next_action": str(stop.get("next_action") or "none")[:64],
                     "also": also_clean,
                     "also_counts": also_counts,
                     "also_total": int(len(also_clean))}
    else:
        stop_view = {"stop_class": "unknown",
                     "stop_code": "unknown:empty",
                     "reason_ja": "停止分類を確認できません:"
                     + str(stop_err or "Error"),
                     "next_action": "none", "also": [],
                     "also_counts": {}, "also_total": 0}

    next_step = _next_step_view(
        manager=manager, project_id=pid, signature=signature,
        signature_err=signature_err, stop=stop, stop_err=stop_err,
        gate=gate, funnel=funnel, preflight=preflight,
        proposals=proposals, summary=summary,
        autopilot_enabled=bool(autopilot_enabled))

    # B-1補正(0): 達成条件が0件のPJは証拠確認ではなく人の作成待ち。
    # 件数は0のまま・goal_achievedはfalse・「達成」「進んでいる」に見せない。
    if int(summary.get("total", 0)) == 0:
        basis_zero = ("目標と達成条件・計画が未作成のため次の一手を確定できません。"
                      "計画署名=" + (_truncate_sig(signature, 12)
                                  if str(signature_err or "") == "" and str(signature or "") != ""
                                  else "確認できず")
                      + " 停止=" + (str(stop_view.get("stop_class") or "")
                                  if isinstance(stop_view, dict) else "確認できず"))
        zero_gate = "ok"
        if isinstance(next_step, dict):
            raw_gate = next_step.get("gate_status")
            if raw_gate is None:
                zero_gate = "preflight_unknown"
            else:
                zero_gate = str(raw_gate)
                if zero_gate == "":
                    zero_gate = "preflight_unknown"
        next_step = {"kind": "human_confirm", "title_ja": "人の確認が必要",
                     "detail_ja": "目標と達成条件・計画が未作成です。作成してください",
                     "action": "create_goal_plan", "screen": "目標と計画",
                     "basis": basis_zero[:500],
                     "dry_run_action": "none",
                     "gate_status": zero_gate}

    return {"schema": SCHEMA,
            "project_id": pid,
            "read_only": True,
            "external_sends": 0,
            "external_calls": 0,
            "plan_signature": _truncate_sig(signature, 128),
            "plan_signature_error": str(signature_err or ""),
            "contract_error": str(contract_reason or ""),
            "preflight_status": preflight_status,
            "preflight_reason": str(preflight_reason or ""),
            "stop": stop_view,
            "criteria": criteria,
            "summary": summary,
            "proposals": proposals_view,
            "autopilot": autopilot_view,
            "next_step": next_step}
