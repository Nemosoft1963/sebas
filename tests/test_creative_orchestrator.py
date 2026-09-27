from app.creative_orchestrator import public_creative_data


def test_public_creative_data_excludes_private_fields():
    campaign = {
        "title": "相談", "audience": "企業", "offer": "課題整理",
        "call_to_action": "申し込む", "client_secret": "never-send",
        "lead_records": [{"email": "private@example.test"}],
    }
    brand = {
        "brand_name": "Local Supporter", "primary_color": "#155eef",
        "secondary_color": "#172033", "accent_color": "#52d6a7",
        "background_color": "#f7faff", "tone": "信頼",
        "internal_note": "never-send",
    }
    result = public_creative_data(campaign, brand)
    assert set(result) == {"title", "audience", "offer", "call_to_action", "brand"}
    encoded = str(result)
    assert "never-send" not in encoded
    assert "private@example.test" not in encoded
