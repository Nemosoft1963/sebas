"""修正B: 実行後の判定を証拠付き項目別にする。

形式検査は前段として保持し、各必須項目について位置・引用・方法・結果・理由を
保存する。数・見出し・必須語句は決定的に検査する。文意はローカルLLMで根拠箇所
とともに確認するが、根拠なし・曖昧・読取失敗はPASSにしない。
"""
from __future__ import annotations

import hashlib
import re


RESULTS = ("PASS", "FAIL", "UNTESTABLE")
HUMAN_CONFIRMED = "HUMAN_CONFIRMED"
# 集約でPASS相当として扱う結果。機械のPASSと人の確認は区別して保存する。
PASS_EQUIVALENT = ("PASS", HUMAN_CONFIRMED)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _cut(text: str, start: int, length: int = 160) -> str:
    flat = re.sub(r"\s+", " ", str(text or ""))
    stop = min(len(flat), start + length)
    return flat[start:stop]


def _count_markers(content: str, markers: list[str]) -> tuple[int, str, int]:
    """数量の決定的な数え方。分母0・件数不明で率や件数を作らない。"""
    lines = str(content or "").splitlines()
    best = 0
    best_marker = ""
    best_pos = -1
    for marker in markers or []:
        marker = str(marker or "")
        if not marker:
            continue
        if marker in {"##", "#"}:
            count = sum(1 for line in lines if line.lstrip().startswith(marker + " "))
            pos = next((i for i, line in enumerate(lines) if line.lstrip().startswith(marker + " ")), -1)
        else:
            count = sum(1 for line in lines if marker in line)
            pos = next((i for i, line in enumerate(lines) if marker in line), -1)
        if count > best:
            best, best_marker, best_pos = count, marker, pos
    return best, best_marker, best_pos


def _find_heading(content: str, heading: str) -> tuple[bool, int, str]:
    lines = str(content or "").splitlines()
    wanted = _norm(heading)
    for index, line in enumerate(lines):
        match = re.match(r"^\s*#{1,6}\s+(.+?)\s*$", line)
        if not match:
            continue
        if _norm(match.group(1)) == wanted or wanted in _norm(match.group(1)):
            return True, index, _cut("\n".join(lines[max(0, index - 1):index + 2]), 0)
    # 番号付き見出しの吸収。
    stripped = re.sub(r"^[0-9０-９]+\s*[.．、)）:]\s*", "", wanted)
    for index, line in enumerate(lines):
        match = re.match(r"^\s*#{1,6}\s+(.+?)\s*$", line)
        if not match:
            continue
        if _norm(match.group(1)) == stripped or stripped in _norm(match.group(1)):
            return True, index, _cut("\n".join(lines[max(0, index - 1):index + 2]), 0)
    return False, -1, ""


def _find_phrase(content: str, keywords: list[str], match: str = "all") -> tuple[bool, int, str]:
    """必須語句の存在確認。match='any'は『いずれか』、『all』は全てを要求する。

    既定は'all'(従来どおり)。'any'/'all'以外は'all'として扱う(fail-closed)。
    """
    keys = [str(k) for k in (keywords or []) if str(k)]
    if not keys:
        return True, 0, _cut(content, 0, 120)
    kind = str(match or "all").lower()
    if kind not in ("any", "all"):
        kind = "all"
    blob = str(content or "")
    if kind == "any":
        lines = blob.splitlines()
        for index, line in enumerate(lines):
            if any(k in line for k in keys):
                return True, index, _cut(line, 0)
        positions = []
        for keyword in keys:
            pos = blob.find(keyword)
            if pos >= 0:
                positions.append(pos)
        if not positions:
            return False, -1, ""
        first = min(positions)
        return True, first, _cut(blob, first)
    lines = blob.splitlines()
    for index, line in enumerate(lines):
        if all(str(k) in line for k in keys):
            return True, index, _cut(line, 0)
    # 複数語は同一行に無くても、全語が文書内にあれば位置は最初の語とする。
    positions = []
    for keyword in keys:
        pos = blob.find(keyword)
        if pos < 0:
            return False, -1, ""
        positions.append(pos)
    first = min(positions)
    return True, first, _cut(blob, first)


def _check_forbid(content: str, forbid: list[str]) -> tuple[bool, int, str]:
    for phrase in forbid or []:
        phrase = str(phrase or "")
        if not phrase:
            continue
        pos = str(content or "").find(phrase)
        if pos >= 0:
            return False, pos, _cut(content, pos)
    return True, 0, ""


def _read_text(resolve, path: str) -> tuple[str | None, str]:
    try:
        target = resolve(path)
        return target.read_text(encoding="utf-8-sig"), ""
    except (OSError, ValueError) as exc:
        return None, f"読み取り失敗: {type(exc).__name__}"


# P1-1: 日時の役割別照合(決定的・対象成果物に限定)。許容表記(15:00/15時/午後3時/
# 正午、日付の2026年11月14日/11/14等)を正規化(分・年月日)して照合する。
# 別の時刻や別の成果物の値を拾わない。代表値を1つだけ記録に残す。
def _p1_parse_artifact_time(raw: str) -> tuple[int, int] | None:
    import unicodedata as _ud

    body = re.sub(r"[\s　]+", "", _ud.normalize("NFKC", str(raw or "")))
    if body == "正午":
        return 12, 0
    match = re.match(r"^(午前|午後)?(\d{1,2})時(\d{1,2})?分?$", body)
    if match:
        meridiem, hour, minute = match.group(1) or "", int(match.group(2)), int(match.group(3) or 0)
        if meridiem == "午後" and hour < 12:
            hour += 12
        if meridiem == "午前" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
        return None
    match = re.match(r"^(\d{1,2}):(\d{2})$", body)
    if match and 0 <= int(match.group(1)) <= 23 and 0 <= int(match.group(2)) <= 59:
        return int(match.group(1)), int(match.group(2))
    return None


def _p1_parse_artifact_date(raw: str) -> tuple[int, int, int] | None:
    import unicodedata as _ud

    body = re.sub(r"[\s　]+", "", _ud.normalize("NFKC", str(raw or "")))
    match = re.match(r"^(\d{4})年(\d{1,2})月(\d{1,2})日$", body)
    if match:
        return int(match.group(1)), int(match.group(2)), int(match.group(3))
    match = re.match(r"^(\d{4})/(\d{1,2})/(\d{1,2})$", body)
    if match:
        return int(match.group(1)), int(match.group(2)), int(match.group(3))
    match = re.match(r"^(\d{1,2})月(\d{1,2})日$", body)
    if match:
        return 0, int(match.group(1)), int(match.group(2))
    match = re.match(r"^(\d{1,2})/(\d{1,2})$", body)
    if match:
        return 0, int(match.group(1)), int(match.group(2))
    return None


def _check_datetime_item(item_id: str, content: str, expected: dict) -> dict:
    """役割別日時項目の検証。対象の成果物本文だけを見て、原本値を正規化照合する。"""
    role = expected.get("datetime_role")
    keywords = [str(k) for k in (expected.get("keywords") or []) if str(k)]
    minutes = expected.get("minutes")
    date_parts = expected.get("date")
    canonical = str(expected.get("canonical") or "")
    label = {None: "日時", "event_date": "開催日", "start_time": "開始時刻",
             "end_time": "終了時刻", "prep_time": "準備開始時刻"}.get(role, "日時")
    blob = str(content or "")
    found_kind = ""
    found_raw = ""
    found_pos = -1
    if isinstance(minutes, int):
        for match in re.finditer(
                r"(午前|午後)?\d{1,2}時(?:\d{1,2}分)?|\d{1,2}:\d{2}|正午", blob):
            parsed = _p1_parse_artifact_time(match.group(0))
            if parsed is not None and parsed[0] * 60 + parsed[1] == minutes:
                found_kind, found_raw, found_pos = "time", match.group(0), match.start()
                break
    elif isinstance(date_parts, (list, tuple)) and len(date_parts) == 3:
        try:
            want_y, want_m, want_d = int(date_parts[0]), int(date_parts[1]), int(date_parts[2])
        except (TypeError, ValueError):
            want_y, want_m, want_d = -1, -1, -1
        for match in re.finditer(
                r"\d{4}年\d{1,2}月\d{1,2}日|\d{4}/\d{1,2}/\d{1,2}"
                r"|\d{1,2}月\d{1,2}日|\d{1,2}/\d{1,2}", blob):
            parsed = _p1_parse_artifact_date(match.group(0))
            if parsed is None:
                continue
            got_y, got_m, got_d = parsed
            # 年が原本に無い表記(11/14)は月日だけで照合する(推測で年を補わない)。
            if want_y == 0 and (got_m, got_d) == (want_m, want_d):
                found_kind, found_raw, found_pos = "date", match.group(0), match.start()
                break
            if want_y != 0 and (got_y, got_m, got_d) == (want_y, want_m, want_d):
                found_kind, found_raw, found_pos = "date", match.group(0), match.start()
                break
    if found_pos < 0 and keywords:
        found, pos, _quote = _find_phrase(blob, keywords, "any")
        if found:
            found_raw = next((k for k in keywords if k in blob), keywords[0])
            found_kind, found_pos = "keyword", pos
    if found_pos < 0:
        want = canonical or "、".join(keywords)
        return {
            "item_id": item_id, "result": "FAIL", "line": -1, "quote": "",
            "reason": f"必須の{label}なし: {want}",
        }
    return {
        "item_id": item_id, "result": "PASS", "line": found_pos,
        "quote": _cut(blob, found_pos, 120),
        "reason": f"必須の{label}を確認: {found_raw}",
    }


def _immediate_child_sections(content: str, heading: str) -> list[tuple[str, str]]:
    """Read named subheadings beneath one Markdown section, without siblings."""
    lines = str(content or "").splitlines()
    found, start, _ = _find_heading(content, heading)
    if not found or start < 0 or start >= len(lines):
        return []
    parent = re.match(r"^\s*(#{1,6})\s+", lines[start])
    if not parent:
        return []
    parent_level = len(parent.group(1))
    children: list[tuple[str, str]] = []
    child_title = ""
    body: list[str] = []
    for line in lines[start + 1:]:
        child_heading = re.match(r"^\s*(#{1,6})\s+(.+)", line)
        if child_heading:
            level = len(child_heading.group(1))
            if level <= parent_level:
                break
            if level == parent_level + 1:
                if child_title:
                    children.append((child_title, "".join(body)))
                child_title, body = child_heading.group(2).strip(), []
            continue
        if child_title and line.strip() and not line.lstrip().startswith(("- ", "* ", "|")):
            body.append(line.strip())
    if child_title:
        children.append((child_title, "".join(body)))
    return children


def check_item_deterministic(item: dict, content: str | None, read_error: str = "") -> dict:
    """決定的な検査(count/headings/phrase)。読取失敗・根拠なしはPASSにしない。"""
    item_id = str(item.get("item_id") or "")
    method = str(item.get("method") or "")
    expected = item.get("expected") if isinstance(item.get("expected"), dict) else {}
    required = bool(item.get("required", True))
    if content is None:
        return {
            "item_id": item_id, "method": method, "result": "UNTESTABLE",
            "required": required, "line": -1, "quote": "",
            "reason": read_error or "読み取り失敗のため判定不能",
        }
    if method == "count":
        markers = list(expected.get("markers") or [])
        wanted = expected.get("count")
        try:
            wanted_number = int(wanted)
        except (TypeError, ValueError):
            return {
                "item_id": item_id, "method": method, "result": "UNTESTABLE",
                "required": required, "line": -1, "quote": "",
                "reason": "数量の期待値が未定義のため判定不能",
            }
        found, marker, pos = _count_markers(content, markers)
        unit = str(expected.get("unit") or "")
        if unit == "段階":
            # Numbered Markdown stage headings are more reliable than the
            # incidental occurrence of words such as ステップ in prose.
            stage_headings = list(re.finditer(
                r"(?m)^\s*#{1,6}\s*(\d+)[.)．]\s+\S", content
            ))
            if stage_headings:
                numbers = [int(match.group(1)) for match in stage_headings]
                found, marker, pos = len(stage_headings), "番号付き段階見出し", stage_headings[0].start()
                if numbers != list(range(1, len(numbers) + 1)):
                    found = -1
        elif unit == "品":
            products = _immediate_child_sections(content, "魅力の説明")
            if products:
                found, marker, pos = len(products), "商品別見出し", content.find(products[0][0])
        elif unit == "件" and re.search(r"(?im)^\s*#{1,6}\s*FAQ(?:\b|[（(])", content):
            # Count questions only inside the FAQ section, never all question
            # marks or generic 件/項目 words in the rest of the document.
            faq_heading = re.search(r"(?im)^\s*#{1,6}\s*FAQ(?:\b|[（(])[^\n]*", content)
            section_start = faq_heading.end()
            next_heading = re.search(r"(?m)^\s*#{1,6}\s+", content[section_start:])
            section_end = section_start + next_heading.start() if next_heading else len(content)
            question_rows = list(re.finditer(
                r"(?m)^\s*(\d+)[.)．]\s+\S", content[section_start:section_end]
            ))
            if question_rows:
                numbers = [int(match.group(1)) for match in question_rows]
                found, marker, pos = len(question_rows), "FAQ番号付き質問", section_start + question_rows[0].start()
                if numbers != list(range(1, len(numbers) + 1)):
                    found = -1
        quote = _cut(content, max(0, pos), 120) if pos >= 0 else ""
        if found == wanted_number:
            return {
                "item_id": item_id, "method": method, "result": "PASS",
                "required": required, "line": pos, "quote": quote,
                "reason": f"数量が一致: {found}件(期待{wanted_number}件)",
            }
        return {
            "item_id": item_id, "method": method, "result": "FAIL",
            "required": required, "line": pos, "quote": quote,
            "reason": f"数量が不一致: {found}件(期待{wanted_number}件)",
        }
    if method == "section_char_count":
        headings = [str(h) for h in (expected.get("headings") or []) if str(h)]
        try:
            minimum, maximum = int(expected.get("minimum")), int(expected.get("maximum"))
        except (TypeError, ValueError):
            minimum, maximum = -1, -1
        if minimum < 1 or maximum < minimum or not headings:
            return {"item_id": item_id, "method": method, "result": "UNTESTABLE",
                    "required": required, "line": -1, "quote": "", "reason": "字数条件が未定義"}
        lines = content.splitlines()
        found_lengths = []
        for heading in headings:
            found, start, _quote = _find_heading(content, heading)
            if not found:
                return {"item_id": item_id, "method": method, "result": "FAIL",
                        "required": required, "line": -1, "quote": "",
                        "reason": "字数検査対象の見出し不足: " + heading}
            children = _immediate_child_sections(content, heading)
            if children:
                found_lengths.extend((title, len(body)) for title, body in children)
            else:
                section = []
                for line in lines[start + 1:]:
                    if re.match(r"^\s*#{1,6}\s+", line):
                        break
                    if line.strip() and not line.lstrip().startswith(("- ", "* ", "|")):
                        section.append(line.strip())
                found_lengths.append((heading, len("".join(section))))
        valid = all(minimum <= length <= maximum for _, length in found_lengths)
        return {"item_id": item_id, "method": method,
                "result": "PASS" if valid else "FAIL", "required": required,
                "line": -1, "quote": str(found_lengths)[:300],
                "reason": f"各見出し下の本文字数を確認（許容{minimum}～{maximum}字）"}
    if method == "approval_stage_matrix":
        stages = [str(stage) for stage in (expected.get("stages") or []) if str(stage)]
        terms = [str(term) for term in (expected.get("decision_terms") or []) if str(term)]
        if len(stages) < 3 or not terms:
            return {"item_id": item_id, "method": method, "result": "UNTESTABLE",
                    "required": required, "line": -1, "quote": "", "reason": "段階または承認判断語が未定義"}
        lines = content.splitlines()
        missing = []
        matched = []
        for stage in stages:
            candidates = [(index, line) for index, line in enumerate(lines) if stage in line]
            def local_decision(index: int, line: str) -> bool:
                nearby = [line]
                for next_line in lines[index + 1:index + 3]:
                    if re.match(r"^\s*#{1,6}\s+", next_line):
                        break
                    nearby.append(next_line)
                return any(term in segment for term in terms for segment in nearby)
            decided = next(((index, line) for index, line in candidates
                            if local_decision(index, line)), None)
            if decided is None:
                missing.append(stage)
            else:
                matched.append(f"{stage}@{decided[0] + 1}")
        return {"item_id": item_id, "method": method,
                "result": "FAIL" if missing else "PASS", "required": required,
                "line": -1, "quote": ", ".join(matched)[:300],
                "reason": "承認要否がない段階: " + "、".join(missing) if missing else "全段階の承認要否を確認"}
    if method == "headings":
        headings = [str(h) for h in (expected.get("headings") or []) if str(h).strip()]
        if not headings:
            return {
                "item_id": item_id, "method": method, "result": "UNTESTABLE",
                "required": required, "line": -1, "quote": "",
                "reason": "検査方法未定義のため判定不能",
            }
        missing = []
        first_line, first_quote = -1, ""
        for heading in headings:
            found, line, quote = _find_heading(content, heading)
            if not found:
                missing.append(heading)
            elif first_line < 0:
                first_line, first_quote = line, quote
        if not missing:
            return {
                "item_id": item_id, "method": method, "result": "PASS",
                "required": required, "line": first_line, "quote": first_quote,
                "reason": "必須見出しを確認: " + "、".join(headings),
            }
        return {
            "item_id": item_id, "method": method, "result": "FAIL",
            "required": required, "line": -1, "quote": "",
            "reason": "必須見出し不足: " + "、".join(missing),
        }
    # A1: 対象成果物で照合する。契約に target_artifact がある場合は
    # 保存された対象の本文だけを見る(別の成果物にあるだけでは PASS にしない)。
    target_artifact = ""
    if isinstance(expected, dict):
        target_artifact = str(expected.get("target_artifact") or "")
    if target_artifact and str(item.get("artifact_path") or "") != target_artifact:
        return {
            "item_id": item_id, "method": method, "result": "FAIL",
            "required": required, "line": -1, "quote": "",
            "reason": f"対象成果物の不一致: {item.get('artifact_path')} は {target_artifact} ではない",
        }
    if method == "phrase":
        ok_forbid, forbid_pos, forbid_quote = _check_forbid(content, list(expected.get("forbid") or []))
        if not ok_forbid:
            return {
                "item_id": item_id, "method": method, "result": "FAIL",
                "required": required, "line": forbid_pos, "quote": forbid_quote,
                "reason": "禁止表現を検出",
            }
        keywords = [str(k) for k in (expected.get("keywords") or []) if str(k)]
        match_kind = str(expected.get("match") or "all").lower()
        if match_kind not in ("any", "all"):
            match_kind = "all"
        role = expected.get("datetime_role")
        minutes = expected.get("minutes")
        date_parts = expected.get("date")
        if role is not None or minutes is not None or date_parts is not None:
            checked = _check_datetime_item(
                str(item_id or ""), str(content or ""),
                expected if isinstance(expected, dict) else {},
            )
            checked["required"] = required
            checked["method"] = method
            return checked
        found, pos, quote = _find_phrase(content, keywords, match_kind)
        if not found:
            return {
                "item_id": item_id, "method": method, "result": "FAIL",
                "required": required, "line": -1, "quote": "",
                "reason": "必須語句なし: " + "、".join(keywords),
            }
        return {
            "item_id": item_id, "method": method, "result": "PASS",
            "required": required, "line": pos, "quote": quote,
            "reason": "必須語句を確認: " + ("、".join(keywords) if keywords else "内容あり"),
        }
    return {
        "item_id": item_id, "method": method, "result": "UNTESTABLE",
        "required": required, "line": -1, "quote": "",
        "reason": "検査方法未定義のため判定不能",
    }


def build_local_semantic_judge(local_complete) -> object:
    """文意の項目の判定器。既存のローカル呼出(_local_complete等)を接続する。

    判定は根拠の引用つき。LLMが使えない・失敗・曖昧・引用が成果物と一致しない
    → UNTESTABLE(PASSにしない)。外部AIは使わない。local_complete は
    async (system, prompt) -> str を受け、JSON文字列を返す想定。
    """
    import asyncio as _asyncio
    import json as _json

    def _judge(item: dict, content: str):
        requirement = str((item or {}).get("requirement") or "")
        prompt = (
            "次の要求が成果物で満たされているか判定してください。"
            "要求に『この成果物の担当範囲（…）』があれば、その範囲に対応する条件だけをこの成果物で判定してください。"
            "他の成果物が担当する条件をこの成果物に要求しないでください。"
            "担当範囲内の必須内容が欠けているならFAILとし、根拠の引用つきでJSONだけを返してください。"
            '{"result":"PASS|FAIL|UNTESTABLE","quote":"成果物からの抜粋","reason":"理由","line":-1}\n'
            f"要求: {requirement[:500]}\n"
            f"成果物:\n{str(content or '')[:6000]}"
        )
        try:
            result = local_complete(
                "あなたはローカルで動作する文意の判定器です。根拠の引用なしにPASSにしません。",
                prompt,
            )
        except Exception as exc:
            # Network timeouts (for example httpx.ReadTimeout) are also
            # unverifiable, never a PASS or a task-wide crash.
            raise RuntimeError(f"local semantic judge failed: {type(exc).__name__}") from exc
        if _asyncio.iscoroutine(result):
            raise RuntimeError("local semantic judge must be sync in verification path")
        try:
            verdict = _json.loads(str(result or ""))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("local semantic judge returned non-JSON") from exc
        if not isinstance(verdict, dict):
            raise RuntimeError("local semantic judge returned non-dict")
        return verdict

    return _judge


def check_item_semantic(item: dict, content: str | None, read_error: str = "",
                        semantic_judge=None) -> dict:
    """文意の確認はローカルLLMのみ。根拠なし・曖昧・失敗はPASSにしない。

    P1-1: 例外は種別・タイムアウトか・応答形式の不正か・項目ID・成果物パスだけを
    記録し、秘密情報・原本本文・LLM応答本文は含めない。エラーは UNTESTABLE
    のまま(判定エラーを PASS に読み替えない)。
    """
    item_id = str(item.get("item_id") or "")
    required = bool(item.get("required", True))
    expected = item.get("expected") if isinstance(item.get("expected"), dict) else {}
    keywords = [str(k) for k in (expected.get("keywords") or []) if str(k)]
    artifact_path = str(item.get("artifact_path") or "")

    def _diag(reason: str, quote: str = "") -> dict:
        return {
            "item_id": item_id, "method": "llm_semantic", "result": "UNTESTABLE",
            "required": required, "line": -1, "quote": quote,
            "reason": reason, "artifact_path": artifact_path,
            "semantic_diagnosis": {
                "item_id": item_id, "artifact_path": artifact_path,
                "detail": reason,
            },
        }
    if content is None:
        return _diag(read_error or "読み取り失敗のため判定不能")
    if keywords:
        found, pos, _ = _find_phrase(content, keywords)
        if not found:
            return {
                "item_id": item_id, "method": "llm_semantic", "result": "FAIL",
                "required": required, "line": -1, "quote": "",
                "reason": "判断の根拠語句なし: " + "、".join(keywords),
                "artifact_path": artifact_path,
            }
    if semantic_judge is None:
        return _diag("人の確認待ち: ローカルLLMの判断が未実施")
    try:
        verdict = semantic_judge(item, content)
    except Exception as exc:
        kind_name = type(exc).__name__
        text = f"{kind_name}: {exc}"
        # タイムアウト(httpx.ReadTimeout等)は種別として明示する。内容は載せない。
        if "timeout" in kind_name.lower() or "timeout" in text.lower():
            detail = (f"人の確認待ち: ローカルLLMの失敗({kind_name}、タイムアウト、"
                      f"項目={item_id}、成果物={artifact_path})")
        elif "JSON" in kind_name or "JSON" in text or "non-" in text.lower():
            detail = (f"人の確認待ち: ローカルLLMの失敗({kind_name}、応答形式の不正、"
                      f"項目={item_id}、成果物={artifact_path})")
        else:
            detail = (f"人の確認待ち: ローカルLLMの失敗({kind_name}、"
                      f"項目={item_id}、成果物={artifact_path})")
        return _diag(detail)
    if not isinstance(verdict, dict):
        return _diag(f"人の確認待ち: ローカルLLMの失敗(応答形式の不正、項目={item_id}、成果物={artifact_path})")
    result = str(verdict.get("result") or "").upper()
    quote = str(verdict.get("quote") or "")
    reason = str(verdict.get("reason") or "")
    line = verdict.get("line", -1)
    try:
        line_number = int(line)
    except (TypeError, ValueError):
        line_number = -1
    if result not in RESULTS:
        return _diag(f"人の確認待ち: 判断が曖昧(項目={item_id}、成果物={artifact_path})")
    if result == "PASS" and not quote.strip():
        return _diag(f"人の確認待ち: 根拠引用なし(項目={item_id}、成果物={artifact_path})")
    if result == "PASS" and quote not in str(content or ""):
        return _diag(f"人の確認待ち: 引用が成果物と一致しない(項目={item_id}、成果物={artifact_path})")
    checked = {
        "item_id": item_id, "method": "llm_semantic", "result": result,
        "required": required, "line": line_number, "quote": quote[:300],
        "reason": reason[:500] or ("根拠付きで確認" if result == "PASS" else "根拠付きで不適合"),
        "artifact_path": artifact_path,
    }
    return checked


def verify_requirement_items(task: dict, resolve, semantic_judge=None,
                             registered_text: str | None = None) -> list[dict]:
    """一般文書の実行器に追加する項目別検証。形式検査は前段として保持される。

    registered_text を渡すと、保存後の成果物本文の根拠確認(P0-C)を既存行の
    後に追加する。渡さない場合は従来どおり(既存呼び出しの互換を維持)。
    """
    import json as _json

    try:
        contract = _json.loads(task.get("acceptance_criteria", ""))
    except (TypeError, ValueError):
        return []
    if not isinstance(contract, dict):
        return []
    matrix = contract.get("requirement_matrix")
    if not isinstance(matrix, list) or not matrix:
        return []
    rows: list[dict] = []
    cache: dict[str, tuple[str | None, str]] = {}
    for item in sorted(matrix, key=lambda r: str(r.get("item_id") or "") if isinstance(r, dict) else ""):
        if not isinstance(item, dict):
            continue
        path = str(item.get("artifact_path") or "")
        # 追補は説明資料であり検証対象の成果物ではない。契約が壊れて追補を指しても
        # その本文を合否判定に使用せず、対象不明として fail-closed にする。
        if re.match(r"(?:^|/)goal_supplement_[^/]*\.md$", path):
            rows.append({
                "item_id": str(item.get("item_id") or ""),
                "method": str(item.get("method") or ""),
                "result": "UNTESTABLE", "required": bool(item.get("required", True)),
                "artifact_path": path, "line": -1, "quote": "",
                "reason": "追補ファイルは項目の合否判定に使用しない",
                "requirement": str(item.get("requirement") or ""),
                "criterion_id": str(item.get("criterion_id") or ""),
                "expected": dict(item.get("expected") or {}),
            })
            continue
        if not path:
            rows.append({
                "item_id": str(item.get("item_id") or ""), "method": str(item.get("method") or ""),
                "result": "UNTESTABLE", "required": bool(item.get("required", True)),
                "artifact_path": "", "line": -1, "quote": "",
                "reason": "対象成果物不明のため判定不能",
            })
            continue
        if path not in cache:
            cache[path] = _read_text(resolve, path)
        content, read_error = cache[path]
        method = str(item.get("method") or "")
        if method == "artifact_count":
            expected = item.get("expected") if isinstance(item.get("expected"), dict) else {}
            outputs = [str(row.get("path") or "") for row in (contract.get("outputs") or [])
                       if isinstance(row, dict) and str(row.get("path") or "").endswith(".md")]
            wanted = expected.get("count")
            try:
                wanted = int(wanted)
            except (TypeError, ValueError):
                wanted = -1
            count_ok = wanted > 0 and (bool(expected.get("per_output")) or len(outputs) == wanted)
            if expected.get("per_output"):
                count_ok = count_ok and wanted == 1 and bool(outputs)
                if expected.get("distinct_categories"):
                    categories = [str(row.get("artifact_category") or "") for row in
                                  (contract.get("outputs") or []) if isinstance(row, dict)
                                  and str(row.get("path") or "").endswith(".md")]
                    count_ok = count_ok and len(categories) == len(set(categories)) and all(categories)
            missing = []
            for output_path in outputs:
                try:
                    if not resolve(output_path).is_file():
                        missing.append(output_path)
                except (OSError, ValueError, RuntimeError, AttributeError):
                    missing.append(output_path)
            result = "PASS" if count_ok and not missing else "FAIL"
            checked = {"item_id": str(item.get("item_id") or ""), "method": method,
                       "result": result, "required": bool(item.get("required", True)),
                       "line": -1, "quote": ", ".join(outputs)[:300] if result == "PASS" else "",
                       "reason": "成果物点数とファイルを確認" if result == "PASS" else
                                 "成果物点数またはファイルの不一致"}
        elif method == "llm_semantic":
            checked = check_item_semantic(item, content, read_error, semantic_judge)
        elif method == "input_missing":
            # P0-A: 判定不能の記録。合格にも不合格にも倒さない。理由と必要な入力を残す。
            checked = {
                "item_id": str(item.get("item_id") or ""), "method": method,
                "result": "UNTESTABLE", "required": bool(item.get("required", True)),
                "line": -1, "quote": "",
                "reason": str(((item.get("expected") if isinstance(item.get("expected"), dict) else {}) or {}).get("reason") or "入力不足のため判定不能"),
            }
        elif method in ("count", "section_char_count", "approval_stage_matrix", "headings", "phrase"):
            checked = check_item_deterministic(item, content, read_error)
        else:
            checked = {
                "item_id": str(item.get("item_id") or ""), "method": method,
                "result": "UNTESTABLE", "required": bool(item.get("required", True)),
                "line": -1, "quote": "",
                "reason": "検査方法未定義のため判定不能",
            }
        checked["artifact_path"] = path
        checked["requirement"] = str(item.get("requirement") or "")
        checked["criterion_id"] = str(item.get("criterion_id") or "")
        checked["expected"] = dict(item.get("expected") or {})
        rows.append(checked)
    # 合否は契約済み成果物の本文だけで決める。resolve 実装が result/ 配下の追補を
    # 副作用として走査・生成しても、同じ入力成果物に対する結果は変化させない。
    # （行の生成順や追補ファイル自体を検証対象へ追加する処理は行わない。）
    # P0-C: 成果物本文の根拠確認は既存の項目別検証の後に追加する。
    # 既存の具体的拒否/FAILの理由を隠さない(順序を守る)。要確認は必須の
    # UNTESTABLEとして記録し、単純削除しない。修正候補は提示のみで自動適用しない。
    if registered_text is not None:
        try:
            rows.extend(build_p0c_grounding_rows(task, resolve, registered_text))
        except (ValueError, TypeError, AttributeError, RuntimeError) as exc:
            rows.append({
                "item_id": "P0C-EXEC-01",
                "criterion_id": str(((contract.get("criterion_ids") or [""])[0]) or ""),
                "artifact_path": "",
                "requirement": "成果物本文の根拠確認",
                "method": "artifact_grounding",
                "result": "UNTESTABLE",
                "required": True,
                "line": -1,
                "quote": "",
                "reason": f"検査できなかった: 根拠確認の実行失敗({type(exc).__name__})。要確認",
            })
    return rows


def summarize_criterion(rows: list[dict]) -> dict[str, str]:
    """SCのPASSは配下の必須項目がすべてPASSのときだけ。

    文意の項目(llm_semantic)の HUMAN_CONFIRMED はPASS相当として集約する。
    機械項目(count/headings/phrase)の HUMAN_CONFIRMED は集約しない
    (台帳に記録されないため、ここに来たら UNTESTABLE に倒す)。
    """
    by_criterion: dict[str, str] = {}
    grouped: dict[str, list[dict]] = {}
    for row in rows or []:
        grouped.setdefault(str(row.get("criterion_id") or ""), []).append(row)
    for criterion_id, items in grouped.items():
        required_items = [r for r in items if r.get("required", True)]
        if not required_items:
            by_criterion[criterion_id] = "UNTESTABLE"
            continue
        normalized: list[str] = []
        for row in required_items:
            result = str(row.get("result") or "")
            method = str(row.get("method") or "")
            if result == HUMAN_CONFIRMED and method != "llm_semantic":
                normalized.append("UNTESTABLE")
            else:
                normalized.append(result)
        results = normalized
        if any(r == "FAIL" for r in results):
            by_criterion[criterion_id] = "FAIL"
        elif any(r == "UNTESTABLE" for r in results):
            by_criterion[criterion_id] = "UNTESTABLE"
        elif all(r in PASS_EQUIVALENT for r in results):
            by_criterion[criterion_id] = "PASS"
        else:
            by_criterion[criterion_id] = "UNTESTABLE"
    return by_criterion


def summarize_with_human(rows: list[dict]) -> dict[str, str]:
    """SC判定の集約。PASS/FAIL/人の確認待ち(UNTESTABLE)を区別する。

    必須項目がすべて PASS または(文意の項目で)HUMAN_CONFIRMED のときだけ PASS。
    FAIL が1件でもあれば FAIL。UNTESTABLE が残れば人の確認待ち。
    """
    return summarize_criterion(rows)


def ledger_payload(project_id: str, plan_signature: str, source_hash: str,
                   rows: list[dict], artifact_hashes: dict[str, str]) -> dict:
    """項目別検証台帳の1件。最終検証レポートはこの台帳からのみ生成する。"""
    ordered = sorted(rows or [], key=lambda r: (
        str(r.get("criterion_id") or ""), str(r.get("item_id") or "")))
    material = repr(sorted(
        (str(r.get("item_id") or ""), str(r.get("artifact_path") or ""),
         str(r.get("result") or ""), str(r.get("quote") or "")[:80])
        for r in ordered
    ))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return {
        "project_id": str(project_id or ""),
        "plan_signature": str(plan_signature or ""),
        "source_hash": str(source_hash or ""),
        "artifact_hashes": dict(artifact_hashes or {}),
        "rows": ordered,
        "criterion_verdicts": summarize_criterion(ordered),
        "ledger_digest": digest,
    }


def ledger_invalidated(saved: dict, plan_signature: str, source_hash: str,
                       artifact_hashes: dict[str, str]) -> list[str]:
    """計画・原本・成果物の変更で該当項目の判定を無効化する。"""
    invalid: list[str] = []
    if not isinstance(saved, dict):
        return []
    if str(saved.get("plan_signature") or "") != str(plan_signature or ""):
        invalid.append("plan_changed")
    if str(saved.get("source_hash") or "") != str(source_hash or ""):
        invalid.append("source_changed")
    before = saved.get("artifact_hashes") if isinstance(saved.get("artifact_hashes"), dict) else {}
    for path, digest in (artifact_hashes or {}).items():
        if str(before.get(path) or "") != str(digest or ""):
            invalid.append(f"artifact_changed:{path}")
    return sorted(set(invalid))


# P0-C: 成果物本文の根拠確認(保存後・完了ゲート前)。決定的・ローカルのみ。
# 確約語は汎用の語に限り、特定案件の語彙を製品コードに入れない。
# 照合は完全一致・正規化一致(全角/半角・空白・表記ゆれ)に限り、文意の推測はしない。
# LLM出力の自己申告は入力として読まない。失敗は拒否・UNTESTABLE・例外送出の
# いずれかにし、代替値で続行しない。
P0C_VERSION = "p0c-artifact-grounding-v1"
P0C_METHOD = "artifact_grounding"

P0C_PROMISE_STEMS = (
    "返却", "返金", "無料", "無償", "保証", "割引", "必ず", "お約束", "お預かり",
    "支払いは不要", "お支払いは不要",
)
P0C_HEDGE_MARKERS = ("要確認", "提案", "仮", "未確認", "検討中", "確認中")

_P0C_SENT_SPLIT_RE = re.compile(r"(?<=[。！？])|\n+")


def _p0c_norm(text: str) -> str:
    import unicodedata as _ud

    return re.sub(r"[\s　]+", "", _ud.normalize("NFKC", str(text or "")))


def _p0c_split_sentences(text: str) -> list[tuple[str, int, int]]:
    """成果物本文を文(句点・改行)に分ける。(文, 行番号, 文字位置)を返す。順序安定。"""
    blob = str(text or "")
    lines = blob.splitlines()
    out: list[tuple[str, int, int]] = []
    offset = 0
    for line_no, line in enumerate(lines, 1):
        parts = [p for p in _P0C_SENT_SPLIT_RE.split(line) if str(p or "").strip()]
        if not parts and line.strip() == "":
            offset += len(line) + 1
            continue
        if not parts:
            parts = [line]
        for part in parts:
            sent = str(part or "").strip()
            if not sent:
                continue
            pos = blob.find(sent, offset)
            if pos < 0:
                pos = offset
            out.append((sent, line_no, pos))
            offset = pos + len(sent)
    return out


def _p0c_registered_canons(registered_text: str) -> tuple[set[tuple[str, str]], list[dict]]:
    """登録事実・原本から抽出した事実の正規化集合(出典つき照合用)。"""
    from app.structured_planning import (
        _p0a_unsupported_candidates as _candidates,
        _p0a_unsupported_canon as _canon,
    )

    found, _scoped = _candidates(str(registered_text or ""))
    canons: set[tuple[str, str]] = set()
    for row in found:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("kind") or "")
        value = str(row.get("value") or "")
        if not kind or not value:
            continue
        canons.add((kind, _canon(kind, value)))
    return canons, [row for row in found if isinstance(row, dict)]


def check_p0c_artifact_grounding(
    artifact_text: str | None,
    registered_text: str | None,
    artifact_path: str = "",
    read_error: str = "",
) -> dict:
    """成果物本文の根拠確認。出典に無い値・確約を列挙する。推測しない。

    戻り値は辞書のみ。checked=False・passed=False(合格にしない)で失敗を表し、
    代替値(空リスト・文字列化・変更前の値)で続行しない。呼び出し側は
    UNTESTABLE行として台帳に残す。
    """
    from app.structured_planning import (
        _p0a_unsupported_candidates as _candidates,
        _p0a_unsupported_canon as _canon,
        _p0c_derived_relative_minutes as _relative_minutes,
    )

    if not isinstance(registered_text, str) or not str(registered_text or "").strip():
        return {"version": P0C_VERSION, "checked": False, "passed": False,
                "checkable": False, "source_readable": False,
                "reason": "source_unreadable: registered_text is missing or empty",
                "unsupported_assertions": [], "out_of_scope": [],
                "suggestions": [], "artifact_path": str(artifact_path or "")}
    if artifact_text is None:
        return {"version": P0C_VERSION, "checked": False, "passed": False,
                "checkable": False, "source_readable": True,
                "reason": f"検査できなかった: {read_error or '成果物が読めない'}",
                "unsupported_assertions": [], "out_of_scope": [],
                "suggestions": [], "artifact_path": str(artifact_path or "")}
    if not str(artifact_text or "").strip():
        return {"version": P0C_VERSION, "checked": False, "passed": False,
                "checkable": False, "source_readable": True,
                "reason": "artifact_unreadable: 成果物が空のため検査できなかった",
                "unsupported_assertions": [], "out_of_scope": [],
                "suggestions": [], "artifact_path": str(artifact_path or "")}
    try:
        source_canons, source_canons_rows = _p0c_registered_canons(registered_text)
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError(f"p0c source extraction failed: {type(exc).__name__}") from exc
    norm_registered = _p0c_norm(registered_text)
    derived_relative_minutes = _relative_minutes(registered_text)
    # P1-1: 原本の確約語(保証・年齢条件など)を正規化集合に含める。原本にある
    # 確約は検出しないが、原本に無い確約はこれまで通り検出する(見逃さない)。
    promise_canons = {_p0c_norm(stem) for stem in P0C_PROMISE_STEMS if _p0c_norm(stem)}
    age_canons = {_p0c_norm(row.get("value") or "") for row in source_canons_rows
                  if isinstance(row, dict) and str(row.get("kind") or "") == "age"}
    sentences = _p0c_split_sentences(artifact_text)
    unsupported: list[dict] = []
    scoped: list[dict] = []
    for sent, line_no, pos in sentences:
        try:
            found, out = _candidates(sent)
        except (ValueError, TypeError, AttributeError) as exc:
            raise RuntimeError(
                f"p0c sentence extraction failed: {type(exc).__name__}") from exc
        for row in found:
            if not isinstance(row, dict):
                continue
            kind = str(row.get("kind") or "")
            value = str(row.get("value") or "")
            if not kind or not value:
                continue
            try:
                canon = _canon(kind, value)
            except (ValueError, TypeError, AttributeError) as exc:
                raise RuntimeError(
                    f"p0c canon failed: {type(exc).__name__}") from exc
            if (kind, canon) in source_canons:
                continue
            # N分前/N分後は、登録時刻同士の差がN分のときだけ導出可能とする。
            relative = re.search(
                rf"(?<![0-9０-９]){re.escape(value)}\s*(前|後)", sent)
            if kind == "number" and relative:
                import unicodedata as _ud
                number = re.match(r"([0-9０-９]{1,3})", value)
                minutes = int(_ud.normalize("NFKC", number.group(1))) if number else -1
                if (relative.group(1) == "前"
                        and ("準備" in sent or "開始" in sent)
                        and minutes in derived_relative_minutes):
                    continue
            unsupported.append({"kind": kind, "value": value,
                                "sentence": sent, "line": line_no, "position": pos})
        for row in out:
            if not isinstance(row, dict):
                continue
            scoped.append({"kind": str(row.get("kind") or "proper"),
                           "value": str(row.get("value") or ""),
                           "sentence": sent, "line": line_no, "position": pos,
                           "reason": str(row.get("reason") or "検査範囲外")})
        norm_sent = _p0c_norm(sent)
        if any(str(m) in sent for m in P0C_HEDGE_MARKERS):
            continue
        hit_stems = [stem for stem in P0C_PROMISE_STEMS if stem and stem in sent]
        if not hit_stems:
            continue
        if norm_sent and norm_sent in norm_registered:
            continue
        grounded = False
        for stem in hit_stems:
            if _p0c_norm(stem) and _p0c_norm(stem) in norm_registered:
                grounded = True
                break
        if grounded:
            continue
        unsupported.append({"kind": "promise", "value": hit_stems[0],
                            "sentence": sent, "line": line_no, "position": pos})
    unsupported.sort(key=lambda r: (str(r.get("kind") or ""), str(r.get("value") or ""),
                                    int(r.get("position") or 0)))
    scoped.sort(key=lambda r: (str(r.get("value") or ""), int(r.get("position") or 0)))
    suggestions = [{
        "sentence": row["sentence"], "position": int(row.get("position") or 0),
        "line": int(row.get("line") or -1),
        "proposal": (
            f"「{str(row.get('sentence') or '')[:80]}」は原本に根拠がないため、"
            f"断定的な記載にせず提案・要確認として記載する（例: 文末に「（要確認：原本に根拠なし）」を付す）。"
            f"自動で削除・変更しない。種別={row.get('kind')}"
        ),
    } for row in unsupported]
    passed = not unsupported and not scoped
    reason = ("out_of_scope_and_unsupported_assertions_present" if unsupported and scoped else
              "out_of_scope_present" if scoped else
              "unsupported_assertions_present" if unsupported else "")
    return {"version": P0C_VERSION, "checked": True, "passed": passed,
            "checkable": True, "source_readable": True, "reason": reason,
            "unsupported_assertions": unsupported, "out_of_scope": scoped,
            "suggestions": suggestions, "artifact_path": str(artifact_path or "")}


def build_p0c_grounding_rows(task: dict, resolve, registered_text: str | None) -> list[dict]:
    """保存後の成果物本文から要確認の必須UNTESTABLE行を作る。単純削除しない。

    registered_text が None(旧呼び出し)のときは空列を返す(既存の挙動を変えない)。
    空文字(出典なし)のときは空集合と照合し、値・確約があれば検出する。
    読めない成果物・出典不能は『検査できなかった』でUNTESTABLEにする。
    修正候補は差分案として提示し、自動適用しない。
    """
    import json as _json

    if registered_text is None:
        return []
    try:
        contract = _json.loads(task.get("acceptance_criteria", ""))
    except (TypeError, ValueError):
        return []
    if not isinstance(contract, dict):
        return []
    criterion_ids = [str(c) for c in (contract.get("criterion_ids") or []) if str(c)]
    if not criterion_ids:
        return []
    outputs = [str(o.get("path") or "") for o in (contract.get("outputs") or [])
               if isinstance(o, dict) and str(o.get("path") or "").lower().endswith(".md")]
    matrix = contract.get("requirement_matrix")
    paths = list(outputs)
    if isinstance(matrix, list):
        for row in matrix:
            if not isinstance(row, dict):
                continue
            path = str(row.get("artifact_path") or "")
            if path.lower().endswith(".md") and path not in paths:
                paths.append(path)
    paths.sort()
    if not paths:
        return []
    by_artifact: dict[str, str] = {}
    if isinstance(matrix, list):
        for row in matrix:
            if not isinstance(row, dict):
                continue
            path = str(row.get("artifact_path") or "")
            cid = str(row.get("criterion_id") or "")
            if path and cid and path not in by_artifact:
                by_artifact[path] = cid
    default_cid = criterion_ids[0]
    rows: list[dict] = []
    counter = 0
    for path in paths:
        cid = by_artifact.get(path, default_cid)
        try:
            content, read_error = _read_text(resolve, path)
        except (OSError, ValueError) as exc:
            content, read_error = None, f"読み取り失敗: {type(exc).__name__}"
        try:
            checked = check_p0c_artifact_grounding(
                content, registered_text, artifact_path=path,
                read_error=read_error)
        except (ValueError, TypeError, AttributeError, RuntimeError) as exc:
            counter += 1
            rows.append({
                "item_id": f"{cid}-P0C-{counter:02d}",
                "criterion_id": cid, "artifact_path": path,
                "requirement": "成果物本文の根拠確認",
                "method": P0C_METHOD, "result": "UNTESTABLE",
                "required": True, "line": -1, "quote": "",
                "reason": f"検査できなかった: 根拠確認の実行失敗({type(exc).__name__})。要確認",
                "expected": {"version": P0C_VERSION, "checkable": False},
            })
            continue
        if not checked.get("checked"):
            counter += 1
            rows.append({
                "item_id": f"{cid}-P0C-{counter:02d}",
                "criterion_id": cid, "artifact_path": path,
                "requirement": "成果物本文の根拠確認",
                "method": P0C_METHOD, "result": "UNTESTABLE",
                "required": True, "line": -1, "quote": "",
                "reason": (
                    f"検査できなかった: {checked.get('reason') or '確認不能'}。"
                    "出典・成果物が読める状態で再検証する。要確認"),
                "expected": {"version": P0C_VERSION, "checkable": False,
                             "reason": str(checked.get("reason") or "")},
            })
            continue
        for entry in list(checked.get("out_of_scope") or []):
            if not isinstance(entry, dict):
                continue
            counter += 1
            sent = str(entry.get("sentence") or "")
            rows.append({
                "item_id": f"{cid}-P0C-{counter:02d}",
                "criterion_id": cid, "artifact_path": path,
                "requirement": "成果物本文の根拠確認",
                "method": P0C_METHOD, "result": "UNTESTABLE",
                "required": True, "line": int(entry.get("line") or -1),
                "quote": sent[:300],
                "reason": (
                    f"検査範囲外のため合格にしない(要確認): "
                    f"{entry.get('kind')}/{entry.get('value')}。位置=行{entry.get('line')}・"
                    f"{entry.get('position')}。{entry.get('reason') or ''}"),
                "expected": {"version": P0C_VERSION, "scope": "out_of_scope",
                             "value": str(entry.get("value") or "")},
            })
        by_sentence: dict[str, dict] = {}
        for entry in list(checked.get("unsupported_assertions") or []):
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("sentence") or "")
            if not key or key in by_sentence:
                continue
            by_sentence[key] = entry
        suggestions = {str(s.get("sentence") or ""): str(s.get("proposal") or "")
                       for s in (checked.get("suggestions") or [])
                       if isinstance(s, dict)}
        for sent, entry in sorted(by_sentence.items(),
                                  key=lambda kv: int((kv[1] or {}).get("position") or 0)):
            counter += 1
            proposal = suggestions.get(sent, "確定文にせず提案・要確認として記載する")
            rows.append({
                "item_id": f"{cid}-P0C-{counter:02d}",
                "criterion_id": cid, "artifact_path": path,
                "requirement": "成果物本文の根拠確認",
                "method": P0C_METHOD, "result": "UNTESTABLE",
                "required": True, "line": int(entry.get("line") or -1),
                "quote": sent[:300],
                "reason": (
                    f"原本外の断定の疑い(要確認): {entry.get('kind')}/"
                    f"{entry.get('value')}が出典にない（位置=行{entry.get('line')}・"
                    f"{entry.get('position')}）。原文: {sent[:80]}。"
                    f"修正候補: {proposal[:200]}"),
                "expected": {"version": P0C_VERSION,
                             "kind": str(entry.get("kind") or ""),
                             "value": str(entry.get("value") or ""),
                             "suggestion": proposal[:500]},
            })
    rows.sort(key=lambda r: (str(r.get("criterion_id") or ""),
                             str(r.get("item_id") or "")))
    renumbered: list[dict] = []
    counters: dict[str, int] = {}
    for row in rows:
        cid = str(row.get("criterion_id") or default_cid)
        counters[cid] = int(counters.get(cid, 0)) + 1
        fixed = dict(row)
        fixed["item_id"] = f"{cid}-P0C-{counters[cid]:02d}"
        fixed["criterion_id"] = cid
        renumbered.append(fixed)
    return renumbered


# P0-B: 失敗理由別の修正ループ。項目別検証の各未達(FAIL/UNTESTABLE)を規則で分類する。
# 推測で自動修復の対象にしないため、不明は system_error ではなく unclassified。
FAILURE_KINDS = (
    "artifact_missing",
    "wrong_target",
    "phrase_missing",
    "semantic_quote_mismatch",
    "unsupported_fact",
    "human_decision",
    "external_evidence",
    "system_error",
    "unclassified",
)

# 自動修復の実行対象。wrong_target は対象パスの確認自体が P0-A の検査対象であり、
# P0-B で検証契約を書き換えないため、人への提示に倒す(上限の「phrase_missing /
# wrong_target に限る」の範囲内で、さらに安全側に狭める)。
P0B_AUTO_REPAIRABLE_KINDS = ("phrase_missing",)

# 同じ失敗署名での自動修復の再試行上限。
P0B_MAX_REPAIR_ATTEMPTS = 2
# semantic_quote_mismatch の判定再試行は1回だけ。
P0B_SEMANTIC_REQUOTE_MAX = 1
# P0-B 全体のバックストップ(署名が変わり続けても無限に修復しない)。
P0B_MAX_REPAIR_ROUNDS = 3

_HUMAN_OPERATION_HINTS = {
    "artifact_missing": "欠けている成果物ファイルを確認し、生成元タスクを再実行するか、不足資料を登録して再計画してください",
    "wrong_target": "項目の対象パスが正しい成果物を指しているか確認し、取り違えなら計画の対象パスを修正して再計画してください",
    "phrase_missing": "欠けている必須語句を原本の値で補い、成果物を修正して再検証してください",
    "semantic_quote_mismatch": "引用と成果物の不一致箇所を確認し、文意の項目を人の確認フローで記録してください",
    "unsupported_fact": "原本にない断定を削除または要確認に書き換え、根拠がある場合だけ再検証してください",
    "human_decision": "人の確認・承認を行い、確認フローで結果を記録してください",
    "external_evidence": "必要な外部実行(送信・公開・面談など)を人間が行い、証拠を登録して再検証してください",
    "system_error": "台帳・読取の状態を確認し、復旧後に再検証してください",
    "unclassified": "失敗内容を確認し、原因を特定してから対応してください",
}


def failure_operator_hint(kind: str) -> str:
    """判断待ちのときに人が行う操作の文面。分類ごとに固定。"""
    return _HUMAN_OPERATION_HINTS.get(
        str(kind or ""),
        "失敗内容を確認し、原因を特定してから対応してください",
    )


def _has_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def classify_item_failure(row: dict | None) -> str | None:
    """未達の項目別検証行を失敗の種類に分類する。規則のみ。LLM出力は使わない。

    FAIL/UNTESTABLE だけを分類し、それ以外(PASS/HUMAN_CONFIRMED 等)は None。
    壊れた行(dict でない・必須キーが無い)は system_error(記録は残す)。
    既存の具体的な reason 文言を上書きせず、具体的なものから順に判定する。
    """
    if not isinstance(row, dict):
        return "system_error"
    result = str(row.get("result") or "")
    if result not in ("FAIL", "UNTESTABLE"):
        return None
    method = str(row.get("method") or "")
    reason = str(row.get("reason") or "")
    artifact_path = str(row.get("artifact_path") or "")

    # 1. 成果物ファイルが無い/読めない(対象不明を含む)。
    if (
        not artifact_path
        or "対象成果物不明" in reason
        or "読み取り失敗" in reason
        or "成果物点数またはファイルの不一致" in reason
    ):
        return "artifact_missing"

    # 2. 文意判定の引用が成果物に一致しない(実走 R14/R15 の文言)。
    if "引用が成果物と一致しない" in reason:
        return "semantic_quote_mismatch"

    # 3. 対象パスの取り違え(理由に明示がある場合)。
    if _has_any(reason, ("wrong_target", "取り違え", "対象パス")):
        return "wrong_target"

    # 4. 原本外の断定・矛盾(書いてはいけないことを書いた)。
    if _has_any(reason, ("原本外", "根拠のない断定", "unsupported", "矛盾", "禁止表現を検出",
                         "検査範囲外", "根拠確認")):
        return "unsupported_fact"

    # 5. 外部の証拠・実行が必要。
    if _has_any(reason, ("承認済み外部", "実行証拠", "外部実行", "外部の証拠", "公開URL")):
        return "external_evidence"

    # 6. 必須語句・見出し・内容の欠落(追記で直るもの)。
    if method in ("phrase", "headings", "section_char_count", "approval_stage_matrix"):
        if result == "FAIL":
            return "phrase_missing"
    if method == "llm_semantic" and "判断の根拠語句なし" in reason:
        return "phrase_missing"

    # 7. 台帳・読取などのシステム異常(理由が具体的で、人の確認待ち文言が無いもの)。
    if (
        _has_any(reason, ("台帳", "ledger", "OperationalError", "sqlite", "接続失敗", "データベース"))
        or not method
    ):
        return "system_error"

    # 8. 人の確認・承認が必要(引用不一致は上で拾い済み)。
    if _has_any(reason, ("人の確認待ち", "承認", "HUMAN", "確認が必要", "未実施", "要確認")):
        return "human_decision"
    if method in ("llm_semantic", "input_missing"):
        # 文意・入力不足の UNTESTABLE は、他に分類できなければ人への提示。
        return "human_decision"

    # 9. 契約の不備(検査方法・期待値の未定義)や数量不一致は、不明として扱う。
    return "unclassified"


def failure_signature(item_id: str, kind: str, artifact_hash: str, source_hash: str) -> str:
    """同じ失敗署名での再試行上限に使う署名。項目ID・種類・成果物・原本の組。"""
    return "|".join([
        str(item_id or ""),
        str(kind or ""),
        str(artifact_hash or "unknown"),
        str(source_hash or "unknown"),
    ])
