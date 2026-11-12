"""G3-b: 外部下書きをローカルAIへの参考情報として渡すためのヒント.

外部通信・LLM呼出・ストア作成なし。純粋関数 + ContextVar のみ。
外部下書きの工程を直接計画に取り込まない。信用しない下書きとして扱う。
"""
from __future__ import annotations

import hashlib
import json
import re
from contextvars import ContextVar

EXTERNAL_DRAFT_HINT = ContextVar("external_draft_hint", default=None)

MAX_HINT_TASKS = 12
MAX_TITLE_CHARS = 80
MAX_SUMMARY_CHARS = 160

_HEADER = "【外部骨子(参考・未検証。事実の根拠にしない。登録原本と矛盾したら原本を優先し、ここにある題名や文言をそのまま採用しない)】"

# [顧客…] 形の参照記号 (G3-a と同じ粒度)
_REF_RE = re.compile(r"\[(?:顧客|取引先|担当者|案件)[^\[\]\r\n]{0,40}\]")
# 拡張子付きファイル名 (代表的なもの + 一般的な文書/表/コード)
_EXT_RE = re.compile(
    r"[A-Za-z0-9_\-一-龯ぁ-んァ-ン]+\.(?:pdf|docx|xlsx|xlsm|xls|md|py|txt|csv|json|html|pptx|doc)\b",
    re.I,
)
# 絶対パス ("/" で始まる経路)。数値の分数 (1/2) を拾わないよう英字を要求する。
_ABS_PATH_RE = re.compile(r"(?<![\w:/])/[A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-./]*)?")

_CLAIM_MARKERS_RAW = ("実施済み", "完了済み", "達成済み", "確認済み")
_CLAIM_MARKERS_ASCII = ("completed", "achieved", "verified")


def _normalize_space(text: object) -> str:
    blob = str(text or "")
    blob = blob.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return " ".join(blob.split())


def _split_sentences(text: str) -> list[str]:
    # 英字のピリオドでファイル名・URLを壊さないよう、.!? は空白続きのときだけ区切る。
    parts = re.split(r"(?<=[。！？])|\n+|(?<=[.!?])\s+", str(text or ""))
    out = [part.strip() for part in parts if part.strip()]
    return out or ([str(text or "").strip()] if str(text or "").strip() else [])


def _flatten_pending(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        if value.strip():
            found.append(value.strip())
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(_flatten_pending(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_flatten_pending(item))
    return found


def _contains_second_category(title: str, summary: str) -> bool:
    blob = f"{title}\n{summary}"
    if "://" in blob:
        return True
    if "C:\\" in blob or "C:/" in blob:
        return True
    if "result/" in blob:
        return True
    if "workspace:" in blob:
        return True
    if _EXT_RE.search(blob):
        return True
    if _ABS_PATH_RE.search(blob):
        return True
    if _REF_RE.search(blob):
        return True
    return False


def build_hint(job: dict, current_plan_signature: str, pid: str) -> dict:
    """純粋関数。外部通信・LLM呼出・ストア作成なし。"""
    if not isinstance(job, dict) or job.get("pid") != pid:
        raise ValueError("job_not_found")
    if job.get("status") != "external_generated":
        raise ValueError("job_not_usable")
    if job.get("plan_signature") != current_plan_signature:
        raise ValueError("plan_changed")
    draft = job.get("external_draft")
    if not isinstance(draft, dict):
        raise ValueError("draft_missing")
    tasks = draft.get("tasks")
    if not isinstance(tasks, list) or len(tasks) == 0:
        raise ValueError("draft_missing")
    draft_sha256 = hashlib.sha256(
        json.dumps(draft, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    job_id = str(job.get("id") or job.get("job_id") or "")

    # 禁止文字列 (参考ブロックに含めない)。空・短すぎる断片は無視する。
    outputs_paths: list[str] = []
    for item in tasks:
        if not isinstance(item, dict):
            continue
        for name in item.get("outputs") or []:
            if isinstance(name, str) and name.strip():
                outputs_paths.append(name.strip())
    depends_values: list[str] = []
    for item in tasks:
        if not isinstance(item, dict):
            continue
        for name in item.get("depends_on") or []:
            if isinstance(name, str) and name.strip():
                depends_values.append(name.strip())
    required_outputs = [
        str(name).strip()
        for name in (draft.get("required_outputs") or [])
        if isinstance(name, str) and str(name).strip()
    ]
    pending_strings = [
        text for text in _flatten_pending(draft.get("external_operations_pending"))
        if text and len(text) >= 2
    ]
    fact_strings = [
        str(name).strip()
        for name in (draft.get("unverified_facts") or [])
        if isinstance(name, str) and str(name).strip() and len(str(name).strip()) >= 2
    ]
    claim_sources: list = []
    for source in (job.get("unverified_claims"), draft.get("unverified_claims")):
        if isinstance(source, list):
            claim_sources.extend(source)
    claim_strings = [
        str(name).strip()
        for name in claim_sources
        if isinstance(name, str) and str(name).strip() and len(str(name).strip()) >= 2
    ]
    # 文章レベルの除外用 (本文コピーを防ぐ)
    sentence_forbidden = [
        text
        for text in (
            outputs_paths + required_outputs + pending_strings + fact_strings + claim_strings
        )
        if text and len(text) >= 2
    ]
    depends_for_sentence = [text for text in depends_values if text and len(text) >= 2]

    lines: list[str] = []
    for item in tasks:
        if len(lines) >= MAX_HINT_TASKS:
            break
        if not isinstance(item, dict):
            continue
        title_norm = _normalize_space(item.get("title") or "")
        desc_norm = _normalize_space(item.get("description") or "")
        if not title_norm:
            continue
        # タイトルに禁止文字列・主張マーカーが入る工程は丸ごと除外する。
        # depends_on はタイトルと同一になり得るためタイトル判定には使わない。
        title_forbidden = [t for t in sentence_forbidden if t and t in title_norm]
        title_has_claim = any(mark in title_norm for mark in _CLAIM_MARKERS_RAW) or any(
            mark in title_norm.lower() for mark in _CLAIM_MARKERS_ASCII
        )
        if title_forbidden or title_has_claim:
            continue
        # 説明文から禁止文を除く (該当する文だけ落とす)
        sentences = _split_sentences(desc_norm)
        kept: list[str] = []
        for sentence in sentences:
            if any(lit in sentence for lit in sentence_forbidden):
                continue
            if any(lit in sentence for lit in depends_for_sentence):
                continue
            if any(mark in sentence for mark in _CLAIM_MARKERS_RAW):
                continue
            lowered = sentence.lower()
            if any(mark in lowered for mark in _CLAIM_MARKERS_ASCII):
                continue
            kept.append(sentence)
        summary_full = _normalize_space(" ".join(kept))
        if not summary_full:
            continue
        title_trunc = title_norm[:MAX_TITLE_CHARS]
        summary_trunc = summary_full[:MAX_SUMMARY_CHARS]
        # 第二種別 (URL・絶対パス・拡張子等) は工程ごと除外する
        if _contains_second_category(title_trunc, summary_trunc):
            continue
        # 切り詰め前の全文でも判定する (切り詰めで隠れる漏れを防ぐ)
        if _contains_second_category(title_norm, summary_full):
            continue
        lines.append(f"- {title_trunc}: {summary_trunc}")

    if not lines:
        raise ValueError("draft_missing")
    text = _HEADER + "\n" + "\n".join(lines[:MAX_HINT_TASKS])
    return {
        "job_id": job_id,
        "draft_sha256": draft_sha256,
        "task_count": len(lines[:MAX_HINT_TASKS]),
        "text": text,
    }


def current_hint_text() -> str:
    """現在の参考ブロックを返す。未設定なら空文字。"""
    value = EXTERNAL_DRAFT_HINT.get()
    if value is None:
        return ""
    if isinstance(value, dict):
        text = value.get("text")
        return str(text) if isinstance(text, str) else ""
    if isinstance(value, str):
        return value
    return ""
