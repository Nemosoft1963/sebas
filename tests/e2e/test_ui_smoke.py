"""Read-only browser smoke test for the local Sebas UI.

Run explicitly with SEBAS_E2E_BASE_URL=http://127.0.0.1:8099.
No project action or external-AI request is submitted.
"""
import os

import pytest
playwright = pytest.importorskip("playwright.sync_api")
Page = playwright.Page
expect = playwright.expect


@pytest.mark.e2e
@pytest.mark.skipif(not os.environ.get("SEBAS_E2E_BASE_URL"), reason="Set SEBAS_E2E_BASE_URL for local UI smoke")
def test_goal_review_controls_mount_without_starting_run(page: Page) -> None:
    base_url = os.environ.get("SEBAS_E2E_BASE_URL", "").rstrip("/")
    if not base_url:
        pytest.skip("Set SEBAS_E2E_BASE_URL to run against a local Sebas instance")
    if not base_url.startswith(("http://127.0.0.1:", "http://localhost:")):
        pytest.fail("E2E target must be the local loopback service")

    response = page.goto(base_url + "/", wait_until="domcontentloaded", timeout=30000)
    assert response is not None and response.status == 200
    expect(page.locator("#projectSelect")).to_be_attached(timeout=15000)
    expect(page.locator("#goalReviewPanel")).to_be_attached(timeout=15000)
    expect(page.locator("#autoLoopCards")).to_be_attached(timeout=15000)
    summary = page.locator("#autoLoopSummary")
    expect(summary).to_be_attached(timeout=15000)
    page.evaluate("window.selectWorkflowTab('goal_plan')")
    expect(summary).to_be_visible(timeout=15000)
    start = page.locator("#autoLoopSummary ~ button").nth(1)
    expect(start).to_be_disabled()
    summary.fill("公開可能な架空案件の目的と成果物")
    expect(page.locator("#autoLoopStatus")).to_contain_text("再確認")
    expect(start).to_be_disabled()