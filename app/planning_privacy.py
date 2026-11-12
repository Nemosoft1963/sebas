"""G0-1: 外部AIへの送信前検査(拒否リスト方式・明らかな混入を止めるだけ).

G2-1で決定的(正規表現・文字列比較だけ。LLM・外部通信なし)な検査を追加した。
この検査は拒否リスト方式で、明らかな混入を止めるだけである。
通過は「安全」の意味ではない。言い換え・ホモグリフ・網羅的な氏名/住所の
検出はできない。境界は見逃しより止める側に倒している(誤検出の可能性を残す)。
各コードの限界:
- phone: 日本の代表的な形式だけ。国際番号の網羅・市外局番の完全検証はしない。
- postal_code: 〒付きか住所語近傍だけ。単なる7桁は止めない。
- address: 都道府県+市区町村+番地の並びだけ。地名の網羅・住居表示の完全検証はしない。
- person_name: ラベル付き・敬称付きだけ。網羅的な氏名検出はしない。許可リストで一般語を除外。
- bank_account: 近傍の数字・IBAN形だけ。支店名単独・口座名義の判定はしない。
- amount_detail: 通貨記号・単位付きで4桁以上か万円・億円だけ。 bareな数値・年・日付・件数は止めない。
- long_number: 区切り除去で10桁以上だけ。64桁16進(hash_like)とは二重に数えない。
- encoded_blob: 長いbase64・%連続・\\u/\\x連続・数値文字参照連続だけ。短い断片・難読化の網羅はしない。
- source_quote: 24文字シングルの完全一致だけ。言い換え・要約・24文字未満は止めない。
"""
from __future__ import annotations

import re
import unicodedata

CODES = (
    "secret",
    "email",
    "abs_path",
    "url_query",
    "hash_like",
    "blocked_term",
    "unusable_input",
    "check_error",
    "phone",
    "postal_code",
    "address",
    "person_name",
    "bank_account",
    "amount_detail",
    "long_number",
    "encoded_blob",
    "source_quote",
)

_EXPECTED_ERRORS = (ValueError, KeyError, TypeError, AttributeError, UnicodeError, re.error)

_CONDITION_TOKEN_RE = re.compile(r"^(?:SC\d{1,3}|C\d{1,3}|S\d{1,3}|STEP\d{1,3}|TASK[_-]?\d+)$", re.I)
_EMAIL_RE = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", re.I)
_UNC_RE = re.compile(r"\\\\[^\\/\s]+\\[^\\/\s]+")
_DRIVE_RE = re.compile(r"(?<![a-z0-9_])[a-z]:[\\/][^\s]*", re.I)
_HOST_ABS_RE = re.compile(r"(?<![\w])/(?:home|mnt|etc|var|usr|opt|root|data|srv|tmp)(?=/|$|[^a-z0-9])", re.I)
_URL_QUERY_RE = re.compile(r"https?://[^\s?#]+\?[^\s]+", re.I)
_HASH64_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")
_UUID_RE = re.compile(
    r"(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])"
)
_UUID_LOOSE_RE = re.compile(
    r"[0-9a-f]{8}\s*[-_\s]*[0-9a-f]{4}\s*[-_\s]*[0-9a-f]{4}\s*[-_\s]*"
    r"[0-9a-f]{4}\s*[-_\s]*[0-9a-f]{12}",
    re.I,
)

_PREFS = (
    "北海道|青森県|岩手県|宮城県|秋田県|山形県|福島県|茨城県|栃木県|群馬県|"
    "埼玉県|千葉県|東京都|神奈川県|新潟県|富山県|石川県|福井県|山梨県|長野県|"
    "岐阜県|静岡県|愛知県|三重県|滋賀県|京都府|大阪府|兵庫県|奈良県|和歌山県|"
    "鳥取県|島根県|岡山県|広島県|山口県|徳島県|香川県|愛媛県|高知県|福岡県|"
    "佐賀県|長崎県|熊本県|大分県|宮崎県|鹿児島県|沖縄県"
)
_PHONE_FORMATTED_RE = re.compile(r"(?<!\d)0\d{1,4}[\s\-\(\)（）]+?\d{1,4}[\s\-\(\)（）]+?\d{3,4}(?!\d)")
_PHONE_PLUS81_RE = re.compile(r"\+81[\s\-\(\)（）]*\d[\d\s\-\(\)（）]{6,14}")
_PHONE_COMPACT_MOBILE_RE = re.compile(r"(?<![0-9a-z])0[789]0\d{7,8}(?![0-9a-z])")
_PHONE_COMPACT_GENERIC_RE = re.compile(r"(?<![0-9a-z])0\d{9,10}(?![0-9a-z])")

_POSTAL_WITH_MARK_RE = re.compile(r"〒\s*\d{3}\s*\-?\s*\d{4}")
_POSTAL_CANDIDATE_RE = re.compile(r"(?<!\d)\d{3}\s*\-?\s*\d{4}(?!\d)")
_POSTAL_COMPACT_RE = re.compile(r"(?<!\d)\d{7}(?!\d)")
_ADDRESS_INDICATOR_RE = re.compile(
    r"北海道|青森県|岩手県|宮城県|秋田県|山形県|福島県|茨城県|栃木県|群馬県|"
    r"埼玉県|千葉県|東京都|神奈川県|新潟県|富山県|石川県|福井県|山梨県|長野県|"
    r"岐阜県|静岡県|愛知県|三重県|滋賀県|京都府|大阪府|兵庫県|奈良県|和歌山県|"
    r"鳥取県|島根県|岡山県|広島県|山口県|徳島県|香川県|愛媛県|高知県|福岡県|"
    r"佐賀県|長崎県|熊本県|大分県|宮崎県|鹿児島県|沖縄県|"
    r"都|道|府|県|市|区|町|村|郡|丁目"
)
_ADDRESS_RE = re.compile(
    r"(?:" + _PREFS + r")[^\n]{0,12}?(?:市|区|町|村|郡)[^\n]{0,12}?"
    r"(?:\d+\s*丁目|\d+\s*-\s*\d+(?:\s*-\s*\d+)?)"
)

_PERSON_LABEL_RE = re.compile(r"(?:氏名|名前|担当者|代表者|顧客名|取引先担当|申込者)\s*[:：]\s*[^\s:：、,。]{2,10}")
_PERSON_LABEL_COMPACT_RE = re.compile(
    r"(?:氏名|名前|担当者|代表者|顧客名|取引先担当|申込者)"
    r"[a-z\u3041-\u3096\u30a1-\u30fa\u4e00-\u9fff]{2,10}"
)
_HONORIFIC_RE = re.compile(
    r"([\u3041-\u3096\u30a1-\u30fa\u30fc\u4e00-\u9fff]{1,6})\s?(?:先生|様|さん|殿|氏)"
)
# 敬称直前の名前部分は漢字または全角カタカナ(長音含む)を含むこと(ひらがなだけは対象外)
_NAME_HAS_KANJI_OR_KATAKANA_RE = re.compile(r"[\u4e00-\u9fff\u30a1-\u30fa\u30fc]")
# 敬称直後の1文字の扱い:助詞・句読点・空白・文末は一致、語を続ける文字は不一致
_HONORIFIC_SINGLE_OK_AFTER = frozenset("はがのにをへとも")
_HONORIFIC_PREFIX_OK_AFTER = ("から", "より", "です")
_HONORIFIC_WORD_CONTINUE_RE = re.compile(r"[\u3041-\u3096\u30a1-\u30fa\u4e00-\u9fff\u30fc]")
_PERSON_ALLOW = frozenset({
    "お客様", "各位", "皆様", "担当者様", "利用者様", "ユーザー様",
    "ユーザ様", "お世話", "お世話様",
    "同氏", "各氏", "諸氏", "氏名",
})

_BANK_KEYWORD_RE = re.compile(r"口座番号|口座|普通|当座")
_BANK_ORG_RE = re.compile(r"銀行|支店|信用金庫")
_BANK_NUM_RE = re.compile(r"(?<!\d)\d{5,8}(?!\d)")
_IBAN_RE = re.compile(r"(?<![a-z0-9])[a-z]{2}\d{2}[a-z0-9]{10,30}(?![a-z0-9])")

_AMOUNT_SYMBOL_RE = re.compile(r"[¥$]\s*\d[\d\s,，．\.]*\d")
_AMOUNT_UNIT_RE = re.compile(r"\d[\d\s,，]*\s*(?:万円|億円|円|yen|usd)", re.I)

_LONG_SEP_RE = re.compile(r"[\s\-‐‑‒–—―,，、]+")
_LONG_RUN_RE = re.compile(r"(?<![0-9a-z])\d{10,}(?![0-9a-z])")
_LONG_T_RE = re.compile(r"(?<![0-9a-z])t\d{13}(?![0-9a-z])")

_B64_RUN_RE = re.compile(r"(?<![0-9a-z+/=])[0-9a-z+/=]{48,}(?![0-9a-z+/=])")
_PCT_RE = re.compile(r"(?:%[0-9a-f]{2}){8,}")
_ESC_RE = re.compile(r"(?:\\(?:u[0-9a-f]{4}|x[0-9a-f]{2})){6,}")
_ENT_DEC_RE = re.compile(r"(?:&#\d+;){6,}")
_ENT_HEX_RE = re.compile(r"(?:&#x[0-9a-f]+;){6,}")

_SOURCE_WINDOW = 24
_SOURCE_LIMIT = 200_000


def _normalize(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).lower()
    return "".join(
        ch for ch in normalized
        if unicodedata.category(ch) not in ("Cc", "Cf")
    )


def _compact_symbols(text: str) -> str:
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE)


def _is_condition_or_step_token(term: str) -> bool:
    return bool(_CONDITION_TOKEN_RE.fullmatch(term.strip()))


def _phone_hits(s1: str, s_compact: str) -> int:
    hits = 0
    for match in _PHONE_FORMATTED_RE.finditer(s1):
        digits = re.sub(r"\D", "", match.group(0))
        if digits.startswith("0") and 10 <= len(digits) <= 11:
            hits += 1
    for match in _PHONE_PLUS81_RE.finditer(s1):
        digits = re.sub(r"\D", "", match.group(0))
        # +81 の後に国内番号9〜11桁(先頭0の有無は問わない)
        tail = digits[2:] if digits.startswith("81") else digits
        if 9 <= len(tail) <= 11:
            hits += 1
    # 記号・空白で分割した形は compact で連続数字になる
    for match in _PHONE_COMPACT_MOBILE_RE.finditer(s_compact):
        hits += 1
    if hits == 0:
        for match in _PHONE_COMPACT_GENERIC_RE.finditer(s_compact):
            # mobile は上で数えたので重複を避ける
            token = match.group(0)
            if re.fullmatch(r"0[789]0\d{7,8}", token):
                continue
            hits += 1
    return hits


def _postal_hits(s1: str, s_compact: str) -> int:
    hits = 0
    marked_spans: list[tuple[int, int]] = []
    for match in _POSTAL_WITH_MARK_RE.finditer(s1):
        hits += 1
        marked_spans.append(match.span())
    for match in _POSTAL_CANDIDATE_RE.finditer(s1):
        start, end = match.span()
        if any(start < m_end and end > m_start for m_start, m_end in marked_spans):
            continue
        # 直前に〒があるものは上で数えた
        if "〒" in s1[max(0, start - 4):start]:
            continue
        window = s1[max(0, start - 40):end + 40]
        if _ADDRESS_INDICATOR_RE.search(window):
            hits += 1
    # 分割形(1 2 3-4 5 6 7)は compact で7桁になる
    for match in _POSTAL_COMPACT_RE.finditer(s_compact):
        window = s_compact[max(0, match.start() - 40):match.end() + 40]
        if _ADDRESS_INDICATOR_RE.search(window):
            # s1 側ですでに数えたものと二重に数えない
            token = match.group(0)
            if re.search(r"〒\s*" + token[:3] + r"\s*\-?\s*" + token[3:], s1):
                continue
            hits += 1
            break
    return hits


def _address_hits(s1: str, s_compact: str) -> int:
    hits = len(list(_ADDRESS_RE.finditer(s1)))
    if hits == 0:
        # compact は記号除去で番地のハイフンが消えるため数字だけの並びで見る
        compact_re = re.compile(
            r"(?:" + _PREFS + r").{0,12}?(?:市|区|町|村|郡).{0,12}?"
            r"(?:\d+丁目|\d{2,8})"
        )
        # 長い入力でも線形に終わるよう量指定を小さく保つ
        found = compact_re.search(s_compact)
        if found:
            # s1 側の正規形で番地らしい数字があるときだけ数える
            if re.search(r"\d+\s*丁目|\d+\s*-\s*\d+", s1):
                hits = 1
    return hits


# 連結形ラベル(コロンなし)の直後が助詞なら人名ではない(正規表現自体は変えない)
_PERSON_LABEL_PARTICLE_RE = re.compile(
    r"^(?:氏名|名前|担当者|代表者|顧客名|取引先担当|申込者)"
    r"(?:は|が|の|に|を|へ|と|も|から|より|です)"
)


def _honorific_follow_ok(s1: str, end: int) -> bool:
    """敬称直後が助詞・句読点・空白・文末なら True、語を続ける文字なら False。"""
    tail = s1[end:]
    if tail == "":
        return True
    if tail.startswith(_HONORIFIC_PREFIX_OK_AFTER):
        return True
    head = tail[0]
    if head in _HONORIFIC_SINGLE_OK_AFTER:
        return True
    if _HONORIFIC_WORD_CONTINUE_RE.match(head):
        return False
    return True


def _person_name_hits(s1: str, s_compact: str) -> int:
    hits = 0
    for match in _PERSON_LABEL_RE.finditer(s1):
        hits += 1
    if _PERSON_LABEL_RE.search(s1) is None:
        for match in _PERSON_LABEL_COMPACT_RE.finditer(s_compact):
            token = match.group(0)
            if any(allow in token for allow in _PERSON_ALLOW):
                continue
            if _PERSON_LABEL_PARTICLE_RE.match(token):
                continue
            # ラベル直後の2〜10文字が名前らしいときだけ
            hits += 1
            break
    if hits > 0:
        return hits
    # 敬称付きは s1(正規化後・記号を除かない形)だけで見る。s_compact は使わない。
    for match in _HONORIFIC_RE.finditer(s1):
        token = match.group(0)
        if token in _PERSON_ALLOW:
            continue
        if any(allow in token for allow in _PERSON_ALLOW):
            continue
        # ひらがなだけの語は対象外(漢字または全角カタカナを含む1〜6文字だけ)
        if _NAME_HAS_KANJI_OR_KATAKANA_RE.search(match.group(1)) is None:
            continue
        # 敬称直後が語を続ける文字(氏名の名・さんまのま等)なら対象外
        if not _honorific_follow_ok(s1, match.end()):
            continue
        hits += 1
        break
    return hits


def _bank_hits(s1: str) -> int:
    hits = 0
    lines = s1.split("\n")
    compact_lines = [_compact_symbols(line) for line in lines]
    for line, compact in zip(lines, compact_lines):
        found = False
        for keyword in (_BANK_KEYWORD_RE, _BANK_ORG_RE):
            for key in keyword.finditer(line):
                # 同じ行・30文字以内に5〜8桁
                start = max(0, key.start() - 30)
                end = key.end() + 30
                window = line[start:end]
                if _BANK_NUM_RE.search(window):
                    found = True
                    break
                c_start = max(0, key.start() - 30)
                c_end = key.end() + 30
                # 分割形は compact 行で見る(記号除去で行長が縮むため広めに見る)
                c_window = compact[max(0, c_start):c_end + 30]
                if _BANK_NUM_RE.search(c_window):
                    found = True
                    break
            if found:
                break
        if found:
            hits += 1
    if _IBAN_RE.search(s1) or _IBAN_RE.search(_compact_symbols(s1)):
        hits += 1
    return hits


def _amount_hits(s1: str) -> int:
    hits = 0
    for match in _AMOUNT_SYMBOL_RE.finditer(s1):
        digits = re.sub(r"\D", "", match.group(0))
        if len(digits) >= 4:
            hits += 1
    for match in _AMOUNT_UNIT_RE.finditer(s1):
        token = match.group(0)
        digits = re.sub(r"\D", "", token)
        if "万円" in token or "億円" in token:
            hits += 1
        elif len(digits) >= 4:
            hits += 1
    return hits


def _long_number_hits(s1: str) -> int:
    hits = 0
    if _LONG_T_RE.search(s1):
        hits += 1
    nosep = _LONG_SEP_RE.sub("", s1)
    for match in _LONG_RUN_RE.finditer(nosep):
        token = match.group(0)
        if len(token) == 64 and re.fullmatch(r"[0-9a-f]{64}", token):
            continue
        hits += 1
    return hits


def _encoded_blob_hits(s1: str) -> int:
    hits = 0
    for match in _B64_RUN_RE.finditer(s1):
        token = match.group(0)
        if len(set(token)) >= 10 and re.search(r"\d", token) and re.search(r"[a-z]", token):
            hits += 1
            break
    if _PCT_RE.search(s1):
        hits += 1
    if _ESC_RE.search(s1):
        hits += 1
    if _ENT_DEC_RE.search(s1) or _ENT_HEX_RE.search(s1):
        hits += 1
    return hits


def _source_quote_result(
    body_compact: str, source_texts: list[str],
) -> tuple[int, int]:
    truncated = 0
    shingles: set[str] = set()
    for raw in source_texts:
        text = raw[:_SOURCE_LIMIT] if len(raw) > _SOURCE_LIMIT else raw
        if len(raw) > _SOURCE_LIMIT:
            truncated += 1
        compact = _compact_symbols(_normalize(text))
        if len(compact) < _SOURCE_WINDOW:
            continue
        for index in range(len(compact) - _SOURCE_WINDOW + 1):
            shingles.add(compact[index:index + _SOURCE_WINDOW])
    if not shingles or len(body_compact) < _SOURCE_WINDOW:
        return 0, truncated
    count = 0
    for index in range(len(body_compact) - _SOURCE_WINDOW + 1):
        if body_compact[index:index + _SOURCE_WINDOW] in shingles:
            count += 1
    return count, truncated


def check_outbound_text(text, blocked_terms=(), source_texts=()) -> dict:
    """送信前検査。純粋関数(ストア書込・外部通信・LLM呼出なし).

    戻り値 {"ok": bool, "codes": [...], "details": {...}}。
    ok はコードが空のときだけ True。比較は NFKC・小文字化・
    制御文字/ゼロ幅文字/ソフトハイフン除去後の文字列と、
    記号・空白を除いた形の両方に対して行う。

    拒否リスト方式で明らかな混入を止めるだけ。通過は安全の意味ではない。
    言い換え・ホモグリフ・網羅的な氏名/住所の検出はできない。
    新しいコードの境界は見逃しより止める側に倒している。
    source_texts は文字列のリストであること。それ以外は check_error。
    """
    if not isinstance(text, str) or text == "" or text.strip() == "":
        return {"ok": False, "codes": ["unusable_input"], "details": {"unusable_input": True}}
    try:
        if not isinstance(source_texts, (list, tuple)):
            return {"ok": False, "codes": ["check_error"], "details": {"check_error": True}}
        for entry in source_texts:
            if not isinstance(entry, str):
                return {"ok": False, "codes": ["check_error"], "details": {"check_error": True}}
        s1 = _normalize(text)
        if s1.strip() == "":
            return {"ok": False, "codes": ["unusable_input"], "details": {"unusable_input": True}}
        s_compact = _compact_symbols(s1)
        s_email = re.sub(r"[\s*|·•‐‑‒–—―…/\\]+", "", s1)
        s_path = re.sub(r"\s+", "", s1)
        s_hash_nosep = re.sub(r"[\s\-_.*|/\\:;]+", "", s1)
        codes: list[str] = []
        details: dict = {}

        from app.plan_review_loop import contains_secret as _contains_secret

        secret_label = _contains_secret(s1) or _contains_secret(text)
        if secret_label:
            codes.append("secret")
            details["secret"] = True
            details["secret_label"] = str(secret_label)

        if _EMAIL_RE.search(s1) or _EMAIL_RE.search(s_email):
            codes.append("email")
            details["email"] = True

        if _UNC_RE.search(s1) or _UNC_RE.search(s_path):
            codes.append("abs_path")
            details["abs_path"] = True
        elif _DRIVE_RE.search(s1) or _DRIVE_RE.search(s_path):
            codes.append("abs_path")
            details["abs_path"] = True
        elif _HOST_ABS_RE.search(s1) or _HOST_ABS_RE.search(s_path):
            codes.append("abs_path")
            details["abs_path"] = True

        if _URL_QUERY_RE.search(s1) or _URL_QUERY_RE.search(s_path):
            codes.append("url_query")
            details["url_query"] = True

        if (_HASH64_RE.search(s1) or _HASH64_RE.search(s_hash_nosep)
                or _UUID_RE.search(s1) or _UUID_LOOSE_RE.search(s1)):
            codes.append("hash_like")
            details["hash_like"] = True

        terms: list[str] = list(blocked_terms or ())
        for raw_term in terms:
            term_norm = _normalize(raw_term)
            if len(term_norm.strip()) <= 2:
                continue
            if _is_condition_or_step_token(term_norm):
                continue
            term_compact = _compact_symbols(term_norm)
            if term_norm.strip() and term_norm.strip() in s1:
                codes.append("blocked_term")
                details["blocked_term"] = True
                break
            if len(term_compact) > 2 and term_compact in s_compact:
                codes.append("blocked_term")
                details["blocked_term"] = True
                break

        phone_hits = _phone_hits(s1, s_compact)
        if phone_hits:
            codes.append("phone")
            details["phone"] = True
            details["phone_count"] = int(phone_hits)

        postal_hits = _postal_hits(s1, s_compact)
        if postal_hits:
            codes.append("postal_code")
            details["postal_code"] = True
            details["postal_code_count"] = int(postal_hits)

        address_hits = _address_hits(s1, s_compact)
        if address_hits:
            codes.append("address")
            details["address"] = True
            details["address_count"] = int(address_hits)

        person_hits = _person_name_hits(s1, s_compact)
        if person_hits:
            codes.append("person_name")
            details["person_name"] = True
            details["person_name_count"] = int(person_hits)

        bank_hits = _bank_hits(s1)
        if bank_hits:
            codes.append("bank_account")
            details["bank_account"] = True
            details["bank_account_count"] = int(bank_hits)

        amount_hits = _amount_hits(s1)
        if amount_hits:
            codes.append("amount_detail")
            details["amount_detail"] = True
            details["amount_detail_count"] = int(amount_hits)

        long_hits = _long_number_hits(s1)
        if long_hits:
            codes.append("long_number")
            details["long_number"] = True
            details["long_number_count"] = int(long_hits)

        blob_hits = _encoded_blob_hits(s1)
        if blob_hits:
            codes.append("encoded_blob")
            details["encoded_blob"] = True
            details["encoded_blob_count"] = int(blob_hits)

        if list(source_texts):
            quote_hits, truncated = _source_quote_result(s_compact, list(source_texts))
            if truncated:
                details["source_truncated"] = int(truncated)
            if quote_hits:
                codes.append("source_quote")
                details["source_quote"] = True
                details["source_quote_count"] = int(quote_hits)

        codes = list(dict.fromkeys(codes))
        return {"ok": not codes, "codes": codes, "details": details}
    except _EXPECTED_ERRORS:
        return {"ok": False, "codes": ["check_error"], "details": {"check_error": True}}
