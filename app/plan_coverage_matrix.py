"""Stage 0-2: 指摘被覆行列 (issue coverage matrix).

決定的な純粋関数群。LLM の自己申告を信用せず、対応表
(`build_issue_bindings` の `candidate_task_keys` / `candidate_criterion_ids` /
`state`) と候補 `changes` の `target` を照合する。外部AIの指摘本文は命令と
して扱わない。秘密値・指摘本文の全文は保持しない。
"""
from __future__ import annotations

from app.experience_store import fingerprint

MATRIX_VERSION = "coverage-matrix-v1"
COVERAGE_STATES = ("uncovered", "partial", "covered_candidate", "rejected")
BLOCKING_DISPOSITIONS = {"development", "business_fact", "unresolved"}
EXEC_PIPELINE_TARGET = "execution_pipeline"


def _change_targets(changes) -> list:
    targets = []
    for change in changes or []:
        if isinstance(change, dict):
            target = str(change.get("target") or "")
            if target and target not in targets:
                targets.append(target)
    return targets


def change_id_of(change: dict) -> str:
    """変更の決定的な短い識別子。内容はハッシュ化するだけで保持しない。"""
    target = str((change or {}).get("target") or "")
    after = (change or {}).get("after")
    return fingerprint(["change", target, after if isinstance(after, str) else ""])[:16]


def _change_by_target(changes, target: str) -> dict | None:
    for change in changes or []:
        if isinstance(change, dict) and str(change.get("target") or "") == str(target):
            return change
    return None


def _binding_of(issue: dict):
    binding = (issue or {}).get("binding")
    return binding if isinstance(binding, dict) else None


def _step_number(task_order, task_key: str) -> int:
    try:
        return list(task_order or []).index(str(task_key)) + 1
    except (ValueError, TypeError):
        return 0


def _delta_refs(changes, targets: list) -> list:
    refs = []
    for target in targets or []:
        change = _change_by_target(changes, target)
        if change is not None:
            refs.append({"target": str(target), "change_id": change_id_of(change)})
    return refs


def coverage_gate_error(matrix) -> str | None:
    """全行 covered_candidate でなければ日本語の拒否理由を返す。行列が無ければ None。"""
    rows = matrix.get("rows") if isinstance(matrix, dict) else None
    if not rows:
        return None
    bad = [r for r in rows if isinstance(r, dict) and r.get("coverage") != "covered_candidate"]
    if not bad:
        return None
    parts = [f"{r.get('issue_id', '?')}({r.get('coverage', '?')}: {r.get('reason', '')})"
             for r in bad]
    return ("被覆不足のため適用できません。全ての指摘が covered_candidate である必要があります。"
            "未被覆: " + " / ".join(parts))


def _amend_shared_target(action: dict | None) -> str:
    """同一 amend 差分を複数指摘で共有してよい場合の target。既定では共有しない(空文字)。"""
    return ""


def build_coverage_matrix(*, issues, actions, changes, task_order=None) -> dict:
    """指摘ごとに1行の被覆行列を決定的に作る。同一入力では同一出力(時刻なし)。

    - `multiple_targets` で複数工程に束縛された指摘は工程単位の子行へ分割し、
      元の指摘IDを親行・子の `parent_issue_id` に残す。親行は消さない。
    - `covered_candidate` は束縛された全工程に差分がある場合だけ。別指摘の
      amend 差分は共有しない(1件の変更が複数指摘を解消したと推定しない)。
      ただし同一 target を指す正式な対応(`binds.task_key`)を持つ amend は、
      その target について共有して被覆できる。
    - 全体再構成 (`execution_pipeline`) は全工程を被覆する。
    - `invalid_reference` / `ambiguous` は `uncovered`。対応種別が停止系の
      指摘は `rejected`(被覆ではなく停止として扱う)。
    - 対応表の無い指摘は修正対応の有無で判定する(既存経路の互換用。理由に明記)。
    """
    actions_by_id: dict[str, dict] = {}
    for action in actions or []:
        if isinstance(action, dict) and action.get("issue_id"):
            actions_by_id.setdefault(str(action["issue_id"]), action)
    pool = _change_targets(changes)
    pipeline = EXEC_PIPELINE_TARGET in pool
    consumed: set[str] = set()
    amend_shared: dict[str, str] = {}
    for issue in issues or []:
        if not isinstance(issue, dict):
            continue
        issue_id = str(issue.get("id") or issue.get("issue_id") or "")
        if issue_id:
            shared = _amend_shared_target(actions_by_id.get(issue_id))
            if shared:
                amend_shared[issue_id] = shared
    rows: list[dict] = []

    for issue in issues or []:
        if not isinstance(issue, dict):
            continue
        issue_id = str(issue.get("id") or issue.get("issue_id") or "")
        if not issue_id:
            continue
        binding = _binding_of(issue)
        action = actions_by_id.get(issue_id)
        action = action if isinstance(action, dict) else None
        disposition = str((action or {}).get("disposition") or "")
        action_targets = []
        if action and str(action.get("target") or ""):
            action_targets.append(str(action["target"]))
        if disposition in BLOCKING_DISPOSITIONS:
            rows.append({
                "issue_id": issue_id,
                "parent_issue_id": "",
                "bound_steps": [],
                "bound_sc": [],
                "delta_refs": _delta_refs(changes, [t for t in action_targets if t in pool]),
                "coverage": "rejected",
                "reason": f"対応種別が{disposition}のため対応しない。被覆ではなく停止として扱う",
            })
            continue
        state = str((binding or {}).get("state") or "")
        if state in ("invalid_reference", "ambiguous"):
            confirmed = (action or {}).get('binds') or {}
            confirmed_sc = [str(x) for x in confirmed.get('goal_criterion_ids') or [] if str(x)]
            if disposition == 'rebuild_generic' and pipeline and confirmed_sc:
                rows.append({
                    "issue_id": issue_id, "parent_issue_id": "",
                    "bound_steps": list((binding or {}).get("steps") or []),
                    "bound_sc": confirmed_sc,
                    "delta_refs": _delta_refs(changes, [EXEC_PIPELINE_TARGET]),
                    "coverage": "covered_candidate",
                    "reason": "明示確認した達成条件と全体再構成差分がある",
                })
                continue
            rows.append({
                "issue_id": issue_id,
                "parent_issue_id": "",
                "bound_steps": list((binding or {}).get("steps") or []),
                "bound_sc": [str(x) for x in ((binding or {}).get("candidate_criterion_ids") or [])],
                "delta_refs": [],
                "coverage": "uncovered",
                "reason": f"対応表が{state}のため対応付けできない",
            })
            continue
        bound_keys = [str(x) for x in ((binding or {}).get("candidate_task_keys") or []) if str(x)]
        bound_steps = [x for x in ((binding or {}).get("steps") or []) if isinstance(x, int)]
        bound_sc = [str(x) for x in ((binding or {}).get("candidate_criterion_ids") or []) if str(x)]
        if not bound_keys:
            # 対応表の無い指摘(既存経路の互換)。修正対応の有無だけで判定する。
            hit = [t for t in action_targets if t in pool]
            if pipeline or hit:
                rows.append({
                    "issue_id": issue_id,
                    "parent_issue_id": "",
                    "bound_steps": list(bound_steps),
                    "bound_sc": list(bound_sc),
                    "delta_refs": _delta_refs(changes, [EXEC_PIPELINE_TARGET] if pipeline else hit),
                    "coverage": "covered_candidate",
                    "reason": "対応表なし。修正対応の有無で判定し差分がある",
                })
            else:
                rows.append({
                    "issue_id": issue_id,
                    "parent_issue_id": "",
                    "bound_steps": list(bound_steps),
                    "bound_sc": list(bound_sc),
                    "delta_refs": [],
                    "coverage": "uncovered",
                    "reason": "対応表がなく修正対応もない",
                })
            continue
        if state == "multiple_targets" and len(bound_keys) >= 2:
            children = []
            for key in bound_keys:
                number = _step_number(task_order, key)
                if number:
                    child_id = f"{issue_id}::s{number:02d}"
                    child_steps = [number]
                else:
                    child_id = f"{issue_id}::{fingerprint(['child', issue_id, key])[:8]}"
                    child_steps = []
                if pipeline:
                    coverage = "covered_candidate"
                    refs = _delta_refs(changes, [EXEC_PIPELINE_TARGET])
                    reason = "全体再構成により被覆される"
                elif key in pool and (key not in consumed or amend_shared.get(issue_id) == key):
                    coverage = "covered_candidate"
                    refs = _delta_refs(changes, [key])
                    reason = "束縛された工程にこの候補の差分がある"
                    consumed.add(key)
                elif key in pool:
                    coverage = "partial"
                    refs = _delta_refs(changes, [key])
                    reason = "差分はあるが他の指摘で利用済みのため単独解消とみなさない"
                else:
                    coverage = "uncovered"
                    refs = []
                    reason = "束縛された工程にこの候補の差分がない"
                children.append({
                    "issue_id": child_id,
                    "parent_issue_id": issue_id,
                    "bound_steps": child_steps,
                    "bound_sc": list(bound_sc),
                    "delta_refs": refs,
                    "coverage": coverage,
                    "reason": f"親指摘の分割子。{reason}",
                })
            coverages = {c["coverage"] for c in children}
            if coverages == {"covered_candidate"}:
                parent_coverage = "covered_candidate"
                parent_reason = f"分割した子{len(children)}件が全て被覆された。親は消さず残す"
            elif "uncovered" in coverages:
                parent_coverage = "uncovered"
                parent_reason = f"分割した子{len(children)}件のうち未被覆がある"
            else:
                parent_coverage = "partial"
                parent_reason = f"分割した子{len(children)}件の一部だけ被覆された"
            parent_refs: list[dict] = []
            for child in children:
                for ref in child["delta_refs"]:
                    if ref not in parent_refs:
                        parent_refs.append(ref)
            rows.append({
                "issue_id": issue_id,
                "parent_issue_id": "",
                "bound_steps": list(bound_steps),
                "bound_sc": list(bound_sc),
                "delta_refs": parent_refs,
                "coverage": parent_coverage,
                "reason": parent_reason,
            })
            rows.extend(children)
            continue
        if pipeline:
            rows.append({
                "issue_id": issue_id,
                "parent_issue_id": "",
                "bound_steps": list(bound_steps),
                "bound_sc": list(bound_sc),
                "delta_refs": _delta_refs(changes, [EXEC_PIPELINE_TARGET]),
                "coverage": "covered_candidate",
                "reason": "全体再構成により束縛された全工程が被覆される",
            })
            continue
        have = [k for k in bound_keys if k in pool]
        shared = [k for k in have if k in consumed and amend_shared.get(issue_id) != k]
        fresh = [k for k in have if k not in consumed or amend_shared.get(issue_id) == k]
        if len(have) == len(bound_keys) and not shared:
            consumed.update(fresh)
            rows.append({
                "issue_id": issue_id,
                "parent_issue_id": "",
                "bound_steps": list(bound_steps),
                "bound_sc": list(bound_sc),
                "delta_refs": _delta_refs(changes, bound_keys),
                "coverage": "covered_candidate",
                "reason": f"束縛された全{len(bound_keys)}工程にこの候補の差分がある",
            })
        elif not have:
            rows.append({
                "issue_id": issue_id,
                "parent_issue_id": "",
                "bound_steps": list(bound_steps),
                "bound_sc": list(bound_sc),
                "delta_refs": [],
                "coverage": "uncovered",
                "reason": "束縛された工程にこの候補の差分がない",
            })
        elif shared:
            rows.append({
                "issue_id": issue_id,
                "parent_issue_id": "",
                "bound_steps": list(bound_steps),
                "bound_sc": list(bound_sc),
                "delta_refs": _delta_refs(changes, have),
                "coverage": "partial",
                "reason": f"差分はあるが{','.join(shared)}は他の指摘で利用済みのため単独解消とみなさない",
            })
        else:
            missing = [k for k in bound_keys if k not in pool]
            rows.append({
                "issue_id": issue_id,
                "parent_issue_id": "",
                "bound_steps": list(bound_steps),
                "bound_sc": list(bound_sc),
                "delta_refs": _delta_refs(changes, have),
                "coverage": "partial",
                "reason": f"束縛{len(bound_keys)}工程のうち{len(have)}工程に差分がある。不足: {','.join(missing)}",
            })
    return {"version": MATRIX_VERSION, "rows": rows}
