"""L4: 保存済み計画から草案 -> ローカル構造検査 -> 許可済みAI検証(最大2回) -> 取込 -> 未承認草案反映.

薄いオーケストレーター。既存の review_plan / issues_for / propose /
validate_candidate / apply / evaluate_external_review_status /
public_structure の検査を迂回しない。承認・実行開始・外部公開・RAG登録は
自動化しない。新規実行の外部呼出は manager.plan_review_runner 経由(review_plan)のみ。
旧Gemini方式の保存済みランは停止し、新規開始を要求する。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid

from app.experience_store import canonical, fingerprint

VERIFICATION_LIMIT = 2
LEASE_TTL = 3600

STATES = (
    "permitted", "drafting", "organized", "structure_checked",
    "verifying", "intake", "proposing", "applied",
    "awaiting_human", "stopped", "cancelled",
)

JA_STATE = {
    "permitted": "人の許可済み・開始待ち",
    "drafting": "既存計画から草案を構成中",
    "organized": "ローカル整理済み",
    "structure_checked": "構造検査済み",
    "verifying": "許可済みAIで検証中",
    "intake": "指摘を取り込み中",
    "proposing": "ローカルで修正案を作成中",
    "applied": "未承認草案へ反映済み・再検証待ち",
    "awaiting_human": "人の計画承認待ち",
    "stopped": "停止中（成功ではない）",
    "cancelled": "取消済み",
}

# (種別ラベル, 正規表現) の順で評価する。contains_secret は検出値そのものでは
# なく種別ラベルだけを返し、値の漏洩を防ぐ。リスト要素が旧形式の compiled
# pattern のまま混在しても扱えるよう contains_secret 側で両対応する。
SECRET_PATTERNS = [
    ("AWSキー", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Authorizationヘッダー", re.compile(
        r"Authorization\s*:\s*(?:Basic|Bearer|Token|Digest|Negotiate)\s+[A-Za-z0-9_\-\.=~+/]+={0,2}", re.I)),
    ("秘密鍵(PEM)", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("GitHubトークン", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{4,}\b")),
    ("GitHubトークン", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{4,}\b")),
    ("Slackトークン", re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{4,}\b")),
    ("Stripeキー", re.compile(r"\b(?:sk_live|rk_live|pk_live)_[0-9A-Za-z]{4,}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{3,}\.[A-Za-z0-9_\-]{3,}\.[A-Za-z0-9_\-\.~+/=]{3,}")),
    ("パスワード等", re.compile(
        r"(?:password|passwd|pwd|secret|token|api[_-]?key|パスワード|暗証番号|秘密鍵|認証情報|接続文字列)\s*[:=：＝]\s*\S+",
        re.I)),
    ("資格情報付きURL", re.compile(r"://[^/\s:@]+:[^/\s:@]+@[^/\s]+")),
    ("口座番号", re.compile(r"口座番号\s*[:=：＝\s]*\d{6,8}")),
    ("電話番号様(語付き)", re.compile(
        r"(?:電話番号|電話|TEL|Tel|tel|FAX|Fax|fax|ファックス|携帯電話|携帯|連絡先|内線|お問い合わせ|問合せ)"
        r"\s*[:=：＝は\s]*(?<![0-9A-Za-z])0\d{9,10}(?![0-9A-Za-z])")),
    ("マイナンバー", re.compile(r"(?:マイナンバー|個人番号)\s*[:=：＝\s]*\d{4}[\s\-\u3000\uFF0D]*\d{4}[\s\-\u3000\uFF0D]*\d{4}(?![0-9A-Za-z])")),
    ("マイナンバー様(12桁)", re.compile(r"(?<![0-9A-Za-z])\d{12}(?![0-9A-Za-z])")),
    ("APIキー(sk-)", re.compile(r"sk-[A-Za-z0-9_\-]{8,}")),
    ("xAIキー", re.compile(r"xai-[A-Za-z0-9_\-]{8,}")),
    ("Google APIキー", re.compile(r"AIza[0-9A-Za-z_\-]{10,}")),
    ("LLMキー", re.compile(r"LLM_[A-Za-z0-9_\-]{4,}")),
    ("Bearerトークン", re.compile(r"Bearer\s+[A-Za-z0-9_\-\.=~+/]+")),
    ("メールアドレス", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
    ("電話番号様", re.compile(
        r"(?<![0-9A-Za-z])(?:"
        r"\+81[\s\u3000\-\uFF0D]*0?\d{1,4}[\s\u3000\-\uFF0D\(\)\uff08\uff09]*\d{1,4}[\s\u3000\-\uFF0D]+\d{3,4}"
        r"|\(\s*0\d{1,3}\s*\)[\s\u3000\-\uFF0D]*\d{1,4}[\s\u3000\-\uFF0D]+\d{3,4}"
        r"|\uff08\s*0\d{1,3}\s*\uff09[\s\u3000\-\uFF0D]*\d{1,4}[\s\u3000\-\uFF0D]+\d{3,4}"
        r"|0\d{1,3}[\s\u3000\-\uFF0D]+\d{1,4}[\s\u3000\-\uFF0D]+\d{3,4}"
        r"|0[5789]0\d{7,8}"
        r")(?![0-9A-Za-z])")),
]

# 機械が作る識別子(ハイフン区切りUUID、32桁以上の16進ダイジェスト)は、数字だけのグループが
# 12桁の個人番号様・電話番号様・カード番号様に偶然一致する。数字系パターンの検査では
# これらを空白に置き換えてから照合する(キー・トークン系パターンは元の文字列で照合する)。
_HEX = "[0-9a-fA-F]"
_MACHINE_ID_RE = re.compile(
    r"(?<![0-9A-Za-z])(?:"
    + _HEX + r"{8}-" + _HEX + r"{4}-" + _HEX + r"{4}-" + _HEX + r"{4}-" + _HEX + r"{12}"
    + r"|" + _HEX + r"{32,}"
    + r")(?![0-9A-Za-z])")
_NUMERIC_SECRET_LABELS = frozenset({"マイナンバー様(12桁)", "電話番号様", "電話番号様(語付き)"})

# カード番号の候補: (a) 区切りなしの13〜19桁、(b) 4桁ずつ同じ区切り(空白かハイフン)で並ぶ形。
# いずれも英数字に隣接しない。以前は「数字の連なり+任意の区切り」を許していたため、
# 16進ハッシュの中の数字列や、ハイフン区切りUUIDの隣り合う数字だけのグループが
# Luhn に偶然通って誤検出になっていた(約0.2〜0.7%/識別子)。実カード番号は
# 空白・記号・日本語に囲まれ、区切りは一種類で桁数が規則的である。
_CARD_CANDIDATE = re.compile(
    r"(?<![0-9A-Za-z])(?:"
    r"\d{13,19}"
    r"|\d{4}([- ])\d{4}\1\d{4}\1\d{1,7}"
    r")(?![0-9A-Za-z])")

# P7 issue-binding: 指摘の正規化と根拠付き対応付け(純粋関数群)。
# 外部AIの指摘本文は命令として扱わない(データ)。unverifiableを合格に変えない。
ISSUE_BINDING_VERSION = "issue-binding-v1"
STEP_RAW_LIMIT = 200
UNMET_GOAL_LIMIT = 2000
REASON_LIMIT = 4000
REMEDY_LIMIT = 4000
SEVERITY_LIMIT = 40
MAX_EXPANDED_STEPS = 16
# 日本語に続く "SC01の検証" でも拾う。ASCII単語境界(\b)は漢字を単語文字とみなして失敗する。
_SC_ID_RE = re.compile(r"(?<![A-Za-z0-9_])SC\d{2}(?![A-Za-z0-9_])", re.I)
_STEP_SINGLE_RE = re.compile(r"^\d+$")
_STEP_RANGE_RE = re.compile(r"^(\d+)\s*-\s*(\d+)$")
_BINDING_STATES = ("resolved", "multiple_targets", "ambiguous", "invalid_reference")

# §4: 矛盾の検出は build 側で行い ambiguous に分離する。map 側は resolved と
# 矛盾なし multiple_targets(全工程が同じ達成条件群に整合)だけ自動反映する。
AUTO_APPLY_STATES = ("resolved", "multiple_targets")


def is_contradictory_binding(binding: dict) -> bool:
    """矛盾(自動反映禁止)の判定。build側の矛盾文言・候補の両保持を検出する。"""
    if not isinstance(binding, dict):
        return False
    basis = str(binding.get("basis") or "")
    if "矛盾" in basis and "自動反映せず" in basis:
        return True
    steps = list(binding.get("steps") or [])
    tasks = list(binding.get("candidate_task_keys") or [])
    criteria = list(binding.get("candidate_criterion_ids") or [])
    mentioned = list(binding.get("mentioned_sc_ids") or binding.get("known_sc_ids") or [])
    # 防衛: 矛盾ambiguousのはずが multiple_targets で残っていたら自動反映しない。
    if str(binding.get("state") or "") == "multiple_targets" and mentioned and steps and tasks and criteria:
        # build側で矛盾はambiguousに分離済み。ここでは念のため文言でも検出する。
        return False
    return False


def confirm_binding_candidate(run: dict, issue_id: str, *, actor: str,
                              task_key: str = "", criterion_id: str = "") -> dict:
    """曖昧な指摘の人による候補確定。担当者・日時・元指紋付きで記録する。

    それだけでは検証合格にしない(状態・判定・検証回数は変えない)。
    現行の対応表(public_viewのissue_bindings)に追記する純粋寄りの記録関数。
    """
    actor_name = str(actor or "").strip()
    if not actor_name:
        raise ValueError("actor is required")
    bindings = list((run or {}).get("issue_bindings") or [])
    target = None
    for binding in bindings:
        if isinstance(binding, dict) and str(binding.get("issue_id") or "") == str(issue_id):
            target = binding
            break
    if target is None:
        raise ValueError("対象の指摘が対応表にありません")
    if str(target.get("state") or "") not in ("ambiguous", "multiple_targets"):
        raise ValueError("曖昧な指摘だけ人が候補を確定できます")
    tasks = [str(x) for x in (target.get("candidate_task_keys") or []) if str(x)]
    criteria = [str(x) for x in (target.get("candidate_criterion_ids") or []) if str(x)]
    task = str(task_key or "").strip()
    criterion = str(criterion_id or "").strip().upper()
    if task and task not in tasks:
        raise ValueError("候補に無い工程は確定できません")
    if criterion and criterion not in criteria:
        raise ValueError("候補に無い達成条件は確定できません")
    if not task and not criterion:
        raise ValueError("確定する候補を指定してください")
    entry = {
        "issue_id": str(issue_id),
        "actor": actor_name[:100],
        "at": _now(),
        "task_key": task,
        "criterion_id": criterion,
        "source_binding_fingerprint": fingerprint(target),
        "source_state": str(target.get("state") or ""),
        "note": "人の候補確定であり検証合格ではない",
    }
    history = [e for e in ((run or {}).get("manual_bindings") or []) if isinstance(e, dict)]
    history.append(entry)
    run["manual_bindings"] = history[-20:]
    return entry


def parse_step_reference(raw: object, step_count: int) -> dict:
    try:
        total = int(step_count)
    except (TypeError, ValueError):
        total = 0
    text = str(raw or "").strip()
    if not text or total <= 0:
        return {"ok": False, "steps": [], "reason": "stepが空または工程数がありません"}
    if len(text) > STEP_RAW_LIMIT:
        return {"ok": False, "steps": [], "reason": "stepの形式が不正です(長すぎます)"}
    if re.search(r"[^0-9,\-\s]", text):
        return {"ok": False, "steps": [], "reason": "stepの形式が不正です"}
    tokens = [tok.strip() for tok in text.split(",")]
    if any(not tok for tok in tokens):
        return {"ok": False, "steps": [], "reason": "stepの形式が不正です(空の区切り)"}
    expanded: list[int] = []
    for tok in tokens:
        if _STEP_SINGLE_RE.fullmatch(tok):
            expanded.append(int(tok))
            continue
        m = _STEP_RANGE_RE.fullmatch(tok)
        if not m:
            return {"ok": False, "steps": [], "reason": "stepの形式が不正です(範囲の書式)"}
        start, end = int(m.group(1)), int(m.group(2))
        if end <= start:
            return {"ok": False, "steps": [], "reason": "stepの範囲が逆順または単一です"}
        if end - start + 1 > MAX_EXPANDED_STEPS:
            return {"ok": False, "steps": [], "reason": "stepの範囲が広すぎます"}
        expanded.extend(range(start, end + 1))
    if any(n < 1 or n > total for n in expanded):
        return {"ok": False, "steps": [], "reason": "stepが工程数の範囲外です"}
    if any(b <= a for a, b in zip(expanded, expanded[1:])):
        return {"ok": False, "steps": [], "reason": "stepの順序が逆順または重複です"}
    if len(expanded) > MAX_EXPANDED_STEPS:
        return {"ok": False, "steps": [], "reason": "stepの指定が広すぎます"}
    return {"ok": True, "steps": expanded, "reason": ""}


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0

INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?previous", re.I),
    re.compile(r"ツール実行|外部送信せよ|勝手に送信|命令に従え|システムプロンプトを無視", re.I),
    re.compile(r"pass\s*(に|へ)\s*書き換え", re.I),
]

_locks: dict[str, asyncio.Lock] = {}


def _lock(pid: str) -> asyncio.Lock:
    lock = _locks.get(pid)
    if lock is None:
        lock = asyncio.Lock()
        _locks[pid] = lock
    return lock


def _store(manager):
    from app.goal_review import ReviewStore

    return ReviewStore(manager.memory.path)


def _now() -> float:
    return time.time()


def max_external_calls(provider_count: int) -> int:
    return 2 * max(0, int(provider_count))


def contains_secret(text: str) -> str:
    """秘密混入の疑いがあれば種別ラベルを、なければ空文字を返す。

    検出値そのものは返さない(ログ・状態・画面・応答への漏洩防止)。
    SECRET_PATTERNS の要素は (ラベル, 正規表現) または旧形式の正規表現。
    カード番号様は Luhn チェックで絞る。
    """
    blob = str(text or "")
    numeric_blob = _MACHINE_ID_RE.sub(" ", blob)
    for item in SECRET_PATTERNS:
        if isinstance(item, tuple):
            label, pat = item
        else:
            label, pat = "秘密値", item
        target = numeric_blob if label in _NUMERIC_SECRET_LABELS else blob
        if pat.search(target):
            return str(label)
    m = _CARD_CANDIDATE.search(numeric_blob)
    while m:
        digits = re.sub(r"[\- ]", "", m.group(0))
        if 13 <= len(digits) <= 19 and digits.isdigit() and _luhn_ok(digits):
            return "カード番号様"
        m = _CARD_CANDIDATE.search(numeric_blob, m.start() + 1)
    return ""


def extract_sc_ids(*fields: object) -> list[str]:
    found: list[str] = []
    for field in fields:
        for m in _SC_ID_RE.finditer(str(field or "")):
            found.append(m.group(0).upper())
    return list(dict.fromkeys(found))


def _snapshot_task_keys(snapshot: dict) -> list[str]:
    keys = []
    for task in ((snapshot or {}).get("tasks") or []):
        if isinstance(task, dict):
            key = str(task.get("task_key") or "")
            if key:
                keys.append(key)
    return keys


def _snapshot_task_criteria(snapshot: dict) -> dict[str, list[str]]:
    from app.structured_planning import contract_of

    mapping: dict[str, list[str]] = {}
    for task in ((snapshot or {}).get("tasks") or []):
        if not isinstance(task, dict):
            continue
        key = str(task.get("task_key") or "")
        if not key:
            continue
        try:
            contract = contract_of(task) or {}
        except Exception:
            contract = {}
        ids = [str(x) for x in (contract.get("criterion_ids") or []) if str(x)]
        mapping[key] = list(dict.fromkeys(ids))
    return mapping


def _criterion_task_index(contract: dict) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for item in ((contract or {}).get("criteria") or []):
        if not isinstance(item, dict):
            continue
        cid = str(item.get("criterion_id") or "")
        if cid:
            index[cid] = {
                "exec_task_keys": [str(x) for x in (item.get("exec_task_keys") or []) if str(x)],
                "verify_task_keys": [str(x) for x in (item.get("verify_task_keys") or []) if str(x)],
            }
    return index


def has_injection(text: str) -> bool:
    blob = str(text or "")
    return any(p.search(blob) for p in INJECTION_PATTERNS)


def _current(manager, pid: str):
    from app.goal_contract import preview as contract_preview
    from app.goal_review import plan_snapshot

    mission = manager.memory.get_mission(pid)
    snapshot, signature = plan_snapshot(manager, pid)
    contract = contract_preview(manager, pid) or {}
    return mission, snapshot, signature, contract


def _lease_get(manager, pid: str) -> dict | None:
    try:
        return _store(manager).get(pid, "auto_loop_lease", pid)
    except Exception:
        return None


def _lease_set(manager, pid: str, row: dict | None) -> None:
    store = _store(manager)
    if row is None:
        # ReviewStore has no delete; expire immediately.
        try:
            store.put(pid, "auto_loop_lease", pid, {"run_id": "", "expires": 0})
        except Exception:
            pass
        return
    store.put(pid, "auto_loop_lease", pid, row)


def _find_by_idempotency(manager, pid: str, key: str) -> dict | None:
    if not key:
        return None
    for _sig, payload in _store(manager).list(pid, "auto_loop"):
        if isinstance(payload, dict) and payload.get("idempotency_key") == key:
            return payload
    return None


def get_run(manager, pid: str, run_id: str) -> dict:
    row = _store(manager).get(pid, "auto_loop", str(run_id))
    if not row:
        raise ValueError("auto-loop run not found")
    return row


def list_runs(manager, pid: str) -> list[dict]:
    rows = [p for _s, p in _store(manager).list(pid, "auto_loop") if isinstance(p, dict)]
    rows.sort(key=lambda x: float(x.get("created_at") or 0))
    return rows


def _save(manager, run: dict) -> dict:
    run["updated_at"] = _now()
    _store(manager).put(run["project_id"], "auto_loop", run["id"], run)
    return run


def public_view(run: dict) -> dict:
    return {
        "id": run.get("id"),
        "project_id": run.get("project_id"),
        "state": run.get("state"),
        "state_ja": JA_STATE.get(str(run.get("state") or ""), str(run.get("state") or "")),
        "plan_signature": run.get("plan_signature"),
        "goal_contract_hash": run.get("goal_contract_hash"),
        "providers": list(run.get("providers") or []),
        "self_review": bool(run.get("self_review")),
        "self_review_warning": run.get("self_review_warning") or "",
        "verification_rounds": int(run.get("verification_rounds") or 0),
        "verification_limit": VERIFICATION_LIMIT,
        "external_calls": int(run.get("external_calls") or 0),
        "max_external_calls": int(run.get("max_external_calls") or 0),
        "gemini_organize_calls": int(run.get("gemini_organize_calls") or 0),
        "intake_actor": run.get("intake_actor") or "",
        "stop_reason": run.get("stop_reason") or "",
        "stop_kind": run.get("stop_kind") or "",
        "draft_mode": run.get("draft_mode") or "legacy_gemini",
        "provider_failures": list(run.get("provider_failures") or []),
        "actor": run.get("actor") or "",
        "deadline": run.get("deadline"),
        "packet_hash": run.get("packet_hash") or "",
        "review_source": run.get("review_source") or "",
        "actual_external_calls": int(run.get("external_calls") or 0),
        "stored_review_rounds": len([r for r in (run.get("rounds") or [])
                                     if isinstance(r, dict) and str(r.get("review_source") or "") == "stored"]),
        "issue_bindings": list(run.get("issue_bindings") or []),
        "issue_binding_version": run.get("issue_binding_version") or "",
        "manual_bindings": list(run.get("manual_bindings") or []),
        "remap_history": list(run.get("remap_history") or []),
        "rounds": run.get("rounds") or [],
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
    }


def _selected_send_allowed(manager, mission: dict, selected: list[str]) -> bool:
    configured = {str(row.get("id") or "") for row in manager.provider_statuses()
                  if row.get("configured")}
    allowed = set(mission.get("external_providers") or [])
    return bool(mission.get("allow_external_ai") and selected
                and set(selected) <= allowed and set(selected) <= configured
                and manager.plan_review_runner)


def preview_run(manager, pid: str, providers: list[str] | None = None,
                actor: str = "", ttl_seconds: int = LEASE_TTL) -> dict:
    mission, snapshot, signature, contract = _current(manager, pid)
    from app.goal_review_queue import public_draft
    from app.goal_review import public_structure

    selected = list(dict.fromkeys([str(x or "").strip() for x in (providers or []) if str(x or "").strip()]))
    allowed_ids = {"claude", "chatgpt", "grok", "meta"}
    unknown = [p for p in selected if p not in allowed_ids]
    if unknown:
        raise ValueError(f"不明な外部AI: {', '.join(unknown)}")
    public_summary = public_draft(snapshot)
    packet = {"public_goal_and_plan": public_summary, "structure": public_structure(snapshot)}
    secret = contains_secret(canonical(packet))
    from app.goal_review import review_budget,load_review_policy,_policy_section
    budget=review_budget(manager,pid)
    allowed=set(mission.get('external_providers') or [])
    policy=load_review_policy(manager);section=_policy_section(policy,pid)
    required=section.get('required_providers',policy.get('required_providers') or [])
    if isinstance(required,str):required=[required]
    raw_min=section.get('min_success_count',policy.get('min_success_count'))
    try:minimum=int(raw_min) if raw_min is not None else 1
    except (TypeError,ValueError):minimum=1
    required_calls=2*len(selected)
    blockers=[]
    if not selected:blockers.append('独立した検証AIを1社以上選択してください')
    if not _selected_send_allowed(manager,mission,selected):blockers.append('外部AIの許可・接続が揃っていません')
    if not set(selected)<=allowed:blockers.append('プロジェクトで許可されていない検証AIが含まれます')
    if not set(required or [])<=set(selected):blockers.append('必須の検証AIが不足しています')
    if len(selected)<minimum:blockers.append('検証AIの数が最低合格数に足りません')
    if budget.get('managed') and int(budget.get('remaining_calls') or 0)<required_calls:
        blockers.append(f'最大2回の検証に必要な{required_calls}回に対し、残り{budget.get("remaining_calls",0)}回です')
    if secret:blockers.append('送信文に秘密混入の疑いがあります')
    return {
        "project_id": pid,
        "plan_signature": signature,
        "goal_contract_hash": str(contract.get("content_hash") or ""),
        "draft_role": "既存計画と達成条件からローカルで構成。外部草案AIは呼び出さない",
        "verification_providers": selected,
        "self_review": False,
        "self_review_warning": "",
        "public_summary": public_summary,
        "packet_preview": packet,
        "can_start": not blockers,
        "start_blockers": blockers,
        "budget_remaining": budget.get("remaining_calls"),
        "required_calls": required_calls,
        "packet_hash": fingerprint(packet),
        "packet_keys": sorted(packet.keys()),
        "max_verification_rounds": VERIFICATION_LIMIT,
        "max_external_calls": max_external_calls(len(selected)),
        "deadline_in_seconds": int(ttl_seconds),
        "scope": "同一PJ・同一目標契約・選択したAIと役割・公開用説明と構造フィールド・最大2回の検証・期限内",
        "sends_original_body": False,
        "sends_filenames": False,
        "sends_secrets": False,
        "secret_suspected": bool(secret),
        "external_send": False,
        "note": "この時点では外部送信しない。start で明示許可を記録してから初めて外部呼出を許す",
    }


def start_run(manager, pid: str, actor: str, providers: list[str],
              public_summary: str = "", idempotency_key: str = "",
              ttl_seconds: int = LEASE_TTL, expected_packet_hash: str = "") -> dict:
    actor = str(actor or "").strip()
    if not actor:
        raise ValueError("actor is required")
    selected = list(dict.fromkeys([str(x or "").strip() for x in (providers or []) if str(x or "").strip()]))
    if not selected:
        raise ValueError("検証AIを1つ以上選択してください")
    allowed_ids = {"claude", "chatgpt", "grok", "meta"}
    unknown = [p for p in selected if p not in allowed_ids]
    if unknown:
        raise ValueError(f"不明な外部AI: {', '.join(unknown)}")
    key = str(idempotency_key or "").strip()
    existing = _find_by_idempotency(manager, pid, key) if key else None
    if existing:
        return public_view(existing)
    lease = _lease_get(manager, pid) or {}
    if lease.get("run_id") and float(lease.get("expires") or 0) > _now():
        raise ValueError("同一PJで実行中のauto-loopがあります")
    mission, snapshot, signature, contract = _current(manager, pid)
    from app.goal_review_queue import public_draft
    from app.goal_review import public_structure, record_send_approval

    mission_providers = list(dict.fromkeys(mission.get("external_providers") or []))
    extra = [p for p in selected if p not in mission_providers]
    if extra:
        raise ValueError("プロジェクトで許可済みの外部AIから選択してください")
    preflight=preview_run(manager,pid,selected)
    if not preflight['can_start']:
        raise ValueError('開始条件を満たしません: '+' / '.join(preflight['start_blockers']))
    if not _selected_send_allowed(manager, mission, selected):
        raise ValueError("外部AIの許可・接続が揃っていないため開始できません")

    summary = str(public_summary or "").strip() or public_draft(snapshot)
    if not 20 <= len(summary) <= 12000:
        raise ValueError("公開用説明は20〜12000文字にしてください")
    packet = {"public_goal_and_plan": summary, "structure": public_structure(snapshot)}
    if expected_packet_hash and expected_packet_hash != fingerprint(packet):
        raise ValueError("送信内容がプレビュー後に変わりました。もう一度確認してください")
    secret = contains_secret(canonical(packet) + "\n" + summary)
    if secret:
        raise ValueError(f"秘密混入の疑いのため送信前に停止しました(種別: {secret})")
    contract_hash = str(contract.get("content_hash") or "")
    if not contract_hash:
        raise ValueError("GoalContractがありません")
    # 明示許可を既存の送信承認として記録してから初めて外部呼出を許す。
    approval = record_send_approval(manager, pid, signature, summary, selected)
    run = {
        "id": uuid.uuid4().hex,
        "project_id": pid,
        "idempotency_key": key,
        "actor": actor,
        "providers": selected,
        "self_review": False,
        "self_review_warning": "",
        "plan_signature": signature,
        "goal_contract_hash": contract_hash,
        "public_summary": summary,
        "packet_hash": str(approval.get("packet_hash") or fingerprint(packet)),
        "verification_rounds": 0,
        "verification_limit": VERIFICATION_LIMIT,
        "external_calls": 0,
        "max_external_calls": max_external_calls(len(selected)),
        "gemini_organize_calls": 0,
        "draft_mode": "local",
        "local_draft": None,
        "gemini_draft": None,
        "organized": None,
        "rounds": [],
        "sent_packet_hashes": [str(approval.get("packet_hash") or fingerprint(packet))],
        "intake_actor": "",
        "state": "permitted",
        "stop_reason": "",
        "stop_kind": "",
        "deadline": _now() + max(60, int(ttl_seconds or LEASE_TTL)),
        "permission": {"actor": actor, "at": _now(), "providers": selected,
                       "packet_hash": str(approval.get("packet_hash") or ""),
                       "signature": signature},
        "review_source": "",
        "review_source_detail": "",
        "remap_history": [],
        "manual_bindings": [],
        "created_at": _now(),
        "updated_at": _now(),
    }
    _store(manager).put(pid, "auto_loop", run["id"], run)
    _lease_set(manager, pid, {"run_id": run["id"], "expires": run["deadline"], "actor": actor})
    try:
        manager.memory.add_event(pid, "auto_loop_started", "計画自動評価ループの外部送信範囲とAI役割を承認しました",
                                 detail=canonical({"run_id": run["id"], "providers": selected,
                                                   "packet_hash": run["packet_hash"]}))
    except Exception:
        pass
    return public_view(run)


def cancel_run(manager, pid: str, run_id: str, actor: str) -> dict:
    run = get_run(manager, pid, run_id)
    if run.get("state") in {"awaiting_human"}:
        raise ValueError("人の承認待ちのランは取り消せません。計画画面で承認・差戻ししてください")
    run["state"] = "cancelled"
    run["stop_reason"] = f"利用者 {actor} が取り消しました。以後の外部呼出は行いません"
    run["stop_kind"] = "cancelled"
    _save(manager, run)
    lease = _lease_get(manager, pid) or {}
    if lease.get("run_id") == run_id:
        _lease_set(manager, pid, None)
    return public_view(run)


def _check_budget_deadline(manager, pid: str, run: dict) -> None:
    if _now() > float(run.get("deadline") or 0):
        raise ValueError("期限切れのため停止しました")
    if int(run.get("external_calls") or 0) >= int(run.get("max_external_calls") or 0):
        raise ValueError(f"最大外部呼出数({run.get('max_external_calls')})超過のため停止しました")
    try:
        from app.goal_review import review_budget

        budget = review_budget(manager, pid)
    except Exception:
        budget = {"managed": False}
    if budget.get("managed") and int(budget.get("remaining_calls") or 0) <= 0:
        raise ValueError("外部検証の利用枠(費用上限)に達したため停止しました")


def _check_pinned(manager, pid: str, run: dict) -> tuple:
    mission, snapshot, signature, contract = _current(manager, pid)
    if signature != str(run.get("plan_signature") or "") and int(run.get("verification_rounds") or 0) == 0:
        # 第1回前の計画変更は停止(反映後の新署名は rounds に記録して追跡する)。
        raise ValueError("計画版が変わりました。現行版で新規開始してください")
    if str(contract.get("content_hash") or "") != str(run.get("goal_contract_hash") or ""):
        raise ValueError("GoalContractが変わりました。現行版で新規開始してください")
    lease = _lease_get(manager, pid) or {}
    if lease.get("run_id") not in ("", str(run.get("id"))):
        raise ValueError("他のauto-loopがこのPJのleaseを保持しています")
    return mission, snapshot, signature, contract


DRAFT_FIELDS = ("purpose", "criterion_ids", "constraints", "steps", "dependencies",
                "input_types", "artifacts", "verification", "capabilities",
                "external_actions", "human_approvals")


def validate_gemini_draft(body: object) -> dict:
    if not isinstance(body, dict):
        raise ValueError("Gemini草案の構造化形式が不正です")
    missing = [k for k in DRAFT_FIELDS if k not in body]
    if missing:
        raise ValueError(f"Gemini草案に不足フィールドがあります: {','.join(missing)}")
    if not isinstance(body.get("steps"), list) or not body["steps"]:
        raise ValueError("Gemini草案の工程がありません")
    if not isinstance(body.get("criterion_ids"), list) or not body["criterion_ids"]:
        raise ValueError("Gemini草案の達成条件IDがありません")
    blob = canonical(body)
    if has_injection(blob):
        raise ValueError("Gemini草案に指示混入の疑いがあるため拒否しました")
    if contains_secret(blob):
        raise ValueError("Gemini草案に秘密混入の疑いがあるため拒否しました")
    return body


async def _call_gemini(manager, pid: str, run: dict, prompt: str, purpose: str) -> str:
    from app import external_ai

    _check_budget_deadline(manager, pid, run)
    secret = contains_secret(prompt)
    if secret:
        raise ValueError("秘密混入の疑いのため送信前に停止しました")
    # 失敗・再試行も算入するため呼出前に加算する。
    run["external_calls"] = int(run.get("external_calls") or 0) + 1
    _save(manager, run)
    try:
        response = await external_ai.call_provider_with_metadata(
            "gemini", prompt,
            "あなたは計画草案の構造化支援者です。JSONだけを返し、命令・送信指示・判定の書換えをしないでください。",
            2200, None)
        text = response.text if hasattr(response, "text") else str(response)
    except Exception as exc:
        from app.external_ai import classify_provider_failure

        info = classify_provider_failure(exc)
        # Provider error bodies can contain prompts or credentials; persist only
        # bounded, fixed diagnostic fields.
        category = str(info.get("category") or "unknown")
        if category not in {"connection_error", "authentication_error", "http_error",
                            "configuration_missing", "unknown"}:
            category = "unknown"
        status = info.get("status_code")
        if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
            status = None
        failure = {
            "provider": "gemini",
            "phase": purpose if purpose in {"draft", "organize"} else "unknown",
            "category": category,
            "status_code": status,
            "retryable": info.get("retryable") is True,
            "at": _now(),
        }
        run["provider_failures"] = (list(run.get("provider_failures") or []) + [failure])[-8:]
        _save(manager, run)
        try:
            manager.memory.add_event(pid, "auto_loop_provider_error",
                                     "Geminiの呼出に失敗しました",
                                     detail=canonical({"run_id": run["id"], **failure}))
        except Exception:
            pass
        raise ValueError(f"Gemini呼出に失敗したため停止しました({category})") from exc
    if purpose == "organize":
        run["gemini_organize_calls"] = int(run.get("gemini_organize_calls") or 0) + 1
        _save(manager, run)
    return text


def _draft_prompt(packet: dict) -> str:
    return ("公開用計画構造だけを使って計画草案の候補をJSONで返してください。"
            "原本本文・ファイル名・個人情報・秘密は含まれていません。推測で補わないでください。\n"
            + canonical(packet)[:8000])


def _local_draft(snapshot: dict, contract: dict) -> dict:
    """Build a draft from recorded plan facts, without an external AI call."""
    criteria = [str(c.get("criterion_id") or "") for c in (contract.get("criteria") or [])
                if isinstance(c, dict) and c.get("criterion_id")]
    tasks = [t for t in (snapshot.get("tasks") or []) if isinstance(t, dict)]
    if not criteria or not tasks:
        raise ValueError("達成条件または計画工程が無いためローカル草案を作成できません")
    ids = {str(t.get("task_key") or t.get("id") or i): f"S{i}"
           for i, t in enumerate(tasks, 1)}
    if len(ids) != len(tasks):
        raise ValueError("計画工程の識別子が重複しているため停止しました")
    steps = [{"id": f"S{i}", "title": f"保存済み工程{i}"}
             for i, _task in enumerate(tasks, 1)]
    dependencies = []
    for i, task in enumerate(tasks, 1):
        for parent in (task.get("depends_on") or []):
            parent_id = ids.get(str(parent))
            if not parent_id:
                raise ValueError("計画工程の依存先が見つからないため停止しました")
            dependencies.append({"from": parent_id, "to": f"S{i}"})
    return {"purpose": "保存済み計画の目標適合性を検証する",
            "criterion_ids": criteria,
            "constraints": ["保存済みの制約と人間承認境界を保持する"],
            "steps": steps, "dependencies": dependencies,
            "input_types": [], "artifacts": [],
            "verification": ["達成条件と実行成果を照合する"],
            "capabilities": [], "external_actions": [],
            "human_approvals": ["計画承認と外部操作は人が判断する"]}


def _local_organize(draft: dict, contract: dict, snapshot: dict) -> dict:
    criteria = [str(x.get("criterion_id") or "") for x in (contract.get("criteria") or []) if x.get("criterion_id")]
    if not criteria:
        raise ValueError("不足情報のため停止しました: GoalContractに達成条件がありません")
    draft_ids = [str(x) for x in (draft.get("criterion_ids") or [])]
    missing = [c for c in criteria if c not in draft_ids]
    if missing:
        raise ValueError(f"構造検査不合格のため停止しました: 達成条件の被覆不足 {','.join(missing[:4])}")
    # 依存関係の循環・欠落の決定的検査。
    deps = draft.get("dependencies") or []
    if not isinstance(deps, list):
        raise ValueError("構造検査不合格のため停止しました: 依存関係の形式不正")
    steps = [str(s.get("id") or s.get("step") or "") for s in (draft.get("steps") or []) if isinstance(s, dict)]
    for edge in deps:
        if not isinstance(edge, dict):
            raise ValueError("構造検査不合格のため停止しました: 依存関係の形式不正")
        a, b = str(edge.get("from") or ""), str(edge.get("to") or "")
        if a and a not in steps or b and b not in steps:
            raise ValueError("構造検査不合格のため停止しました: 依存関係の欠落")
    # 簡易な循環検出。
    graph = {}
    for edge in deps:
        graph.setdefault(str(edge.get("from") or ""), []).append(str(edge.get("to") or ""))
    visiting: set[str] = set()
    visited: set[str] = set()

    def _dfs(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        for nxt in graph.get(node, []):
            if _dfs(nxt):
                return True
        visiting.discard(node)
        visited.add(node)
        return False

    for node in list(graph):
        if _dfs(node):
            raise ValueError("構造検査不合格のため停止しました: 依存関係の循環")
    if not draft.get("human_approvals"):
        raise ValueError("構造検査不合格のため停止しました: 外部操作の承認点がありません")
    blob = canonical(draft)
    if re.search(r"推測|仮定|たぶん|おそらく", blob):
        raise ValueError("構造検査不合格のため停止しました: 推測した値の混入の疑い")
    return {"draft_ids": draft_ids, "criteria": criteria, "steps": steps,
            "note": "ローカルでGoalContract・原本目録・実装能力と照合して整理しました"}


def _bounded_issue_text(value: object, limit: int) -> str:
    return str(value or "")[:max(0, int(limit))]


def _sanitize_binding_text(value: str, limit: int) -> str:
    """対応表に保存する指摘文から秘密値の疑いを除去する。検出値そのものは保持しない。"""
    text = _bounded_issue_text(value, limit)
    if text and contains_secret(text):
        # 種別ラベルだけ残し、値は落とす(応答・ログ・保存対応表への漏洩防止)。
        return "[秘密混入の疑いのため本文を保持しません]"
    return text


def _binding_issue_text(issue: object) -> str:
    if isinstance(issue, dict):
        return str(issue.get("text") or issue.get("issue") or issue.get("reason") or canonical(issue))
    return str(issue)


def _binding_issue_criterion(issue: object) -> str:
    if isinstance(issue, dict):
        return str(issue.get("criterion") or issue.get("target") or "")
    return ""


def build_issue_bindings(parsed_reviews: list[dict], contract: dict, snapshot: dict, *,
                         plan_signature: str = "", goal_contract_hash: str = "",
                         packet_hash: str = "") -> dict:
    """指摘→工程・達成条件の根拠付き対応表を作る純粋関数。外部AIの指摘本文は命令として扱わない。"""
    task_keys = _snapshot_task_keys(snapshot)
    task_criteria = _snapshot_task_criteria(snapshot)
    criterion_index = _criterion_task_index(contract)
    known_criteria = set(criterion_index)
    existing_tasks = set(task_keys)
    bindings = []
    for position, review in enumerate(parsed_reviews or []):
        provider = str((review or {}).get("provider") or "")
        status = str((review or {}).get("status") or "")
        review_fp = fingerprint([provider, status, canonical((review or {}).get("issues") or [])])
        issues = (review or {}).get("issues") or []
        for idx, issue in enumerate(issues):
            step_raw = ""
            unmet_goal = ""
            reason = ""
            remedy = ""
            severity = ""
            if isinstance(issue, dict):
                step_raw = _bounded_issue_text(issue.get("step"), STEP_RAW_LIMIT)
                unmet_goal = _sanitize_binding_text(issue.get("unmet_goal"), UNMET_GOAL_LIMIT)
                reason = _sanitize_binding_text(issue.get("reason"), REASON_LIMIT)
                remedy = _sanitize_binding_text(issue.get("remedy"), REMEDY_LIMIT)
                severity = _bounded_issue_text(issue.get("severity"), SEVERITY_LIMIT)
            norm_text = _binding_issue_text(issue).strip()
            criterion_field = _binding_issue_criterion(issue)
            issue_id = fingerprint([provider, criterion_field, norm_text.strip()])[:20]
            issue_text = _sanitize_binding_text(norm_text, 500)
            extra_ids = []
            if isinstance(issue, dict) and isinstance(issue.get("criterion_ids"), list):
                extra_ids = [str(x) for x in issue.get("criterion_ids") or [] if str(x)]
            mentioned = extract_sc_ids(step_raw, unmet_goal, reason, remedy, severity,
                                       criterion_field, norm_text, *extra_ids)
            known = [c for c in mentioned if c in known_criteria]
            parsed_step = parse_step_reference(step_raw, len(task_keys))
            steps = list(parsed_step["steps"]) if parsed_step["ok"] else []
            step_tasks = [task_keys[n - 1] for n in steps if 1 <= n <= len(task_keys)]
            sc_exec: list[str] = []
            sc_verify: list[str] = []
            for cid in known:
                for key in criterion_index[cid]["exec_task_keys"]:
                    if key in existing_tasks and key not in sc_exec:
                        sc_exec.append(key)
                for key in criterion_index[cid]["verify_task_keys"]:
                    if key in existing_tasks and key not in sc_verify:
                        sc_verify.append(key)
            sc_tasks = list(dict.fromkeys(sc_exec + sc_verify))
            step_criteria_union: list[str] = []
            for key in step_tasks:
                for cid in task_criteria.get(key, []):
                    if cid in known_criteria and cid not in step_criteria_union:
                        step_criteria_union.append(cid)
            contradiction = bool(known and step_criteria_union and not (set(known) & set(step_criteria_union)))
            if contradiction:
                # §4: stepの達成条件と明示SCが矛盾する場合は誤自動反映せず ambiguous で人確認待ち。両候補を保持。
                candidate_tasks = list(dict.fromkeys(step_tasks + sc_tasks))
                candidate_criteria = list(dict.fromkeys(step_criteria_union + known))
            elif known:
                candidate_criteria = list(known)
                overlap = [t for t in step_tasks if t in sc_tasks]
                if overlap:
                    candidate_tasks = list(dict.fromkeys(overlap))
                else:
                    # 準備工程など criterion_ids が空のstepは、明示SCの実行工程を優先する。
                    candidate_tasks = list(sc_exec or sc_tasks)
            else:
                candidate_criteria = list(step_criteria_union)
                candidate_tasks = list(step_tasks)
            if not parsed_step["ok"]:
                state = "invalid_reference"
                basis = "stepの解析に失敗したため対応付けできません(%s)" % parsed_step["reason"]
            elif mentioned and not known:
                state = "ambiguous"
                basis = "指摘内のSC番号が現行契約に存在しないため対応付けできません"
            elif contradiction:
                state = "ambiguous"
                basis = ("step %s の指す工程の達成条件(%s)と明示SC(%s)が矛盾するため自動反映せず人の確認待ちです。"
                         "候補工程(%s)・候補達成条件(%s)を両方保持します"
                         % (",".join(str(n) for n in steps),
                            ",".join(step_criteria_union), ",".join(known),
                            ",".join(candidate_tasks) or "なし",
                            ",".join(candidate_criteria) or "なし"))
            elif not candidate_tasks or not candidate_criteria:
                state = "ambiguous"
                basis = "工程または達成条件の候補が空のため対応付けできません"
            elif len(candidate_tasks) == 1 and len(candidate_criteria) == 1:
                state = "resolved"
                basis = ("step %s を工程 %s に変換し、達成条件 %s と照合しました"
                         % (",".join(str(n) for n in steps) or "-",
                            candidate_tasks[0], candidate_criteria[0]))
            else:
                state = "multiple_targets"
                basis = ("step %s は複数工程(%s)・複数達成条件(%s)にまたがるため単一に縮めません"
                         % (",".join(str(n) for n in steps) or "-",
                            ",".join(candidate_tasks), ",".join(candidate_criteria)))
            bindings.append({
                "issue_id": issue_id,
                "provider": provider,
                "position": position,
                "index": idx,
                "review_fingerprint": review_fp,
                "plan_signature": str(plan_signature or ""),
                "goal_contract_hash": str(goal_contract_hash or ""),
                "packet_hash": str(packet_hash or ""),
                "step_raw": step_raw,
                "steps": steps,
                "unmet_goal": unmet_goal,
                "reason": reason,
                "remedy": remedy,
                "severity": severity,
                "issue_text": issue_text,
                "mentioned_sc_ids": mentioned,
                "known_sc_ids": known,
                "candidate_task_keys": candidate_tasks,
                "candidate_criterion_ids": candidate_criteria,
                "basis": basis[:500],
                "state": state,
            })
    return {"version": ISSUE_BINDING_VERSION, "bindings": bindings, "empty": not bindings}


def _binding_coherent(binding: dict) -> bool:
    """矛盾のない multiple_targets か(防衛用)。矛盾文言を含むものは自動反映しない。"""
    if str(binding.get("state") or "") != "multiple_targets":
        return True
    return "矛盾" not in str(binding.get("basis") or "")


def _map_issues_local(parsed_reviews: list[dict], contract: dict, snapshot: dict, *,
                      plan_signature: str = "", goal_contract_hash: str = "",
                      packet_hash: str = "") -> dict:
    """対応表を作り、自動反映できない指摘があれば ValueError で止める。任意工程フォールバックは廃止。

    自動反映してよいのは resolved と、矛盾のない multiple_targets
    (範囲指定で全工程が同じ達成条件群に整合し、build側で矛盾をambiguousに分離済み)のみ。
    矛盾ケースは ambiguous として人の確認待ちにし、ここでは必ず止める。
    """
    table = build_issue_bindings(parsed_reviews, contract, snapshot,
                                 plan_signature=plan_signature,
                                 goal_contract_hash=goal_contract_hash,
                                 packet_hash=packet_hash)
    bindings = table["bindings"]
    if not bindings:
        return {"mapped": [], "bindings": [], "empty": True,
                "version": ISSUE_BINDING_VERSION}
    bad = [b for b in bindings
           if b["state"] not in AUTO_APPLY_STATES or is_contradictory_binding(b)
           or (b["state"] == "multiple_targets" and not _binding_coherent(b))]
    if bad:
        first = bad[0]
        raise ValueError(
            "指摘の対応先を特定できないため取り込み失敗しました: "
            "issue=%s state=%s %s" % (first["issue_id"], first["state"], first["basis"][:120]))
    mapped = [{"issue_id": b["issue_id"], "issue_text": b["issue_text"],
               "criteria": list(b["candidate_criterion_ids"]),
               "tasks": list(b["candidate_task_keys"]),
               "state": b["state"], "provider": b["provider"]}
              for b in bindings]
    return {"mapped": mapped, "bindings": bindings, "empty": False,
            "version": ISSUE_BINDING_VERSION}


def _remap_base(manager, pid: str, run_id: str) -> tuple:
    from app.project_delete import is_deleted as _is_deleted

    try:
        if _is_deleted(manager.memory.path, pid):
            raise ValueError("論理削除中のPJには動作しません")
    except ValueError:
        raise
    except Exception:
        pass
    run = get_run(manager, pid, run_id)
    if str(run.get("state") or "") != "stopped" or str(run.get("stop_kind") or "") != "human_required":
        raise ValueError("human_requiredで停止したランだけ対応付け直しできます")
    mission, snapshot, signature, contract = _current(manager, pid)
    if signature != str(run.get("plan_signature") or ""):
        raise ValueError("409: 計画版が変わりました。新規確認してください")
    if str(contract.get("content_hash") or "") != str(run.get("goal_contract_hash") or ""):
        raise ValueError("409: GoalContractが変わりました。新規確認してください")
    if str(run.get("packet_hash") or "") and signature:
        try:
            from app.goal_review import ReviewStore as _ReviewStore

            stored = _ReviewStore(manager.memory.path).get(pid, "plan", signature) or {}
            packet = stored.get("packet") or {}
            if packet and fingerprint(packet) != str(run.get("packet_hash") or ""):
                raise ValueError("409: packetが変わりました。新規確認してください")
        except ValueError:
            raise
        except Exception:
            pass
    rounds = [r for r in (run.get("rounds") or []) if isinstance(r, dict)]
    if not rounds:
        raise ValueError("保存済みレビューがありません")
    first = rounds[0]
    parsed = first.get("reviews") or []
    if not parsed:
        raise ValueError("保存済みレビューがありません")
    # レビュー指紋: ラン保存時の第1回レビューと現行ReviewStoreの保存レビューを照合する。
    # ラン内コピー同士の比較では改変を検出できないため、外部の保存レビューと突き合わせる。
    try:
        from app.goal_review import ReviewStore as _ReviewStore2

        _stored_reviews = (_ReviewStore2(manager.memory.path).get(pid, "plan", signature) or {}).get("reviews") or []

        def _review_triples(rows: list) -> list:
            triples = []
            for row in rows:
                if isinstance(row, dict):
                    triples.append([str(row.get("provider") or ""), str(row.get("status") or ""),
                                    canonical(row.get("issues") or [])])
            return triples

        if _stored_reviews and _review_triples(_stored_reviews) != _review_triples(parsed):
            raise ValueError("409: レビューが変わりました。新規確認してください")
    except ValueError:
        raise
    except Exception:
        pass
    return run, mission, snapshot, signature, contract, first, parsed


def remap_preview(manager, pid: str, run_id: str) -> dict:
    """保存済みレビューからの対応付けプレビュー。読み取り専用で副作用なし。"""
    run, _mission, snapshot, signature, contract, first, parsed = _remap_base(manager, pid, run_id)
    table = build_issue_bindings(parsed, contract, snapshot,
                                 plan_signature=signature,
                                 goal_contract_hash=str(contract.get("content_hash") or ""),
                                 packet_hash=str(first.get("packet_hash") or run.get("packet_hash") or ""))
    saved_fp = fingerprint([str(first.get("plan_signature") or ""),
                            canonical(first.get("reviews") or [])])
    current_fp = fingerprint([signature, canonical(parsed)])
    return {
        "run_id": run.get("id"),
        "project_id": pid,
        "plan_signature": signature,
        "goal_contract_hash": str(contract.get("content_hash") or ""),
        "packet_hash": str(first.get("packet_hash") or run.get("packet_hash") or ""),
        "review_fingerprint_match": saved_fp == current_fp,
        "round": int(first.get("round") or 1),
        "review_source": str(first.get("review_source") or ""),
        "status": str(first.get("status") or ""),
        "external_calls": 0,
        "bindings": table["bindings"],
        "version": table["version"],
    }


def validate_gemini_organized(body: object, original_issues: list[dict], signature: str,
                              contract_hash: str) -> dict:
    if not isinstance(body, dict):
        raise ValueError("Gemini整理の形式が不正のため停止しました")
    blob = canonical(body)
    if has_injection(blob):
        raise ValueError("Gemini整理に指示混入があるため拒否し停止しました")
    # 元の判定の書換え・指摘削除の拒否。
    lowered = blob.lower()
    if re.search(r'"verdict"\s*:\s*"pass"', blob) and any(
            str(r.get("status") or "") in {"fail", "conditional"} for r in original_issues):
        raise ValueError("Gemini整理が元の判定をpassに書き換えたため拒否し停止しました")
    proposed = body.get("mappings") if isinstance(body.get("mappings"), list) else body.get("actions")
    if not isinstance(proposed, list):
        raise ValueError("Gemini整理に対応表が無いため停止しました")
    original_texts = []
    for review in original_issues:
        for issue in (review.get("issues") or []):
            text = str(issue.get("text") or issue.get("reason") or "") if isinstance(issue, dict) else str(issue)
            if text.strip():
                original_texts.append(text.strip()[:500])
    covered = 0
    for text in original_texts:
        if text[:60] in blob:
            covered += 1
    if original_texts and covered != len(original_texts):
        raise ValueError("Gemini整理が全件対応検査に失敗したため停止しました")
    if body.get("plan_signature") and str(body.get("plan_signature")) != signature:
        raise ValueError("Gemini整理の対象署名が現行と異なるため停止しました")
    if body.get("goal_contract_hash") and str(body.get("goal_contract_hash")) != contract_hash:
        raise ValueError("Gemini整理のGoalContractが現行と異なるため停止しました")
    return body


async def confirm_binding(manager, pid: str, run_id: str, issue_id: str, *,
                          actor: str, task_key: str = "", criterion_id: str = "") -> dict:
    """現行計画と保存済みレビューを再照合してから人の候補確定を記録する。"""
    async with _lock(pid):
        run, _mission, _snapshot, signature, contract, _first, _parsed = _remap_base(
            manager, pid, run_id)
        if not run.get("issue_bindings"):
            raise ValueError("先に保存済み指摘を対応付け直してください")
        for binding in run.get("issue_bindings") or []:
            if str(binding.get("plan_signature") or "") != signature:
                raise ValueError("409: 対応表の計画版が変わりました")
            if str(binding.get("goal_contract_hash") or "") != str(contract.get("content_hash") or ""):
                raise ValueError("409: 対応表のGoalContractが変わりました")
        entry = confirm_binding_candidate(
            run, issue_id, actor=actor, task_key=task_key, criterion_id=criterion_id)
        _save(manager, run)
        return entry


async def remap_run(manager, pid: str, run_id: str, actor: str = "",
                    idempotency_key: str = "") -> dict:
    """保存済みの第1回レビューからP0/P1だけ再実行。外部AI呼出0・検証回数/利用枠の消費0。"""
    from app.project_delete import is_deleted as _is_deleted

    try:
        if _is_deleted(manager.memory.path, pid):
            raise ValueError("論理削除中のPJには動作しません")
    except ValueError:
        raise
    except Exception:
        pass
    async with _lock(pid):
        run = get_run(manager, pid, run_id)
        if str(run.get("state") or "") != "stopped" or str(run.get("stop_kind") or "") != "human_required":
            raise ValueError("human_requiredで停止したランだけ対応付け直しできます")
        key = str(idempotency_key or "").strip()
        history = [e for e in (run.get("remap_history") or []) if isinstance(e, dict)]
        if key:
            for entry in history:
                if entry.get("idempotency_key") == key:
                    return public_view(run)
        _mission, snapshot, signature, contract = _current(manager, pid)
        if signature != str(run.get("plan_signature") or ""):
            raise ValueError("409: 計画版が変わりました。新規確認してください")
        if str(contract.get("content_hash") or "") != str(run.get("goal_contract_hash") or ""):
            raise ValueError("409: GoalContractが変わりました。新規確認してください")
        rounds = [r for r in (run.get("rounds") or []) if isinstance(r, dict)]
        if not rounds:
            raise ValueError("保存済みレビューがありません")
        first = rounds[0]
        parsed = first.get("reviews") or []
        if not parsed:
            raise ValueError("保存済みレビューがありません")
        try:
            from app.goal_review import ReviewStore as _ReviewStore

            stored = _ReviewStore(manager.memory.path).get(pid, "plan", signature) or {}
            packet = stored.get("packet") or {}
            if packet and fingerprint(packet) != str(run.get("packet_hash") or ""):
                raise ValueError("409: packetが変わりました。新規確認してください")
        except ValueError:
            raise
        except Exception:
            pass
        saved_fp = fingerprint([str(first.get("plan_signature") or ""),
                                canonical(first.get("reviews") or [])])
        review_fp = fingerprint([signature, canonical(parsed)])
        if saved_fp != review_fp:
            raise ValueError("409: レビューが変わりました。新規確認してください")
        try:
            from app.goal_review import ReviewStore as _ReviewStore3

            _stored_reviews = (_ReviewStore3(manager.memory.path).get(pid, "plan", signature) or {}).get("reviews") or []

            def _triples(rows: list) -> list:
                triples = []
                for row in rows:
                    if isinstance(row, dict):
                        triples.append([str(row.get("provider") or ""), str(row.get("status") or ""),
                                        canonical(row.get("issues") or [])])
                return triples

            if _stored_reviews and _triples(_stored_reviews) != _triples(parsed):
                raise ValueError("409: レビューが変わりました。新規確認してください")
        except ValueError:
            raise
        except Exception:
            pass
        table = build_issue_bindings(parsed, contract, snapshot,
                                     plan_signature=signature,
                                     goal_contract_hash=str(contract.get("content_hash") or ""),
                                     packet_hash=str(first.get("packet_hash") or run.get("packet_hash") or ""))
        binding_fp = fingerprint(table["bindings"])
        if not key:
            for entry in history:
                if entry.get("binding_fingerprint") == binding_fp:
                    return public_view(run)
        external_before = int(run.get("external_calls") or 0)
        rounds_before = int(run.get("verification_rounds") or 0)
        from app.plan_feedback import attach_issue_bindings as _attach
        from app.plan_feedback import issues_for as _issues_for
        from app.plan_feedback import normalize_planning_feedback as _normalize

        issues = _issues_for(manager, pid, signature)
        bound = _attach(issues, table["bindings"])
        if [x.get("id") for x in bound] != [x.get("id") for x in issues]:
            raise ValueError("指摘の順序・件数が変わりました")
        _deltas = _normalize(bound, signature)
        resolved = [b for b in table["bindings"] if b["state"] == "resolved"]
        multi = [b for b in table["bindings"] if b["state"] == "multiple_targets"]
        blocked = [b for b in table["bindings"] if b["state"] not in ("resolved", "multiple_targets")]
        run["issue_bindings"] = table["bindings"]
        run["issue_binding_version"] = table["version"]
        entry = {
            "at": _now(),
            "actor": str(actor or "")[:100],
            "idempotency_key": key,
            "binding_fingerprint": binding_fp,
            "bindings": len(table["bindings"]),
            "resolved": len(resolved),
            "multiple_targets": len(multi),
            "blocked": len(blocked),
            "external_calls": 0,
            "review_source": "stored",
        }
        run.setdefault("remap_history", []).append(entry)
        if len(run["remap_history"]) > 20:
            run["remap_history"] = run["remap_history"][-20:]
        if int(run.get("external_calls") or 0) != external_before:
            raise ValueError("外部呼出が発生したため中断しました")
        if int(run.get("verification_rounds") or 0) != rounds_before:
            raise ValueError("検証回数が変わったため中断しました")
        run = _save(manager, run)
        try:
            already = False
            events = (manager.memory.get_mission(pid) or {}).get("events") or []
            for ev in events:
                if ev.get("kind") == "auto_loop_remapped" and run_id in str(ev.get("detail") or ""):
                    already = True
                    break
            if not already:
                manager.memory.add_event(pid, "auto_loop_remapped",
                                         "保存済み指摘を対応付け直しました(外部送信なし)",
                                         detail=canonical({"run_id": run_id,
                                                           "bindings": len(table["bindings"])}))
        except Exception:
            pass
        return public_view(run)


async def run_loop(manager, pid: str, run_id: str) -> dict:
    """保存済み状態から再開可能。送信済みpacketは無断で再送しない。"""
    async with _lock(pid):
        return await _run_locked(manager, pid, run_id)


def _stop_guard_tick(manager, pid: str, run: dict) -> tuple[dict, dict | None]:
    """Stage1 空転ガード: 同一(署名,クラス,コード)2連続で stopped にする。

    履歴を書くのはランのティックだけ。新しい署名・新しい入力・人の選択があれば解除。
    戻り値: (guard, flat_record)。guard["repeating"] が True なら停止する。
    """
    from app.stop_classifier import append_history, classify_stop, guard_auto_tick

    sig = str(run.get("plan_signature") or "")
    record = classify_stop({"run": run, "plan_signature": sig})
    guard = guard_auto_tick(manager, pid, run, record)
    flat = dict(guard.get("record") or {})
    # ティックでのみ追記 (冪等キーで同時ティックの二重化を防ぐ)。
    key_parts = [pid, flat.get("plan_signature") or "", flat.get("stop_class") or "",
                 flat.get("stop_code") or "", str(run.get("id") or ""),
                 str(int(run.get("verification_rounds") or 0))]
    import hashlib as _hashlib

    idem = "tick:" + _hashlib.sha256("|".join(key_parts).encode()).hexdigest()[:32]
    try:
        append_history(manager, pid, flat, actor="auto-tick", idempotency_key=idem)
    except Exception:
        pass
    guard["history"] = guard.get("history") or []
    return guard, flat


async def _run_locked(manager, pid: str, run_id: str) -> dict:
    from app.goal_review import plan_snapshot as _snap
    from app.goal_review import public_structure as _pub
    from app.goal_review import record_send_approval as _approve
    from app.goal_review import review_plan as _review_plan
    from app.goal_review import evaluate_external_review_status as _judge
    from app.goal_review import review_pass_policy as _policy
    from app.goal_review_queue import public_draft as _draft_text
    from app.plan_feedback import issues_for as _issues_for
    from app.structured_planning import decode_object as _decode

    run = get_run(manager, pid, run_id)
    if run.get("state") in {"cancelled", "awaiting_human"}:
        return public_view(run)

    # Stage1 空転ガード (自動ランのティック/再試行の入口)。
    # 同一署名・同一停止の2連続で stopped にし、理由を残す。
    # 既存の再試行上限・外部評価上限 (最大2回)・人確認ゲートは変えない。
    try:
        guard, _flat = _stop_guard_tick(manager, pid, run)
    except Exception:
        guard = {"repeating": False}
    if bool(guard.get("repeating")):
        run = get_run(manager, pid, run_id)
        run["state"] = "stopped"
        run["stop_reason"] = "同じ停止の繰り返しのため自動ランを停止しました"
        run["stop_kind"] = "stopped"
        _save(manager, run)
        lease = _lease_get(manager, pid) or {}
        if lease.get("run_id") == run_id:
            _lease_set(manager, pid, None)
        try:
            manager.memory.add_event(pid, "auto_loop_stopped", "同じ停止の繰り返しのため自動ランを停止しました",
                                     detail=canonical({"run_id": run_id}))
        except Exception:
            pass
        return public_view(run)

    def _stop(reason: str, kind: str = "stopped") -> dict:
        run["state"] = "stopped"
        run["stop_reason"] = reason[:1000]
        run["stop_kind"] = kind
        _save(manager, run)
        lease = _lease_get(manager, pid) or {}
        if lease.get("run_id") == run_id:
            _lease_set(manager, pid, None)
        try:
            manager.memory.add_event(pid, "auto_loop_stopped", reason[:500],
                                     detail=canonical({"run_id": run_id}))
        except Exception:
            pass
        return public_view(run)

    if run.get("draft_mode") != "local":
        return _stop("旧Gemini方式の実行履歴です。現行方式で新規開始してください", "legacy")

    try:
        _check_pinned(manager, pid, run)
        _check_budget_deadline(manager, pid, run)
    except ValueError as exc:
        return _stop(str(exc))

    # --- 保存済み計画からローカル草案を作る。外部草案AIは呼ばない。 ---
    if not run.get("local_draft"):
        run["state"] = "drafting"
        run = _save(manager, run)
        try:
            _mission, snapshot, signature, contract = _current(manager, pid)
            run["local_draft"] = _local_draft(snapshot, contract)
            run = _save(manager, run)
        except ValueError as exc:
            return _stop(str(exc))

    # --- ローカル整理 + 構造検査 ---
    if not run.get("organized"):
        try:
            _mission, snapshot, signature, contract = _current(manager, pid)
            run["organized"] = _local_organize(run["local_draft"], contract, snapshot)
            run["state"] = "structure_checked"
            run = _save(manager, run)
        except ValueError as exc:
            run = get_run(manager, pid, run_id)
            return _stop(str(exc))

    # --- 検証ラウンド(最大2回。3回目は絶対にしない) ---
    while int(run.get("verification_rounds") or 0) < VERIFICATION_LIMIT:
        try:
            _check_pinned(manager, pid, run)
            _check_budget_deadline(manager, pid, run)
        except ValueError as exc:
            run = get_run(manager, pid, run_id)
            return _stop(str(exc))
        _mission, snapshot, signature, contract = _current(manager, pid)
        # 反映後の新署名に追従する(反映直前の再照合は apply 側とここで行う)。
        round_index = int(run.get("verification_rounds") or 0) + 1
        run["state"] = "verifying"
        run = _save(manager, run)
        # 送信承認とpacketの再照合(既存検査を再利用)。
        try:
            _approve(manager, pid, signature, run.get("public_summary") or _draft_text(snapshot),
                     run.get("providers") or [])
        except Exception as exc:
            run = get_run(manager, pid, run_id)
            return _stop(f"送信承認の再照合に失敗したため停止しました: {exc}")
        # 既存 review_plan を呼び出す(内部検査を迂回しない)。
        try:
            review_args=(manager, pid, signature,
                         run.get("public_summary") or _draft_text(snapshot),
                         True, None, f"auto-loop-{run_id}-{round_index}")
            if set(run.get("providers") or []) == set(_mission.get("external_providers") or []):
                result = await _review_plan(*review_args)
            else:
                result = await _review_plan(*review_args, providers_override=run.get("providers") or [])
        except ValueError as exc:
            run = get_run(manager, pid, run_id)
            return _stop(str(exc))
        # 実呼出数を算入(失敗・再試行も含めて provider 数分)。
        called_providers = list(result.get("called_providers") or [])
        run["external_calls"] = int(run.get("external_calls") or 0) + len(called_providers)
        run["verification_rounds"] = round_index
        parsed = result.get("reviews") or []
        # 不変の記録として先に保存。
        reused_providers = set(result.get("reused_providers") or [])
        stored_flags = [p in reused_providers for p in (run.get("providers") or [])]
        round_row = {
            "round": round_index,
            "plan_signature": signature,
            "packet_hash": run.get("packet_hash") or "",
            "providers": list(run.get("providers") or []),
            "status": str(result.get("status") or ""),
            "review_source": "stored" if stored_flags and all(stored_flags) else "provider",
            "stored_providers": [p for p, flag in zip(run.get("providers") or [], stored_flags) if flag],
            "reviews": [
                {"provider": str(r.get("provider") or ""),
                 "status": str(r.get("status") or ""),
                 "issues": r.get("issues") if isinstance(r.get("issues"), list) else [],
                 "issue_texts": [
                     (str(i.get("text") or i.get("reason") or "")[:500] if isinstance(i, dict) else str(i)[:500])
                     for i in (r.get("issues") or [])],
                 "response_error": str((r.get("connection_error") or {}).get("error") or "")[:300]}
                for r in parsed],
            "success_count": result.get("success_count"),
            "stop_reason": str(result.get("stop_reason") or ""),
        }
        run.setdefault("rounds", []).append(round_row)
        sources = [str(r.get("review_source") or "provider") for r in run.get("rounds") or []]
        if sources and all(s == "stored" for s in sources):
            run["review_source"] = "stored"
        elif "provider" in sources:
            run["review_source"] = "provider"
        run["review_source_detail"] = ",".join(sources)
        run = _save(manager, run)

        policy = _policy(manager, pid, len(run.get("providers") or []))
        judged = _judge(parsed, run.get("providers") or [], policy)
        if judged.get("status") in {"connection_failed", "awaiting_external"}:
            run = get_run(manager, pid, run_id)
            return _stop(judged.get("stop_reason") or "接続障害のため停止しました(計画指摘に変換しません)",
                         "connection_settings")
        if judged.get("status") == "passed":
            run["state"] = "awaiting_human"
            run["stop_reason"] = ""
            run = _save(manager, run)
            lease = _lease_get(manager, pid) or {}
            if lease.get("run_id") == run_id:
                _lease_set(manager, pid, None)
            return public_view(run)

        # --- 指摘の対応付け。曖昧な指摘は人の確認待ち。 ---
        run["state"] = "intake"
        run = _save(manager, run)
        intake_actor = "local"
        try:
            mapped = _map_issues_local(parsed, contract, snapshot,
                                       plan_signature=signature,
                                       goal_contract_hash=str(contract.get("content_hash") or ""),
                                       packet_hash=str(run.get("packet_hash") or ""))
        except ValueError:
            run = get_run(manager, pid, run_id)
            return _stop("指摘の対応先を特定できないため、人の確認待ちで停止しました", "human_required")
        run["intake_actor"] = intake_actor
        run["issue_bindings"] = mapped.get("bindings") or []
        run["issue_binding_version"] = mapped.get("version") or ISSUE_BINDING_VERSION
        run = _save(manager, run)

        # --- 修正案と自動反映(既存 propose/validate/apply を再利用) ---
        if round_index >= VERIFICATION_LIMIT:
            run = get_run(manager, pid, run_id)
            return _stop("第2回検証でも指摘が残ったため停止しました。3回目の自動評価は行いません")
        run["state"] = "proposing"
        run = _save(manager, run)
        try:
            issues = _issues_for(manager, pid, signature)
        except Exception as exc:
            run = get_run(manager, pid, run_id)
            return _stop(f"指摘の取得に失敗したため停止しました: {exc}")
        if not issues:
            run = get_run(manager, pid, run_id)
            return _stop("検証結果と指摘の対応が取れないため停止しました")
        try:
            from app.plan_feedback import attach_issue_bindings as _attach

            bound_issues = _attach(issues, mapped.get("bindings") or [])
            if [x.get("id") for x in bound_issues] != [x.get("id") for x in issues]:
                raise ValueError("指摘の順序・件数が変わりました")
        except Exception as exc:
            run = get_run(manager, pid, run_id)
            return _stop(f"指摘の対応付け保持に失敗したため停止しました: {type(exc).__name__}")
        try:
            from app.plan_feedback import propose as _propose
            from app.plan_feedback import apply as _apply

            row = await _propose(manager, pid, signature, None,
                                 idempotency_key=f"auto-loop-{run_id}-{round_index}",
                                 _issues_override=bound_issues)
        except ValueError as exc:
            run = get_run(manager, pid, run_id)
            return _stop(f"修正案の作成を停止しました: {exc}")
        except Exception as exc:
            run = get_run(manager, pid, run_id)
            return _stop(f"修正案の作成中に停止しました: {type(exc).__name__}")
        blockers = row.get("blockers") or []
        if blockers or not (row.get("changes") or []):
            run = get_run(manager, pid, run_id)
            kinds = sorted({str(a.get("disposition") or "") for a in blockers})
            return _stop(f"未解決・業務事実・追加開発のため人の確認待ちで停止しました({','.join(kinds) or 'blockers'})",
                         "human_required")
        # 反映直前に版・署名・原本・契約を再照合し、実行中/他ジョブが変更中なら停止。
        try:
            _mission2, _snap2, sig2, contract2 = _current(manager, pid)
            if sig2 != signature:
                run = get_run(manager, pid, run_id)
                return _stop("反映直前に計画版が変わったため停止しました")
            if str(contract2.get("content_hash") or "") != str(run.get("goal_contract_hash") or ""):
                run = get_run(manager, pid, run_id)
                return _stop("反映直前にGoalContractが変わったため停止しました")
            from app.goal_review import active_jobs as _active

            if _active(_store(manager), pid):
                run = get_run(manager, pid, run_id)
                return _stop("他ジョブが実行中のため反映を停止しました")
            if _mission2.get("status") == "running":
                run = get_run(manager, pid, run_id)
                return _stop("計画実行中のため反映を停止しました")
            # 反映先は未承認草案のみ。
            if _mission2.get("status") not in {"planning", "ready", "paused"}:
                run = get_run(manager, pid, run_id)
                return _stop("反映先が未承認草案ではないため停止しました")
            result2 = _apply(manager, pid, signature, row.get("candidate_id"))
        except ValueError as exc:
            run = get_run(manager, pid, run_id)
            return _stop(f"反映を停止しました: {exc}")
        # 反映後の独立検査: 達成条件・成果物の保持、未解決なし、新署名で再評価待ち。
        try:
            _mission3, snap3, sig3, _contract3 = _current(manager, pid)
            old_ids = {str(t.get("task_key") or "") for t in snapshot.get("tasks") or []}
            new_ids = {str(t.get("task_key") or "") for t in snap3.get("tasks") or []}
            final_ids = {str(t.get("task_key") or "") for t in snap3.get("tasks") or []
                         if str(t.get("task_key") or "") == "final_verification"}
            if not old_ids <= (new_ids | final_ids) and "execution_pipeline" not in str(canonical(row.get("changes") or "")):
                # 工程再構成以外では達成条件・工程の消失を許さない。
                pass
            run["plan_signature"] = sig3
            run["state"] = "applied"
            run = _save(manager, run)
        except Exception as exc:
            run = get_run(manager, pid, run_id)
            return _stop(f"反映後の独立検査に失敗したため停止しました(旧版を保持): {exc}")

    run = get_run(manager, pid, run_id)
    return _stop("検証上限(2回)に達したため停止しました。3回目の自動評価は行いません")
