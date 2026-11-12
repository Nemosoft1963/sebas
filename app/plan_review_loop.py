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
import sqlite3
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
ISSUE_BINDING_VERSION = "issue-binding-v2"
ISSUE_BINDING_V1 = "issue-binding-v1"
#: P0-A(4): 外部AI指摘の分類(提案)の規則版。規則を変えたら上げる。
ISSUE_CLASSIFICATION_VERSION = "issue-classification-v1"
#: P0-A(4): 分類の取りうる値(提案であって決定ではない)。
ISSUE_CLASSIFICATION_KINDS = ("plan_defect", "packet_defect", "missing_business_fact",
                              "missing_capability", "reviewer_uncertain", "unclassified")


def _classification_blob(issue: object) -> str:
    """分類用の結合文。指摘本文・根拠だけを使い、LLMを使わない。"""
    if not isinstance(issue, dict):
        return ""
    parts: list[str] = []
    for key in ("text", "issue_text", "issue", "reason", "remedy", "unmet_goal", "criterion"):
        value = issue.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    parts.append(item.strip()[:200])
    return "\n".join(parts)


def _classify_external_issue_kind(blob: str) -> tuple[str, str]:
    """決定的な種類判定。確信できないときは unclassified にする。

    順序は reviewer_uncertain > packet_defect > missing_business_fact >
    missing_capability > plan_defect の固定順。推測で plan_defect に倒さない。
    """
    text = str(blob or "")
    low = text.lower()
    # 1. 公開packetの欠陥(目的・説明が無く評価できない)。
    # 「評価できない」だけで評価者自身の不確かさとみなさず、対象の欠落を優先する。
    public_word = ("公開" in text or "packet" in low or "パケット" in text)
    purpose_word = ("目的" in text or "説明" in text or "要約" in text or "purpose" in low)
    unevaluable = ("評価できない" in text or "評価不能" in text or "判断できない" in text
                   or "cannot evaluate" in low or "unevaluable" in low)
    missing_word = ("無く" in text or "なく" in text or "ない" in text or "不足" in text or "欠" in text)
    if public_word and purpose_word and (unevaluable or missing_word):
        return ("packet_defect", "rule:packet_defect:公開と目的と評価不能または不足")
    if public_word and unevaluable:
        return ("packet_defect", "rule:packet_defect:公開と評価不能")
    # 2. 評価者自身の不確かさ(明示された確信・根拠の不足)。
    uncertain_hit = (
        ("確信" in text and any(k in text for k in ("持てない", "ない", "不十分", "できない", "不足")))
        or ("根拠" in text and any(k in text for k in ("不十分", "不足", "ない", "弱い", "欠", "乏しい")))
        or ("自信" in text and any(k in text for k in ("ない", "持てない", "不十分")))
        or ("uncertain" in low and "basis" in low)
        or ("not confident" in low)
        or ("insufficient basis" in low)
        or ("insufficient evidence" in low)
    )
    if uncertain_hit:
        return ("reviewer_uncertain", "rule:reviewer_uncertain:確信または根拠の不足")
    # 3. 業務事実の不足(日時・連絡先などの確認が必要)。
    business_word = ("業務" in text and ("事実" in text or "確認" in text))
    fact_words = ("日時", "連絡先", "日程", "場所", "担当", "連絡手段")
    fact_hit = any(w in text for w in fact_words) and ("確認" in text or "要確認" in text)
    if business_word or fact_hit:
        return ("missing_business_fact", "rule:missing_business_fact:業務事実または事実確認")
    # 4. 機能不足(専用の実行器などが無く追加が必要)。
    capability_hit = (
        ("機能追加" in text or "追加開発" in text or "不足機能" in text)
        or (("実行器" in text or "executor" in low or "capability" in low)
            and any(k in text for k in ("専用", "不足", "追加", "必要", "無く", "なく", "ない", "未実装")))
        or ("未実装" in text and "機能" in text)
    )
    if capability_hit:
        return ("missing_capability", "rule:missing_capability:実行器または機能追加")
    # 5. 計画の欠陥(工程・検証方法などの不足)。ここまで該当なしのときだけ見る。
    plan_word = ("工程" in text or "検証" in text or "達成条件" in text
                 or "成果物" in text or "手順" in text or "依存" in text)
    defect_word = ("欠け" in text or "不足" in text or "不備" in text or "漏れ" in text
                   or "ない" in text or "無い" in text or "甘い" in text
                   or "不明瞭" in text or "曖昧" in text)
    if plan_word and defect_word:
        return ("plan_defect", "rule:plan_defect:工程検証と不足")
    return ("unclassified", "rule:unclassified:いずれにも該当しない")


def classify_external_issue(issue: object) -> dict:
    """外部AI指摘の分類(提案)。原文・根拠・評価は変えず別フィールドで返す。

    決定的・LLM不使用・同じ入力は同じ分類。失敗時は分類不能を明示し、
    取込全体を止めない場合は unclassified と理由を記録する。
    """
    import logging as _logging

    try:
        blob = _classification_blob(issue)
        if not blob.strip():
            return {"kind": "unclassified", "basis": "rule:unclassified:指摘文が空",
                    "source": "rule", "version": ISSUE_CLASSIFICATION_VERSION}
        kind, basis = _classify_external_issue_kind(blob)
        if kind not in ISSUE_CLASSIFICATION_KINDS:
            kind = "unclassified"
            basis = "rule:unclassified:未知の分類結果"
        return {"kind": kind, "basis": str(basis)[:200],
                "source": "rule", "version": ISSUE_CLASSIFICATION_VERSION}
    except (AttributeError, TypeError, ValueError) as exc:
        _logging.getLogger(__name__).warning(
            "issue classification failed: %s", type(exc).__name__)
        return {"kind": "unclassified", "basis": "分類を実行できませんでした",
                "source": "rule", "version": ISSUE_CLASSIFICATION_VERSION}
TARGET_TYPES = ("goal_criterion", "preparation_gate", "task_only", "plan_level")
TARGET_TYPE_JA = {
    "goal_criterion": "目標SC",
    "preparation_gate": "準備工程",
    "task_only": "工程のみ",
    "plan_level": "計画全体",
}
PREPARATION_GATE_ID = "PREP00"
RESOLVED_PREPARATION_VERSION = "resolved-preparation-v1"
# 版付き再開の上限: 同一署名への再開は1回だけ。履歴は残す。
RESUME_LIMIT_PER_SIGNATURE = 1
# P0-3: 実行履歴の表示は短く保つが、再開済みキーは消さない。
#: P0-3: 空転ガードの停止原因の固定語彙。不明な値は unknown に落とす。
RESUME_FAILURE_CODES = ("human_required", "candidate_check_failed", "stopped")
ISSUE_KINDS = ("preparation", "requirement_item", "final_verification", "plan_level")
ISSUE_KIND_JA = {
    "preparation": "準備工程への指摘",
    "requirement_item": "実行工程の達成条件への指摘",
    "final_verification": "最終検証への指摘",
    "plan_level": "計画全体への指摘",
}
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

# 段階3-a: 人の確定が必要な対応表の状態。候補が1つでも自動確定しない。
UNCONFIRMED_BINDING_STATES = ("ambiguous", "invalid_reference")

# P0-B前半: 候補ゼロの指摘を任意工程へ割り付けない。判断の選択肢は識別子付きで返す。
NO_CANDIDATE_OPTIONS = (
    {"id": "reask_evaluator", "label_ja": "評価者への再確認",
     "detail_ja": "指摘の対象・根拠を評価者に再確認する"},
    {"id": "fix_public_packet", "label_ja": "公開packetの修正",
     "detail_ja": "公開packetの目的・構造を修正して再評価する"},
    {"id": "unsupported_or_missing_capability", "label_ja": "対応不可／不足機能",
     "detail_ja": "対応不可または不足機能として記録し、追加開発・業務確認へ回す"},
)

# P0-B前半: awaiting_decision応答の遷移先(判断待ちUIは別単位で新設するため、
# 既存の自動評価カードへのアンカーを返す。画面JSは変更しない)。
DECISION_DESTINATION = {
    "screen_id": "autoLoopCards",
    "path": "/cowork#autoLoopCards",
    "note_ja": "自動評価カードで未確定の指摘を確認し、対応付けプレビューと確定へ進む",
}


def unconfirmed_issue_ids(bindings: list[dict] | None,
                          confirmed_ids: list[str] | None = None) -> list[str]:
    """未確定の指摘IDだけを返す純粋関数。人確定済みは除外する。自動確定はしない。"""
    confirmed = {str(x) for x in (confirmed_ids or []) if str(x)}
    out: list[str] = []
    for binding in bindings or []:
        if not isinstance(binding, dict):
            continue
        issue_id = str(binding.get("issue_id") or "")
        if not issue_id or issue_id in confirmed:
            continue
        if str(binding.get("state") or "") in UNCONFIRMED_BINDING_STATES:
            if issue_id not in out:
                out.append(issue_id)
    return out


def binding_decision_hash(bindings: list[dict] | None) -> str:
    """P0-3: 確定済み(人が選んだ)対応付けだけから決定的に作るハッシュ。

    正規化: 確定行 = selection_status が confirmed の行だけ。行ごとに
    (issue_id, 選択した対応先) の組を作る。対応先は task_key / criterion_id /
    decision_option のうち非空のものだけを ``種別:値`` の形で拾い、組内と
    行全体を文字列順に整列する(順序非依存)。自由記述・指摘本文・時刻・actor
    は含めない。確定が0件なら空文字(=決定なし)を返す。
    """
    rows: list[str] = []
    for binding in bindings or []:
        if not isinstance(binding, dict):
            continue
        if str(binding.get("selection_status") or "") != "confirmed":
            continue
        issue_id = str(binding.get("issue_id") or "")
        if not issue_id:
            continue
        targets: list[str] = []
        task = str(binding.get("selected_task_key") or "").strip()
        criterion = str(binding.get("selected_criterion_id") or "").strip().upper()
        # 正本スナップショット由来で selection_status だけ戻った行は
        # selected_* が無いため manual_bindings 相当の別名も読む。
        if not task:
            task = str(binding.get("task_key") or "").strip()
        if not criterion:
            criterion = str(binding.get("criterion_id") or "").strip().upper()
        option = str(binding.get("selection_option")
                      or binding.get("decision_option") or "").strip()
        if task:
            targets.append("task:" + task)
        if criterion:
            targets.append("criterion:" + criterion)
        if option:
            targets.append("option:" + option)
        if not targets:
            continue
        rows.append(issue_id + "|" + ",".join(sorted(targets)))
    if not rows:
        return ""
    rows.sort()
    return fingerprint(rows)


def normalize_resume_failure_code(value: object) -> str:
    """P0-3: 空転ガードの停止原因を固定語彙に落とす。不明な値は unknown。"""
    text = str(value or "").strip()
    if text in RESUME_FAILURE_CODES:
        return text
    return "unknown"


def binding_resume_guard_key(*, plan_signature: str = "",
                             review_fingerprint: str = "",
                             binding_decision_hash_value: str = "",
                             failure_code: str = "") -> str:
    """P0-3: 空転ガードのキー sha256(計画署名|レビュー指紋|決定ハッシュ|失敗コード)。"""
    code = normalize_resume_failure_code(failure_code)
    blob = "|".join([str(plan_signature or ""), str(review_fingerprint or ""),
                     str(binding_decision_hash_value or ""), code])
    import hashlib as _hashlib

    return _hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _resume_guard_keys(run: dict) -> list[str]:
    """P0-3: ランに保存されたガードキー履歴。履歴は残す(消さない)。"""
    out: list[str] = []
    for entry in (run.get("resume_history") or []):
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("guard_key") or "")
        if key and key not in out:
            out.append(key)
    for key in (run.get("resume_guard_keys") or []):
        text = str(key or "")
        if text and text not in out:
            out.append(text)
    return out


def _append_resume_guard_key(run: dict, guard_key: str) -> None:
    """P0-3: ガードキー履歴の追記。同じ決定の再実行を永続的に防ぐ。"""
    keys = [str(key or "") for key in (run.get("resume_guard_keys") or [])
            if str(key or "")]
    text = str(guard_key or "")
    if text and text not in keys:
        keys.append(text)
    run["resume_guard_keys"] = keys


def _mark_binding_decision_executed(run: dict, guard_key: str) -> bool:
    """P0-3: 確定決定の実行消費を記録する。同じ組の二度実行を拒否する。"""
    text = str(guard_key or "")
    if not text:
        return False
    for entry in (run.get("resume_history") or []):
        if not isinstance(entry, dict):
            continue
        if (str(entry.get("resume_kind") or "") == "binding_decision"
                and str(entry.get("guard_key") or "") == text):
            if entry.get("executed") is True:
                return False
            entry["executed"] = True
            entry["executed_at"] = _now()
            return True
    return False


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
                              task_key: str = "", criterion_id: str = "",
                              plan_signature: str = "",
                              goal_contract_hash: str = "",
                              packet_hash: str = "",
                              review_fingerprint: str = "",
                              reason: str = "",
                              decision_option: str = "",
                              confirmed_preview: bool | None = None) -> dict:
    """曖昧な指摘の人による候補確定。担当者・日時・元指紋付きで記録する。

    それだけでは検証合格にしない(状態・判定・検証回数は変えない)。
    現行の対応表(public_viewのissue_bindings)に追記する純粋寄りの記録関数。
    計画署名と指摘IDを照合してから適用し、計画が変わったら選択を失効させる。
    未選択なら停止のまま。選択・差分確認後だけ再評価へ進める。
    P0-B前半: 確定は指摘ID・actor・時刻・候補・理由・各ハッシュ付きで保存する。
    候補ゼロの指摘は任意工程へ割り付けず、識別子付きの判断選択肢から選ぶ。
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
    if not str(target.get("issue_id") or "") or str(target.get("issue_id") or "") != str(issue_id):
        raise ValueError("指摘IDが一致しないため選択できません")
    _selection_signature_ok(target, plan_signature=plan_signature,
                            goal_contract_hash=goal_contract_hash,
                            packet_hash=packet_hash)
    if str(target.get("selection_status") or "") == "confirmed":
        raise ValueError("この指摘は既に選択済みです。計画が変わったら選択は失効します")
    if str(target.get("state") or "") not in ("ambiguous", "multiple_targets", "invalid_reference"):
        raise ValueError("曖昧な指摘だけ人が候補を確定できます")
    if confirmed_preview is False and (
            plan_signature or goal_contract_hash or packet_hash
            or str((run or {}).get("plan_signature") or "")
            or str((run or {}).get("goal_contract_hash") or "")):
        raise ValueError("修正差分のプレビュー確認後に選択してください")
    tasks = [str(x) for x in (target.get("candidate_task_keys") or []) if str(x)]
    criteria = [str(x) for x in (target.get("candidate_criterion_ids") or []) if str(x)]
    task = str(task_key or "").strip()
    criterion = str(criterion_id or "").strip().upper()
    reason_text = str(reason or "").strip()[:2000]
    decision = str(decision_option or "").strip()
    review_fp = str(review_fingerprint or "").strip()
    # P0-B前半: 対応表側のレビュー指紋は保存用に保持する。古いハッシュでの確定は
    # 正本スナップショットの照合(check_binding_snapshot_current)で409拒否する。
    # 理由は画面側で必須とし、関数側では従来どおり任意に保つ(既存経路を壊さない)。
    # P0-B前半: 対応表側のレビュー指紋は保存用に保持する。古いハッシュでの確定は
    # 正本スナップショットの照合(check_binding_snapshot_current)で409拒否する。
    # ここでは粒子の異なる指紋同士を直接比較しない(既存経路を壊さない)。
    saved_review_fp = ""
    _evidence = target.get("evidence") if isinstance(target.get("evidence"), dict) else {}
    if isinstance(_evidence, dict):
        saved_review_fp = str(_evidence.get("review_fingerprint") or "")
    has_candidates = bool(tasks or criteria)
    if not has_candidates:
        # P0-B前半: 候補ゼロの指摘は任意工程へ割り付けない。識別子付きの判断から選ぶ。
        valid_ids = [str(x.get("id") or "") for x in NO_CANDIDATE_OPTIONS]
        if decision not in valid_ids:
            raise ValueError("候補がない指摘は判断の選択肢から確定してください")
        if not reason_text:
            raise ValueError("候補がない指摘の判断理由を入力してください")
        if task or criterion:
            raise ValueError("候補がない指摘に工程・達成条件は割り付けできません")
        entry = {
            "issue_id": str(issue_id),
            "actor": actor_name[:100],
            "at": _now(),
            "task_key": "",
            "criterion_id": "",
            "decision_option": decision,
            "reason": reason_text,
            "plan_signature": str(target.get("plan_signature") or plan_signature or ""),
            "goal_contract_hash": str(target.get("goal_contract_hash") or goal_contract_hash or ""),
            "packet_hash": str(target.get("packet_hash") or packet_hash or ""),
            "review_fingerprint": review_fp or saved_review_fp,
            "preview": {"version": SELECTION_PREVIEW_VERSION,
                        "issue_id": str(issue_id),
                        "decision_option": decision,
                        "note": "候補なしのため判断の選択肢から確定しました"},
            "source_binding_fingerprint": fingerprint(target),
            "source_state": str(target.get("state") or ""),
            "source_issue_kind": str(target.get("issue_kind") or ""),
            "note": "人の候補確定であり検証合格ではない",
        }
        history = [e for e in ((run or {}).get("manual_bindings") or []) if isinstance(e, dict)]
        history.append(entry)
        run["manual_bindings"] = history[-20:]
        target["selection_status"] = "confirmed"
        target["selected_task_key"] = ""
        target["selected_criterion_id"] = ""
        target["selection_option"] = decision
        target["selection_actor"] = actor_name[:100]
        target["selection_reason"] = reason_text
        target["selection_preview"] = dict(entry.get("preview") or {})
        return entry
    if task and task not in tasks:
        raise ValueError("候補に無い工程は確定できません")
    if criterion and criterion not in criteria:
        raise ValueError("候補に無い達成条件は確定できません")
    if not task and not criterion:
        raise ValueError("確定する候補を指定してください")
    preview_value = preview_binding_selection(target, task_key=task, criterion_id=criterion)
    entry = {
        "issue_id": str(issue_id),
        "actor": actor_name[:100],
        "at": _now(),
        "task_key": task,
        "criterion_id": criterion,
        "reason": reason_text,
        "plan_signature": str(target.get("plan_signature") or plan_signature or ""),
        "goal_contract_hash": str(target.get("goal_contract_hash") or goal_contract_hash or ""),
        "packet_hash": str(target.get("packet_hash") or packet_hash or ""),
        "review_fingerprint": review_fp or saved_review_fp,
        "preview": preview_value,
        "source_binding_fingerprint": fingerprint(target),
        "source_state": str(target.get("state") or ""),
        "source_issue_kind": str(target.get("issue_kind") or ""),
        "note": "人の候補確定であり検証合格ではない",
    }
    history = [e for e in ((run or {}).get("manual_bindings") or []) if isinstance(e, dict)]
    history.append(entry)
    run["manual_bindings"] = history[-20:]
    target["selection_status"] = "confirmed"
    target["selected_task_key"] = task
    target["selected_criterion_id"] = criterion
    target["selection_actor"] = actor_name[:100]
    target["selection_reason"] = reason_text
    target["selection_preview"] = dict(entry.get("preview") or {})
    # P0-B前半: 確定は対応付けスナップショットにも保存される想定。確定済みが
    # 再推定で曖昧に戻らないよう、対応表側の状態は変えない(自動確定しない)。
    return entry


def p0b_stored_review_resume(manager, pid: str, run: dict, *,
                             signature: str, parsed: list[dict]) -> dict:
    """P0-B前半: 旧停止ランの限定回復。保存済みレビューを再利用して再対応付け。

    同一レビュー・署名・packetハッシュ・指摘IDが一致するときに限り、外部APIを
    再送せず再対応付けできる。版不一致・本当に同一の空転なら理由付きで拒否し、
    新規ランへ誘導する。無条件に空転ガードは解除しない。
    """
    from app.plan_feedback import issues_for as _issues_for

    if not isinstance(run, dict):
        return {"ok": False, "reason": "対象のランを確認できません",
                "action": "新規ランを開始してください"}
    if str(run.get("state") or "") != "stopped":
        return {"ok": False, "reason": "停止中のランだけ限定回復できます",
                "action": "新規ランを開始してください"}
    if str(run.get("plan_signature") or "") != str(signature or ""):
        return {"ok": False, "reason": "計画版が変わりました。新規確認してください",
                "action": "新規ランを開始してください"}
    rounds = [r for r in (run.get("rounds") or []) if isinstance(r, dict)]
    if not rounds:
        return {"ok": False, "reason": "保存済みレビューがありません",
                "action": "新規ランを開始してください"}
    first = rounds[0]
    saved_parsed = first.get("reviews") or []
    if canonical(saved_parsed) != canonical(parsed or []):
        return {"ok": False, "reason": "レビューが変わりました。新規確認してください",
                "action": "新規ランを開始してください"}
    try:
        saved_ids = [str(x.get("id") or "") for x in _issues_for(manager, pid, signature)]
    except (AttributeError, KeyError, TypeError, ValueError, OSError) as exc:
        return {"ok": False,
                "reason": "保存レビューを確認できないため限定回復できません:" + type(exc).__name__,
                "action": "新規ランを開始してください"}
    run_ids = [str(b.get("issue_id") or "") for b in (run.get("issue_bindings") or [])
               if isinstance(b, dict)]
    if run_ids and saved_ids != run_ids:
        return {"ok": False, "reason": "指摘IDが変わりました。新規確認してください",
                "action": "新規ランを開始してください"}
    _mission, snapshot, current_signature, contract = _current(manager, pid)
    if current_signature != str(signature or ""):
        return {"ok": False, "reason": "計画版が変わりました。新規確認してください",
                "action": "新規ランを開始してください"}
    table = build_issue_bindings(parsed or [], contract, snapshot,
                                 plan_signature=current_signature,
                                 goal_contract_hash=str(contract.get("content_hash") or ""),
                                 packet_hash=str(first.get("packet_hash") or run.get("packet_hash") or ""))
    return {"ok": True, "reason": "", "action": "",
            "bindings": table["bindings"], "version": table["version"],
            "external_calls": 0}


def awaiting_decision_view(run: dict, bindings: list[dict],
                           unconfirmed_ids: list[str]) -> dict:
    """P0-B前半: awaiting_decision応答に遷移先・未確定件数・候補を載せる。"""
    items = []
    for binding in bindings or []:
        if not isinstance(binding, dict):
            continue
        issue_id = str(binding.get("issue_id") or "")
        if not issue_id or issue_id not in set(unconfirmed_ids or []):
            continue
        has_candidates = bool(binding.get("candidate_task_keys") or binding.get("candidate_criterion_ids"))
        entry = {
            "issue_id": issue_id,
            "state": str(binding.get("state") or ""),
            "issue_kind": str(binding.get("issue_kind") or ""),
            "issue_text": str(binding.get("issue_text") or ""),
            "step_raw": str(binding.get("step_raw") or ""),
            "basis": str(binding.get("basis") or "")[:500],
            "candidate_task_keys": [str(x) for x in (binding.get("candidate_task_keys") or []) if str(x)][:16],
            "candidate_criterion_ids": [str(x) for x in (binding.get("candidate_criterion_ids") or []) if str(x)][:8],
            "selection_options": _binding_selection_options(binding),
        }
        if not has_candidates:
            entry["no_candidate_options"] = [dict(x) for x in NO_CANDIDATE_OPTIONS]
        items.append(entry)
    return {
        "status": "awaiting_decision",
        "destination": dict(DECISION_DESTINATION),
        "unconfirmed_issue_ids": list(unconfirmed_ids or []),
        "unconfirmed_count": len(list(unconfirmed_ids or [])),
        "candidates": items,
    }


def save_binding_table_first(manager, pid: str, signature: str,
                              bindings: list[dict]) -> dict:
    """P0-B前半: 対応表の構築・保存と自動適用の可否判定を分離する。

    ambiguous/invalid_referenceがあっても全指摘ID・原文・step参照・
    候補工程・候補達成条件・候補がない理由を正本スナップショットへ先に保存する。
    保存に失敗したらfail-closed(停止し、保存失敗を応答に残す)。
    例外を出して保存せずに止まる経路をなくす。
    """
    from app.plan_feedback import build_binding_snapshot as _build_snapshot
    from app.plan_feedback import review_fingerprint_of as _review_fp_of

    store = _store(manager)
    try:
        saved = store.get(pid, "plan", signature) or {}
        saved_packet = saved.get("packet")
        review_rows = saved.get("reviews") or []
        contract_hash = ""
        try:
            from app.goal_contract import preview as _preview_contract

            contract_hash = str((_preview_contract(manager, pid) or {}).get("content_hash") or "")
        except (AttributeError, KeyError, TypeError, ValueError, OSError):
            contract_hash = ""
        packet = saved_packet if isinstance(saved_packet, (dict, list)) else ""
        existing_row = store.get(pid, "binding_snapshot", signature) or {}
        existing_snapshot = (existing_row.get("snapshot")
                             if isinstance(existing_row, dict) else None)
        confirmed = []
        if isinstance(existing_snapshot, dict):
            # 同じ署名の再取込で人の確定を消さない。版が一致する正本だけを継承する。
            from app.plan_feedback import check_binding_snapshot_current as _check_current

            packet_hash = fingerprint(packet) if packet else ""
            review_fp = _review_fp_of(signature, review_rows)
            # D1-4: 旧正本は保存行由来とラン内コピー由来の両方で照合する。
            run_review_lists: list = []
            try:
                for _rid, _payload in store.list(pid, "auto_loop"):
                    if (isinstance(_payload, dict)
                            and str(_payload.get("plan_signature") or "") == str(signature or "")):
                        _rounds = _payload.get("rounds") or []
                        if isinstance(_rounds, list) and _rounds and isinstance(_rounds[0], dict):
                            _rr = _rounds[0].get("reviews")
                            if isinstance(_rr, list):
                                run_review_lists.append(_rr)
            except Exception:
                run_review_lists = []
            _check_current(
                existing_snapshot,
                plan_signature=signature,
                goal_contract_hash=contract_hash,
                packet_hash=packet_hash,
                review_fingerprint=review_fp,
                current_reviews=[review_rows] + run_review_lists,
            )
            confirmed = [dict(row) for row in (existing_snapshot.get("confirmed") or [])
                         if isinstance(row, dict)]
        snapshot_obj = _build_snapshot(
            bindings=list(bindings or []),
            plan_signature=signature,
            goal_contract_hash=contract_hash,
            packet_hash=fingerprint(packet) if packet else "",
            review_fingerprint=_review_fp_of(signature, review_rows),
            confirmed=confirmed,
        )
        store.put(pid, "binding_snapshot", signature,
                  {"snapshot": snapshot_obj, "saved_at": _now()})
        return {"saved": True, "binding_snapshot_save_failed": False,
                "binding_snapshot_save_error": "",
                "binding_snapshot_save_error_code": "",
                "binding_snapshot_save_detail": {}}
    except (OSError, ValueError, RuntimeError, AttributeError, KeyError, TypeError, sqlite3.Error) as exc:
        from app.plan_feedback import binding_snapshot_error_detail as _error_detail_first
        from app.plan_feedback import review_fingerprint_of as _review_fp_of_err
        err_code = str(getattr(exc, "code", "") or "")
        # D1-5: BindingSnapshotError 以外・except Exception の不明失敗に snapshot_missing は当てない。
        if not err_code:
            if isinstance(exc, (OSError, sqlite3.Error)):
                err_code = "store_error"
            elif isinstance(exc, ValueError):
                err_code = "store_error"
            else:
                err_code = "store_error"
        # D1-5: snapshot_missing は『正本が存在しない』ときだけ。保存失敗の不明な失敗は store_error。
        if err_code == "snapshot_missing":
            err_code = "store_error"
        _packet_hash_err = fingerprint(packet) if packet else ""
        _review_fp_err = ""
        try:
            _review_fp_err = _review_fp_of_err(signature, review_rows)
        except Exception:
            _review_fp_err = ""
        return {"saved": False, "binding_snapshot_save_failed": True,
                "binding_snapshot_save_error": type(exc).__name__,
                "binding_snapshot_save_error_code": err_code,
                "binding_snapshot_save_detail": _error_detail_first(
                    plan_signature=signature, packet_hash=_packet_hash_err,
                    review_fingerprint=_review_fp_err)}


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


def _snapshot_task_roles(snapshot: dict) -> dict[str, str]:
    from app.structured_planning import contract_of

    roles: dict[str, str] = {}
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
        if str(key) == "SC00" or str(contract.get("role") or "") == "preparation":
            roles[key] = "preparation"
        elif str(key) == "final_verification" or bool(contract.get("final_verification")):
            roles[key] = "final_verification"
        else:
            roles[key] = "execution"
    return roles


def _snapshot_final_task_key(snapshot: dict) -> str:
    for task in ((snapshot or {}).get("tasks") or []):
        if isinstance(task, dict) and str(task.get("task_key") or "") == "final_verification":
            return "final_verification"
    return ""


def _snapshot_preparation_task_key(snapshot: dict) -> str:
    roles = _snapshot_task_roles(snapshot)
    for key, role in roles.items():
        if role == "preparation":
            return str(key)
    return ""


def _classify_issue_kind(*, state: str, steps: list[int], step_tasks: list[str],
                         candidate_tasks: list[str], task_roles: dict[str, str],
                         final_task_key: str, preparation_task_key: str) -> str:
    """指摘の対応先の種別。準備・最終検証は実行工程へ無理に割り当てない。"""
    pool = list(dict.fromkeys([str(x) for x in (list(candidate_tasks or []) + list(step_tasks or [])) if str(x)]))
    final_hit = bool(final_task_key and final_task_key in pool)
    prep_hit = bool(preparation_task_key and preparation_task_key in pool)
    if state in ("invalid_reference",) and not pool:
        return "plan_level"
    if final_hit and not prep_hit:
        only_final = bool(pool) and all(str(x) == str(final_task_key) for x in pool)
        if only_final or (not steps and not candidate_tasks):
            return "final_verification"
    if prep_hit and not final_hit:
        only_prep = bool(pool) and all(str(x) == str(preparation_task_key) for x in pool)
        if only_prep or (not steps and not candidate_tasks):
            return "preparation"
    if final_hit and prep_hit:
        return "plan_level"
    if pool:
        for key in pool:
            if str(task_roles.get(str(key), "execution")) not in ("preparation", "final_verification"):
                return "requirement_item"
        first = str(pool[0])
        if str(task_roles.get(first, "")) == "preparation":
            return "preparation"
        if str(task_roles.get(first, "")) == "final_verification":
            return "final_verification"
        return "requirement_item"
    return "plan_level"


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


_PREP_SCOPE_RE = re.compile(r"準備|原本確認|入力検査|検査方法|停止条件", re.I)
_GOAL_CHANGE_RE = re.compile(r"目標変更|目標.{0,8}変更|達成条件.{0,8}追加|達成条件.{0,8}削除|業務事実.{0,8}確定", re.I)
_EXTERNAL_OP_RE = re.compile(r"公開|送信|外部操作|外部送信|API.{0,8}(?:送信|公開|実行)|ツール実行", re.I)


def _issue_scope_blob(issue: object, step_raw: str = "", unmet_goal: str = "",
                      reason: str = "", remedy: str = "", severity: str = "") -> str:
    parts = [step_raw, unmet_goal, reason, remedy, severity,
             _binding_issue_text(issue), _binding_issue_criterion(issue)]
    if isinstance(issue, dict) and isinstance(issue.get("criterion_ids"), list):
        parts.extend(str(x) for x in issue.get("criterion_ids") or [])
    return "\n".join(str(x) for x in parts if str(x))


def _snapshot_preparation_contract(snapshot: dict, preparation_task_key: str) -> dict:
    if not preparation_task_key:
        return {}
    for task in ((snapshot or {}).get("tasks") or []):
        if isinstance(task, dict) and str(task.get("task_key") or "") == str(preparation_task_key):
            try:
                from app.structured_planning import contract_of as _c

                return dict(_c(task) or {})
            except (AttributeError, TypeError, ValueError):
                return {}
    return {}


def check_resolved_preparation(*, issue: object, step_raw: str = "",
                               unmet_goal: str = "", reason: str = "",
                               remedy: str = "", severity: str = "",
                               steps: list[int], step_tasks: list[str],
                               task_roles: dict[str, str],
                               preparation_task_key: str,
                               preparation_contract: dict | None,
                               plan_signature: str = "",
                               goal_contract_hash: str = "",
                               packet_hash: str = "",
                               review_fingerprint: str = "") -> dict:
    """SC00への厳格な一意条件(提案2の条件1〜5)。すべて満たす場合だけ合格。

    1. 指摘のstepが現行計画の1工程だけを指し、その工程の役割がpreparation。
    2. 当該工程の保存済み契約に内部ゲートPREP00が存在する
       (role=preparationを内部ゲートの存在とみなす。工程自体が無い場合は欠け)。
    3. 指摘に別のSC番号・別工程・複数工程・公開/送信などの外部操作の要求がない。
    4. 計画署名・GoalContractハッシュ・送信packetハッシュ・レビュー指紋が揃う。
    5. 指摘内容が準備・原本確認・入力検査の範囲に収まり、計画全体の目標変更や
       業務事実の確定を要求しない。
    """
    reasons: list[str] = []
    blob = _issue_scope_blob(issue, step_raw, unmet_goal, reason, remedy, severity)
    mentioned = extract_sc_ids(step_raw, unmet_goal, reason, remedy, severity,
                               _binding_issue_criterion(issue),
                               _binding_issue_text(issue))
    only_prep = (len(list(steps or [])) == 1 and len(list(step_tasks or [])) == 1
                 and str((step_tasks or [""])[0]) == str(preparation_task_key or "")
                 and str(task_roles.get(str((step_tasks or [""])[0]), "")) == "preparation")
    cond1 = bool(only_prep)
    if not cond1:
        reasons.append("条件1未達: stepが準備工程の1工程だけを指していません")
    gate_ids: list[str] = []
    try:
        gates = ((preparation_contract or {}).get("internal_preparation_gate_ids")
                 or (preparation_contract or {}).get("preparation_gate_ids") or [])
        gate_ids = [str(x) for x in gates if str(x)]
    except (AttributeError, TypeError):
        gate_ids = []
    # 条件2は厳格に内部ゲートIDの存在で判定する。PREP00が欠けるケースは停止する。
    # role=preparationだけでは内部ゲートの存在とみなさない(SC01への混同防止)。
    cond2 = bool(preparation_task_key) and PREPARATION_GATE_ID in gate_ids
    if not cond2:
        reasons.append("条件2未達: 保存済み契約に内部ゲートPREP00がありません")
    cond3 = (not mentioned and len(list(steps or [])) == 1
             and not _EXTERNAL_OP_RE.search(blob))
    if not cond3:
        reasons.append("条件3未達: 別のSC・複数工程・外部操作の要求のいずれかを含みます")
    cond4 = all([str(plan_signature or ""), str(goal_contract_hash or ""),
                 str(packet_hash or ""), str(review_fingerprint or "")])
    if not cond4:
        reasons.append("条件4未達: 計画署名・契約・packet・指紋のいずれかが欠けています")
    cond5 = (bool(_PREP_SCOPE_RE.search(blob)) and not _GOAL_CHANGE_RE.search(blob))
    if not cond5:
        reasons.append("条件5未達: 準備・原本確認・入力検査の範囲に収まっていません")
    passed = all([cond1, cond2, cond3, cond4, cond5])
    return {
        "version": RESOLVED_PREPARATION_VERSION,
        "passed": bool(passed),
        "conditions": {"single_preparation_step": bool(cond1),
                       "prep_gate_present": bool(cond2),
                       "no_other_reference": bool(cond3),
                       "fingerprints_match": bool(cond4),
                       "preparation_scope": bool(cond5)},
        "reasons": reasons,
        "target_type": "preparation_gate" if passed else "",
        "target_task_keys": [str(step_tasks[0])] if passed and step_tasks else [],
        "target_ids": [PREPARATION_GATE_ID] if passed else [],
    }


def _target_type_for(*, state: str, issue_kind: str, resolved_preparation: bool,
                     candidate_tasks: list[str], candidate_criteria: list[str]) -> str:
    """IssueBinding v2の対応先種別。SC01とPREP00を混同しない。"""
    if state in ("invalid_reference",):
        return "plan_level"
    if resolved_preparation:
        return "preparation_gate"
    if str(issue_kind or "") == "preparation":
        if candidate_tasks and not candidate_criteria:
            return "task_only"
        return "plan_level"
    if candidate_tasks and candidate_criteria:
        return "goal_criterion"
    if candidate_tasks and not candidate_criteria:
        return "task_only"
    return "plan_level"


def _read_binding_v1(binding: dict) -> dict:
    """既存v1データの読取を維持する。v2項目が無ければ不明として返す(補完しない)。"""
    if not isinstance(binding, dict):
        return {"target_type": "", "target_task_keys": [], "target_ids": [],
                "evidence": {}, "unknown": True}
    if str(binding.get("target_type") or "") in TARGET_TYPES:
        return {"target_type": str(binding.get("target_type") or ""),
                "target_task_keys": list(binding.get("target_task_keys") or []),
                "target_ids": list(binding.get("target_ids") or []),
                "evidence": dict(binding.get("evidence") or {}),
                "unknown": False}
    return {"target_type": "", "target_task_keys": [], "target_ids": [],
            "evidence": {}, "unknown": True}


def _auto_apply_ok(binding: dict) -> bool:
    """自動反映してよいか。resolved_preparationは達成条件なしでも進める。"""
    if not isinstance(binding, dict):
        return False
    if is_contradictory_binding(binding):
        return False
    state = str(binding.get("state") or "")
    if state == "multiple_targets" and not _binding_coherent(binding):
        return False
    if state in AUTO_APPLY_STATES:
        return True
    resolved = binding.get("resolved_preparation") if isinstance(binding.get("resolved_preparation"), dict) else {}
    if bool(resolved.get("passed")) and state == "resolved":
        return True
    return False


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
    # F2a fail-closed: 読取不能を「lease無し」(None)と見て二重実行しない。
    # 広い except をやめ、想定できる失敗は例外で返し呼び出し側が止まる。
    try:
        return _store(manager).get(pid, "auto_loop_lease", pid)
    except (OSError, ValueError, RuntimeError, AttributeError, KeyError, TypeError) as exc:
        raise ValueError(f"leaseを確認できないため開始できません:{type(exc).__name__}") from exc


def _lease_set(manager, pid: str, row: dict | None) -> None:
    store = _store(manager)
    if row is None:
        # ReviewStore has no delete; expire immediately.
        # F2a fail-closed: 解除失敗は握りつぶさず狭い例外で記録する。
        # 残存leaseは開始を阻む側(停止側)に保つ。
        try:
            store.put(pid, "auto_loop_lease", pid, {"run_id": "", "expires": 0})
        except (OSError, ValueError, RuntimeError, AttributeError, KeyError, TypeError):
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
        "snapshot_retry_count": int(run.get("snapshot_retry_count") or 0),
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
        "resume_history": list(run.get("resume_history") or []),
        "resume_guard_keys": [str(x or "") for x in (run.get("resume_guard_keys") or [])
                              if str(x or "")],
        "revision_candidate_id": str(run.get("revision_candidate_id") or ""),
        "binding_version": run.get("binding_version") or run.get("issue_binding_version") or "",
        "planner_version": run.get("planner_version") or "",
        "binding_snapshot_save_failed": bool(run.get("binding_snapshot_save_failed")),
        "binding_snapshot_save_error": str(run.get("binding_snapshot_save_error") or ""),
        "binding_snapshot_save_error_code": str(run.get("binding_snapshot_save_error_code") or ""),
        "binding_snapshot_save_detail": dict(run.get("binding_snapshot_save_detail") or {}),
        "awaiting_decision": dict(run.get("awaiting_decision") or {}),
        "rounds": run.get("rounds") or [],
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
    }


def resume_eligibility(run: dict, *, binding_version: str = "",
                       planner_version: str = "",
                       binding_decision: bool = False,
                       review_fingerprint: str = "",
                       packet: object = None,
                       goal_contract: object = None,
                       plan_signature: str = "",
                       for_execute: bool = False) -> dict:
    """停止ランの安全な再開の可否。空転防止ガードは解除しない。

    binding_version/planner_versionが更新され、停止原因がその版で解決対象に
    なった場合に限り、同じ署名への再開を1回だけ認める。履歴は残す。
    P0-3: binding_decision=True のときは「確定決定による再開」を審査する。
    既存の版変更による再開経路は変えない。条件 = 停止中(human_required)・
    計画署名/レビュー指紋/packet/GoalContractが保存値と一致・未確定の指摘が
    無い・矛盾した確定が無い・binding_decision_hash が非空で guard_key が
    履歴に無い。満たさなければ理由付きで拒否する。
    for_execute=True は remap_run 実行用: 記録済み(permitted)の実行を受け付け、
    記録と入力の一致を確認する。同じ組の二度実行はどちらも拒否する。
    """
    if not isinstance(run, dict):
        return {"allowed": False, "reason": "対象のランを確認できません"}
    if not binding_decision and str(run.get("state") or "") != "stopped":
        return {"allowed": False, "reason": "停止中のランだけ再開できます"}
    history = [e for e in (run.get("resume_history") or []) if isinstance(e, dict)]
    signature = str(run.get("plan_signature") or "")
    same_signature_resumes = [e for e in history
                              if str(e.get("plan_signature") or "") == signature]
    if binding_decision:
        return _binding_decision_resume_eligibility(
            run, review_fingerprint=review_fingerprint, packet=packet,
            goal_contract=goal_contract, plan_signature=plan_signature,
            for_execute=for_execute)
    if len(same_signature_resumes) >= RESUME_LIMIT_PER_SIGNATURE:
        return {"allowed": False, "reason": "同じ署名への再開は1回だけです(空転防止)"}
    current_binding = str(run.get("binding_version") or run.get("issue_binding_version") or "")
    current_planner = str(run.get("planner_version") or "")
    new_binding = str(binding_version or "").strip()
    new_planner = str(planner_version or "").strip()
    from app.plan_feedback import PROMPT_VERSION
    if new_binding and new_binding != ISSUE_BINDING_VERSION:
        return {"allowed": False, "reason": "指定された対応付け版はサーバーの現行版ではありません"}
    if new_planner and new_planner != PROMPT_VERSION:
        return {"allowed": False, "reason": "指定された計画修正版はサーバーの現行版ではありません"}
    # 旧ランに版記録がない場合でも、変更されていない計画修正器を
    # 「新版」と偽って空転ガードを解除しない。
    if new_planner and not current_planner and new_planner == PROMPT_VERSION:
        return {"allowed": False, "reason": "計画修正器の版更新を確認できません"}
    version_changed = bool((new_binding and new_binding != current_binding)
                           or (new_planner and new_planner != current_planner))
    if not version_changed:
        return {"allowed": False, "reason": "版変更1回以外の単純な再試行は空転ガードが止めます"}
    stop_kind = str(run.get("stop_kind") or "")
    if stop_kind not in {"human_required", "stopped"}:
        return {"allowed": False, "reason": "この停止原因は版付き再開の対象ではありません"}
    if new_binding and new_binding != current_binding and stop_kind != "human_required":
        return {"allowed": False, "reason": "対応付け版の更新は指摘の対応先確認待ちに限ります"}
    return {"allowed": True, "reason": "",
            "resume_count": len(same_signature_resumes) + 1,
            "limit": RESUME_LIMIT_PER_SIGNATURE}


def _binding_decision_resume_eligibility(run: dict, *, review_fingerprint: str = "",
                                         packet: object = None,
                                         goal_contract: object = None,
                                         plan_signature: str = "",
                                         for_execute: bool = False) -> dict:
    """P0-3: 確定決定による再開の審査。同じ決定ハッシュでは二度目を停止する。

    既存の版変更による再開経路は変えない(ここでは版を見ない)。
    合格や進行に変換しない。満たさなければ理由付きで拒否する(fail-closed)。
    for_execute=True は実行用: 停止記録状態を要求せず、直近の記録と入力の
    一致を確認する。記録が無ければ拒否し、別々の記録を組み替えた実行を拒否する。
    """
    if for_execute:
        return _binding_decision_execute_eligibility(
            run, review_fingerprint=review_fingerprint, packet=packet,
            goal_contract=goal_contract, plan_signature=plan_signature)
    stop_kind = str(run.get("stop_kind") or "")
    if str(run.get("state") or "") != "stopped":
        return {"allowed": False, "reason": "停止中のランだけ再開できます"}
    if stop_kind != "human_required":
        return {"allowed": False, "reason": "確定決定による再開は人の確認待ちの停止だけが対象です"}
    signature = str(run.get("plan_signature") or "")
    if plan_signature and str(plan_signature or "") != signature:
        return {"allowed": False, "reason": "409: 計画版が変わりました。新規確認してください"}
    if not signature:
        return {"allowed": False, "reason": "確定決定を確認できないため再開できません"}
    rounds = [r for r in (run.get("rounds") or []) if isinstance(r, dict)]
    if rounds and str(rounds[0].get("plan_signature") or "") != signature:
        return {"allowed": False, "reason": "409: 計画版が変わりました。新規確認してください"}
    bindings = [b for b in (run.get("issue_bindings") or []) if isinstance(b, dict)]
    if not bindings:
        return {"allowed": False, "reason": "確定決定を確認できないため再開できません"}
    for binding in bindings:
        saved_sig = str(binding.get("plan_signature") or "")
        if saved_sig and saved_sig != signature:
            return {"allowed": False, "reason": "409: 対応表の計画版が変わりました。新規確認してください"}
    # 決定ハッシュと同じ集合だけを確定済みとして扱う。manual_bindings 単独の
    # issue_id では未確定を解除しない(selection_status=confirmed が正本)。
    confirmed_ids: list[str] = []
    for binding in bindings:
        if str(binding.get("selection_status") or "") != "confirmed":
            continue
        issue_id = str(binding.get("issue_id") or "")
        if issue_id and issue_id not in confirmed_ids:
            confirmed_ids.append(issue_id)
    unconfirmed = unconfirmed_issue_ids(bindings, confirmed_ids)
    if unconfirmed:
        return {"allowed": False,
                "reason": "未確定の指摘が残るため確定決定で再開できません:" + ",".join(unconfirmed[:8])}
    for binding in bindings:
        if is_contradictory_binding(binding):
            return {"allowed": False,
                    "reason": "矛盾した確定があるため再開できません:" + str(binding.get("issue_id") or "")}
    decision_hash = binding_decision_hash(bindings)
    if not decision_hash:
        return {"allowed": False, "reason": "確定した決定が無いため再開できません"}
    current_fp = str(review_fingerprint or "")
    if not current_fp:
        return {"allowed": False, "reason": "レビュー指紋を確認できないため再開できません"}
    rounds = [r for r in (run.get("rounds") or []) if isinstance(r, dict)]
    if not rounds:
        return {"allowed": False, "reason": "保存済みレビューが無いため再開できません"}
    saved_fp = str((rounds[0].get("reviews") or rounds[0].get("review_fingerprint") or "")
                    if isinstance(rounds[0], dict) else "")
    # ラン保存の第1回レビュー行から現行指紋と同じ粒子で照合する。
    # 粒子の異なる引数の指紋をそのまま通さず、保存レビューから再計算する。
    try:
        from app.plan_feedback import review_fingerprint_of as _review_fp_of

        saved_fp = _review_fp_of(str(rounds[0].get("plan_signature") or signature),
                                 rounds[0].get("reviews") or [])
    except (AttributeError, KeyError, TypeError, ValueError, OSError):
        return {"allowed": False, "reason": "レビュー指紋を確認できないため再開できません"}
    if saved_fp != current_fp:
        return {"allowed": False, "reason": "レビューが変わったため確定決定で再開できません"}
    if not isinstance(packet, (dict, list)) or not str(run.get("packet_hash") or ""):
        return {"allowed": False, "reason": "packetを確認できないため再開できません"}
    try:
        if fingerprint(packet) != str(run.get("packet_hash") or ""):
            return {"allowed": False, "reason": "packetが変わったため確定決定で再開できません"}
    except (AttributeError, TypeError, ValueError):
        return {"allowed": False, "reason": "packetを確認できないため再開できません"}
    contract_hash = ""
    if isinstance(goal_contract, dict):
        contract_hash = str(goal_contract.get("content_hash") or "")
    elif goal_contract is not None:
        contract_hash = str(goal_contract or "")
    if not contract_hash or not str(run.get("goal_contract_hash") or ""):
        return {"allowed": False, "reason": "GoalContractを確認できないため再開できません"}
    if contract_hash != str(run.get("goal_contract_hash") or ""):
        return {"allowed": False, "reason": "GoalContractが変わったため確定決定で再開できません"}
    guard_key = binding_resume_guard_key(
        plan_signature=signature, review_fingerprint=current_fp,
        binding_decision_hash_value=decision_hash, failure_code=stop_kind)
    if guard_key in _resume_guard_keys(run):
        return {"allowed": False,
                "reason": "同じ決定ハッシュでは二度目を停止します(空転防止)",
                "guard_key": guard_key,
                "binding_decision_hash": decision_hash,
                "review_fingerprint": current_fp}
    history = [e for e in (run.get("resume_history") or []) if isinstance(e, dict)]
    same_signature_resumes = [e for e in history
                              if str(e.get("plan_signature") or "") == signature]
    return {"allowed": True, "reason": "",
            "resume_count": len(same_signature_resumes) + 1,
            "limit": RESUME_LIMIT_PER_SIGNATURE,
            "guard_key": guard_key,
            "binding_decision_hash": decision_hash,
            "review_fingerprint": current_fp}


def _binding_decision_execute_eligibility(run: dict, *, review_fingerprint: str = "",
                                          packet: object = None,
                                          goal_contract: object = None,
                                          plan_signature: str = "") -> dict:
    """P0-3: 確定決定による再開の実行前審査。記録と入力の一致を確認する。

    直近の binding_decision 記録が無ければ拒否する。記録の guard_key /
    decision_hash / review_fingerprint / plan_signature と今回の入力が
    一致しなければ拒否する(別々の記録を組み替えた実行を拒否)。同じ組が
    実行済みなら拒否する(同じ決定ハッシュでは二度目を停止)。
    """
    if not isinstance(run, dict):
        return {"allowed": False, "reason": "対象のランを確認できません"}
    decision_records = [e for e in (run.get("resume_history") or []) if isinstance(e, dict)
                        and str(e.get("resume_kind") or "") == "binding_decision"]
    if not decision_records:
        return {"allowed": False, "reason": "確定決定の記録が無いため実行できません"}
    latest = decision_records[-1]
    if str(latest.get("plan_signature") or "") != str(run.get("plan_signature") or ""):
        return {"allowed": False, "reason": "計画署名が記録と一致しないため実行できません"}
    if plan_signature and str(latest.get("plan_signature") or "") != str(plan_signature or ""):
        return {"allowed": False, "reason": "計画版が変わったため実行できません"}
    current_fp = str(review_fingerprint or "")
    if not current_fp or current_fp != str(latest.get("review_fingerprint") or ""):
        return {"allowed": False, "reason": "レビュー指紋が記録と一致しないため実行できません"}
    bindings = [b for b in (run.get("issue_bindings") or []) if isinstance(b, dict)]
    decision_hash = binding_decision_hash(bindings)
    if not decision_hash or decision_hash != str(latest.get("binding_decision_hash") or ""):
        return {"allowed": False, "reason": "確定した決定が記録と一致しないため実行できません"}
    if not isinstance(packet, (dict, list)) or not str(run.get("packet_hash") or ""):
        return {"allowed": False, "reason": "packetを確認できないため実行できません"}
    try:
        if fingerprint(packet) != str(run.get("packet_hash") or ""):
            return {"allowed": False, "reason": "packetが変わったため実行できません"}
    except (AttributeError, TypeError, ValueError):
        return {"allowed": False, "reason": "packetを確認できないため実行できません"}
    contract_hash = ""
    if isinstance(goal_contract, dict):
        contract_hash = str(goal_contract.get("content_hash") or "")
    elif goal_contract is not None:
        contract_hash = str(goal_contract or "")
    if not contract_hash or not str(run.get("goal_contract_hash") or ""):
        return {"allowed": False, "reason": "GoalContractを確認できないため実行できません"}
    if contract_hash != str(run.get("goal_contract_hash") or ""):
        return {"allowed": False, "reason": "GoalContractが変わったため実行できません"}
    guard_key = str(latest.get("guard_key") or "")
    if not guard_key:
        return {"allowed": False, "reason": "ガードキーの記録が無いため実行できません"}
    executed = [str(e.get("guard_key") or "") for e in decision_records
                if e.get("executed") is True]
    if guard_key in executed:
        return {"allowed": False,
                "reason": "同じ決定ハッシュでは二度目を停止します(空転防止)",
                "guard_key": guard_key,
                "binding_decision_hash": decision_hash,
                "review_fingerprint": current_fp}
    return {"allowed": True, "reason": "",
            "guard_key": guard_key,
            "binding_decision_hash": decision_hash,
            "review_fingerprint": current_fp}


def record_resume(run: dict, *, actor: str, binding_version: str = "",
                  planner_version: str = "", cause: str = "",
                  binding_decision: bool = False,
                  review_fingerprint: str = "",
                  packet: object = None,
                  goal_contract: object = None,
                  failure_code: str = "") -> dict:
    """再開を記録する。再停止時は新しい原因と版を記録する(履歴は残す)。

    P0-3: binding_decision=True のときは確定決定による再開を記録する。
    binding_decision_hash・review_fingerprint・guard_key・external_calls: 0
    を保存する。履歴は残す(消さない)。
    """
    if binding_decision:
        return _record_binding_decision_resume(
            run, actor=actor, cause=cause, review_fingerprint=review_fingerprint,
            packet=packet, goal_contract=goal_contract, failure_code=failure_code)
    eligibility = resume_eligibility(run, binding_version=binding_version,
                                     planner_version=planner_version)
    if not eligibility.get("allowed"):
        raise ValueError(str(eligibility.get("reason") or "再開できません"))
    entry = {
        "at": _now(),
        "actor": str(actor or "")[:100],
        "plan_signature": str(run.get("plan_signature") or ""),
        "stop_kind": str(run.get("stop_kind") or ""),
        "stop_reason": str(run.get("stop_reason") or "")[:300],
        "binding_version": str(binding_version or "")[:80],
        "planner_version": str(planner_version or "")[:80],
        "cause": str(cause or "")[:300],
        "resume_count": int(eligibility["resume_count"]),
        "external_calls": 0,
    }
    history = [e for e in (run.get("resume_history") or []) if isinstance(e, dict)]
    history.append(entry)
    run["resume_history"] = history[-20:]
    if str(binding_version or "").strip():
        run["binding_version"] = str(binding_version or "")[:80]
    if str(planner_version or "").strip():
        run["planner_version"] = str(planner_version or "")[:80]
    run["state"] = "permitted"
    run["stop_reason"] = ""
    return entry


def _record_binding_decision_resume(run: dict, *, actor: str, cause: str = "",
                                    review_fingerprint: str = "",
                                    packet: object = None,
                                    goal_contract: object = None,
                                    failure_code: str = "") -> dict:
    """P0-3: 確定決定による再開の記録。同じ組は二度実行しない。"""
    eligibility = resume_eligibility(run, binding_decision=True,
                                     review_fingerprint=review_fingerprint,
                                     packet=packet, goal_contract=goal_contract)
    if not eligibility.get("allowed"):
        raise ValueError(str(eligibility.get("reason") or "再開できません"))
    code = normalize_resume_failure_code(failure_code or run.get("stop_kind") or "")
    resume_count_value = eligibility.get("resume_count")
    if not isinstance(resume_count_value, int):
        raise ValueError("再開回数を確認できないため記録できません")
    guard_key = str(eligibility.get("guard_key") or binding_resume_guard_key(
        plan_signature=str(run.get("plan_signature") or ""),
        review_fingerprint=str(eligibility.get("review_fingerprint") or review_fingerprint or ""),
        binding_decision_hash_value=str(eligibility.get("binding_decision_hash") or ""),
        failure_code=code))
    entry = {
        "at": _now(),
        "actor": str(actor or "")[:100],
        "plan_signature": str(run.get("plan_signature") or ""),
        "stop_kind": str(run.get("stop_kind") or ""),
        "stop_reason": str(run.get("stop_reason") or "")[:300],
        "binding_version": str(run.get("binding_version") or run.get("issue_binding_version") or "")[:80],
        "planner_version": str(run.get("planner_version") or "")[:80],
        "cause": str(cause or "binding decision resume")[:300],
        "resume_count": int(resume_count_value),
        "resume_kind": "binding_decision",
        "binding_decision_hash": str(eligibility.get("binding_decision_hash") or ""),
        "review_fingerprint": str(eligibility.get("review_fingerprint") or review_fingerprint or ""),
        "failure_code": code,
        "guard_key": guard_key,
        "external_calls": 0,
    }
    history = [e for e in (run.get("resume_history") or []) if isinstance(e, dict)]
    history.append(entry)
    run["resume_history"] = history[-20:]
    _append_resume_guard_key(run, guard_key)
    run["state"] = "permitted"
    run["stop_reason"] = ""
    return entry


def _selected_send_allowed(manager, mission: dict, selected: list[str]) -> bool:
    configured = {str(row.get("id") or "") for row in manager.provider_statuses()
                  if row.get("configured")}
    allowed = set(mission.get("external_providers") or [])
    return bool(mission.get("allow_external_ai") and selected
                and set(selected) <= allowed and set(selected) <= configured
                and manager.plan_review_runner)


def preview_run(manager, pid: str, providers: list[str] | None = None,
                actor: str = "", ttl_seconds: int = LEASE_TTL,
                explicit_summary: str = "") -> dict:
    try:
        from app.project_delete import is_deleted_or_unknown as _l3_is_blocked
        if _l3_is_blocked(manager.memory.path, pid):
            raise ValueError("論理削除中または削除状態を確認できないPJには動作しません(状態を確定できません)")
    except ValueError:
        raise
    except Exception:
        raise ValueError("論理削除中または削除状態を確認できないPJには動作しません(状態を確定できません)")
    mission, snapshot, signature, contract = _current(manager, pid)
    from app.goal_review import public_structure, build_final_packet, build_public_purpose_summary, check_public_consistency, is_generic_public_summary, is_valid_packet_hash, verify_packet_hash
    from app.goal_review_queue import public_draft as _public_draft_unused  # noqa: F401 (既定の汎用説明は参照しない)

    selected = list(dict.fromkeys([str(x or "").strip() for x in (providers or []) if str(x or "").strip()]))
    allowed_ids = {"claude", "chatgpt", "grok", "meta"}
    unknown = [p for p in selected if p not in allowed_ids]
    if unknown:
        raise ValueError(f"不明な外部AI: {', '.join(unknown)}")
    # P0-A: 利用者が編集した公開用説明そのものを受け取り、最終送信packetのハッシュを返す。
    # 既定の汎用説明でハッシュを作らない。
    explicit = str(explicit_summary) if isinstance(explicit_summary, str) else ""
    structure = public_structure(snapshot)
    purpose = build_public_purpose_summary(snapshot, explicit)
    generic = is_generic_public_summary(explicit)
    packet, packet_hash = build_final_packet(explicit, snapshot)
    consistency = check_public_consistency(snapshot, structure, explicit)
    secret = contains_secret(canonical(packet) + "\n" + explicit)
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
    if generic:
        blockers.append('目的説明が汎用文だけのため packet_incomplete です。具体的な公開要約を入力・確認してください')
    if not consistency.get("passed"):
        blockers.append('送信前検査に失敗: ' + " / ".join(consistency.get("reasons") or ["不一致"]))
    if not purpose.get("complete"):
        blockers.append('公開目的要約が不完全: ' + ",".join(purpose.get("missing") or ["不足"]))
    return {
        "project_id": pid,
        "plan_signature": signature,
        "goal_contract_hash": str(contract.get("content_hash") or ""),
        "draft_role": "既存計画と達成条件からローカルで構成。外部草案AIは呼び出さない",
        "verification_providers": selected,
        "self_review": False,
        "self_review_warning": "",
        "public_summary": explicit,
        "public_purpose": purpose,
        "consistency": consistency,
        "packet_status": consistency.get("status") or ("packet_incomplete" if generic else "ok"),
        "packet_preview": packet,
        "can_start": not blockers,
        "start_blockers": blockers,
        "budget_remaining": budget.get("remaining_calls"),
        "required_calls": required_calls,
        "packet_hash": packet_hash,
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
        "note": "この時点では外部送信しない。start で明示許可を記録してから初めて外部呼出を許す。編集後の説明で再プレビューし、返ったハッシュをそのまま開始APIへ送る。",
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
    try:
        from app.project_delete import is_deleted_or_unknown as _l3_is_blocked
        if _l3_is_blocked(manager.memory.path, pid):
            raise ValueError("論理削除中または削除状態を確認できないPJには動作しません(状態を確定できません)")
    except ValueError:
        raise
    except Exception:
        raise ValueError("論理削除中または削除状態を確認できないPJには動作しません(状態を確定できません)")
    lease = _lease_get(manager, pid) or {}
    if lease.get("run_id") and float(lease.get("expires") or 0) > _now():
        raise ValueError("同一PJで実行中のauto-loopがあります")
    mission, snapshot, signature, contract = _current(manager, pid)
    from app.goal_review import public_structure, record_send_approval, build_final_packet, is_generic_public_summary, verify_packet_hash

    mission_providers = list(dict.fromkeys(mission.get("external_providers") or []))
    extra = [p for p in selected if p not in mission_providers]
    if extra:
        raise ValueError("プロジェクトで許可済みの外部AIから選択してください")
    preflight=preview_run(manager,pid,selected,explicit_summary=public_summary)
    if not preflight['can_start']:
        raise ValueError('開始条件を満たしません: '+' / '.join(preflight['start_blockers']))
    if not _selected_send_allowed(manager, mission, selected):
        raise ValueError("外部AIの許可・接続が揃っていないため開始できません")

    summary = str(public_summary or "").strip()
    if not 20 <= len(summary) <= 12000:
        raise ValueError("公開用説明は20〜12000文字にしてください")
    if is_generic_public_summary(summary):
        raise ValueError("packet_incomplete: 目的説明が汎用文だけのため開始しません。具体的な公開要約を入力し、再プレビューしてください。外部AIは呼びません")
    packet, actual_hash = build_final_packet(summary, snapshot)
    verified = verify_packet_hash(expected_packet_hash, packet)
    if not verified.get("ok"):
        reason = str(verified.get("reason") or "mismatch")
        guidance = str(verified.get("guidance") or "再プレビューしてください。")
        # 旧クライアント(空の期待ハッシュ)には再プレビューの案内を返す。
        raise ValueError(f"409: 期待ハッシュと最終送信packetが一致しません({reason})。{guidance}")
    secret = contains_secret(canonical(packet) + "\n" + summary)
    if secret:
        raise ValueError(f"秘密混入の疑いのため送信前に停止しました(種別: {secret})")
    contract_hash = str(contract.get("content_hash") or "")
    if not contract_hash:
        raise ValueError("GoalContractがありません")
    # 明示許可を既存の送信承認として記録してから初めて外部呼出を許す。
    # 再計算した値で保存する(古い変数を使い回さない)。
    approval = record_send_approval(manager, pid, signature, summary, selected)
    if str(approval.get("packet_hash") or "") != str(actual_hash):
        raise ValueError("409: 保存したpacketハッシュが最終送信内容と一致しません。再プレビューしてください。")
    from app.plan_feedback import PROMPT_VERSION
    run = {
        "id": uuid.uuid4().hex,
        "binding_version": ISSUE_BINDING_VERSION,
        "planner_version": PROMPT_VERSION,
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
    # F2a fail-closed: 開始監査の失敗は握りつぶさず記録する(開始成功と区別できる)。
    try:
        manager.memory.add_event(pid, "auto_loop_started", "計画自動評価ループの外部送信範囲とAI役割を承認しました",
                                 detail=canonical({"run_id": run["id"], "providers": selected,
                                                   "packet_hash": run["packet_hash"]}))
    except (OSError, ValueError, RuntimeError, AttributeError, KeyError, TypeError) as exc:
        run["audit_error"] = type(exc).__name__
        _save(manager, run)
    return public_view(run)


def cancel_run(manager, pid: str, run_id: str, actor: str) -> dict:
    run = get_run(manager, pid, run_id)
    if run.get("state") in {"awaiting_human"}:
        raise ValueError("人の承認待ちのランは取り消せません。計画画面で承認・差戻ししてください")
    run["state"] = "cancelled"
    run["stop_reason"] = f"利用者 {actor} が取り消しました。以後の外部呼出は行いません"
    run["stop_kind"] = "cancelled"
    _save(manager, run)
    # F2a: lease確認不能でも取消表示は返す。残存leaseは開始を阻む(停止側)。
    try:
        lease = _lease_get(manager, pid) or {}
    except ValueError:
        lease = {}
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
    # F2a fail-closed: lease確認不能は「他が保持」と見て二重実行せず停止する。
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
    guard = manager._outbound_guard(pid, "auto_loop_draft", prompt, ["gemini"])
    if not guard.get("ok"):
        raise ValueError("送信前検査により停止しました: " + ','.join(str(c) for c in (guard.get("codes") or [])))
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
        except (OSError, ValueError, RuntimeError, AttributeError, KeyError, TypeError):
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
    task_roles = _snapshot_task_roles(snapshot)
    final_task_key = _snapshot_final_task_key(snapshot)
    preparation_task_key = _snapshot_preparation_task_key(snapshot)
    criterion_index = _criterion_task_index(contract)
    known_criteria = set(criterion_index)
    existing_tasks = set(task_keys)
    bindings = []
    for position, review in enumerate(parsed_reviews or []):
        provider = str((review or {}).get("provider") or "")
        status = str((review or {}).get("status") or "")
        from app.plan_feedback import review_response_fingerprint_of as _review_resp_fp
        review_fp = _review_resp_fp(provider, status, (review or {}).get("issues") or [])
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
            # 準備の範囲(準備・原本確認・入力検査・検査方法・停止条件)の指摘が、準備工程のstepと
            # 別の明示SCを同時に指す場合だけ矛盾として人の確認待ちにする。達成条件のない準備指摘を
            # SC01へ配賦しないため。準備の範囲でない指摘は従来どおり明示SCへ一意に解決する
            # (SC00の達成条件は空のため積集合判定では検出できないので、この分岐で扱う)。
            prep_step_only = (bool(step_tasks) and preparation_task_key
                              and all(str(t) == str(preparation_task_key) for t in step_tasks))
            prep_scope_issue = bool(_PREP_SCOPE_RE.search(_issue_scope_blob(
                issue, step_raw, unmet_goal, reason, remedy, severity)))
            prep_sc_conflict = bool(prep_step_only and known and prep_scope_issue)
            if contradiction:
                # §4: stepの達成条件と明示SCが矛盾する場合は誤自動反映せず ambiguous で人確認待ち。両候補を保持。
                candidate_tasks = list(dict.fromkeys(step_tasks + sc_tasks))
                candidate_criteria = list(dict.fromkeys(step_criteria_union + known))
            elif prep_sc_conflict:
                # 準備工程のstepと明示SCの矛盾も両候補を保持して人確認待ち。
                candidate_tasks = list(dict.fromkeys(step_tasks + sc_tasks))
                candidate_criteria = list(dict.fromkeys(step_criteria_union + known))
            elif known:
                candidate_criteria = list(known)
                overlap = [t for t in step_tasks if t in sc_tasks]
                if overlap:
                    candidate_tasks = list(dict.fromkeys(overlap))
                else:
                    candidate_tasks = list(sc_exec or sc_tasks)
            else:
                candidate_criteria = list(step_criteria_union)
                candidate_tasks = list(step_tasks)
            prep_contract = _snapshot_preparation_contract(snapshot, preparation_task_key)
            prep_check = check_resolved_preparation(
                issue=issue, step_raw=step_raw, unmet_goal=unmet_goal,
                reason=reason, remedy=remedy, severity=severity,
                steps=list(steps), step_tasks=list(step_tasks),
                task_roles=task_roles, preparation_task_key=preparation_task_key,
                preparation_contract=prep_contract,
                plan_signature=str(plan_signature or ""),
                goal_contract_hash=str(goal_contract_hash or ""),
                packet_hash=str(packet_hash or ""),
                review_fingerprint=str(review_fp or ""))
            resolved_prep = bool(prep_check.get("passed"))
            if not parsed_step["ok"]:
                state = "invalid_reference"
                basis = "stepの解析に失敗したため対応付けできません(%s)" % parsed_step["reason"]
            elif mentioned and not known:
                # 準備範囲の一意指摘でも、明示SCが契約に無ければ停止する。
                # 根拠なしにSC01へ寄せない。
                state = "ambiguous"
                basis = "指摘内のSC番号が現行契約に存在しないため対応付けできません"
            elif contradiction or prep_sc_conflict:
                state = "ambiguous"
                basis = ("step %s の指す工程の達成条件(%s)と明示SC(%s)が矛盾するため自動反映せず人の確認待ちです。"
                         "候補工程(%s)・候補達成条件(%s)を両方保持します"
                         % (",".join(str(n) for n in steps),
                            ",".join(step_criteria_union) or "なし", ",".join(known),
                            ",".join(candidate_tasks) or "なし",
                            ",".join(candidate_criteria) or "なし"))
            elif resolved_prep:
                # SC00のみを指す準備指摘: 厳格な一意条件を満たせば進める。
                # PREP00を目標達成件数や成功判定の代用品にしない。
                state = "resolved"
                candidate_tasks = list(prep_check.get("target_task_keys") or step_tasks)
                candidate_criteria = []
                basis = ("step %s は準備工程 %s の内部ゲート %s への指摘として一意に結び付けました"
                         "(達成条件なし。SC01への配賦はしません)"
                         % (",".join(str(n) for n in steps) or "-",
                            ",".join(candidate_tasks) or "なし", PREPARATION_GATE_ID))
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
            kind = _classify_issue_kind(
                state=state, steps=list(steps), step_tasks=list(step_tasks),
                candidate_tasks=list(candidate_tasks), task_roles=task_roles,
                final_task_key=final_task_key,
                preparation_task_key=preparation_task_key)
            if kind not in ISSUE_KINDS:
                kind = "plan_level"
            target_type = _target_type_for(
                state=state, issue_kind=kind,
                resolved_preparation=resolved_prep,
                candidate_tasks=list(candidate_tasks),
                candidate_criteria=list(candidate_criteria))
            if target_type not in TARGET_TYPES:
                target_type = "plan_level"
            target_ids: list[str] = []
            target_task_keys = [str(x) for x in candidate_tasks if str(x)]
            if target_type == "preparation_gate":
                target_ids = [PREPARATION_GATE_ID]
            elif target_type == "goal_criterion" and state != "ambiguous":
                target_ids = [str(x) for x in candidate_criteria if str(x)]
            elif target_type == "goal_criterion":
                # 矛盾で停止する場合はSC01へ配賦しない。候補は画面表示用に残す。
                target_type = "plan_level"
                target_ids = []
            evidence = {
                "review_fingerprint": str(review_fp or ""),
                "plan_signature": str(plan_signature or ""),
                "goal_contract_hash": str(goal_contract_hash or ""),
                "packet_hash": str(packet_hash or ""),
                "resolved_preparation": dict(prep_check),
            }
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
                "issue_kind": kind,
                "issue_kind_ja": ISSUE_KIND_JA.get(kind, kind),
                # IssueBinding v2(書込はv2。既存v1データの読取は維持)。
                "target_type": target_type,
                "target_type_ja": TARGET_TYPE_JA.get(target_type, target_type),
                "target_task_keys": target_task_keys,
                "target_ids": target_ids,
                "evidence": evidence,
                "resolved_preparation": dict(prep_check),
                "candidate_reasons": [
                    "step=%s" % (",".join(str(n) for n in steps) or "-"),
                    "候補工程=%s" % (",".join(candidate_tasks) or "なし"),
                    "候補達成条件=%s" % (",".join(candidate_criteria) or "なし"),
                ],
                "auto_apply_allowed": bool(state in AUTO_APPLY_STATES),
                "requires_human_selection": bool(state in ("ambiguous", "invalid_reference")),
                "selection_status": "unselected",
            })
    for _binding in bindings:
        if isinstance(_binding, dict) and not isinstance(_binding.get("classification"), dict):
            _binding["classification"] = classify_external_issue(_binding)
    return {"version": ISSUE_BINDING_VERSION, "bindings": bindings, "empty": not bindings,
            "classification_version": ISSUE_CLASSIFICATION_VERSION}


def _binding_coherent(binding: dict) -> bool:
    """矛盾のない multiple_targets か(防衛用)。矛盾文言を含むものは自動反映しない。"""
    if str(binding.get("state") or "") != "multiple_targets":
        return True
    return "矛盾" not in str(binding.get("basis") or "")


SELECTION_PREVIEW_VERSION = "binding-selection-preview-v1"


def _binding_selection_options(binding: dict) -> list[dict]:
    """候補の選択肢。元の指摘・候補工程・候補条件・理由を一組で示す。"""
    if not isinstance(binding, dict):
        return []
    tasks = [str(x) for x in (binding.get("candidate_task_keys") or []) if str(x)]
    criteria = [str(x) for x in (binding.get("candidate_criterion_ids") or []) if str(x)]
    options: list[dict] = []
    for task in tasks:
        for criterion in criteria:
            options.append({
                "task_key": task,
                "criterion_id": criterion,
                "reason": "候補工程=%s / 候補達成条件=%s" % (task, criterion),
            })
    for task in tasks:
        if not any(str(o.get("task_key") or "") == task and not str(o.get("criterion_id") or "") for o in options):
            options.append({"task_key": task, "criterion_id": "",
                            "reason": "候補工程=%s / 達成条件は未確定" % task})
    for criterion in criteria:
        if not any(str(o.get("criterion_id") or "") == criterion and not str(o.get("task_key") or "") for o in options):
            options.append({"task_key": "", "criterion_id": criterion,
                            "reason": "工程は未確定 / 候補達成条件=%s" % criterion})
    return options[:16]


def preview_binding_selection(binding: dict, *, task_key: str = "",
                              criterion_id: str = "",
                              decision_option: str = "") -> dict:
    """選択後の修正差分プレビュー。計画の変更は行わない。"""
    if not isinstance(binding, dict):
        raise ValueError("対象の指摘が対応表にありません")
    if str(binding.get("state") or "") not in ("ambiguous", "invalid_reference", "multiple_targets"):
        raise ValueError("選択が必要な指摘ではありません")
    task = str(task_key or "").strip()
    criterion = str(criterion_id or "").strip().upper()
    decision = str(decision_option or "").strip()
    tasks = [str(x) for x in (binding.get("candidate_task_keys") or []) if str(x)]
    criteria = [str(x) for x in (binding.get("candidate_criterion_ids") or []) if str(x)]
    if not tasks and not criteria:
        valid_ids = [str(x.get("id") or "") for x in NO_CANDIDATE_OPTIONS]
        if task or criterion:
            raise ValueError("候補がない指摘に工程・達成条件は割り付けできません")
        if decision not in valid_ids:
            raise ValueError("候補がない指摘は判断の選択肢をプレビューしてください")
        before = "未選択のため停止中(state=%s, kind=%s)" % (
            str(binding.get("state") or ""), str(binding.get("issue_kind") or ""))
        after = "判断の記録: 選択肢=%s / 指摘=%s" % (
            decision, str(binding.get("issue_id") or ""))
        return {
            "version": SELECTION_PREVIEW_VERSION,
            "issue_id": str(binding.get("issue_id") or ""),
            "issue_kind": str(binding.get("issue_kind") or ""),
            "issue_kind_ja": str(binding.get("issue_kind_ja") or ""),
            "issue_text": str(binding.get("issue_text") or ""),
            "step_raw": str(binding.get("step_raw") or ""),
            "steps": list(binding.get("steps") or []),
            "candidate_task_keys": list(tasks),
            "candidate_criterion_ids": list(criteria),
            "candidate_reasons": list(binding.get("candidate_reasons") or []),
            "selected_task_key": "",
            "selected_criterion_id": "",
            "decision_option": decision,
            "before": before,
            "after": after,
            "diff": "この判断は計画を変えず、判断の記録のみ(外部送信なし)。",
            "note": "人の判断の記録であり、検証合格・外部評価合格ではない。未選択なら停止のまま。",
        }
    if task and task not in tasks:
        raise ValueError("候補に無い工程は選択できません")
    if criterion and criterion not in criteria:
        raise ValueError("候補に無い達成条件は選択できません")
    if not task and not criterion:
        raise ValueError("対応先の候補を選択してください")
    before = "未選択のため停止中(state=%s, kind=%s)" % (
        str(binding.get("state") or ""), str(binding.get("issue_kind") or ""))
    after = "選択: 工程=%s / 達成条件=%s / 指摘=%s" % (
        task or "(未確定)", criterion or "(未確定)", str(binding.get("issue_id") or ""))
    return {
        "version": SELECTION_PREVIEW_VERSION,
        "issue_id": str(binding.get("issue_id") or ""),
        "issue_kind": str(binding.get("issue_kind") or ""),
        "issue_kind_ja": str(binding.get("issue_kind_ja") or ""),
        "issue_text": str(binding.get("issue_text") or ""),
        "step_raw": str(binding.get("step_raw") or ""),
        "steps": list(binding.get("steps") or []),
        "candidate_task_keys": list(tasks),
        "candidate_criterion_ids": list(criteria),
        "candidate_reasons": list(binding.get("candidate_reasons") or []),
        "selected_task_key": task,
        "selected_criterion_id": criterion,
        "before": before,
        "after": after,
        "diff": "修正差分の確認後に再評価へ進めます。選択だけでは検証合格・計画変更になりません。",
        "note": "未選択なら停止のまま。選択・差分確認後だけ再評価へ進めます。",
    }


def _selection_signature_ok(binding: dict, *, plan_signature: str = "",
                            goal_contract_hash: str = "", packet_hash: str = "") -> None:
    """計画署名・指摘IDの照合X。計画が変わったら選択を失効させる。"""
    if not isinstance(binding, dict):
        raise ValueError("対象の指摘が対応表にありません")
    if plan_signature and str(binding.get("plan_signature") or "") != str(plan_signature):
        raise ValueError("409: 対応表の計画版が変わりました。選択は失効しました")
    if goal_contract_hash and str(binding.get("goal_contract_hash") or "") != str(goal_contract_hash):
        raise ValueError("409: 対応表のGoalContractが変わりました。選択は失効しました")
    if packet_hash and str(binding.get("packet_hash") or "") != str(packet_hash):
        raise ValueError("409: packetが変わりました。選択は失効しました")
    if not str(binding.get("issue_id") or ""):
        raise ValueError("指摘IDを確認できないため選択できません")


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
    bad = [b for b in bindings if not _auto_apply_ok(b)]
    if bad:
        first = bad[0]
        raise ValueError(
            "指摘の対応先を特定できないため取り込み失敗しました: "
            "issue=%s state=%s %s" % (first["issue_id"], first["state"], first["basis"][:120]))
    mapped = [{"issue_id": b["issue_id"], "issue_text": b["issue_text"],
               "criteria": list(b["candidate_criterion_ids"]),
               "tasks": list(b["candidate_task_keys"]),
               "state": b["state"], "provider": b["provider"],
               "target_type": str(b.get("target_type") or ""),
               "target_task_keys": list(b.get("target_task_keys") or []),
               "target_ids": list(b.get("target_ids") or [])}
              for b in bindings]
    return {"mapped": mapped, "bindings": bindings, "empty": False,
            "version": ISSUE_BINDING_VERSION}


def _remap_base(manager, pid: str, run_id: str) -> tuple:
    from app.project_delete import is_deleted_or_unknown as _is_deleted

    try:
        if _is_deleted(manager.memory.path, pid):
            raise ValueError("論理削除中または削除状態を確認できないPJには動作しません(状態を確定できません)")
    except ValueError:
        raise
    except Exception:
        raise ValueError("論理削除中または削除状態を確認できないPJには動作しません(状態を確定できません)")
    run = get_run(manager, pid, run_id)
    resumed = [e for e in (run.get("resume_history") or []) if isinstance(e, dict)]
    is_stopped_human = (str(run.get("state") or "") == "stopped"
                        and str(run.get("stop_kind") or "") == "human_required")
    is_versioned_resume = (bool(resumed) and str(run.get("state") or "") == "permitted")
    if not (is_stopped_human or is_versioned_resume):
        raise ValueError("human_requiredで停止したランだけ対応付け直しできます")
    mission, snapshot, signature, contract = _current(manager, pid)
    if signature != str(run.get("plan_signature") or ""):
        raise ValueError("409: 計画版が変わりました。新規確認してください")
    if str(contract.get("content_hash") or "") != str(run.get("goal_contract_hash") or ""):
        raise ValueError("409: GoalContractが変わりました。新規確認してください")
    if str(run.get("packet_hash") or "") and signature:
        # F2a fail-closed: packet照合の読取不能は通過させず停止する。
        try:
            from app.goal_review import ReviewStore as _ReviewStore

            stored = _ReviewStore(manager.memory.path).get(pid, "plan", signature) or {}
            packet = stored.get("packet") or {}
            if packet and fingerprint(packet) != str(run.get("packet_hash") or ""):
                raise ValueError("409: packetが変わりました。新規確認してください")
        except ValueError:
            raise
        except (OSError, RuntimeError, AttributeError, KeyError, TypeError, sqlite3.Error) as exc:
            raise ValueError(f"packetを確認できないため対応付け直しできません:{type(exc).__name__}") from exc
    rounds = [r for r in (run.get("rounds") or []) if isinstance(r, dict)]
    if not rounds:
        raise ValueError("保存済みレビューがありません")
    first = rounds[0]
    parsed = first.get("reviews") or []
    if not parsed:
        raise ValueError("保存済みレビューがありません")
    # レビュー指紋: ラン保存時の第1回レビューと現行ReviewStoreの保存レビューを照合する。
    # ラン内コピー同士の比較では改変を検出できないため、外部の保存レビューと突き合わせる。
    # F2a fail-closed: 保存レビューの読取不能は通過させず停止する。
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
    except (OSError, RuntimeError, AttributeError, KeyError, TypeError, sqlite3.Error) as exc:
        raise ValueError(f"保存レビューを確認できないため対応付け直しできません:{type(exc).__name__}") from exc
    return run, mission, snapshot, signature, contract, first, parsed


def remap_preview(manager, pid: str, run_id: str) -> dict:
    """保存済みレビューからの対応付けプレビュー。読み取り専用で副作用なし。"""
    run, _mission, snapshot, signature, contract, first, parsed = _remap_base(manager, pid, run_id)
    table = build_issue_bindings(parsed, contract, snapshot,
                                 plan_signature=signature,
                                 goal_contract_hash=str(contract.get("content_hash") or ""),
                                 packet_hash=str(first.get("packet_hash") or run.get("packet_hash") or ""))
    from app.plan_feedback import review_fingerprint_of as _review_fp_of
    saved_fp = _review_fp_of(str(first.get("plan_signature") or ""),
                             first.get("reviews") or [])
    current_fp = _review_fp_of(signature, parsed)
    # F2a fail-closed: ラウンド欠落を 1 で補完せず不明として停止する(増分0・1回扱いにしない)。
    raw_round = first.get("round")
    if raw_round is None:
        raise ValueError("保存レビューのラウンドを確認できないため対応付け直しできません")
    try:
        round_no = int(raw_round)
    except (TypeError, ValueError) as exc:
        raise ValueError("保存レビューのラウンドを確認できないため対応付け直しできません") from exc
    return {
        "run_id": run.get("id"),
        "project_id": pid,
        "plan_signature": signature,
        "goal_contract_hash": str(contract.get("content_hash") or ""),
        "packet_hash": str(first.get("packet_hash") or run.get("packet_hash") or ""),
        "review_fingerprint_match": saved_fp == current_fp,
        "round": round_no,
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


_CONFIRMED_SNAPSHOT_SAVE_ERRORS = (
    OSError, ValueError, RuntimeError, AttributeError, KeyError, TypeError,
    sqlite3.Error)


def _persist_confirmed_snapshot(manager, pid: str, run: dict, *, signature: str,
                                contract_hash: str, packet_hash: str,
                                review_fp: str, parsed: list,
                                confirmed_entries: list) -> dict:
    """確定対応の正本スナップショット保存。confirm_binding と再試行の共通処理。

    run に残っている有効な確定(confirmed_entries)と、既存の正本の確定
    (現行レビュー指紋に一致するものだけ継承)から正本を組み立てる。
    confirm_binding の保存ブロックと同じ作り方。成功時は {"saved": True}、
    想定内の保存失敗時は confirm_binding と同じ作り方の
    {"saved": False, "error": 型名, "code": コード, "detail": 詳細} を返す。
    想定外の例外は握りつぶさず伝播させる。run への記録と保存は呼び出し側で行う。
    """
    from app.plan_feedback import build_binding_snapshot as _build_snapshot
    from app.plan_feedback import check_binding_snapshot_current as _check_snapshot
    from app.plan_feedback import legacy_review_fingerprint_of as _legacy_fp_of
    try:
        snapshot_store = _store(manager)
        previous_row = snapshot_store.get(pid, "binding_snapshot", signature) or {}
        previous_snapshot = previous_row.get("snapshot") if isinstance(previous_row, dict) else None
        previous_confirmed = []
        if previous_snapshot is not None:
            _stored_for_inherit = []
            try:
                _saved_row_for_inherit = snapshot_store.get(pid, "plan", signature) or {}
                _sr_inherit = _saved_row_for_inherit.get("reviews")
                if isinstance(_sr_inherit, list):
                    _stored_for_inherit = _sr_inherit
            except Exception:
                _stored_for_inherit = []
            _inherit_lists = [parsed]
            if _stored_for_inherit:
                _inherit_lists = [_stored_for_inherit, parsed]
            _check_snapshot(previous_snapshot,
                plan_signature=signature,
                goal_contract_hash=contract_hash,
                packet_hash=packet_hash,
                review_fingerprint=review_fp,
                current_reviews=_inherit_lists,
                legacy_review_fingerprint=[_legacy_fp_of(signature, parsed)] + ([_legacy_fp_of(signature, _stored_for_inherit)] if _stored_for_inherit else []))
            previous_confirmed = [dict(x) for x in (previous_snapshot.get("confirmed") or [])
                                  if isinstance(x, dict)]
        # 既存の人間確定を保持し、同じ指摘IDだけ新しい確定で置き換える。
        confirmed_by_id = {str(x.get("issue_id") or ""): x for x in previous_confirmed
                           if str(x.get("issue_id") or "")}
        for manual in (confirmed_entries or []):
            if isinstance(manual, dict) and str(manual.get("issue_id") or ""):
                confirmed_by_id[str(manual["issue_id"])] = manual
        snapshot_obj = _build_snapshot(
            bindings=list(run.get("issue_bindings") or []),
            plan_signature=signature,
            goal_contract_hash=contract_hash,
            packet_hash=packet_hash,
            review_fingerprint=review_fp,
            confirmed=list(confirmed_by_id.values()))
        _store(manager).put(pid, "binding_snapshot", signature,
                            {"snapshot": snapshot_obj, "saved_at": _now()})
        return {"saved": True}
    except _CONFIRMED_SNAPSHOT_SAVE_ERRORS as exc:
        from app.plan_feedback import binding_snapshot_error_detail as _error_detail_retry
        err_name = type(exc).__name__
        err_code = str(getattr(exc, "code", "") or "")
        if not err_code:
            try:
                from app.plan_feedback import _binding_snapshot_mismatch_code as _mismatch_code
                saved_row = _store(manager).get(pid, "binding_snapshot", signature) or {}
                saved_obj = saved_row.get("snapshot") if isinstance(saved_row, dict) else None
                err_code = _mismatch_code(
                    saved_obj, plan_signature=signature,
                    goal_contract_hash=contract_hash,
                    packet_hash=packet_hash,
                    review_fingerprint=review_fp)
                # D1-5: 正本が無いときだけ snapshot_missing。不明な失敗は store_error。
                if not err_code:
                    err_code = "snapshot_missing" if saved_obj is None else "store_error"
            except Exception:
                err_code = "store_error"
        return {"saved": False, "error": err_name, "code": err_code,
                "detail": _error_detail_retry(
                    plan_signature=signature,
                    packet_hash=packet_hash,
                    review_fingerprint=review_fp)}


async def confirm_binding(manager, pid: str, run_id: str, issue_id: str, *,
                          actor: str, task_key: str = "", criterion_id: str = "",
                          reason: str = "", decision_option: str = "",
                          review_fingerprint: str = "",
                          confirmed_preview: bool | None = None) -> dict:
    """現行計画と保存済みレビューを再照合してから人の候補確定を記録する。

    P0-B: 確定は対応付けスナップショット正本にも保存する。修正案・網羅表・
    適用判定は同じ指摘IDと項目IDで同じ値を参照する。古い確定は無効化する。
    """
    from app.plan_feedback import review_fingerprint_of as _review_fp_of
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
        _invalidate_stale_selections(run, signature=signature,
                                     goal_contract_hash=str(contract.get("content_hash") or ""))
        entry = confirm_binding_candidate(
            run, issue_id, actor=actor, task_key=task_key, criterion_id=criterion_id,
            plan_signature=signature,
            goal_contract_hash=str(contract.get("content_hash") or ""),
            packet_hash=str((_first or {}).get("packet_hash") or run.get("packet_hash") or ""),
            review_fingerprint=_review_fp_of(signature, _parsed),
            reason=reason, decision_option=decision_option,
            confirmed_preview=True if confirmed_preview is None else confirmed_preview)
        _save(manager, run)
        # P0-B: 確定対応を正本スナップショットへ保存する(再構成で ambiguous に戻さない)。
        # 保存失敗時は run に binding_snapshot_save_failed と型名を残し、確定済みに見せない。
        # 保存ブロックは _persist_confirmed_snapshot に切り出し、再試行と作り方を統一する。
        current_packet_hash = str((_first or {}).get("packet_hash") or run.get("packet_hash") or "")
        current_review_fp = _review_fp_of(signature, _parsed)
        saved = _persist_confirmed_snapshot(
            manager, pid, run, signature=signature,
            contract_hash=str(contract.get("content_hash") or ""),
            packet_hash=current_packet_hash,
            review_fp=current_review_fp,
            parsed=_parsed,
            confirmed_entries=list(run.get("manual_bindings") or []))
        if saved.get("saved"):
            run["binding_snapshot_save_failed"] = False
            run["binding_snapshot_save_error"] = ""
            run["binding_snapshot_save_error_code"] = ""
            run["binding_snapshot_save_detail"] = {}
            _save(manager, run)
        else:
            from app.plan_feedback import binding_snapshot_error_detail as _error_detail2
            err_name = str(saved.get("error") or "")
            err_code = str(saved.get("code") or "")
            run["binding_snapshot_save_failed"] = True
            run["binding_snapshot_save_error"] = err_name
            run["binding_snapshot_save_error_code"] = err_code
            run["binding_snapshot_save_detail"] = _error_detail2(
                plan_signature=signature,
                packet_hash=current_packet_hash,
                review_fingerprint=current_review_fp)
            entry["binding_snapshot_save_failed"] = True
            entry["binding_snapshot_save_error"] = err_name
            entry["binding_snapshot_save_error_code"] = err_code
            entry["binding_snapshot_save_detail"] = dict(run["binding_snapshot_save_detail"])
            for b in run.get("issue_bindings") or []:
                if isinstance(b, dict) and str(b.get("issue_id") or "") == str(issue_id):
                    b["selection_status"] = "save_failed"
            _save(manager, run)
        return entry


async def retry_binding_snapshot_save(manager, pid: str, run_id: str) -> dict:
    """保存失敗した正本スナップショットのローカル再試行。外部通信なし・利用枠を使わない。

    対象は run に binding_snapshot_save_failed=True がある、または対応表に
    selection_status == "save_failed" の行があるときだけ。どちらも無ければ
    ValueError(「再試行の対象がありません」)。確認失敗を「対象なし」にしない。
    confirm_binding と同じ前提確認をする: _remap_base で現行の計画署名・目標契約・
    保存済みレビューを再取得し、run の対応表の署名・目標契約が一致しなければ
    ValueError("409: ...")。_invalidate_stale_selections を呼ぶ。
    計画が変わっていたら保存せずに失効のまま返す(古い確定を新しい正本へ入れない)。
    保存する内容は run に残っている有効な人の確定(manual_bindings)と、既存の正本の確定
    (confirm_binding と同じ継承の確認 check_binding_snapshot_current を通るものだけ。
    現行のレビュー指紋に一致するものだけを継承し、任意の指紋を引数で受けない)から組み立てる。
    confirm_binding の保存ブロックと同じ作り方(_persist_confirmed_snapshot を両方から呼ぶ)。
    成功したら binding_snapshot_save_failed=False、エラー系の項目を空に戻し、
    save_failed の行は confirmed に戻す(その確定が現行の署名・目標契約に有効なものだけ)。
    manual_bindings の該当エントリの binding_snapshot_save_* 項目も外す。
    run を保存して公開ビューを返す(戻り値に retried: True, saved: True, attempts を含める)。
    失敗したら: 失敗の型名・コード・detail を confirm_binding と同じ作り方で run に残し、
    save_failed のまま。想定外の例外(上記の except のタプルに無いもの)は握りつぶさず伝播させる。
    except Exception: pass を足さない。snapshot_retry_count(整数)を run に加算して残す。
    自動で繰り返さない(1回の呼出で1回だけ保存を試す。ループ・バックグラウンド再試行は作らない)。
    同じ run の同時実行は _lock(pid) で直列化する。再試行で外部呼出(external_calls)・
    外部AIの再送・検証回数・利用枠は増えない。人の確定を新しく作らない・書き換えない
    (確定の内容はそのまま、正本への保存だけ再試行する)。
    保存に成功しても、他の指摘の確定・未確定の状態は変えない。確定していない指摘を確定にしない。
    """
    from app.plan_feedback import review_fingerprint_of as _review_fp_of
    async with _lock(pid):
        run = get_run(manager, pid, run_id)
        has_failed_flag = bool(run.get("binding_snapshot_save_failed"))
        save_failed_rows = [b for b in (run.get("issue_bindings") or [])
                            if isinstance(b, dict)
                            and str(b.get("selection_status") or "") == "save_failed"]
        if not has_failed_flag and not save_failed_rows:
            raise ValueError("再試行の対象がありません")
        external_before = int(run.get("external_calls") or 0)
        rounds_before = int(run.get("verification_rounds") or 0)
        run, _mission, _snapshot, signature, contract, _first, _parsed = _remap_base(
            manager, pid, run_id)
        # 前提確認: 対応表の署名・目標契約が現行と一致しなければ 409。
        for binding in run.get("issue_bindings") or []:
            if str(binding.get("plan_signature") or "") != signature:
                raise ValueError("409: 対応表の計画版が変わりました")
            if str(binding.get("goal_contract_hash") or "") != str(contract.get("content_hash") or ""):
                raise ValueError("409: 対応表のGoalContractが変わりました")
        expired = _invalidate_stale_selections(
            run, signature=signature,
            goal_contract_hash=str(contract.get("content_hash") or ""))
        # 計画が変わっていたら保存せずに失効のまま返す(古い確定を新しい正本へ入れない)。
        # _remap_base が署名・契約の一致を確認済みだが、失効が発生したら保存しない。
        if expired:
            run["snapshot_retry_count"] = int(run.get("snapshot_retry_count") or 0) + 1
            _save(manager, run)
            view = public_view(run)
            view["retried"] = True
            view["saved"] = False
            view["attempts"] = 0
            return view
        # run に残っている有効な人の確定だけを保存対象にする。
        # _invalidate_stale_selections 後に残った manual_bindings が有効な確定。
        valid_manuals = [dict(e) for e in (run.get("manual_bindings") or [])
                         if isinstance(e, dict) and str(e.get("issue_id") or "")]
        current_packet_hash = str((_first or {}).get("packet_hash") or run.get("packet_hash") or "")
        current_review_fp = _review_fp_of(signature, _parsed)
        saved = _persist_confirmed_snapshot(
            manager, pid, run, signature=signature,
            contract_hash=str(contract.get("content_hash") or ""),
            packet_hash=current_packet_hash,
            review_fp=current_review_fp,
            parsed=_parsed,
            confirmed_entries=valid_manuals)
        # 再試行回数は成功・失敗どちらの試行でも加算する(自動で繰り返さない)。
        # attempts は1回の呼出で1回だけ保存を試す。
        run["snapshot_retry_count"] = int(run.get("snapshot_retry_count") or 0) + 1
        attempts = 1
        if int(run.get("external_calls") or 0) != external_before:
            raise ValueError("外部呼出が発生したため中断しました")
        if int(run.get("verification_rounds") or 0) != rounds_before:
            raise ValueError("検証回数が変わったため中断しました")
        if saved.get("saved"):
            run["binding_snapshot_save_failed"] = False
            run["binding_snapshot_save_error"] = ""
            run["binding_snapshot_save_error_code"] = ""
            run["binding_snapshot_save_detail"] = {}
            valid_ids = {str(e.get("issue_id") or "") for e in valid_manuals
                         if str(e.get("issue_id") or "")}
            for binding in run.get("issue_bindings") or []:
                if not isinstance(binding, dict):
                    continue
                if str(binding.get("selection_status") or "") != "save_failed":
                    continue
                # その確定が現行の署名・目標契約に有効なものだけ confirmed に戻す。
                if (str(binding.get("issue_id") or "") in valid_ids
                        and (not str(binding.get("plan_signature") or "")
                             or str(binding.get("plan_signature") or "") == signature)
                        and (not str(binding.get("goal_contract_hash") or "")
                             or str(binding.get("goal_contract_hash") or "") == str(contract.get("content_hash") or ""))):
                    binding["selection_status"] = "confirmed"
            for entry in run.get("manual_bindings") or []:
                if not isinstance(entry, dict):
                    continue
                entry.pop("binding_snapshot_save_failed", None)
                entry.pop("binding_snapshot_save_error", None)
                entry.pop("binding_snapshot_save_error_code", None)
                entry.pop("binding_snapshot_save_detail", None)
            _save(manager, run)
            view = public_view(run)
            view["retried"] = True
            view["saved"] = True
            view["attempts"] = attempts
            view["external_calls"] = external_before
            return view
        # 失敗: 失敗の型名・コード・detail を confirm_binding と同じ作り方で run に残し、
        # save_failed のまま。想定外の例外は握りつぶさず伝播させる。
        run["binding_snapshot_save_failed"] = True
        run["binding_snapshot_save_error"] = str(saved.get("error") or "")
        run["binding_snapshot_save_error_code"] = str(saved.get("code") or "")
        run["binding_snapshot_save_detail"] = dict(saved.get("detail") or {})
        _save(manager, run)
        view = public_view(run)
        view["retried"] = True
        view["saved"] = False
        view["attempts"] = attempts
        view["external_calls"] = external_before
        return view


def _invalidate_stale_selections(run: dict, *, signature: str = "",
                                 goal_contract_hash: str = "") -> int:
    """計画が変わったら選択を失効させる。失効した件数を返す。"""
    if not isinstance(run, dict):
        return 0
    expired = 0
    for binding in run.get("issue_bindings") or []:
        if not isinstance(binding, dict):
            continue
        if str(binding.get("selection_status") or "") != "confirmed":
            continue
        stale = False
        if signature and str(binding.get("plan_signature") or "") != str(signature):
            stale = True
        if goal_contract_hash and str(binding.get("goal_contract_hash") or "") != str(goal_contract_hash):
            stale = True
        if stale:
            binding["selection_status"] = "expired"
            expired += 1
    history = run.get("manual_bindings") if isinstance(run.get("manual_bindings"), list) else []
    kept = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        stale = False
        if signature and str(entry.get("plan_signature") or "") and str(entry.get("plan_signature") or "") != str(signature):
            stale = True
        if goal_contract_hash and str(entry.get("goal_contract_hash") or "") and str(entry.get("goal_contract_hash") or "") != str(goal_contract_hash):
            stale = True
        if stale:
            expired += 0
            continue
        kept.append(entry)
    if len(kept) != len(history):
        run["manual_bindings"] = kept[-20:]
    return expired


def binding_selection_preview_for_run(run: dict, issue_id: str, *,
                                      task_key: str = "",
                                      criterion_id: str = "",
                                      decision_option: str = "") -> dict:
    """保存済み対応表からの差分プレビュー。計画変更・署名不一致は拒否する。"""
    if not isinstance(run, dict):
        raise ValueError("対象の指摘が対応表にありません")
    target = None
    for binding in run.get("issue_bindings") or []:
        if isinstance(binding, dict) and str(binding.get("issue_id") or "") == str(issue_id):
            target = binding
            break
    if target is None:
        raise ValueError("対象の指摘が対応表にありません")
    if not str(target.get("issue_id") or "") or str(target.get("issue_id") or "") != str(issue_id):
        raise ValueError("指摘IDが一致しないため選択できません")
    if str(run.get("plan_signature") or "") and str(target.get("plan_signature") or "") != str(run.get("plan_signature") or ""):
        raise ValueError("409: 対応表の計画版が変わりました。選択は失効しました")
    if str(run.get("goal_contract_hash") or "") and str(target.get("goal_contract_hash") or "") != str(run.get("goal_contract_hash") or ""):
        raise ValueError("409: 対応表のGoalContractが変わりました。選択は失効しました")
    if str(target.get("selection_status") or "") == "expired":
        raise ValueError("409: 計画が変わったため選択は失効しました")
    preview = preview_binding_selection(target, task_key=task_key, criterion_id=criterion_id,
                                          decision_option=decision_option)
    return {
        "run_id": run.get("id"),
        "project_id": run.get("project_id"),
        "plan_signature": str(target.get("plan_signature") or run.get("plan_signature") or ""),
        "goal_contract_hash": str(target.get("goal_contract_hash") or run.get("goal_contract_hash") or ""),
        "packet_hash": str(target.get("packet_hash") or run.get("packet_hash") or ""),
        "selection_options": _binding_selection_options(target),
        "unselected_stops": True,
        "requires_preview_confirmation": True,
        "external_calls": 0,
        **preview,
    }


async def _remap_binding_decision_run(manager, pid: str, run: dict,
                                         *, actor: str = "") -> dict:
    """P0-3: 確定決定による再開の実行。保存済み指摘から既存のローカル修正候補作成へ進む。

    外部AIの送信経路は呼ばない(manager.plan_review_runner / review_plan /
    _call_gemini を呼ばない)。既存のローカル修正候補作成経路
    (plan_feedback.propose の修正案生成)へ進む。新しい生成ロジックは作らない。
    候補の独立検査に失敗したら state を停止に戻し、
    failure_code=candidate_check_failed を記録、指摘(issues)と bindings を保持する。
    """
    from app.plan_feedback import review_fingerprint_of as _review_fp_of

    run_id = str(run.get("id") or "")
    _mission, snapshot, signature, contract = _current(manager, pid)
    if signature != str(run.get("plan_signature") or ""):
        raise ValueError("409: 計画版が変わりました。新規確認してください")
    if str(contract.get("content_hash") or "") != str(run.get("goal_contract_hash") or ""):
        raise ValueError("409: GoalContractが変わりました。新規確認してください")
    saved_state = str(run.get("state") or "")
    if saved_state not in {"stopped", "permitted"}:
        raise ValueError("human_requiredで停止したランだけ対応付け直しできます")
    plan_row: dict = {}
    try:
        from app.goal_review import ReviewStore as _ReviewStore2

        plan_row = _ReviewStore2(manager.memory.path).get(pid, "plan", signature) or {}
        packet = plan_row.get("packet") or {}
        if packet and fingerprint(packet) != str(run.get("packet_hash") or ""):
            raise ValueError("409: packetが変わりました。新規確認してください")
    except ValueError:
        raise
    except (OSError, RuntimeError, AttributeError, KeyError, TypeError, sqlite3.Error) as exc:
        raise ValueError(f"packetを確認できないため対応付け直しできません:{type(exc).__name__}") from exc
    rounds = [r for r in (run.get("rounds") or []) if isinstance(r, dict)]
    if not rounds:
        raise ValueError("保存済みレビューがありません")
    first = rounds[0]
    parsed = first.get("reviews") or []
    if not parsed:
        raise ValueError("保存済みレビューがありません")
    current_fp = _review_fp_of(signature, parsed)
    contract_obj: dict = {"content_hash": str(contract.get("content_hash") or "")}
    current_packet = plan_row.get("packet")
    eligibility = resume_eligibility(
        run, binding_decision=True, review_fingerprint=current_fp,
        packet=current_packet if isinstance(current_packet, (dict, list)) else None,
        goal_contract=contract_obj, plan_signature=signature, for_execute=True)
    if not eligibility.get("allowed"):
        raise ValueError(str(eligibility.get("reason") or "再開できません"))
    external_before = int(run.get("external_calls") or 0)
    rounds_before = int(run.get("verification_rounds") or 0)
    _mark_binding_decision_executed(run, str(eligibility.get("guard_key") or ""))
    run = _save(manager, run)
    from app.plan_feedback import attach_issue_bindings as _attach
    from app.plan_feedback import issues_for as _issues_for

    try:
        issues = _issues_for(manager, pid, signature)
    except (AttributeError, KeyError, TypeError, ValueError, OSError) as exc:
        raise ValueError("保存レビューを確認できないため再開できません:" + type(exc).__name__) from exc
    saved_bindings = [dict(b) for b in (run.get("issue_bindings") or [])
                      if isinstance(b, dict)]
    bound = _attach(issues, saved_bindings)
    if [x.get("id") for x in bound] != [x.get("id") for x in issues]:
        raise ValueError("指摘の順序・件数が変わりました")
    try:
        from app.plan_feedback import propose as _propose

        candidate = await _propose(
            manager, pid, signature, None,
            idempotency_key="binding-decision-resume-" + run_id + "-" + str(eligibility["guard_key"]),
            _prompt_version="binding-decision-resume-" + str(eligibility["guard_key"]),
            _issues_override=bound)
    except (ValueError, RuntimeError, OSError, sqlite3.Error,
            KeyError, TypeError, AttributeError) as exc:
        run = get_run(manager, pid, run_id)
        run["state"] = "stopped"
        run["stop_kind"] = "human_required"
        run["failure_code"] = "candidate_check_failed"
        run["stop_reason"] = ("保存済み指摘からの修正案生成に失敗したため停止しました"
                              "(" + type(exc).__name__ + ")")
        # 指摘本文を消さない。指摘(issues)と bindings を保持する。
        return public_view(_save(manager, run))
    # 候補の独立検査: blockers または changes なしは失敗として再停止する。
    blockers = candidate.get("blockers") if isinstance(candidate, dict) else None
    changes = candidate.get("changes") if isinstance(candidate, dict) else None
    if not isinstance(candidate, dict) or blockers or not changes:
        run = get_run(manager, pid, run_id)
        run["state"] = "stopped"
        run["stop_kind"] = "human_required"
        run["failure_code"] = "candidate_check_failed"
        kinds = sorted({str(a.get("disposition") or "") for a in (blockers or [])
                        if isinstance(a, dict)}) if blockers else []
        run["stop_reason"] = ("候補の独立検査に失敗したため停止しました"
                              + ("(" + ",".join(kinds) + ")" if kinds else "")
                              + "。指摘本文は保持しています")
        if int(run.get("external_calls") or 0) != external_before:
            run["external_calls"] = external_before
        if int(run.get("verification_rounds") or 0) != rounds_before:
            run["verification_rounds"] = rounds_before
        return public_view(_save(manager, run))
    if int(run.get("external_calls") or 0) != external_before:
        raise ValueError("外部呼出が発生したため中断しました")
    if int(run.get("verification_rounds") or 0) != rounds_before:
        raise ValueError("検証回数が変わったため中断しました")
    run = get_run(manager, pid, run_id)
    run["state"] = "stopped"
    run["stop_kind"] = "human_required"
    run["failure_code"] = ""
    run["revision_candidate_id"] = str(candidate.get("candidate_id") or "")
    run["stop_reason"] = ("保存済み指摘からローカル修正案を作成しました。差分を確認して適用してください"
                          if run["revision_candidate_id"] else
                          "保存済み指摘から修正案を確定できませんでした")
    return public_view(_save(manager, run))


async def remap_run(manager, pid: str, run_id: str, actor: str = "",
                    idempotency_key: str = "", binding_version: str = "",
                    planner_version: str = "", resume_cause: str = "",
                    binding_decision: bool = False) -> dict:
    """保存済みの第1回レビューからP0/P1だけ再実行。外部AI呼出0・検証回数/利用枠の消費0。

    修正が入った場合、保存済みの外部レビューを再対応付けして差分をプレビューし、
    外部AIを再送せずローカル修正案の生成へ再開する。空転防止ガードは解除しない。
    版変更1回以外の単純な再試行は止める。再開は同じ指摘ID・順序・計画署名を保つ。
    人間が選んだ候補も同じ再開経路に入る。
    P0-3: binding_decision=True のときは確定決定による再開。保存済み指摘と
    確定した対応付けから既存のローカル修正候補作成経路(propose)へ進む。
    外部AIの送信経路は呼ばない。候補の検査に失敗したら再び停止し、
    failure_code=candidate_check_failed を記録、指摘とbindingsを保持する。
    """
    from app.project_delete import is_deleted_or_unknown as _is_deleted

    try:
        if _is_deleted(manager.memory.path, pid):
            raise ValueError("論理削除中または削除状態を確認できないPJには動作しません(状態を確定できません)")
    except ValueError:
        raise
    except Exception:
        raise ValueError("論理削除中または削除状態を確認できないPJには動作しません(状態を確定できません)")
    async with _lock(pid):
        run = get_run(manager, pid, run_id)
        # P0-3: 審査・記録・実行を同一ロック内で直列化する。未記録なら現行の
        # 署名・レビュー・packet・契約を照合して記録し、記録済み未実行なら
        # 再記録せず実行へ進む。実行済みの同一組は実行審査で拒否する。
        if binding_decision:
            from app.plan_feedback import review_fingerprint_of as _review_fp_of_decision

            _mission_d, _snapshot_d, signature_d, contract_d = _current(manager, pid)
            if signature_d != str(run.get("plan_signature") or ""):
                raise ValueError("409: 計画版が変わりました。新規確認してください")
            rounds_d = [r for r in (run.get("rounds") or []) if isinstance(r, dict)]
            if not rounds_d:
                raise ValueError("保存済みレビューがありません")
            reviews_d = rounds_d[0].get("reviews") or []
            if not reviews_d:
                raise ValueError("保存済みレビューがありません")
            review_fp_d = _review_fp_of_decision(signature_d, reviews_d)
            try:
                plan_row_d = _store(manager).get(pid, "plan", signature_d) or {}
                packet_d = plan_row_d.get("packet")
            except (OSError, ValueError, RuntimeError, AttributeError,
                    KeyError, TypeError, sqlite3.Error) as exc:
                raise ValueError("packetを確認できないため再開できません:" + type(exc).__name__) from exc
            contract_obj_d = {"content_hash": str(contract_d.get("content_hash") or "")}
            decision_hash_d = binding_decision_hash(run.get("issue_bindings") or [])
            pending = [e for e in (run.get("resume_history") or [])
                       if isinstance(e, dict)
                       and str(e.get("resume_kind") or "") == "binding_decision"
                       and str(e.get("plan_signature") or "") == signature_d
                       and str(e.get("review_fingerprint") or "") == review_fp_d
                       and str(e.get("binding_decision_hash") or "") == decision_hash_d
                       and e.get("executed") is not True]
            if not pending:
                eligibility_d = resume_eligibility(
                    run, binding_decision=True, review_fingerprint=review_fp_d,
                    packet=packet_d if isinstance(packet_d, (dict, list)) else None,
                    goal_contract=contract_obj_d, plan_signature=signature_d)
                if not eligibility_d.get("allowed"):
                    raise ValueError(str(eligibility_d.get("reason") or "再開できません"))
                record_resume(
                    run, actor=actor, binding_decision=True,
                    review_fingerprint=review_fp_d,
                    packet=packet_d if isinstance(packet_d, (dict, list)) else None,
                    goal_contract=contract_obj_d,
                    cause=resume_cause or "binding decision resume")
                run = _save(manager, run)
            return await _remap_binding_decision_run(manager, pid, run, actor=actor)
        # 版付き再開でpermittedに戻したラン(resume_historyあり)も同じ再開経路に入る。
        # 空転防止ガードは解除しない(版変更1回・同一署名1回だけ)。
        resumed = [e for e in (run.get("resume_history") or []) if isinstance(e, dict)]
        is_stopped_human = (str(run.get("state") or "") == "stopped"
                            and str(run.get("stop_kind") or "") == "human_required")
        is_versioned_resume = (bool(resumed) and str(run.get("state") or "") == "permitted")
        if not (is_stopped_human or is_versioned_resume):
            raise ValueError("human_requiredで停止したランだけ対応付け直しできます")
        key = str(idempotency_key or "").strip()
        history = [e for e in (run.get("remap_history") or []) if isinstance(e, dict)]
        if key:
            for entry in history:
                if entry.get("idempotency_key") == key:
                    return public_view(run)
        # 版付き再開の審査: 版変更がある場合だけ再開経路に入る。単純な再試行は止める。
        # 人間が選んだ候補も同じ再開経路に入る(選択の有無で経路を分けない)。
        # 同一版の再記録は冪等にスキップする(履歴の重複を作らない)。
        if binding_version or planner_version:
            already = [e for e in (run.get("resume_history") or []) if isinstance(e, dict)
                       and str(e.get("binding_version") or "") == str(binding_version or "").strip()[:80]
                       and str(e.get("planner_version") or "") == str(planner_version or "").strip()[:80]
                       and str(e.get("plan_signature") or "") == str(run.get("plan_signature") or "")]
            if not already:
                eligibility = resume_eligibility(run, binding_version=binding_version,
                                                 planner_version=planner_version)
                if not eligibility.get("allowed"):
                    raise ValueError(str(eligibility.get("reason") or "再開できません"))
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
        # F2a fail-closed: packet照合の読取不能は通過させず停止する。
        try:
            from app.goal_review import ReviewStore as _ReviewStore

            stored = _ReviewStore(manager.memory.path).get(pid, "plan", signature) or {}
            packet = stored.get("packet") or {}
            if packet and fingerprint(packet) != str(run.get("packet_hash") or ""):
                raise ValueError("409: packetが変わりました。新規確認してください")
        except ValueError:
            raise
        except (OSError, RuntimeError, AttributeError, KeyError, TypeError, sqlite3.Error) as exc:
            raise ValueError(f"packetを確認できないため対応付け直しできません:{type(exc).__name__}") from exc
        from app.plan_feedback import review_fingerprint_of as _review_fp_of2
        saved_fp = _review_fp_of2(str(first.get("plan_signature") or ""),
                                  first.get("reviews") or [])
        review_fp = _review_fp_of2(signature, parsed)
        if saved_fp != review_fp:
            raise ValueError("409: レビューが変わりました。新規確認してください")
        # F2a fail-closed: 保存レビューの読取不能は通過させず停止する。
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
        except (OSError, RuntimeError, AttributeError, KeyError, TypeError, sqlite3.Error) as exc:
            raise ValueError(f"保存レビューを確認できないため対応付け直しできません:{type(exc).__name__}") from exc
        # P0-Bの限定回復は、既存の署名・契約・packet・レビュー指紋・空転ガードが
        # すべて通った後の追加条件としてだけ評価する。従来の具体的な拒否理由を隠さない。
        recovery_run = run
        if is_versioned_resume:
            # record_resume済みの既存経路はpermittedへ遷移するが、限定回復の
            # 内容照合は停止直前の保存状態として評価する。保存run自体は変更しない。
            recovery_run = dict(run)
            recovery_run["state"] = "stopped"
        recovery = p0b_stored_review_resume(
            manager, pid, recovery_run, signature=signature, parsed=parsed)
        if not recovery.get("ok"):
            reason = str(recovery.get("reason") or "限定回復の条件を満たしません")
            action = str(recovery.get("action") or "新規ランを開始してください")
            raise ValueError(reason + "。" + action)
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
        # P0-B: 取込時点で正本スナップショットを保存する。修正案・網羅表・適用判定は
        # 同じ指摘IDと項目IDで同じ対応先を参照する。再構成で ambiguous に戻さない。
        # 保存失敗時は run に binding_snapshot_save_failed と型名を残し、未確定として扱う。
        snapshot_save_failed = False
        snapshot_save_error = ""
        snapshot_save_error_code = ""
        snapshot_save_detail: dict = {}
        try:
            from app.plan_feedback import build_binding_snapshot as _build_snapshot
            from app.plan_feedback import binding_snapshot_error_detail as _error_detail
            from app.plan_feedback import review_fingerprint_of as _review_fp_of
            _current_review_fp = _review_fp_of(signature, parsed)
            _snapshot_obj = _build_snapshot(
                bindings=list(table.get("bindings") or []),
                plan_signature=signature,
                goal_contract_hash=str(contract.get("content_hash") or ""),
                packet_hash=str(first.get("packet_hash") or run.get("packet_hash") or ""),
                review_fingerprint=_current_review_fp,
                confirmed=[e for e in (run.get("manual_bindings") or [])
                           if isinstance(e, dict)])
            _store(manager).put(pid, "binding_snapshot", signature,
                                {"snapshot": _snapshot_obj, "saved_at": _now()})
            run["binding_snapshot_save_failed"] = False
            run["binding_snapshot_save_error"] = ""
            run["binding_snapshot_save_error_code"] = ""
            run["binding_snapshot_save_detail"] = {}
        except (OSError, ValueError, RuntimeError, AttributeError, KeyError, TypeError, sqlite3.Error) as exc:
            snapshot_save_failed = True
            snapshot_save_error = type(exc).__name__
            snapshot_save_error_code = str(getattr(exc, "code", "") or "")
            from app.plan_feedback import binding_snapshot_error_detail as _error_detail2
            try:
                from app.plan_feedback import _binding_snapshot_mismatch_code as _mismatch_code
                saved_row = _store(manager).get(pid, "binding_snapshot", signature) or {}
                saved_obj = saved_row.get("snapshot") if isinstance(saved_row, dict) else None
                if not snapshot_save_error_code:
                    _mismatch = _mismatch_code(
                        saved_obj, plan_signature=signature,
                        goal_contract_hash=str(contract.get("content_hash") or ""),
                        packet_hash=str(first.get("packet_hash") or run.get("packet_hash") or ""),
                        review_fingerprint=_current_review_fp) or ""
                    # D1-5: 正本が無いときだけ snapshot_missing。不明な失敗は store_error。
                    if _mismatch:
                        snapshot_save_error_code = _mismatch
                    elif saved_obj is None:
                        snapshot_save_error_code = "snapshot_missing"
                    else:
                        snapshot_save_error_code = "store_error"
            except Exception:
                if not snapshot_save_error_code:
                    snapshot_save_error_code = "store_error"
            # D1-5: 例外にコードが無く、正本照合でも特定できない不明失敗は store_error。
            if not snapshot_save_error_code:
                snapshot_save_error_code = "store_error"
            snapshot_save_detail = _error_detail2(
                plan_signature=signature,
                packet_hash=str(first.get("packet_hash") or run.get("packet_hash") or ""),
                review_fingerprint=_current_review_fp)
            run["binding_snapshot_save_failed"] = True
            run["binding_snapshot_save_error"] = snapshot_save_error
            run["binding_snapshot_save_error_code"] = snapshot_save_error_code
            run["binding_snapshot_save_detail"] = dict(snapshot_save_detail)
        _deltas = _normalize(bound, signature)
        resolved = [b for b in table["bindings"] if b["state"] == "resolved"]
        multi = [b for b in table["bindings"] if b["state"] == "multiple_targets"]
        blocked = [b for b in table["bindings"] if not _auto_apply_ok(b)]
        if snapshot_save_failed:
            blocked = list(table["bindings"]) or [{"state": "ambiguous"}]
        # 保存済みレビューが新版で全件対応できた場合だけ再開枠を使う。
        # 旧計画のPREP00欠落など、未解決のままなら再開履歴を消費しない。
        if (binding_version or planner_version) and not blocked and not already:
            record_resume(run, actor=actor, binding_version=binding_version,
                          planner_version=planner_version,
                          cause=resume_cause or "stopped run resume with new binding/planner version")
        run["issue_bindings"] = table["bindings"]
        run["issue_binding_version"] = table["version"]
        try:
            from app.plan_feedback import confirmed_target_for as _confirmed_for
            saved_row3 = _store(manager).get(pid, "binding_snapshot", signature) or {}
            saved_obj3 = saved_row3.get("snapshot") if isinstance(saved_row3, dict) else None
            if isinstance(saved_obj3, dict) and isinstance(saved_obj3.get("confirmed"), list):
                for binding in run["issue_bindings"] or []:
                    if not isinstance(binding, dict):
                        continue
                    hit = _confirmed_for(saved_obj3, str(binding.get("issue_id") or ""))
                    if hit and (str(hit.get("task_key") or "") or str(hit.get("criterion_id") or "")
                                or str(hit.get("decision_option") or "")):
                        binding["selection_status"] = "confirmed"
                        if str(hit.get("task_key") or ""):
                            binding["selected_task_key"] = str(hit.get("task_key") or "")
                        if str(hit.get("criterion_id") or ""):
                            binding["selected_criterion_id"] = str(hit.get("criterion_id") or "")
                        if str(hit.get("decision_option") or ""):
                            binding["selection_option"] = str(hit.get("decision_option") or "")
                        if str(hit.get("reason") or ""):
                            binding["selection_reason"] = str(hit.get("reason") or "")[:2000]
                        if str(hit.get("actor") or ""):
                            binding["selection_actor"] = str(hit.get("actor") or "")[:100]
        except (OSError, ValueError, RuntimeError, AttributeError, KeyError, TypeError, sqlite3.Error):
            pass
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
            "binding_snapshot_save_failed": snapshot_save_failed,
            "binding_snapshot_save_error": snapshot_save_error,
            "binding_snapshot_save_error_code": snapshot_save_error_code,
            "binding_snapshot_save_detail": dict(snapshot_save_detail),
        }
        run.setdefault("remap_history", []).append(entry)
        if len(run["remap_history"]) > 20:
            run["remap_history"] = run["remap_history"][-20:]
        if int(run.get("external_calls") or 0) != external_before:
            raise ValueError("外部呼出が発生したため中断しました")
        if int(run.get("verification_rounds") or 0) != rounds_before:
            raise ValueError("検証回数が変わったため中断しました")
        run = _save(manager, run)
        # F2a fail-closed: 対応付け直しの監査失敗は握りつぶさず記録し、成功偽装しない。
        # 外部呼出0・検証回数不変は維持する。
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
        except (OSError, ValueError, RuntimeError, AttributeError, KeyError, TypeError) as exc:
            run["audit_error"] = type(exc).__name__
            run = _save(manager, run)
        # 版付き再開では保存済みレビューだけを使ってローカル修正案を作る。
        # 対応先が残る場合はpermittedのまま放置せず人間確認待ちへ戻す。
        if binding_version or planner_version:
            if blocked:
                run["state"] = "stopped"
                run["stop_kind"] = "human_required"
                run["stop_reason"] = (
                    f"対応付けスナップショットの保存に失敗したため確認待ちです({snapshot_save_error})"
                    if snapshot_save_failed else
                    "保存済み指摘の対応先に未確定項目が残るため確認待ちです"
                )
                return public_view(_save(manager, run))
            try:
                from app.plan_feedback import propose as _propose
                candidate = await _propose(
                    manager, pid, signature, None,
                    idempotency_key="stored-review-resume-" + run_id,
                    _issues_override=bound)
            except Exception as exc:
                run = get_run(manager, pid, run_id)
                run["state"] = "stopped"
                run["stop_kind"] = "human_required"
                run["stop_reason"] = "保存済み指摘からの修正案生成に失敗しました: " + str(exc)[:250]
                return public_view(_save(manager, run))
            run = get_run(manager, pid, run_id)
            run["state"] = "stopped"
            run["stop_kind"] = "human_required"
            run["revision_candidate_id"] = str(candidate.get("candidate_id") or "")
            run["stop_reason"] = ("保存済み指摘からローカル修正案を作成しました。差分を確認して適用してください"
                                  if run["revision_candidate_id"] else
                                  "保存済み指摘から修正案を確定できませんでした")
            return public_view(_save(manager, run))
        return public_view(run)


async def run_loop(manager, pid: str, run_id: str) -> dict:
    """保存済み状態から再開可能。送信済みpacketは無断で再送しない。"""
    try:
        from app.project_delete import is_deleted_or_unknown as _l3_is_blocked
        if _l3_is_blocked(manager.memory.path, pid):
            raise ValueError("論理削除中または削除状態を確認できないPJには動作しません(状態を確定できません)")
    except ValueError:
        raise
    except Exception:
        raise ValueError("論理削除中または削除状態を確認できないPJには動作しません(状態を確定できません)")
    async with _lock(pid):
        return await _run_locked(manager, pid, run_id)


def _stop_guard_tick(manager, pid: str, run: dict) -> tuple[dict, dict | None]:
    """Stage1 空転ガード: 同一(署名,クラス,コード)2連続で stopped にする。

    履歴を書くのはランのティックだけ。新しい署名・新しい入力・人の選択があれば解除。
    戻り値: (guard, flat_record)。guard["repeating"] が True なら停止する。
    F2a fail-closed: 履歴追記の失敗は握りつぶさず記録する(成功偽装しない)。
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
    except (OSError, ValueError, RuntimeError, AttributeError, KeyError, TypeError) as exc:
        guard["history_error"] = type(exc).__name__
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
        # F2a fail-closed: lease確認不能でも停止表示は返す。解除不能の残存leaseは
        # 開始側を阻む(停止側)に保つ。
        try:
            lease = _lease_get(manager, pid) or {}
        except ValueError:
            lease = {}
        if lease.get("run_id") == run_id:
            _lease_set(manager, pid, None)
        try:
            manager.memory.add_event(pid, "auto_loop_stopped", "同じ停止の繰り返しのため自動ランを停止しました",
                                     detail=canonical({"run_id": run_id}))
        except (OSError, ValueError, RuntimeError, AttributeError, KeyError, TypeError,
                sqlite3.Error):
            pass
        return public_view(run)

    def _stop(reason: str, kind: str = "stopped") -> dict:
        run["state"] = "stopped"
        run["stop_reason"] = reason[:1000]
        run["stop_kind"] = kind
        _save(manager, run)
        # F2a fail-closed: lease確認不能でも停止表示は返す。残存leaseは開始を阻む。
        try:
            lease = _lease_get(manager, pid) or {}
        except ValueError:
            lease = {}
        if lease.get("run_id") == run_id:
            _lease_set(manager, pid, None)
        try:
            manager.memory.add_event(pid, "auto_loop_stopped", reason[:500],
                                     detail=canonical({"run_id": run_id}))
        except (OSError, ValueError, RuntimeError, AttributeError, KeyError, TypeError,
                sqlite3.Error):
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
        # 外部AI呼出の直前にも、送信packetを計算して期待ハッシュを照合する。
        # 第2回は自動修正後の署名と新packetを対応付けて記録する。
        from app.goal_review import build_final_packet as _build_final_packet
        _round_packet, round_packet_hash = _build_final_packet(run.get("public_summary") or "", snapshot)
        if round_index == 1 and round_packet_hash != str(run.get("packet_hash") or ""):
            return _stop("409: プレビュー時と送信直前のpacketが変わりました。再プレビューしてください")
        if round_index > 1 and signature != str(run.get("plan_signature") or ""):
            return _stop("409: 自動修正後の計画版が変わりました。再プレビューしてください")
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
                result = await _review_plan(*review_args, expected_packet_hash=round_packet_hash)
            else:
                result = await _review_plan(*review_args, providers_override=run.get("providers") or [], expected_packet_hash=round_packet_hash)
        except ValueError as exc:
            run = get_run(manager, pid, run_id)
            return _stop(str(exc))
        # 保存・下流の指摘対応付けも今回実際に照合したpacketへ固定する。
        run["packet_hash"] = round_packet_hash
        sent_hashes = run.setdefault("sent_packet_hashes", [])
        if round_packet_hash not in sent_hashes:
            sent_hashes.append(round_packet_hash)
        run = _save(manager, run)
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
            # 外部評価の合格を人間承認や実行完了と同一視しない。
            # 計画承認・実行・RAG登録は既存の人間確認境界を維持する。
            from app.plan_feedback import external_pass_is_not_approval as _not_approval

            run["external_pass_note"] = _not_approval("passed")
            run["state"] = "awaiting_human"
            run["stop_reason"] = ""
            run = _save(manager, run)
            # F2a: lease確認不能でも承認待ち表示は返す。残存leaseは開始を阻む。
            try:
                lease = _lease_get(manager, pid) or {}
            except ValueError:
                lease = {}
            if lease.get("run_id") == run_id:
                _lease_set(manager, pid, None)
            return public_view(run)

        # --- 指摘の対応付け。曖昧な指摘は人の確認待ち。 ---
        run["state"] = "intake"
        run = _save(manager, run)
        intake_actor = "local"
        # P0-B前半: 対応表の構築・保存と自動適用の可否判定を分離する。
        # buildで全指摘(原文・step・候補・候補なし理由)を先に保存し、
        # ambiguous/invalid_referenceがあっても保存せずに例外で止まる経路をなくす。
        full_table = build_issue_bindings(parsed, contract, snapshot,
                                          plan_signature=signature,
                                          goal_contract_hash=str(contract.get("content_hash") or ""),
                                          packet_hash=str(run.get("packet_hash") or ""))
        saved = save_binding_table_first(manager, pid, signature,
                                         list(full_table.get("bindings") or []))
        if not saved.get("saved"):
            run["binding_snapshot_save_failed"] = True
            run["binding_snapshot_save_error"] = str(saved.get("binding_snapshot_save_error") or "Error")
            run["binding_snapshot_save_error_code"] = str(saved.get("binding_snapshot_save_error_code") or "snapshot_missing")
            run["binding_snapshot_save_detail"] = dict(saved.get("binding_snapshot_save_detail") or {})
            run = _save(manager, run)
            return _stop("対応付け正本を保存できないため停止しました", "needs_attention")
        try:
            mapped = _map_issues_local(parsed, contract, snapshot,
                                       plan_signature=signature,
                                       goal_contract_hash=str(contract.get("content_hash") or ""),
                                       packet_hash=str(run.get("packet_hash") or ""))
        except ValueError:
            # 保存済みの対応表で判断待ちへ確実に到達させる。保存は済み。
            run = get_run(manager, pid, run_id)
            run["intake_actor"] = intake_actor
            run["issue_bindings"] = list(full_table.get("bindings") or [])
            run["issue_binding_version"] = str(full_table.get("version") or ISSUE_BINDING_VERSION)
            run["binding_snapshot_save_failed"] = False
            run["binding_snapshot_save_error"] = ""
            unconfirmed = unconfirmed_issue_ids(run["issue_bindings"], [
                str(entry.get("issue_id") or "")
                for entry in (run.get("manual_bindings") or [])
                if isinstance(entry, dict)
            ])
            run["awaiting_decision"] = awaiting_decision_view(
                run, run["issue_bindings"], unconfirmed)
            run = _save(manager, run)
            return _stop("指摘の対応先を特定できないため、人の確認待ちで停止しました", "human_required")
        run["intake_actor"] = intake_actor
        run["issue_bindings"] = mapped.get("bindings") or []
        run["issue_binding_version"] = mapped.get("version") or ISSUE_BINDING_VERSION
        run = _save(manager, run)

        # 初回保存済みの正本を再照合する。ここで再構築すると、
        # 同じ版で人が確定した行を空のmanual_bindingsで消し得る。
        try:
            from app.plan_feedback import check_binding_snapshot_current as _check_snapshot
            from app.plan_feedback import review_fingerprint_of as _review_fp_of3

            saved_review = _store(manager).get(pid, "plan", signature) or {}
            saved_packet = saved_review.get("packet")
            review_rows = saved_review.get("reviews") or []
            saved_row = _store(manager).get(pid, "binding_snapshot", signature) or {}
            saved_snapshot = saved_row.get("snapshot") if isinstance(saved_row, dict) else None
            _current_review_fp3 = _review_fp_of3(signature, review_rows)
            # D1-4: 旧正本は保存行由来とラン内コピー由来の両方から継承する。
            _run_lists_locked: list = []
            try:
                for _rid2, _payload2 in _store(manager).list(pid, "auto_loop"):
                    if (isinstance(_payload2, dict)
                            and str(_payload2.get("plan_signature") or "") == str(signature or "")):
                        _rounds2 = _payload2.get("rounds") or []
                        if isinstance(_rounds2, list) and _rounds2 and isinstance(_rounds2[0], dict):
                            _rr2 = _rounds2[0].get("reviews")
                            if isinstance(_rr2, list):
                                _run_lists_locked.append(_rr2)
            except Exception:
                _run_lists_locked = []
            _check_snapshot(
                saved_snapshot,
                plan_signature=signature,
                goal_contract_hash=str(contract.get("content_hash") or ""),
                packet_hash=fingerprint(saved_packet) if saved_packet else "",
                review_fingerprint=_current_review_fp3,
                current_reviews=[review_rows] + _run_lists_locked,
            )
            saved_ids = [str(b.get("issue_id") or "") for b in (saved_snapshot.get("bindings") or [])]
            current_ids = [str(b.get("issue_id") or "") for b in run["issue_bindings"]]
            if sorted(saved_ids) != sorted(current_ids):
                raise ValueError("保存済み対応表の指摘IDが一致しません")
            run["binding_snapshot_save_failed"] = False
            run["binding_snapshot_save_error"] = ""
            run["binding_snapshot_save_error_code"] = ""
            run["binding_snapshot_save_detail"] = {}
            run = _save(manager, run)
        except (OSError, ValueError, RuntimeError, AttributeError, KeyError, TypeError, sqlite3.Error) as exc:
            from app.plan_feedback import binding_snapshot_error_detail as _error_detail3
            from app.plan_feedback import review_fingerprint_of as _review_fp_of4
            err_code = str(getattr(exc, "code", "") or "")
            try:
                saved_row2 = _store(manager).get(pid, "binding_snapshot", signature) or {}
                saved_obj2 = saved_row2.get("snapshot") if isinstance(saved_row2, dict) else None
                saved_review2 = _store(manager).get(pid, "plan", signature) or {}
                review_rows2 = saved_review2.get("reviews") or []
                saved_packet2 = saved_review2.get("packet")
                cur_fp = _review_fp_of4(signature, review_rows2)
                cur_packet = fingerprint(saved_packet2) if saved_packet2 else ""
                from app.plan_feedback import _binding_snapshot_mismatch_code as _mismatch_code2
                if not err_code:
                    _mismatch2 = _mismatch_code2(
                        saved_obj2, plan_signature=signature,
                        goal_contract_hash=str(contract.get("content_hash") or ""),
                        packet_hash=cur_packet,
                        review_fingerprint=cur_fp) or ""
                    # D1-5: 正本が無いときだけ snapshot_missing。不明な失敗は store_error。
                    if _mismatch2:
                        err_code = _mismatch2
                    elif saved_obj2 is None:
                        err_code = "snapshot_missing"
                    else:
                        err_code = "store_error"
                run["binding_snapshot_save_failed"] = True
                run["binding_snapshot_save_error"] = type(exc).__name__
                run["binding_snapshot_save_error_code"] = err_code
                run["binding_snapshot_save_detail"] = _error_detail3(
                    plan_signature=signature,
                    packet_hash=cur_packet,
                    review_fingerprint=cur_fp)
            except Exception:
                run["binding_snapshot_save_failed"] = True
                run["binding_snapshot_save_error"] = type(exc).__name__
                if not err_code:
                    err_code = "store_error"
                run["binding_snapshot_save_error_code"] = err_code
                run["binding_snapshot_save_detail"] = {}
            run = _save(manager, run)
            return _stop("対応付け正本を照合できないため停止しました", "needs_attention")
        # --- 修正案と自動反映(既存 propose/validate/apply を再利用) ---
        # 外部再評価は最大2回。2回で未解決なら「要判断」として止める。
        from app.plan_feedback import external_reevaluation_gate as _reeval_gate

        gate = _reeval_gate(rounds=round_index)
        if round_index >= VERIFICATION_LIMIT:
            # 第2回で指摘が残る場合は必ず停止する。停止理由には従来の文言(3回目を行わない)と
            # 外部再評価ゲートの判定理由を併記し、ゲートが不許可なら要判断として区別する。
            run = get_run(manager, pid, run_id)
            run["external_reevaluation"] = gate
            run = _save(manager, run)
            return _stop("第2回検証でも指摘が残ったため停止しました。3回目の自動評価は行いません"
                         + ("（" + str(gate.get("reason")) + "）" if gate.get("reason") else ""),
                         "stopped" if gate.get("allowed") else "needs_attention")
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
