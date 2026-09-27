#!/usr/bin/env python3
"""Convert approved success-case export items into experience-RAG lesson files.

Each input item is already kind=success with a labelled multi-field content
block. This script generalizes situation/action/outcome into 1-3 lesson
sentences and writes one JSON file per experience for experience_cli add.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "success-cases-export.json"
DEFAULT_OUTPUT = ROOT / "experience_lessons"

LABELS = (
    "事例",
    "状況",
    "施策",
    "成果",
    "成功要因",
    "条件・限界",
    "条件",
    "限界",
)
LABEL_RE = re.compile(
    r"【(?P<label>" + "|".join(LABELS) + r")】\s*(?P<body>.*?)(?=\n【(?:事例|状況|施策|成果|成功要因|条件・限界|条件|限界)】|\Z)",
    re.DOTALL,
)
JP_RE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")


def parse_labelled_content(content: str) -> dict[str, str]:
    text = str(content or "").strip()
    fields: dict[str, str] = {}
    for match in LABEL_RE.finditer(text):
        label = match.group("label")
        body = " ".join(match.group("body").strip().split())
        if body:
            fields[label] = body
    return fields


def is_japanese(text: str) -> bool:
    return bool(JP_RE.search(text or ""))


def take_sentences(text: str, count: int, max_chars: int) -> str:
    text = " ".join(str(text or "").split())
    if not text:
        return ""
    japanese = is_japanese(text)
    if japanese:
        chunks = [part.strip() for part in re.split(r"(?<=。)", text) if part.strip()]
        selected = "".join(chunks[:count])
    else:
        chunks = [part.strip() for part in re.split(r"(?<=\.)\s+", text) if part.strip()]
        selected = " ".join(chunks[:count])
    if len(selected) <= max_chars:
        return selected
    if japanese and "。" in selected[:max_chars]:
        return selected[: selected.rfind("。", 0, max_chars) + 1]
    if (not japanese) and "." in selected[:max_chars]:
        return selected[: selected.rfind(".", 0, max_chars) + 1]
    return selected[: max_chars - 1].rstrip("、,; ") + "…"


def strip_end(text: str) -> str:
    return re.sub(r"[。．.\s]+$", "", text or "")


def build_lesson(fields: dict[str, str], fallback: str) -> str:
    situation = take_sentences(fields.get("状況", ""), 1, 240)
    action = take_sentences(fields.get("施策", ""), 2, 280)
    outcome = take_sentences(fields.get("成果", ""), 1, 200)
    sample = " ".join(part for part in (situation, action, outcome) if part)
    japanese = is_japanese(sample or fallback)
    parts: list[str] = []
    if japanese:
        situation_core = strip_end(situation)
        action_core = strip_end(action)
        if situation_core and action_core:
            if situation_core.endswith(("た", "だ", "である", "です", "ます", "った", "いた")):
                parts.append(f"{situation_core}ときは、{action_core}。")
            else:
                parts.append(f"{situation_core}という状況で、{action_core}。")
        elif action_core:
            parts.append(f"{action_core}。")
        elif situation_core:
            parts.append(f"{situation_core}。")
        if outcome:
            parts.append(f"その結果、{strip_end(outcome)}。")
        lesson = "".join(parts).strip()
    else:
        for part in (situation, action, outcome):
            if not part:
                continue
            parts.append(part if part.endswith(".") else strip_end(part) + ".")
        lesson = " ".join(parts).strip()
    if not lesson:
        lesson = take_sentences(fallback, 2, 400)
    lesson = " ".join(lesson.split())
    if len(lesson) > 700:
        lesson = take_sentences(lesson, 3, 700)
    if lesson and not lesson.endswith(("。", ".", "…")):
        lesson += "。" if is_japanese(lesson) else "."
    return lesson


def convert_item(item: dict) -> dict:
    if not isinstance(item, dict):
        raise ValueError("item must be an object")
    kind = item.get("kind") or "success"
    if kind != "success":
        raise ValueError("unsupported kind: %s" % kind)
    content = item.get("content") or ""
    fields = parse_labelled_content(content)
    lesson = build_lesson(fields, content)
    if not lesson.strip():
        raise ValueError("empty lesson content")
    evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    source = str(evidence.get("source") or "").strip()
    if not source:
        raise ValueError("evidence.source is required")
    return {
        "kind": "success",
        "content": lesson,
        "applicability": {},
        "evidence": {"source": source},
    }


def load_items(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    items = payload.get("export", {}).get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("export.items is empty")
    return items


def write_lessons(items: list[dict], output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for index, item in enumerate(items, 1):
        record = convert_item(item)
        path = output_dir / f"lesson-{index:02d}.json"
        path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="成功事例JSONを経験RAG登録形式へ変換する")
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args(argv)
    input_path = Path(args.input)
    output_dir = Path(args.output)
    items = load_items(input_path)
    paths = write_lessons(items, output_dir)
    print("wrote %d files under %s" % (len(paths), output_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
