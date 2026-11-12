"""AG2: 実行前計画の構造テキスト(公開可能範囲のみ).

純関数・決定的。状態を読まないし書かない。例外を外へ出さない。
題名・説明・達成条件文・パス・原本名・自由記述は出さない。
種別・出力種類・検証方法・承認境界は既存契約の値を写すだけ。
"""
from __future__ import annotations

from app.structured_planning import contract_of as _contract_of

PREAMBLE = (
    "実行前の計画評価です。成果物本体はまだ作成されていないのが正常です。"
    "未作成であることだけを理由に、不合格・判定不能にしないでください。"
    "評価するのは、目標と達成条件に対して、"
    "工程・依存関係・出力契約・検証方法が足りているかです。"
    "この構造は工程の並びと出力契約だけを示し、入力の事実が揃っているかは保証しません。"
)

_NO_INFO = "工程情報なし"

_OUTPUT_ORDER = ("文書", "表", "検証報告", "準備記録", "外部操作記録", "不明")

_METHODS = (
    "count",
    "artifact_count",
    "section_char_count",
    "approval_stage_matrix",
    "headings",
    "phrase",
    "llm_semantic",
)


def _kind_of(contract) -> str:
    if not isinstance(contract, dict):
        return "不明"
    if contract.get("final_verification") is True:
        return "最終検証"
    role = contract.get("role")
    if not isinstance(role, str):
        return "不明"
    if role == "preparation":
        return "準備"
    if role == "final_verification":
        return "最終検証"
    if role == "external_action":
        return "外部操作"
    if role == "execution":
        acts = contract.get("action_requirements")
        if isinstance(acts, list) and len(acts) > 0:
            return "外部操作"
        return "実行"
    return "不明"


def _single_output_kind(item) -> str:
    if not isinstance(item, dict):
        return "不明"
    cat = item.get("artifact_category")
    path = item.get("path")
    cat_s = cat if isinstance(cat, str) else ""
    path_s = path if isinstance(path, str) else ""
    low = path_s.lower()
    if cat_s == "verification_report":
        return "検証報告"
    if cat_s == "preparation_record":
        return "準備記録"
    if cat_s == "external_action_record":
        return "外部操作記録"
    if low.endswith(".csv"):
        return "表"
    if low.endswith(".md"):
        return "文書"
    if cat_s == "table":
        return "表"
    if cat_s in (
        "document",
        "checklist",
        "faq_document",
        "procedure_document",
        "reviewed_document",
        "multi_output_document",
    ):
        return "文書"
    return "不明"


def _outputs_label(contract) -> str:
    if not isinstance(contract, dict):
        return "不明×0"
    raw = contract.get("outputs")
    if not isinstance(raw, list) or len(raw) == 0:
        return "不明×0"
    counts: dict[str, int] = {}
    for item in raw:
        kind = _single_output_kind(item)
        counts[kind] = counts.get(kind, 0) + 1
    parts: list[str] = []
    for kind in _OUTPUT_ORDER:
        if kind in counts:
            parts.append(kind + "×" + str(counts[kind]))
    if not parts:
        return "不明×0"
    return "、".join(parts)


def _deps_label(task, numbers: dict) -> str:
    if not isinstance(task, dict):
        return "不明"
    if not isinstance(numbers, dict):
        return "不明"
    raw = task.get("depends_on")
    if not isinstance(raw, list):
        return "不明"
    nums: list[int] = []
    for dep in raw:
        if not isinstance(dep, str):
            continue
        if dep in numbers:
            num = numbers[dep]
            if isinstance(num, int) and num not in nums:
                nums.append(num)
    if not nums:
        return "なし"
    return ",".join(str(n) for n in nums)


def _prior_label(task, numbers: dict, self_no: int) -> str:
    if not isinstance(task, dict):
        return "不明"
    if not isinstance(numbers, dict):
        return "不明"
    if not isinstance(self_no, int):
        return "不明"
    raw = task.get("depends_on")
    if not isinstance(raw, list):
        return "不明"
    if len(raw) == 0:
        return "なし"
    self_key = task.get("task_key")
    for dep in raw:
        if not isinstance(dep, str):
            return "不明"
        if dep not in numbers:
            return "順序不正"
        num = numbers[dep]
        if not isinstance(num, int):
            return "不明"
        if num >= self_no:
            return "順序不正"
        if isinstance(self_key, str) and self_key != "" and dep == self_key:
            return "順序不正"
    return "あり"


def _verify_label(contract) -> str:
    if not isinstance(contract, dict):
        return "不明"
    raw = contract.get("requirement_matrix")
    if not isinstance(raw, list):
        return "不明"
    if len(raw) == 0:
        return "なし"
    found: list[str] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        method = row.get("method")
        if not isinstance(method, str):
            continue
        if method in _METHODS and method not in found:
            found.append(method)
    if not found:
        return "不明"
    return "、".join(sorted(found))


def _approval_label(contract) -> str:
    if not isinstance(contract, dict):
        return "不明"
    for key in ("approval_required", "human_confirmation_required"):
        if key in contract and not isinstance(contract.get(key), bool):
            return "不明"
    if "action_requirements" in contract and not isinstance(
        contract.get("action_requirements"), list
    ):
        return "不明"
    role = contract.get("role")
    if "role" in contract and not isinstance(role, str):
        return "不明"
    if contract.get("approval_required") is True:
        return "人間承認が必要"
    if contract.get("human_confirmation_required") is True:
        return "人間承認が必要"
    acts = contract.get("action_requirements")
    if isinstance(acts, list) and len(acts) > 0:
        return "外部操作は予定のみ"
    if role == "external_action":
        return "外部操作は予定のみ"
    return "なし"


def build_structure_text(mission: dict) -> str:
    if not isinstance(mission, dict):
        return ""
    if "tasks" not in mission or mission.get("tasks") is None:
        return ""
    raw = mission.get("tasks")
    if not isinstance(raw, list):
        return ""
    if len(raw) == 0:
        return PREAMBLE + "\n" + _NO_INFO
    order: list = list(raw)
    renumbered: dict[str, int] = {}
    for pos, task in enumerate(order, 1):
        if isinstance(task, dict):
            key = task.get("task_key")
            if isinstance(key, str) and key != "" and key not in renumbered:
                renumbered[key] = pos
    contracts: list = []
    for task in order:
        if isinstance(task, dict):
            contracts.append(_contract_of(task))
        else:
            contracts.append(None)
    has_any = False
    for item in contracts:
        if isinstance(item, dict):
            has_any = True
            break
    if not has_any:
        return PREAMBLE + "\n" + _NO_INFO
    lines: list[str] = []
    for idx, task in enumerate(order, 1):
        contract = contracts[idx - 1]
        kind = _kind_of(contract)
        deps = _deps_label(task, renumbered)
        outs = _outputs_label(contract)
        prior = _prior_label(task, renumbered, idx)
        ver = _verify_label(contract)
        appr = _approval_label(contract)
        lines.append(
            "工程"
            + str(idx)
            + ": 種別="
            + kind
            + "; 依存="
            + deps
            + "; 出力="
            + outs
            + "; 先行工程="
            + prior
            + "; 検証="
            + ver
            + "; 承認境界="
            + appr
        )
    return PREAMBLE + "\n" + "\n".join(lines)
