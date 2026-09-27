from app.memory.short_term import ShortTermMemory
from app.premarketing import capture_and_queue, lead_score


def test_lead_score_requires_substantive_opt_in_information():
    weak = lead_score("", "", "短い課題", "", "未定", False)
    qualified = lead_score(
        "Example株式会社", "DX責任者",
        "機密資料を外部へ出さずに分析し、現場業務を改善する方法を検討しています。",
        "1か月以内", "100万円程度", True,
    )
    assert weak < 60
    assert qualified >= 60


def test_lead_score_is_bounded():
    assert lead_score("会社", "責任者", "課題" * 100, "すぐ", "予算あり", True) <= 100


def test_campaign_capture_queues_qualified_lead_for_approval(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("lead loop")
    campaign = memory.create_campaign(
        project["id"], "ローカルAI相談", "機密業務を扱うDX担当者",
        "30分の課題診断とPoC適合性確認", "無料相談を予約", "premarketing/test",
    )
    lead, action = capture_and_queue(
        memory, campaign, name="山田", email="lead@example.test",
        company="Example株式会社", role="DX責任者",
        problem="機密資料を外部へ出さずに分析し、業務改善へ利用する方法を検討しています。",
        timeline="1か月以内", budget="100万円程度", consent=True,
    )

    leads = memory.list_leads(project["id"])
    actions = memory.list_actions(project["id"])
    assert lead["id"] == leads[0]["id"]
    assert action and action["id"] == actions[0]["id"]
    assert leads[0]["status"] == "contact_queued"
    assert leads[0]["score"] >= 60
    assert actions[0]["status"] == "pending_approval"
    assert actions[0]["target"] == "lead@example.test"
