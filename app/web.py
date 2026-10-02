from __future__ import annotations

import asyncio
import hashlib
import html
import io
import json
import logging
import os
import smtplib
import sqlite3
import tempfile
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, File, Header, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi import Form
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from app.context_files import extract_context_file, normalize_context_filename
from app.canva_connect import CanvaConnectClient, CanvaConnectConfig, canva_status
from app.core import Ollama
from app.cowork import build_status
from app.creative_quality import (
    asset_manifest, automatic_quality_review, build_brand_profile, creative_brief_markdown, creative_view,
    quality_report_markdown, validate_design_url, validate_image_url,
)
from app.external_ai import PROVIDERS, call_provider_with_metadata, provider_statuses, run_capability_reviews, run_plan_reviews, run_research
from app.creative_orchestrator import (
    component_summary, generate_creative_components, public_creative_data,
)
from app.google_premarketing import (
    GoogleAuthenticationRequired, GoogleFormsPublisher, GooglePremarketingConfig,
    GooglePremarketingError, sync_campaign_responses,
)
from app.landing_page import (
    google_sites_automation_payload, landing_page_manifest, manifest_json, render_google_sites_copy,
    render_landing_page_html, validate_google_site_url,
    verify_public_landing_html,
)
from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator
from app.premarketing import capture_and_queue, lead_score as _lead_score
from app.premarketing_monitor import campaign_execution_monitor
from app.project_memos import write_project_memos
from app.public_web_research import PublicWebResearcher
from app.social_premarketing import (
    CHANNELS, social_package, social_package_json, social_package_markdown,
    validate_evidence_url,
)
from app.tabular_data import SafeTableExecutor
from app.workspace_files import WorkspaceSandbox, normalize_workspace_path
from app.yayoi_accounting import analyze_yayoi_workbook

ROOT = Path(__file__).resolve().parents[1]
LOGGER = logging.getLogger(__name__)
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "memory" / "conversations.db"
WORKSPACE_ROOT = Path(os.getenv("WORKSPACE_ROOT", "/workspace"))
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://host.docker.internal:11434")
DEFAULT_OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gpt-oss:20b")
MODEL_SETTINGS_PATH = DATA_DIR / "memory" / "local_model.json"


def load_local_model_selection(default: str = DEFAULT_OLLAMA_MODEL) -> str:
    try:
        data = json.loads(MODEL_SETTINGS_PATH.read_text(encoding="utf-8"))
        selected = str(data.get("model", "")).strip()
        if selected and len(selected) <= 200:
            return selected
    except (OSError, ValueError, TypeError):
        pass
    return default


def save_local_model_selection(model: str) -> None:
    MODEL_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = MODEL_SETTINGS_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"model": model}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(MODEL_SETTINGS_PATH)


OLLAMA_MODEL = load_local_model_selection()
OPEN_WEBUI_INTERNAL_URL = os.getenv("OPEN_WEBUI_INTERNAL_URL", "http://open-webui:8080").rstrip("/")
CPTR_INTERNAL_URL = os.getenv("CPTR_INTERNAL_URL", "http://cptr:8000").rstrip("/")
GOOGLE_BROWSER_INTERNAL_URL = os.getenv(
    "GOOGLE_BROWSER_INTERNAL_URL", "http://google-publisher-browser:8000"
).rstrip("/")
FRONT_AI_PROVIDER = os.getenv("FRONT_AI_PROVIDER", "ollama").strip().lower()
PROJECT_CONTEXT_PROMPT_CHARS = max(10000, min(int(os.getenv("PROJECT_CONTEXT_PROMPT_CHARS", "60000")), 200000))
PROJECT_CONTEXT_STORED_CHARS = 2_000_000
PROJECT_CONTEXT_STORED_BYTES = 100 * 1024 * 1024
FRONT_AI_REASONING_EFFORT = os.getenv("FRONT_AI_REASONING_EFFORT", "high").strip() or None
CONTEXT_TURNS = max(1, min(int(os.getenv("CONTEXT_TURNS", "12")), 50))
CONTEXT_MAX_CHARS = max(2000, min(int(os.getenv("CONTEXT_MAX_CHARS", "24000")), 200000))
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "あなたは全AIの会話コンテキストを管理するフロントAIです。過去の共有コンテキストを踏まえ、日本語で自然かつ正確に回答してください。",
)

memory: ShortTermMemory | None = None
llm: Ollama | None = None
orchestrator: ProjectOrchestrator | None = None
workspace: WorkspaceSandbox | None = None
asr_model = None
engine_enabled = True
executions: dict[str, dict] = {}
model_switch_lock = asyncio.Lock()
model_warmup_task: asyncio.Task | None = None
model_warmup_state = {"status": "pending", "model": OLLAMA_MODEL, "error": ""}
google_publisher: GoogleFormsPublisher
google_sync_task: asyncio.Task | None = None


def start_execution(kind: str, label: str, project_id: str | None = None,
                    project_name: str | None = None, stage: str = "開始") -> str:
    execution_id = uuid.uuid4().hex
    now = time.time()
    executions[execution_id] = {
        "id": execution_id, "kind": kind, "label": label[:200],
        "project_id": project_id, "project_name": project_name,
        "status": "running", "stage": stage, "model": None,
        "error": None, "started_at": now, "updated_at": now,
        "completed_at": None,
    }
    return execution_id


def update_execution(execution_id: str, stage: str, model: str | None = None) -> None:
    item = executions.get(execution_id)
    if not item:
        return
    item["stage"] = stage
    item["updated_at"] = time.time()
    if model:
        item["model"] = model


def finish_execution(execution_id: str, status: str, stage: str,
                     model: str | None = None, error: str | None = None) -> None:
    item = executions.get(execution_id)
    if not item:
        return
    now = time.time()
    item.update({"status": status, "stage": stage, "updated_at": now, "completed_at": now})
    if model:
        item["model"] = model
    if error:
        item["error"] = error[:500]
    completed = sorted(
        (entry for entry in executions.values() if entry["status"] != "running"),
        key=lambda entry: entry["completed_at"] or 0,
        reverse=True,
    )
    for stale in completed[50:]:
        executions.pop(stale["id"], None)


def execution_view(item: dict) -> dict:
    end = item["completed_at"] or time.time()
    return {**item, "elapsed_ms": round((end - item["started_at"]) * 1000, 1)}

class ResearchRequest(BaseModel):
    topic: str = Field(min_length=3, max_length=20000)
    providers: list[str]
    synthesizer: str
    style: str = "report"
    session_id: str | None = Field(default=None, max_length=128)
    project_id: str = Field(default="default", max_length=64)


class LocalModelSelectionPayload(BaseModel):
    model: str = Field(min_length=1, max_length=200)


class ProjectPayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    context_text: str = Field(default="", max_length=100000)
    workspace_path: str = Field(default="", max_length=500)


class MissionPayload(BaseModel):
    goal: str = Field(default="", max_length=20000)
    success_criteria: str = Field(default="", max_length=20000)
    constraints_text: str = Field(default="", max_length=20000)
    allow_external_ai: bool = False
    external_providers: list[str] = Field(default_factory=list, max_length=10)
    max_parallel_tasks: int = Field(default=2, ge=1, le=4)


class MissionInstructionPayload(BaseModel):
    instruction: str = Field(min_length=1, max_length=4000)


class WorkspaceOperationPayload(BaseModel):
    operations: list[dict] = Field(min_length=1, max_length=20)
    workspace_path: str = Field(default="", max_length=500)


class ExternalActionPayload(BaseModel):
    kind: str = Field(min_length=1, max_length=40)
    target: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=50000)
    task_id: str | None = Field(default=None, max_length=64)


class ExternalActionEvidencePayload(BaseModel):
    evidence: str = Field(min_length=3, max_length=10000)


class PremarketingCampaignPayload(BaseModel):
    title: str = Field(min_length=2, max_length=160)
    audience: str = Field(min_length=3, max_length=2000)
    offer: str = Field(min_length=3, max_length=5000)
    call_to_action: str = Field(min_length=2, max_length=500)


class GoogleSiteResultPayload(BaseModel):
    edit_url: str = Field(default="", max_length=2000)
    public_url: str = Field(default="", max_length=2000)


class SocialEvidencePayload(BaseModel):
    public_post_url: str = Field(min_length=8, max_length=2000)


class CreativePackagePayload(BaseModel):
    provider: str = Field(default="canva", pattern="^(canva|local|other)$")
    brand_name: str = Field(default="Local Supporter", max_length=120)
    primary_color: str = Field(default="#155eef", max_length=7)
    secondary_color: str = Field(default="#172033", max_length=7)
    accent_color: str = Field(default="#52d6a7", max_length=7)
    background_color: str = Field(default="#f7faff", max_length=7)
    tone: str = Field(default="信頼感があり、具体的で、過度な効果を約束しない", max_length=500)


class CreativeReviewPayload(BaseModel):
    design_url: str = Field(default="", max_length=2000)
    image_url: str = Field(default="", max_length=4000)
    review_text: str = Field(min_length=10, max_length=10000)
    quality_score: int = Field(ge=0, le=100)


class CreativeOrchestrationPayload(BaseModel):
    providers: list[str] | None = Field(default=None, max_length=5)
    use_canva: bool = True


class LandingRecomposePayload(BaseModel):
    instruction: str = Field(min_length=3, max_length=2000)
    mode: str = Field(default="ai_canva", pattern="^(ai_canva|local)$")


async def _sync_google_campaign(campaign: dict) -> dict:
    try:
        result = await sync_campaign_responses(memory, campaign, google_publisher)
    except GoogleAuthenticationRequired as exc:
        memory.update_campaign_publication(
            campaign["project_id"], campaign["id"], "reauth_required",
            publication_error=str(exc)[:2000],
        )
        raise
    except GooglePremarketingError as exc:
        memory.update_campaign_publication(
            campaign["project_id"], campaign["id"], "failed",
            publication_error=str(exc)[:2000],
        )
        raise
    status = "monitoring" if campaign.get("google_site_url") else "awaiting_site"
    memory.update_campaign_publication(
        campaign["project_id"], campaign["id"], status,
        last_synced_at=result["last_synced_at"], publication_error="",
    )
    if result["fetched"]:
        memory.add_event(
            campaign["project_id"], "google_form_responses_synced",
            f"Googleフォーム回答を同期: 取込{result['imported']}件、重複{result['duplicates']}件、除外{result['rejected']}件",
            detail=json.dumps({**result, "errors": result["errors"]}, ensure_ascii=False)[:12000],
        )
    return result


async def _google_sync_loop() -> None:
    interval = max(30, min(int(os.getenv("GOOGLE_FORM_SYNC_INTERVAL", "300")), 86400))
    while True:
        await asyncio.sleep(interval)
        if not google_publisher.config.ready:
            continue
        for project in memory.list_projects():
            for campaign in memory.list_campaigns(project["id"]):
                if campaign.get("google_form_id") and campaign.get("publication_status") in {
                    "awaiting_site", "published", "monitoring",
                }:
                    try:
                        await _sync_google_campaign(campaign)
                    except GooglePremarketingError:
                        continue


@asynccontextmanager
async def lifespan(_: FastAPI):
    global memory, llm, orchestrator, workspace, google_publisher, google_sync_task
    global model_warmup_task
    memory = ShortTermMemory(DB_PATH)
    recovered_forms = memory.recover_interrupted_form_publications()
    if recovered_forms:
        LOGGER.warning("Marked %s interrupted Google form publication(s) for reconciliation", recovered_forms)
    try:
        from app.ocr_runtime import install_default_runtime
        install_default_runtime()
    except Exception as exc:
        LOGGER.warning("OCR runtime installation failed during startup: %s", exc, exc_info=True)
    llm = Ollama(OLLAMA_URL, OLLAMA_MODEL)
    workspace = WorkspaceSandbox(WORKSPACE_ROOT)
    orchestrator = ProjectOrchestrator(
        memory, llm, project_static_context, run_research, provider_statuses,
        run_plan_reviews, workspace, run_capability_reviews,
        SafeTableExecutor(memory, workspace), PublicWebResearcher(),
    )
    try:
        from app.restart_convergence import reconcile_after_restart
        reconcile_after_restart(memory, orchestrator)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            'restart convergence failed during startup: %s', exc, exc_info=True,
        )
    from app.goal_review_queue import loop as review_loop
    review_queue_task=asyncio.create_task(review_loop(orchestrator))
    google_publisher = GoogleFormsPublisher(GooglePremarketingConfig.from_env())
    google_sync_task = asyncio.create_task(_google_sync_loop())
    if engine_enabled:
        model_warmup_task = asyncio.create_task(_warmup_selected_model())
    for project in memory.list_projects():
        mission = memory.get_mission(project["id"])
        if mission["status"] == "completed":
            unfinished = [
                task for task in mission["tasks"]
                if task["status"] not in {"completed", "skipped"}
            ]
            if unfinished:
                memory.set_mission_status(
                    project["id"], "paused",
                    f"起動時整合性検査で未完了タスク{len(unfinished)}件を検出し、完了状態を解除しました",
                    "mission_state_reconciled",
                )
        if mission["goal"].strip():
            write_project_memos(memory, project["id"])
    try:
        yield
    finally:
        if google_sync_task:
            google_sync_task.cancel()
            try:
                await google_sync_task
            except asyncio.CancelledError:
                pass
        if model_warmup_task and not model_warmup_task.done():
            model_warmup_task.cancel()
            await asyncio.gather(model_warmup_task, return_exceptions=True)
        review_queue_task.cancel()
        await asyncio.gather(review_queue_task,return_exceptions=True)
        await google_publisher.close()
        await orchestrator.shutdown()


app = FastAPI(title="Local Cowork + Local Voice AI", version="1.0.0", lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(ROOT / "app" / "static" / "index.html", headers={"Cache-Control": "no-store, no-cache, must-revalidate"})




@app.get("/static/project_mission.css")
async def project_mission_css():
    return FileResponse(ROOT / "app" / "static" / "project_mission.css", media_type="text/css", headers={"Cache-Control": "no-store, no-cache, must-revalidate"})


@app.get("/static/project_mission.js")
async def project_mission_js():
    return FileResponse(ROOT / "app" / "static" / "project_mission.js", media_type="text/javascript", headers={"Cache-Control": "no-store, no-cache, must-revalidate"})


@app.get("/static/presenters.js")
async def presenters_js():
    return FileResponse(ROOT / "app" / "static" / "presenters.js", media_type="text/javascript", headers={"Cache-Control": "no-store, no-cache, must-revalidate"})

@app.get("/static/local_models.js")
async def local_models_js():
    return FileResponse(ROOT / "app" / "static" / "local_models.js", media_type="text/javascript", headers={"Cache-Control": "no-store, no-cache, must-revalidate"})

@app.get("/cowork")
async def cowork_dashboard():
    return FileResponse(ROOT / "app" / "static" / "cowork.html", headers={"Cache-Control": "no-store, no-cache, must-revalidate"})


def front_provider():
    return PROVIDERS.get(FRONT_AI_PROVIDER)


def require_project(project_id: str) -> dict:
    project = memory.get_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project




def project_static_context(project: dict) -> tuple[str, list[dict]]:
    files = memory.list_context_files(project["id"], include_content=True)
    manifest = ["## 登録資料目録"]
    for item in files:
        manifest.append(
            f"- {item['filename']} | 種別={item['file_kind']} | "
            f"原本={item['size_bytes']} bytes | 抽出={item['char_count']}文字"
            + (f" | {item['extraction_note']}" if item["extraction_note"] else "")
        )
    parts = ["\n".join(manifest)]
    if project["context_text"]:
        parts.append("## 手入力コンテキスト\n" + project["context_text"])
    used = len("\n\n".join(parts))
    # User originals must not be displaced by large generated execution memos.
    ordered_files = sorted(files, key=lambda item: item.get("source") == "memo")
    for item in ordered_files:
        if not item.get("content") or used >= PROJECT_CONTEXT_PROMPT_CHARS:
            continue
        header = f"## 資料本文: {item['filename']} ({item['file_kind']})\n"
        available = PROJECT_CONTEXT_PROMPT_CHARS - used - len(header) - 2
        if available <= 0:
            break
        excerpt = item["content"][:available]
        if len(excerpt) < len(item["content"]):
            excerpt += "\n[本文はコンテキスト上限により省略]"
        section = header + excerpt
        parts.append(section)
        used += len(section) + 2
    return "\n\n".join(parts)[:PROJECT_CONTEXT_PROMPT_CHARS], files


def shared_project_context(project_id: str, session_id: str) -> tuple[dict, str]:
    project = require_project(project_id)
    static_context, _ = project_static_context(project)
    conversation = memory.context_text(session_id, CONTEXT_TURNS, CONTEXT_MAX_CHARS, project_id)
    parts = []
    if static_context:
        parts.append("# プロジェクト固定コンテキスト\n" + static_context)
    if conversation:
        parts.append("## このプロジェクト・セッションの会話履歴\n" + conversation)
    return project, "\n\n".join(parts)


@app.get("/api/projects")
async def list_projects():
    return memory.list_projects()


@app.post("/api/projects", status_code=201)
async def create_project(payload: ProjectPayload):
    name = payload.name.strip()
    if not name:
        raise HTTPException(422, "Project name is required")
    try:
        workspace_path = normalize_workspace_path(payload.workspace_path, allow_empty=True)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return memory.create_project(name, payload.context_text, workspace_path)


@app.put("/api/projects/{project_id}")
async def update_project(project_id: str, payload: ProjectPayload):
    name = payload.name.strip()
    if not name:
        raise HTTPException(422, "Project name is required")
    try:
        workspace_path = normalize_workspace_path(payload.workspace_path, allow_empty=True)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    project = memory.update_project(project_id, name, payload.context_text, workspace_path)
    if not project:
        raise HTTPException(404, "Project not found")
    return project




@app.get("/api/projects/{project_id}/mission")
async def get_project_mission(project_id: str):
    require_project(project_id)
    from app.upgrade_store import UpgradeStore
    result = memory.get_mission(project_id)
    from app.goal_review import execution_gate
    result['execution_gate']=execution_gate(orchestrator,project_id)
    store = UpgradeStore(memory.path)
    from app.upgrade_runtime import task_route_status
    result['task_routes'] = [task_route_status(store, memory.path, project_id, task, result['plan_version'])
                             for task in result['tasks']]
    from app.detail_service import view as detail_view
    result['detailed_tasks'] = []
    for route in result['task_routes']:
        if route['route']=='detailed':
            result['detailed_tasks'].append(detail_view(orchestrator,project_id,route['task_id']))
    result['quality_attempts'] = []
    for attempt in store.latest(project_id):
        aid = attempt['attempt_id']
        sends = store.records('presentation_manifests',project_id,aid)
        result['quality_attempts'].append({
            'task_id': attempt['task_id'], 'attempt_id': aid, 'state': attempt['state'],
            'mode': attempt['mode'], 'started_at': attempt['started_at'],
            'actual_models': sorted({x.get('model') for x in sends if x.get('model')}),
            'actual_requests': len(sends),
            'contract_hash': attempt['contract_hash'],
            'validation': store.records('validation_runs',project_id,aid)[-1:],
            'failures': store.records('failure_records',project_id,aid),
        })
    return result


@app.put("/api/projects/{project_id}/mission")
async def save_project_mission(project_id: str, payload: MissionPayload):
    require_project(project_id)
    current = memory.get_mission(project_id)
    if current["status"] == "running":
        raise HTTPException(409, "実行中は目標を変更できません。先に一時停止してください")
    goal = payload.goal.strip()
    if not goal:
        raise HTTPException(422, "プロジェクト目標を入力してください")
    unknown = [provider for provider in payload.external_providers if provider not in PROVIDERS]
    if unknown:
        raise HTTPException(422, f"不明な外部AI: {', '.join(unknown)}")
    reset_plan = bool(current["tasks"]) and any((
        current["goal"] != goal,
        current["success_criteria"] != payload.success_criteria.strip(),
        current["constraints_text"] != payload.constraints_text.strip(),
    ))
    result = memory.save_mission(
        project_id, goal, payload.success_criteria, payload.constraints_text,
        payload.allow_external_ai, payload.external_providers,
        payload.max_parallel_tasks, reset_plan,
    )
    write_project_memos(memory, project_id)
    return memory.get_mission(project_id)


@app.post("/api/projects/{project_id}/mission/instructions")
async def add_project_mission_instruction(project_id: str, payload: MissionInstructionPayload):
    require_project(project_id)
    try:
        result = memory.add_mission_instruction(project_id, payload.instruction)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    write_project_memos(memory, project_id)
    return result


@app.post("/api/projects/{project_id}/mission/plan/generate")
async def generate_project_plan(project_id: str):
    project = require_project(project_id)
    mission = memory.get_mission(project_id)
    if mission["status"] == "running":
        raise HTTPException(409, "実行中は計画を再生成できません")
    if not engine_enabled or not await llm.health():
        raise HTTPException(503, "計画生成に必要なローカルLLMが利用できません")
    execution_id = start_execution(
        "project_plan", mission["goal"] or project["name"], project_id, project["name"],
        "ローカルLLMが実行計画を作成中",
    )
    try:
        result = await orchestrator.generate_plan(project_id)
        finish_execution(execution_id, "success", "計画生成完了", OLLAMA_MODEL)
        return result
    except (ValueError, KeyError) as exc:
        finish_execution(execution_id, "error", "計画生成エラー", OLLAMA_MODEL, str(exc))
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        finish_execution(execution_id, "error", "計画生成エラー", OLLAMA_MODEL, str(exc))
        raise HTTPException(502, f"計画生成に失敗しました: {exc}") from exc


@app.post("/api/projects/{project_id}/mission/plan/approve")
async def approve_project_plan(project_id: str):
    require_project(project_id)
    try:
        return orchestrator.approve(project_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/projects/{project_id}/mission/start")
async def start_project_mission(project_id: str):
    require_project(project_id)
    if not engine_enabled or not await llm.health():
        raise HTTPException(503, "計画実行に必要なローカルLLMが利用できません")
    try:
        return await orchestrator.start(project_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/projects/{project_id}/mission/pause")
async def pause_project_mission(project_id: str):
    require_project(project_id)
    return await orchestrator.pause(project_id)


@app.post("/api/projects/{project_id}/mission/cancel")
async def cancel_project_mission(project_id: str):
    require_project(project_id)
    return await orchestrator.pause(project_id, cancelled=True)


@app.post("/api/projects/{project_id}/mission/retry")
async def retry_project_mission(project_id: str):
    require_project(project_id)
    try:
        return orchestrator.retry_failed(project_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/projects/{project_id}/mission/verify")
async def verify_project_mission(project_id: str):
    require_project(project_id)
    try:
        return orchestrator.verify_completed(project_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


ALLOWED_EXTERNAL_ACTIONS = {"email", "proposal", "meeting", "poc", "contract", "manual"}


@app.get("/api/projects/{project_id}/actions")
async def list_project_actions(project_id: str):
    require_project(project_id)
    return memory.list_actions(project_id)


@app.post("/api/projects/{project_id}/actions")
async def create_project_action(project_id: str, payload: ExternalActionPayload):
    require_project(project_id)
    kind = payload.kind.strip().lower()
    if kind not in ALLOWED_EXTERNAL_ACTIONS:
        raise HTTPException(400, "未対応の外部アクション種別です")
    if payload.task_id:
        mission = memory.get_mission(project_id)
        if not any(task["id"] == payload.task_id for task in mission["tasks"]):
            raise HTTPException(400, "対象タスクがこのプロジェクトにありません")
    action = memory.create_action(
        project_id, kind, payload.target.strip(), payload.content.strip(), payload.task_id,
    )
    memory.add_event(
        project_id, "external_action_proposed",
        f"外部アクションを承認待ちで登録: {kind} → {payload.target.strip()}",
        payload.task_id, detail=json.dumps({
            "action_id": action["id"], "kind": kind, "target": payload.target.strip(),
        }, ensure_ascii=False),
    )
    return action


@app.post("/api/projects/{project_id}/actions/{action_id}/approve")
async def approve_project_action(project_id: str, action_id: str):
    require_project(project_id)
    action = memory.get_action(project_id, action_id)
    if not action:
        raise HTTPException(404, "外部アクションが見つかりません")
    if action["status"] != "pending_approval":
        raise HTTPException(409, "承認待ちのアクションだけを承認できます")
    updated = memory.update_action(project_id, action_id, "approved")
    memory.add_event(
        project_id, "external_action_approved",
        f"外部アクションを承認: {action['kind']} → {action['target']}",
        action.get("task_id"), detail=f"action_id={action_id}",
    )
    return updated


def _send_approved_email(action: dict) -> str:
    host = os.getenv("SMTP_HOST", "").strip()
    port = int(os.getenv("SMTP_PORT", "587"))
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    sender = os.getenv("SMTP_FROM", username).strip()
    recipient = parseaddr(action["target"])[1]
    if not host or not sender or not recipient or "@" not in recipient:
        raise RuntimeError("SMTP_HOST、SMTP_FROM、有効な送信先が必要です")
    subject, separator, body = action["content"].partition("\n")
    if not separator:
        subject, body = "Local Coworkからのご連絡", subject
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject.removeprefix("件名:").strip()[:200]
    message.set_content(body.strip())
    use_ssl = os.getenv("SMTP_SSL", "false").strip().lower() in {"1", "true", "yes"}
    smtp_type = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    with smtp_type(host, port, timeout=30) as client:
        if not use_ssl and os.getenv("SMTP_STARTTLS", "true").strip().lower() in {"1", "true", "yes"}:
            client.starttls()
        if username:
            client.login(username, password)
        refused = client.send_message(message)
        if refused:
            raise RuntimeError("SMTPサーバーが一部送信先を拒否しました")
    return f"SMTP accepted; recipient={recipient}; subject={message['Subject']}"


@app.post("/api/projects/{project_id}/actions/{action_id}/execute")
async def execute_project_action(project_id: str, action_id: str):
    require_project(project_id)
    action = memory.get_action(project_id, action_id)
    if not action:
        raise HTTPException(404, "外部アクションが見つかりません")
    if action["status"] != "approved":
        raise HTTPException(409, "人間が承認済みのアクションだけを実行できます")
    if action["kind"] != "email":
        raise HTTPException(409, "自動実行できるのはemailです。その他は実施後に証拠を登録してください")
    try:
        evidence = await asyncio.to_thread(_send_approved_email, action)
    except Exception as exc:
        memory.update_action(project_id, action_id, "failed", error=str(exc)[:2000])
        memory.add_event(
            project_id, "external_action_failed", "承認済みメール送信に失敗しました",
            action.get("task_id"), detail=f"action_id={action_id}; error={str(exc)[:1000]}",
        )
        raise HTTPException(502, str(exc)) from exc
    updated = memory.update_action(project_id, action_id, "executed", evidence=evidence)
    memory.add_event(
        project_id, "external_action_executed",
        f"承認済みメールを実行: {action['target']}", action.get("task_id"),
        detail=json.dumps({"action_id": action_id, "evidence": evidence}, ensure_ascii=False),
    )
    return updated


@app.post("/api/projects/{project_id}/actions/{action_id}/complete")
async def complete_project_action(project_id: str, action_id: str,
                                  payload: ExternalActionEvidencePayload):
    require_project(project_id)
    action = memory.get_action(project_id, action_id)
    if not action:
        raise HTTPException(404, "外部アクションが見つかりません")
    if action["status"] != "approved":
        raise HTTPException(409, "承認済みアクションだけに実行証拠を登録できます")
    updated = memory.update_action(
        project_id, action_id, "executed", evidence=payload.evidence.strip(),
    )
    memory.add_event(
        project_id, "external_action_executed",
        f"外部アクションの実行証拠を登録: {action['kind']} → {action['target']}",
        action.get("task_id"), detail=json.dumps({
            "action_id": action_id, "evidence": payload.evidence.strip(),
        }, ensure_ascii=False),
    )
    return updated


@app.get("/api/projects/{project_id}/premarketing")
async def get_premarketing(project_id: str):
    require_project(project_id)
    campaigns = memory.list_campaigns(project_id)
    leads = memory.list_leads(project_id)
    for campaign in campaigns:
        campaign["creative"] = creative_view(
            memory.get_campaign_creative(project_id, campaign["id"])
        )
        campaign["capture_url"] = f"/premarketing/{campaign['public_token']}"
        campaign["landing_preview_url"] = f"/premarketing/{campaign['public_token']}/landing"
        campaign["social_shares"] = memory.list_social_shares(project_id, campaign["id"])
        campaign["monitor"] = campaign_execution_monitor(
            campaign, campaign["social_shares"], leads,
        )
    config = google_publisher.config
    browser_available = False
    browser_state = "stopped"
    try:
        async with httpx.AsyncClient(timeout=2) as browser_client:
            response = await browser_client.get(
                f"{GOOGLE_BROWSER_INTERNAL_URL}/api/config"
            )
        browser_available = response.status_code == 200
        if browser_available:
            browser_config = response.json()
            browser_state = (
                "setup_required" if browser_config.get("needs_setup", True) else "ready"
            )
    except httpx.HTTPError:
        pass
    return {
        "campaigns": campaigns, "leads": leads,
        "creative_production": {
            "runtime": "local_supporter",
            "codex_required": False,
            "providers": provider_statuses(),
            "canva": canva_status(),
        },
        "google": {
            "configured": config.ready,
            "missing": config.missing(),
            "account": config.account_email,
            "browser_available": browser_available,
            "browser_state": browser_state,
        },
    }


@app.post("/api/projects/{project_id}/premarketing/campaigns/{campaign_id}/google/request")
async def request_google_publication(project_id: str, campaign_id: str):
    require_project(project_id)
    campaign = memory.get_campaign(project_id, campaign_id)
    if not campaign:
        raise HTTPException(404, "キャンペーンが見つかりません")
    if campaign.get("google_form_id"):
        raise HTTPException(409, "Googleフォームは作成済みです")
    if campaign.get("publication_status") in {"publishing_form", "failed", "reauth_required"}:
        raise HTTPException(
            409, "前回の作成結果が未確認です。Google側で同名フォームの有無を照合してから復旧してください",
        )
    updated = memory.update_campaign_publication(
        project_id, campaign_id, "awaiting_approval", publication_error="",
    )
    memory.add_event(
        project_id, "google_publication_requested",
        f"Google外部公開を承認待ちへ登録: {campaign['title']}",
        detail=json.dumps({
            "campaign_id": campaign_id,
            "account": google_publisher.config.account_email,
            "scope": "Googleフォーム作成・公開、Google Sites掲載、回答同期",
        }, ensure_ascii=False),
    )
    return updated


@app.post("/api/projects/{project_id}/premarketing/campaigns/{campaign_id}/google/approve")
async def approve_google_publication(project_id: str, campaign_id: str):
    require_project(project_id)
    campaign = memory.get_campaign(project_id, campaign_id)
    if not campaign:
        raise HTTPException(404, "キャンペーンが見つかりません")
    if campaign.get("publication_status") != "awaiting_approval":
        raise HTTPException(409, "承認待ちのGoogle公開だけを承認できます")
    now = datetime.now(timezone.utc).isoformat()
    updated = memory.update_campaign_publication(
        project_id, campaign_id, "approved", publication_approved_at=now,
        publication_error="",
    )
    memory.add_event(
        project_id, "google_publication_approved",
        f"Google外部公開を承認: {campaign['title']}",
        detail=f"campaign_id={campaign_id}",
    )
    return updated


@app.post("/api/projects/{project_id}/premarketing/campaigns/{campaign_id}/google/publish-form")
async def publish_google_form(project_id: str, campaign_id: str):
    require_project(project_id)
    campaign = memory.get_campaign(project_id, campaign_id)
    if not campaign:
        raise HTTPException(404, "キャンペーンが見つかりません")
    if not campaign.get("publication_approved_at"):
        raise HTTPException(409, "人間によるGoogle公開承認が必要です")
    existing_id = str(campaign.get("google_form_id") or "")
    resumed = bool(existing_id)
    if resumed:
        if campaign.get("publication_status") not in {"failed", "reauth_required"}:
            raise HTTPException(409, "Googleフォームは作成済み、または処理中です")
        try:
            claimed = memory.claim_form_resume(project_id, campaign_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    else:
        if campaign.get("publication_status") != "approved":
            raise HTTPException(
                409, "承認済みの新規作成だけを実行できます。ID不明の中断時はGoogle側の作成結果を先に照合してください",
            )
        try:
            claimed = memory.claim_form_publication(project_id, campaign_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    attempts = int(claimed.get("publication_attempts") or 0)
    try:
        if resumed:
            result = await google_publisher.resume_existing(claimed)
        else:
            def checkpoint(form_id, responder_uri):
                memory.update_campaign_publication(
                    project_id, campaign_id, "publishing_form",
                    google_form_id=form_id, google_form_url=responder_uri,
                )
            result = await google_publisher.create_and_publish(claimed, on_created=checkpoint)
    except GoogleAuthenticationRequired as exc:
        updated = memory.update_campaign_publication(
            project_id, campaign_id, "reauth_required",
            publication_attempts=attempts, publication_error=str(exc)[:2000],
        )
        raise HTTPException(409, str(exc)) from exc
    except GooglePremarketingError as exc:
        updated = memory.update_campaign_publication(
            project_id, campaign_id, "failed",
            publication_attempts=attempts, publication_error=str(exc)[:2000],
        )
        raise HTTPException(502, str(exc)) from exc
    updated = memory.update_campaign_publication(
        project_id, campaign_id, "awaiting_site",
        google_form_id=result["form_id"], google_form_url=result["responder_uri"],
        google_question_map=json.dumps(result["question_map"], ensure_ascii=False),
        publication_attempts=attempts, publication_error="",
    )
    memory.add_event(
        project_id, "google_form_published",
        f"Googleフォームを{'既存IDから再開・公開' if resumed else '作成・公開'}: {campaign['title']}",
        detail=json.dumps({
            "campaign_id": campaign_id, "form_id": result["form_id"],
            "responder_uri": result["responder_uri"],
        }, ensure_ascii=False),
    )
    return updated


@app.post("/api/projects/{project_id}/premarketing/campaigns/{campaign_id}/google/site")
async def register_google_site(project_id: str, campaign_id: str,
                               payload: GoogleSiteResultPayload):
    require_project(project_id)
    campaign = memory.get_campaign(project_id, campaign_id)
    if not campaign:
        raise HTTPException(404, "キャンペーンが見つかりません")
    if not campaign.get("google_form_id"):
        raise HTTPException(409, "先にGoogleフォームを公開してください")
    if not campaign.get("site_publication_approved_at"):
        raise HTTPException(409, "ランディングページ公開の人間承認が必要です")
    edit_url, public_url = payload.edit_url.strip(), payload.public_url.strip()
    if not public_url:
        raise HTTPException(400, "公開済みGoogle Sites URLを指定してください")
    try:
        if edit_url:
            validate_google_site_url(edit_url, edit=True)
        validate_google_site_url(public_url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if campaign.get("site_publication_status") == "published":
        if (campaign.get("google_site_url") == public_url
                and (not campaign.get("landing_asset_version")
                     or campaign.get("landing_asset_version") == campaign.get("published_asset_version"))):
            return campaign
        raise HTTPException(
            409, "公開済みLPのURLや版を上書きできません。改訂を作成して再承認してください",
        )
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            response = await client.get(public_url, headers={"User-Agent": "LocalSupporter/1.0"})
    except httpx.HTTPError as exc:
        memory.update_campaign_site(
            project_id, campaign_id, "failed", site_publication_error="公開URLへ接続できません",
        )
        raise HTTPException(502, "Google Sites公開URLへ接続できません") from exc
    campaign["creative"] = creative_view(
        memory.get_campaign_creative(project_id, campaign_id)
    )
    expected_version = landing_page_manifest(campaign)["asset_version"]
    checks = verify_public_landing_html(response.text, campaign) if response.status_code == 200 else {}
    if response.status_code != 200 or not all(checks.values()):
        failed_checks = [key for key, passed in checks.items() if not passed]
        detail = "、".join(failed_checks) or f"HTTP {response.status_code}"
        message = f"公開LPが承認済み原案と一致しません: {detail}"
        memory.update_campaign_site(
            project_id, campaign_id, "failed",
            site_publication_error=message,
        )
        raise HTTPException(409, message)
    now = datetime.now(timezone.utc).isoformat()
    updated = memory.update_campaign_publication(
        project_id, campaign_id, "monitoring", google_site_edit_url=edit_url,
        google_site_url=public_url, published_at=now, publication_error="",
    )
    updated = memory.update_campaign_site(
        project_id, campaign_id, "published", google_site_edit_url=edit_url,
        google_site_url=public_url, published_at=now, site_publication_error="",
        landing_revision_status="published",
        landing_asset_version=expected_version,
        published_asset_version=expected_version,
    )
    memory.add_event(
        project_id, "google_site_registered",
        f"Google Sites公開結果を登録: {campaign['title']}",
        detail=json.dumps({
            "campaign_id": campaign_id, "public_url": public_url,
        }, ensure_ascii=False),
    )
    return updated


@app.post("/api/projects/{project_id}/premarketing/campaigns/{campaign_id}/landing/recompose")
async def recompose_landing_page(project_id: str, campaign_id: str,
                                 payload: LandingRecomposePayload):
    project = require_project(project_id)
    campaign = memory.get_campaign(project_id, campaign_id)
    if not campaign:
        raise HTTPException(404, "キャンペーンが見つかりません")
    if campaign.get("site_publication_status") != "published" or not campaign.get("google_site_url"):
        raise HTTPException(409, "公開済みLPだけを再構成できます")
    if payload.mode == "ai_canva" and not CanvaConnectConfig.from_env().configured:
        status = canva_status()
        raise HTTPException(
            409, status["message"] + "。設定後に再実行するか、ローカル再構成を選択してください",
        )
    instruction = payload.instruction.strip()
    revision = int(campaign.get("landing_revision") or 0) + 1
    now = datetime.now(timezone.utc).isoformat()
    base = f"premarketing/{campaign_id}/landing/revisions/{revision}"
    snapshot = {
        "revision": revision, "created_at": now,
        "instruction": instruction, "mode": payload.mode,
        "public_url": campaign.get("google_site_url") or "",
        "edit_url": campaign.get("google_site_edit_url") or "",
        "campaign": {key: campaign.get(key) for key in (
            "title", "audience", "offer", "call_to_action", "landing_assets_path",
        )},
        "creative": creative_view(memory.get_campaign_creative(project_id, campaign_id)),
    }
    try:
        workspace.apply_operations(project.get("workspace_path", ""), project_id, [{
            "action": "write_text", "path": f"{base}/revision_request.json",
            "content": json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        }])
    except (OSError, ValueError) as exc:
        raise HTTPException(409, f"現在のLP状態を保存できません: {exc}") from exc
    memory.update_campaign_site(
        project_id, campaign_id, "published", landing_revision=revision,
        landing_revision_status="generating", landing_revision_instruction=instruction,
        landing_revision_mode=payload.mode, landing_revision_started_at=now,
        site_publication_approved_at=None, site_publication_error="",
        landing_asset_version="",
    )
    try:
        package = await build_creative_package(
            project_id, campaign_id, CreativePackagePayload(
                provider="canva" if payload.mode == "ai_canva" else "local",
                tone=("LP再構成方針: " + instruction)[:500],
            ),
        )
        if payload.mode == "ai_canva":
            generated = await orchestrate_creative(
                project_id, campaign_id, CreativeOrchestrationPayload(use_canva=True),
            )
        else:
            generated = await auto_generate_creative(project_id, campaign_id)
    except Exception as exc:
        memory.update_campaign_site(
            project_id, campaign_id, "published", landing_revision_status="failed",
            site_publication_error=str(exc)[:2000],
        )
        raise
    updated = memory.update_campaign_site(
        project_id, campaign_id, "published", landing_revision_status="review_ready",
        site_publication_error="",
    )
    memory.add_event(
        project_id, "landing_recompose_ready",
        f"公開中LPを維持して再構成案を生成: {campaign['title']} / 改訂{revision}",
        detail=json.dumps({"campaign_id": campaign_id, "revision": revision,
                           "mode": payload.mode}, ensure_ascii=False),
    )
    return {
        "campaign": updated, "revision": revision, "package": package,
        "generated": generated,
        "preview_url": f"/premarketing/{campaign['public_token']}/creative/preview",
        "public_site_preserved": True, "human_approval_required": True,
    }


@app.post("/api/projects/{project_id}/premarketing/campaigns/{campaign_id}/creative/package")
async def build_creative_package(project_id: str, campaign_id: str,
                                 payload: CreativePackagePayload):
    project = require_project(project_id)
    campaign = memory.get_campaign(project_id, campaign_id)
    if not campaign:
        raise HTTPException(404, "キャンペーンが見つかりません")
    try:
        brand = build_brand_profile(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    base = f"premarketing/{campaign_id}/creative"
    brief = creative_brief_markdown(campaign, brand, payload.provider)
    manifest = asset_manifest(campaign, brand, payload.provider)
    report = quality_report_markdown(campaign, brand)
    try:
        audit = workspace.apply_operations(project.get("workspace_path", ""), project_id, [
            {"action": "write_text", "path": f"{base}/creative_brief.md", "content": brief},
            {"action": "write_text", "path": f"{base}/brand_profile.json",
             "content": json.dumps(brand, ensure_ascii=False, indent=2) + "\n"},
            {"action": "write_text", "path": f"{base}/asset_manifest.json",
             "content": json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"},
            {"action": "write_text", "path": f"{base}/quality_report.md", "content": report},
        ])
    except (OSError, ValueError) as exc:
        raise HTTPException(409, f"品質向上資材を保存できません: {exc}") from exc
    updated = creative_view(memory.update_campaign_creative(
        project_id, campaign_id, "brief_ready", provider=payload.provider,
        brand_json=json.dumps(brand, ensure_ascii=False), brief_path=f"{base}/creative_brief.md",
        manifest_path=f"{base}/asset_manifest.json",
        quality_report_path=f"{base}/quality_report.md", design_url="", image_url="",
        review_text="", quality_score=0, error="", approved_at=None,
    ))
    if campaign.get("site_publication_status") != "published" and campaign.get("landing_assets_path"):
        memory.update_campaign_site(
            project_id, campaign_id, "draft_ready",
            site_publication_approved_at=None, site_publication_error="",
        )
    memory.add_event(
        project_id, "creative_package_created",
        f"クリエイティブ品質向上資材を生成: {campaign['title']}",
        detail=json.dumps({"campaign_id": campaign_id, "provider": payload.provider,
                           "artifacts": audit}, ensure_ascii=False)[:12000],
    )
    return {"creative": updated, "brief": brief, "artifacts": audit,
            "canva_url": "https://www.canva.com/", "minimum_quality_score": 70}


@app.post("/api/projects/{project_id}/premarketing/campaigns/{campaign_id}/creative/review")
async def register_creative_review(project_id: str, campaign_id: str,
                                   payload: CreativeReviewPayload):
    project = require_project(project_id)
    campaign = memory.get_campaign(project_id, campaign_id)
    creative = memory.get_campaign_creative(project_id, campaign_id)
    if not campaign or not creative:
        raise HTTPException(404, "品質向上資材が見つかりません")
    if creative.get("status") not in {"brief_ready", "reviewed", "failed"}:
        raise HTTPException(409, "レビュー登録できる状態ではありません")
    try:
        design_url = validate_design_url(payload.design_url, creative.get("provider") or "canva")
        image_url = validate_image_url(payload.image_url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    brand = creative_view(creative).get("brand") or build_brand_profile()
    report = quality_report_markdown(
        campaign, brand, score=payload.quality_score, review=payload.review_text,
    )
    try:
        workspace.apply_operations(project.get("workspace_path", ""), project_id, [
            {"action": "write_text", "path": creative["quality_report_path"], "content": report},
        ])
    except (OSError, ValueError) as exc:
        raise HTTPException(409, f"品質レポートを更新できません: {exc}") from exc
    updated = creative_view(memory.update_campaign_creative(
        project_id, campaign_id, "reviewed", design_url=design_url, image_url=image_url,
        review_text=payload.review_text.strip(), quality_score=payload.quality_score,
        error="", approved_at=None,
    ))
    memory.add_event(
        project_id, "creative_review_registered",
        f"クリエイティブレビューを登録: {campaign['title']} / {payload.quality_score}点",
        detail=json.dumps({"campaign_id": campaign_id, "design_url": design_url,
                           "image_url": image_url}, ensure_ascii=False)[:12000],
    )
    return updated


@app.post("/api/projects/{project_id}/premarketing/campaigns/{campaign_id}/creative/auto-generate")
async def auto_generate_creative(project_id: str, campaign_id: str):
    project = require_project(project_id)
    campaign = memory.get_campaign(project_id, campaign_id)
    stored = memory.get_campaign_creative(project_id, campaign_id)
    if not campaign or not stored:
        raise HTTPException(404, "先にクリエイティブ制作仕様を生成してください")
    if stored.get("status") not in {"brief_ready", "reviewed", "failed"}:
        raise HTTPException(409, "自動デザインを生成できる状態ではありません")
    creative = creative_view(stored)
    brand = creative.get("brand") or build_brand_profile()
    assessment = automatic_quality_review(campaign, brand)
    report = quality_report_markdown(
        campaign, brand, score=assessment["score"], review=assessment["review"],
    )
    preview_creative = dict(creative)
    preview_creative.update({
        "status": "approved", "provider": "local", "design_url": "", "image_url": "",
        "quality_score": assessment["score"], "review_text": assessment["review"],
    })
    preview_campaign = dict(campaign)
    preview_campaign["creative"] = preview_creative
    base = f"premarketing/{campaign_id}/creative"
    try:
        audit = workspace.apply_operations(project.get("workspace_path", ""), project_id, [
            {"action": "write_text", "path": f"{base}/auto_design_preview.html",
             "content": render_landing_page_html(preview_campaign)},
            {"action": "write_text", "path": f"{base}/auto_design_review.json",
             "content": json.dumps(assessment, ensure_ascii=False, indent=2) + "\n"},
            {"action": "write_text", "path": creative["quality_report_path"], "content": report},
        ])
    except (OSError, ValueError) as exc:
        raise HTTPException(409, f"自動デザインを保存できません: {exc}") from exc
    updated = creative_view(memory.update_campaign_creative(
        project_id, campaign_id, "reviewed", provider="local", design_url="", image_url="",
        review_text=assessment["review"], quality_score=assessment["score"],
        error="", approved_at=None,
    ))
    if campaign.get("site_publication_status") != "published":
        memory.update_campaign_site(
            project_id, campaign_id, "draft_ready",
            site_publication_approved_at=None, site_publication_error="",
        )
    memory.add_event(
        project_id, "creative_auto_generated",
        f"ローカルAIデザインと自動レビューを生成: {campaign['title']} / {assessment['score']}点",
        detail=json.dumps({"campaign_id": campaign_id, "checks": assessment["checks"],
                           "artifacts": audit}, ensure_ascii=False)[:12000],
    )
    return {
        "creative": updated,
        "preview_url": f"/premarketing/{campaign['public_token']}/creative/preview",
        "artifacts": audit,
        "human_approval_required": True,
    }


@app.post("/api/projects/{project_id}/premarketing/campaigns/{campaign_id}/creative/orchestrate")
async def orchestrate_creative(
    project_id: str, campaign_id: str, payload: CreativeOrchestrationPayload,
):
    project = require_project(project_id)
    campaign = memory.get_campaign(project_id, campaign_id)
    stored = memory.get_campaign_creative(project_id, campaign_id)
    if not campaign or not stored:
        raise HTTPException(404, "先にクリエイティブ制作仕様を生成してください")
    if stored.get("status") not in {"brief_ready", "reviewed", "failed"}:
        raise HTTPException(409, "部品を自動生成できる状態ではありません")
    mission = memory.get_mission(project_id)
    allowed = list(mission.get("external_providers") or []) if mission.get("allow_external_ai") else []
    requested = allowed if payload.providers is None else payload.providers
    denied = [provider for provider in requested if provider not in allowed]
    if denied:
        raise HTTPException(403, f"外部AIの利用許可がありません: {', '.join(denied)}")
    creative = creative_view(stored)
    brand = creative.get("brand") or build_brand_profile()
    components = await generate_creative_components(campaign, brand, requested)
    successful_components = [item for item in components if item.get("ok")]
    canva_result: dict[str, Any] | None = None
    canva_error = ""
    canva_config = CanvaConnectConfig.from_env()
    if payload.use_canva and canva_config.configured:
        client = CanvaConnectClient(canva_config)
        try:
            canva_result = await client.create_from_public_data(
                public_creative_data(campaign, brand)
            )
        except Exception as exc:
            canva_error = str(exc)[:2000]
        finally:
            await client.close()
    elif payload.use_canva:
        canva_error = "Canva Connect API未設定のためローカル組版へフォールバック"
    assessment = automatic_quality_review(campaign, brand)
    review_parts = [component_summary(components), assessment["review"]]
    if canva_result:
        review_parts.insert(1, "Canvaブランドテンプレートへの自動流込みが完了しました。")
    elif canva_error:
        review_parts.insert(1, canva_error)
    review = "\n".join(review_parts)
    report = quality_report_markdown(
        campaign, brand, score=assessment["score"], review=review,
    )
    provider = "canva" if canva_result and canva_result.get("design_url") else "local"
    design_url = str((canva_result or {}).get("design_url") or "")
    preview_creative = dict(creative)
    preview_creative.update({
        "status": "approved", "provider": provider, "design_url": design_url,
        "image_url": "", "quality_score": assessment["score"], "review_text": review,
    })
    preview_campaign = dict(campaign)
    preview_campaign["creative"] = preview_creative
    base = f"premarketing/{campaign_id}/creative"
    production_manifest = {
        "version": 1,
        "runtime": "local_supporter",
        "codex_used": False,
        "public_data_fields": ["title", "audience", "offer", "call_to_action", "brand"],
        "requested_providers": requested,
        "components": components,
        "canva": canva_result,
        "canva_error": canva_error,
        "fallback": provider == "local",
        "quality": assessment,
    }
    try:
        audit = workspace.apply_operations(project.get("workspace_path", ""), project_id, [
            {"action": "write_text", "path": f"{base}/component_production.json",
             "content": json.dumps(production_manifest, ensure_ascii=False, indent=2) + "\n"},
            {"action": "write_text", "path": f"{base}/auto_design_preview.html",
             "content": render_landing_page_html(preview_campaign)},
            {"action": "write_text", "path": creative["quality_report_path"], "content": report},
        ])
    except (OSError, ValueError) as exc:
        raise HTTPException(409, f"部品・デザイン成果物を保存できません: {exc}") from exc
    updated = creative_view(memory.update_campaign_creative(
        project_id, campaign_id, "reviewed", provider=provider,
        design_url=design_url, image_url="", review_text=review,
        quality_score=assessment["score"], error="", approved_at=None,
    ))
    if campaign.get("site_publication_status") != "published":
        memory.update_campaign_site(
            project_id, campaign_id, "draft_ready",
            site_publication_approved_at=None, site_publication_error="",
        )
    memory.add_event(
        project_id, "creative_components_orchestrated",
        f"各AI部品生成と組版を完了: {campaign['title']} / {len(successful_components)}部品",
        detail=json.dumps({
            "campaign_id": campaign_id, "providers": requested,
            "successful_components": len(successful_components),
            "canva": bool(canva_result), "fallback": provider == "local",
            "codex_used": False, "artifacts": audit,
        }, ensure_ascii=False)[:12000],
    )
    return {
        "creative": updated, "components": components, "canva": canva_result,
        "canva_error": canva_error, "fallback": provider == "local",
        "preview_url": f"/premarketing/{campaign['public_token']}/creative/preview",
        "artifacts": audit, "human_approval_required": True, "codex_used": False,
    }


@app.post("/api/projects/{project_id}/premarketing/campaigns/{campaign_id}/creative/approve")
async def approve_creative(project_id: str, campaign_id: str):
    require_project(project_id)
    campaign = memory.get_campaign(project_id, campaign_id)
    creative = memory.get_campaign_creative(project_id, campaign_id)
    if not campaign or not creative:
        raise HTTPException(404, "品質レビューが見つかりません")
    if creative.get("status") != "reviewed":
        raise HTTPException(409, "レビュー済みのデザインだけを承認できます")
    if int(creative.get("quality_score") or 0) < 70:
        raise HTTPException(409, "品質スコア70以上になるようデザインを修正してください")
    now = datetime.now(timezone.utc).isoformat()
    updated = creative_view(memory.update_campaign_creative(
        project_id, campaign_id, "approved", approved_at=now, error="",
    ))
    if campaign.get("landing_revision_status") == "review_ready":
        memory.update_campaign_site(
            project_id, campaign_id, campaign.get("site_publication_status") or "published",
            landing_revision_status="creative_approved",
        )
    memory.add_event(
        project_id, "creative_approved",
        f"LPクリエイティブを承認: {campaign['title']}",
        detail=f"campaign_id={campaign_id}; score={creative['quality_score']}",
    )
    return updated


@app.post("/api/projects/{project_id}/premarketing/campaigns/{campaign_id}/google/site/package")
async def build_google_site_package(project_id: str, campaign_id: str):
    project = require_project(project_id)
    campaign = memory.get_campaign(project_id, campaign_id)
    if not campaign:
        raise HTTPException(404, "キャンペーンが見つかりません")
    if not campaign.get("google_form_id") or not campaign.get("google_form_url"):
        raise HTTPException(409, "先にGoogleフォームを公開してください")
    if not campaign.get("publication_approved_at"):
        raise HTTPException(409, "Google公開の人間承認が必要です")
    creative = creative_view(memory.get_campaign_creative(project_id, campaign_id))
    if not creative or creative.get("status") != "approved":
        raise HTTPException(409, "先にデザイン品質向上を実施し、レビュー結果を承認してください")
    campaign["creative"] = creative
    base = f"premarketing/{campaign_id}/landing"
    manifest = landing_page_manifest(campaign)
    try:
        audit = workspace.apply_operations(project.get("workspace_path", ""), project_id, [
            {"action": "write_text", "path": f"{base}/landing_page.html",
             "content": render_landing_page_html(campaign)},
            {"action": "write_text", "path": f"{base}/google_sites_copy.md",
             "content": render_google_sites_copy(campaign)},
            {"action": "write_text", "path": f"{base}/landing_manifest.json",
             "content": manifest_json(campaign)},
        ])
    except (OSError, ValueError) as exc:
        raise HTTPException(409, f"LP成果物を保存できません: {exc}") from exc
    updated = memory.update_campaign_site(
        project_id, campaign_id, "draft_ready", landing_assets_path=base,
        site_publication_approved_at=None, site_publication_error="",
        landing_revision_status="draft_ready",
        landing_asset_version=manifest["asset_version"],
    )
    memory.add_event(
        project_id, "google_site_package_created",
        f"Google Sites用ランディングページ資材を生成: {campaign['title']}",
        detail=json.dumps({"campaign_id": campaign_id, "artifacts": audit}, ensure_ascii=False)[:12000],
    )
    return {
        "campaign": updated, "manifest": manifest, "artifacts": audit,
        "preview_url": f"/premarketing/{campaign['public_token']}/landing",
        "publisher_browser_url": "http://127.0.0.1:8010",
        "google_sites_new_url": "https://sites.google.com/new",
    }


@app.get("/api/google-sites/automation/draft")
async def get_google_sites_automation_draft():
    candidates: list[tuple[str, dict]] = []
    for project in memory.list_projects():
        for campaign in memory.list_campaigns(project["id"]):
            if (
                campaign.get("site_publication_status") == "approved"
                and campaign.get("publication_approved_at")
                and campaign.get("site_publication_approved_at")
                and campaign.get("google_form_url")
                and (not campaign.get("google_site_url") or
                     campaign.get("landing_revision_status") == "approved")
                and (memory.get_campaign_creative(project["id"], campaign["id"]) or {}).get("status") == "approved"
            ):
                candidates.append((project["id"], campaign))
    if not candidates:
        raise HTTPException(404, "公開承認済みのGoogle Sites下書きがありません")
    if len(candidates) > 1:
        raise HTTPException(409, "公開承認済み下書きが複数あります。対象を1件に絞ってください")
    project_id, campaign = candidates[0]
    campaign["creative"] = creative_view(
        memory.get_campaign_creative(project_id, campaign["id"])
    )
    payload = google_sites_automation_payload(campaign)
    payload["project_id"] = project_id
    payload["landing_revision"] = int(campaign.get("landing_revision") or 0)
    payload["is_update"] = bool(campaign.get("google_site_url"))
    payload["google_site_edit_url"] = campaign.get("google_site_edit_url") or ""
    if campaign.get("landing_asset_version") != payload["asset_version"]:
        raise HTTPException(409, "保存済みLP版と配置用データが一致しません。LP資材を再生成してください")
    return JSONResponse(
        payload,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.post("/api/projects/{project_id}/premarketing/campaigns/{campaign_id}/google/site/request")
async def request_google_site_publication(project_id: str, campaign_id: str):
    require_project(project_id)
    campaign = memory.get_campaign(project_id, campaign_id)
    if not campaign:
        raise HTTPException(404, "キャンペーンが見つかりません")
    if not campaign.get("landing_assets_path"):
        raise HTTPException(409, "先にランディングページ資材を生成してください")
    if not campaign.get("landing_asset_version"):
        raise HTTPException(409, "LP版情報がありません。LP資材を再生成してください")
    if (memory.get_campaign_creative(project_id, campaign_id) or {}).get("status") != "approved":
        raise HTTPException(409, "先にデザイン品質レビューを承認してください")
    updated = memory.update_campaign_site(
        project_id, campaign_id, "awaiting_approval",
        site_publication_approved_at=None, site_publication_error="",
        landing_revision_status="awaiting_approval",
    )
    memory.add_event(
        project_id, "google_site_publication_requested",
        f"Google Sitesランディングページ公開を承認待ちへ登録: {campaign['title']}",
        detail=f"campaign_id={campaign_id}",
    )
    return updated


@app.post("/api/projects/{project_id}/premarketing/campaigns/{campaign_id}/google/site/approve")
async def approve_google_site_publication(project_id: str, campaign_id: str):
    require_project(project_id)
    campaign = memory.get_campaign(project_id, campaign_id)
    if not campaign:
        raise HTTPException(404, "キャンペーンが見つかりません")
    if campaign.get("site_publication_status") != "awaiting_approval":
        raise HTTPException(409, "承認待ちのランディングページ公開だけを承認できます")
    if not campaign.get("landing_asset_version"):
        raise HTTPException(409, "LP版情報がありません。LP資材を再生成してください")
    if (memory.get_campaign_creative(project_id, campaign_id) or {}).get("status") != "approved":
        raise HTTPException(409, "デザイン品質が承認されていません")
    now = datetime.now(timezone.utc).isoformat()
    updated = memory.update_campaign_site(
        project_id, campaign_id, "approved", site_publication_approved_at=now,
        site_publication_error="", landing_revision_status="approved",
    )
    memory.add_event(
        project_id, "google_site_publication_approved",
        f"Google Sitesランディングページ公開を承認: {campaign['title']}",
        detail=f"campaign_id={campaign_id}",
    )
    return updated


@app.post("/api/projects/{project_id}/premarketing/campaigns/{campaign_id}/social/package")
async def build_social_package(project_id: str, campaign_id: str):
    project = require_project(project_id)
    campaign = memory.get_campaign(project_id, campaign_id)
    if not campaign:
        raise HTTPException(404, "キャンペーンが見つかりません")
    if campaign.get("site_publication_status") != "published" or not campaign.get("google_site_url"):
        raise HTTPException(409, "先にGoogle Sitesの公開URLを検証・登録してください")
    previous = memory.list_social_shares(project_id, campaign_id)
    if any(item.get("status") == "evidence_registered" or item.get("evidence_url")
           for item in previous):
        raise HTTPException(
            409, "公開投稿URLが登録済みのため既存キットは再生成できません。新しいキャンペーン版を作成してください",
        )
    try:
        items = social_package(campaign)
        base = f"premarketing/{campaign_id}/social"
        operations = []
        archive_paths = []
        if previous:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            archive_base = f"{base}/archive/{stamp}"
            archive_paths = [
                f"{archive_base}/social_post_kit.md",
                f"{archive_base}/social_post_manifest.json",
            ]
            operations.extend([
                {"action": "write_text", "path": archive_paths[0],
                 "content": social_package_markdown(campaign, previous)},
                {"action": "write_text", "path": archive_paths[1],
                 "content": social_package_json(previous)},
            ])
        operations.extend([
            {"action": "write_text", "path": f"{base}/social_post_kit.md",
             "content": social_package_markdown(campaign, items)},
            {"action": "write_text", "path": f"{base}/social_post_manifest.json",
             "content": social_package_json(items)},
        ])
        audit = workspace.apply_operations(
            project.get("workspace_path", ""), project_id, operations,
        )
        shares = memory.save_social_drafts(project_id, campaign_id, items)
    except (OSError, ValueError) as exc:
        raise HTTPException(409, f"SNS投稿キットを生成できません: {exc}") from exc
    memory.add_event(
        project_id, "social_package_regenerated" if previous else "social_package_created",
        f"SNS投稿キットを{'再生成' if previous else '生成'}: {campaign['title']}",
        detail=json.dumps({"campaign_id": campaign_id, "channels": list(CHANNELS),
                           "regenerated": bool(previous),
                           "previous": [
                               {key: item.get(key) for key in (
                                   "channel", "status", "open_count", "evidence_url",
                                   "approved_at", "last_opened_at",
                               )}
                               for item in previous
                           ],
                           "archive_paths": archive_paths,
                           "artifacts": audit}, ensure_ascii=False)[:12000],
    )
    return {"shares": shares, "artifacts": audit, "assets_path": base,
            "regenerated": bool(previous), "archive_paths": archive_paths}


@app.post("/api/projects/{project_id}/premarketing/campaigns/{campaign_id}/social/request")
async def request_social_approval(project_id: str, campaign_id: str):
    require_project(project_id)
    campaign = memory.get_campaign(project_id, campaign_id)
    if not campaign:
        raise HTTPException(404, "キャンペーンが見つかりません")
    try:
        shares = memory.update_social_campaign_status(
            project_id, campaign_id, "draft_ready", "awaiting_approval",
        )
    except ValueError as exc:
        raise HTTPException(409, "承認申請できるSNS文案がありません") from exc
    memory.add_event(
        project_id, "social_publication_requested",
        f"SNS投稿文案を承認待ちへ登録: {campaign['title']}",
        detail=json.dumps({"campaign_id": campaign_id, "channels": list(CHANNELS)},
                          ensure_ascii=False),
    )
    return {"shares": shares}


@app.post("/api/projects/{project_id}/premarketing/campaigns/{campaign_id}/social/approve")
async def approve_social_package(project_id: str, campaign_id: str):
    require_project(project_id)
    campaign = memory.get_campaign(project_id, campaign_id)
    if not campaign:
        raise HTTPException(404, "キャンペーンが見つかりません")
    if campaign.get("site_publication_status") != "published" or not campaign.get("google_site_url"):
        raise HTTPException(409, "公開済みLPが確認できないためSNS文案を承認できません")
    try:
        shares = memory.update_social_campaign_status(
            project_id, campaign_id, "awaiting_approval", "approved",
        )
    except ValueError as exc:
        raise HTTPException(409, "承認待ちのSNS文案がありません") from exc
    memory.add_event(
        project_id, "social_publication_approved",
        f"SNS投稿文案を承認: {campaign['title']}",
        detail=json.dumps({"campaign_id": campaign_id, "channels": list(CHANNELS)},
                          ensure_ascii=False),
    )
    return {"shares": shares}


@app.post("/api/projects/{project_id}/premarketing/campaigns/{campaign_id}/social/{channel}/open")
async def open_social_composer(project_id: str, campaign_id: str, channel: str):
    require_project(project_id)
    if channel not in CHANNELS:
        raise HTTPException(404, "対応していないSNSです")
    try:
        share = memory.mark_social_opened(project_id, campaign_id, channel)
    except ValueError as exc:
        raise HTTPException(409, "承認済みのSNS文案だけを利用できます") from exc
    memory.add_event(
        project_id, "social_composer_opened",
        f"{share['label']}の投稿操作を開始（投稿成功は未確認）",
        detail=json.dumps({"campaign_id": campaign_id, "channel": channel,
                           "open_count": share["open_count"]}, ensure_ascii=False),
    )
    return {key: share[key] for key in (
        "channel", "label", "mode", "post_text", "tracking_url", "compose_url",
        "status", "open_count",
    )}


@app.post("/api/projects/{project_id}/premarketing/campaigns/{campaign_id}/social/{channel}/evidence")
async def register_social_evidence(project_id: str, campaign_id: str, channel: str,
                                   payload: SocialEvidencePayload):
    require_project(project_id)
    try:
        evidence_url = validate_evidence_url(channel, payload.public_post_url)
        previous = memory.get_social_share(project_id, campaign_id, channel)
        share = memory.register_social_evidence(
            project_id, campaign_id, channel, evidence_url,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not (previous and previous.get("status") == "evidence_registered"
            and previous.get("evidence_url") == evidence_url):
        memory.add_event(
            project_id, "social_evidence_registered",
            f"{share['label']}の公開投稿URLを登録",
            detail=json.dumps({"campaign_id": campaign_id, "channel": channel,
                               "public_post_url": evidence_url}, ensure_ascii=False),
        )
    return share


@app.post("/api/projects/{project_id}/premarketing/campaigns/{campaign_id}/google/sync")
async def sync_google_form(project_id: str, campaign_id: str):
    require_project(project_id)
    campaign = memory.get_campaign(project_id, campaign_id)
    if not campaign:
        raise HTTPException(404, "キャンペーンが見つかりません")
    try:
        return await _sync_google_campaign(campaign)
    except GoogleAuthenticationRequired as exc:
        raise HTTPException(409, str(exc)) from exc
    except GooglePremarketingError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/projects/{project_id}/premarketing/campaigns")
async def create_premarketing_campaign(project_id: str,
                                       payload: PremarketingCampaignPayload):
    project = require_project(project_id)
    campaign = memory.create_campaign(
        project_id, payload.title.strip(), payload.audience.strip(),
        payload.offer.strip(), payload.call_to_action.strip(), "",
    )
    base = f"premarketing/{campaign['id']}"
    campaign_markdown = f"""# {payload.title.strip()}

## 対象顧客

{payload.audience.strip()}

## 提供価値

{payload.offer.strip()}

## CTA

{payload.call_to_action.strip()}

## 実行フロー

1. 公開前に訴求、個人情報の取扱い、連絡方法を確認する。
2. `{f'/premarketing/{campaign['public_token']}'}` を承認済みチャネルへ掲載する。
3. 同意付き回答をローカルDBへ保存し、100点満点で自動評価する。
4. 60点以上のリードは承認待ちメールアクションへ自動登録する。
5. 人間承認後に連絡し、実行証拠を目標判定へ接続する。

## 安全条件

- 同意のない連絡先を営業利用しない。
- 架空の企業、担当者、回答、商談実績を生成しない。
- 外部公開と送信は明示承認後に行う。
"""
    landing_copy = f"""# {payload.title.strip()}

## こんな課題はありませんか

{payload.audience.strip()}

## ご提供できること

{payload.offer.strip()}

## 次のステップ

{payload.call_to_action.strip()}

入力情報は相談対応とサービス改善のために利用し、同意なく第三者へ提供しません。
"""
    calendar = (
        "週,目的,コンテンツ,CTA,KPI,状態\n"
        "1,課題認知,業務課題チェックリスト,診断フォーム閲覧,閲覧数,未実施\n"
        "2,解決策理解,ローカルAI活用解説,相談フォーム送信,リード数,未実施\n"
        "3,信頼形成,匿名ケーススタディ,個別相談予約,有望リード数,未実施\n"
        "4,提案移行,PoC設計ガイド,提案相談,承認済み連絡数,未実施\n"
    )
    audit = workspace.apply_operations(project.get("workspace_path", ""), project_id, [
        {"action": "write_text", "path": f"{base}/campaign.md", "content": campaign_markdown},
        {"action": "write_text", "path": f"{base}/landing_copy.md", "content": landing_copy},
        {"action": "write_text", "path": f"{base}/content_calendar.csv", "content": calendar},
    ])
    memory.update_campaign_assets(campaign["id"], base)
    memory.add_event(
        project_id, "premarketing_campaign_created",
        f"プレマーケティングキャンペーンを作成: {payload.title.strip()}",
        detail=json.dumps({
            "campaign_id": campaign["id"], "capture_url": f"/premarketing/{campaign['public_token']}",
            "artifacts": audit,
        }, ensure_ascii=False)[:12000],
    )
    campaign = memory.get_campaign_by_token(campaign["public_token"]) or campaign
    campaign["capture_url"] = f"/premarketing/{campaign['public_token']}"
    return campaign


@app.get("/premarketing/{token}", response_class=HTMLResponse)
async def premarketing_capture_page(token: str):
    campaign = memory.get_campaign_by_token(token)
    if not campaign or campaign["status"] != "active":
        raise HTTPException(404, "キャンペーンが見つかりません")
    title, audience = html.escape(campaign["title"]), html.escape(campaign["audience"])
    offer, cta = html.escape(campaign["offer"]), html.escape(campaign["call_to_action"])
    return HTMLResponse(f"""<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>{title}</title><style>body{{font-family:system-ui;max-width:760px;margin:40px auto;padding:20px;line-height:1.7}}input,textarea,select{{display:block;width:100%;box-sizing:border-box;margin:6px 0 18px;padding:10px}}button{{padding:12px 24px}}.hp{{display:none}}</style><main><h1>{title}</h1><h2>対象となる方</h2><p>{audience}</p><h2>ご提供内容</h2><p>{offer}</p><h2>{cta}</h2><form method="post"><label>お名前<input name="name" required maxlength="120"></label><label>メール<input type="email" name="email" required maxlength="320"></label><label>会社・組織<input name="company" maxlength="200"></label><label>役割<input name="role" maxlength="200"></label><label>現在の課題<textarea name="problem" required minlength="10" maxlength="4000"></textarea></label><label>検討時期<input name="timeline" maxlength="200" placeholder="例: 1か月以内"></label><label>予算感<input name="budget" maxlength="200" placeholder="未定でも可"></label><label class="hp">Website<input name="website" tabindex="-1" autocomplete="off"></label><label><input type="checkbox" name="consent" value="true" required>相談対応のために入力情報を利用することへ同意します</label><button>相談を申し込む</button></form></main></html>""")


@app.get("/premarketing/{token}/landing", response_class=HTMLResponse)
async def premarketing_landing_preview(token: str):
    campaign = memory.get_campaign_by_token(token)
    if not campaign or campaign["status"] != "active":
        raise HTTPException(404, "キャンペーンが見つかりません")
    if not campaign.get("google_form_url"):
        raise HTTPException(409, "問い合わせ用Googleフォームが未公開です")
    campaign["creative"] = creative_view(
        memory.get_campaign_creative(campaign["project_id"], campaign["id"])
    )
    try:
        rendered = render_landing_page_html(campaign)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return HTMLResponse(rendered, headers={
        "Content-Security-Policy": (
            "default-src 'none'; style-src 'unsafe-inline'; img-src data: https:; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
        )
    })


@app.get("/premarketing/{token}/creative/preview", response_class=HTMLResponse)
async def premarketing_creative_preview(token: str):
    campaign = memory.get_campaign_by_token(token)
    if not campaign or campaign["status"] != "active":
        raise HTTPException(404, "キャンペーンが見つかりません")
    if not campaign.get("google_form_url"):
        raise HTTPException(409, "問い合わせ用Googleフォームが未公開です")
    creative = creative_view(
        memory.get_campaign_creative(campaign["project_id"], campaign["id"])
    )
    if not creative or creative.get("status") not in {"reviewed", "approved"}:
        raise HTTPException(409, "確認できる自動デザインがありません")
    preview_creative = dict(creative)
    preview_creative["status"] = "approved"
    preview_campaign = dict(campaign)
    preview_campaign["creative"] = preview_creative
    return HTMLResponse(render_landing_page_html(preview_campaign), headers={
        "Content-Security-Policy": (
            "default-src 'none'; style-src 'unsafe-inline'; img-src data: https:; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
        )
    })


@app.post("/premarketing/{token}", response_class=HTMLResponse)
async def submit_premarketing_lead(
    token: str, name: str = Form(...), email: str = Form(...),
    company: str = Form(""), role: str = Form(""), problem: str = Form(...),
    timeline: str = Form(""), budget: str = Form(""),
    consent: bool = Form(False), website: str = Form(""),
):
    campaign = memory.get_campaign_by_token(token)
    if not campaign or campaign["status"] != "active":
        raise HTTPException(404, "キャンペーンが見つかりません")
    if website.strip():
        return HTMLResponse("受付しました")
    try:
        capture_and_queue(
            memory, campaign, name=name, email=email, company=company, role=role,
            problem=problem, timeline=timeline, budget=budget, consent=consent,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return HTMLResponse("<!doctype html><html lang='ja'><meta charset='utf-8'><body><h1>お申し込みを受け付けました</h1><p>内容を確認後、担当者からご連絡します。</p></body></html>")

@app.get("/api/projects/{project_id}/workspace")
async def list_project_workspace(project_id: str):
    project = require_project(project_id)
    try:
        return workspace.list_entries(project["workspace_path"], project_id)
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/projects/{project_id}/workspace/operations")
async def operate_project_workspace(project_id: str, payload: WorkspaceOperationPayload):
    project = require_project(project_id)
    try:
        audit = workspace.apply_operations(project["workspace_path"], project_id, payload.operations)
    except FileNotFoundError as exc:
        raise HTTPException(404, f"Workspace source not found: {exc}") from exc
    except FileExistsError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc
    for item in audit:
        memory.add_event(project_id, "workspace_operation", f"{item['action']}: {item['path']}", detail=json.dumps(item, ensure_ascii=False))
    write_project_memos(memory, project_id)
    return {"audit": audit, "workspace": workspace.list_entries(project["workspace_path"], project_id)}


@app.post("/api/projects/{project_id}/workspace/upload", status_code=201)
async def upload_project_workspace_file(project_id: str, file: UploadFile = File(...),
                                        relative_path: str = Form(default="")):
    project = require_project(project_id)
    target = relative_path.strip() or (file.filename or "")
    data = await file.read(50 * 1024 * 1024 + 1)
    try:
        saved = workspace.save_upload(project["workspace_path"], project_id, target, data)
    except FileExistsError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc
    memory.add_event(project_id, "workspace_upload", f"upload: {saved['path']}", detail=json.dumps(saved, ensure_ascii=False))
    return saved


@app.get("/api/projects/{project_id}/workspace/files/{relative_path:path}/download")
async def download_project_workspace_file(project_id: str, relative_path: str):
    project = require_project(project_id)
    try:
        _, _, path = workspace.resolve_file(project["workspace_path"], project_id, relative_path, must_exist=True)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Workspace file not found") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not path.is_file():
        raise HTTPException(400, "Workspace path is not a file")
    return FileResponse(path, filename=path.name)

@app.get("/api/projects/{project_id}/mission/artifacts/download")
async def download_project_mission_artifacts(project_id: str):
    project = require_project(project_id)
    listing = workspace.list_entries(project["workspace_path"], project_id)
    artifact_entries = [
        item for item in listing.get("entries", [])
        if item.get("kind") == "file"
        and str(item.get("path", "")).replace("\\", "/").startswith(("成果フォルダ/", "output/", "result/"))
    ]
    if not artifact_entries:
        raise HTTPException(404, "Downloadable mission artifacts were not found")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for item in artifact_entries:
            relative = str(item["path"])
            try:
                _, _, path = workspace.resolve_file(
                    project["workspace_path"], project_id, relative, must_exist=True
                )
            except (FileNotFoundError, ValueError):
                continue
            if path.is_file():
                bundle.writestr(relative.replace("\\", "/"), path.read_bytes())
    if not archive.getbuffer().nbytes:
        raise HTTPException(404, "Downloadable mission artifacts were not found")
    filename = f"{project_id}-artifacts.zip"
    return Response(
        archive.getvalue(), media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

@app.get("/api/projects/{project_id}/context-files")
async def list_project_context_files(project_id: str):
    require_project(project_id)
    return memory.list_context_files(project_id)


@app.post("/api/projects/{project_id}/context-files", status_code=201)
async def upload_project_context(project_id: str, file: UploadFile = File(...),
                                 relative_path: str = Form(default="")):
    project = require_project(project_id)
    try:
        filename = normalize_context_filename(file.filename or "", relative_path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    max_bytes = 10 * 1024 * 1024
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(413, "One context file must be 10 MB or smaller")
    total_bytes = memory.context_file_bytes(project_id) + len(data)
    if total_bytes > PROJECT_CONTEXT_STORED_BYTES:
        raise HTTPException(413, "Total project context originals must be 100 MB or smaller")
    extraction = await asyncio.to_thread(
        extract_context_file, filename, data, file.content_type or ""
    )
    total_chars = (
        len(project["context_text"]) + memory.context_file_chars(project_id)
        + len(extraction.content)
    )
    if total_chars > PROJECT_CONTEXT_STORED_CHARS:
        raise HTTPException(413, "Total extracted project context must be 2,000,000 characters or fewer")
    item = memory.add_context_file(
        project_id, filename, extraction.content, len(data), data,
        extraction.mime_type, extraction.file_kind, extraction.note,
        hashlib.sha256(data).hexdigest(),
    )
    try:
        stored = workspace.store_original(
            project.get("workspace_path", ""), project_id, filename, data
        )
    except (ValueError, OSError) as exc:
        raise HTTPException(400, f"Context original could not be stored in Workspace: {exc}") from exc
    memory.add_event(
        project_id, "source_stored",
        f"registered original stored: {stored['path']}",
        detail=json.dumps(stored, ensure_ascii=False),
    )
    return {
        "project_id": project_id, "file": item,
        "workspace_original": stored,
        "context_file_count": len(memory.list_context_files(project_id)),
        "total_context_chars": total_chars, "total_context_bytes": total_bytes,
    }



@app.post("/api/projects/{project_id}/context-files/materialize")
async def materialize_project_context_files(project_id: str):
    project = require_project(project_id)
    stored_items = []
    for summary in memory.list_context_files(project_id):
        if summary.get("source") == "memo":
            continue
        item = memory.get_context_file(project_id, summary["id"])
        data = item.get("original_data") if item else None
        if data is None:
            continue
        try:
            stored_items.append(workspace.store_original(
                project.get("workspace_path", ""), project_id,
                item["filename"], data,
            ))
        except (ValueError, OSError) as exc:
            raise HTTPException(400, f"Context original could not be stored in Workspace: {exc}") from exc
    memory.add_event(
        project_id, "sources_materialized",
        f"registered originals stored in Workspace: {len(stored_items)}",
        detail=json.dumps(stored_items, ensure_ascii=False)[:12000],
    )
    return {"project_id": project_id, "stored": stored_items, "count": len(stored_items)}

@app.get("/api/projects/{project_id}/context-files/{file_id}/download")
async def download_project_context_file(project_id: str, file_id: str):
    require_project(project_id)
    item = memory.get_context_file(project_id, file_id)
    if not item:
        raise HTTPException(404, "Context file not found")
    data = item["original_data"]
    if data is None:
        data = item["content"].encode("utf-8")
    encoded = quote(item["filename"], safe="")
    return Response(
        content=data,
        media_type=item["mime_type"] or "application/octet-stream",
        headers={
            "Content-Disposition":
                f"attachment; filename=\"context-file\"; filename*=UTF-8''{encoded}"
        },
    )


@app.get("/api/projects/{project_id}/context-files/{file_id}/accounting-preview")
async def preview_project_accounting_file(project_id: str, file_id: str):
    require_project(project_id)
    item = memory.get_context_file(project_id, file_id)
    if not item:
        raise HTTPException(404, "Context file not found")
    data = item["original_data"]
    if data is None:
        raise HTTPException(400, "Original Excel file is not available")
    try:
        return await asyncio.to_thread(
            analyze_yayoi_workbook, item["filename"], data
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/projects/{project_id}/context-files/{file_id}")
async def delete_project_context_file(project_id: str, file_id: str):
    require_project(project_id)
    if not memory.delete_context_file(project_id, file_id):
        raise HTTPException(404, "Context file not found")
    return {"project_id": project_id, "file_id": file_id, "deleted": True}

@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str):
    await orchestrator.pause(project_id, cancelled=True)
    try:
        deleted_turns = memory.delete_project(project_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, "Project not found") from exc
    return {"project_id": project_id, "deleted_turns": deleted_turns}


async def local_model_catalog() -> list[dict]:
    async with httpx.AsyncClient(timeout=20) as client:
        tags_response = await client.get(f"{OLLAMA_URL}/api/tags")
        tags_response.raise_for_status()
        try:
            running_response = await client.get(f"{OLLAMA_URL}/api/ps")
            running_response.raise_for_status()
            loaded = {
                str(item.get("name") or item.get("model") or "").strip()
                for item in running_response.json().get("models", [])
            }
        except (httpx.HTTPError, ValueError, TypeError):
            loaded = set()

        models = []
        for entry in tags_response.json().get("models", []):
            name = str(entry.get("name") or entry.get("model") or "").strip()
            if not name:
                continue
            try:
                show_response = await client.post(
                    f"{OLLAMA_URL}/api/show", json={"model": name}
                )
                show_response.raise_for_status()
                shown = show_response.json()
            except (httpx.HTTPError, ValueError, TypeError):
                continue
            capabilities = sorted({
                str(item).strip().lower()
                for item in shown.get("capabilities", [])
                if str(item).strip()
            })
            if "completion" not in capabilities:
                continue
            details = shown.get("details") or entry.get("details") or {}
            models.append({
                "name": name,
                "size": int(entry.get("size") or 0),
                "modified_at": entry.get("modified_at"),
                "family": str(details.get("family") or ""),
                "parameter_size": str(details.get("parameter_size") or ""),
                "quantization_level": str(details.get("quantization_level") or ""),
                "capabilities": capabilities,
                "thinking_supported": "thinking" in capabilities,
                "selected": name == OLLAMA_MODEL,
                "loaded": name in loaded,
            })
    return sorted(models, key=lambda item: (not item["selected"], item["name"].lower()))


@app.get("/api/local-models")
async def list_local_models():
    try:
        models = await local_model_catalog()
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise HTTPException(503, f"Ollama model list is unavailable: {exc}") from exc
    return {
        "selected": OLLAMA_MODEL,
        "engine_enabled": engine_enabled,
        "models": models,
    }


@app.put("/api/local-models/selected")
async def select_local_model(payload: LocalModelSelectionPayload):
    global OLLAMA_MODEL, model_warmup_state
    requested = payload.model.strip()
    async with model_switch_lock:
        if any(item["status"] == "running" for item in executions.values()):
            raise HTTPException(
                409, "実行中の処理があります。完了または停止してからモデルを切り替えてください。"
            )
        try:
            models = await local_model_catalog()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise HTTPException(503, f"Ollama model list is unavailable: {exc}") from exc
        available = {item["name"]: item for item in models}
        if requested not in available:
            raise HTTPException(
                400, "インストール済みの会話用Ollamaモデルを選択してください。"
            )
        previous = OLLAMA_MODEL
        if requested == previous:
            return {
                "status": "unchanged",
                "selected": requested,
                "engine_enabled": engine_enabled,
                "model": available[requested],
            }

        if engine_enabled:
            model_warmup_state = {"status": "warming", "model": requested, "error": ""}
            try:
                # Load the candidate first. If it fails, the selected model and
                # persistent setting remain untouched.
                await load_model(True, requested)
            except httpx.HTTPError as exc:
                model_warmup_state = {
                    "status": "error", "model": requested, "error": str(exc)[:500],
                }
                raise HTTPException(
                    503, f"選択したローカルLLMを読み込めませんでした: {exc}"
                ) from exc

        try:
            save_local_model_selection(requested)
        except OSError as exc:
            if engine_enabled:
                try:
                    await load_model(False, requested)
                    await load_model(True, previous)
                except httpx.HTTPError:
                    pass
            raise HTTPException(500, f"モデル設定を保存できませんでした: {exc}") from exc

        OLLAMA_MODEL = requested
        llm.set_model(requested)
        if engine_enabled and previous != requested:
            try:
                await load_model(False, previous)
            except httpx.HTTPError:
                pass
        model_warmup_state = {
            "status": "ready" if engine_enabled else "stopped",
            "model": requested,
            "error": "",
        }
        selected = dict(available[requested])
        selected["selected"] = True
        selected["loaded"] = engine_enabled
        return {
            "status": "switched",
            "selected": requested,
            "engine_enabled": engine_enabled,
            "model": selected,
        }


@app.get("/api/health")
async def health():
    ollama_ok = await llm.health()
    provider = front_provider()
    external_ready = bool(provider and provider.configured)
    return {
        "status": "ok",
        "ollama": ollama_ok,
        "engine_enabled": engine_enabled,
        "model": provider.model if external_ready else OLLAMA_MODEL,
        "front_provider": provider.label if external_ready else "Local Ollama",
        "front_provider_id": provider.id if external_ready else "ollama",
        "front_ready": external_ready or ollama_ok,
        "fallback_model": OLLAMA_MODEL,
        "local_optimization": {
            "context_length": int(os.getenv("OLLAMA_NUM_CTX", "8192")),
            "max_output_tokens": int(os.getenv("OLLAMA_NUM_PREDICT", "1536")),
            "reasoning": os.getenv("OLLAMA_THINK", "medium"),
            "keep_alive": os.getenv("OLLAMA_KEEP_ALIVE", "30m"),
            "warmup": dict(model_warmup_state),
        },
        "safe_table_executor": True,
        "ollama_url": OLLAMA_URL,
        "asr": "faster-whisper-small",
        "context_turn_limit": CONTEXT_TURNS,
    }


@app.get("/api/cowork/status")
async def cowork_status():
    return await build_status(llm, OLLAMA_MODEL, OPEN_WEBUI_INTERNAL_URL, CPTR_INTERNAL_URL)


@app.get("/api/monitor")
async def monitor_status():
    items = [execution_view(item) for item in executions.values()]
    active = sorted((item for item in items if item["status"] == "running"), key=lambda item: item["started_at"])
    recent = sorted((item for item in items if item["status"] != "running"), key=lambda item: item["completed_at"] or 0, reverse=True)[:20]
    return {"engine_enabled": engine_enabled, "active_count": len(active), "active": active, "recent": recent}

@app.get("/api/research/providers")
async def research_providers():
    return provider_statuses()


@app.post("/api/research")
async def research(request: ResearchRequest):
    session_id = request.session_id or uuid.uuid4().hex
    project, shared_context = shared_project_context(request.project_id, session_id)
    execution_id = start_execution(
        "research", request.topic.strip(), request.project_id, project["name"],
        f"{len(request.providers)}個の外部AIへ問い合わせ中",
    )
    prior_turns = memory.turn_count(session_id, request.project_id)
    started = time.perf_counter()
    try:
        from app.experience_memory import configured_memory, memory_scope
        from app.experience_store import fingerprint
        setting = configured_memory(memory.path, request.project_id)
        if setting:
            service, mode = setting
            with memory_scope(service, request.project_id, fingerprint(shared_context), mode=mode, external_allowed=True):
                result = await run_research(request.topic.strip(), request.providers, request.synthesizer, request.style, shared_context)
        else:
            result = await run_research(request.topic.strip(), request.providers, request.synthesizer, request.style, shared_context)
        update_execution(execution_id, "調査結果を保存中", result.get("synthesis_model"))
        elapsed = (time.perf_counter() - started) * 1000
        saved = False
        stored_answer = result.get("synthesis")
        if not stored_answer:
            successful = [item for item in result.get("results", []) if item.get("ok")]
            if successful:
                stored_answer = "\n\n".join(f"[{item['label']}]\n{item['answer']}" for item in successful)
        monitor_model = result.get("synthesis_model") or "external-ai-ensemble"
        if stored_answer:
            memory.save(session_id, f"[外部AI調査]\n{request.topic.strip()}", stored_answer, monitor_model, elapsed, False, request.project_id)
            saved = True
        result["session_id"] = session_id
        result["project"] = {"id": project["id"], "name": project["name"]}
        result["latency_ms"] = round(elapsed, 1)
        context_files = memory.list_context_files(request.project_id)
        result["context"] = {
            "prior_turns": prior_turns,
            "project_context_chars": len(project["context_text"]) + sum(item["char_count"] for item in context_files),
            "context_file_count": len(context_files),
            "saved_to_front": saved,
            "turns_after": memory.turn_count(session_id, request.project_id),
        }
        finish_execution(execution_id, "success", "完了", monitor_model)
        return result
    except ValueError as exc:
        finish_execution(execution_id, "error", "入力エラー", error=str(exc))
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        finish_execution(execution_id, "error", "実行エラー", error=str(exc))
        raise

async def load_model(loaded: bool, model: str | None = None):
    target_model = model or OLLAMA_MODEL
    # Warm-up must not start an unconstrained answer. One non-thinking token is
    # sufficient to make Ollama load and validate the selected model.
    async with httpx.AsyncClient(timeout=float(os.getenv("OLLAMA_MODEL_LOAD_TIMEOUT", "300"))) as client:
        response = await client.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": target_model,
                "prompt": " " if loaded else "",
                "stream": False,
                "think": False,
                "keep_alive": os.getenv("OLLAMA_KEEP_ALIVE", "30m") if loaded else 0,
                "options": {
                    "num_ctx": min(int(os.getenv("OLLAMA_NUM_CTX", "8192")), 8192),
                    "num_predict": 1,
                    "temperature": 0,
                },
            },
        )
        response.raise_for_status()


async def _warmup_selected_model():
    global model_warmup_state
    target = OLLAMA_MODEL
    model_warmup_state = {"status": "warming", "model": target, "error": ""}
    try:
        async with model_switch_lock:
            await load_model(True, target)
        model_warmup_state = {"status": "ready", "model": target, "error": ""}
    except (httpx.HTTPError, asyncio.CancelledError) as exc:
        if isinstance(exc, asyncio.CancelledError):
            raise
        model_warmup_state = {
            "status": "error", "model": target, "error": str(exc)[:500],
        }


@app.post("/api/control/start")
async def start_engine():
    global engine_enabled, model_warmup_state
    provider = front_provider()
    local_front = not (provider and provider.configured)
    if local_front and not await llm.health():
        raise HTTPException(503, "Front AI and Ollama are unavailable")
    if local_front:
        last_error = None
        for attempt in range(2):
            try:
                await load_model(True)
                last_error = None
                break
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt == 0:
                    await asyncio.sleep(2)
        if last_error:
            raise HTTPException(503, f"Failed to load local controller: {last_error}") from last_error
    engine_enabled = True
    if local_front:
        model_warmup_state = {
            "status": "ready", "model": OLLAMA_MODEL, "error": "",
        }
    return {"status": "running", "model": provider.model if provider and provider.configured else OLLAMA_MODEL}

@app.post("/api/control/stop")
async def stop_engine():
    global engine_enabled, model_warmup_state
    await orchestrator.pause_all()
    engine_enabled = False
    if await llm.health():
        try:
            await load_model(False)
        except httpx.HTTPError:
            pass
    model_warmup_state = {
        "status": "stopped", "model": OLLAMA_MODEL, "error": "",
    }
    return {"status": "stopped"}


def get_asr():
    global asr_model
    if asr_model is None:
        from faster_whisper import WhisperModel
        asr_model = WhisperModel(os.getenv("ASR_MODEL", "small"), device="cpu", compute_type="int8")
    return asr_model


@app.post("/api/transcribe")
async def transcribe(file: UploadFile = File(...)):
    filename = Path(file.filename or "audio.webm").name
    execution_id = start_execution("transcribe", filename, stage="音声データ受信中")
    suffix = Path(filename).suffix or ".webm"
    temp_path = None
    try:
        data = await file.read()
        if not data:
            raise HTTPException(400, "Empty recording")
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp:
            temp.write(data)
            temp_path = temp.name
        update_execution(execution_id, "音声認識中", "faster-whisper-small")

        def run():
            segments, _ = get_asr().transcribe(temp_path, language="ja", vad_filter=False, beam_size=5, condition_on_previous_text=False)
            return "".join(segment.text for segment in segments).strip()

        text = await asyncio.to_thread(run)
        if not text:
            raise HTTPException(422, "Speech was not recognized")
        finish_execution(execution_id, "success", "文字起こし完了", "faster-whisper-small")
        return {"text": text}
    except Exception as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        finish_execution(execution_id, "error", "音声認識エラー", "faster-whisper-small", str(detail))
        raise
    finally:
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)

@app.get("/api/mic/health")
async def mic_health():
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            response = await client.get("http://host.docker.internal:8098/health")
        except httpx.HTTPError as exc:
            raise HTTPException(503, f"Windows mic bridge unavailable: {exc}") from exc
    return response.json()


@app.post("/api/mic/start")
async def mic_start():
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            response = await client.post("http://host.docker.internal:8098/record/start")
        except httpx.HTTPError as exc:
            raise HTTPException(503, f"Windows mic bridge unavailable: {exc}") from exc
    if response.is_error:
        try:
            detail = response.json().get("detail", response.text)
        except Exception:
            detail = response.text
        raise HTTPException(response.status_code, detail)
    return response.json()


@app.post("/api/mic/stop")
async def mic_stop():
    async with httpx.AsyncClient(timeout=180) as client:
        try:
            response = await client.post("http://host.docker.internal:8098/record/stop")
        except httpx.HTTPError as exc:
            raise HTTPException(503, f"Windows mic bridge unavailable: {exc}") from exc
    if response.is_error:
        try:
            detail = response.json().get("detail", response.text)
        except Exception:
            detail = response.text
        raise HTTPException(response.status_code, detail)
    return response.json()


@app.get("/api/history")
async def history(limit: int = 30, session_id: str | None = None, project_id: str = "default"):
    require_project(project_id)
    limit = min(max(limit, 1), 100)
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        if session_id:
            rows = db.execute(
                "SELECT id,session_id,project_id,created_at,user_text,assistant_text,model,latency_ms,interrupted FROM turns WHERE session_id=? AND project_id=? ORDER BY id DESC LIMIT ?",
                (session_id, project_id, limit),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT id,session_id,project_id,created_at,user_text,assistant_text,model,latency_ms,interrupted FROM turns WHERE project_id=? ORDER BY id DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
    return [dict(row) for row in rows]


def markdown_download(content: str, filename: str) -> Response:
    encoded = quote(filename, safe="")
    return Response(
        content=("\ufeff" + content).encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=\"result.md\"; filename*=UTF-8''{encoded}"},
    )



@app.get("/api/projects/{project_id}/mission/report/download")
async def download_project_mission_report(project_id: str):
    project = require_project(project_id)
    mission = memory.get_mission(project_id)
    if not mission["tasks"] and not mission["final_report"]:
        raise HTTPException(404, "保存された計画・成果がありません")
    sections = [
        f"# {project['name']} — プロジェクト実行報告",
        f"## 目標\n\n{mission['goal']}",
        f"## 達成条件\n\n{mission['success_criteria'] or '未指定'}",
        f"## 計画\n\n{mission['plan_summary'] or '未生成'}",
    ]
    for task in mission["tasks"]:
        sections.append(
            f"## {task['position']}. {task['title']}\n\n"
            f"- 状態: {task['status']}\n"
            f"- 実行方式: {task['mode']}\n"
            f"- 完了判定: {task['acceptance_criteria']}\n\n"
            f"{task['result'] or task['error'] or '成果なし'}"
        )
    if mission["final_report"]:
        sections.append("## 最終報告\n\n" + mission["final_report"])
    return markdown_download("\n\n---\n\n".join(sections), f"{project['name']}-project-report.md")


def turn_as_markdown(row: sqlite3.Row, project: dict) -> str:
    return (
        f"# 保存結果 #{row['id']}\n\n"
        f"- プロジェクト: {project['name']}\n"
        f"- 実行日時: {row['created_at']}\n"
        f"- モデル: {row['model']}\n"
        f"- 処理時間: {round(row['latency_ms'] / 1000, 2)} 秒\n\n"
        f"## 依頼内容\n\n{row['user_text']}\n\n"
        f"## 処理結果\n\n{row['assistant_text']}\n"
    )


@app.get("/api/history/{turn_id}/download")
async def download_history_result(turn_id: int, project_id: str = "default"):
    project = require_project(project_id)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    try:
        row = db.execute(
            "SELECT id,project_id,created_at,user_text,assistant_text,model,latency_ms FROM turns WHERE id=? AND project_id=?",
            (turn_id, project_id),
        ).fetchone()
    finally:
        db.close()
    if not row:
        raise HTTPException(404, "Saved result not found")
    return markdown_download(turn_as_markdown(row, project), f"{project['name']}-result-{turn_id}.md")


@app.get("/api/projects/{project_id}/history/download")
async def download_project_history(project_id: str):
    project = require_project(project_id)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            "SELECT id,project_id,created_at,user_text,assistant_text,model,latency_ms FROM turns WHERE project_id=? ORDER BY id",
            (project_id,),
        ).fetchall()
    finally:
        db.close()
    sections = [f"# {project['name']} — 保存済み結果\n"]
    if rows:
        sections.extend(turn_as_markdown(row, project) for row in rows)
    else:
        sections.append("保存済みの結果はありません。\n")
    return markdown_download("\n\n---\n\n".join(sections), f"{project['name']}-all-results.md")

@app.get("/api/context/{session_id}")
async def context_status(session_id: str, project_id: str = "default"):
    project, text = shared_project_context(project_id, session_id)
    conversation = memory.context_text(session_id, CONTEXT_TURNS, CONTEXT_MAX_CHARS, project_id)
    context_files = memory.list_context_files(project_id)
    context_file_chars = sum(item["char_count"] for item in context_files)
    return {
        "session_id": session_id,
        "project": project,
        "turns": memory.turn_count(session_id, project_id),
        "conversation_chars": len(conversation),
        "manual_context_chars": len(project["context_text"]),
        "context_file_count": len(context_files),
        "context_file_chars": context_file_chars,
        "project_context_chars": len(project["context_text"]) + context_file_chars,
        "context_chars": len(text),
        "turn_limit": CONTEXT_TURNS,
        "max_chars": CONTEXT_MAX_CHARS,
    }

@app.delete("/api/context/{session_id}")
async def clear_context(session_id: str, project_id: str = "default"):
    require_project(project_id)
    return {"session_id": session_id, "project_id": project_id, "deleted_turns": memory.clear(session_id, project_id)}


def build_front_prompt(shared_context: str, user: str) -> str:
    context = shared_context or "このプロジェクトには固定コンテキストや過去の会話履歴がありません。"
    return (
        "以下はフロントAIが保持する、選択中プロジェクト専用のコンテキストです。\n"
        "--- プロジェクトコンテキスト開始 ---\n" + context + "\n--- プロジェクトコンテキスト終了 ---\n\n"
        "現在のユーザー依頼:\n" + user
    )

@app.websocket("/ws/chat")
async def chat(ws: WebSocket):
    await ws.accept()
    sid = ws.query_params.get("session_id") or uuid.uuid4().hex
    await ws.send_json({"type": "session", "session_id": sid})
    try:
        while True:
            request = json.loads(await ws.receive_text())
            user = str(request.get("message", "")).strip()
            project_id = str(request.get("project_id", "default")).strip() or "default"
            if not engine_enabled:
                await ws.send_json({"type": "error", "message": "AI engine is stopped"})
                continue
            if not user:
                await ws.send_json({"type": "error", "message": "Empty message"})
                continue
            try:
                project, shared_context = shared_project_context(project_id, sid)
            except HTTPException as exc:
                await ws.send_json({"type": "error", "message": exc.detail})
                continue
            execution_id = start_execution("chat", user, project_id, project["name"], "回答準備中")
            started = time.perf_counter()
            prior_turns = memory.turn_count(sid, project_id)
            history_messages = memory.recent(sid, CONTEXT_TURNS, project_id)
            await ws.send_json({"type": "start", "project_id": project_id})
            answer: list[str] = []
            model = OLLAMA_MODEL
            provider_id = "ollama"
            fallback_reason = None
            try:
                provider = front_provider()
                if provider and provider.configured:
                    update_execution(execution_id, f"{provider.label}で回答生成中", provider.model)
                    try:
                        from contextlib import nullcontext
                        from app.experience_memory import configured_memory, memory_scope
                        from app.experience_store import fingerprint
                        setting = configured_memory(memory.path, project_id)
                        context = memory_scope(setting[0], project_id, fingerprint(shared_context), mode=setting[1], external_allowed=False) if setting else nullcontext()
                        with context:
                            response = await call_provider_with_metadata(
                                provider.id,
                                build_front_prompt(shared_context, user),
                                SYSTEM_PROMPT,
                                max_tokens=int(os.getenv("FRONT_AI_MAX_TOKENS", "2400")),
                                reasoning_effort=FRONT_AI_REASONING_EFFORT if provider.id == "chatgpt" else None,
                            )
                        answer.append(response.text)
                        model = response.model
                        provider_id = provider.id
                        await ws.send_json({"type": "token", "content": response.text})
                    except Exception as exc:
                        fallback_reason = str(exc)[:500]
                if not answer:
                    update_execution(execution_id, "ローカルLLMへフォールバック中", OLLAMA_MODEL)
                    if not await llm.health():
                        raise RuntimeError(fallback_reason or "Front AI and Ollama are unavailable")
                    local_system = SYSTEM_PROMPT
                    static_context, _ = project_static_context(project)
                    if static_context:
                        local_system += "\n\n# プロジェクト固定コンテキスト\n" + static_context
                    from app.experience_memory import augment_project_prompt
                    from app.experience_store import fingerprint
                    local_user = await augment_project_prompt(memory.path, project_id, fingerprint(shared_context), user)
                    messages = [{"role": "system", "content": local_system}] + history_messages + [{"role": "user", "content": local_user}]
                    async for token in llm.stream(messages):
                        answer.append(token)
                        await ws.send_json({"type": "token", "content": token})
                elapsed = (time.perf_counter() - started) * 1000
                text = "".join(answer).strip()
                update_execution(execution_id, "会話履歴を保存中", model)
                memory.save(sid, user, text, model, elapsed, False, project_id)
                finish_execution(execution_id, "success", "完了", model)
                await ws.send_json({
                    "type": "done", "latency_ms": round(elapsed, 1), "model": model,
                    "provider": provider_id, "fallback": bool(fallback_reason and provider_id == "ollama"),
                    "context_turns": prior_turns, "project_id": project_id, "project_name": project["name"],
                })
            except Exception as exc:
                finish_execution(execution_id, "error", "回答エラー", model, str(exc))
                await ws.send_json({"type": "error", "message": str(exc)})
    except WebSocketDisconnect:
        return


class DetailedReviewPayload(BaseModel):
    plan_id: str
    candidate_hash: str
    decision: str
    notes: str
    meaning_checked: bool = False
    applicability_checked: bool = False
    requirements_checked: bool = False


class DetailedResumePayload(BaseModel):
    replan_reason: str = ''


@app.get('/api/projects/{project_id}/tasks/{task_id}/details')
async def get_detailed_task(project_id: str, task_id: str):
    require_project(project_id)
    from app.detail_service import view
    try:return view(orchestrator,project_id,task_id)
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc


@app.post('/api/projects/{project_id}/tasks/{task_id}/details/review')
async def review_detailed_task(project_id: str, task_id: str, payload: DetailedReviewPayload):
    require_project(project_id)
    from app.detail_service import record_review
    try:return record_review(orchestrator,project_id,task_id,payload.plan_id,payload.candidate_hash,payload.decision,payload.notes,
                             [payload.meaning_checked,payload.applicability_checked,payload.requirements_checked])
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc


@app.post('/api/projects/{project_id}/tasks/{task_id}/details/resume')
async def resume_detailed_task(project_id: str, task_id: str, payload: DetailedResumePayload):
    require_project(project_id)
    from app.detail_service import prepare_resume
    try:return prepare_resume(orchestrator,project_id,task_id,payload.replan_reason)
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc

# Private imported reports are intentionally outside project shared context.
import sys as _sys
from app.agent_examples_api import install as _install_agent_examples
_install_agent_examples(app, _sys.modules[__name__])


@app.get("/static/agent_examples.js")
async def agent_examples_javascript():
    return FileResponse(ROOT / "app" / "static" / "agent_examples.js", media_type="text/javascript", headers={"Cache-Control":"no-store"})

from app.procedure_learning_api import install as _install_procedure_learning
_install_procedure_learning(app, _sys.modules[__name__])

@app.get('/static/procedure_learning.js')
async def procedure_learning_javascript():
    return FileResponse(ROOT / 'app' / 'static' / 'procedure_learning.js', media_type='text/javascript', headers={'Cache-Control':'no-store'})


@app.get('/static/triz_invention.js')
async def triz_invention_javascript():
    return FileResponse(ROOT / 'app' / 'static' / 'triz_invention.js', media_type='text/javascript', headers={'Cache-Control':'no-store'})


@app.get('/static/triz_general.js')
async def triz_general_javascript():
    return FileResponse(ROOT / 'app' / 'static' / 'triz_general.js', media_type='text/javascript', headers={'Cache-Control':'no-store'})


from app.review_queue import install as _install_review_queue
_install_review_queue(app, _sys.modules[__name__])

@app.get('/static/review_queue.js')
async def review_queue_javascript():
    return FileResponse(ROOT / 'app' / 'static' / 'review_queue.js', media_type='text/javascript', headers={'Cache-Control':'no-store'})

@app.get('/static/artifact_shelf.js')
async def artifact_shelf_js():
    return FileResponse(ROOT / 'app' / 'static' / 'artifact_shelf.js', media_type='text/javascript')

@app.get('/static/artifact_shelf.css')
async def artifact_shelf_css():
    return FileResponse(ROOT / 'app' / 'static' / 'artifact_shelf.css', media_type='text/css')

@app.get('/api/projects/{project_id}/vehicle-profit')
async def vehicle_profit_status(project_id: str):
    require_project(project_id)
    from app.vehicle_workflow import applicable, load_input, digest, sources, requested_months
    mission = memory.get_mission(project_id)
    envelope = load_input(orchestrator, project_id) if applicable(mission) else None
    return {'applicable': applicable(mission), 'version': mission['plan_version'],
            'months': requested_months(mission), 'input_hash': digest(envelope) if envelope else None,
            'sources': sources(orchestrator, project_id) if applicable(mission) else [],
            'status': mission['status'], 'data': envelope.get('data') if envelope else None,
            'automatic': __import__('app.vehicle_service',fromlist=['state']).state(orchestrator,project_id) if applicable(mission) else None}


@app.get('/api/projects/{project_id}/evidence/trace')
async def get_evidence_trace(project_id: str, vehicle_id: str = '', month: str = '', category: str = ''):
    require_project(project_id)
    from app.evidence_graph import trace
    if not str(vehicle_id or '').strip() or not str(month or '').strip() or not str(category or '').strip():
        raise HTTPException(400, 'vehicle_id, month, category が必要です')
    return trace(orchestrator, project_id, vehicle_id.strip(), month.strip(), category.strip())


@app.get('/api/projects/{project_id}/evidence/summary')
async def get_evidence_summary(project_id: str, limit: int = 50):
    require_project(project_id)
    from app.evidence_graph import summary
    return summary(orchestrator, project_id, limit=limit)


class VehicleInputPayload(BaseModel):
    version: int
    previous_hash: str | None = None
    confirmed: bool = False
    data: dict


@app.post('/api/projects/{project_id}/vehicle-profit/input')
async def save_vehicle_profit_input(project_id: str, payload: VehicleInputPayload):
    require_project(project_id)
    from app.vehicle_workflow import applicable, load_input, normalize_input, sources, digest, resolve, write_json, requested_months, INPUT_PATH
    from app.structured_planning import contract_of
    mission = memory.get_mission(project_id)
    if not applicable(mission) or not any((contract_of(t) or {}).get('execution_kind')=='vehicle_calculate' for t in mission['tasks']):
        raise HTTPException(409, '車両別計算の実行計画を生成してください')
    if mission['status']=='running' or mission['plan_version']!=payload.version:
        raise HTTPException(409, '実行中または計画版が変わっています。更新してください')
    previous = load_input(orchestrator, project_id)
    if payload.previous_hash != (digest(previous) if previous else None):
        raise HTTPException(409, '入力が別の操作で変更されています。更新してください')
    snapshot = sources(orchestrator, project_id)
    try:
        if any(k in payload.data for k in ('auto_extraction','provisional_numeric')):
            raise ValueError('自動抽出証跡は手入力で登録できません。自動抽出を再実行してください')
        data = normalize_input(payload.data, requested_months(mission))
        if data.get('source_snapshot') != snapshot:
            raise ValueError('入力テンプレートの原本版が現在と一致しません。最新の原本を確認してください')
    except (ValueError,TypeError,KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc
    envelope = {'data':data,'confirmed':payload.confirmed,'source_hash':digest(snapshot)}
    if previous and digest(previous)==digest(envelope):
        return {'changed':False,'message':'入力条件は同じです。再試行状態は変更していません'}
    target = resolve(orchestrator, project_id, INPUT_PATH)
    if previous:
        write_json(target.parent/'history'/(digest(previous)+'.json'),previous)
    write_json(target,envelope)
    # Reset only the affected calculation descendants; source extraction is retained when unchanged.
    for task in mission['tasks']:
        kind=(contract_of(task) or {}).get('execution_kind')
        if kind in {'vehicle_extract','vehicle_calculate','vehicle_verify'}:
            memory.update_task(task['id'],'pending',error='')
        elif kind=='vehicle_sources':
            path=resolve(orchestrator,project_id,(contract_of(task))['outputs'][0]['path'])
            try:
                from app.vehicle_workflow import read_json
                unchanged=digest(read_json(path)['sources'])==digest(snapshot)
            except (OSError,ValueError,KeyError):unchanged=False
            if not unchanged:memory.update_task(task['id'],'pending',error='')
    memory.set_mission_status(project_id,'paused','計算入力を更新しました。実行再開で変更対象を計算します','vehicle_input_updated')
    return {'changed':True,'input_hash':digest(envelope)}

@app.get('/api/projects/{project_id}/workflow-readiness')
async def get_workflow_readiness(project_id: str):
    require_project(project_id)
    from app.workflow_readiness import build_readiness
    return build_readiness(orchestrator, project_id)


@app.get('/api/projects/{project_id}/goal-metrics')
async def get_goal_metrics(project_id: str):
    require_project(project_id)
    from app.goal_metrics import collect_baseline
    return collect_baseline(orchestrator, project_id)


@app.get('/api/projects/{project_id}/goal-state')
async def get_goal_state(project_id: str):
    require_project(project_id)
    from app.goal_state_machine import read_goal_state
    return read_goal_state(orchestrator, project_id)


@app.get('/static/workflow_readiness.js')
async def workflow_readiness_js():
    return FileResponse(ROOT/'app'/'static'/'workflow_readiness.js', media_type='text/javascript', headers={'Cache-Control':'no-store'})



@app.get('/api/projects/{project_id}/goal-contract')
async def get_goal_contract(project_id: str):
    require_project(project_id)
    from app.goal_contract import preview
    return preview(orchestrator, project_id)


class GoalContractPayload(BaseModel):
    payload: dict = Field(default_factory=dict)


@app.put('/api/projects/{project_id}/goal-contract')
async def put_goal_contract(project_id: str, body: GoalContractPayload):
    require_project(project_id)
    from app.goal_contract import put_draft
    return put_draft(orchestrator, project_id, body.payload or None)


@app.post('/api/projects/{project_id}/goal-contract/activate')
async def activate_goal_contract(project_id: str):
    require_project(project_id)
    from app.goal_contract import activate
    try:
        return activate(orchestrator, project_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get('/api/projects/{project_id}/goal-contract/retention-check')
async def goal_contract_retention(project_id: str):
    require_project(project_id)
    from app.goal_contract import preview
    from app.requirement_retention import inspect
    contract = preview(orchestrator, project_id)
    return inspect(memory.get_mission(project_id), contract)


@app.post('/api/projects/{project_id}/goal-contract/decompose')
async def decompose_goal_contract(project_id: str):
    require_project(project_id)
    from app.goal_decompose import DecomposeUnavailable, decompose
    try:
        return await decompose(orchestrator, project_id)
    except DecomposeUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc


class GoalDecomposeConfirmPayload(BaseModel):
    draft_items: list[dict] = Field(default_factory=list)
    operations: list[dict] = Field(default_factory=list)
    reviewed_by: str = Field(default='', max_length=100)


@app.post('/api/projects/{project_id}/goal-contract/decompose/confirm')
async def confirm_goal_decompose(project_id: str, payload: GoalDecomposeConfirmPayload):
    require_project(project_id)
    actor = payload.reviewed_by.strip()
    if not actor:
        raise HTTPException(422, 'reviewed_by is required')
    from app.goal_decompose import DecomposeRejected, confirm
    try:
        return confirm(
            orchestrator, project_id, payload.draft_items, payload.operations, actor,
        )
    except DecomposeRejected as exc:
        if exc.code == 'FLAG_OFF':
            raise HTTPException(409, detail={'code': 'FLAG_OFF', 'message': str(exc)}) from exc
        if exc.status_code == 422:
            raise HTTPException(422, str(exc)) from exc
        raise HTTPException(409, detail={'code': exc.code, 'message': str(exc)}) from exc


@app.get('/api/projects/{project_id}/plan/coverage')
async def get_plan_coverage(project_id: str):
    require_project(project_id)
    from app.plan_coverage import get as get_coverage
    return get_coverage(orchestrator, project_id) or {}


@app.get('/api/projects/{project_id}/completion-replay')
async def get_completion_replay(project_id: str):
    require_project(project_id)
    from app.completion_replay import preview
    return preview(orchestrator, project_id)


@app.get('/api/projects/{project_id}/completion-gate')
async def get_completion_gate(project_id: str):
    require_project(project_id)
    from app.completion_gate import evaluate
    return evaluate(orchestrator, project_id, persist=False)


@app.post('/api/projects/{project_id}/completion-gate/evaluate')
async def post_completion_gate_evaluate(project_id: str):
    require_project(project_id)
    from app.completion_gate import evaluate
    return evaluate(orchestrator, project_id, persist=True)


class CompletionGateAcceptancePayload(BaseModel):
    accepted_by: str = Field(min_length=1, max_length=100)
    note: str = Field(default='', max_length=4000)


@app.post('/api/projects/{project_id}/completion-gate/accept')
async def post_completion_gate_accept(project_id: str, payload: CompletionGateAcceptancePayload):
    require_project(project_id)
    actor = payload.accepted_by.strip()
    if not actor:
        raise HTTPException(422, 'accepted_by is required')
    from app.completion_gate import accept
    try:
        return accept(orchestrator, project_id, accepted_by=actor, note=payload.note)
    except ValueError as exc:
        if str(exc) == 'accepted_by is required':
            raise HTTPException(422, str(exc)) from exc
        raise HTTPException(409, str(exc)) from exc


class AutoResumeEnablePayload(BaseModel):
    enabled: bool
    actor: str = Field(min_length=1, max_length=100)


@app.get('/api/projects/{project_id}/auto-resume')
async def get_auto_resume(project_id: str):
    require_project(project_id)
    from app.safe_auto_resume import enabled, records
    return {'enabled': enabled(orchestrator, project_id), 'records': records(orchestrator, project_id)}


@app.post('/api/projects/{project_id}/auto-resume/enable')
async def post_auto_resume_enable(project_id: str, payload: AutoResumeEnablePayload):
    require_project(project_id)
    actor = payload.actor.strip()
    if not actor:
        raise HTTPException(422, 'actor is required')
    from app.safe_auto_resume import set_enabled
    try:
        result = set_enabled(orchestrator, project_id, payload.enabled)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    memory.add_event(project_id, 'auto_resume_setting_changed',
                     f"安全な自動再開を{'有効' if payload.enabled else '無効'}にしました",
                     detail=json.dumps({'actor': actor, 'enabled': payload.enabled}, ensure_ascii=False))
    return result


@app.post('/api/projects/{project_id}/auto-resume/run')
async def post_auto_resume_run(project_id: str):
    require_project(project_id)
    from app.safe_auto_resume import resume
    try:
        return await resume(orchestrator, project_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


class NextActionExecutePayload(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=200)
    chain: bool = False


@app.get('/api/projects/{project_id}/next-action')
async def get_next_action(project_id: str):
    require_project(project_id)
    from app.next_action_controller import compute
    return compute(orchestrator, project_id)


@app.post('/api/projects/{project_id}/next-action/execute')
async def post_next_action_execute(project_id: str, payload: NextActionExecutePayload):
    require_project(project_id)
    from app.next_action_controller import NextActionRefused, execute
    try:
        return execute(orchestrator, project_id, payload.idempotency_key, chain=payload.chain)
    except NextActionRefused as exc:
        status = 422 if exc.code == 'EMPTY_IDEMPOTENCY_KEY' else 409
        raise HTTPException(status, detail={'code': exc.code, 'message': str(exc)}) from exc


@app.get('/api/projects/{project_id}/goal-review')
async def goal_review_state(project_id: str):
    require_project(project_id)
    from app.goal_review import ReviewStore,plan_snapshot,execution_snapshot,detail_payloads,public_structure,enabled,all_reviews_history,provider_review_outcomes,review_pass_policy
    store=ReviewStore(memory.path);snapshot,signature=plan_snapshot(orchestrator,project_id)
    proof,result_signature,issues=execution_snapshot(orchestrator,project_id)
    from app.plan_feedback import issues_for
    from app.goal_review import review_budget
    details=[]
    for item in detail_payloads(orchestrator,project_id):
        _,sig=plan_snapshot(orchestrator,project_id,item['payload'])
        details.append({'task_id':item['task_id'],'signature':sig,'review':store.get(project_id,'plan',sig),'feedback':issues_for(orchestrator,project_id,sig),'revision':store.get(project_id,'revision',sig)})
    with store.connect() as db:
        history=[dict(json.loads(x[1]),signature=x[0]) for x in db.execute("SELECT signature,payload FROM reviews WHERE project=? AND kind='result'",(project_id,))]
    with store.connect() as db:
        revision_history=[dict(json.loads(x[1]),signature=x[0]) for x in db.execute("SELECT signature,payload FROM reviews WHERE project=? AND kind='revision' ORDER BY rowid DESC LIMIT 20",(project_id,))]
    from app.goal_review import send_allowed, orchestration_view, active_jobs
    from app.workflow_readiness import build_readiness
    orch=orchestration_view(orchestrator,project_id)
    jobs=active_jobs(store,project_id)
    readiness=build_readiness(orchestrator,project_id)
    plan_review=store.get(project_id,'plan',signature)
    if plan_review:
        plan_review=dict(plan_review)
        providers=list(dict.fromkeys(memory.get_mission(project_id).get('external_providers') or [x.get('provider') for x in plan_review.get('reviews') or [] if x.get('provider')]))
        plan_review.setdefault('provider_outcomes',provider_review_outcomes(plan_review.get('reviews') or [], providers))
        plan_review.setdefault('pass_policy',review_pass_policy(orchestrator,project_id,len(providers)))
        if 'connection_errors' not in plan_review:
            plan_review['connection_errors']=[{
                'provider':x.get('provider'),'status_code':(x.get('connection_error') or {}).get('status_code'),
                'category':(x.get('connection_error') or {}).get('category') or 'connection_error',
                'error':((x.get('connection_error') or {}).get('error') or (x.get('response') or {}).get('error') or '')[:2000],
            } for x in plan_review.get('reviews') or [] if x.get('status')=='connection_error']
    return {'budget':review_budget(orchestrator,project_id),'revision_history':revision_history,'required':enabled(orchestrator,project_id),'public_draft':__import__('app.goal_review_queue',fromlist=['public_draft']).public_draft(snapshot),'plan_signature':signature,'plan_review':plan_review,
            'feedback':issues_for(orchestrator,project_id,signature),'revision':store.get(project_id,'revision',signature),'public_structure':public_structure(snapshot),'details':details,'result_signature':result_signature,'result_issues':issues,
            'artifacts':proof['artifacts'],'result_review':store.get(project_id,'result',result_signature),'history':history,
            'history_all':all_reviews_history(orchestrator,project_id),
            'send_allowed':send_allowed(orchestrator,project_id),'active_job':readiness.get('active_job'),
            'replan_failure':readiness.get('replan_failure'),
            'pipeline_stage':readiness.get('pipeline_stage'),
            'pipeline_next_action':readiness.get('pipeline_next_action'),
            'resume_from':readiness.get('resume_from') or orch.get('resume_from') or '',
            'last_completed_stage':readiness.get('last_completed_stage') or orch.get('last_completed_stage') or '',
            'blocking_error':readiness.get('blocking_error') or orch.get('blocking_error') or ''}


class GoalPlanReviewPayload(BaseModel):
    signature: str
    public_summary: str = Field(min_length=20,max_length=12000)
    safe_to_send: bool = False
    task_id: str | None = None
    idempotency_key: str = Field(default='',max_length=80)


class GoalSendApprovalPayload(BaseModel):
    signature: str
    public_summary: str = Field(min_length=20,max_length=12000)


@app.post('/api/projects/{project_id}/goal-review/plan')
async def submit_goal_plan_review(project_id: str,payload: GoalPlanReviewPayload):
    require_project(project_id)
    from app.goal_review import review_plan, send_allowed
    if not send_allowed(orchestrator, project_id):
        raise HTTPException(409, '外部AIの許可・接続が揃っていないため送信できません。ローカルの指摘取込と修正は利用できます')
    try:return await review_plan(orchestrator,project_id,payload.signature,payload.public_summary,payload.safe_to_send,payload.task_id,payload.idempotency_key or None)
    except (ValueError,KeyError) as exc:raise HTTPException(409,str(exc)) from exc


@app.post('/api/projects/{project_id}/goal-review/send-approval')
async def approve_goal_plan_send(project_id: str, payload: GoalSendApprovalPayload):
    require_project(project_id)
    from app.goal_review import record_send_approval, send_allowed, plan_snapshot
    if not send_allowed(orchestrator, project_id):
        raise HTTPException(409, '外部AIの許可・接続が揃っていないため送信承認できません')
    _, current = plan_snapshot(orchestrator, project_id)
    if current != payload.signature:
        raise HTTPException(409, '計画が変わりました。再読込してください')
    return record_send_approval(orchestrator, project_id, payload.signature, payload.public_summary)


class HumanResultPayload(BaseModel):
    signature: str
    reviewer: str = Field(min_length=1,max_length=100)
    notes: str = Field(min_length=1,max_length=4000)
    lesson: str = Field(default='',max_length=8000)
    conditions: str = Field(default='',max_length=2000)
    checks: list[bool] = []
    days: int = Field(default=30,ge=1,le=366)


@app.post('/api/projects/{project_id}/goal-review/result')
async def submit_human_result(project_id: str,payload: HumanResultPayload):
    require_project(project_id)
    from app.goal_review import approve_result
    try:return await approve_result(orchestrator,project_id,payload.signature,payload.reviewer,payload.notes,payload.lesson,payload.conditions,payload.checks,payload.days)
    except (ValueError,KeyError) as exc:raise HTTPException(409,str(exc)) from exc


@app.post('/api/projects/{project_id}/goal-review/revoke')
async def revoke_human_result(project_id: str,payload: HumanResultPayload):
    require_project(project_id)
    from app.goal_review import revoke_result
    try:return revoke_result(orchestrator,project_id,payload.signature,payload.reviewer,payload.notes)
    except (ValueError,KeyError) as exc:raise HTTPException(409,str(exc)) from exc


@app.post('/api/projects/{project_id}/goal-review/index')
async def retry_result_index(project_id: str):
    require_project(project_id)
    from app.experience_memory import configured_memory
    setting=configured_memory(memory.path,project_id)
    if not setting:raise HTTPException(409,'このプロジェクトのRAGが無効です')
    try:
        count=await asyncio.to_thread(setting[0].reindex,project_id)
        from app.goal_review import ReviewStore,evidence_valid
        store=ReviewStore(memory.path)
        with store.connect() as db:rows=db.execute("SELECT signature,payload FROM reviews WHERE project=? AND kind='result'",(project_id,)).fetchall()
        for signature,payload in rows:
            row=json.loads(payload)
            if row.get('status')=='approved':
                row['index_status']='indexed' if evidence_valid(memory.path,project_id,{'human_result':signature,'experience_id':row['experience_id']}) else 'stale'
                store.put(project_id,'result',signature,row)
        return {'indexed':count}
    except Exception as exc:raise HTTPException(503,'ローカル索引の更新に失敗: '+type(exc).__name__) from exc


@app.get('/static/goal_review.js')
async def goal_review_js():
    return FileResponse(ROOT/'app'/'static'/'goal_review.js',media_type='text/javascript',headers={'Cache-Control':'no-store'})


@app.get('/static/ocr_review.js')
async def ocr_review_js():
    return FileResponse(ROOT/'app'/'static'/'ocr_review.js', media_type='text/javascript', headers={'Cache-Control':'no-store'})


@app.get('/static/experience_import.js')
async def experience_import_js():
    return FileResponse(ROOT/'app'/'static'/'experience_import.js', media_type='text/javascript', headers={'Cache-Control':'no-store'})


@app.get('/static/ocr_review.css')
async def ocr_review_css():
    return FileResponse(ROOT/'app'/'static'/'ocr_review.css', media_type='text/css', headers={'Cache-Control':'no-store'})


class PlanFeedbackPayload(BaseModel):
    signature: str
    task_id: str | None = None
    provider: str = Field(default='',max_length=100)
    text: str = Field(default='',max_length=16000)
    candidate_id: str = ''
    idempotency_key: str = Field(default='',max_length=80)
    criterion: str = Field(default='',max_length=80)


class LegacyFeedbackImportPayload(BaseModel):
    signature: str
    legacy_id: str
    task_id: str | None = None


@app.post('/api/projects/{project_id}/goal-review/feedback/import')
async def import_plan_feedback(project_id: str,payload: PlanFeedbackPayload):
    require_project(project_id)
    from app.plan_feedback import import_feedback
    try:return import_feedback(orchestrator,project_id,payload.signature,payload.provider,payload.text,payload.task_id,payload.criterion)
    except (ValueError,KeyError) as exc:raise HTTPException(409,str(exc)) from exc


@app.post('/api/projects/{project_id}/goal-review/feedback/import-legacy')
async def import_legacy_plan_feedback(project_id: str, payload: LegacyFeedbackImportPayload):
    require_project(project_id)
    from app.plan_feedback import import_legacy_review
    try:return import_legacy_review(orchestrator,project_id,payload.signature,payload.legacy_id,payload.task_id)
    except (ValueError,KeyError) as exc:raise HTTPException(409,str(exc)) from exc


@app.post('/api/projects/{project_id}/goal-review/feedback/propose')
async def propose_plan_feedback(project_id: str,payload: PlanFeedbackPayload):
    require_project(project_id)
    if not engine_enabled or not await llm.health():raise HTTPException(503,'ローカルLLMが利用できません')
    from app.plan_feedback import propose
    try:return await propose(orchestrator,project_id,payload.signature,payload.task_id,idempotency_key=payload.idempotency_key or None)
    except (ValueError,KeyError) as exc:raise HTTPException(409,str(exc)) from exc
    except Exception as exc:raise HTTPException(502,'修正案を生成できませんでした: '+str(exc)[:500]) from exc


class PlanFeedbackRepairPayload(BaseModel):
    contract_hash: str
    signature: str
    candidate_id: str
    task_id: str | None = None
    patches: list[dict] = Field(default_factory=list)


@app.get('/api/projects/{project_id}/goal-review/feedback/repair-preview')
async def preview_plan_feedback_repair(project_id: str, signature: str, candidate_id: str,
                                       task_id: str | None = None):
    require_project(project_id)
    from app.plan_feedback import repair_preview
    try:
        return repair_preview(orchestrator, project_id, signature, candidate_id, task_id)
    except (ValueError, KeyError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post('/api/projects/{project_id}/goal-review/feedback/repair')
async def repair_plan_feedback(project_id: str, payload: PlanFeedbackRepairPayload):
    require_project(project_id)
    from app.plan_feedback import repair_saved_draft
    try:
        return repair_saved_draft(orchestrator, project_id, payload.signature,
                                  payload.candidate_id, payload.patches, payload.task_id, payload.contract_hash)
    except (ValueError, KeyError) as exc:
        raise HTTPException(409, str(exc)) from exc

@app.post('/api/projects/{project_id}/goal-review/feedback/apply')
async def apply_plan_feedback(project_id: str,payload: PlanFeedbackPayload):
    require_project(project_id)
    from app.plan_feedback import apply
    try:return apply(orchestrator,project_id,payload.signature,payload.candidate_id,payload.task_id)
    except (ValueError,KeyError) as exc:raise HTTPException(409,str(exc)) from exc


class VehiclePreparePayload(BaseModel):
    version: int
    idempotency_key: str = Field(default='',max_length=80)

@app.post('/api/projects/{project_id}/vehicle-profit/prepare')
async def prepare_vehicle_profit(project_id: str, payload: VehiclePreparePayload):
    require_project(project_id)
    from app.vehicle_service import run_prepare
    try:
        return await asyncio.to_thread(
            run_prepare, orchestrator, project_id, payload.version, payload.idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc

class VehicleAllocationItem(BaseModel):
    vehicle_id: str = Field(min_length=1, max_length=100)
    ratio: float | str | int


class VehicleDecisionPayload(BaseModel):
    version: int
    input_hash: str
    issue_id: str
    vehicle_id: str = ''
    exclude: bool = False
    reason: str
    apply_to: str = 'this_month'
    allocations: list[VehicleAllocationItem] = Field(default_factory=list)
    date_from: str | None = None
    date_to: str | None = None

@app.post('/api/projects/{project_id}/vehicle-profit/decision')
async def decide_vehicle_profit(project_id: str, payload: VehicleDecisionPayload):
    require_project(project_id)
    from app.vehicle_service import decide
    from app.vehicle_workflow import AllocationRuleError
    body=payload.model_dump()
    body['allocations']=[item if isinstance(item,dict) else item.model_dump() for item in (payload.allocations or [])]
    try:return decide(orchestrator,project_id,**body)
    except AllocationRuleError as exc:raise HTTPException(422,str(exc)) from exc
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc


class QuestionAnswerPayload(VehicleDecisionPayload):
    answered_by: str = Field(min_length=1, max_length=100)


@app.get('/api/projects/{project_id}/questions')
async def get_project_questions(project_id: str, limit: int = 5):
    require_project(project_id)
    from app.question_view import build_questions
    return build_questions(orchestrator, project_id, limit=min(5, max(0, limit)))


@app.post('/api/projects/{project_id}/questions/answer')
async def answer_project_question(project_id: str, payload: QuestionAnswerPayload):
    require_project(project_id)
    actor = payload.answered_by.strip()
    if not actor:
        raise HTTPException(422, 'answered_by is required')
    from app.goal_completion_flag import enabled
    if not enabled(memory.path, project_id):
        raise HTTPException(409, detail={'code': 'FLAG_OFF', 'message': '既存の回答画面を利用してください'})
    from app.vehicle_service import decide
    from app.vehicle_workflow import AllocationRuleError, load_input
    from app.goal_completion_store import GoalCompletionStore
    from app.question_view import fact_key_for
    body = payload.model_dump(exclude={'answered_by'})
    body['allocations'] = [item if isinstance(item, dict) else item.model_dump() for item in (payload.allocations or [])]
    try:
        decide(orchestrator, project_id, **body)
    except AllocationRuleError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    envelope = load_input(orchestrator, project_id)
    decision = (envelope.get('decisions') or [])[-1]
    months = list(decision.get('months') or [])
    fact_key = fact_key_for(decision.get('subject_key') or {}, months)
    value = {
        'subject_key': decision.get('subject_key') or {}, 'months': months,
        'vehicle_id': decision.get('vehicle_id') or '', 'exclude': bool(decision.get('exclude')),
        'allocations': list(decision.get('allocations') or []), 'apply_to': decision.get('apply_to') or '',
    }
    current_state = __import__('app.vehicle_service', fromlist=['state']).state(orchestrator, project_id)
    try:
        fact = GoalCompletionStore(memory.path).record_fact(
            project_id, fact_key, value, payload.issue_id, actor, payload.input_hash,
            envelope.get('source_hash') or '', payload.version, payload.reason,
        )
    except Exception as exc:
        LOGGER.warning('goal fact recording failed after decision project=%s: %s', project_id, exc, exc_info=True)
        return {'state': current_state, 'fact': None, 'fact_error': str(exc)[:500]}
    return {'state': current_state,
            'fact': {'id': fact['id'], 'fact_key': fact['fact_key'], 'version': fact['version']}}


def _read_goal_facts(project_id: str, fact_key: str | None = None) -> list[dict]:
    path = Path(memory.path).parent / 'goal_completion.sqlite3'
    if not path.exists():
        return []
    db = sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'goal_facts' not in names:
            return []
        if fact_key is not None:
            rows = db.execute(
                'SELECT * FROM goal_facts WHERE project_id=? AND fact_key=? ORDER BY version',
                (project_id, fact_key),
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT f.* FROM goal_facts f JOIN (
                       SELECT fact_key, MAX(version) AS version FROM goal_facts
                       WHERE project_id=? GROUP BY fact_key
                   ) latest ON latest.fact_key=f.fact_key AND latest.version=f.version
                   WHERE f.project_id=? ORDER BY f.fact_key""",
                (project_id, project_id),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item['value'] = json.loads(item.pop('value_json'))
            result.append(item)
        return result
    except (sqlite3.OperationalError, ValueError):
        return []
    finally:
        db.close()


@app.get('/api/projects/{project_id}/facts')
async def get_project_facts(project_id: str):
    require_project(project_id)
    return {'facts': _read_goal_facts(project_id)}


@app.get('/api/projects/{project_id}/facts/history')
async def get_project_fact_history(project_id: str, fact_key: str):
    require_project(project_id)
    if not fact_key.strip():
        raise HTTPException(422, 'fact_key is required')
    return {'facts': _read_goal_facts(project_id, fact_key)}


class CancelGoalReviewQueuePayload(BaseModel):
    signature: str

@app.post('/api/projects/{project_id}/goal-review/queue/cancel')
async def cancel_goal_review_queue(project_id: str, payload: CancelGoalReviewQueuePayload):
    require_project(project_id)
    from app.goal_review import ReviewStore
    store=ReviewStore(memory.path);row=store.get(project_id,'plan_queue',payload.signature)
    if not row:return {'status':'not_queued'}
    if row.get('status')=='running':
        row['status']='cancelled';store.put(project_id,'plan_queue',payload.signature,row)
        return {'status':'cancelled','message':'以後の再試行を停止しました。送信済みの依頼は取り消せません'}
    row['status']='cancelled';store.put(project_id,'plan_queue',payload.signature,row)
    current=store.get(project_id,'plan',payload.signature)
    if current and current.get('status')=='waiting_budget':
        current['status']='unverified';current['queue_cancelled']=True;store.put(project_id,'plan',payload.signature,current)
    return {'status':'cancelled'}


class OcrRequestPayload(BaseModel):
    idempotency_key: str = Field(default='', max_length=128)
    purpose: str = Field(default='context', max_length=40)
    user_requested_original_match: bool = False
    page_count: int | None = Field(default=None, ge=1, le=500)


class OcrReviewPayload(BaseModel):
    decision: str = Field(min_length=1, max_length=40)
    reviewer: str = Field(default='', max_length=100)
    reason: str = Field(default='', max_length=4000)
    corrected_values: dict = Field(default_factory=dict)
    correction_reason: str = Field(default='', max_length=4000)


class OcrRagPayload(BaseModel):
    confirm_rag: bool = False
    revoke: bool = False
    reviewer: str = Field(default='', max_length=100)
    reason: str = Field(default='', max_length=4000)


def _ocr_http_error(exc) -> HTTPException:
    return HTTPException(exc.status_code, detail=exc.as_detail())


def _ocr_store():
    from app.ocr_store import OcrStore
    return OcrStore(memory.path)


def _ocr_original_bytes(project_id: str, context_file_id: str):
    item = memory.get_context_file(project_id, context_file_id)
    if not item:
        return None, None
    data = item.get('original_data')
    if data is None and item.get('content') is not None:
        data = str(item.get('content') or '').encode('utf-8')
    return data, item


@app.post('/api/projects/{project_id}/context-files/{file_id}/ocr')
async def request_context_file_ocr(
    project_id: str, file_id: str, payload: OcrRequestPayload,
    idempotency_key: str | None = Header(default=None, alias='Idempotency-Key'),
):
    require_project(project_id)
    from app.ocr_review import OcrApiError, enqueue_ocr_run
    item = memory.get_context_file(project_id, file_id)
    if not item:
        raise HTTPException(404, detail={'error_code': 'not_found', 'message': 'コンテキストファイルが見つかりません'})
    header_key = idempotency_key if isinstance(idempotency_key, str) else ''
    key = (payload.idempotency_key or '').strip() or header_key.strip()
    original = item.get('original_data')
    if original is None and item.get('content') is not None:
        original = str(item.get('content') or '').encode('utf-8')
    store = _ocr_store()
    try:
        from app.ocr_review import resolve_ocr_page_spec
        actual_count, pages_quality = resolve_ocr_page_spec(original, payload.page_count)
        view, _duplicate, status_code = enqueue_ocr_run(
            store,
            project_id=project_id,
            context_file_id=file_id,
            original_bytes=original,
            expected_sha256=str(item.get('sha256') or ''),
            filename=str(item.get('filename') or 'document.pdf'),
            idempotency_key=key,
            page_count=actual_count,
            purpose=payload.purpose,
            user_requested_original_match=payload.user_requested_original_match,
            pages_quality=pages_quality,
        )
    except OcrApiError as exc:
        raise _ocr_http_error(exc) from exc
    return JSONResponse(view, status_code=status_code)


@app.get('/api/projects/{project_id}/context-files/{file_id}/ocr')
async def list_context_file_ocr_runs(project_id: str, file_id: str):
    require_project(project_id)
    from app.ocr_review import OcrApiError, list_file_ocr_runs
    item = memory.get_context_file(project_id, file_id)
    if not item:
        raise HTTPException(404, detail={'error_code': 'not_found', 'message': 'コンテキストファイルが見つかりません'})
    try:
        runs = list_file_ocr_runs(_ocr_store(), project_id=project_id, context_file_id=file_id)
    except OcrApiError as exc:
        raise _ocr_http_error(exc) from exc
    return {'file_id': file_id, 'runs': runs}


@app.get('/api/projects/{project_id}/ocr/{run_id}')
async def get_ocr_run(project_id: str, run_id: str):
    require_project(project_id)
    from app.ocr_review import OcrApiError, run_status_view
    try:
        return run_status_view(_ocr_store(), project_id=project_id, run_id=run_id)
    except OcrApiError as exc:
        raise _ocr_http_error(exc) from exc


@app.get('/api/projects/{project_id}/ocr/{run_id}/artifacts')
async def get_ocr_artifacts(project_id: str, run_id: str):
    require_project(project_id)
    from app.ocr_review import OcrApiError, list_ocr_artifacts, require_complete_run
    store = _ocr_store()
    try:
        run = require_complete_run(store.get_run(run_id), project_id=project_id, run_id=run_id)
        return {'run_id': run_id, 'files': list_ocr_artifacts(run)}
    except OcrApiError as exc:
        raise _ocr_http_error(exc) from exc


@app.get('/api/projects/{project_id}/ocr/{run_id}/artifacts/{name}')
async def download_ocr_artifact(project_id: str, run_id: str, name: str):
    require_project(project_id)
    from app.ocr_review import OcrApiError, load_ocr_artifact_file, require_complete_run
    store = _ocr_store()
    try:
        run = require_complete_run(store.get_run(run_id), project_id=project_id, run_id=run_id)
        data, media_type, filename = load_ocr_artifact_file(run, name)
    except OcrApiError as exc:
        raise _ocr_http_error(exc) from exc
    encoded = quote(filename, safe='')
    return Response(
        content=data,
        media_type=media_type,
        headers={
            'X-Content-Type-Options': 'nosniff',
            'Content-Disposition': f"attachment; filename=\"ocr-artifact\"; filename*=UTF-8''{encoded}",
            'Cache-Control': 'no-store',
        },
    )


@app.get('/api/projects/{project_id}/ocr/{run_id}/evidence/{field_id}')
async def get_ocr_evidence(project_id: str, run_id: str, field_id: str):
    require_project(project_id)
    from app.ocr_review import OcrApiError, field_evidence
    try:
        return field_evidence(_ocr_store(), project_id=project_id, run_id=run_id, field_id=field_id)
    except OcrApiError as exc:
        raise _ocr_http_error(exc) from exc


@app.post('/api/projects/{project_id}/ocr/{run_id}/review')
async def review_ocr_run(project_id: str, run_id: str, payload: OcrReviewPayload):
    require_project(project_id)
    from app.ocr_review import OcrApiError, require_complete_run, submit_ocr_review
    store = _ocr_store()
    try:
        run = require_complete_run(store.get_run(run_id), project_id=project_id, run_id=run_id)
        original, _item = _ocr_original_bytes(project_id, str(run.get('context_file_id') or ''))
        return submit_ocr_review(
            store, project_id=project_id, run_id=run_id,
            decision=payload.decision, reviewer=payload.reviewer, reason=payload.reason,
            original_bytes=original, corrected_values=payload.corrected_values,
            correction_reason=payload.correction_reason,
        )
    except OcrApiError as exc:
        raise _ocr_http_error(exc) from exc


@app.post('/api/projects/{project_id}/ocr/{run_id}/publish')
async def publish_ocr_run_api(project_id: str, run_id: str):
    require_project(project_id)
    from app.ocr_review import OcrApiError, publish_ocr_run, require_complete_run
    store = _ocr_store()
    try:
        run = require_complete_run(store.get_run(run_id), project_id=project_id, run_id=run_id)
        original, _item = _ocr_original_bytes(project_id, str(run.get('context_file_id') or ''))
        return publish_ocr_run(
            store, project_id=project_id, run_id=run_id, original_bytes=original,
        )
    except OcrApiError as exc:
        raise _ocr_http_error(exc) from exc


class ImportSuccessCasesPayload(BaseModel):
    items: list[dict] = Field(min_length=1, max_length=200)
    proof: str = Field(default="成功事例収集エージェントでの人手レビュー済み")
    actor: str = Field(min_length=1)


@app.post('/api/projects/{project_id}/experience/import-success-cases')
async def import_success_cases_api(project_id: str, payload: ImportSuccessCasesPayload):
    require_project(project_id)
    if not payload.actor.strip():
        raise HTTPException(422, "actor is required")
    from app.experience_memory import import_success_cases, MemoryPolicyError
    try:
        return import_success_cases(DB_PATH, project_id, payload.items, payload.proof, payload.actor)
    except MemoryPolicyError as exc:
        raise HTTPException(409, detail={"code": "EXPERIENCE_OFF", "message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


class RecoveryStartPayload(BaseModel):
    task_key: str = Field(min_length=1, max_length=100)
    failure: dict = Field(default_factory=dict)
    actor: str = Field(min_length=1, max_length=100)
    input_version: str = Field(min_length=1, max_length=200)
    source_hash: str = Field(default='', max_length=128)
    ocr_needs_review: bool = False


class RecoveryAdvancePayload(BaseModel):
    next_state: str = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=1, max_length=2000)
    evidence: dict = Field(default_factory=dict)
    actor: str = Field(min_length=1, max_length=100)


@app.get('/api/projects/{project_id}/recovery')
async def get_recovery_records(project_id: str):
    require_project(project_id)
    from app.recovery_record import list_records
    return {'records': list_records(orchestrator, project_id)}


@app.post('/api/projects/{project_id}/recovery/start')
async def post_recovery_start(project_id: str, payload: RecoveryStartPayload):
    require_project(project_id)
    from app.recovery_record import start
    try:
        return start(orchestrator, project_id, payload.task_key, payload.failure,
                     payload.actor.strip(), payload.input_version, payload.source_hash,
                     payload.ocr_needs_review)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post('/api/projects/{project_id}/recovery/{rid}/advance')
async def post_recovery_advance(project_id: str, rid: str, payload: RecoveryAdvancePayload):
    require_project(project_id)
    from app.recovery_record import advance
    try:
        return advance(orchestrator, project_id, rid, payload.next_state,
                       payload.reason, payload.evidence, payload.actor.strip())
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


class RecoveryApplyPayload(BaseModel):
    reviewer: str = Field(min_length=1, max_length=100)
    note: str = Field(default='', max_length=4000)
    expected_version: int | None = None


@app.get('/api/projects/{project_id}/plan/recovery/preview')
@app.post('/api/projects/{project_id}/plan/recovery/preview')
async def get_plan_recovery_preview(project_id: str):
    require_project(project_id)
    from app.plan_recovery import preview_recovery
    try:
        return preview_recovery(orchestrator, project_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post('/api/projects/{project_id}/plan/recovery/apply')
async def post_plan_recovery_apply(project_id: str, payload: RecoveryApplyPayload):
    require_project(project_id)
    actor = payload.reviewer.strip()
    if not actor:
        raise HTTPException(422, 'reviewer is required')
    from app.plan_recovery import apply_recovery
    try:
        return apply_recovery(
            orchestrator, project_id,
            reviewer=actor,
            note=payload.note,
            expected_current_version=payload.expected_version,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post('/api/projects/{project_id}/ocr/{run_id}/rag')
async def register_ocr_rag_api(project_id: str, run_id: str, payload: OcrRagPayload):
    require_project(project_id)
    from app.ocr_review import OcrApiError, register_ocr_rag, require_complete_run, revoke_ocr_rag
    store = _ocr_store()
    try:
        run = require_complete_run(store.get_run(run_id), project_id=project_id, run_id=run_id)
        if payload.revoke:
            return revoke_ocr_rag(
                store, project_id=project_id, run_id=run_id,
                reviewer=payload.reviewer, reason=payload.reason,
            )
        original, _item = _ocr_original_bytes(project_id, str(run.get('context_file_id') or ''))
        return register_ocr_rag(
            store, project_id=project_id, run_id=run_id, original_bytes=original,
            confirm_rag=payload.confirm_rag, reviewer=payload.reviewer, reason=payload.reason,
        )
    except OcrApiError as exc:
        raise _ocr_http_error(exc) from exc
