"""LLM-verbatim requirement decomposition for generic (non-vehicle) missions.

LLM output is untrusted: only original substrings (whitespace-normalized) are
adopted. decompose() never writes DB, contracts, inputs, or artifacts.
Human confirm may remove or merge quotes only; it never adds or edits wording.
"""
from __future__ import annotations

import inspect
import json
import re

from app.core import Ollama
from app.goal_completion_flag import enabled
from app.goal_contract import SCHEMA, content_hash, from_mission, put_draft
from app.requirement_retention import inspect as inspect_retention
from app.vehicle_workflow import applicable, requested_months


MAX_ITEMS = 30
MAX_QUOTE_CHARS = 400
MAX_SOURCE_CHARS = 12000
ALLOWED_KINDS = frozenset({"requirement", "constraint", "exclusion", "context"})
KEEP_KINDS = frozenset({"requirement", "constraint", "exclusion"})
ALLOWED_OPS = frozenset({"remove", "merge"})

DECOMPOSE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "maxItems": MAX_ITEMS,
            "items": {
                "type": "object",
                "properties": {
                    "quote": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["requirement", "constraint", "exclusion", "context"],
                    },
                },
                "required": ["quote", "kind"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["items"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = (
    "あなたは要求文書から『原文の文字列そのままで』独立した要求の節を切り出すだけです。"
    "言い換え・要約・補完・新しい語の追加は禁止です。"
    "各quoteは入力本文に一字一句含まれる連続した引用でなければなりません。"
    "空白の違いは無視してよいですが、語を足したり変えたりしてはいけません。"
    "指定されたJSONだけを返してください。"
)


class DecomposeUnavailable(Exception):
    """Local LLM cannot be used; callers map this to HTTP 503."""

    status_code = 503


class DecomposeRejected(Exception):
    """Validation or policy rejection. status_code is 409 or 422."""

    def __init__(self, code: str, message: str, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.status_code = int(status_code)


def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _source_fields(mission: dict) -> dict[str, str]:
    return {
        "goal": str(mission.get("goal") or ""),
        "success_criteria": str(mission.get("success_criteria") or ""),
        "constraints": str(mission.get("constraints_text") or ""),
    }


def _combined_source(fields: dict[str, str]) -> str:
    return "\n".join(fields.get(name) or "" for name in ("goal", "success_criteria", "constraints"))


def _compact_map(text: str) -> tuple[str, list[int]]:
    compact: list[str] = []
    mapping: list[int] = []
    for index, char in enumerate(text):
        if char.isspace():
            continue
        compact.append(char)
        mapping.append(index)
    return "".join(compact), mapping


def _locate_quote(fields: dict[str, str], quote: str) -> tuple[str, int, int] | None:
    raw = str(quote or "")
    if not raw.strip():
        return None
    for name, text in fields.items():
        pos = text.find(raw)
        if pos >= 0:
            return name, pos, pos + len(raw)
    needle, _ = _compact_map(raw)
    if not needle:
        return None
    for name, text in fields.items():
        hay, mapping = _compact_map(text)
        pos = hay.find(needle)
        if pos < 0:
            continue
        orig_start = mapping[pos]
        orig_end = mapping[pos + len(needle) - 1] + 1
        return name, orig_start, orig_end
    return None


def split_sentences(text: str) -> list[dict]:
    """Split source text into sentences by 。．, newlines, ;, and bullet prefixes."""
    source = str(text or "")
    if not source.strip():
        return []
    spans: list[tuple[int, int]] = []
    start = 0
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        if char in "。．":
            spans.append((start, index + 1))
            index += 1
            start = index
            continue
        if char in ";；":
            if start < index:
                spans.append((start, index))
            index += 1
            start = index
            continue
        if char == "\n":
            if start < index:
                spans.append((start, index))
            index += 1
            while index < length and source[index] == "\n":
                index += 1
            start = index
            continue
        index += 1
    if start < length:
        spans.append((start, length))
    sentences = []
    for begin, end in spans:
        chunk = source[begin:end]
        stripped = chunk.strip()
        if not stripped:
            continue
        lead = re.match(r"^([-*・●]|\d+[.．)、])\s+", stripped)
        body = stripped
        if lead:
            body = stripped[lead.end():].strip() or stripped
        if not body:
            continue
        sentences.append({"span": stripped, "start": begin, "end": end})
    return sentences


def _overlap(a0: int, a1: int, b0: int, b1: int) -> bool:
    return a0 < a1 and b0 < b1 and a0 < b1 and b0 < a1


def build_correspondence(fields: dict[str, str], items: list[dict]) -> tuple[list[dict], list[dict]]:
    correspondence: list[dict] = []
    uncovered: list[dict] = []
    located = []
    for item in items:
        found = _locate_quote(fields, item.get("quote") or "")
        if found:
            located.append((item, found[0], found[1], found[2]))
    for field_name, text in fields.items():
        for sentence in split_sentences(text):
            item_ids = []
            for item, item_field, start, end in located:
                if item_field != field_name:
                    continue
                if _overlap(sentence["start"], sentence["end"], start, end):
                    item_ids.append(item["item_id"])
                    continue
                if _norm_ws(item.get("quote") or "") and (
                    _norm_ws(item["quote"]) in _norm_ws(sentence["span"])
                    or _norm_ws(sentence["span"]) in _norm_ws(item["quote"])
                ):
                    item_ids.append(item["item_id"])
            row = {
                "field": field_name,
                "span": sentence["span"],
                "item_ids": list(dict.fromkeys(item_ids)),
            }
            correspondence.append(row)
            if not row["item_ids"]:
                uncovered.append({"field": field_name, "span": sentence["span"]})
    return correspondence, uncovered


def _parse_llm_payload(raw) -> dict:
    if isinstance(raw, dict):
        payload = raw
    elif isinstance(raw, (bytes, bytearray)):
        payload = json.loads(raw.decode("utf-8"))
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise DecomposeUnavailable("ローカルLLMの分解応答が空です")
        try:
            payload = json.loads(text)
        except ValueError as exc:
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                try:
                    payload = json.loads(text[start:end + 1])
                except ValueError as nested:
                    raise DecomposeUnavailable("ローカルLLMの分解応答がJSONではありません") from nested
            else:
                raise DecomposeUnavailable("ローカルLLMの分解応答がJSONではありません") from exc
    else:
        raise DecomposeUnavailable("ローカルLLMの分解応答が不正です")
    if not isinstance(payload, dict) or "items" not in payload:
        raise DecomposeUnavailable("ローカルLLMの分解応答のスキーマが不正です")
    if set(payload) - {"items"}:
        raise DecomposeUnavailable("ローカルLLMの分解応答のスキーマが不正です")
    items = payload.get("items")
    if not isinstance(items, list):
        raise DecomposeUnavailable("ローカルLLMの分解応答のスキーマが不正です")
    return payload


def _adopt_items(fields: dict[str, str], payload: dict) -> tuple[list[dict], list[dict]]:
    adopted: list[dict] = []
    dropped: list[dict] = []
    seen: set[str] = set()
    for index, raw in enumerate(payload.get("items") or []):
        if not isinstance(raw, dict):
            raise DecomposeUnavailable("ローカルLLMの分解応答のスキーマが不正です")
        if set(raw) - {"quote", "kind"}:
            raise DecomposeUnavailable("ローカルLLMの分解応答のスキーマが不正です")
        quote = raw.get("quote")
        kind = raw.get("kind")
        if not isinstance(quote, str) or not isinstance(kind, str):
            raise DecomposeUnavailable("ローカルLLMの分解応答のスキーマが不正です")
        if kind not in ALLOWED_KINDS:
            raise DecomposeUnavailable("ローカルLLMの分解応答のスキーマが不正です")
        stripped = quote.strip()
        if not stripped:
            dropped.append({"quote": quote, "kind": kind, "reason": "empty"})
            continue
        if len(quote) > MAX_QUOTE_CHARS:
            dropped.append({"quote": quote[:MAX_QUOTE_CHARS], "kind": kind, "reason": "too_long"})
            continue
        located = _locate_quote(fields, quote)
        if located is None:
            dropped.append({"quote": quote, "kind": kind, "reason": "not_verbatim"})
            continue
        field_name, start, end = located
        key = re.sub(r"\s+", "", quote)
        if key in seen:
            dropped.append({"quote": quote, "kind": kind, "reason": "duplicate"})
            continue
        if len(adopted) >= MAX_ITEMS:
            dropped.append({"quote": quote, "kind": kind, "reason": "max_items"})
            continue
        seen.add(key)
        adopted.append({
            "item_id": f"DC{len(adopted) + 1:02d}",
            "kind": kind,
            "quote": quote,
            "field": field_name,
            "span": {"start": start, "end": end},
        })
    return adopted, dropped


def _retention_preview(mission: dict, items: list[dict]) -> dict:
    quotes = [item["quote"] for item in items if item.get("kind") in KEEP_KINDS]
    criteria = [
        {
            "criterion_id": f"SC{index:02d}",
            "statement": quote,
        }
        for index, quote in enumerate(quotes, 1)
    ]
    return inspect_retention(mission, {"extracted_criteria": quotes, "criteria": criteria})


def _truncate_for_llm(text: str) -> str:
    value = str(text or "")
    if len(value) <= MAX_SOURCE_CHARS:
        return value
    return value[:MAX_SOURCE_CHARS]


async def _await_maybe(value):
    if inspect.isawaitable(value):
        return await value
    return value


def _require_local_llm(manager) -> Ollama:
    llm = getattr(manager, "llm", None)
    if not isinstance(llm, Ollama):
        raise DecomposeUnavailable("ローカルLLMが利用できません")
    return llm


async def decompose(manager, project_id: str) -> dict:
    """Enumerate verbatim requirement quotes. Never writes storage."""
    mission = manager.memory.get_mission(project_id)
    if applicable(mission):
        return {
            "applicable": False,
            "reason": "車両案件は専用テンプレートを使います",
            "draft": True,
            "saved": False,
            "items": [],
            "correspondence": [],
            "uncovered": [],
            "dropped": [],
            "retention_preview": None,
        }
    llm = _require_local_llm(manager)
    healthy = await _await_maybe(llm.health())
    if not healthy:
        raise DecomposeUnavailable("ローカルLLMが利用できません")
    fields = _source_fields(mission)
    user_prompt = (
        "次の原文から、独立した要求・制約・除外・文脈の節を原文どおり引用してください。\n"
        "quoteは原文に含まれる文字列そのままで、言い換えないでください。\n\n"
        f"## goal\n{_truncate_for_llm(fields['goal'])}\n\n"
        f"## success_criteria\n{_truncate_for_llm(fields['success_criteria'])}\n\n"
        f"## constraints\n{_truncate_for_llm(fields['constraints'])}\n"
    )
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    complete_json = getattr(llm, "complete_json", None)
    if not callable(complete_json):
        raise DecomposeUnavailable("ローカルLLMが利用できません")
    try:
        raw = await _await_maybe(complete_json(messages, DECOMPOSE_JSON_SCHEMA))
    except DecomposeUnavailable:
        raise
    except Exception as exc:
        raise DecomposeUnavailable("ローカルLLMの分解に失敗しました") from exc
    payload = _parse_llm_payload(raw)
    items, dropped = _adopt_items(fields, payload)
    correspondence, uncovered = build_correspondence(fields, items)
    return {
        "applicable": True,
        "draft": True,
        "saved": False,
        "model": str(getattr(llm, "model", "") or ""),
        "items": items,
        "correspondence": correspondence,
        "uncovered": uncovered,
        "dropped": dropped,
        "retention_preview": _retention_preview(mission, items),
    }


def _quote_sort_key(fields: dict[str, str], quote: str, fallback: int) -> tuple[int, int]:
    order = {"goal": 0, "success_criteria": 1, "constraints": 2}
    located = _locate_quote(fields, quote)
    if located is None:
        return (99, fallback)
    field_name, start, _end = located
    return (order.get(field_name, 99), start)


def _apply_operations(items: list[dict], operations: list, fields: dict[str, str]) -> list[dict]:
    current = list(items)
    by_id = {item["item_id"]: item for item in current}
    for raw in operations or []:
        if not isinstance(raw, dict):
            raise DecomposeRejected("INVALID_OPERATION", "許可されない操作です", 422)
        op = str(raw.get("op") or "").strip()
        if op not in ALLOWED_OPS:
            raise DecomposeRejected("INVALID_OPERATION", "許可されない操作です", 422)
        if op == "remove":
            item_id = str(raw.get("item_id") or "").strip()
            if item_id not in by_id:
                raise DecomposeRejected("UNKNOWN_ITEM", "対象項目がありません", 422)
            current = [item for item in current if item["item_id"] != item_id]
            by_id = {item["item_id"]: item for item in current}
            continue
        ids = raw.get("item_ids")
        if not isinstance(ids, list) or len(ids) < 2:
            raise DecomposeRejected("INVALID_MERGE", "統合対象が不正です", 422)
        selected = []
        seen_ids = set()
        for item_id in ids:
            key = str(item_id or "").strip()
            if key in seen_ids:
                continue
            if key not in by_id:
                raise DecomposeRejected("UNKNOWN_ITEM", "対象項目がありません", 422)
            seen_ids.add(key)
            selected.append(by_id[key])
        if len(selected) < 2:
            raise DecomposeRejected("INVALID_MERGE", "統合対象が不正です", 422)
        ordered = sorted(
            selected,
            key=lambda item: _quote_sort_key(fields, item.get("quote") or "", 0),
        )
        merged_quote = "".join(item.get("quote") or "" for item in ordered)
        survivor = dict(ordered[0])
        survivor["quote"] = merged_quote
        located = _locate_quote(fields, merged_quote)
        if located:
            survivor["field"] = located[0]
            survivor["span"] = {"start": located[1], "end": located[2]}
        keep_ids = {item["item_id"] for item in ordered}
        replaced = False
        next_items = []
        for item in current:
            if item["item_id"] not in keep_ids:
                next_items.append(item)
                continue
            if not replaced:
                next_items.append(survivor)
                replaced = True
        current = next_items
        by_id = {item["item_id"]: item for item in current}
    return current


def _contract_from_items(manager, project_id: str, items: list[dict], reviewed_by: str, model: str) -> dict:
    mission = manager.memory.get_mission(project_id)
    quotes = [str(item.get("quote") or "") for item in items]
    criteria = []
    for index, item in enumerate(items, 1):
        quote = str(item.get("quote") or "")
        field_name = str(item.get("field") or "goal")
        criteria.append({
            "criterion_id": f"SC{index:02d}",
            "statement": quote,
            "type": "factual",
            "test_method": "artifact_headings",
            "required_evidence": ["result_markdown"],
            "human_decision_required": False,
            "weight": 1.0,
            "source_spans": [{"field": field_name, "excerpt": quote[:180]}],
            "exec_task_keys": [f"SC{index:02d}"],
            "verify_task_keys": ["final_verification"],
        })
    base = from_mission(mission, manager, project_id)
    payload = {
        "schema": SCHEMA,
        "goal_id": base.get("goal_id"),
        "version": int(mission.get("plan_version") or 0) + 1,
        "objective": str(mission.get("goal") or "").strip() or (quotes[0] if quotes else ""),
        "scope": {
            "period": requested_months(mission) if applicable(mission) else [],
            "template": "",
        },
        "exclusions": [
            str(item.get("quote") or "") for item in items if item.get("kind") == "exclusion"
        ],
        "constraints": [
            str(item.get("quote") or "") for item in items if item.get("kind") == "constraint"
        ] or [line.strip() for line in str(mission.get("constraints_text") or "").splitlines() if line.strip()],
        "completion_policy": {
            "require_independent_reconciliation": False,
            "require_human_acceptance": True,
            "provisional_is_not_achieved": True,
        },
        "source_versions": {
            "mission_plan_version": mission.get("plan_version") or 0,
            "vehicle_engine_revision": None,
            "decomposed_by": "llm-verbatim",
            "reviewed_by": reviewed_by,
            "model": model or "",
        },
        "extracted_criteria": quotes,
        "criteria": criteria,
    }
    payload["retention"] = inspect_retention(mission, payload)
    payload["content_hash"] = content_hash(payload)
    return payload


def confirm(manager, project_id: str, draft_items, operations, reviewed_by: str) -> dict:
    """Apply remove/merge only, then save a draft contract. Never activates."""
    actor = str(reviewed_by or "").strip()
    if not actor:
        raise DecomposeRejected("REVIEWED_BY_REQUIRED", "reviewed_by is required", 422)
    if not enabled(manager.memory.path, project_id):
        raise DecomposeRejected("FLAG_OFF", "goal completion flag is off", 409)
    mission = manager.memory.get_mission(project_id)
    if applicable(mission):
        raise DecomposeRejected("NOT_APPLICABLE", "車両案件は専用テンプレートを使います", 409)
    fields = _source_fields(mission)
    if not isinstance(draft_items, list) or not draft_items:
        raise DecomposeRejected("EMPTY_ITEMS", "確認対象の項目がありません", 422)
    verified: list[dict] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(draft_items):
        if not isinstance(raw, dict):
            raise DecomposeRejected("INVALID_ITEM", "draft_items が不正です", 422)
        quote = raw.get("quote")
        if not isinstance(quote, str) or _locate_quote(fields, quote) is None:
            raise DecomposeRejected("NOT_VERBATIM", "原文に含まれない引用は確認できません", 409)
        item_id = str(raw.get("item_id") or f"DC{index + 1:02d}")
        if item_id in seen_ids:
            raise DecomposeRejected("DUPLICATE_ITEM", "項目IDが重複しています", 422)
        seen_ids.add(item_id)
        kind = str(raw.get("kind") or "requirement")
        if kind not in ALLOWED_KINDS:
            kind = "requirement"
        located = _locate_quote(fields, quote)
        field_name, start, end = located
        verified.append({
            "item_id": item_id,
            "kind": kind,
            "quote": quote,
            "field": str(raw.get("field") or field_name),
            "span": {"start": start, "end": end},
        })
    if operations is None:
        operations = []
    if not isinstance(operations, list):
        raise DecomposeRejected("INVALID_OPERATION", "許可されない操作です", 422)
    confirmed = _apply_operations(verified, operations, fields)
    if not confirmed:
        raise DecomposeRejected("EMPTY_AFTER_CONFIRM", "削除の結果、要求が0件になります", 409)
    _before_corr, uncovered_before = build_correspondence(fields, verified)
    _after_corr, uncovered_after = build_correspondence(fields, confirmed)
    model = str(getattr(getattr(manager, "llm", None), "model", "") or "")
    payload = _contract_from_items(manager, project_id, confirmed, actor, model)
    stored = put_draft(manager, project_id, payload)
    return {
        "applicable": True,
        "saved": True,
        "activated": False,
        "reviewed_by": actor,
        "items": confirmed,
        "uncovered_before": uncovered_before,
        "uncovered_after": uncovered_after,
        "retention": stored.get("retention") or payload.get("retention"),
        "contract": stored,
    }
