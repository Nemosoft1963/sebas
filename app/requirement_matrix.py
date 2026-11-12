"""一般的な要求項目の抽出(特定案件の語彙を持たない)。

決定的・ローカルのみ。達成条件の文章から個数要求・列挙語・鉤括弧語句・
否定要求・明記要求・見出しを一般規則で抽出する。抽出漏れは機械照合で
疑義化し、人の確認待ち(needs_review)に倒す。fail-closed: 不明は別扱い、
分母0で率を作らない。
"""
from __future__ import annotations

import contextvars
import re

METHODS = ("count", "artifact_count", "section_char_count", "approval_stage_matrix", "headings", "phrase", "llm_semantic", "input_missing", "artifact_grounding")

# 個数の単位を一般化。漢字の単位だけを見て、案件固有の名詞は持たない。
_COUNT_UNIT_RE = re.compile(r"(\d+)\s*(問|件|品|点|段階|項目|種類|個|通|冊|本)")

_QUOTE_RE_DQ = re.compile(r"「([^「」]{1,30})」")
_QUOTE_RE_SQ = re.compile(r"『([^『』]{1,30})』")

_PAREN_RE = re.compile(r"[（\(]([^（）\(\)]{2,80})[）\)]")

_NEGATION_RE = re.compile(
    r"(断定しない|断定せず|含めない|記載しない|載せない|書かない|しない|せず|禁止|避ける|控える|作らない)"
)

_MARK_WORDS = ("仮", "架空", "要確認", "想定")

_SPLIT_RE = re.compile(r"[、，,・/／]")
_CONJ_RE = re.compile(r"(および|及び|並びに|ならびに|或いは|あるいは)")

_ENUM_VERB_RE = re.compile(
    r"([\w一-鿿ぁ-んァ-ヶー]{1,12}(?:[、，,][\w一-鿿ぁ-んァ-ヶー]{1,12}){1,7}?)"
    r"\s*(?:を|について|につき|に関して|として)?\s*(?:[\w一-鿿ぁ-んァ-ヶー]{1,8}(?:に|く|と)\s*)?(?=記載|含め|載せ|明記|盛り込|掲載|説明)"
)
_BARE_COMMA_ENUM_RE = re.compile(
    r"(?<![（\(])([\w一-鿿ぁ-んァ-ヶー]{1,8}(?:[、，,][\w一-鿿ぁ-んァ-ヶー]{1,8}){2,7})"
)
_LEADING_PARTICLE_RE = re.compile(
    r"^[\w一-鿿ぁ-んァ-ヶー]{1,12}?(?:に|へ|で|から|より|の|は|が|を|と)(?=[\w一-鿿ぁ-んァ-ヶー]{1,12}$)"
)
_TRAILING_PARTICLE_RE = re.compile(
    r"(?:の|に|へ|で|から|より|は|が|を|と)[\w一-鿿ぁ-んァ-ヶー]{1,12}$"
)
_TRAILING_VERB_CONT_RE = re.compile(
    r"(?<=[一-鿿])(?:し|め|り|て|で|き|ぎ|び|み|い)$"
)
_CLAUSE_SPLIT_RE = re.compile(r"[。！？\n]+")
_SUBCLAUSE_BOUNDARY_RE = re.compile(
    r"(断定しない|断定せず|含めない|記載しない|載せない|書かない|しない|せず|禁止|避ける|控える|作らない|含め|記載し|載せ|明記し|作成し|整理し|定義し)\s*[、，,]?\s*"
)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _source_ref(source_ids: list[str]) -> str:
    for value in source_ids or []:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _item(
    criterion_id: str,
    index: int,
    artifact_path: str,
    requirement: str,
    method: str,
    required: bool,
    source_ref: str,
    expected: dict,
) -> dict:
    return {
        "item_id": f"{criterion_id}-R{index:02d}",
        "criterion_id": criterion_id,
        "artifact_path": artifact_path,
        "requirement": requirement,
        "method": method,
        "required": bool(required),
        "source_ref": source_ref,
        "expected": dict(expected or {}),
    }


def _item_id_number(item_id: str, criterion_id: str) -> int | None:
    """SC01-R02 形式の番号部分だけを取り出す。形式外は None。"""
    prefix = f"{criterion_id}-R"
    text = str(item_id or "")
    if not text.startswith(prefix):
        return None
    try:
        number = int(text[len(prefix):])
    except (TypeError, ValueError):
        return None
    return number if 1 <= number <= 9999 else None


def assert_unique_item_ids(items: list[dict], *, where: str = "requirement_matrix") -> None:
    """全項目を通して item_id の重複を検出したら ValueError にする。

    黙って上書き・握りつぶさない。決定的・順序安定。空・欠落は重複に数えない。
    """
    ids: list[str] = []
    for row in items or []:
        if not isinstance(row, dict):
            continue
        text = str(row.get("item_id") or "").strip()
        if text:
            ids.append(text)
    duplicates = sorted({value for value in set(ids) if ids.count(value) > 1})
    if duplicates:
        raise ValueError(
            f"duplicate item_id in {where}: {', '.join(duplicates[:5])}")


def ensure_unique_item_ids(criterion_id: str, items: list[dict]) -> list[dict]:
    """全項目を通して item_id を一意にする単一の出口。

    方針: 既に一意なIDは変えない(安定性)。重複は後から来た側だけを
    その criterion の次の空き番号(R02形式)に付け替える。決定的
    (同じ入力なら毎回同じID)。採番後の並べ替えは行わない。
    """
    rows = [dict(row) if isinstance(row, dict) else row for row in (items or [])]
    taken: set[str] = set()
    for row in rows:
        if isinstance(row, dict):
            text = str(row.get("item_id") or "").strip()
            if text:
                taken.add(text)
    seen: set[str] = set()
    used_numbers: dict[str, set[int]] = {}
    for text in taken:
        for row in rows:
            if not isinstance(row, dict):
                continue
            cid = str(row.get("criterion_id") or criterion_id or "").strip() or str(criterion_id or "")
            if text == str(row.get("item_id") or "").strip():
                number = _item_id_number(text, cid)
                if number is not None:
                    used_numbers.setdefault(cid, set()).add(number)
                break
    fixed: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            fixed.append(row)
            continue
        text = str(row.get("item_id") or "").strip()
        if not text or text not in seen:
            seen.add(text)
            fixed.append(row)
            continue
        cid = str(row.get("criterion_id") or criterion_id or "").strip() or str(criterion_id or "")
        used = used_numbers.setdefault(cid, set())
        candidate = ""
        for number in range(1, 9999):
            name = f"{cid}-R{number:02d}" if number < 100 else f"{cid}-R{number}"
            if name not in taken and name not in seen:
                candidate = name
                used.add(number)
                break
        if not candidate:
            raise ValueError(f"cannot uniquify item_id: {text}")
        new_row = dict(row)
        new_row["item_id"] = candidate
        seen.add(candidate)
        taken.add(candidate)
        fixed.append(new_row)
    return fixed


def _md_outputs(outputs: list[dict]) -> list[dict]:
    """項目化の対象はMarkdown成果物のみ。CSVは形式検査(列・行)で検証する。"""
    cleaned = []
    for output in outputs or []:
        if not isinstance(output, dict) or not output.get("path"):
            continue
        if str(output.get("path") or "").lower().endswith(".md"):
            cleaned.append(output)
    return cleaned


def _first_output(outputs: list[dict]) -> str:
    for output in _md_outputs(outputs):
        return str(output["path"])
    return ""


def _markers_for_unit(unit: str) -> list[str]:
    unit = str(unit or "")
    if unit in ("問", "件", "項目"):
        return ["問", "件", "項目", "Q", "q", "?", "？"]
    if unit in ("品", "点", "個", "種類", "通", "冊", "本"):
        return ["品", "点", "個", "種類", "通", "冊", "本"]
    if unit == "段階":
        return ["段階", "ステップ", "STEP", "Step", "step"]
    return [unit]


def _clean_term(term: str) -> str:
    text = str(term or "").strip()
    text = re.sub(r"^[「『\"'\(（\[]+", "", text).strip()
    text = re.sub(r"[」』\"'\)）\]]+$", "", text).strip()
    text = _CONJ_RE.sub("", text).strip()
    text = re.sub(r"^(?:および|及び|並びに|ならびに|及び|と|や)+", "", text).strip()
    text = re.sub(r"(?:および|及び|並びに|ならびに|及び|と|や)+$", "", text).strip()
    return text.strip()


def _split_enumeration(fragment: str) -> list[str]:
    text = str(fragment or "")
    # 「A、B、およびC」の「、および」を区切りに変える。
    text = re.sub(r"[、，,]\s*(?:および|及び|並びに|ならびに)\s*", "、", text)
    text = _CONJ_RE.sub("、", text)
    parts = _SPLIT_RE.split(text)
    terms: list[str] = []
    for part in parts:
        term = _clean_term(part)
        if not term or len(term) > 20:
            continue
        if re.fullmatch(r"\d+", term):
            continue
        if len(term) < 1:
            continue
        # 助詞だけ・記号だけは採らない。
        if re.fullmatch(r"[とやのをにへでがはも、。・/／]+", term):
            continue
        terms.append(term)
    # 順序を保って重複を除く。
    seen: set[str] = set()
    ordered: list[str] = []
    for term in terms:
        if term not in seen:
            seen.add(term)
            ordered.append(term)
    return ordered


def _strip_leading_qualifier(term: str) -> str:
    """『報告書に月次の売上』のような先頭要素から助詞付きの修飾語を剥がす。"""
    text = str(term or "").strip()
    while True:
        match = _LEADING_PARTICLE_RE.match(text)
        if not match:
            break
        text = text[match.end():].strip()
    return text


def _extract_enumeration_terms(text: str) -> list[str]:
    found: list[str] = []
    # 括弧内の列挙を優先する。「(A、B、C)」の形。
    for match in _PAREN_RE.finditer(text):
        inner = match.group(1)
        if not re.search(r"[、，,・/／]", inner) and not _CONJ_RE.search(inner):
            continue
        for term in _split_enumeration(inner):
            if term not in found:
                found.append(term)
    # 「A、B、およびC」のように接続詞を伴う列挙。「A・B・C」「A/B/C」も採る。
    for match in re.finditer(
        r"([\w一-鿿ぁ-んァ-ヶー]{1,12}(?:[、，,・/／][\w一-鿿ぁ-んァ-ヶー]{1,12}){0,4}"
        r"[、，,]\s*(?:および|及び|並びに|ならびに|或いは|あるいは)\s*"
        r"[\w一-鿿ぁ-んァ-ヶー]{1,12})",
        text,
    ):
        for index, term in enumerate(_split_enumeration(match.group(1))):
            if index == 0:
                term = _strip_leading_qualifier(term)
            if term and term not in found:
                found.append(term)
    # 読点の列挙の直後に要求の動詞(記載・含め・載せ・明記・盛り込・掲載・説明)が来る形。
    for match in re.finditer(
        r"([^\n。！？（\(\)）]{2,80}?)\s*(?:を|について|につき|に関して|として)\s*(?:記載|含め|載せ|明記|盛り込|掲載|説明)",
        text,
    ):
        fragment = match.group(1)
        if not re.search(r"[、，,]", fragment):
            continue
        raw_terms = _split_enumeration(fragment)
        cleaned_terms: list[str] = []
        for index, term in enumerate(raw_terms):
            if index == 0:
                term = _strip_leading_qualifier(term)
            if not term or len(term) > 12 or _COUNT_UNIT_RE.search(term) or _NEGATION_RE.search(term):
                continue
            if re.search(r"(?:作成|整理|定義|確認|検証|報告|実施|実行|記載|含め|載せ|明記|盛り込|掲載|説明)", term):
                continue
            if _TRAILING_VERB_CONT_RE.search(term):
                continue
            cleaned_terms.append(term)
        if len(cleaned_terms) >= 2:
            for term in cleaned_terms:
                if term not in found:
                    found.append(term)
    # 「・」「/」で区切られた列挙(裸の読点だけの文は採らない)。
    for match in re.finditer(
        r"([\w一-鿿ぁ-んァ-ヶー]{1,12}(?:[・/／][\w一-鿿ぁ-んァ-ヶー]{1,12}){1,5})",
        text,
    ):
        fragment = match.group(1)
        if _COUNT_UNIT_RE.search(fragment):
            continue
        for term in _split_enumeration(fragment):
            if term not in found:
                found.append(term)
    # 「次の項目: A/B/C」のような指示の後ろの列挙。
    for match in re.finditer(r"[::：]\s*([^\n。]{2,80})", text):
        tail = match.group(1)
        if not re.search(r"[、，,・/／]", tail) and "/" not in tail:
            continue
        for term in _split_enumeration(tail):
            if term not in found:
                found.append(term)
    return found[:12]


def _extract_unanchored_comma_enumerations(text: str) -> list[str]:
    """要求の動詞が無い読点の連なり(項目にできない列挙らしき表現)を抽出する。"""
    unanchored: list[str] = []
    # 括弧内は _extract_enumeration_terms で処理済みなので除外する。
    masked = _PAREN_RE.sub(" ", text)
    # 要求動詞付きの読点列挙・接続詞付きの列挙も除外する。
    masked = re.sub(
        r"[^\n。！？（\(\)）]{2,80}?\s*(?:を|について|につき|に関して|として)\s*(?:記載|含め|載せ|明記|盛り込|掲載|説明)",
        " ",
        masked,
    )
    masked = re.sub(
        r"[\w一-鿿ぁ-んァ-ヶー]{1,12}(?:[、，,・/／][\w一-鿿ぁ-んァ-ヶー]{1,12}){0,4}"
        r"[、，,]\s*(?:および|及び|並びに|ならびに|或いは|あるいは)\s*"
        r"[\w一-鿿ぁ-んァ-ヶー]{1,12}",
        " ",
        masked,
    )
    for match in _BARE_COMMA_ENUM_RE.finditer(masked):
        fragment = match.group(1)
        # 直後に「報告」「整理」「確認」「検証」「定義」「作成」等の非列挙動詞が来る場合は対象外にするか判定:
        # ただし「売上、費用、利益の推移に関して社内文書を作成する」のように名詞列挙が埋め込まれている場合は疑義にする。
        after = masked[match.end():match.end() + 20]
        if re.match(r"^\s*(?:を|について|につき|に関して)?\s*(?:[\w一-鿿ぁ-んァ-ヶー]{1,6}(?:に|く|と)\s*)?(?:報告|検証|確認|整理|定義|評価|判断|実施|実行)", after):
            continue
        parts = _split_enumeration(fragment)
        if len(parts) < 3:
            continue
        cleaned_parts: list[str] = []
        for idx, part in enumerate(parts):
            if idx == 0:
                part = _strip_leading_qualifier(part)
            if idx == len(parts) - 1:
                part = _TRAILING_PARTICLE_RE.sub("", part).strip()
            if not part or len(part) > 8:
                continue
            if _COUNT_UNIT_RE.search(part) or _NEGATION_RE.search(part):
                continue
            if re.search(r"(?:作成|整理|定義|確認|検証|報告|実施|実行|記載|含め|載せ|明記|盛り込|掲載|説明)", part):
                continue
            if _TRAILING_VERB_CONT_RE.search(part):
                continue
            cleaned_parts.append(part)
        if len(cleaned_parts) >= 3:
            joined = "、".join(cleaned_parts)
            if joined not in unanchored:
                unanchored.append(joined)
    return unanchored


def _split_negation_clauses(text: str) -> list[str]:
    """文を否定節・肯定節の単位に分割する。"""
    clauses: list[str] = []
    for sentence in _CLAUSE_SPLIT_RE.split(str(text or "")):
        sentence = sentence.strip()
        if not sentence:
            continue
        start = 0
        for match in _SUBCLAUSE_BOUNDARY_RE.finditer(sentence):
            end = match.start(1) + len(match.group(1))
            sub = sentence[start:end].strip(" 、，,。")
            if sub:
                clauses.append(sub)
            start = match.end()
        tail = sentence[start:].strip(" 、，,。")
        if tail:
            clauses.append(tail)
    return clauses


def _classify_quotes_and_negations(text: str) -> tuple[list[str], list[str], list[str], list[str]]:
    """(required_quotes, forbid_quotes, unquoted_negation_clauses, contradictions) を返す。"""
    required_quotes: list[str] = []
    forbid_quotes: list[str] = []
    unquoted_negations: list[str] = []
    clauses = _split_negation_clauses(text)
    for clause in clauses:
        has_neg = bool(_NEGATION_RE.search(clause))
        clause_quotes = _extract_quotes(clause)
        if has_neg:
            if clause_quotes:
                for q in clause_quotes:
                    # 鉤括弧の直後に肯定の明記動詞が来る場合(「要確認」と明記する 等)は必須側。
                    after_quote = re.search(
                        re.escape(q) + r"[」』]\s*(?:と|を|として)?\s*(?:明記|記載|含め|載せ|盛り込|掲載|説明)",
                        clause,
                    )
                    if after_quote:
                        if q not in required_quotes:
                            required_quotes.append(q)
                    else:
                        if q not in forbid_quotes:
                            forbid_quotes.append(q)
            else:
                if clause not in unquoted_negations:
                    unquoted_negations.append(clause)
        else:
            for q in clause_quotes:
                if q not in required_quotes:
                    required_quotes.append(q)
    # 全体の鉤括弧で万一漏れたものを拾う。
    for q in _extract_quotes(text):
        if q not in required_quotes and q not in forbid_quotes:
            required_quotes.append(q)
    # 必須かつ禁止の両方に入る語は矛盾として分離する。
    contradictions = [q for q in required_quotes if q in forbid_quotes]
    if contradictions:
        required_quotes = [q for q in required_quotes if q not in contradictions]
        forbid_quotes = [q for q in forbid_quotes if q not in contradictions]
    return required_quotes, forbid_quotes, unquoted_negations, contradictions


def _extract_quotes(text: str) -> list[str]:
    found: list[str] = []
    for pattern in (_QUOTE_RE_DQ, _QUOTE_RE_SQ):
        for match in pattern.finditer(text):
            term = str(match.group(1) or "").strip()
            if not term or len(term) > 30:
                continue
            if term not in found:
                found.append(term)
    return found


# P0-A: 検証契約の正規化(要求文と検査対象の分離・対象パスの照合・無意味語句の拒否)。
# 特定案件の語彙を製品コードに入れない。汎用的な役割語(案内文・チェックリスト)と
# 汎用概念(日時・日付・時刻)だけを使う。推測で契約を作らず、具体値が原本に無ければ
# input_missing として止める(判定不能。合格にも不合格にも倒さない)。
P0A_CONTRACT_VERSION = "p0a-verify-contract-v1"
P0A_TEMPORAL_CONCEPTS = ("日時", "日付", "時刻")

_P0A_DATE_RE = re.compile(r"\d{4}年\d{1,2}月\d{1,2}日|\d{1,2}月\d{1,2}日")
_P0A_TIME_RE = re.compile(r"\d{1,2}:\d{2}|\d{1,2}時(?:\d{1,2}分)?")

# 要求文自体を必須語句にしたとみなす目安。短い具体値(日時・固有名)を誤検出しない
# よう、長さと達成条件文中の出現と要求の言い回しの3点をすべて要求する。抽出単位が
# 長い句(「終了後確認を含める」など)のときは、末尾の要求動詞を除いた核が短い
# 具体語(「終了後確認」など)なら要求文扱いにしない。
_P0A_REQUIREMENT_PHRASE_MARKERS = (
    "には", "とは", "を含", "について", "として", "を確認", "を作成",
    "を記載", "を載せ", "を明記", "を掲載", "を説明",
)
_P0A_REQUIREMENT_TRAIL_RE = re.compile(
    r"(を含める|を含め|を含む|について|につき|に関して|として|を確認(?:する|し)?|を作成(?:する|し)?|を記載(?:する|し)?|を載せ(?:る|し)?|を明記(?:する|し)?|を掲載(?:する|し)?|を説明(?:する|し)?|を盛り込(?:む|み)?|には|とは)$"
)

# 抽出の残りかす(役割語・係助詞の付着)を取り除く narrow な規則。該当しない語は変えない。
_P0A_RESIDUE_PREFIX_RE = re.compile(
    r"^(?:.*?)(?:には|とは|への|からの|よりの|での|のに|に|へ|で|は|が|を|と)\s*"
)
_P0A_ROLE_WORDS = ("案内文", "チェックリスト")


def is_p0a_contact_values_resolved(values: object, registered_text: str | None = None) -> bool:
    """連絡先 phrase 項目の照合対象が実値かどうか。実値なら True、空なら False。"""
    cleaned = [str(value or "").strip() for value in values if str(value or "").strip()]
    return bool(cleaned)


def is_p0a_requirement_sentence_keyword(keyword: str, criterion: str) -> bool:
    """要求節そのものを必須語句にした場合だけ拒否する。短い核語や通常の述語句は許可する。"""
    text = _norm(keyword).strip("。！？")
    criterion_text = _norm(criterion)
    if len(text) < 6 or text not in criterion_text:
        return False
    # 通常抽出器が作る「名詞句＋作成する」等を、長いという理由だけで拒否しない。
    # 拒否対象は、句点・改行単位の要求節をほぼ丸ごと語句化したものに限定する。
    clauses = [
        _norm(part).strip("。！？")
        for part in re.split(r"[。！？\n]+", str(criterion or ""))
        if _norm(part).strip("。！？")
    ]
    if any(text == clause and len(text) >= 12 for clause in clauses):
        return True
    # P0-A の誤りである「成果物役割には概念/内容」の形は節の一部でも拒否する。
    # 「終了後確認を含める」は核語へ正規化できるため、ここでは拒否しない。
    if re.search(r"(?:には|とは)", text) and any(
        marker in text for marker in ("日時", "日付", "時刻", "準備", "終了後")
    ):
        return True
    core = _P0A_REQUIREMENT_TRAIL_RE.sub("", text).strip()
    return not core and any(marker in text for marker in _P0A_REQUIREMENT_PHRASE_MARKERS)


def _p0a_concept_of(term: str) -> str:
    text = str(term or "").strip()
    for concept in P0A_TEMPORAL_CONCEPTS:
        if text == concept or text.endswith(concept):
            return concept
    return ""


def _p0a_core_term(term: str) -> str:
    """役割語・係助詞の付着だけを剥がす。該当しない語はそのまま返す。"""
    text = str(term or "").strip()
    if not text:
        return text
    if (any(word in text for word in _P0A_ROLE_WORDS)
            or re.match(r"^(は|が|を|に|へ|で|と|や)", text)):
        core = _P0A_RESIDUE_PREFIX_RE.sub("", text).strip()
        if core and core != text and len(core) <= 12:
            return core
    return text


def _p0a_registered_datetime_values(registered_text: str) -> list[str]:
    blob = str(registered_text or "")
    found: list[str] = []
    for pattern in (_P0A_DATE_RE, _P0A_TIME_RE):
        for match in pattern.finditer(blob):
            raw = match.group(0)
            if raw not in found:
                found.append(raw)
    return sorted(found)


def _p0a_clauses_with(criterion: str, term: str) -> list[str]:
    clauses = [part.strip() for part in re.split(r"[。\n]", str(criterion or "")) if part.strip()]
    hits = [part for part in clauses if term and term in part]
    if hits:
        return hits
    core = _p0a_core_term(term)
    if core != term:
        hits = [part for part in clauses if core and core in part]
        if hits:
            return hits
    return [str(criterion or "")]


def _p0a_role_expected_path(term: str, criterion: str, outputs: list[dict]) -> str | None:
    """汎用的な役割(案内文/document・チェックリスト/checklist)で対象パスを決める。

    案件固有の語彙は使わない。役割が特定できない(一意でない・手がかり無し)ときは
    None を返し、既存の割当てを変えない。
    """
    md = _md_outputs(outputs)
    if len(md) < 2:
        return None
    checklists = sorted(str(o.get("path") or "") for o in md
                        if str(o.get("artifact_category") or "") == "checklist"
                        or "_checklist" in str(o.get("path") or ""))
    documents = sorted(str(o.get("path") or "") for o in md
                    if str(o.get("path") or "") not in checklists)
    if not checklists or not documents:
        return None
    scope = "\n".join(_p0a_clauses_with(criterion, term))
    mentions_check = "チェックリスト" in scope
    mentions_doc = "案内文" in scope
    if mentions_check and not mentions_doc:
        return checklists[0]
    if mentions_doc and not mentions_check:
        return documents[0]
    return None


# P1-1: 日時の役割と節の対応。準備開始は「準備」を含む節が名指しする成果物に属する。
# 節が複数の成果物に触れる・名指ししない場合は推測せず、汎用の役割対応で決める。
# (チェックリスト=準備・終了後確認、案内文=開催日・開始・終了・会場・対象・持ち物)。
# 「終了後確認」の中の「終了」は終了時刻の手がかりにしない(別項目への誤一致防止)。
_P1_ROLE_CLAUSE_RES = {
    "prep_time": re.compile(r"準備開始時刻|準備開始|準備|受付開始|開場|集合"),
    "start_time": re.compile(r"開始時刻|開始|開演|スタート"),
    "end_time": re.compile(r"終了時刻|終了(?!後)|閉会"),
    "event_date": re.compile(r"開催日|開催|日時|日付"),
}
_P1_ROLE_DEFAULT_IS_CHECKLIST = {"prep_time"}


def _p1_role_clause_target(role: str, criterion: str, outputs: list[dict]) -> str | None:
    """その役割の節が名指しする成果物だけを返す。一意でなければ None(推測しない)。"""
    pattern = _P1_ROLE_CLAUSE_RES.get(str(role) or "")
    if pattern is None:
        return None
    md = _md_outputs(outputs)
    if len(md) < 2:
        return None
    checklists = sorted(str(o.get("path") or "") for o in md
                        if str(o.get("artifact_category") or "") == "checklist"
                        or "_checklist" in str(o.get("path") or ""))
    documents = sorted(str(o.get("path") or "") for o in md
                    if str(o.get("path") or "") not in checklists)
    if not checklists or not documents:
        return None
    for clause in [part.strip() for part in re.split(r"[。\n]", str(criterion or "")) if part.strip()]:
        if not pattern.search(clause):
            continue
        mentions_check = "チェックリスト" in clause
        mentions_doc = "案内文" in clause
        if mentions_check and not mentions_doc:
            return checklists[0]
        if mentions_doc and not mentions_check:
            return documents[0]
        return None
    return None


def _p1_role_default_path(role: str, outputs: list[dict]) -> str | None:
    """節が名指ししないときの汎用の役割対応。決められなければ None。"""
    md = _md_outputs(outputs)
    if len(md) < 2:
        return None
    checklists = sorted(str(o.get("path") or "") for o in md
                        if str(o.get("artifact_category") or "") == "checklist"
                        or "_checklist" in str(o.get("path") or ""))
    documents = sorted(str(o.get("path") or "") for o in md
                    if str(o.get("path") or "") not in checklists)
    if not checklists or not documents:
        return None
    if str(role) in _P1_ROLE_DEFAULT_IS_CHECKLIST:
        return checklists[0]
    if str(role) in ("event_date", "start_time", "end_time"):
        return documents[0]
    return None
# 役割の違う値の取り違えを防ぐため、原本値に役割ラベルが決定的に伴う場合だけ
# 役割を付け、推測できない値は役割不明のまま汎用項目にする(推測で決めない)。
# 各項目は許容表記(15:00/15時/午後3時、日付の2026年11月14日/11/14等)を variants
# に明示し、検証側は正規化(分・年月日)で照合する。契約の種別 match: any|all を
# expected に明示して保存し、検証がそれを守る。特定案件の語彙は持たない。
P1_DATETIME_ROLES = (
    ("event_date", "開催日", ("開催日", "開催", "日付"), "date"),
    ("start_time", "開始時刻", ("開始", "開演", "スタート"), "time"),
    ("end_time", "終了時刻", ("終了", "閉会"), "time"),
    ("prep_time", "準備開始時刻", ("準備開始", "準備", "集合", "受付開始", "開場"), "time"),
)
P1_DATETIME_ROLE_LABELS = {role: label for role, label, _aliases, _kind in P1_DATETIME_ROLES}

_P1_RANGE_RE = re.compile(
    r"(\d{1,2}:\d{2}|(?:午前|午後)?\d{1,2}時(?:\d{1,2}分)?|正午)\s*(?:〜|～|~|-|―|から)\s*"
    r"(\d{1,2}:\d{2}|(?:午前|午後)?\d{1,2}時(?:\d{1,2}分)?|正午)")
_P1_START_LABEL_RE = re.compile(r"開始時刻|開始|開演|スタート")
_P1_END_LABEL_RE = re.compile(r"終了時刻|終了|閉会")
_P1_PREP_LABEL_RE = re.compile(r"準備開始時刻|準備開始|準備|集合|受付開始|開場")
# 役割ラベルの最長一致(「準備開始時刻」は開始時刻に含めない)。
_P1_ROLE_LABEL_RES = (
    ("prep_time", re.compile(r"準備開始時刻|準備開始|受付開始|開場|集合|準備")),
    ("start_time", re.compile(r"開始時刻|開始|開演|スタート")),
    ("end_time", re.compile(r"終了時刻|終了|閉会")),
)
_P1_DATE_LABEL_RE = re.compile(r"開催日|開催|日付|日時")
_P1_ANY_MARK_RE = re.compile(r"いずれか|どれか|または|もしくは|あるいは")

_P1_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_P1_PHONE_RE = re.compile(r"0\d{1,4}[-\s　]*\d{1,4}[-\s　]*\d{3,4}")
_P0A_URL_RE = re.compile(r"https?://[^\s、。）)\"'<>]+")

# A1: 問い合わせ先の実値を取得する単一のインターフェース。A3で登録台帳に
# 差し替える。呼び出し側は直接抽出せず、この関数だけを使う。
# A3: 登録台帳への接続・用途の区別・契約への束縛・入力待ち・any照合。
# 優先順位: (1)登録台帳の現在の値(用途に使用可能)、(2)台帳が空のときだけ
# 登録コンテキストの値へフォールバック。読取失敗は送出してフォールバックしない。
_CONTACT_REGISTRY_PROVIDER = None
_CONTACT_DEFAULT_PURPOSE = "public"
_CONTACT_REQUEST_CONTEXT = contextvars.ContextVar(
    "contact_request_context",
    default={"project_id": "", "purpose": None, "selected_kinds": None},
)


def set_contact_registry_provider(provider) -> None:
    """A3用の差し替え口。provider(project_id) -> list[dict]。Noneで既定に戻す。"""
    global _CONTACT_REGISTRY_PROVIDER
    _CONTACT_REGISTRY_PROVIDER = provider


def set_contact_default_purpose(purpose: str | None = None) -> None:
    """検証用途の既定。不明は public(厳しい側)に倒す。"""
    global _CONTACT_DEFAULT_PURPOSE
    text = str(purpose or "").strip()
    _CONTACT_DEFAULT_PURPOSE = text if text in ("internal_demo", "public") else "public"


def set_contact_request_context(project_id=None, purpose=None,
                                selected_kinds=None):
    """要求単位の文脈を設定し、呼出側が復元できる ContextVar token を返す。"""
    text = str(purpose or "").strip() if purpose is not None else ""
    effective_purpose = text if text in ("internal_demo", "public") else None
    if selected_kinds is None:
        cleaned = None
    else:
        try:
            kinds = [str(k).strip() for k in list(selected_kinds)]
        except TypeError:
            kinds = []
        cleaned = [k for k in kinds if k in ("email", "phone", "form")]
    return _CONTACT_REQUEST_CONTEXT.set({
        "project_id": str(project_id or ""),
        "purpose": effective_purpose,
        "selected_kinds": cleaned,
    })


def reset_contact_request_context(token) -> None:
    """set_contact_request_context が返した token で以前の文脈へ戻す。"""
    _CONTACT_REQUEST_CONTEXT.reset(token)


def configure_contact_registry(memory_path=None, default_purpose=None) -> None:
    """本番配線用の最小設定。台帳の直読先と用途の既定を保持する。"""
    global _CONTACT_REGISTRY_PROVIDER
    if default_purpose is not None:
        set_contact_default_purpose(default_purpose)
    if memory_path is None:
        return
    try:
        from app.contact_registry import registry_contact_provider as _provider
    except (ImportError, AttributeError, ValueError):
        return
    path = memory_path

    def _bound(project_id: str = ""):
        return _provider(path, project_id)

    _CONTACT_REGISTRY_PROVIDER = _bound


def _contact_effective_purpose(purpose=None) -> str:
    context = _CONTACT_REQUEST_CONTEXT.get()
    for candidate in (purpose, context.get("purpose"), _CONTACT_DEFAULT_PURPOSE):
        text = str(candidate or "").strip()
        if text in ("internal_demo", "public"):
            return text
    return "public"


def _contact_request_project_id(explicit: str = "") -> str:
    text = str(explicit or "").strip()
    if text:
        return text
    return str(_CONTACT_REQUEST_CONTEXT.get().get("project_id") or "")


def _contact_request_selected(explicit=None) -> list[str] | None:
    if explicit is not None:
        try:
            kinds = [str(k).strip() for k in list(explicit)]
        except TypeError:
            return None
        return [k for k in kinds if k in ("email", "phone", "form")]
    selected = _CONTACT_REQUEST_CONTEXT.get().get("selected_kinds")
    if selected is None:
        return None
    return list(selected)


def _contact_value_sha256(value: str) -> str:
    import hashlib as _hashlib

    return _hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _contact_policy_kinds(criterion_text: str) -> set[str]:
    """方針文からの最小の経路推定(明示選択が無いときの入力待ち判定用)。"""
    text = str(criterion_text or "")
    kinds: set[str] = set()
    if re.search(r"フォーム|form|URL|ウェブサイト|サイト", text, re.I):
        kinds.add("form")
    if re.search(r"メール|mail|@", text, re.I):
        kinds.add("email")
    if re.search(r"電話|TEL|phone|ファックス|FAX", text, re.I):
        kinds.add("phone")
    return kinds


def _contact_read_registry(project_id: str):
    """台帳の現在値を読む。(state, raw)。stateは ok/unregistered。

    読取失敗(RegistryReadError)は送出する(未登録に倒さない)。
    提供関数が未設定のときは unregistered。
    """
    provider = _CONTACT_REGISTRY_PROVIDER
    if provider is None:
        return "unregistered", []
    try:
        from app.contact_registry import RegistryReadError as _ReadError
    except (ImportError, AttributeError, ValueError):
        _ReadError = None  # type: ignore[assignment]
    try:
        provided = provider(project_id)
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        if _ReadError is not None and isinstance(exc, _ReadError):
            raise
        try:
            from app.contact_registry import RegistryReadError as _RegistryReadError
        except (ImportError, AttributeError, ValueError) as import_exc:
            raise RuntimeError(
                f"contact registry unreadable: {type(exc).__name__}; "
                f"error type unavailable: {type(import_exc).__name__}") from exc
        raise _RegistryReadError(
            "row_unreadable", sqlite_error=type(exc).__name__,
            detail=str(exc)[:500]) from exc
    except Exception as exc:
        try:
            from app.contact_registry import RegistryReadError as _RegistryReadError
        except (ImportError, AttributeError, ValueError) as import_exc:
            raise RuntimeError(
                f"contact registry unreadable: {type(exc).__name__}; "
                f"error type unavailable: {type(import_exc).__name__}") from exc
        raise _RegistryReadError(
            "row_unreadable", sqlite_error=type(exc).__name__,
            detail=str(exc)[:500]) from exc
    raw = [entry for entry in (provided or []) if isinstance(entry, dict)]
    if not raw:
        return "unregistered", []
    return "ok", raw


def _contact_usable_entries(project_id: str, purpose=None):
    """用途に使用可能な台帳値。(state, usable, effective_purpose)。

    stateは ok/unregistered。読取失敗は送出する。fictional/registeredは
    usable_for で用途別に選別する(公開では使えないものは含めない)。
    """
    effective = _contact_effective_purpose(purpose)
    state, raw = _contact_read_registry(project_id)
    if state != "ok":
        return state, [], effective
    try:
        from app.contact_registry import (
            RegistryReadError as _RegistryReadError,
            usable_for as _usable,
        )
    except (ImportError, AttributeError, ValueError) as exc:
        try:
            from app.contact_registry import RegistryReadError as _RegistryReadError
        except (ImportError, AttributeError, ValueError) as import_exc:
            raise RuntimeError(
                f"contact usage policy unreadable: {type(exc).__name__}; "
                f"error type unavailable: {type(import_exc).__name__}") from exc
        raise _RegistryReadError(
            "row_unreadable", sqlite_error=type(exc).__name__,
            detail="usable_for import failed") from exc
    usable: list[dict] = []
    rejected: list[str] = []
    for entry in raw:
        check = {"source_kind": str(entry.get("source_kind") or ""),
                 "expires_at": entry.get("expires_at")}
        try:
            decision = _usable(check, effective)
        except (ValueError, TypeError, AttributeError, RuntimeError, OSError) as exc:
            rejected.append(
                f"{entry.get('route_id') or entry.get('kind') or 'unknown'}:"
                f"{type(exc).__name__}")
            continue
        if isinstance(decision, dict) and decision.get("usable"):
            usable.append(entry)
    for entry in usable:
        if rejected:
            entry = entry
            entry["usage_policy_rejections"] = list(rejected)
    return state, usable, effective


def resolve_contact_values(project_id: str = "", registered_text: str | None = None,
                             purpose: str | None = None,
                             selected_kinds: list[str] | None = None) -> list[dict]:
    """登録された連絡先の実値だけを返す。推測・発明しない。

    各要素は {kind: email|phone|form, value, source_kind, source_ref,
    contact_route_id, version, value_sha256, purpose}。
    優先順位: (1)登録台帳の現在の値(用途に使用可能)、(2)台帳が空のときだけ
    登録コンテキストの値へフォールバック。読取失敗は送出する(フォールバックせず)。
    明示選択があるときはその経路だけに絞る。台帳由来でない値は
    source_kind=registered_context のまま残す。
    実値が1つも無ければ空列(呼び出し側は input_missing にする)。
    """
    effective_pid = _contact_request_project_id(project_id)
    selected = _contact_request_selected(selected_kinds)
    if _CONTACT_REGISTRY_PROVIDER is not None and effective_pid:
        state, usable, effective = _contact_usable_entries(effective_pid, purpose)
        if state == "ok" and usable:
            use = list(usable)
            if selected:
                narrowed = [entry for entry in use
                            if str(entry.get("kind") or "") in selected]
                # 明示選択があるときはその経路だけ使う。勝手に別経路を使わない。
                use = narrowed
            cleaned: list[dict] = []
            for entry in use:
                value = str(entry.get("value") or "").strip()
                kind = str(entry.get("kind") or "").strip() or "form"
                if not value or kind not in ("email", "phone", "form"):
                    continue
                cleaned.append({
                    "kind": kind,
                    "value": value,
                    "source_kind": str(entry.get("source_kind") or "registered"),
                    "source_ref": str(entry.get("source_ref") or ""),
                    "contact_route_id": str(entry.get("route_id") or ""),
                    "version": entry.get("version"),
                    "value_sha256": _contact_value_sha256(value),
                    "purpose": effective,
                })
            seen: set[tuple[str, str]] = set()
            ordered: list[dict] = []
            for entry in cleaned:
                key = (entry["kind"], entry["value"])
                if key not in seen:
                    seen.add(key)
                    ordered.append(entry)
            use_sorted = sorted(ordered, key=lambda entry: (entry["kind"], entry["value"]))
            # 明示選択ありで使える値が無いときは空列(URLを発明せず input_missing)。
            # 明示選択なしで使える値が無いとき(用途不適合だけ)も空列。
            # 台帳が空のときだけ下の登録コンテキストへフォールバックする。
            # 用途不適合(fictional の public 等)は空列のまま理由は呼出側が付ける。
            return use_sorted
        if state == "ok":
            # 台帳に現在値はあるが用途に使用可能な値が無い。
            return []
    blob = str(registered_text or "")
    if not blob.strip():
        return []
    found: list[dict] = []
    for match in _P1_EMAIL_RE.finditer(blob):
        value = match.group(0)
        if value not in [entry["value"] for entry in found]:
            found.append({"kind": "email", "value": value,
                          "source_kind": "registered_context",
                          "source_ref": ""})
    for match in _P1_PHONE_RE.finditer(blob):
        value = re.sub(r"[\s　]+", "", match.group(0))
        if value not in [entry["value"] for entry in found]:
            found.append({"kind": "phone", "value": value,
                          "source_kind": "registered_context",
                          "source_ref": ""})
    for match in _P0A_URL_RE.finditer(blob):
        value = match.group(0).rstrip("。、")
        if value and value not in [entry["value"] for entry in found]:
            found.append({"kind": "form", "value": value,
                          "source_kind": "registered_context",
                          "source_ref": ""})
    return sorted(found, key=lambda entry: (entry["kind"], entry["value"]))
_P1_SINGLE_INCLUDE_RE = re.compile(
    r"([一-鿿ぁ-んァ-ヶーA-Za-z0-9]{2,12}?)"
    r"(?:を|について|につき|に関して|として)?\s*"
    r"(?:を含め|を含める|を記載し|を載せ|を明記し|を掲載し|を盛り込み|を説明し)"
    r"|([一-鿿ぁ-んァ-ヶーA-Za-z0-9]{2,12})\s*(?:を|について|につき|に関して|として)?\s*"
    r"(?:含め|含める|記載|載せ|明記|掲載|盛り込|説明)(?:る|し|する)?")
_P1_CONTACT_WORD_RE = re.compile(r"問い合わせ|問合せ|連絡先")


def _p0a_fact_hash(registered_text: str | None) -> str:
    """原本ハッシュ(何を根拠にした値か)。決定的・短縮表示。"""
    import hashlib as _hashlib

    blob = str(registered_text or "")
    if not blob.strip():
        return ""
    return _hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _p0a_enrich_expected(
    expected: dict,
    *,
    semantic_role: str = "",
    target_artifact: str = "",
    accepted_forms: list[str] | None = None,
    match_policy: str = "",
    source_fact_id: str = "",
    source_hash: str = "",
) -> dict:
    """検証可能な事実のフィールドを付与する。既存の形は壊さない。"""
    enriched = dict(expected or {})
    if semantic_role and "semantic_role" not in enriched:
        enriched["semantic_role"] = semantic_role
    if target_artifact and "target_artifact" not in enriched:
        enriched["target_artifact"] = target_artifact
    if accepted_forms is not None and "accepted_forms" not in enriched:
        enriched["accepted_forms"] = list(accepted_forms)
    if match_policy and "match_policy" not in enriched:
        enriched["match_policy"] = match_policy
    if source_fact_id and "source_fact_id" not in enriched:
        enriched["source_fact_id"] = source_fact_id
    if source_hash and "source_hash" not in enriched:
        enriched["source_hash"] = source_hash
    return enriched


def _p1_extract_role_datetimes(registered_text: str) -> tuple[dict[str, list[str]], list[str]]:
    """登録事実から日時値を役割別に取り出す。決定的・順序安定・推測なし。

    役割ラベルが決定的に伴う値だけ役割を付け、範囲表記(A〜B)は開始/終了に
    割り当てる。ラベルが無い・複数役割が競合する値は汎用(役割不明)にする。
    """
    blob = str(registered_text or "")
    roles: dict[str, list[str]] = {role: [] for role, _label, _aliases, _kind in P1_DATETIME_ROLES}
    generic: list[str] = []
    ranged: set[str] = set()
    for match in _P1_RANGE_RE.finditer(blob):
        roles["start_time"].append(match.group(1))
        roles["end_time"].append(match.group(2))
        ranged.add(match.group(1))
        ranged.add(match.group(2))
    for match in _P0A_DATE_RE.finditer(blob):
        raw = match.group(0)
        if raw in ranged:
            continue
        window = blob[max(0, match.start() - 14):match.end() + 6]
        if _P1_DATE_LABEL_RE.search(window):
            roles["event_date"].append(raw)
        elif raw not in generic:
            generic.append(raw)
    for match in _P0A_TIME_RE.finditer(blob):
        raw = match.group(0)
        if raw in ranged:
            continue
        # 役割ラベルは「開始時刻10:00」のように時刻の直前に付く。前方の時刻値を
        # 除外しても、別の役割語が混ざる窓(「開始時刻10:00 終了時刻15:00」の
        # 「終了時刻15:00」の窓に「開始」が入る等)は競合として汎用に倒す。
        window_start = max(0, match.start() - 20)
        prev_value_end = 0
        for pattern in (_P0A_DATE_RE, _P0A_TIME_RE):
            for prev in pattern.finditer(blob, 0, match.start()):
                if prev.end() <= match.start() and prev.end() > prev_value_end:
                    prev_value_end = prev.end()
        if prev_value_end > window_start:
            window_start = prev_value_end
        window = blob[window_start:match.end() + 2]
        hits = [False, False, False]
        for position, (_role, label_re) in enumerate(_P1_ROLE_LABEL_RES):
            for label_match in label_re.finditer(window):
                # より長い役割ラベルに含まれる短い一致は数えない
                # (窓が「準備開始時刻...」のとき、その中の「開始...」は開始時刻にしない)。
                overlapped = False
                for other_position, (_other_role, other_re) in enumerate(_P1_ROLE_LABEL_RES):
                    if other_position == position:
                        continue
                    for other_match in other_re.finditer(window):
                        if (len(other_match.group(0)) > len(label_match.group(0))
                                and other_match.start() <= label_match.start()
                                and label_match.end() <= other_match.end()
                                and position != 0):
                            overlapped = True
                            break
                    if overlapped:
                        break
                if not overlapped:
                    hits[position] = True
                    break
        if sum(hits) == 1:
            if hits[0]:
                roles["prep_time"].append(raw)
            elif hits[1]:
                roles["start_time"].append(raw)
            else:
                roles["end_time"].append(raw)
        elif raw not in generic:
            generic.append(raw)
    for key in roles:
        roles[key] = sorted(set(roles[key]))
    return roles, sorted(set(generic))


def _p1_time_variants(hour: int, minute: int) -> list[str]:
    """時刻の許容表記。検証は正規化(分)で行い、こちらは契約の明示用。"""
    variants = [f"{hour}:{minute:02d}"]
    if minute:
        variants.append(f"{hour}時{minute}分")
    else:
        variants.append(f"{hour}時")
        variants.append(f"{hour}時00分")
    if hour == 0:
        variants.append("午前0時")
    elif 1 <= hour <= 11:
        variants.append(f"午前{hour}時")
        if minute:
            variants.append(f"午前{hour}時{minute}分")
    elif hour == 12:
        variants.append("正午")
        variants.append("12時")
    elif 13 <= hour <= 23:
        variants.append(f"午後{hour - 12}時")
        if minute:
            variants.append(f"午後{hour - 12}時{minute}分")
    return list(dict.fromkeys(variants))


def _p1_date_variants(year: int, month: int, day: int) -> list[str]:
    return [f"{year}年{month}月{day}日", f"{year}/{month}/{day}",
            f"{month}/{day}", f"{month}月{day}日"]


def _p1_parse_time_value(raw: str) -> tuple[int, int] | None:
    """登録値の時刻を(時, 分)にする。決定的に読めない値は None(推測しない)。"""
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


def _p1_parse_date_value(raw: str) -> tuple[int, int, int] | None:
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


def _p1_want_path(probe: str, criterion: str, outputs: list[dict], default: str) -> str:
    if not outputs:
        return default
    want = _p0a_role_expected_path(probe, criterion, outputs) if probe else None
    if want:
        return want
    return default


# P0-A: 要求節の核への置き換え。判定する対象と保存・照合する対象を一致させる。
# 末尾の要求動詞を除いた核に置き換え、元の語を cored_from に残す。
def _p0a_requirement_core(keyword: str) -> str:
    """要求節の核。末尾の要求動詞と前置きの役割語を除く。推測で膨らませない。

    A1: 「架空」「仮」等の装飾語が核に残る場合は除き、照合は実値で行う。
    例: 「架空の問い合わせ先を含め」→核は空(連絡先実値で検証)。
    """
    text = _norm(str(keyword or "")).strip("。！？")
    if not text:
        return ""
    core = _P0A_REQUIREMENT_TRAIL_RE.sub("", text).strip()
    if not core:
        return ""
    # 「案内文にはX」「チェックリストにはX」の前置きを除く。
    core = re.sub(r"^(?:.*?には|.*?とは)\s*", "", core).strip()
    core = _clean_term(core)
    # 問い合わせ先の装飾(架空・仮等)は核にしない。実値照合に任せる。
    core = re.sub(r"^(?:架空|仮|想定)(?:の)?", "", core).strip()
    core = _clean_term(core)
    return core.strip()


def _p0a_core_is_contact(core: str, original: str = "") -> bool:
    blob = f"{core} {original}"
    return bool(_P1_CONTACT_WORD_RE.search(blob))


def _p0a_existing_headings(items: list[dict], target_path: str) -> set[str]:
    headings: set[str] = set()
    for row in items or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("artifact_path") or "") != target_path:
            continue
        expected = row.get("expected") if isinstance(row.get("expected"), dict) else {}
        for heading in list((expected or {}).get("headings") or []):
            text = str(heading or "").strip()
            if text:
                headings.add(text)
    return headings

def _p0a_heading_covers(headings: set[str], core: str) -> bool:
    """核が見出しで既に確認されるか。同一見出しの重複だけを除く。

    P0-B回帰のため、長い見出し文中の部分一致では除去しない。
    例: 見出し「チェックリスト：準備・受付・終了後の手順」は核「準備」の
    確認にはならない。核と同一の見出し(「終了後確認」等)だけを重複とみなす。
    """
    needle = _norm(str(core or "")).strip()
    if not needle:
        return False
    for heading in headings:
        hay = _norm(str(heading or "")).strip()
        if not hay:
            continue
        if needle == hay:
            return True
    return False


def _p0a_core_items(
    criterion_id: str,
    criterion: str,
    matrix: list[dict],
    outputs: list[dict],
    registered_text: str,
) -> tuple[list[dict], dict]:
    """保存される keyword を要求節の核に置き換える。判定と照合の対象を一致させる。

    - 末尾の要求動詞を除いた核に置き換え、元の語を cored_from に残す。
    - 問い合わせ先の核は登録連絡先の値で検証する(具体値が無ければ input_missing)。
    - 核が既に見出しで確認される場合は重複する項目を作らない。
    - 核が残らない場合は input_missing にする(推測しない)。
    - 要求節の全文は保存しない。既存の具体的拒否は隠さない(順序を守る)。
    """
    rows = [row for row in (matrix or []) if isinstance(row, dict)]
    fixed: list[dict] = []
    grounded = 0
    recored: list[str] = []
    missing: list[str] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    sref = next((str(r.get("source_ref") or "") for r in rows
                 if str(r.get("source_ref") or "")), "")

    def _key(method: str, path: str, keywords: list[str]) -> tuple[str, tuple[str, ...]]:
        return (method, path, tuple(keywords))

    for row in rows:
        if str(row.get("method") or "") != "phrase":
            key = ("other", str(row.get("item_id") or ""))
            if key not in seen:
                seen.add(key)
                fixed.append(row)
            continue
        expected = dict(row.get("expected") or {})
        keywords = [str(k) for k in (expected.get("keywords") or []) if str(k)]
        # 核に置き換え済み・分解済み・役割別日時は対象外(重複させない)。
        # A1: 連絡先の装飾付き要求節(「架空の問い合わせ先を含め」等)は
        # 未分解でも実値照合へ寄せる。P0-B回帰の前提は変えない。
        if expected.get("decomposed_from") or expected.get("grounded_concept"):
            key = _key("phrase", str(row.get("artifact_path") or ""), keywords)
            if key not in seen:
                seen.add(key)
                fixed.append(row)
            continue
        if len(keywords) != 1:
            fixed.append(row)
            continue
        original = keywords[0]
        core = _p0a_requirement_core(original)
        trail_changed = _P0A_REQUIREMENT_TRAIL_RE.sub("", _norm(original).strip("。！？")).strip()
        if not core or core == original and trail_changed == original:
            # 要求節の形ではない通常語句。対象パスの照合だけ行う。
            fixed.append(row)
            continue
        if not core:
            item = dict(row)
            item["method"] = "input_missing"
            item["requirement"] = f"「{original}」の核が残らないため判定不能"
            item["expected"] = {
                "concept": "requirement_core",
                "reason": f"要求節「{original}」から検証可能な核を取り出せないため検査契約を作れない",
                "needed_inputs": ["検証可能な具体値を含む達成条件・登録事実の追加"],
                "cored_from": original,
            }
            missing.append(str(item.get("item_id") or ""))
            fixed.append(item)
            continue
        headings = _p0a_existing_headings(
            fixed + rows[rows.index(row) + 1:],
            str(row.get("artifact_path") or ""),
        )
        # 問い合わせ先は登録連絡先の値(resolve_contact_values)で検証する。
        if _p0a_core_is_contact(core, original):
            selected = _contact_request_selected()
            mode, usable, effective, _note = _contact_registry_state_for_build(criterion)
            narrowed = list(usable)
            if selected:
                narrowed = [entry for entry in usable
                            if str(entry.get("kind") or "") in selected]
            if mode == "registry" and narrowed:
                built_expected, values = _contact_build_expected(
                    narrowed, effective=effective, selected=selected)
                target = _p1_want_path(core, criterion, outputs,
                                       str(row.get("artifact_path") or ""))
                key = _key("phrase", target, values)
                if key in seen:
                    continue
                seen.add(key)
                item = dict(row)
                item["artifact_path"] = target
                item["requirement"] = f"必須の問い合わせ先「{'、'.join(values)}」のいずれかを含む"
                item["expected"] = _p0a_enrich_expected(
                    {"keywords": values, "match": "any",
                     "cored_from": original,
                     **built_expected},
                    semantic_role="連絡先",
                    target_artifact=target,
                    accepted_forms=list(values),
                    match_policy="any",
                    source_fact_id="contact",
                    source_hash=_p0a_fact_hash(registered_text),
                )
                recored.append(str(item.get("item_id") or ""))
                grounded += 1
                fixed.append(item)
                continue
            if mode in ("registry", "no_usable", "unreadable"):
                reason, needed, requirement = _contact_input_missing_reason(
                    mode=mode if mode != "registry" else "no_usable",
                    effective=effective, selected=selected,
                    criterion=criterion)
                target = _p1_want_path(core, criterion, outputs,
                                       str(row.get("artifact_path") or ""))
                key = _key("input_missing", target, [core])
                if key not in seen:
                    seen.add(key)
                    item = dict(row)
                    item["artifact_path"] = target
                    item["method"] = "input_missing"
                    item["requirement"] = requirement
                    item["expected"] = _p0a_enrich_expected(
                        {
                            "concept": "contact",
                            "reason": reason,
                            "needed_inputs": [needed],
                            "cored_from": original,
                            "contact_purpose": effective,
                        },
                        semantic_role="連絡先",
                        target_artifact=target,
                        accepted_forms=[],
                        match_policy="any",
                        source_fact_id="contact",
                        source_hash=_p0a_fact_hash(registered_text),
                    )
                    missing.append(str(item.get("item_id") or ""))
                    fixed.append(item)
                continue
            resolved = resolve_contact_values(
                _contact_request_project_id(), registered_text)
            contacts = [entry.get("value") for entry in resolved if entry.get("value")]
            source_hash = _p0a_fact_hash(registered_text)
            target = _p1_want_path(core, criterion, outputs, str(row.get("artifact_path") or ""))
            if contacts and is_p0a_contact_values_resolved(contacts, registered_text):
                key = _key("phrase", target, contacts)
                if key in seen:
                    continue
                seen.add(key)
                item = dict(row)
                item["artifact_path"] = target
                item["requirement"] = f"必須の問い合わせ先「{'、'.join(contacts)}」のいずれかを含む"
                item["expected"] = _p0a_enrich_expected(
                    {"keywords": contacts, "match": "any",
                     "cored_from": original,
                     "source_kind": "registered_context"},
                    semantic_role="連絡先",
                    target_artifact=target,
                    accepted_forms=list(contacts),
                    match_policy="any",
                    source_fact_id="contact",
                    source_hash=source_hash,
                )
                recored.append(str(item.get("item_id") or ""))
                grounded += 1
                fixed.append(item)
            else:
                key = _key("input_missing", target, [core])
                if key not in seen:
                    seen.add(key)
                    item = dict(row)
                    item["artifact_path"] = target
                    item["method"] = "input_missing"
                    item["requirement"] = "問い合わせ先の具体値が原本にないため判定不能"
                    item["expected"] = _p0a_enrich_expected(
                        {
                            "concept": "contact",
                            "reason": "問い合わせ先(メール/電話)の具体値が登録事実に明示されていないため検査契約を作れない",
                            "needed_inputs": ["問い合わせ先(メール/電話)の具体値を含む登録事実の追加"],
                            "cored_from": original,
                        },
                        semantic_role="連絡先",
                        target_artifact=target,
                        accepted_forms=[],
                        match_policy="all",
                        source_fact_id="contact",
                        source_hash=_p0a_fact_hash(registered_text),
                    )
                    missing.append(str(item.get("item_id") or ""))
                    fixed.append(item)
            continue
        # 見出しで既に確認される核は重複させない。
        if _p0a_heading_covers(headings, core):
            continue
        key = _key(str(row.get("method") or "phrase"),
                   str(row.get("artifact_path") or ""), [core])
        if key in seen:
            continue
        seen.add(key)
        item = dict(row)
        item["requirement"] = f"必須語「{core}」を含む"
        item["expected"] = _p0a_enrich_expected(
            {"keywords": [core], "match": "all",
             "cored_from": original},
            semantic_role=core,
            target_artifact=str(row.get("artifact_path") or ""),
            accepted_forms=[core],
            match_policy="all",
            source_fact_id="keyword",
            source_hash=_p0a_fact_hash(registered_text),
        )
        recored.append(str(item.get("item_id") or ""))
        grounded += 1
        fixed.append(item)
    report = {"grounded": grounded, "recored": recored, "input_missing": missing}
    return fixed, report


def _p1_role_target_path(role: str, criterion: str, outputs: list[dict], fallback: str) -> str:
    """役割別日時項目の対象パス。節の名指しを優先し、次に汎用の役割対応を使う。

    節が複数の成果物に触れる・名指ししない場合は推測せず汎用の役割対応に倒し、
    それでも決められなければ fallback を返す(呼出側で入力不足にする)。
    別の成果物の値を拾わない。
    """
    clause_target = _p1_role_clause_target(role, criterion, outputs)
    if clause_target is not None:
        return clause_target
    role_default = _p1_role_default_path(role, outputs)
    if role_default is not None:
        return role_default
    if fallback:
        return fallback
    first = _first_output(outputs)
    return first


def _p1_build_datetime_items(
    criterion_id: str,
    concept: str,
    criterion: str,
    outputs: list[dict],
    registered_text: str,
    sref: str,
    default_path: str = "",
) -> tuple[list[dict], dict]:
    """概念(日時/日付/時刻)の要求を役割別の項目に分ける。

    原本値がある役割だけ項目を作る(無い役割の推測はしない)。達成条件に明示の
    役割語(〜時刻)があるのに原本値が無い役割は input_missing にする。裸の内容語
    (準備など時刻を伴わない言及)は時刻の要求とみなさない。各項目は match:any
    (許容表記のいずれか)を明示する。
    """
    roles, generic = _p1_extract_role_datetimes(registered_text)
    criterion_text = str(criterion or "")
    fallback = default_path or _p0a_role_expected_path(concept, criterion_text, outputs) or _first_output(outputs)
    built: list[dict] = []
    grounded = 0
    missing: list[str] = []
    index = 0

    def _next_id() -> str:
        nonlocal index
        index += 1
        return f"{criterion_id}-R{index:02d}"

    # 『いずれか』の契約は1項目の ANY にする(実装も ANY)。
    if _P1_ANY_MARK_RE.search(criterion_text):
        impending: list[str] = []
        for role, _label, _aliases, _kind in P1_DATETIME_ROLES:
            impending.extend(roles.get(role) or [])
        impending.extend(generic)
        impending = sorted(set(impending))
        if impending:
            target = fallback
            row_id = _next_id()
            built.append({
                "item_id": row_id, "criterion_id": criterion_id,
                "artifact_path": target,
                "requirement": f"必須語「{'、'.join(impending)}」のいずれかを含む（{concept}の具体値）",
                "method": "phrase", "required": True, "source_ref": sref,
                "expected": {"keywords": impending, "match": "any",
                             "grounded_concept": concept},
            })
            return built, {"grounded": 1, "input_missing": []}
    for role, label, _aliases, kind in P1_DATETIME_ROLES:
        values = roles.get(role) or []
        explicit = label in criterion_text
        if not values:
            if explicit:
                row_id = _next_id()
                built.append({
                    "item_id": row_id, "criterion_id": criterion_id,
                    "artifact_path": fallback,
                    "requirement": f"{label}の具体値が原本にないため判定不能",
                    "method": "input_missing", "required": True, "source_ref": sref,
                    "expected": {
                        "concept": concept, "datetime_role": role,
                        "reason": f"{label}の具体値が登録事実に明示されていないため検査契約を作れない",
                        "needed_inputs": [f"{label}の具体値を含む登録事実の追加"],
                    },
                })
                missing.append(row_id)
            continue
        if concept not in criterion_text and not explicit:
            continue
        for raw in values:
            row_id = _next_id()
            if kind == "date":
                parsed = _p1_parse_date_value(raw)
                if parsed is None or parsed[0] == 0:
                    canonical, extra = raw, {}
                else:
                    year, month, day = parsed
                    canonical = f"{year}年{month}月{day}日"
                    extra = {"date": [year, month, day],
                             "variants": _p1_date_variants(year, month, day)}
            else:
                parsed = _p1_parse_time_value(raw)
                if parsed is None:
                    canonical, extra = raw, {}
                else:
                    hour, minute = parsed
                    canonical = f"{hour}:{minute:02d}"
                    extra = {"minutes": hour * 60 + minute,
                             "variants": _p1_time_variants(hour, minute)}
            keywords = list(extra.get("variants") or [raw])
            target = _p1_role_target_path(role, criterion_text, outputs, fallback)
            expected = {"keywords": keywords, "match": "any",
                        "grounded_concept": concept,
                        "datetime_role": role, "canonical": canonical,
                        "datetime_normalized": True}
            expected.update(extra)
            built.append({
                "item_id": row_id, "criterion_id": criterion_id,
                "artifact_path": target,
                "requirement": f"必須語「{canonical}」を含む（{label}の具体値）",
                "method": "phrase", "required": True, "source_ref": sref,
                "expected": expected,
            })
            grounded += 1
    for raw in generic:
        if concept not in criterion_text:
            continue
        row_id = _next_id()
        parsed_time = _p1_parse_time_value(raw)
        parsed_date = _p1_parse_date_value(raw)
        if parsed_time is not None:
            hour, minute = parsed_time
            canonical = f"{hour}:{minute:02d}"
            extra = {"minutes": hour * 60 + minute,
                     "variants": _p1_time_variants(hour, minute)}
        elif parsed_date is not None and parsed_date[0] != 0:
            year, month, day = parsed_date
            canonical = f"{year}年{month}月{day}日"
            extra = {"date": [year, month, day],
                     "variants": _p1_date_variants(year, month, day)}
        else:
            canonical, extra = raw, {}
        keywords = list(extra.get("variants") or [raw])
        expected = {"keywords": keywords, "match": "any",
                    "grounded_concept": concept,
                    "datetime_role": None, "canonical": canonical,
                    "datetime_normalized": True}
        expected.update(extra)
        if "日時" in str(criterion_text or "") and "準備" in str(criterion_text or ""):
            # 日時と準備開始が同じ概念に混ざる達成条件では、役割不明の時刻値は
            # 準備開始の項目に含めず、原本値の検証は役割別項目に任せる(誤一致防止)。
            pass
        else:
            built.append({
                "item_id": row_id, "criterion_id": criterion_id,
                "artifact_path": fallback,
                "requirement": f"必須語「{canonical}」を含む（{concept}の具体値）",
                "method": "phrase", "required": True, "source_ref": sref,
                "expected": expected,
            })
            grounded += 1
    return built, {"grounded": grounded, "input_missing": missing}


def _p1_covered_terms(items: list[dict]) -> set[str]:
    covered: set[str] = set()
    for row in items or []:
        if not isinstance(row, dict):
            continue
        expected = row.get("expected") if isinstance(row.get("expected"), dict) else {}
        for key in list((expected or {}).get("keywords") or []):
            if str(key):
                covered.add(str(key))
        for heading in list((expected or {}).get("headings") or []):
            if str(heading):
                covered.add(str(heading))
        for found in re.findall(r"「([^」]{1,30})」", str(row.get("requirement") or "")):
            covered.add(found)
    return covered


def _p1_registered_contacts(registered_text: str) -> list[str]:
    """後方互換の薄い wrapper。実値は resolve_contact_values に集約する。"""
    values = [entry.get("value") for entry in resolve_contact_values(
        _contact_request_project_id(), registered_text)
              if entry.get("value")]
    return sorted(values)


def _contact_registry_state_for_build(criterion: str):
    """契約生成時の台帳状態。(mode, usable, effective, source_ref_note)。

    modeは registry/unregistered/unreadable/no_usable。読取失敗は送出せず
    ここでは unreadable を返す(呼出側が input_missing にする)。
    """
    pid = _contact_request_project_id()
    if _CONTACT_REGISTRY_PROVIDER is None or not pid:
        return "unregistered", [], _contact_effective_purpose(), ""
    try:
        from app.contact_registry import RegistryReadError as _ReadError
    except (ImportError, AttributeError, ValueError):
        _ReadError = None  # type: ignore[assignment]
    try:
        state, usable, effective = _contact_usable_entries(pid, None)
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        return ("unreadable", [], _contact_effective_purpose(),
                f"registry_unreadable:{type(exc).__name__}")
    except Exception as exc:
        return ("unreadable", [], _contact_effective_purpose(),
                f"registry_unreadable:{type(exc).__name__}")
    if state != "ok":
        return "unregistered", [], effective, ""
    if not usable:
        return "no_usable", [], effective, ""
    return "registry", list(usable), effective, ""


def _contact_build_expected(entries: list[dict], *, effective: str,
                            source_kind_note: str = "",
                            selected: list[str] | None = None,
                            source_fact_id: str = "contact"):
    """問い合わせ先の expected を作る。match=any・版束縛・用途・理由を含む。"""
    values = [str(entry.get("value") or "") for entry in entries
              if str(entry.get("value") or "")]
    values = sorted(set(values))
    expected: dict = {"keywords": values, "match": "any"}
    route_ids = sorted({str(entry.get("route_id") or "") for entry in entries
                        if str(entry.get("route_id") or "")})
    versions = sorted({entry.get("version") for entry in entries
                       if isinstance(entry.get("version"), int)})
    hashes = sorted({str(entry.get("value_sha256") or _contact_value_sha256(
                         str(entry.get("value") or "")))
                     for entry in entries if str(entry.get("value") or "")})
    if len(route_ids) == 1:
        expected["contact_route_id"] = route_ids[0]
    elif route_ids:
        expected["contact_route_ids"] = route_ids
    if len(versions) == 1:
        expected["contact_version"] = versions[0]
    elif versions:
        expected["contact_versions"] = versions
    if len(hashes) == 1:
        expected["contact_value_sha256"] = hashes[0]
    elif hashes:
        expected["contact_value_sha256_list"] = hashes
    expected["contact_purpose"] = effective
    if source_kind_note:
        expected["contact_source_note"] = source_kind_note
    if selected:
        expected["contact_selected_kinds"] = list(selected)
    source_kinds = sorted({str(entry.get("source_kind") or "") for entry in entries})
    if "registered_context" in source_kinds and len(source_kinds) == 1:
        expected["source_kind"] = "registered_context"
    return expected, values


def _contact_input_missing_reason(*, mode: str, effective: str,
                                  selected: list[str] | None,
                                  criterion: str) -> tuple[str, str, list[str]]:
    """(reason, needed_inputs[0], requirement文)。推測せず入力待ちにする。"""
    if mode == "unreadable":
        return ("登録台帳を読めないため判定不能(フォールバックしない)",
                "登録台帳の読取状態の復旧",
                "問い合わせ先は登録台帳の読取不能のため判定不能")
    if selected:
        kinds_ja = {"email": "メール", "phone": "電話", "form": "Webフォーム"}
        want = "・".join(kinds_ja.get(k, k) for k in selected)
        return (f"明示選択された経路({want})に使用可能な登録値が無いため判定不能",
                f"明示選択された経路({want})の実値の登録",
                f"明示選択された問い合わせ経路({want})の実値が未登録のため判定不能")
    policy_kinds = _contact_policy_kinds(criterion)
    if effective == "public" and mode in ("registry", "no_usable"):
        return ("公開用途に使用可能な登録値が無いため判定不能"
                "(架空値は公開に使えない・未確認値は要確認)",
                "公開用途に使用可能な問い合わせ先の登録または確認",
                "公開用途に使用可能な問い合わせ先の登録値が無いため判定不能")
    if policy_kinds == {"form"}:
        return ("Webフォーム優先の方針だがURL未登録のため判定不能(URLは発明しない)",
                "問い合わせフォームURLの登録",
                "問い合わせフォームURLが未登録のため判定不能")
    return ("問い合わせ先の具体値が登録事実に無いため判定不能",
            "問い合わせ先(メール/電話/フォームURL)の具体値を含む登録事実の追加",
            "問い合わせ先の具体値が原本にないため判定不能")


def _p1_single_include_items(
    norm_text: str,
    items: list[dict],
    cleaned_outputs: list[dict],
    default_path: str,
    registered_text: str,
) -> list[dict]:
    """『Xを含め/記載し』型の単独要求節を検証可能な条件に分解する。

    要求節自体を丸ごと必須フレーズにしない。問い合わせ先は原本の連絡先値に、
    『〜確認』は必須見出しに、その他は必須語に分解する。既に項目化済みの語は
    作らない。registered がある場合だけ追加する(旧呼び出しの互換を維持)。
    """
    if not cleaned_outputs:
        return []
    covered = _p1_covered_terms(items)
    out: list[dict] = []
    seen: set[str] = set()
    for match in _P1_SINGLE_INCLUDE_RE.finditer(str(norm_text or "")):
        term = _clean_term(match.group(1) or match.group(2) or "")
        term = re.sub(r"^(?:.*?には)", "", term).strip()
        if not term or len(term) < 2 or len(term) > 12 or term in seen:
            continue
        if term in covered:
            continue
        if term in P0A_TEMPORAL_CONCEPTS or term in _P0A_ROLE_WORDS:
            continue
        # 日時の役割要求は役割別の具体値項目が担う。要求節そのものを語句にしない。
        if any(label in term for label in P1_DATETIME_ROLE_LABELS.values()):
            continue
        if term.endswith("には") or term.endswith("とは"):
            continue
        seen.add(term)
        target = _p1_want_path(term, str(norm_text or ""), cleaned_outputs, default_path)
        # 同じ節に成果物役割が複数ある場合は、対象語の直前にある最寄りの役割を使う。
        term_pos = str(norm_text or "").find(term)
        if term_pos >= 0:
            prefix = str(norm_text or "")[:term_pos]
            role_positions = [(prefix.rfind(role_word), role_word)
                              for role_word in _P0A_ROLE_WORDS if prefix.rfind(role_word) >= 0]
            if role_positions:
                nearest_role = max(role_positions)[1]
                role_target = _p0a_role_expected_path(nearest_role, nearest_role, cleaned_outputs)
                if role_target:
                    target = role_target
        if _P1_CONTACT_WORD_RE.search(term):
            # A3: 連絡先は登録台帳の使用可能な値を優先し、台帳が空のときだけ
            # 登録コンテキストの値へフォールバックする。読取失敗は input_missing。
            # 方針と異なる登録値は勝手に使わず、明示選択があるときはその経路だけ使う。
            # 照合は match=any(いずれか1つ)。版・ハッシュ・用途を expected に束縛する。
            selected = _contact_request_selected()
            mode, usable, effective, _note = _contact_registry_state_for_build(
                str(norm_text or ""))
            narrowed = list(usable)
            if selected:
                narrowed = [entry for entry in usable
                            if str(entry.get("kind") or "") in selected]
            if mode == "registry" and narrowed:
                built_expected, values = _contact_build_expected(
                    narrowed, effective=effective, selected=selected)
                built_expected = _p0a_enrich_expected(
                    {"keywords": values, "match": "any",
                     "cored_from": term, "decomposed_from": term,
                     **built_expected},
                    semantic_role="連絡先",
                    target_artifact=target,
                    accepted_forms=list(values),
                    match_policy="any",
                    source_fact_id="contact",
                    source_hash=_p0a_fact_hash(registered_text),
                )
                out.append({
                    "artifact_path": target,
                    "requirement": f"必須の問い合わせ先「{'、'.join(values)}」のいずれかを含む",
                    "method": "phrase", "required": True,
                    "expected": built_expected,
                })
            elif mode in ("registry", "no_usable", "unreadable"):
                reason, needed, requirement = _contact_input_missing_reason(
                    mode=mode if mode != "registry" else "no_usable",
                    effective=effective, selected=selected,
                    criterion=str(norm_text or ""))
                out.append({
                    "artifact_path": target,
                    "requirement": requirement,
                    "method": "input_missing", "required": True,
                    "expected": _p0a_enrich_expected(
                        {
                            "concept": "contact",
                            "reason": reason,
                            "needed_inputs": [needed],
                            "cored_from": term, "decomposed_from": term,
                            "contact_purpose": effective,
                        },
                        semantic_role="連絡先",
                        target_artifact=target,
                        accepted_forms=[],
                        match_policy="any",
                        source_fact_id="contact",
                        source_hash=_p0a_fact_hash(registered_text),
                    ),
                })
            else:
                # 台帳が空のときだけ登録コンテキストへフォールバック(従来どおり)。
                # 照合は登録値のいずれか(any)。版束縛は台帳由来のみ。
                resolved = resolve_contact_values(
                    _contact_request_project_id(), str(registered_text or ""))
                contacts = [entry.get("value") for entry in resolved if entry.get("value")]
                target = _p1_want_path(term, str(norm_text or ""), cleaned_outputs, default_path)
                if contacts:
                    out.append({
                        "artifact_path": target,
                        "requirement": f"必須の問い合わせ先「{'、'.join(contacts)}」のいずれかを含む",
                        "method": "phrase", "required": True,
                        "expected": _p0a_enrich_expected(
                            {"keywords": contacts, "match": "any",
                             "cored_from": term, "decomposed_from": term,
                             "source_kind": "registered_context"},
                            semantic_role="連絡先",
                            target_artifact=target,
                            accepted_forms=list(contacts),
                            match_policy="any",
                            source_fact_id="contact",
                            source_hash=_p0a_fact_hash(registered_text),
                        )
                    })
                else:
                    out.append({
                        "artifact_path": target,
                        "requirement": "問い合わせ先の具体値が原本にないため判定不能",
                        "method": "input_missing", "required": True,
                        "expected": _p0a_enrich_expected(
                            {
                                "concept": "contact",
                                "reason": "問い合わせ先(メール/電話)の具体値が登録事実に明示されていないため検査契約を作れない",
                                "needed_inputs": ["問い合わせ先(メール/電話)の具体値を含む登録事実の追加"],
                                "cored_from": term, "decomposed_from": term,
                            },
                            semantic_role="連絡先",
                            target_artifact=target,
                            accepted_forms=[],
                            match_policy="any",
                            source_fact_id="contact",
                            source_hash=_p0a_fact_hash(registered_text),
                        ),
                    })
            continue
        if term.endswith("確認"):
            out.append({
                "artifact_path": target,
                "requirement": f"{target}に必須見出し「{term}」を含む",
                "method": "headings", "required": True,
                "expected": _p0a_enrich_expected(
                    {"headings": [term], "decomposed_from": term},
                    semantic_role="終了後確認" if "終了後" in term else term,
                    target_artifact=target,
                    accepted_forms=[term],
                    match_policy="all",
                    source_fact_id="heading",
                    source_hash=_p0a_fact_hash(registered_text),
                ),
            })
            continue
        out.append({
            "artifact_path": target,
            "requirement": f"必須語「{term}」を含む",
            "method": "phrase", "required": True,
            "expected": {"keywords": [term], "match": "all",
                         "decomposed_from": term},
        })
    return out


def normalize_p0a_items(
    criterion_id: str,
    criterion: str,
    matrix: list[dict],
    outputs: list[dict],
    registered_text: str | None = None,
) -> tuple[list[dict], dict]:
    """P0-A の正規化。要求文と検査対象の分離・対象パスの照合を行う。

    - 汎用概念(日時・日付・時刻)は原本の具体値に置き換える。具体値が無ければ
      推測せず input_missing にする(判定不能。合格にも不合格にも倒さない)。
    - 役割語の付着などの残りかすは narrow に取り除く。それ以外の語は変えない。
    - 対象パスは汎用的な役割で決め直す(規則が LLM の指定を上書きする)。
    - 要求文自体の語句化の除去は行わない(保存前検査で不合格にするため残す)。
    """
    rows = list(matrix or [])
    fixed: list[dict] = []
    grounded = 0
    rerouted: list[str] = []
    recored: list[str] = []
    missing: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            fixed.append(row)
            continue
        item = dict(row)
        expected = dict(item.get("expected") or {})
        if str(item.get("method") or "") == "phrase":
            keywords = [str(k) for k in (expected.get("keywords") or []) if str(k)]
            if len(keywords) == 1:
                anchor = keywords[0]
                concept = _p0a_concept_of(anchor)
                if not concept:
                    core = _p0a_core_term(anchor)
                    if core != anchor:
                        concept = _p0a_concept_of(core)
                        keywords = [core]
                        expected["keywords"] = keywords
                        expected["cored_from"] = anchor
                        item["requirement"] = f"必須語「{core}」を含む"
                        recored.append(str(item.get("item_id") or ""))
                        anchor = core
                if concept and registered_text is not None:
                    values = _p0a_registered_datetime_values(registered_text)
                    if values:
                        expected["keywords"] = values
                        expected["grounded_concept"] = concept
                        item["requirement"] = (
                            f"必須語「{'、'.join(values)}」を含む（{concept}の具体値）"
                        )
                        grounded += 1
                    else:
                        item["method"] = "input_missing"
                        item["requirement"] = f"{concept}の具体値が原本にないため判定不能"
                        expected = {
                            "concept": concept,
                            "reason": (
                                f"{concept}の具体値が登録事実に明示されていないため検査契約を作れない"
                            ),
                            "needed_inputs": ["開催日・時刻などの具体値を含む登録事実の追加"],
                        }
                        item["expected"] = expected
                        missing.append(str(item.get("item_id") or ""))
                        fixed.append(item)
                        continue
                probe = keywords[0] if len(keywords) == 1 else ""
                want: str | None = None
                if probe:
                    want = _p0a_role_expected_path(probe, criterion, outputs)
                    if want is None and concept:
                        want = _p0a_role_expected_path(concept, criterion, outputs)
                if want and str(item.get("artifact_path") or "") != want:
                    item["artifact_path"] = want
                    rerouted.append(str(item.get("item_id") or ""))
        item["expected"] = expected
        fixed.append(item)
    # 概念だけが要求され、対応する項目が無いときは作る(作らないと無検査になる)。
    if registered_text is not None:
        cleaned_outputs = _md_outputs(outputs)
        sref = next((str(r.get("source_ref") or "") for r in fixed
                     if isinstance(r, dict) and str(r.get("source_ref") or "")), "")
        # P1-1: 日時概念の要求を役割別の項目に分ける(開始/終了/準備の取り違え防止)。
        # 原本値がある役割だけ作る。無い役割は推測せず、明示の役割語がある場合
        # だけ input_missing にする。既存の具体値1件の置換(実走P0-A)より優先する。
        # A1: 役割別日時項目に検証可能な事実のフィールドを付与する。
        # 既存の expected の形は壊さない。
        replaced = False
        role_jp = {"event_date": "開催日", "start_time": "開始時刻",
                   "end_time": "終了時刻", "prep_time": "準備開始時刻",
                   None: "日時"}
        source_hash_all = _p0a_fact_hash(registered_text)
        for concept in P0A_TEMPORAL_CONCEPTS:
            if concept not in str(criterion or ""):
                continue
            built, sub = _p1_build_datetime_items(
                str(criterion_id or ""), concept, criterion, outputs,
                registered_text, sref,
                default_path=(_first_output(outputs) if cleaned_outputs else ""))
            if built or sub.get("grounded") or sub.get("input_missing"):
                enriched_built: list[dict] = []
                for row in built:
                    if not isinstance(row, dict):
                        enriched_built.append(row)
                        continue
                    new_row = dict(row)
                    new_expected = dict(new_row.get("expected") or {})
                    role = new_expected.get("datetime_role")
                    keywords = [str(k) for k in (new_expected.get("keywords") or []) if str(k)]
                    match_policy = str(new_expected.get("match") or "")
                    new_row["expected"] = _p0a_enrich_expected(
                        new_expected,
                        semantic_role=str(role_jp.get(role, role) or "日時"),
                        target_artifact=str(new_row.get("artifact_path") or ""),
                        accepted_forms=keywords,
                        match_policy=match_policy,
                        source_fact_id=f"datetime:{role}",
                        source_hash=source_hash_all,
                    )
                    enriched_built.append(new_row)
                built = enriched_built
            if built or sub.get("grounded") or sub.get("input_missing"):
                fixed = [row for row in fixed if not (
                    isinstance(row, dict) and str(row.get("method") or "") == "phrase"
                    and (row.get("expected") or {}).get("grounded_concept") == concept)]
                fixed.extend(built)
                grounded += int(sub.get("grounded") or 0)
                missing.extend(sub.get("input_missing") or [])
                replaced = True
        if replaced:
            # 以降の既存処理(汎用の概念補完)は役割分割済みのため行わない。
            report = {"version": P0A_CONTRACT_VERSION, "grounded": grounded,
                      "recored": recored, "rerouted": rerouted, "input_missing": missing}
            # C0: 早期リターン経路も単一出口で一意にする(日時役割分割の新規ID衝突を防ぐ)。
            try:
                fixed = ensure_unique_item_ids(str(criterion_id or ""), fixed)
            except (TypeError, ValueError, AttributeError):
                raise
            except Exception as exc:
                raise ValueError(
                    f"item id uniquify failed: {type(exc).__name__}") from exc
            assert_unique_item_ids(fixed, where="normalize_p0a_items")
            return fixed, report
        covered: set[str] = set()
        for row in fixed:
            if not isinstance(row, dict):
                continue
            exp = row.get("expected") if isinstance(row.get("expected"), dict) else {}
            for key in ("grounded_concept", "concept"):
                if str((exp or {}).get(key) or ""):
                    covered.add(str((exp or {}).get(key)))
            if str(row.get("method") or "") == "phrase":
                for keyword in ((exp or {}).get("keywords") or []):
                    found_concept = _p0a_concept_of(str(keyword))
                    if found_concept:
                        covered.add(found_concept)
        sref = next((str(r.get("source_ref") or "") for r in fixed
                     if isinstance(r, dict) and str(r.get("source_ref") or "")), "")
        for concept in P0A_TEMPORAL_CONCEPTS:
            if concept not in str(criterion or "") or concept in covered:
                continue
            values = _p0a_registered_datetime_values(registered_text)
            target = (_p0a_role_expected_path(concept, criterion, outputs)
                      or _first_output(outputs))
            if not target:
                continue
            if values:
                fixed.append(_item(
                    str(criterion_id or ""), len(fixed) + 1, target,
                    f"必須語「{'、'.join(values)}」を含む（{concept}の具体値）",
                    "phrase", True, sref,
                    {"keywords": values, "grounded_concept": concept}))
                grounded += 1
            else:
                fixed.append(_item(
                    str(criterion_id or ""), len(fixed) + 1, target,
                    f"{concept}の具体値が原本にないため判定不能",
                    "input_missing", True, sref,
                    {"concept": concept,
                     "reason": f"{concept}の具体値が登録事実に明示されていないため検査契約を作れない",
                     "needed_inputs": ["開催日・時刻などの具体値を含む登録事実の追加"]}))
                missing.append(f"new-{concept}")
    report = {"version": P0A_CONTRACT_VERSION, "grounded": grounded,
              "recored": recored, "rerouted": rerouted, "input_missing": missing}
    # C0: normalize を通る経路も単一出口で一意にする。保存後の再正規化で
    # 日時役割分割が新規IDを振り直して既存IDと衝突する重複を防ぐ。
    # 既に一意なIDは変えない。IDが無い行は決定的に補う。
    try:
        from app.requirement_matrix import (
            assert_unique_item_ids as _assert_unique,
            ensure_unique_item_ids as _ensure_unique,
        )
    except (ImportError, AttributeError, ValueError):
        raise
    try:
        fixed = _ensure_unique(str(criterion_id or ""), fixed)
    except (TypeError, ValueError, AttributeError):
        raise
    except Exception as exc:
        raise ValueError(
            f"item id uniquify failed: {type(exc).__name__}") from exc
    _assert_unique(fixed, where="normalize_p0a_items")
    return fixed, report


def p0a_contract_issues(criterion: str, matrix: list[dict], outputs: list[dict]) -> list[str]:
    """保存前の P0-A 検査。要求文の語句化・対象パスの取り違えを不合格にする。"""
    issues: list[str] = []
    for row in (matrix or []):
        if not isinstance(row, dict):
            continue
        item_id = str(row.get("item_id") or "")
        method = str(row.get("method") or "")
        if method == "phrase":
            expected = row.get("expected") if isinstance(row.get("expected"), dict) else {}
            keywords = [str(k) for k in ((expected or {}).get("keywords") or []) if str(k)]
            for keyword in keywords:
                if is_p0a_requirement_sentence_keyword(keyword, criterion):
                    issues.append(
                        f"requirement sentence must not be a keyword: {item_id} ({keyword[:20]})")
                    break
            want: str | None = None
            # P1-1: 日時の役割別項目は節の名指し→汎用の役割対応の順で対象パスを照合する。
            # 汎用(役割不明)は概念(日時等)で照合する。従来の単一語句の照合も維持する。
            role = (expected or {}).get("datetime_role")
            concept = str((expected or {}).get("grounded_concept") or "")
            if role in P1_DATETIME_ROLE_LABELS:
                clause_target = _p1_role_clause_target(
                    str(role), criterion, outputs)
                role_default = _p1_role_default_path(str(role), outputs)
                want = clause_target if clause_target is not None else role_default
                if want is None:
                    want = _p1_want_path(P1_DATETIME_ROLE_LABELS[str(role)],
                                        criterion, outputs,
                                        _first_output(outputs))
                if want is None and concept:
                    want = _p0a_role_expected_path(concept, criterion, outputs)
            elif concept:
                want = _p0a_role_expected_path(concept, criterion, outputs)
            else:
                probe = keywords[0] if len(keywords) == 1 else ""
                want = _p0a_role_expected_path(probe, criterion, outputs) if probe else None
            if want and str(row.get("artifact_path") or "") != want:
                issues.append(
                    f"wrong_target: {item_id} points at {row.get('artifact_path')} "
                    f"but belongs to {want}")
        if method == "input_missing":
            expected = row.get("expected") if isinstance(row.get("expected"), dict) else {}
            if not str((expected or {}).get("reason") or ""):
                issues.append(f"input_missing without reason: {item_id}")
    return sorted(set(issues))


def extract_requirement_items(
    criterion_id: str,
    criterion: str,
    outputs: list[dict],
    source_ids: list[str],
    llm_suggestions: list[dict] | None = None,
    require_semantic: bool = False,
    registered_text: str | None = None,
) -> list[dict]:
    """達成条件1件を原子項目へ分解する。常に決定的。空にしない(見出しは作る)。

    P0-A: registered_text を渡すと、要求文と検査対象の分離(具体値化)・対象パスの
    照合(役割語→出力パス)を行う。渡さない場合は従来どおりの抽出だけを行う。
    """
    text = _norm(criterion)
    source_ref = _source_ref(list(source_ids or []))
    items: list[dict] = []
    index = 1

    def add(artifact_path: str, requirement: str, method: str, required: bool, expected: dict) -> None:
        nonlocal index
        if not artifact_path or method not in METHODS:
            return
        items.append(_item(criterion_id, index, artifact_path, requirement, method, required, source_ref, expected))
        index += 1

    def target_for(term: str, cleaned_outputs: list[dict], default: str) -> str:
        matches = [
            str(output.get("path") or "") for output in cleaned_outputs
            if any(term in str(h) or str(h) in term
                   for h in (output.get("required_headings") or []) if str(h))
        ]
        return matches[0] if len(matches) == 1 else default

    cleaned = sorted(_md_outputs(outputs), key=lambda o: str(o.get("path") or ""))
    if not cleaned:
        return []
    first_path = str(cleaned[0].get("path") or "")

    # 個数の要求。ファイル数は本文中の語の出現回数ではなく成果物契約で検査する。
    for match in _COUNT_UNIT_RE.finditer(text):
        number, unit = match.group(1), match.group(2)
        try:
            wanted = int(number)
        except (TypeError, ValueError):
            continue
        nearby = text[max(0, match.start() - 24):min(len(text), match.end() + 12)]
        is_artifact_count = bool(
            re.search(r"Markdown|成果物|文書|ファイル", nearby, re.I)
            and (unit == "点" or "ファイル" in nearby)
        )
        # 「1ファイル」の数詞は単位正規表現では「1本」として部分一致し得る。
        # この場合もファイル数として扱い、本文中の「本」を数える契約にしない。
        after_match = text[match.end():match.end() + 5]
        if unit == "本" and after_match.startswith("ファイル"):
            is_artifact_count = True
        if is_artifact_count:
            # 「案内文1ファイルと…合計2ファイル」では内訳を重複契約にせず、
            # 明示された合計だけを成果物数として採用する。
            total_context = text[max(0, match.start() - 8):match.start()]
            if "合計" not in total_context and re.search(
                    r"合計\s*\d+\s*(?:点|件|個|本|ファイル)", text[match.end():]):
                continue
            per_output = bool(re.search(r"各\s*$", text[max(0, match.start() - 3):match.start()]))
            add(
                first_path,
                f"成果物の点数: {'各' if per_output else '合計'}{number}{unit}",
                "artifact_count",
                True,
                {"count": wanted, "unit": unit, "per_output": per_output,
                 "distinct_categories": per_output},
            )
            continue
        markers = _markers_for_unit(unit)
        add(
            first_path,
            f"数量要求: {number}{unit}を満たす",
            "count",
            True,
            {"count": wanted, "unit": unit, "markers": markers},
        )

    # 「各品を約80字」等は見出しごとの本文長を検査する。
    length_match = re.search(r"(\d+)\s*字(?:程度|前後|ほど)", text)
    if length_match and len(cleaned) >= 1:
        target_length = int(length_match.group(1))
        target_output = cleaned[0]
        section_headings = [str(h) for h in (target_output.get("required_headings") or [])
                            if str(h) and not any(g in str(h) for g in
                            ("根拠と未確認事項", "実施状態と次の行動", "概要", "注意事項"))]
        if "各" in text and section_headings:
            add(str(target_output.get("path") or first_path),
                f"各項目の説明文を{target_length}字程度にする",
                "section_char_count", True,
                {"target": target_length, "minimum": max(1, round(target_length * 0.75)),
                 "maximum": round(target_length * 1.25), "headings": section_headings})

    # 各段階の承認要否は、一括キーワードではなく段階ごとに確認する。
    if "各段階" in text and "承認" in text:
        stage_match = re.search(
            r"([一-鿿ぁ-んァ-ヶー]+(?:、[一-鿿ぁ-んァ-ヶー]+){2,})までの\d+段階", text
        )
        if stage_match:
            stages = [part.strip() for part in stage_match.group(1).split("、") if part.strip()]
            if len(stages) >= 3:
                add(first_path, "各段階の承認要否を個別に示す", "approval_stage_matrix", True,
                    {"stages": stages, "decision_terms": ["承認", "不要"]})

    # A1: 連絡先・終了後確認の要求節は列挙抽出で語句化しない。
    # 「架空の問い合わせ先を含め」「終了後確認を含める」等は後段の核語化・
    # 単独分解の実値/見出し照合へ寄せる。
    raw_enums = _extract_enumeration_terms(text)
    filtered_enums: list[str] = []
    for term in raw_enums:
        if _P1_CONTACT_WORD_RE.search(term):
            continue
        core_probe = _p0a_requirement_core(term)
        if core_probe and _p0a_heading_covers(
                _p0a_existing_headings(
                    [{"artifact_path": first_path,
                      "expected": {"headings": [
                          heading for output in cleaned
                          for heading in (output.get("required_headings") or [])]}}],
                    first_path),
                core_probe):
            continue
        filtered_enums.append(term)
    # 列挙された必須の語。
    for term in filtered_enums:
        add(
            target_for(term, cleaned, first_path),
            f"必須語「{term}」を含む",
            "phrase",
            True,
            {"keywords": [term]},
        )

    # 鉤括弧の語句と否定節を分類。同じ語が必須と禁止の両方に入ることは禁止。
    required_quotes, forbid_quotes, unquoted_negations, contradictions = _classify_quotes_and_negations(text)

    for term in forbid_quotes:
        if term in contradictions:
            continue
        if any(
            isinstance(row.get("expected"), dict)
            and (
                term in list((row.get("expected") or {}).get("keywords") or [])
                or term in list((row.get("expected") or {}).get("forbid") or [])
            )
            for row in items
        ):
            continue
        add(
            first_path,
            f"「{term}」の断定・記載を避ける",
            "phrase",
            True,
            {"keywords": [], "forbid": [term]},
        )

    for term in required_quotes:
        if term in contradictions:
            continue
        if any(
            isinstance(row.get("expected"), dict)
            and (
                term in list((row.get("expected") or {}).get("keywords") or [])
                or term in list((row.get("expected") or {}).get("forbid") or [])
            )
            for row in items
        ):
            continue
        add(
            first_path,
            f"必須語「{term}」を含む",
            "phrase",
            True,
            {"keywords": [term]},
        )

    # 否定・禁止の要求。鉤括弧が無い否定節は、直後に「Yと明記する」等の代替明記が続く
    # 「Xと断定せず『Y』と明記する」のような前置き節、および手段・状態の制限(自動投稿せず 等)を除き、
    # 文意の必須項目(llm_semantic)にする。
    for neg_clause in unquoted_negations:
        is_contrast_prefix = bool(
            re.search(r"断定せず$", neg_clause)
            and required_quotes
            and re.search(re.escape(neg_clause) + r"\s*[、，,]?\s*[「『]", text)
        )
        if is_contrast_prefix:
            continue
        if re.search(r"自動.{0,6}せず", neg_clause):
            continue
        sentence = neg_clause[:120]
        add(
            first_path,
            f"否定要求の適合: {sentence}",
            "llm_semantic",
            True,
            {"keywords": []},
        )

    # 仮・架空・要確認・想定の明記。禁止語に入っている語や矛盾語は必須に追加しない。
    for word in _MARK_WORDS:
        if word in text and word not in contradictions:
            if any(
                isinstance(row.get("expected"), dict)
                and (
                    word in list((row.get("expected") or {}).get("keywords") or [])
                    or word in list((row.get("expected") or {}).get("forbid") or [])
                )
                for row in items
            ):
                continue
            for target_output in cleaned:
                add(
                    str(target_output.get("path") or first_path),
                    f"「{word}」の明記を確認する",
                    "phrase",
                    True,
                    {"keywords": [word]},
                )

    # 見出し: 契約の required_headings は headings 項目(成果物ごと)。
    for output in cleaned:
        path = str(output.get("path") or "")
        headings = [str(h) for h in (output.get("required_headings") or []) if str(h).strip()]
        if not headings:
            continue
        add(
            path,
            f"{path}に必須見出しを含む",
            "headings",
            True,
            {"headings": headings},
        )

    # ローカルLLMの補助抽出は任意。根拠(引用)が達成条件文にある場合だけ採用する。
    for suggestion in grounded_llm_items(text, llm_suggestions or []):
        if not isinstance(suggestion, dict):
            continue
        method = str(suggestion.get("method") or "")
        requirement = str(suggestion.get("requirement") or "")[:200]
        expected = suggestion.get("expected") if isinstance(suggestion.get("expected"), dict) else {}
        if method not in METHODS or not requirement:
            continue
        target = str(suggestion.get("artifact_path") or first_path)
        if target not in [str(o.get("path") or "") for o in cleaned]:
            target = first_path
        add(target, requirement, method, bool(suggestion.get("required", True)), expected)

    # 各完成文書について、形式・語句だけでなく内容の適合も独立に照合する。
    # 複数成果物の場合はその資料の見出しで担当範囲を限定し、
    # 他の資料が担う要件をこの資料単独に要求しない。
    if require_semantic:
        clauses = [part.strip() for part in re.split(r"(?<=。)", text) if part.strip()]

        def clause_score(clause: str, output: dict) -> int:
            score = 0
            headings = [re.sub(r"^[0-9０-９]+[.．、)）:]\s*", "", str(h))
                        for h in (output.get("required_headings") or [])]
            for heading in headings:
                if heading in {"根拠と未確認事項", "実施状態と次の行動"}:
                    continue
                if len(heading) >= 2 and heading in clause:
                    score += 2
            category = str(output.get("artifact_category") or "")
            hints = {
                "checklist": ("チェックリスト",),
                "procedure_document": ("手順", "手順書", "マニュアル"),
                "faq_document": ("FAQ", "質問", "回答"),
                "document": ("案内文", "商品説明文", "説明", "魅力", "メニュー"),
            }.get(category, ())
            if any(hint in clause for hint in hints):
                score += 1
            return score

        for output in cleaned:
            path = str(output.get("path") or "")
            headings = [str(h) for h in (output.get("required_headings") or []) if str(h).strip()]
            role = "、".join(headings[:8]) if headings else "指定された成果物"
            assigned = []
            for clause in clauses:
                scores = [clause_score(clause, other) for other in cleaned]
                current = clause_score(clause, output)
                if len(cleaned) == 1 or not any(scores) or (current > 0 and current == max(scores)):
                    assigned.append(clause)
            relevant = "".join(assigned) or text
            add(
                path,
                f"この成果物の担当範囲（{role}）について次の条件を具体的に満たす: {relevant[:400]}",
                "llm_semantic",
                True,
                {"keywords": [], "criterion_semantic": True},
            )

    # 安定順序(PYTHONHASHSEEDに依存しない)。
    items.sort(key=lambda r: (r["criterion_id"], r["item_id"], r["artifact_path"]))
    # P0-A 正規化: 要求文と検査対象の分離(具体値化)・対象パスの照合。規則が LLM の
    # 指定を上書きする。渡さない場合は従来どおり(旧呼び出しの互換を維持)。
    report: dict = {}
    if registered_text is not None:
        cleaned_outputs = _md_outputs(outputs)
        # P1-1: 日時役割の要求節そのものを列挙抽出が語句化した項目は除く。
        items = [row for row in items if not (
            str(row.get("method") or "") == "phrase"
            and any(label in str(keyword)
                    for keyword in ((row.get("expected") or {}).get("keywords") or [])
                    for label in P1_DATETIME_ROLE_LABELS.values())
            and any(marker in str(keyword)
                    for keyword in ((row.get("expected") or {}).get("keywords") or [])
                    for marker in ("含め", "記載", "明記", "掲載", "説明"))
        )]
        # P1-1: 単独の『Xを含め/記載し』型の要求節は検証可能な条件に分解する。
        # 要求節自体を丸ごと必須フレーズにしない。問い合わせ先は原本の連絡先値、
        # 『〜確認』は必須見出しに分解する。既存の列挙抽出は残し、重複は作らない。
        # A1: 連絡先の単独要求は resolve_contact_values の実値で検証する。
        # 実値が無いときは推測せず input_missing にする(URLを発明しない)。
        singles = _p1_single_include_items(text, items, cleaned_outputs,
                                           first_path, registered_text)
        for entry in singles:
            method = str(entry.get("method") or "phrase")
            expected = dict(entry.get("expected") or {})
            decomposed = str(expected.get("decomposed_from") or "")
            artifact = str(entry.get("artifact_path") or first_path)
            if (method == "phrase" and decomposed and _P1_CONTACT_WORD_RE.search(decomposed)
                    and registered_text is not None):
                selected = _contact_request_selected()
                mode, usable, effective, _note = _contact_registry_state_for_build(
                    str(criterion or ""))
                narrowed = list(usable)
                if selected:
                    narrowed = [entry for entry in usable
                                if str(entry.get("kind") or "") in selected]
                if mode == "registry" and narrowed:
                    expected, values = _contact_build_expected(
                        narrowed, effective=effective, selected=selected)
                    entry = dict(entry)
                    entry["requirement"] = (
                        f"必須の問い合わせ先「{'、'.join(values)}」のいずれかを含む")
                    entry["expected"] = {"keywords": values, "match": "any",
                                         "cored_from": decomposed,
                                         "decomposed_from": decomposed,
                                         **expected}
                elif mode in ("registry", "no_usable", "unreadable"):
                    reason, needed, requirement = _contact_input_missing_reason(
                        mode=mode if mode != "registry" else "no_usable",
                        effective=effective, selected=selected,
                        criterion=str(criterion or ""))
                    entry = dict(entry)
                    entry["method"] = "input_missing"
                    entry["requirement"] = requirement
                    entry["expected"] = _p0a_enrich_expected(
                        {"concept": "contact",
                         "reason": reason,
                         "needed_inputs": [needed],
                         "cored_from": decomposed, "decomposed_from": decomposed,
                         "contact_purpose": effective},
                        semantic_role="連絡先",
                        target_artifact=artifact,
                        accepted_forms=[],
                        match_policy="any",
                        source_fact_id="contact",
                        source_hash=_p0a_fact_hash(registered_text),
                    )
                else:
                    contacts = resolve_contact_values(
                        _contact_request_project_id(), registered_text)
                    values = [entry.get("value") for entry in contacts if entry.get("value")]
                    source_hash = _p0a_fact_hash(registered_text)
                    if values:
                        entry = dict(entry)
                        entry["requirement"] = (
                            f"必須の問い合わせ先「{'、'.join(values)}」のいずれかを含む")
                        entry["expected"] = _p0a_enrich_expected(
                            {"keywords": values, "match": "any",
                             "cored_from": decomposed, "decomposed_from": decomposed,
                             "source_kind": "registered_context"},
                            semantic_role="連絡先",
                            target_artifact=artifact,
                            accepted_forms=values,
                            match_policy="any",
                            source_fact_id="contact",
                            source_hash=source_hash,
                        )
                    else:
                        entry = dict(entry)
                        entry["method"] = "input_missing"
                        entry["requirement"] = "問い合わせ先の具体値が原本にないため判定不能"
                        entry["expected"] = _p0a_enrich_expected(
                            {"concept": "contact",
                             "reason": "問い合わせ先(メール/電話)の具体値が登録事実に明示されていないため検査契約を作れない",
                             "needed_inputs": ["問い合わせ先(メール/電話)の具体値を含む登録事実の追加"],
                             "cored_from": decomposed, "decomposed_from": decomposed},
                            semantic_role="連絡先",
                            target_artifact=artifact,
                            accepted_forms=[],
                            match_policy="any",
                            source_fact_id="contact",
                            source_hash=source_hash,
                        )
            add(str(entry.get("artifact_path") or first_path),
                str(entry.get("requirement") or ""),
                str(entry.get("method") or "phrase"),
                bool(entry.get("required", True)),
                dict(entry.get("expected") or {}))
        items.sort(key=lambda r: (r["criterion_id"], r["item_id"], r["artifact_path"]))
        items, report = normalize_p0a_items(criterion_id, criterion, items,
                                            cleaned_outputs, registered_text)
        # A1: 実際の項目生成経路に核語化を配線する。
        # normalize_p0a_items の直後、ID採番の直前に _p0a_core_items を通す。
        # P0-B回帰のため、核語化で変化する行だけを置換し、変化しない行の
        # 順序・ID割当を変えない。何も変化しなければそのまま返す。
        cored, _core_report = _p0a_core_items(
            str(criterion_id or ""), str(criterion or ""), items,
            cleaned_outputs, str(registered_text or ""))
        if len(cored) == len(items):
            merged: list[dict] = []
            for before, after in zip(items, cored):
                if not isinstance(before, dict) or not isinstance(after, dict):
                    merged.append(after)
                    continue
                if str(before.get("item_id") or "") != str(after.get("item_id") or ""):
                    merged.append(before)
                    continue
                merged.append(after)
            items = merged
        else:
            # 行数の差は重複除去によることがある。既存項目を失わず、
            # 命令文が必須語句として残る行だけ核へ置き換える。
            repaired = []
            for before in items:
                row = dict(before)
                expected = dict(row.get("expected") or {})
                keywords = [str(k) for k in (expected.get("keywords") or []) if str(k)]
                if row.get("method") == "phrase" and len(keywords) == 1:
                    original = keywords[0]
                    core = _p0a_requirement_core(original)
                    if (core and core != original
                            and not _p0a_core_is_contact(core, original)):
                        expected["keywords"] = [core]
                        expected["cored_from"] = original
                        row["expected"] = expected
                        row["requirement"] = f"必須語「{core}」を含む"
                repaired.append(row)
            items = repaired
        items.sort(key=lambda r: (str(r.get("criterion_id") or ""),
                                  str(r.get("item_id") or ""),
                                  str(r.get("artifact_path") or "")))
    # C0: 単一の出口で全項目を通して item_id を一意にする。
    # 既に一意で既存台帳・修復が参照しているIDは変えない(安定性)。
    # 重複は後から来た側だけを決定的な次の空き番号に付け替える。
    try:
        items = ensure_unique_item_ids(str(criterion_id or ""), items)
    except (TypeError, ValueError, AttributeError):
        raise
    except Exception as exc:
        raise ValueError(
            f"item id uniquify failed: {type(exc).__name__}") from exc
    # IDを採番し直して安定化する。
    renumbered: list[dict] = []
    for position, row in enumerate(items, 1):
        fixed = dict(row)
        # C0: 既存の一意なIDは変えない。IDが無い行だけ連番を振る。
        if not str(fixed.get("item_id") or "").strip():
            fixed["item_id"] = f"{criterion_id}-R{position:02d}"
        renumbered.append(fixed)
    assert_unique_item_ids(renumbered, where="extract_requirement_items")
    return renumbered


def grounded_llm_items(criterion_text: str, suggestions: list[dict]) -> list[dict]:
    """LLMが挙げた項目のうち、達成条件文に根拠(引用)がある場合だけ採用する。"""
    text = _norm(criterion_text)
    adopted: list[dict] = []
    for suggestion in suggestions or []:
        if not isinstance(suggestion, dict):
            continue
        quote = str(suggestion.get("quote") or suggestion.get("source_quote") or "").strip()
        requirement = str(suggestion.get("requirement") or "")
        # 根拠が無いものは捨てる(原本や目標の代理にしない)。
        if not quote or quote not in text:
            continue
        # 引用が要求文と無関係な場合も捨てる(短い引用の偶然一致を避ける)。
        if len(quote) < 2:
            continue
        if requirement and quote not in requirement and quote not in text:
            continue
        adopted.append(suggestion)
    return adopted


def detect_extraction_doubts(criterion: str, items: list[dict]) -> list[str]:
    """数量・鉤括弧・列挙語・否定要求・矛盾の機械照合で抽出漏れの疑義を出す。疑義は人待ちにする。"""
    text = _norm(criterion)
    doubts: list[str] = []
    if not items:
        # 項目がゼロなら数量の有無によらず疑義にする。
        doubts.append("抽出の疑義: 項目未抽出")
        return sorted(set(doubts))
    rows = [row for row in (items or []) if isinstance(row, dict)]

    # 同じ語が必須(keywords)と禁止(forbid)の両方に入っていないか検査する。
    all_keywords: set[str] = set()
    all_forbids: set[str] = set()
    for row in rows:
        expected = row.get("expected") if isinstance(row.get("expected"), dict) else {}
        for k in expected.get("keywords") or []:
            if str(k):
                all_keywords.add(str(k))
        for f in expected.get("forbid") or []:
            if str(f):
                all_forbids.add(str(f))
    for conflict in sorted(all_keywords & all_forbids):
        doubts.append(f"矛盾する要求の疑義: {conflict}")

    required_quotes, forbid_quotes, unquoted_negations, contradictions = _classify_quotes_and_negations(text)
    for conflict in contradictions:
        doubts.append(f"矛盾する要求の疑義: {conflict}")

    def _covered_term(term: str) -> bool:
        core_forms = {term}
        requirement_core = _p0a_requirement_core(term)
        if requirement_core and requirement_core != term:
            core_forms.add(requirement_core)
        core = _p0a_core_term(term)
        if core != term:
            core_forms.add(core)
        for row in rows:
            requirement = str(row.get("requirement") or "")
            expected = row.get("expected") if isinstance(row.get("expected"), dict) else {}
            # 全体文意の確認項目は抽出済みの語句・列挙を証明しない。
            if expected.get("criterion_semantic") is True:
                continue
            keywords = [str(k) for k in (expected.get("keywords") or []) if str(k)]
            forbid = [str(k) for k in (expected.get("forbid") or []) if str(k)]
            headings = [str(k) for k in (expected.get("headings") or []) if str(k)]
            # P0-A 正規化後: 概念→具体値の付け替え(grounded_concept)や
            # 残りかす除去(cored_from)、P1-1の要求節分解(decomposed_from)は
            # 抽出の証拠として扱う。見出し項目も証拠にする。
            grounded_concept = str((expected or {}).get("grounded_concept") or "")
            cored_from = str((expected or {}).get("cored_from") or "")
            decomposed = str((expected or {}).get("decomposed_from") or "")
            if term and (term in requirement or term in keywords or term in forbid
                         or term in headings
                         or (grounded_concept and grounded_concept in core_forms)
                         or (decomposed and (term == decomposed or decomposed in term
                                              or term in decomposed))
                         or (cored_from and any(t == cored_from or cored_from in t for t in core_forms))):
                return True
            if any(t and (t in requirement or t in keywords or t in forbid or t in headings) for t in core_forms if t != term):
                return True
        # P0-A の input_missing: 具体値が原本に無い概念は「疑義」にしない。
        # 判定不能を記録済みであり、抽出漏れではない。
        for row in rows:
            if not isinstance(row, dict) or str(row.get("method") or "") != "input_missing":
                continue
            expected = row.get("expected") if isinstance(row.get("expected"), dict) else {}
            concept = str((expected or {}).get("concept") or "")
            if concept and term and (term == concept or term in concept or concept in term):
                return True
            if concept == "日時" and term in ("日時", "日付", "時刻"):
                return True
        return False

    # (a) 数量表現のうち、同じ数値の count 項目に結び付かないものは疑義。
    for match in _COUNT_UNIT_RE.finditer(text):
        number, unit = match.group(1), match.group(2)
        try:
            wanted = int(number)
        except (TypeError, ValueError):
            doubts.append(f"数量の抽出漏れの疑義: {number}{unit}")
            continue
        covered = False
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("method") or "") not in {"count", "artifact_count"}:
                continue
            expected = row.get("expected") if isinstance(row.get("expected"), dict) else {}
            try:
                if int(expected.get("count")) == wanted:
                    covered = True
                    break
            except (TypeError, ValueError):
                continue
        if not covered:
            doubts.append(f"数量の抽出漏れの疑義: {number}{unit}")

    # (b) 鉤括弧の語句のうち、どの項目にも結び付かないものは疑義。
    for term in _extract_quotes(text):
        if not _covered_term(term):
            doubts.append(f"語句の抽出漏れの疑義: {term}")

    # (c) 列挙語のうち、どの項目にも結び付かないものは疑義。
    for term in _extract_enumeration_terms(text):
        if not _covered_term(term):
            doubts.append(f"語句の抽出漏れの疑義: {term}")

    # 要求の動詞が無い読点の連なりは、項目にできない列挙らしき表現として疑義にする。
    for unanchored in _extract_unanchored_comma_enumerations(text):
        terms = _split_enumeration(unanchored)
        if not all(_covered_term(_strip_leading_qualifier(t) if i == 0 else t) for i, t in enumerate(terms)):
            doubts.append(f"列挙の抽出漏れの疑義: {unanchored}")

    # (d) 否定の要求があるのに、どの項目(forbid または llm_semantic または対比の必須明記)にも結び付かないものは疑義。
    for neg_clause in unquoted_negations:
        is_contrast_prefix = bool(
            re.search(r"断定せず$", neg_clause)
            and required_quotes
            and re.search(re.escape(neg_clause) + r"\s*[、，,]?\s*[「『]", text)
            and all(_covered_term(q) for q in required_quotes)
        )
        if is_contrast_prefix:
            continue
        if re.search(r"自動.{0,6}せず", neg_clause):
            continue
        neg_covered = any(
            (
                str(row.get("method") or "") == "llm_semantic"
                and (neg_clause in str(row.get("requirement") or "") or "否定要求" in str(row.get("requirement") or ""))
            )
            or bool(((row.get("expected") or {}) if isinstance(row.get("expected"), dict) else {}).get("forbid"))
            for row in rows
        )
        if not neg_covered:
            doubts.append(f"否定要求の抽出漏れの疑義: {neg_clause[:60]}")

    for term in forbid_quotes:
        forbid_covered = any(
            term in list(((row.get("expected") or {}) if isinstance(row.get("expected"), dict) else {}).get("forbid") or [])
            for row in rows
        )
        if not forbid_covered:
            doubts.append(f"否定要求の抽出漏れの疑義: {term}")

    # 明記要求の語のうち、どの項目にも結び付かないものは疑義。
    for word in _MARK_WORDS:
        if word in text and not _covered_term(word):
            doubts.append(f"語句の抽出漏れの疑義: {word}")

    return sorted(set(doubts))


def validate_requirement_matrix(plan: dict) -> dict:
    """構造検証: 未抽出/対象不明/方法未定義/項目ID重複はneeds_review、片方ゼロは不合格。"""
    issues: list[str] = []
    needs_review = False
    tasks = plan.get("tasks") or []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        try:
            import json as _json

            contract = _json.loads(task.get("acceptance_criteria", ""))
        except (TypeError, ValueError):
            continue
        if not isinstance(contract, dict):
            continue
        if contract.get("final_verification"):
            continue
        # 準備・評価タスクは実行SCではないため対象外。
        # 車両ワークフロー等の専用検証契約は対象外(既存の専用検証を維持する)。
        criterion_ids = list(contract.get("criterion_ids") or [])
        if not criterion_ids:
            continue
        if str(contract.get("execution_kind") or "").startswith("vehicle_"):
            continue
        matrix = contract.get("requirement_matrix")
        outputs = _md_outputs(contract.get("outputs") or [])
        output_paths = sorted(str(o.get("path") or "") for o in outputs)
        criterion_text = str(contract.get("criterion") or "")
        if not isinstance(matrix, list) or not matrix:
            issues.append(f"{task.get('task_key')}: requirement items missing")
            needs_review = True
            continue
        # C0: 保存前検査でも item_id の重複は不合格にする(黙って通さない)。
        seen_ids: set[str] = set()
        for row in matrix:
            if not isinstance(row, dict):
                continue
            text = str(row.get("item_id") or "").strip()
            if not text:
                continue
            if text in seen_ids:
                issues.append(f"{task.get('task_key')}: duplicate item id {text}")
                needs_review = True
            else:
                seen_ids.add(text)
        counts: dict[str, int] = {path: 0 for path in output_paths}
        for row in matrix:
            if isinstance(row, dict) and row.get("method") == "artifact_count":
                expected = row.get("expected") if isinstance(row.get("expected"), dict) else {}
                try:
                    wanted = int(expected.get("count"))
                except (TypeError, ValueError):
                    wanted = -1
                if wanted < 1 or (not expected.get("per_output") and wanted != len(output_paths)):
                    issues.append(f"{task.get('task_key')}: artifact count does not match output contracts")
                    needs_review = True
            if not isinstance(row, dict):
                issues.append(f"{task.get('task_key')}: requirement item invalid")
                needs_review = True
                continue
            path = str(row.get("artifact_path") or "")
            method = str(row.get("method") or "")
            requirement = str(row.get("requirement") or "")
            if not path or path not in counts:
                issues.append(f"{task.get('task_key')}: requirement target unknown")
                needs_review = True
                continue
            if method not in METHODS:
                issues.append(f"{task.get('task_key')}: requirement method undefined")
                needs_review = True
                continue
            if method == "input_missing":
                # 判定不能の記録。合格にも不合格にも倒さず、理由と必要な入力を必須にする。
                # input_missing 自体は既存の拒否理由を隠さない(下流の順序検査で先に拒否される)。
                expected = row.get("expected") if isinstance(row.get("expected"), dict) else {}
                if not str((expected or {}).get("reason") or ""):
                    issues.append(f"{task.get('task_key')}: requirement empty")
                needs_review = True
                counts[path] += 1
                continue
            if not requirement:
                issues.append(f"{task.get('task_key')}: requirement empty")
                needs_review = True
                continue
            # P0-A 保存前検査: 既存の具体的拒否(unknown/undefined/empty/zero/doubts)を
            # 先に検査してから P0-A の新規検査を行う(新規検査で既存理由を隠さない)。
            p0a_issues = p0a_contract_issues(criterion_text, [row], outputs)
            for p0a in p0a_issues:
                issues.append(f"{task.get('task_key')}: {p0a}")
            counts[path] += 1
        # 二つの成果物のうち片方の項目がゼロなら不合格(needs_reviewではなくFAIL)。
        if len(output_paths) > 1:
            for path in output_paths:
                if counts.get(path, 0) <= 0:
                    issues.append(f"{task.get('task_key')}: artifact has zero items: {path}")
        # 抽出漏れのローカル照合。
        criterion_text = str(contract.get("criterion") or "")
        doubts = detect_extraction_doubts(criterion_text, matrix if isinstance(matrix, list) else [])
        for doubt in doubts:
            issues.append(f"{task.get('task_key')}: {doubt}")
            needs_review = True
    artifact_zero = [i for i in issues if "artifact has zero items" in i]
    passed = not issues
    return {
        "passed": passed,
        "needs_review": bool(needs_review or not passed and not artifact_zero) if issues else False,
        "needs_human_review": bool(needs_review),
        "artifact_zero_failed": bool(artifact_zero),
        "issues": issues,
    }
