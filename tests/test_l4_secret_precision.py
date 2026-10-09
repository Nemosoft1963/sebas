"""L4 秘密混入検査の精度テスト(誤検出の固定 + 電話厳密化後の検出漏れ防止)。外部通信なし。"""
import uuid
from types import SimpleNamespace

import pytest

from app.experience_store import canonical


def _loop():
    import app.plan_review_loop as loop
    return loop


MUST_DETECT = [
    # 検出漏れの修正: 語+12桁の区切りあり
    "個人番号: 1234 5678 9012",
    "個人番号: 1234-5678-9012",
    "マイナンバー 1234　5678　9012",  # 全角スペース区切り
    # 電話番号(厳密化後も検出すること)
    "連絡先は 03-1234-5678 です",
    "連絡先は 090-1234-5678 です",
    "09012345678",
    "連絡先 +81-3-1234-5678 まで",
    "電話 (03)1234-5678 へ",
    # メール
    "kaku-test-001@example.invalid",
    # 電話厳密化の巻き添え防止: 数字を含まない形でも専用パターンで検出されること
    "AKIAIOSFODNNEXAMPLEX",
    "ghp_ABCDEFGHIJKLMNOPQRSTUVWXabcdef",
    "github_pat_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef",
    "xoxb-ABCDEFGHIJLMNOPQRSTUVWX",
    "sk" + "_live_" + "ABCDEFGHIJKLMNOPQRSTUVWX",
    "eyJAbcDefGhiJklmno.abcDEFghiJKLmnoPQR.AbcDefGhiJKLmnopQRSTUVWXyzABcdefGHI",
]


@pytest.mark.parametrize("text", MUST_DETECT)
def test_precision_detects(text):
    assert _loop().contains_secret(text) != ""


# 選んだ基準(15桁以上の扱い):
# 15桁以上の連続数字は個人番号(12桁)の検出対象外であり、カード番号候補に
# なっても Luhn 検査を通過しないものは検出しない。
# (例: build 123456789012345 の Luhn 総和は 68 で 10 の倍数にならない)
HARMLESS = [
    # 実測された誤検出
    "2026-10-06 に実施し 20261006 を版とする",
    "20261006 を版とする",
    "工程 a1b2c3d4e5f6 を対象とする",
    "工程 t-0123456789ab は18件に対応",
    # 日付の諸形式
    "2026/10/06 に実施する",
    "2026年10月6日 に実施する",
    "期限は2026年10月末、予算の上限は設定しない",
    # SC番号・バージョン・15桁・金額・パス
    "SC01〜SC18の達成条件を満たす",
    "SC07 の達成条件を確認する",
    "バージョン 1.2.3.4 で固定する",
    "build 123456789012345 の版で固定する",
    "金額 1234567 円を計上する",
    "金額 12,000,000円を計上する",
    "ファイルは result/report.md に保存する",
    # 既存の無害文6種(回帰)
    "目標: 販売活動の成果を測定し、報告書を作成する(3工程)",
    "SC01〜SC18の達成条件を満たす",
    "期限は2026年10月末、予算の上限は設定しない",
    "ファイルは result/report.md に保存する",
    "パスワードや認証情報は扱わない(この文は禁止事項の説明であり値を含まない)",
    "承認済みの工程のみ実行する",
]

UUID_HEX_SAMPLES = [uuid.uuid4().hex for _ in range(3)]


@pytest.mark.parametrize("text", HARMLESS)
def test_precision_harmless(text):
    assert _loop().contains_secret(text) == ""


# --- 語付きの区切りなし電話番号(固定電話・地方・フリーダイヤルを含む10〜11桁) ---
PHONE_WORD_MUST_DETECT = [
    "お問い合わせは 0312345678 まで",
    "電話番号 0312345678",
    "TEL: 0312345678",
    "連絡先 0612345678",
    "電話 0120123456",
    "FAX 0312345679",
    "内線 0312345678",
    "電話番号：0312345678",
]

# 語が付かない10桁の数字や、語と離れた数字は、uuid・工程番号・版との区別のため検出しない(意図)。
PHONE_WORD_HARMLESS = [
    "受付 0312345678 を参照",
    "連絡先は 2026-10-06 以降",
    "電話での確認は不要。工程 0123456789 件",
    "連絡先リスト v0312345678x",
]


@pytest.mark.parametrize("text", PHONE_WORD_MUST_DETECT)
def test_phone_with_keyword_without_separator_is_detected(text):
    assert _loop().contains_secret(text) != ""


@pytest.mark.parametrize("text", PHONE_WORD_HARMLESS)
def test_phone_like_digits_without_keyword_are_not_detected(text):
    assert _loop().contains_secret(text) == ""


def test_phone_keyword_detection_does_not_expose_the_value():
    label = _loop().contains_secret("電話番号 0312345678")
    assert label and "0312345678" not in label

@pytest.mark.parametrize("hx", UUID_HEX_SAMPLES)
def test_uuid_hex_not_detected(hx):
    assert _loop().contains_secret(f"工程 {hx} を対象とする") == ""


class Manager(SimpleNamespace):
    pass


def test_generated_plan_has_no_false_positive(tmp_path):
    """本番相当の計画データ生成関数の出力全体が誤検出されないことの実測。"""
    from app.memory.short_term import ShortTermMemory
    from app.structured_planning import compile_plan, compile_task
    from app.workspace_files import WorkspaceSandbox

    mem = ShortTermMemory(tmp_path / "memory" / "conversations.db")
    pid = mem.create_project("l4precision")["id"]
    goal = "疑似目標\n1. 架空手順Aを定義する\n2. 架空手順Bを報告する"
    mem.save_mission(pid, goal, goal, "", True, ["chatgpt"])
    ws = WorkspaceSandbox(tmp_path / "workspace")
    mgr = Manager(memory=mem, workspace=ws, planning_projects=set(), llm=None,
                  workers={}, _sync_memos=lambda pid: None,
                  provider_statuses=lambda: [{"id": "chatgpt", "configured": True}],
                  plan_review_runner=None)
    hx1, hx2 = uuid.uuid4().hex, uuid.uuid4().hex
    criteria = ["架空手順Aを定義する", "架空手順Bを報告する"]
    t1 = compile_task(1, criteria[0], {"title": "架空資料A", "scope": f"架空Aの整理。工程 {hx1} を対象とし t-0123456789ab で管理し 2026-10-06 に実施する",
        "headings": ["目的", "実施内容"], "depends_on": []}, [])
    t2 = compile_task(2, criteria[1], {"title": "架空資料B", "scope": "架空Bの整理。バージョン 1.2.3.4 と金額 12000000円の扱いを整理する",
        "headings": ["目的", "実施内容"], "depends_on": ["SC01"]}, [])
    compiled = compile_plan(criteria, [t1, t2], goal=mem.get_mission(pid)["goal"])
    mem.replace_plan(pid, "l4 precision plan", compiled["tasks"])
    assert mgr is not None
    blob = (canonical(compiled) + "\n" + goal + "\n"
            f"工程 {hx1} と工程 {hx2} を対象とし 20261006 を版とし build 123456789012345 で固定する\n"
            "SC01〜SC18の達成条件を満たし 2026年10月6日 に実施する")
    assert _loop().contains_secret(blob) == ""


def test_hashes_and_uuids_are_never_flagged_as_card_numbers():
    """機械が作る識別子(sha256・UUID)は、数字の連なりが偶然 Luhn に通ってもカード番号と誤検出しない。

    以前は約0.2〜0.7%/識別子で誤検出し、識別子を多く含む文書(指示パッケージ等)が
    実行ごとに不安定に拒否されていた。乱数は固定シードで決定的にする。
    """
    import hashlib
    import random
    import uuid

    rng = random.Random(20261009)
    samples = []
    for _ in range(20000):
        raw = rng.getrandbits(256).to_bytes(32, "big")
        samples.append(hashlib.sha256(raw).hexdigest())
        samples.append(uuid.UUID(int=rng.getrandbits(128), version=4).hex)
        samples.append(str(uuid.UUID(int=rng.getrandbits(128), version=4)))
    hits = [x for x in samples if _loop().contains_secret(x) != ""]
    assert hits == [], hits[:3]
    blob = " ".join(samples[:300])
    assert _loop().contains_secret(blob) == ""


@pytest.mark.parametrize("text", [
    "カード番号は 4111 1111 1111 1111 です",
    "カード番号は 4111-1111-1111-1111 です",
    "番号:4111111111111111。確認",
    "(4111111111111111)",
    "支払い 4111 1111 1111 1111",
])
def test_real_card_numbers_still_detected_after_precision_fix(text):
    assert _loop().contains_secret(text) == "カード番号様"


@pytest.mark.parametrize("text", [
    "ab7f7a00-0752-4127-9873-cebf86005d4e",
    "6b3ec0d0-28e1-4bdc-88cd-299404382297",
    "proposal_id: 4ffe71ea405e2fb3c8991371560790f8",
    "plan_signature 01dbf18e17c08a68bf15a088bbd5c0fae8bb64cb371f038e1e0a5a9c944c8e96",
])
def test_machine_ids_are_not_phone_or_mynumber_lookalikes(text):
    """UUIDの数字だけのグループ(電話番号様・12桁個人番号様に見える)を誤検出しない。"""
    assert _loop().contains_secret(text) == ""


def test_real_numbers_next_to_machine_ids_are_still_detected():
    """識別子のマスクが、同じ文中の本物の電話番号・個人番号の検出を妨げない。"""
    uid = "ab7f7a00-0752-4127-9873-cebf86005d4e"
    assert _loop().contains_secret(f"{uid} 連絡先は 03-1234-5678 です") != ""
    assert _loop().contains_secret(f"{uid} 個人番号: 1234 5678 9012") != ""
