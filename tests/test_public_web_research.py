import socket

import httpx
import pytest

from app.memory.short_term import ShortTermMemory
from app.project_manager import (
    ProjectOrchestrator, build_public_web_evidence_report, required_input_gaps,
)
from app.public_web_research import (
    PublicWebResearchError,
    PublicWebResearcher,
    _result_url,
    validate_public_https_url,
)
from app.structured_planning import compile_plan, compile_task, contract_of, verify_outputs
from app.workspace_files import WorkspaceSandbox


def public_resolver(host, port, type=socket.SOCK_STREAM):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


@pytest.mark.asyncio
async def test_public_url_guard_rejects_local_addresses():
    def local_resolver(host, port, type=socket.SOCK_STREAM):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    with pytest.raises(PublicWebResearchError, match="プライベート"):
        await validate_public_https_url("https://localhost/private", local_resolver)


def test_duckduckgo_redirect_parser_requires_exact_domain_boundary():
    target = "https://example.com/source"
    assert _result_url(
        "https://html.duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fsource"
    ) == target
    malicious = "https://duckduckgo.com.attacker.example/l/?uddg=https%3A%2F%2Fprivate.example"
    assert _result_url(malicious) == malicious


@pytest.mark.asyncio
async def test_collector_prioritizes_official_https_source_and_extracts_text():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "html.duckduckgo.com":
            return httpx.Response(200, text=(
                '<a class="result__a" href="https://example.com/summary">summary</a>'
                '<a class="result__a" href="https://www.chusho.meti.go.jp/koukai.html">official</a>'
            ), request=request)
        body = "<html><title>公式公募要領</title><main>" + ("公募要領の検証可能な本文です。" * 20) + "</main></html>"
        return httpx.Response(200, text=body, headers={"content-type": "text/html"}, request=request)

    researcher = PublicWebResearcher(
        transport=httpx.MockTransport(handler), resolver=public_resolver,
    )
    result = await researcher.collect("ものづくり補助金 公募要領 WEB検索", max_results=1)

    assert result["sources"][0]["official"] is True
    assert result["sources"][0]["url"].startswith("https://www.chusho.meti.go.jp/")
    assert "検証可能な本文" in result["sources"][0]["content"]


@pytest.mark.asyncio
async def test_collector_decodes_shift_jis_public_page():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "html.duckduckgo.com":
            return httpx.Response(200, text=(
                '<a class="result__a" href="https://www.chusho.meti.go.jp/koukai.html">official</a>'
            ), request=request)
        body = (
            '<html><head><meta charset="Shift_JIS"><title>公式公募要領</title></head>'
            '<main>' + ("日本語の検証可能な本文です。" * 20) + '</main></html>'
        ).encode("cp932")
        return httpx.Response(
            200, content=body, headers={"content-type": "text/html"}, request=request,
        )

    researcher = PublicWebResearcher(
        transport=httpx.MockTransport(handler), resolver=public_resolver,
    )
    result = await researcher.collect("ものづくり補助金 公募要領 WEB検索", max_results=1)

    assert result["sources"][0]["title"] == "公式公募要領"
    assert "日本語の検証可能な本文" in result["sources"][0]["content"]


def test_web_instruction_compiles_to_local_first_research_contract():
    task = compile_task(1, "WEBを検索して現行の公募要領を収集する", {
        "title": "公募要領の収集", "scope": "公式一次資料を確認する",
        "headings": ["取得資料", "制度要件"], "depends_on": [],
    }, [])

    assert task["mode"] == "research"
    assert contract_of(task)["public_web_research"]["official_sources_required"] is True
    assert "プロジェクト原本" in task["description"]


@pytest.mark.parametrize("instruction", [
    "情報検索スキルを使ってものづくり補助金の最新情報を取得して反映する",
    "現行の公募要領を公式サイトから確認する",
    "公式PDFを収集して制度の最新情報を確認する",
    "外部の情報検索タスクを最優先にする",
])
def test_japanese_research_instructions_compile_as_web_tasks(instruction):
    task = compile_task(1, instruction, {
        "title": "公式情報収集", "scope": "公式一次資料を取得して根拠を保存する",
        "headings": ["取得資料", "検証結果"], "depends_on": [],
    }, [])

    assert task["mode"] == "research"
    assert contract_of(task)["public_web_research"]["required"] is True


def test_grant_web_query_does_not_forward_full_private_instruction():
    task = compile_task(
        1,
        "ものづくり補助金を検索する。試算表、社内資料、個人情報を送らない。",
        {
            "title": "公式情報収集", "scope": "公式一次資料を取得する",
            "headings": ["取得資料", "検証結果"], "depends_on": [],
        },
        [],
    )

    query = contract_of(task)["public_web_research"]["query"]
    assert query == "ものづくり補助金 最新 公募要領 公式 PDF"
    assert "試算表" not in query


def test_required_input_gate_detects_missing_trial_balance():
    gaps = required_input_gaps(
        {"goal": "最新の試算表をベースに経営計画を作る", "success_criteria": ""},
        {"title": "財務計画", "description": "最新の試算表を基に経営数値を分析する",
         "acceptance_criteria": ""},
        [{"filename": "system-overview.md", "source": "upload"}],
    )

    assert gaps == ["最新の試算表（Excel/CSV/PDF）"]


def test_web_and_nonfinancial_tasks_do_not_require_trial_balance():
    mission = {"goal": "最新の試算表をベースに経営計画を作る", "success_criteria": ""}
    web_task = compile_task(1, "情報検索スキルで最新の公募要領を取得する", {
        "title": "Web調査", "scope": "公式公募要領を取得する",
        "headings": ["取得資料", "要件"], "depends_on": [],
    }, [])
    diagram_task = compile_task(2, "システム構成図を作る", {
        "title": "構成図", "scope": "システム構成とデータフローを整理する",
        "headings": ["構成", "データフロー"], "depends_on": [],
    }, [])

    assert required_input_gaps(mission, web_task, []) == []
    assert required_input_gaps(mission, diagram_task, []) == []


def test_financial_task_still_requires_trial_balance():
    mission = {"goal": "最新の試算表をベースに経営計画を作る", "success_criteria": ""}
    finance_task = compile_task(1, "経営数値を作成する", {
        "title": "財務分析",
        "scope": "最新の試算表を基に売上と利益を分析し投資効果を計算する",
        "headings": ["財務分析", "投資効果"], "depends_on": [],
    }, [])

    assert required_input_gaps(mission, finance_task, []) == [
        "最新の試算表（Excel/CSV/PDF）"
    ]


@pytest.mark.asyncio
async def test_web_capture_is_registered_as_project_source(tmp_path):
    class Researcher:
        async def collect(self, query):
            content = "公式情報です。" * 30
            return {"query": query, "errors": [], "sources": [{
                "url": "https://www.chusho.meti.go.jp/rule.html",
                "title": "公式公募要領", "retrieved_at": "2026-09-15T00:00:00+00:00",
                "content": content, "sha256": "a" * 64, "official": True,
            }]}

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("web", workspace_path="projects/web")
    workspace = WorkspaceSandbox(tmp_path / "workspace")
    manager = ProjectOrchestrator(
        memory, object(), lambda _: ("", []), None, lambda: [],
        workspace=workspace, public_web_researcher=Researcher(),
    )
    task = compile_task(1, "WEBを検索して公募要領を収集する", {
        "title": "Web調査", "scope": "公式公募要領を収集する",
        "headings": ["取得資料", "要件"], "depends_on": [],
    }, [])
    task["id"] = "task-1"

    source_ids = await manager._collect_public_web_evidence(
        project, project["id"], task, contract_of(task)
    )

    files = memory.list_context_files(project["id"], include_content=True)
    assert len(source_ids) == 1
    assert files[0]["source"] == "web"
    assert "https://www.chusho.meti.go.jp/rule.html" in files[0]["content"]


def test_heading_verifier_accepts_cosmetic_number_prefix(tmp_path):
    path = tmp_path / "report.md"
    path.write_text("# 事業概要\n\n" + ("内容" * 350), encoding="utf-8")
    task = compile_task(1, "概要を作る", {
        "title": "概要", "scope": "概要を整理する",
        "headings": ["1. 事業概要", "確認事項"], "depends_on": [],
    }, [])
    contract = contract_of(task)
    contract["outputs"][0]["required_headings"] = ["1. 事業概要"]
    task["acceptance_criteria"] = __import__("json").dumps(contract, ensure_ascii=False)

    assert verify_outputs(task, lambda relative: path) == []


def test_verifier_rejects_uncited_high_risk_numeric_claim(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("claims", workspace_path="projects/claims")
    workspace = WorkspaceSandbox(tmp_path / "workspace")
    content = (
        "# 制度情報\n\n補助金の補助率は70％である。\n\n"
        "# 根拠と未確認事項\n\n根拠資料の記載なし。\n\n"
        "# 実施状態と次の行動\n\n確認を進める。\n\n" + ("説明。" * 180)
    )
    audit = workspace.apply_operations(project["workspace_path"], project["id"], [{
        "action": "write_text", "path": "result/sc01.md", "content": content,
    }])
    task = compile_task(1, "制度情報を整理する", {
        "title": "制度情報", "scope": "補助制度を整理する",
        "headings": ["制度情報", "確認事項"], "depends_on": [],
    }, [])
    contract = contract_of(task)
    contract["outputs"][0]["required_headings"] = [
        "制度情報", "根拠と未確認事項", "実施状態と次の行動",
    ]
    task["acceptance_criteria"] = __import__("json").dumps(contract, ensure_ascii=False)
    manager = ProjectOrchestrator(
        memory, object(), lambda _: ("", []), None, lambda: [], workspace=workspace,
    )

    failures, _ = manager._verify_execution_evidence(
        project, project["id"], task, "完了",
        {"workspace_operations": audit, "table_operations": [], "source_references": []},
        [],
    )

    assert any("高リスク数値・制度主張" in failure for failure in failures)


def test_web_task_cannot_complete_without_captured_web_metadata(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("web evidence", workspace_path="projects/web-evidence")
    workspace = WorkspaceSandbox(tmp_path / "workspace")
    content = (
        "# 取得資料\n\n公式資料を調査した。\n\n"
        "# 検証結果\n\n内容を整理した。\n\n"
        "# 根拠と未確認事項\n\nなし。\n\n"
        "# 実施状態と次の行動\n\n完了。\n\n" + ("説明。" * 180)
    )
    audit = workspace.apply_operations(project["workspace_path"], project["id"], [{
        "action": "write_text", "path": "result/sc01.md", "content": content,
    }])
    task = compile_task(1, "Webを検索して現行公募要領を収集する", {
        "title": "Web調査", "scope": "公式資料を収集する",
        "headings": ["取得資料", "検証結果"], "depends_on": [],
    }, [])
    manager = ProjectOrchestrator(
        memory, object(), lambda _: ("", []), None, lambda: [], workspace=workspace,
    )

    failures, _ = manager._verify_execution_evidence(
        project, project["id"], task, "完了",
        {"workspace_operations": audit, "table_operations": [], "source_references": []},
        [],
    )

    assert any("Web原本証拠がありません" in failure for failure in failures)


def test_deterministic_web_report_contains_every_contracted_heading_and_metadata():
    task = compile_task(1, "Webを検索して現行公募要領を収集する", {
        "title": "Web調査", "scope": "公式資料を収集する",
        "headings": ["事例紹介", "制度要件"], "depends_on": [],
    }, [])
    captured = (
        "# 公開Web取得資料\n\n"
        "- URL: https://www.chusho.meti.go.jp/rule.html\n"
        "- タイトル: 公式公募要領\n"
        "- 取得日時(UTC): 2026-09-15T00:00:00+00:00\n"
        "- 公的候補: はい\n"
        f"- 本文SHA256: {'a' * 64}\n\n"
        "## 抽出本文\n\n公式本文"
    )

    report = build_public_web_evidence_report(task, [{
        "id": "web-1", "source": "web", "filename": "capture.md", "content": captured,
    }])

    for heading in contract_of(task)["outputs"][0]["required_headings"]:
        assert f"## {heading}" in report
    assert "https://www.chusho.meti.go.jp/rule.html" in report
    assert "context:web-1" in report
    assert "a" * 64 in report
    assert "補助率は" not in report


@pytest.mark.asyncio
async def test_web_task_uses_deterministic_report_without_calling_llm(tmp_path):
    class NeverLlm:
        async def complete(self, messages):
            raise AssertionError("Web証拠レポートでLLMを呼び出してはいけません")

    class Researcher:
        async def collect(self, query):
            content = "公式公募要領の取得本文です。" * 30
            return {"query": query, "errors": [], "sources": [{
                "url": "https://www.chusho.meti.go.jp/rule.html",
                "title": "公式公募要領", "retrieved_at": "2026-09-15T00:00:00+00:00",
                "content": content, "sha256": "b" * 64, "official": True,
            }]}

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("deterministic", workspace_path="projects/deterministic")
    memory.save_mission(project["id"], "公募要領を確認する", "", "", False, [])
    web_task = compile_task(1, "Webを検索して現行公募要領を収集する", {
        "title": "Web調査", "scope": "公式資料を収集する",
        "headings": ["事例紹介", "制度要件"], "depends_on": [],
    }, [])
    plan = compile_plan(
        ["Webを検索して現行公募要領を収集する"], [web_task],
    )
    mission = memory.replace_plan(project["id"], plan["summary"], plan["tasks"])
    preparation = next(item for item in mission["tasks"] if item["task_key"] == "SC00")
    memory.update_task(preparation["id"], "completed", result="原本確認済み")
    current = next(item for item in memory.get_mission(project["id"])["tasks"]
                   if item["task_key"] == "SC01")
    workspace = WorkspaceSandbox(tmp_path / "workspace")

    def context_provider(_project):
        return "", memory.list_context_files(project["id"], include_content=True)

    manager = ProjectOrchestrator(
        memory, NeverLlm(), context_provider, None, lambda: [],
        workspace=workspace, public_web_researcher=Researcher(),
    )
    result = await manager._execute_task(project["id"], current["id"])

    _, _, path = workspace.resolve_file(
        project["workspace_path"], project["id"], "result/sc01.md", must_exist=True,
    )
    content = path.read_text(encoding="utf-8")
    assert "必須見出し・取得メタデータ・原本参照: 合格" in result
    assert "## 事例紹介" in content
    assert "https://www.chusho.meti.go.jp/rule.html" in content
    assert any(
        event["kind"] == "deterministic_web_report_generated"
        for event in memory.get_mission(project["id"])["events"]
    )
