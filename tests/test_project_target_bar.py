"""U1: 固定された作業対象帯とプロジェクト設定UIの静的検査。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
JS = (ROOT / "app/static/project_mission.js").read_text(encoding="utf-8")
CSS = (ROOT / "app/static/project_mission.css").read_text(encoding="utf-8")


def test_project_target_bar_precedes_workflow_tabs():
    assert 'id="projectTargetBar"' in HTML
    assert 'aria-label="作業対象"' in HTML
    assert "targetBar.insertAdjacentElement('afterend',host)" in JS
    assert JS.index("targetBar.insertAdjacentElement('afterend',host)") < JS.index("host.querySelectorAll('[role=tab]')")


def test_project_target_controls_and_settings_exist():
    assert 'id="projectSelect"' in HTML
    assert 'id="projectTargetName"' in HTML
    assert 'id="projectNew"' in HTML and "新規作成" in HTML
    assert 'id="projectSettingsToggle"' in HTML and ">設定</button>" in HTML
    assert 'id="projectSettingsPanel"' in HTML
    assert "settingsPanel.appendChild(projectCard)" in JS
    assert "settingsToggle.onclick" in JS
    assert 'el("projectTargetName").textContent=selected&&selected.name||"未選択"' in JS


def test_mobile_css_prevents_target_and_tabs_overlap():
    assert ".project-target{position:sticky;top:0" in CSS
    assert "@media(max-width:760px)" in CSS
    assert ".project-target-main{display:grid" in CSS
    assert ".workflow [role=tablist]{display:grid" in CSS
    assert "minmax(0,1fr)" in CSS
    assert "@media(max-width:400px)" in CSS


def test_existing_project_operations_and_api_paths_remain():
    html_ids = ["projectSave", "projectDelete", "projectMd", "projectDownload", "projectName", "projectContext"]
    for element_id in html_ids:
        assert f'id="{element_id}"' in HTML
    handlers = ["$('#projectNew').onclick", "$('#projectSave').onclick", "$('#projectDelete').onclick", "$('#projectMd').onclick"]
    for handler in handlers:
        assert handler in HTML
    assert "fetch('/api/projects'" in HTML
    assert "fetch('/api/projects/'+encodeURIComponent(currentProject)" in HTML
    assert '"/api/projects/"+encodeURIComponent(currentProject)+"/context-files"' in JS
