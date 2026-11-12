"""P1: version-bound, read-only local repair candidate preview."""
from __future__ import annotations

import re
import unicodedata
from copy import deepcopy

from app.goal_contract import preview as contract_preview
from app.goal_review import ReviewStore, plan_snapshot
from app.plan_feedback import issues_for, normalize_issue_text
from app.structured_planning import contract_of

BUSINESS_FACT = "business_fact"
SAFE_PLAN_PATCH = "safe_plan_patch"
DEVELOPMENT_REQUIRED = "development_required"

_BUSINESS = re.compile(r"リード|有効条件|対象顧客|予算|価格|配賦|所属|乗替|業務事実|承認者")
_DEVELOPMENT = re.compile(r"実装|API|機能追加|専用処理|コード|自動化|連携|認証|データベース")
_ORDER = re.compile(r"順序|先に|後に|依存|禁止|してはいけ|公開前|送信前|承認後")
_ID = re.compile(r"\b(?:SC|C)\d{2}\b", re.I)

#: P2-1: 指摘の修正案(remedy)を工程説明へ反映する際の上限と追記タグ。
REMEDY_MAX_LENGTH = 300
REMEDY_TAG = "指摘対応: "

#: P2-1: remedy に含めたら反映しない語の固定一覧。外部操作の命令
#: (実行・送信・公開・削除・支払)・URL・認証情報らしき語を最小限で列挙する。
#: 判定時は _BUSINESS・_ORDER の既存語彙も再利用する(迷えば反映しない側)。
REMEDY_BLOCKED_WORDS = (
    "実行",
    "送信",
    "送付",
    "公開",
    "削除",
    "消去",
    "支払",
    "決済",
    "パスワード",
    "認証情報",
    "秘密鍵",
    "トークン",
    "指摘対応",
    "転送",
    "投稿",
    "メール",
    "書き込",
    "上書き",
    "課金",
    "購入",
    "振込",
    "送金",
    "入金",
    "注文",
    "外部",
    "apikey",
    "apiキー",
    "curl",
    "wget",
    "sudo",
    "rmrf",
    "upload",
    "post",
    "transfer",
    "purchase",
    "email",
    "http://",
    "https://",
    "send",
    "publish",
    "delete",
    "remove",
    "execute",
    "payment",
    "password",
    "secret",
    "token",
)
_REMEDY_BLOCKED = re.compile(
    "|".join(re.escape(word) for word in REMEDY_BLOCKED_WORDS), re.IGNORECASE)


def _normalize_remedy(value: str) -> str:
    """remedy の正規化(NFKC・制御文字→空白・空白畳み込み)。信頼できない入力用。"""
    text = unicodedata.normalize("NFKC", value)
    chars = [" " if unicodedata.category(ch).startswith("C") else ch for ch in text]
    return re.sub(r"\s+", " ", "".join(chars)).strip()


def _compact_alnum(text: str) -> str:
    """NFKC・小文字化済み文から英数字と日本語の文字だけを残す。"""
    kept = []
    for ch in text:
        if "0" <= ch <= "9" or "a" <= ch <= "z":
            kept.append(ch)
        elif "\u3041" <= ch <= "\u3096" or "\u30a1" <= ch <= "\u30fa":
            kept.append(ch)
        elif ch in ("ー", "々", "〆", "ヵ", "ヶ"):
            kept.append(ch)
        elif "\u3400" <= ch <= "\u9fff" or "\uF900" <= ch <= "\uFAFF":
            kept.append(ch)
        elif "\U00020000" <= ch <= "\U0002EBEF":
            kept.append(ch)
    return "".join(kept)


def _evaluate_remedy(remedy) -> tuple:
    """remedy の反映可否を決定的に判定する。戻り値は (正規化済み, 反映可否, 理由コード)。

    理由コードは no_remedy / remedy_too_long / remedy_blocked /
    remedy_unusable の固定値。反映可否が真のとき理由コードは空。
    禁止語判定は拒否リスト(denylist)であり完全ではない。
    ホモグリフ・言い換えは検出できない。反映は人の承認が前提。
    """
    if remedy is None:
        return "", False, "no_remedy"
    if not isinstance(remedy, str):
        return "", False, "remedy_unusable"
    normalized = _normalize_remedy(remedy)
    if not normalized:
        return "", False, "no_remedy"
    if len(normalized) > REMEDY_MAX_LENGTH:
        return "", False, "remedy_too_long"
    lowered = normalized.lower()
    compact = re.sub(r"\s+", "", lowered)
    calnum = _compact_alnum(lowered)
    if ("http://" in lowered or "https://" in lowered
            or "http://" in compact or "https://" in compact):
        return "", False, "remedy_blocked"
    if (_BUSINESS.search(normalized) or _BUSINESS.search(compact)
            or _BUSINESS.search(calnum)
            or _ORDER.search(normalized) or _ORDER.search(compact)
            or _ORDER.search(calnum)
            or _REMEDY_BLOCKED.search(lowered) or _REMEDY_BLOCKED.search(compact)
            or _REMEDY_BLOCKED.search(calnum)):
        return "", False, "remedy_blocked"
    return normalized, True, ""


def _review_store(memory_path):
    """Instantiate ReviewStore without keeping a leftover test double.

    Tests patch ``app.local_patch_preview.ReviewStore.get``. If this module was
    first imported while ``app.goal_review.ReviewStore`` was patched, the bound
    name can remain a mock after that patch is undone. A mock ``get`` is truthy
    and would invent approvals. Only a real class is used.
    """
    global ReviewStore
    cls = ReviewStore
    if not isinstance(cls, type):
        from app.goal_review import ReviewStore as live

        if not isinstance(live, type):
            raise TypeError("ReviewStore is unavailable")
        ReviewStore = live
        cls = live
    return cls(memory_path)


def _stable_issue_id(issue: dict, index: int) -> str:
    return str(issue.get("id") or issue.get("issue_id") or f"issue-{index:02d}")


def _criterion(issue: dict, criteria: list[dict]) -> dict:
    text = " ".join((str(issue.get("criterion") or ""), str(issue.get("text") or "")))
    wanted = [x.upper() for x in _ID.findall(text)]
    by_id = {str(x.get("criterion_id") or "").upper(): x for x in criteria}
    for ident in wanted:
        if ident in by_id:
            return by_id[ident]
    normalized = normalize_issue_text(text)
    for item in criteria:
        statement = normalize_issue_text(item.get("statement") or "")
        if statement and (statement in normalized or normalized in statement):
            return item
    return {}


def _task_for(criterion: dict, tasks: list[dict]) -> dict:
    keys = list(criterion.get("exec_task_keys") or [])
    cid = str(criterion.get("criterion_id") or "")
    keys.extend([cid] if cid else [])
    by_key = {str(x.get("task_key") or ""): x for x in tasks}
    for key in keys:
        if key in by_key:
            return by_key[key]
    return {}


def _confirmed_targets(manager, project_id: str, signature: str,
                       goal_contract_hash: str, source: list[dict]) -> dict:
    """有効な正本スナップショットの確定対応を issue_id -> 確定記録で返す。

    決定的・読み取り専用・外部通信なし。正本が無い/無効/読めない場合は {}
    を返し、呼び出し側は従来のテキスト照合だけを使う。検証の失敗は
    ValueError 系だけを無効扱いにし、想定外の例外は握りつぶさず伝播させる。
    """
    from app.experience_store import fingerprint as _fingerprint
    from app.plan_feedback import (
        check_binding_snapshot_current, confirmed_target_for, review_fingerprint_of,
    )

    store = _review_store(manager.memory.path)
    row = store.get(project_id, "binding_snapshot", signature)
    if not isinstance(row, dict):
        return {}
    snapshot_obj = row.get("snapshot")
    if not isinstance(snapshot_obj, dict):
        return {}
    review = store.get(project_id, "plan", signature) or {}
    if not isinstance(review, dict):
        review = {}
    packet = review.get("packet")
    packet_hash = _fingerprint(packet) if packet else ""
    reviews = review.get("reviews") or []
    review_fingerprint = review_fingerprint_of(signature, reviews)
    try:
        check_binding_snapshot_current(
            snapshot_obj, plan_signature=signature,
            goal_contract_hash=str(goal_contract_hash or ""),
            packet_hash=packet_hash, review_fingerprint=review_fingerprint,
            current_reviews=reviews)
    except ValueError:
        return {}
    out: dict[str, dict] = {}
    for issue in source or []:
        if not isinstance(issue, dict):
            continue
        iid = str(issue.get("id") or issue.get("issue_id") or "")
        if not iid or iid in out:
            continue
        entry = confirmed_target_for(snapshot_obj, iid)
        if isinstance(entry, dict):
            out[iid] = entry
    return out


def _confirmed_binding(entry: dict | None, by_criterion: dict, by_task: dict):
    """確定記録を現行の達成条件・工程の辞書へ解決する。曖昧なら None。

    確定記録の達成条件・工程が現行計画に存在しない場合は結合しない
    (推測で結ばない)。task_key が空の確定は達成条件から決定的に解決し、
    解決できなければ結合しない。
    """
    if not isinstance(entry, dict):
        return None
    cid = str(entry.get("criterion_id") or "").upper()
    criterion = by_criterion.get(cid) if cid else None
    if not criterion:
        return None
    tkey = str(entry.get("task_key") or "")
    if tkey:
        task = by_task.get(tkey)
        if not task:
            return None
    else:
        task = _task_for(criterion, list(by_task.values()))
        if not task:
            return None
    return criterion, task


def _classification(text: str, task: dict) -> str:
    if _BUSINESS.search(text):
        return BUSINESS_FACT
    contract = contract_of(task) or {}
    dedicated = bool(contract.get("execution_kind") or contract.get("public_web_research"))
    if _DEVELOPMENT.search(text) or dedicated and not _ORDER.search(text):
        return DEVELOPMENT_REQUIRED
    return SAFE_PLAN_PATCH


def _patch(text: str, task: dict, remedy: str = "") -> dict | None:
    """局所修復候補を作る。決定的・書き込みなし・外部呼出なし。

    remedy は信頼できない入力として扱い、_evaluate_remedy で反映可否を
    判定する。反映可能なときだけ既存の定型文に加えて末尾に1行追記する。
    """
    if not task:
        return None
    instruction = (
        "承認済み成果物・公開済みサイト/フォームを再作成せず、既存成果を再利用する。"
        "外部送信・公開・破壊的変更は人間承認後に行い、実行後に検証証拠を記録する。"
    )
    if _ORDER.search(text):
        instruction = "操作順序を固定する: 検証、明示承認、外部送信または公開の順とし、承認前の外部操作を禁止する。"
    before = {
        "description": str(task.get("description") or ""),
        "depends_on": deepcopy(task.get("depends_on") or []),
        "contract": deepcopy(contract_of(task) or {}),
    }
    after = deepcopy(before)
    if instruction not in after["description"]:
        after["description"] = (after["description"].rstrip() + "\n\n局所修復候補: " + instruction).strip()
    normalized, ok, _reason = _evaluate_remedy(remedy)
    if ok:
        line = REMEDY_TAG + normalized
        if line not in after["description"]:
            after["description"] = (after["description"].rstrip() + "\n\n" + line).strip()
    return {"task_key": str(task.get("task_key") or ""), "before": before, "after": after}


def preview(manager, project_id: str, issues: list[dict] | None = None) -> dict:
    """Return a deterministic preview. This function performs no writes or application."""
    mission = manager.memory.get_mission(project_id)
    snapshot, signature = plan_snapshot(manager, project_id)
    contract = contract_preview(manager, project_id) or {}
    criteria = list(contract.get("criteria") or [])
    tasks = list(snapshot.get("tasks") or mission.get("tasks") or [])
    source = list(issues if issues is not None else issues_for(manager, project_id, signature))
    by_criterion = {str(x.get("criterion_id") or "").upper(): x for x in criteria}
    by_criterion.pop("", None)
    by_task = {str(x.get("task_key") or ""): x for x in tasks}
    by_task.pop("", None)
    confirmed = _confirmed_targets(
        manager, project_id, signature, str(contract.get("content_hash") or ""),
        source)
    store = _review_store(manager.memory.path)

    invalidations = []
    if store.get(project_id, "plan", signature):
        invalidations.append({"type": "plan_review", "signature": signature})
    if store.get(project_id, "send_approval", signature):
        invalidations.append({"type": "send_approval", "signature": signature})

    candidates = []
    questions = []
    touches_completed = False
    seen_ids: dict[str, int] = {}
    for issue in source:
        if isinstance(issue, dict):
            _raw = str(issue.get("id") or issue.get("issue_id") or "")
            if _raw:
                seen_ids[_raw] = seen_ids.get(_raw, 0) + 1
    for index, issue in enumerate(source, 1):
        iid = _stable_issue_id(issue, index)
        raw_id = str(issue.get("id") or issue.get("issue_id") or "") if isinstance(issue, dict) else ""
        ambiguous_id = bool(raw_id) and seen_ids.get(raw_id, 0) > 1
        text = normalize_issue_text(issue.get("text") or issue.get("reason") or "")
        criterion = _criterion(issue, criteria)
        task = _task_for(criterion, tasks)
        confirmed_entry = None if ambiguous_id else confirmed.get(iid)
        resolved = None if confirmed_entry is None else _confirmed_binding(
            confirmed_entry, by_criterion, by_task)
        if resolved is not None:
            criterion, task = resolved
            binding_source = "confirmed_snapshot"
            basis = ("人が確定した対応先へ結合: 達成条件=%s 工程=%s"
                     % (str(criterion.get("criterion_id") or ""),
                        str(task.get("task_key") or "")))
        elif bool(criterion and task):
            binding_source = "text_match"
            basis = text[:600] or "現行指摘を現行GoalContractへ再結合"
        else:
            binding_source = "unbound"
            basis = "現行の達成条件・工程に再結合できない: " + (text[:600] or "現行指摘を現行GoalContractへ再結合")
        bound = bool(criterion and task)
        kind = _classification(text, task) if bound else DEVELOPMENT_REQUIRED
        issue_remedy = issue.get("remedy") if isinstance(issue, dict) else ""
        normalized_remedy, remedy_ok, remedy_reason = _evaluate_remedy(issue_remedy)
        patch = (_patch(text, task, remedy=issue_remedy)
                 if bound and kind == SAFE_PLAN_PATCH else None)
        if patch is not None:
            if remedy_ok and normalized_remedy and REMEDY_TAG + normalized_remedy in str(
                    patch.get("after", {}).get("description") or ""):
                patch_basis = "issue_remedy"
                patch_basis_reason = ""
            else:
                patch_basis = "generic"
                patch_basis_reason = remedy_reason or "no_remedy"
        else:
            patch_basis = ""
            patch_basis_reason = ""
        if str(task.get("status") or "") in {"completed", "done", "succeeded"} and patch:
            touches_completed = True
        question = ""
        if kind == BUSINESS_FACT:
            question = f"{text[:160]}について、適用条件と判断根拠を回答してください。"
            questions.append({"issue_id": iid, "question": question})
        candidates.append({
            "issue_id": iid,
            "plan_signature": signature,
            "goal_id": str(contract.get("goal_id") or ""),
            "goal_contract_hash": str(contract.get("content_hash") or ""),
            "criterion_id": str(criterion.get("criterion_id") or ""),
            "task_key": str(task.get("task_key") or ""),
            "basis": basis,
            "classification": kind,
            "bound": bound,
            "binding_source": binding_source,
            "question": question,
            "patch": patch,
            "patch_basis": patch_basis,
            "patch_basis_reason": patch_basis_reason,
        })

    automatic_allowed = False  # P1 requirement: preview/classification only; human gate always required.
    reasons = ["P1では自動適用せず、既存の人間承認ゲートを必須とします"]
    if len(invalidations) >= 2:
        reasons.append("失効対象が2件以上です")
    if touches_completed:
        reasons.append("完了工程を変更する候補があります")
    review = store.get(project_id, "plan", signature) or {}
    externally_passed = review.get("status") == "passed"
    return {
        "project_id": project_id,
        "plan_signature": signature,
        "goal_id": str(contract.get("goal_id") or ""),
        "goal_contract_hash": str(contract.get("content_hash") or ""),
        "candidates": candidates,
        "questions": questions,
        "invalidated_approvals": invalidations,
        "touches_completed_task": touches_completed,
        "automatic_apply_allowed": automatic_allowed,
        "automatic_apply_blocked_reasons": reasons,
        "requires_human_approval": True,
        "requires_external_revalidation": True,
        "external_validation_signature": signature,
        "external_validation_passed": externally_passed,
        "execution_start_allowed": False,  # This preview cannot grant execution approval.
        "preserve_existing_artifacts": True,
        "recreate_published_assets": False,
        "saved": False,
    }
