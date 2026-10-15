"""A-3 fail-open 静的検査のテスト (疑似データのみ・実コード全体の検査)。

- (a) 現コード全体の検査でベースラインに無い新規検出があれば落ちる。
- (b) ベースラインにあるが現コードに無い(直された)検出が残っていれば落ちる。
- (c) 検査モジュール自体の単体テスト (P1〜P4 の違反例・非違反例)。
- (d) high_risk の件数がベースライン記録と一致する。
外部通信なし。製品コードは修正しない (A-1 の app/autopilot.py 修正を除く)。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.fail_open_lint as lint

BASELINE = ROOT / "tests" / "fail_open_baseline.json"


def _baseline():
    return lint.load_baseline(BASELINE)


def test_no_new_findings_beyond_baseline():
    base = _baseline()
    current = lint.lint_project(ROOT)
    new_items, _stale = lint.check_against_baseline(current, base["by_id"])
    assert new_items == [], (
        "ベースラインに無い新しい fail-open 検出があります。新規の違反を入れないこと。"
        f"新規 {len(new_items)} 件: "
        + ", ".join(f"{x['id']} [{x['snippet'][:60]}]" for x in new_items[:10])
    )


def test_no_stale_baseline_entries():
    base = _baseline()
    current = lint.lint_project(ROOT)
    _new, stale = lint.check_against_baseline(current, base["by_id"])
    assert stale == [], (
        "直された(現コードに無い)のにベースラインに残っている検出があります。"
        "直したら一覧から外すこと。"
        f"残留 {len(stale)} 件: " + ", ".join(x["id"] for x in stale[:10])
    )


def test_baseline_counts_match():
    raw = json.loads(BASELINE.read_text(encoding="utf-8"))
    items = raw["findings"]
    assert raw["total"] == len(items), (raw["total"], len(items))
    n_high = sum(1 for x in items if x.get("classification") == "high_risk")
    assert raw["high_risk_count"] == n_high, (raw["high_risk_count"], n_high)
    # (d) 現コードの high_risk 由来の検出件数が記録と一致する。
    current = lint.lint_project(ROOT)
    cur_high = [c for c in current if lint.is_core_module(c["file"])]
    assert len(cur_high) == n_high, (len(cur_high), n_high)
    for x in items:
        assert x.get("classification") in ("allow", "review", "high_risk"), x
        assert x.get("reason"), x
        assert x.get("id") and x.get("file") and x.get("function") \
            and x.get("type"), x


def test_ids_stable_without_lineno():
    import re
    current = lint.lint_project(ROOT)
    assert current, "lint_project が空です"
    for f in current:
        assert ":" in f["id"]
        # 行番号依存の識別子は禁止 (ファイル:関数:型:ハッシュ[#n] の形)。
        m = re.fullmatch(r"(.+):([^:]+):(P1|P2|P3|P4):([0-9a-f]{12})(#\d+)?",
                         f["id"])
        assert m, f["id"]
        rel, func, typ = m.group(1), m.group(2), m.group(3)
        assert rel == f["file"] and func == f["function"] and typ == f["type"]
    # 同じ内容なら同じ識別子 (PYTHONHASHSEED 安定の実測)。
    again = lint.lint_project(ROOT)
    assert [x["id"] for x in again] == [x["id"] for x in current]


# --- (c) 検査モジュール自体の単体テスト ----------------------------------------

def _py(rel, text):
    items, _ = lint.lint_python_text(rel, text)
    return items


def test_p1_detects_swallow_and_suppress():
    rel = "app/架空.py"
    hits = _py(rel, "def f():\n    try:\n        x()\n    except Exception:\n        pass\n")
    assert any(h["type"] == "P1" for h in hits), hits
    hits = _py(rel, "def f():\n    try:\n        x()\n    except BaseException:\n        return None\n")
    assert any(h["type"] == "P1" for h in hits), hits
    bare = _py(rel, "def f():\n    try:\n        x()\n    except:\n        continue\n")
    assert any(h["type"] == "P1" for h in bare), bare
    consts = _py(rel, "def f():\n    try:\n        x()\n    except Exception:\n        return []\n")
    assert any(h["type"] == "P1" for h in consts), consts
    sup = _py(rel, "import contextlib\ndef f():\n    "
                   "with contextlib.suppress(Exception):\n        x()\n")
    assert any(h["type"] == "P1" for h in sup), sup


def test_p1_ignores_narrow_and_logging_reraise():
    rel = "app/架空.py"
    narrow = _py(rel, "def f():\n    try:\n        x()\n    except ValueError:\n        pass\n")
    assert not [h for h in narrow if h["type"] in ("P1", "P2")], narrow
    logged = _py(rel, "def f():\n    import logging\n    try:\n        x()\n"
                      "    except Exception as e:\n        logging.warning('%s', e)\n"
                      "        raise\n")
    assert not [h for h in logged if h["type"] in ("P1", "P2")], logged
    key = _py(rel, "def f():\n    try:\n        x()\n    except KeyError:\n        return None\n")
    assert not [h for h in key if h["type"] in ("P1", "P2")], key


def test_p2_detects_affirmative_return():
    rel = "app/架空.py"
    hits = _py(rel, "def f():\n    try:\n        x()\n    except Exception:\n        return True\n")
    assert any(h["type"] == "P2" for h in hits), hits
    ok = _py(rel, "def f():\n    try:\n        x()\n    except Exception:\n        return 'ok'\n")
    assert any(h["type"] == "P2" for h in ok), ok
    active = _py(rel, "def f():\n    try:\n        x()\n    except Exception:\n"
                      "        return {'ok': True}\n")
    assert any(h["type"] == "P2" for h in active), active
    # 狭い例外での return True は対象外。
    narrow = _py(rel, "def f():\n    try:\n        x()\n    except ValueError:\n        return True\n")
    assert not [h for h in narrow if h["type"] == "P2"], narrow
    # 定数 None の return は P1 (P2 ではない)。
    none_ret = _py(rel, "def f():\n    try:\n        x()\n    except Exception:\n        return None\n")
    assert any(h["type"] == "P1" for h in none_ret)
    assert not [h for h in none_ret if h["type"] == "P2"]


def test_p3_detects_or_true_and_ignores_get_default():
    rel = "app/架空.py"
    hits = _py(rel, "def f(x):\n    return x or True\n")
    assert any(h["type"] == "P3" for h in hits), hits
    one = _py(rel, "def f(x):\n    return x or 1\n")
    assert any(h["type"] == "P3" for h in one), one
    ok = _py(rel, "def f(x):\n    return x or 'ok'\n")
    assert any(h["type"] == "P3" for h in ok), ok
    # .get(k, 0) は対象外 (仕様)。
    get0 = _py(rel, "def f(d):\n    return d.get('k', 0)\n")
    assert not [h for h in get0 if h["type"] == "P3"], get0
    # or False / or 0 は対象外 (肯定の補完ではない)。
    neg = _py(rel, "def f(x):\n    return x or False\n")
    assert not [h for h in neg if h["type"] == "P3"], neg
    zero = _py(rel, "def f(x):\n    return x or 0\n")
    assert not [h for h in zero if h["type"] == "P3"], zero


def test_p4_detects_js_patterns():
    rel = "app/static/架空.js"
    arrow = lint.lint_js_text(rel, "async function load(){const d=await fetch(u).catch(()=>({}));}")
    assert any(h["type"] == "P4" for h in arrow), arrow
    empty = lint.lint_js_text(rel, "function load(){try{doX();}catch(_err){}}")
    assert any(h["type"] == "P4" for h in empty), empty
    ret_only = lint.lint_js_text(rel, "function rawJson(v){try{return JSON.parse(v);}catch(e){return v;}}")
    assert any(h["type"] == "P4" for h in ret_only), ret_only
    # Epoch ガード・メッセージ表示付きの catch は対象外。
    guarded = lint.lint_js_text(
        rel, "async function load(){try{await api();}catch(e){if(t!==epoch)return;st.textContent=e.message;}}")
    assert not [h for h in guarded if h["type"] == "P4"], guarded
    thrown = lint.lint_js_text(
        rel, "function f(){try{g();}catch(e){throw Error('x');}}")
    assert not [h for h in thrown if h["type"] == "P4"], thrown


def test_lint_changed_files_reusable():
    # ベースライン済みの既存ファイルだけを指定しても新規違反は無い。
    out = lint.lint_changed_files(["app/autopilot.py"])
    assert out == [], out[:3]
    # 未知のパスでは空 (落とさない)。
    assert lint.lint_changed_files(["app/存在しない架空.py"]) == []
