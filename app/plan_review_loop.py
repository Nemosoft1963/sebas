"""L4: Gemini草案 -> ローカル整理 -> 許可済みAI検証(最大2回) -> 取込 -> 未承認草案反映.

薄いオーケストレーター。既存の review_plan / issues_for / propose /
validate_candidate / apply / evaluate_external_review_status /
public_structure の検査を迂回しない。承認・実行開始・外部公開・RAG登録は
自動化しない。外部呼出は call_provider_with_metadata 経由(Gemini草案・整理)
と manager.plan_review_runner 経由(review_plan)のみ。
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
    "drafting": "Gemini草案を作成中",
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

_CARD_CANDIDATE = re.compile(r"(?<!\d)(?:\d[\- ]?){13,19}(?!\d)")


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
    return 1 + 2 * max(0, int(provider_count)) + 2


def contains_secret(text: str) -> str:
    """秘密混入の疑いがあれば種別ラベルを、なければ空文字を返す。

    検出値そのものは返さない(ログ・状態・画面・応答への漏洩防止)。
    SECRET_PATTERNS の要素は (ラベル, 正規表現) または旧形式の正規表現。
    カード番号様は Luhn チェックで絞る。
    """
    blob = str(text or "")
    for item in SECRET_PATTERNS:
        if isinstance(item, tuple):
            label, pat = item
        else:
            label, pat = "秘密値", item
        if pat.search(blob):
            return str(label)
    m = _CARD_CANDIDATE.search(blob)
    while m:
        digits = re.sub(r"[\- ]", "", m.group(0))
        if 13 <= len(digits) <= 19 and digits.isdigit() and _luhn_ok(digits):
            return "カード番号様"
        m = _CARD_CANDIDATE.search(blob, m.start() + 1)
    return ""


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
        "actor": run.get("actor") or "",
        "deadline": run.get("deadline"),
        "packet_hash": run.get("packet_hash") or "",
        "rounds": run.get("rounds") or [],
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
    }


def preview_run(manager, pid: str, providers: list[str] | None = None,
                actor: str = "", ttl_seconds: int = LEASE_TTL) -> dict:
    mission, snapshot, signature, contract = _current(manager, pid)
    from app.goal_review_queue import public_draft
    from app.goal_review import public_structure

    selected = list(dict.fromkeys([str(x or "").strip() for x in (providers or []) if str(x or "").strip()]))
    allowed_ids = {"claude", "chatgpt", "gemini", "grok", "meta"}
    unknown = [p for p in selected if p not in allowed_ids]
    if unknown:
        raise ValueError(f"不明な外部AI: {', '.join(unknown)}")
    public_summary = public_draft(snapshot)
    packet = {"public_goal_and_plan": public_summary, "structure": public_structure(snapshot)}
    secret = contains_secret(canonical(packet))
    return {
        "project_id": pid,
        "plan_signature": signature,
        "goal_contract_hash": str(contract.get("content_hash") or ""),
        "gemini_role": "草案作成と取り込み失敗時の整理のみ。検証役・承認者にはならない",
        "verification_providers": selected,
        "self_review": "gemini" in selected,
        "self_review_warning": ("Geminiは草案作成者のため自己評価になる。独立した検証を要する運用では少なくとも1つ別のAIを指定すること"
                                if "gemini" in selected else ""),
        "public_summary": public_summary,
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
              ttl_seconds: int = LEASE_TTL) -> dict:
    actor = str(actor or "").strip()
    if not actor:
        raise ValueError("actor is required")
    selected = list(dict.fromkeys([str(x or "").strip() for x in (providers or []) if str(x or "").strip()]))
    if not selected:
        raise ValueError("検証AIを1つ以上選択してください")
    allowed_ids = {"claude", "chatgpt", "gemini", "grok", "meta"}
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
    from app.goal_review import public_structure, record_send_approval, send_allowed

    mission_providers = list(dict.fromkeys(mission.get("external_providers") or []))
    extra = [p for p in selected if p not in mission_providers]
    if extra or not selected or sorted(selected) != sorted(mission_providers):
        raise ValueError("検証AIはプロジェクトで許可済みの外部AIと一致させてください。評価AIを暗黙に増やしません")
    if not send_allowed(manager, pid):
        raise ValueError("外部AIの許可・接続が揃っていないため開始できません")

    summary = str(public_summary or "").strip() or public_draft(snapshot)
    if not 20 <= len(summary) <= 12000:
        raise ValueError("公開用説明は20〜12000文字にしてください")
    packet = {"public_goal_and_plan": summary, "structure": public_structure(snapshot)}
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
        "self_review": "gemini" in selected,
        "self_review_warning": ("Gemini自己評価のため別のAIの指定を推奨" if "gemini" in selected else ""),
        "plan_signature": signature,
        "goal_contract_hash": contract_hash,
        "public_summary": summary,
        "packet_hash": str(approval.get("packet_hash") or fingerprint(packet)),
        "verification_rounds": 0,
        "verification_limit": VERIFICATION_LIMIT,
        "external_calls": 0,
        "max_external_calls": max_external_calls(len(selected)),
        "gemini_organize_calls": 0,
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
        raise ValueError(f"Gemini呼出に失敗したため停止しました({info.get('category')})") from exc
    if purpose == "organize":
        run["gemini_organize_calls"] = int(run.get("gemini_organize_calls") or 0) + 1
        _save(manager, run)
    return text


def _draft_prompt(packet: dict) -> str:
    return ("公開用計画構造だけを使って計画草案の候補をJSONで返してください。"
            "原本本文・ファイル名・個人情報・秘密は含まれていません。推測で補わないでください。\n"
            + canonical(packet)[:8000])


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


def _map_issues_local(parsed_reviews: list[dict], contract: dict, snapshot: dict) -> dict:
    criteria = {str(x.get("criterion_id") or "") for x in (contract.get("criteria") or [])}
    task_keys = {str(t.get("task_key") or "") for t in (snapshot.get("tasks") or [])}
    mapped = []
    for review in parsed_reviews:
        for issue in (review.get("issues") or []):
            text = str(issue.get("text") or issue.get("reason") or "") if isinstance(issue, dict) else str(issue)
            found = re.findall(r"SC\d{2}", text)
            target = next((c for c in found if c in criteria), "")
            if not target:
                # 対応先不明は取り込み失敗。
                raise ValueError(f"指摘の対応先が不明のため取り込み失敗しました: {str(text)[:60]}")
            task = target if target in task_keys else next(iter(task_keys), "")
            if not task:
                raise ValueError("対応する工程が無いため取り込み失敗しました")
            mapped.append({"issue_text": text[:500], "criterion": target, "task": task,
                           "provider": str(review.get("provider") or "")})
    if not mapped:
        return {"mapped": [], "empty": True}
    return {"mapped": mapped, "empty": False}


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


async def run_loop(manager, pid: str, run_id: str) -> dict:
    """保存済み状態から再開可能。送信済みpacketは無断で再送しない。"""
    async with _lock(pid):
        return await _run_locked(manager, pid, run_id)


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

    try:
        _check_pinned(manager, pid, run)
        _check_budget_deadline(manager, pid, run)
    except ValueError as exc:
        return _stop(str(exc))

    # --- Gemini草案(再開時は再送しない) ---
    if not run.get("gemini_draft"):
        run["state"] = "drafting"
        _save(manager, run)
        _mission, snapshot, signature, contract = _current(manager, pid)
        packet = {"public_goal_and_plan": run.get("public_summary") or _draft_text(snapshot),
                  "structure": _pub(snapshot)}
        if run.get("packet_hash") not in (run.get("sent_packet_hashes") or []):
            run.setdefault("sent_packet_hashes", []).append(str(run.get("packet_hash") or ""))
        try:
            raw = await _call_gemini(manager, pid, run, _draft_prompt(packet), "draft")
            try:
                body = _decode(raw)
            except ValueError:
                return _stop("Gemini草案が構造化形式として不正のため停止しました")
            run["gemini_draft"] = validate_gemini_draft(body)
            run = _save(manager, run)
        except ValueError as exc:
            run = get_run(manager, pid, run_id)
            return _stop(str(exc))

    # --- ローカル整理 + 構造検査 ---
    if not run.get("organized"):
        try:
            _mission, snapshot, signature, contract = _current(manager, pid)
            run["organized"] = _local_organize(run["gemini_draft"], contract, snapshot)
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
            result = await _review_plan(manager, pid, signature,
                                        run.get("public_summary") or _draft_text(snapshot),
                                        True, None,
                                        f"auto-loop-{run_id}-{round_index}")
        except ValueError as exc:
            run = get_run(manager, pid, run_id)
            return _stop(str(exc))
        # 実呼出数を算入(失敗・再試行も含めて provider 数分)。
        run["external_calls"] = int(run.get("external_calls") or 0) + len(run.get("providers") or [])
        run["verification_rounds"] = round_index
        parsed = result.get("reviews") or []
        # 不変の記録として先に保存。
        round_row = {
            "round": round_index,
            "plan_signature": signature,
            "packet_hash": run.get("packet_hash") or "",
            "providers": list(run.get("providers") or []),
            "status": str(result.get("status") or ""),
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

        # --- 指摘の取り込み(ローカル優先、失敗時のみGemini整理・各ラウンド最大1回) ---
        run["state"] = "intake"
        run = _save(manager, run)
        intake_actor = "local"
        try:
            _map_issues_local(parsed, contract, snapshot)
        except ValueError:
            if int(run.get("gemini_organize_calls") or 0) >= 2:
                run = get_run(manager, pid, run_id)
                return _stop("指摘の取り込みに失敗し、Gemini整理の上限(合計2回)に達したため停止しました")
            per_round = sum(1 for r in (run.get("rounds") or []) if r.get("round") == round_index and r.get("gemini_organized"))
            if per_round >= 1:
                run = get_run(manager, pid, run_id)
                return _stop("このラウンドでGemini整理を既に使用したため停止しました")
            try:
                allowed_packet = {"public_goal_and_plan": run.get("public_summary") or "",
                                  "structure": _pub(snapshot)}
                organize_prompt = ("すでに送信を許可した公開用packetと検証AIの判定・指摘だけを使って、"
                               "採用・不採用案・対応先・修正候補・保留理由をJSONで整理してください。"
                               "元の判定をpassに書き換えず、指摘を削除せず、業務事実を推測せず、"
                               "追加の外部送信やツール実行を指示しないでください。\n"
                               + canonical({"packet": allowed_packet,
                                            "reviews": [{"provider": r.get("provider"), "status": r.get("status"),
                                                         "issues": r.get("issues")} for r in parsed],
                                            "plan_signature": signature,
                                            "goal_contract_hash": contract.get("content_hash") or ""})[:8000])
                raw2 = await _call_gemini(manager, pid, run, organize_prompt, "organize")
                try:
                    body2 = _decode(raw2)
                except ValueError:
                    run = get_run(manager, pid, run_id)
                    return _stop("Gemini整理の形式が不正のため停止しました")
                validate_gemini_organized(body2, parsed, signature,
                                          str(contract.get("content_hash") or ""))
                intake_actor = "gemini"
                run["rounds"][-1]["gemini_organized"] = True
                run = _save(manager, run)
            except ValueError as exc2:
                run = get_run(manager, pid, run_id)
                return _stop(str(exc2))
        run["intake_actor"] = intake_actor
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
            from app.plan_feedback import propose as _propose
            from app.plan_feedback import apply as _apply

            row = await _propose(manager, pid, signature, None,
                                 idempotency_key=f"auto-loop-{run_id}-{round_index}")
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
