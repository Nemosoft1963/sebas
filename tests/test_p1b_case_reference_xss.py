"""P1-B UI: 事例参照表示のXSS安全表示（静的検査）。

事例の理由・抜粋はユーザー/外部由来の文字列のため、HTMLとして解釈してはならない。
project_mission.js の事例参照表示は innerHTML を使わず textContent/createElement で
構築すること（innerHTML契約28件の維持）。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PM = (ROOT / "app/static/project_mission.js").read_text(encoding="utf-8")

# 攻撃文字列の例（データとして扱われ、DOMとして解釈されてはならない）。
XSS_PAYLOAD = "<img src=x onerror=alert(1)>"


def _case_section():
    start = PM.find("async function loadCaseReferences(")
    assert start != -1
    end = PM.find("async function loadWorkspace(", start)
    assert end > start
    return PM[start:end]


def test_p1b_case_ui_uses_safe_dom_and_keeps_innerhtml_contract():
    assert PM.count("innerHTML") == 28
    section = _case_section()
    assert "innerHTML" not in section
    assert "insertAdjacentHTML" not in section
    assert "document.write" not in section
    assert "createElement" in section
    assert "textContent" in section
    assert "missionCaseReferenceList" in section


def test_p1b_case_ui_xss_payload_is_rendered_as_text():
    section = _case_section()
    # 外部由来の文字列は textContent 経路でのみ表示される。
    assert "r.reason" in section
    assert "r.excerpt" in section
    assert "innerHTML" not in section
    # 攻撃文字列自体がマークアップとして埋め込まれていない（データでありコードではない）。
    assert XSS_PAYLOAD not in PM


def test_p1b_case_ui_labels_and_evidence_policy_remain():
    section = _case_section()
    assert "参考にした事例" in section
    assert "採用しなかった理由" in section
    assert "criterion.status" in section
    assert "slice(0,400)" in section
    assert ".evidence" not in section
    assert "達成証拠として表示しません" in PM
