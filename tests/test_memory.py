import pytest

from app.memory.short_term import ShortTermMemory


def test_memory_roundtrip(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    memory.save("s", "こんにちは", "こんにちは！", "test", 42.0)
    assert memory.recent("s") == [
        {"role": "user", "content": "こんにちは"},
        {"role": "assistant", "content": "こんにちは！"},
    ]


def test_append_plan_tasks_preserves_completed_work(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("supplement")
    memory.save_mission(project["id"], "goal", "criteria", "", False, [])
    mission = memory.replace_plan(project["id"], "initial", [{
        "task_key": "initial", "title": "Initial", "depends_on": [],
    }])
    memory.update_task(mission["tasks"][0]["id"], "completed", result="kept")

    updated = memory.append_plan_tasks(project["id"], [{
        "task_key": "supplement_1", "title": "Supplement",
        "depends_on": ["initial"], "description": "fill gap",
        "acceptance_criteria": "artifact exists",
    }], "supplement round")

    assert [task["status"] for task in updated["tasks"]] == ["completed", "pending"]
    assert updated["tasks"][0]["result"] == "kept"
    assert updated["tasks"][1]["depends_on"] == ["initial"]
    assert updated["status"] == "running"


def test_add_mission_instruction_preserves_plan_and_returns_chat(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("instruction chat")
    memory.save_mission(project["id"], "goal", "criteria", "original", False, [])
    mission = memory.replace_plan(project["id"], "initial", [{
        "task_key": "initial", "title": "Initial", "depends_on": [],
    }])
    memory.update_task(mission["tasks"][0]["id"], "completed", result="kept")

    updated = memory.add_mission_instruction(project["id"], "公開URLを登録して検証する")

    assert len(updated["tasks"]) == 1
    assert updated["tasks"][0]["status"] == "completed"
    assert updated["tasks"][0]["result"] == "kept"
    assert "公開URLを登録して検証する" in updated["constraints_text"]
    assert [item["kind"] for item in updated["instruction_messages"]] == [
        "mission_instruction_user", "mission_instruction_system",
    ]
    assert updated["instruction_messages"][0]["message"] == "公開URLを登録して検証する"


def test_external_action_requires_approval_and_keeps_evidence(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("actions")
    action = memory.create_action(project["id"], "email", "buyer@example.test", "件名\n本文")
    assert action["status"] == "pending_approval"
    action = memory.update_action(project["id"], action["id"], "approved")
    assert action["approved_at"]
    action = memory.update_action(
        project["id"], action["id"], "executed", evidence="SMTP accepted",
    )
    assert action["status"] == "executed"
    assert action["evidence"] == "SMTP accepted"
    assert memory.list_actions(project["id"])[0]["id"] == action["id"]


def test_premarketing_campaign_and_consent_lead_are_project_scoped(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("premarketing")
    other = memory.create_project("other")
    campaign = memory.create_campaign(
        project["id"], "AI相談", "業務改善担当", "無料診断", "相談予約",
        "premarketing/test",
    )
    lead = memory.create_lead(
        campaign, "担当者", "lead@example.test", "Example", "責任者",
        "機密資料をローカルで分析したい。導入方法と安全性を確認したい。",
        "1か月以内", "検討中", True, 85,
    )
    assert lead["status"] == "qualified"
    assert lead["consent"] is True
    assert memory.list_leads(project["id"])[0]["score"] == 85
    assert memory.list_leads(other["id"]) == []
    memory.update_lead_status(project["id"], lead["id"], "contact_queued")
    assert memory.get_lead(project["id"], lead["id"])["status"] == "contact_queued"


def test_google_site_publication_state_is_persistent_and_project_scoped(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("landing page")
    other = memory.create_project("other")
    campaign = memory.create_campaign(
        project["id"], "AI相談", "DX担当", "30分診断", "問い合わせる", "",
    )
    updated = memory.update_campaign_site(
        project["id"], campaign["id"], "draft_ready",
        landing_assets_path=f"premarketing/{campaign['id']}/landing",
    )
    assert updated["site_publication_status"] == "draft_ready"
    assert updated["landing_assets_path"].endswith("/landing")
    updated = memory.update_campaign_site(
        project["id"], campaign["id"], "published",
        google_site_url="https://sites.google.com/view/example",
        landing_revision=2, landing_revision_status="review_ready",
        landing_revision_instruction="CTAまでの流れを短くする",
        landing_revision_mode="local",
    )
    assert updated["google_site_url"].endswith("/example")
    assert updated["landing_revision"] == 2
    assert updated["landing_revision_status"] == "review_ready"
    assert updated["landing_revision_instruction"] == "CTAまでの流れを短くする"
    with pytest.raises(KeyError):
        memory.update_campaign_site(other["id"], campaign["id"], "approved")


def test_campaign_creative_state_is_persistent_and_project_scoped(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("Creative")
    other = memory.create_project("Other")
    campaign = memory.create_campaign(project["id"], "相談", "対象顧客", "提供価値", "相談する", "")
    saved = memory.update_campaign_creative(
        project["id"], campaign["id"], "reviewed", provider="canva",
        brand_json='{"primary_color":"#155eef"}', quality_score=82,
        review_text="ブランドとCTAを確認済み",
    )
    assert saved["status"] == "reviewed"
    assert saved["quality_score"] == 82
    assert memory.get_campaign_creative(project["id"], campaign["id"])["provider"] == "canva"
    with pytest.raises(KeyError):
        memory.update_campaign_creative(other["id"], campaign["id"], "approved")


def test_shared_context_is_session_scoped_and_clearable(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    memory.save("session-a", "前提A", "回答A", "front", 1.0)
    memory.save("session-b", "前提B", "回答B", "front", 1.0)

    context = memory.context_text("session-a")
    assert "ユーザー: 前提A" in context
    assert "フロントAI: 回答A" in context
    assert "前提B" not in context
    assert memory.turn_count("session-a") == 1
    assert memory.clear("session-a") == 1
    assert memory.turn_count("session-a") == 0


def test_shared_context_respects_character_limit(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    memory.save("s", "古い前提" * 30, "古い回答" * 30, "front", 1.0)
    memory.save("s", "最新の依頼", "最新の回答", "front", 1.0)
    context = memory.context_text("s", limit=12, max_chars=80)
    assert context.startswith("[古いコンテキストは上限により省略]")
    assert "最新の回答" in context

def test_projects_isolate_context_and_history(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    default = memory.get_project("default")
    assert default and default["name"] == "既定プロジェクト"

    alpha = memory.create_project("Alpha", "Alpha専用の前提")
    beta = memory.create_project("Beta", "Beta専用の前提")
    memory.save("same-session", "Alpha質問", "Alpha回答", "front", 1.0, False, alpha["id"])
    memory.save("same-session", "Beta質問", "Beta回答", "front", 1.0, False, beta["id"])

    alpha_context = memory.context_text("same-session", project_id=alpha["id"])
    beta_context = memory.context_text("same-session", project_id=beta["id"])
    assert "Alpha質問" in alpha_context and "Beta質問" not in alpha_context
    assert "Beta質問" in beta_context and "Alpha質問" not in beta_context
    assert memory.turn_count("same-session", alpha["id"]) == 1
    assert memory.turn_count("same-session", beta["id"]) == 1

    updated = memory.update_project(alpha["id"], "Alpha改", "更新済み")
    assert updated and updated["name"] == "Alpha改" and updated["context_text"] == "更新済み"
    assert memory.delete_project(beta["id"]) == 1
    assert memory.get_project(beta["id"]) is None


def test_existing_turns_are_migrated_to_default_project(tmp_path):
    import sqlite3

    path = tmp_path / "legacy.db"
    db = sqlite3.connect(path)
    try:
        with db:
            db.execute("""CREATE TABLE turns (
                id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, created_at TEXT NOT NULL,
                user_text TEXT NOT NULL, assistant_text TEXT NOT NULL, model TEXT NOT NULL,
                latency_ms REAL NOT NULL, interrupted INTEGER NOT NULL DEFAULT 0)""")
            db.execute("INSERT INTO turns(session_id,created_at,user_text,assistant_text,model,latency_ms,interrupted) VALUES(?,?,?,?,?,?,?)", ("legacy", "now", "old question", "old answer", "front", 1.0, 0))
    finally:
        db.close()

    memory = ShortTermMemory(path)
    assert memory.turn_count("legacy", "default") == 1
    assert memory.recent("legacy", project_id="default")[0]["content"] == "old question"

def test_multiple_context_files_are_project_scoped(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("複数MD")
    first = memory.add_context_file(project["id"], "first.md", "# First\nalpha", 13)
    memory.add_context_file(project["id"], "second.md", "# Second\nbeta", 13)

    metadata = memory.list_context_files(project["id"])
    assert [item["filename"] for item in metadata] == ["first.md", "second.md"]
    assert memory.context_file_chars(project["id"]) == len("# First\nalpha") + len("# Second\nbeta")
    contents = memory.list_context_files(project["id"], include_content=True)
    assert contents[0]["content"] == "# First\nalpha"
    assert contents[1]["content"] == "# Second\nbeta"

    assert memory.delete_context_file(project["id"], first["id"])
    assert [item["filename"] for item in memory.list_context_files(project["id"])] == ["second.md"]
    memory.delete_project(project["id"])
    assert memory.list_context_files(project["id"]) == []
