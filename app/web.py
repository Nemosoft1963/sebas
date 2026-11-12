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
    CHANNELS, channel_identity_statuses, resolve_channel_selection,
    social_package, social_package_json, social_package_markdown,
    unselected_channel_drift, validate_evidence_url,
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


class _SocialInvalidationWriteError(RuntimeError):
    """無効化の書込が途中で失敗した。with 文の外へ送出して全件を巻き戻す。"""


NO_INVALIDATION_TARGET_REASON = "再承認待ちに戻す共有行が無い"


def _social_approval_still_present(memory_obj, project_id: str,
                                   campaign_id: str) -> bool:
    """承認済み(approved/composer_opened)の共有行がまだ残っているか。読めなければ残っている扱い。"""
    try:
        rows = memory_obj.list_social_shares(str(project_id or ""), str(campaign_id or ""))
    except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError, KeyError):
        return True
    return any(isinstance(item, dict)
               and str(item.get("status") or "") in ("approved", "composer_opened")
               for item in rows)


def _publication_binding_block(memory_obj, project_id: str, campaign_id: str,
                               binding_status: str, reasons: list[str]) -> HTTPException:
    """承認を再承認待ちへ戻し、409 の例外を返す。無効化の結果(成否と理由)を detail に残す。

    無効化に失敗したときは invalidation_failed=True とし、成功に見せない。
    """
    invalidation = _invalidate_social_approval_on_stale(
        memory_obj, project_id, campaign_id, binding_status, reasons)
    failed = (not invalidation.get("invalidated")
              and str(invalidation.get("reason") or "") != NO_INVALIDATION_TARGET_REASON)
    message = "再プレビュー・再承認が必要です"
    if failed:
        message += "。承認の無効化にも失敗したため、再承認待ちへ戻ったか確認してください"
    return HTTPException(409, detail={
        "code": "PUBLICATION_BINDING_NOT_OK",
        "message": message,
        "binding_status": binding_status,
        "reasons": list(reasons or []),
        "invalidation": invalidation,
        "invalidation_failed": failed,
    })


def _invalidate_social_approval_on_stale(memory_obj, project_id: str,
                                         campaign_id: str, binding_status: str,
                                         reasons: list[str]) -> dict:
    """stale 検出時に承認済みの共有行を再承認待ちへ戻す。

    既存の状態遷移の語に合わせる。既存の evidence_registered の行と証跡は
    変更しない。無効化はイベント(social_approval_invalidated、理由つき)とし
    て残す。書込の成否は返すが、失敗は呼出側で409にする(成功に見せない)。
    """
    pid = str(project_id or "")
    cid = str(campaign_id or "")
    detail_reasons = [str(r) for r in (reasons or []) if str(r)]
    try:
        current = memory_obj.list_social_shares(pid, cid)
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return {"invalidated": False, "reason": "共有行を読めないため無効化できない"}
    targets = [item for item in current
               if isinstance(item, dict)
               and str(item.get("status") or "") in ("approved", "composer_opened")]
    if not targets:
        return {"invalidated": False, "reason": "再承認待ちに戻す共有行が無い"}
    channels = sorted({str(item.get("channel") or "") for item in targets if str(item.get("channel") or "")})
    try:
        # 1件でも更新できなければ例外で with 文を抜け、全件を巻き戻す(半端な状態を残さない)。
        with memory_obj._connect() as db:
            for item in targets:
                channel = str(item.get("channel") or "")
                variant = str(item.get("variant") or "primary")
                cursor = db.execute(
                    """UPDATE campaign_social_shares SET status='awaiting_approval',
                        approved_at=NULL, updated_at=?
                        WHERE project_id=? AND campaign_id=? AND channel=? AND variant=?
                        AND status IN ('approved','composer_opened')""",
                    (datetime.now(timezone.utc).isoformat(), pid, cid, channel, variant))
                if not cursor.rowcount:
                    raise _SocialInvalidationWriteError(channel)
    except _SocialInvalidationWriteError:
        return {"invalidated": False, "channels": channels,
                "reason": "共有行の無効化に失敗した(全件を巻き戻しました)。半端な状態を成功としない"}
    except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError) as exc:
        return {"invalidated": False, "channels": channels,
                "reason": f"共有行の無効化に失敗した: {type(exc).__name__}"}
    try:
        memory_obj.add_event(
            pid, "social_approval_invalidated",
            "SNS承認を再承認待ちに戻した: " + "、".join(channels),
            detail=json.dumps({"campaign_id": cid, "channels": channels,
                               "binding_status": binding_status,
                               "reasons": detail_reasons}, ensure_ascii=False)[:12000],
        )
    except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError, KeyError):
        return {"invalidated": False, "channels": channels,
                "reason": "無効化の監査記録に失敗した。半端な状態を成功としない"}
    return {"invalidated": True, "channels": channels,
            "binding_status": binding_status, "reasons": detail_reasons}


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
    _researcher = PublicWebResearcher()
    # C2: web.search_public のアダプタを本番の研究器生成箇所で登録する。
    # 実通信は作らない。許可は永続の保存先で別に判定する。
    try:
        from app.channel_skills import register_adapter as _register_skill_adapter
        _register_skill_adapter("web.search_public", type(_researcher).__name__)
    except (ImportError, AttributeError, ValueError):
        pass
    # C3: mail.receive の標準読取専用IMAPアダプタを本番の起動時に登録する。
    # 実接続は作らない。資格情報の値は読まない(名前の存在確認は成熟度側で行う)。
    try:
        from app.channel_skills import register_adapter as _register_mail_adapter
        _register_mail_adapter("mail.receive", "StandardImapReadonlyAdapter")
    except (ImportError, AttributeError, ValueError):
        pass
    # C5: mail.send_approved のSMTPアダプタを本番の起動時に登録する。
    # 実通信は承認済みメールだけ。資格情報の値は読まない(存在確認は実行時に行う)。
    try:
        from app.channel_skills import register_adapter as _register_send_adapter
        _register_send_adapter("mail.send_approved", "SMTP")
    except (ImportError, AttributeError, ValueError):
        pass
    orchestrator = ProjectOrchestrator(
        memory, llm, project_static_context, run_research, provider_statuses,
        run_plan_reviews, workspace, run_capability_reviews,
        SafeTableExecutor(memory, workspace), _researcher,
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
    # L3: 論理削除中・削除状態が確認できないPJは通常の読出し対象から外す (データは残す)。
    try:
        mem_path = getattr(memory, "path", None)
        if mem_path is None:
            raise HTTPException(410, "このPJは削除状態を確認できないため表示できません(状態を確認できません)")
        from app.project_delete import delete_state_status as _l3_delete_state
        state = _l3_delete_state(mem_path, project_id)
        if state in ("deleted", "purged"):
            raise HTTPException(410, "このPJは論理削除されています(削除済み一覧から復元できます)")
        if state == "unknown":
            raise HTTPException(410, "このPJは削除状態を確認できないため表示できません(状態を確定できません)")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(410, "このPJは削除状態を確認できないため表示できません(状態を確定できません)")
    return project


def require_project_including_deleted(project_id: str) -> dict:
    """L3削除系API用: 論理削除中も存在確認できる。"""
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
    # 一覧表示は読取専用接続を使い、実行中PJの復旧処理を起動しない。
    return ShortTermMemory(memory.path, read_only=True).list_projects()


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


def _planning_known_providers() -> tuple[set[str], list[str]]:
    """G1: その時点で選べる外部AI(接続済み)の提供元ID。APIキー・秘密は含めない。"""
    ids = sorted({
        str(item.get("id") or "") for item in provider_statuses()
        if isinstance(item, dict) and item.get("configured") and str(item.get("id") or "")
    })
    return set(ids), ids


@app.get("/api/projects/{project_id}/planning-settings")
async def get_planning_settings(project_id: str):
    require_project(project_id)
    from app.goal_review import ReviewStore
    from app.planning_settings import get_settings
    store = ReviewStore(memory.path)
    _known_set, known_ids = _planning_known_providers()
    return {"settings": get_settings(store, project_id), "known_providers": known_ids}


@app.put("/api/projects/{project_id}/planning-settings")
async def put_planning_settings(project_id: str, payload: dict):
    require_project(project_id)
    current = memory.get_mission(project_id)
    if current["status"] == "running":
        raise HTTPException(409, "実行中は目標を変更できません。先に一時停止してください")
    from app.goal_review import ReviewStore
    from app.planning_settings import update_settings
    changes = dict(payload or {})
    known_set, _known_ids = _planning_known_providers()
    try:
        return update_settings(ReviewStore(memory.path), project_id, changes, known_set)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _plan_packet_guard_inputs(project_id: str) -> tuple[list, list]:
    """G2-2: ProjectManager._outbound_guard と同じ取得方法(メモ除外)。

    想定内の例外は ValueError('check_error') に変換し、呼出側で422にする。
    """
    import re as _re
    import sqlite3 as _sqlite3
    _expected = (OSError, ValueError, RuntimeError, AttributeError, KeyError,
                 TypeError, UnicodeError, _re.error, _sqlite3.Error)
    try:
        items = memory.list_context_files(project_id, include_content=False)
    except _expected as exc:
        raise ValueError("check_error") from exc
    terms: list = []
    try:
        for item in items or []:
            if not isinstance(item, dict):
                continue
            if item.get("source") == "memo":
                continue
            for key in ("filename", "id"):
                value = item.get(key)
                if value is not None and str(value).strip():
                    terms.append(str(value))
    except _expected as exc:
        raise ValueError("check_error") from exc
    try:
        content_items = memory.list_context_files(project_id, include_content=True)
    except _expected as exc:
        raise ValueError("check_error") from exc
    source_texts: list = []
    try:
        for content_item in content_items or []:
            if not isinstance(content_item, dict):
                continue
            if content_item.get("source") == "memo":
                continue
            content_value = content_item.get("content")
            if isinstance(content_value, str) and content_value.strip():
                source_texts.append(content_value)
    except _expected as exc:
        raise ValueError("check_error") from exc
    return terms, source_texts


def _packet_public_view(packet: dict, include_text: bool) -> dict:
    view = {
        "id": packet.get("id"),
        "purpose": packet.get("purpose"),
        "provider": packet.get("provider"),
        "plan_signature": packet.get("plan_signature"),
        "sha256": packet.get("sha256"),
        "chars": packet.get("chars"),
        "included_fields": list(packet.get("included_fields") or []),
        "withheld_fields": list(packet.get("withheld_fields") or []),
        "withheld_count": packet.get("withheld_count"),
        "privacy": dict(packet.get("privacy") or {}),
        "status": packet.get("status"),
        "created_at": packet.get("created_at"),
    }
    if include_text:
        view["text"] = packet.get("text", "")
    return view


@app.post("/api/projects/{project_id}/plan-packets/preview")
async def preview_plan_packet(project_id: str, payload: dict):
    require_project(project_id)
    from app.goal_review import ReviewStore
    from app import planning_packet as _packet
    body = dict(payload or {})
    try:
        blocked_terms, source_texts = _plan_packet_guard_inputs(project_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    mission = memory.get_mission(project_id)
    structure_text = None
    if body.get("purpose") == "plan_review":
        from app.plan_review_structure import build_structure_text
        computed = build_structure_text(mission if isinstance(mission, dict) else {})
        if isinstance(computed, str) and computed.strip() != "":
            structure_text = computed
    try:
        packet = _packet.build_packet(
            ReviewStore(memory.path), project_id,
            purpose=body.get("purpose"),
            provider=body.get("provider"),
            public_items=body.get("public_items"),
            plan_signature=_packet.plan_signature_of(mission),
            blocked_terms=blocked_terms,
            source_texts=source_texts,
            withheld_fields=body.get("withheld_fields") or [],
            structure_text=structure_text,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if packet.get("status") == "blocked":
        return _packet_public_view(packet, include_text=False)
    return _packet_public_view(packet, include_text=True)


@app.post("/api/projects/{project_id}/plan-packets/{packet_id}/approve")
async def approve_plan_packet(project_id: str, packet_id: str, payload: dict):
    require_project(project_id)
    current = memory.get_mission(project_id)
    if current.get("status") == "running":
        raise HTTPException(409, "実行中は承認できません。先に一時停止してください")
    from app.goal_review import ReviewStore
    from app import planning_packet as _packet
    body = dict(payload or {})
    try:
        blocked_terms, source_texts = _plan_packet_guard_inputs(project_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        packet = _packet.approve_packet(
            ReviewStore(memory.path), project_id, packet_id,
            body.get("approver"),
            current_plan_signature=_packet.plan_signature_of(current),
            blocked_terms=blocked_terms,
            source_texts=source_texts,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _packet_public_view(packet, include_text=False)


@app.get("/api/projects/{project_id}/plan-packets/audit")
async def list_plan_packet_audit(project_id: str, packet_id: str | None = None):
    require_project(project_id)
    from app.goal_review import ReviewStore
    from app import planning_packet as _packet
    rows = _packet.list_audit(ReviewStore(memory.path), project_id, packet_id)
    return {"audit": rows}


@app.get("/api/projects/{project_id}/plan-packets/approvals")
async def list_plan_packet_approvals(project_id: str):
    require_project(project_id)
    from app.goal_review import ReviewStore
    from app import planning_packet as _packet
    from app import planning_settings as _settings
    store = ReviewStore(memory.path)
    try:
        listed = store.list(project_id, "plan_packet")
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
        raise HTTPException(503, "approval_list_unavailable") from exc
    try:
        mission = memory.get_mission(project_id)
        current_signature = _packet.plan_signature_of(mission if isinstance(mission, dict) else {})
    except (ValueError, KeyError, TypeError, AttributeError, OSError):
        current_signature = ""
    try:
        audit_rows = _packet.list_audit(store, project_id)
    except (ValueError, KeyError, TypeError, AttributeError, OSError):
        audit_rows = []
    last_approved: dict[str, dict] = {}
    for row in audit_rows or []:
        try:
            if not isinstance(row, dict):
                continue
            if str(row.get("action") or "") != "approved":
                continue
            pid_key = str(row.get("packet_id") or "")
            if not pid_key:
                continue
            prev = last_approved.get(pid_key)
            try:
                cur_at = float(row.get("at") or 0)
            except (TypeError, ValueError):
                cur_at = 0
            try:
                prev_at = float((prev or {}).get("at") or 0)
            except (TypeError, ValueError):
                prev_at = 0
            if prev is None or cur_at >= prev_at:
                last_approved[pid_key] = dict(row)
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    items: list[dict] = []
    created_list: list[float] = []
    for _signature, packet in listed or []:
        try:
            if not isinstance(packet, dict):
                continue
            if packet.get("status") != "approved":
                continue
            packet_id = str(packet.get("id") or "")
            purpose = str(packet.get("purpose") or "")
            provider = str(packet.get("provider") or "")
            sha256 = str(packet.get("sha256") or "")
            try:
                chars = int(packet.get("chars"))
            except (TypeError, ValueError):
                raise ValueError("bad chars")
            try:
                created_at = float(packet.get("created_at") or 0)
            except (TypeError, ValueError):
                created_at = 0
            if not packet_id or not purpose or not provider or not sha256:
                raise ValueError("bad packet")
            info = last_approved.get(packet_id) or {}
            try:
                approver = str(info.get("approver") or "")
            except (ValueError, TypeError, AttributeError):
                approver = ""
            try:
                approved_at = float(info.get("at") or 0)
            except (TypeError, ValueError):
                approved_at = 0
            try:
                binding = {"plan_signature": str(packet.get("plan_signature") or ""), "packet_sha256": str(packet.get("sha256") or ""), "provider": str(packet.get("provider") or ""), "purpose": str(packet.get("purpose") or "")}
                checked = _settings.check_permission(store, project_id, purpose, dict(binding))
                if not isinstance(checked, dict) or not checked.get("allowed"):
                    reason = str((checked or {}).get("reason") or "")
                    if reason == "method_not_external":
                        code = "method_not_external"
                    elif reason == "settings_changed":
                        code = "settings_changed"
                    elif reason == "no_permission":
                        code = "no_permission"
                    elif reason.startswith("binding_mismatch"):
                        code = "binding_mismatch"
                    else:
                        code = "check_error"
                    items.append({"packet_id": packet_id, "purpose": purpose, "provider": provider, "sha256": sha256, "chars": chars, "approver": approver, "approved_at": approved_at, "active": False, "inactive_reason": code})
                    created_list.append(created_at)
                    continue
                if not isinstance(current_signature, str) or current_signature == "":
                    raise ValueError("bad signature")
                if str(packet.get("plan_signature") or "") != current_signature:
                    items.append({"packet_id": packet_id, "purpose": purpose, "provider": provider, "sha256": sha256, "chars": chars, "approver": approver, "approved_at": approved_at, "active": False, "inactive_reason": "plan_changed"})
                    created_list.append(created_at)
                    continue
                text = packet.get("text")
                saved_sha = packet.get("sha256")
                if (not isinstance(text, str) or text == "" or not isinstance(saved_sha, str) or saved_sha == "" or hashlib.sha256(text.encode("utf-8")).hexdigest() != saved_sha):
                    items.append({"packet_id": packet_id, "purpose": purpose, "provider": provider, "sha256": sha256, "chars": chars, "approver": approver, "approved_at": approved_at, "active": False, "inactive_reason": "packet_tampered"})
                    created_list.append(created_at)
                    continue
                items.append({"packet_id": packet_id, "purpose": purpose, "provider": provider, "sha256": sha256, "chars": chars, "approver": approver, "approved_at": approved_at, "active": True, "inactive_reason": ""})
                created_list.append(created_at)
            except (ValueError, KeyError, TypeError, AttributeError, OSError):
                items.append({"packet_id": packet_id, "purpose": purpose, "provider": provider, "sha256": sha256, "chars": chars if isinstance(chars, int) else 0, "approver": approver if isinstance(approver, str) else "", "approved_at": approved_at if isinstance(approved_at, (int, float)) else 0, "active": False, "inactive_reason": "check_error"})
                created_list.append(created_at)
        except (ValueError, KeyError, TypeError, AttributeError, OSError):
            continue
    order = sorted(range(len(items)), key=lambda i: (float(items[i].get("approved_at") or 0), created_list[i]), reverse=True)
    return {"approvals": [items[i] for i in order]}


@app.get("/api/projects/{project_id}/plan-packets/{packet_id}/job-key")
async def get_plan_packet_job_key(project_id: str, packet_id: str):
    require_project(project_id)
    from app.goal_review import ReviewStore
    from app import planning_packet as _packet
    from app import external_plan_jobs as _jobs
    try:
        packet = _packet.get_packet(ReviewStore(memory.path), project_id, packet_id)
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
        raise HTTPException(503, "job_key_unavailable") from exc
    if packet is None or not isinstance(packet, dict):
        raise HTTPException(404, "packet_not_found")
    if packet.get("status") != "approved":
        raise HTTPException(409, "packet_not_approved")
    purpose = packet.get("purpose")
    provider = packet.get("provider")
    sha256 = packet.get("sha256")
    if purpose not in ("plan_generation", "plan_review"):
        raise HTTPException(422, "purpose_unsupported")
    if (not isinstance(provider, str) or provider == ""
            or not isinstance(sha256, str) or sha256 == ""
            or not isinstance(purpose, str) or purpose == ""):
        raise HTTPException(503, "job_key_unavailable")
    try:
        mission = memory.get_mission(project_id)
        signature = _packet.plan_signature_of(mission if isinstance(mission, dict) else {})
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
        raise HTTPException(503, "job_key_unavailable") from exc
    if not isinstance(signature, str) or signature == "":
        raise HTTPException(503, "job_key_unavailable")
    try:
        key = _jobs.idempotency_key_of(project_id, signature, sha256, provider, purpose)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise HTTPException(503, "job_key_unavailable") from exc
    if not isinstance(key, str) or key == "":
        raise HTTPException(503, "job_key_unavailable")
    return {"packet_id": str(packet.get("id") or packet_id), "purpose": purpose,
            "provider": provider, "idempotency_key": key}


@app.get("/api/projects/{project_id}/planning-job-capabilities")
async def get_planning_job_capabilities(project_id: str):
    require_project(project_id)
    from app import external_plan_jobs as _plan_jobs
    from app import independent_review as _review_jobs
    return {
        "plan_generation": _plan_jobs.enabled_for_project(
            os.environ.get("SEBAS_EXTERNAL_PLAN_JOBS_PROJECTS", ""), project_id),
        "plan_review": _review_jobs.enabled_for_project(
            os.environ.get("SEBAS_INDEPENDENT_REVIEW_PROJECTS", ""), project_id),
    }


@app.get("/api/projects/{project_id}/plan-packets/{packet_id}")
async def get_plan_packet(project_id: str, packet_id: str):
    require_project(project_id)
    from app.goal_review import ReviewStore
    from app import planning_packet as _packet
    packet = _packet.get_packet(ReviewStore(memory.path), project_id, packet_id)
    if packet is None:
        raise HTTPException(404, "packet not found")
    return _packet_public_view(packet, include_text=True)


@app.post("/api/projects/{project_id}/plan-packets/{packet_id}/revoke")
async def revoke_plan_packet(project_id: str, packet_id: str, payload: dict):
    require_project(project_id)
    from app.goal_review import ReviewStore
    from app import planning_packet as _packet
    body = dict(payload or {})
    try:
        packet = _packet.revoke_packet(
            ReviewStore(memory.path), project_id, packet_id, body.get("approver"))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _packet_public_view(packet, include_text=False)


_EXTERNAL_PLAN_TASKS: dict[str, asyncio.Task] = {}
_INDEPENDENT_REVIEW_TASKS: dict[str, asyncio.Task] = {}


async def _external_plan_runner(provider, prompt, system, max_tokens):
    response = await call_provider_with_metadata(provider, prompt, system,
                                                 max_tokens)
    return {"text": response.text, "model": response.model, "usage": {}}


async def _independent_review_runner(provider, prompt, system, max_tokens):
    response = await call_provider_with_metadata(provider, prompt, system,
                                                 max_tokens)
    return {"text": response.text, "model": response.model, "usage": {}}


@app.post("/api/projects/{project_id}/external-plan-jobs")
async def create_external_plan_job(project_id: str, payload: dict):
    require_project(project_id)
    from app.goal_review import ReviewStore
    from app import external_plan_jobs as _jobs
    from app import planning_packet as _packet
    body = dict(payload or {})
    mission = memory.get_mission(project_id)
    if isinstance(mission, dict) and mission.get("status") == "running":
        raise HTTPException(409, "実行中は送信できません。先に一時停止してください")
    signature = _packet.plan_signature_of(
        mission if isinstance(mission, dict) else {})
    enabled = _jobs.enabled_for_project(
        os.environ.get("SEBAS_EXTERNAL_PLAN_JOBS_PROJECTS", ""), project_id)
    if not enabled:
        raise HTTPException(403, detail="feature_disabled")
    try:
        blocked_terms, source_texts = _plan_packet_guard_inputs(project_id)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    try:
        prepared = _jobs.prepare_job(
            ReviewStore(memory.path), project_id,
            packet_id=body.get("packet_id"),
            provider=body.get("provider"),
            idempotency_key=body.get("idempotency_key"),
            current_plan_signature=signature,
            enabled=enabled,
            approver=body.get("approver"),
            blocked_terms=blocked_terms,
            source_texts=source_texts,
        )
    except ValueError as exc:
        code = str(exc)
        raise HTTPException(422, detail=code) from exc
    if not prepared.get("created"):
        return _jobs.public_view(prepared.get("job"))
    job = prepared.get("job") or {}
    job_id = str(job.get("id") or "")
    text = prepared.get("text") or ""

    async def _run_and_forget(store_path, pid, jid, body_text):
        from app.goal_review import ReviewStore as _Store
        from app import external_plan_jobs as _jobs_inner
        runner = globals().get("_external_plan_runner")
        try:
            await _jobs_inner.run_job(
                _Store(store_path), pid, jid, body_text, runner)
        finally:
            _EXTERNAL_PLAN_TASKS.pop(jid, None)

    task = asyncio.create_task(
        _run_and_forget(memory.path, project_id, job_id, text))
    _EXTERNAL_PLAN_TASKS[job_id] = task
    return JSONResponse(status_code=202,
                        content=_jobs.public_view(job))


@app.get("/api/projects/{project_id}/external-plan-jobs")
async def list_external_plan_jobs(project_id: str):
    require_project(project_id)
    from app.goal_review import ReviewStore
    from app import external_plan_jobs as _jobs
    return {"jobs": [_jobs.public_view(item) for item in
                     _jobs.list_jobs(ReviewStore(memory.path), project_id)]}


@app.get("/api/projects/{project_id}/external-plan-jobs/{job_id}")
async def get_external_plan_job(project_id: str, job_id: str):
    require_project(project_id)
    from app.goal_review import ReviewStore
    from app import external_plan_jobs as _jobs
    job = _jobs.get_job(ReviewStore(memory.path), project_id, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return _jobs.public_view(job)


@app.post("/api/projects/{project_id}/external-plan-jobs/{job_id}/cancel")
async def cancel_external_plan_job(project_id: str, job_id: str):
    require_project(project_id)
    from app.goal_review import ReviewStore
    from app import external_plan_jobs as _jobs
    try:
        job = _jobs.cancel_job(ReviewStore(memory.path), project_id, job_id)
    except ValueError as exc:
        code = str(exc)
        if code == "job_not_found":
            raise HTTPException(404, detail=code) from exc
        if code == "job_not_cancellable":
            raise HTTPException(409, detail=code) from exc
        raise HTTPException(422, detail=code) from exc
    return _jobs.public_view(job)


@app.post("/api/projects/{project_id}/independent-review-jobs")
async def create_independent_review_job(project_id: str, payload: dict):
    require_project(project_id)
    from app.goal_review import ReviewStore
    from app import independent_review as _jobs
    from app import planning_packet as _packet
    body = dict(payload or {})
    mission = memory.get_mission(project_id)
    if isinstance(mission, dict) and mission.get("status") == "running":
        raise HTTPException(409, "実行中は送信できません。先に一時停止してください")
    signature = _packet.plan_signature_of(
        mission if isinstance(mission, dict) else {})
    enabled = _jobs.enabled_for_project(
        os.environ.get("SEBAS_INDEPENDENT_REVIEW_PROJECTS", ""), project_id)
    if not enabled:
        raise HTTPException(403, detail="feature_disabled")
    try:
        blocked_terms, source_texts = _plan_packet_guard_inputs(project_id)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    try:
        prepared = _jobs.prepare_job(
            ReviewStore(memory.path), project_id,
            packet_id=body.get("packet_id"),
            provider=body.get("provider"),
            idempotency_key=body.get("idempotency_key"),
            current_plan_signature=signature,
            enabled=enabled,
            approver=body.get("approver"),
            blocked_terms=blocked_terms,
            source_texts=source_texts,
        )
    except ValueError as exc:
        code = str(exc)
        raise HTTPException(422, detail=code) from exc
    if not prepared.get("created"):
        return _jobs.public_view(prepared.get("job"))
    job = prepared.get("job") or {}
    job_id = str(job.get("id") or "")
    text = prepared.get("text") or ""

    async def _run_and_forget(store_path, pid, jid, body_text):
        from app.goal_review import ReviewStore as _Store
        from app import independent_review as _jobs_inner
        runner = globals().get("_independent_review_runner")
        try:
            await _jobs_inner.run_job(
                _Store(store_path), pid, jid, body_text, runner)
        finally:
            _INDEPENDENT_REVIEW_TASKS.pop(jid, None)

    task = asyncio.create_task(
        _run_and_forget(memory.path, project_id, job_id, text))
    _INDEPENDENT_REVIEW_TASKS[job_id] = task
    return JSONResponse(status_code=202,
                        content=_jobs.public_view(job))


@app.get("/api/projects/{project_id}/independent-review-jobs")
async def list_independent_review_jobs(project_id: str):
    require_project(project_id)
    from app.goal_review import ReviewStore
    from app import independent_review as _jobs
    return {"jobs": [_jobs.public_view(item) for item in
                     _jobs.list_jobs(ReviewStore(memory.path), project_id)]}


@app.get("/api/projects/{project_id}/independent-review-jobs/{job_id}")
async def get_independent_review_job(project_id: str, job_id: str):
    require_project(project_id)
    from app.goal_review import ReviewStore
    from app import independent_review as _jobs
    job = _jobs.get_job(ReviewStore(memory.path), project_id, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return _jobs.public_view(job)


@app.post("/api/projects/{project_id}/independent-review-jobs/{job_id}/cancel")
async def cancel_independent_review_job(project_id: str, job_id: str):
    require_project(project_id)
    from app.goal_review import ReviewStore
    from app import independent_review as _jobs
    try:
        job = _jobs.cancel_job(ReviewStore(memory.path), project_id, job_id)
    except ValueError as exc:
        code = str(exc)
        if code == "job_not_found":
            raise HTTPException(404, detail=code) from exc
        if code == "job_not_cancellable":
            raise HTTPException(409, detail=code) from exc
        raise HTTPException(422, detail=code) from exc
    return _jobs.public_view(job)


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
async def generate_project_plan(project_id: str, payload: dict | None = None):
    project = require_project(project_id)
    mission = memory.get_mission(project_id)
    if mission["status"] == "running":
        raise HTTPException(409, "実行中は計画を再生成できません")
    if not engine_enabled or not await llm.health():
        raise HTTPException(503, "計画生成に必要なローカルLLMが利用できません")
    body = payload if isinstance(payload, dict) else {}
    external_job_id = body.get("external_job_id") if body else None
    if external_job_id is None:
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
    if not isinstance(external_job_id, str) or not external_job_id.strip():
        raise HTTPException(422, "invalid_job_id")
    from app import external_plan_jobs as _ext_jobs
    from app.external_draft_hint import build_hint
    from app.goal_review import ReviewStore
    from app import planning_packet as _packet
    enabled = _ext_jobs.enabled_for_project(
        os.environ.get("SEBAS_EXTERNAL_PLAN_JOBS_PROJECTS", ""), project_id)
    if not enabled:
        raise HTTPException(403, detail="feature_disabled")
    job = _ext_jobs.get_job(ReviewStore(memory.path), project_id, external_job_id.strip())
    if job is None:
        raise HTTPException(404, detail="job_not_found")
    try:
        hint = build_hint(job, _packet.plan_signature_of(mission), project_id)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    execution_id = start_execution(
        "project_plan", mission["goal"] or project["name"], project_id, project["name"],
        "ローカルLLMが実行計画を作成中",
    )
    try:
        result = await orchestrator.generate_plan(project_id, external_hint=hint)
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
    # C5: メールの外部アクションは、承認時に宛先・件名・本文・運用区分の
    # 束縛を先に記録してから承認を確定する(B1・B2の欠陥の再発防止)。
    # 束縛の元データを読めない・記録に失敗したら承認を確定しない(409)。
    pending_binding: dict | None = None
    pending_mode = ""
    if str(action.get("kind") or "") == "email":
        try:
            from app.channel_skill_mail_send import record_approve_binding
            from app.contact_registry import current_operating_mode
            try:
                current = current_operating_mode(memory.path, project_id)
                pending_mode = str((current or {}).get("mode") or "") \
                    if isinstance(current, dict) else ""
            except Exception:
                pending_mode = ""
            pending_binding = await asyncio.to_thread(
                record_approve_binding, memory.path, project_id,
                dict(action),
                pending_mode if pending_mode in ("demo_test", "production")
                else "production")
            if not int(pending_binding.get("id") or 0):
                raise ValueError("承認の束縛のIDを確認できない")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(409, detail={
                "code": "MAIL_SEND_BINDING_WRITE_FAILED",
                "message": "承認の束縛を記録できないため承認を確定できません",
                "error_type": type(exc).__name__,
            }) from exc
    updated = memory.update_action(project_id, action_id, "approved")
    memory.add_event(
        project_id, "external_action_approved",
        f"外部アクションを承認: {action['kind']} → {action['target']}",
        action.get("task_id"), detail=f"action_id={action_id}",
    )
    if pending_binding is not None:
        updated = dict(updated)
        updated["mail_send_binding_id"] = int(pending_binding.get("id") or 0)
        updated["mail_send_binding_sha256"] = str(
            pending_binding.get("binding_sha256") or "")
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
    # C5: kind='email' は共通契約で包んだ送信経路で送る。既存のキー・既存の
    # 状態遷移(executed/failed)は変えず、send_state 等の追加キーだけ付ける。
    try:
        from app.channel_skill_mail_send import (
            MailSendNotConfigured, MailSendRefused, run_mail_send_approved,
        )
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        raise HTTPException(409, "承認済みメール送信を確認できません") from exc
    try:
        result = await asyncio.to_thread(
            run_mail_send_approved, memory.path, project_id, action_id)
    except MailSendNotConfigured as exc:
        raise HTTPException(409, detail={
            "code": "MAIL_SEND_NOT_CONFIGURED",
            "message": str(exc.reason or exc) or "needs_configuration",
            "reason": str(exc.reason or "needs_configuration"),
        }) from exc
    except MailSendRefused as exc:
        reason = str(exc.reason or "")
        raise HTTPException(409, detail={
            "code": "MAIL_SEND_REFUSED",
            "message": f"{reason}: {exc.detail}" if reason else str(exc),
            "reason": reason,
        }) from exc
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    send_state = str(result.get("send_state") or "")
    message = str(result.get("message") or "")
    reasons = list(result.get("reasons") or [])
    if send_state == "accepted":
        # 既存の成功の形(executed＋受理の文)を保つ。配達済みとは書かない。
        evidence = (
            f"SMTP accepted; recipient={action['target']}; "
            f"message_id={result.get('message_id') or ''}; "
            "SMTPサーバーが受理した(配達・既読・返信の証明ではない)")
        updated = memory.update_action(
            project_id, action_id, "executed", evidence=evidence[:2000])
        memory.add_event(
            project_id, "external_action_executed",
            f"承認済みメールを実行: {action['target']}", action.get("task_id"),
            detail=json.dumps({"action_id": action_id, "evidence": evidence},
                              ensure_ascii=False),
        )
        updated = dict(updated)
        updated["send_state"] = "accepted"
        updated["delivery_confirmed"] = False
        updated["message"] = message or (
            "SMTPサーバーが受理した(配達・既読・返信の証明ではない)")
        updated["reasons"] = list(reasons or [])
        updated["message_id"] = str(result.get("message_id") or "")
        return updated
    if send_state == "unknown":
        # 結果不明は failed に『結果不明・自動再送しない』の印だけ付ける。
        # executed/failed 以外の新状態は作らず、既存の状態遷移・画面・試験を壊さない。
        error = ("結果不明・自動再送しない: 人の判断待ち; "
                 + "; ".join(reasons[:3]) if reasons
                 else "結果不明・自動再送しない: 人の判断待ち")
        updated = memory.update_action(
            project_id, action_id, "failed", error=error[:2000])
        memory.add_event(
            project_id, "external_action_failed", "承認済みメール送信の結果が不明です",
            action.get("task_id"), detail=f"action_id={action_id}; error={error[:1000]}",
        )
        updated = dict(updated)
        updated["send_state"] = "unknown"
        updated["delivery_confirmed"] = False
        updated["message"] = message or "結果不明のため自動再送しない。人の判断待ち"
        updated["reasons"] = list(reasons or [])
        updated["message_id"] = str(result.get("message_id") or "")
        return updated
    error = ("送信前の失敗(確実に未送信); " + "; ".join(reasons[:3])
             if reasons else "送信前の失敗(確実に未送信)")
    memory.update_action(project_id, action_id, "failed", error=error[:2000])
    memory.add_event(
        project_id, "external_action_failed", "承認済みメール送信に失敗しました",
        action.get("task_id"), detail=f"action_id={action_id}; error={error[:1000]}",
    )
    failed = memory.get_action(project_id, action_id) or {}
    failed = dict(failed)
    failed["send_state"] = "failed"
    failed["delivery_confirmed"] = False
    failed["message"] = message or "送信前の失敗(確実に未送信)"
    failed["reasons"] = list(reasons or [])
    failed["message_id"] = str(result.get("message_id") or "")
    return failed


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
            memory_path=memory.path, project_id=project_id,
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
    evident = sorted({
        str(item.get("channel") or "")
        for item in previous
        if item.get("status") == "evidence_registered" or item.get("evidence_url")
    })
    if evident:
        raise HTTPException(
            409, "公開投稿URLが登録済みのため既存キットは再生成できません("
            + "、".join(evident)
            + ")。新しいキャンペーン版を作成してください",
        )
    # B1: 登録台帳の channel_selection で分岐する。読取失敗は全媒体生成や
    # 投稿不要で代用しない。未登録と読取不能と投稿不要を区別する。
    selection = resolve_channel_selection(memory.path, project_id, campaign_id)
    status = str(selection.get("status") or "")
    if status == "unreadable":
        raise HTTPException(
            409, "媒体の選択を読めないためSNS投稿キットを生成できません",
        )
    if status == "none_needed":
        drift_none = unselected_channel_drift(
            selection.get("channels") or [], previous)
        memory.add_event(
            project_id, "social_package_none_needed",
            f"投稿不要として登録済みのため生成しません: {campaign['title']}",
            detail=json.dumps({"campaign_id": campaign_id,
                               "unselected_channels_present": drift_none}, ensure_ascii=False)[:12000],
        )
        return {"status": "none_needed", "shares": [], "artifacts": [],
                "assets_path": f"premarketing/{campaign_id}/social",
                "regenerated": False, "archive_paths": [],
                "channel_basis": "none_needed_registered",
                "reason_ja": "投稿不要として登録済みのためキットを生成しません",
                "unselected_channels_present": drift_none}
    if status == "selected":
        channels = list(selection.get("channels") or [])
        channel_basis = "selected"
        reason_ja = "選択した媒体だけで生成します"
    else:
        channels = list(CHANNELS)
        channel_basis = "legacy_all_not_configured"
        reason_ja = "媒体が未設定のため既定の全媒体で生成した"
    # 選択が変わったときの再生成で、選択から外れた媒体に既存の証跡(公開URL
    # 登録済み)がある場合はその媒体のキットを黙って消さない。再生成を拒否する。
    deselected_evidence = sorted({
        str(item.get("channel") or "")
        for item in previous
        if str(item.get("channel") or "") not in set(channels)
        and (item.get("status") == "evidence_registered" or item.get("evidence_url"))
    })
    if deselected_evidence:
        raise HTTPException(
            409, "選択から外れた媒体に公開投稿の証跡があるため再生成できません: "
            + "、".join(deselected_evidence),
        )
    from app.contact_registry import RegistryReadError as _B1RegistryReadError
    try:
        identity_statuses = channel_identity_statuses(
            memory.path, project_id, campaign_id, channels)
    except _B1RegistryReadError:
        raise HTTPException(
            409, "媒体の選択を読めないためSNS投稿キットを生成できません",
        )
    try:
        items = social_package(campaign, channels)
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
    drift = unselected_channel_drift(channels, previous)
    # B2: 生成時の束縛を追記する。読取失敗は『束縛なし』に倒さず409にする。
    # 証跡登録済みの再生成は従来どおり拒否済みのため、ここでは新規 kit 行だけ追加する。
    binding_info: dict = {}
    try:
        from app.publication_binding import (
            BindingReadError as _B2BindingReadError,
            build_binding_payload,
            collect_registry_snapshot,
            current_packet_kit_hashes,
            record_binding,
        )
        registry_snapshot = collect_registry_snapshot(memory.path, project_id, campaign_id)
        packet_sha, kit_sha = current_packet_kit_hashes(
            memory.path, project_id, campaign_id, WORKSPACE_ROOT)
        saved_binding = record_binding(
            memory.path, project_id=project_id, campaign_id=campaign_id,
            stage="kit", binding=build_binding_payload(
                registry_snapshot, packet_sha, kit_sha),
            actor="")
        binding_info = {
            "binding_id": int(saved_binding.get("id") or 0),
            "binding_sha256": str(saved_binding.get("binding_sha256") or ""),
            "stage": "kit",
        }
    except _B2BindingReadError as exc:
        raise HTTPException(
            409, "束縛を記録できないためSNS投稿キットを生成できません") from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(
            409, f"束縛を記録できないためSNS投稿キットを生成できません: {exc}") from exc
    memory.add_event(
        project_id, "social_package_regenerated" if previous else "social_package_created",
        f"SNS投稿キットを{'再生成' if previous else '生成'}: {campaign['title']}",
        detail=json.dumps({"campaign_id": campaign_id, "channels": channels,
                           "channel_basis": channel_basis,
                           "identity_statuses": identity_statuses,
                           "regenerated": bool(previous),
                           "previous": [
                               {key: item.get(key) for key in (
                                   "channel", "status", "open_count", "evidence_url",
                                   "approved_at", "last_opened_at",
                               )}
                               for item in previous
                           ],
                           "archive_paths": archive_paths,
                           "artifacts": audit,
                           "binding": binding_info}, ensure_ascii=False)[:12000],
    )
    binding_status: dict = {}
    try:
        from app.publication_binding import check_publication_binding
        binding_status = check_publication_binding(
            memory.path, project_id, campaign_id, WORKSPACE_ROOT)
    except (OSError, ValueError):
        binding_status = {"status": "unreadable", "reasons": ["束縛の照合を読めない"]}
    safe_status = str(binding_status.get("status") or "unreadable")
    if safe_status not in ("ok", "stale", "no_binding", "blocked", "unreadable"):
        safe_status = "unreadable"
    binding_payload = {"binding_status": safe_status,
                       "reasons": list(binding_status.get("reasons") or [])}
    return {"shares": shares, "artifacts": audit, "assets_path": base,
            "regenerated": bool(previous), "archive_paths": archive_paths,
            "channels": channels, "channel_basis": channel_basis,
            "reason_ja": reason_ja, "identity_status": identity_statuses,
            "unselected_channels_present": drift,
            "binding": binding_info,
            **binding_payload}


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
        detail=json.dumps({"campaign_id": campaign_id, "channels": [
            str(item.get("channel") or "") for item in shares
            if str(item.get("channel") or "")]},
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
    # B2: 承認前に、束縛の正本と成果物を読み取れることを確認する。
    try:
        from app.publication_binding import (
            BindingReadError as _B2BindingReadError,
            build_binding_payload,
            check_publication_binding,
            collect_registry_snapshot,
            current_packet_kit_hashes,
            record_binding,
        )
        registry_snapshot = collect_registry_snapshot(memory.path, project_id, campaign_id)
        packet_sha, kit_sha = current_packet_kit_hashes(
            memory.path, project_id, campaign_id, WORKSPACE_ROOT)
        binding_to_approve = build_binding_payload(
            registry_snapshot, packet_sha, kit_sha)
    except _B2BindingReadError as exc:
        raise HTTPException(
            409, "承認の束縛を読めないため承認を確定できません") from exc
    except Exception as exc:
        # 束縛の元データを読めないなら承認を確定しない(承認前に止める)。
        raise HTTPException(
            409, f"承認の束縛を読めないため承認を確定できません: {type(exc).__name__}") from exc
    try:
        shares = memory.update_social_campaign_status(
            project_id, campaign_id, "awaiting_approval", "approved",
        )
    except ValueError as exc:
        raise HTTPException(409, "承認待ちのSNS文案がありません") from exc
    approved_row = next((item for item in shares
                         if str(item.get("approved_at") or "")), None)
    approved_at = str((approved_row or {}).get("approved_at") or "")
    approved_binding: dict = {}
    try:
        approved_binding = record_binding(
            memory.path, project_id=project_id, campaign_id=campaign_id,
            stage="approved", binding=binding_to_approve,
            approved_at=approved_at, actor="")
        if not int(approved_binding.get("id") or 0):
            raise ValueError("承認の束縛のIDを確認できない")
    except Exception as exc:
        # 承認は確定済み。束縛を残せないなら承認済みのまま残さず再承認待ちへ戻す。
        # 戻せたかは共有行を読み直して確かめる(戻ったと推定しない)。
        invalidation = _invalidate_social_approval_on_stale(
            memory, project_id, campaign_id, "unreadable",
            ["承認の束縛を記録できない"])
        reverted = not _social_approval_still_present(memory, project_id, campaign_id)
        invalidation = {**invalidation, "reverted_to_awaiting": reverted}
        if not reverted:
            raise HTTPException(409, detail={
                "code": "PUBLICATION_BINDING_WRITE_FAILED",
                "message": "承認の束縛を記録できず、承認を取り消せたことも確認できません",
                "invalidation": invalidation,
            }) from exc
        raise HTTPException(409, detail={
            "code": "PUBLICATION_BINDING_WRITE_FAILED",
            "message": "承認の束縛を記録できないため再承認待ちに戻しました",
            "invalidation": invalidation,
        }) from exc
    # 承認後の照合結果を応答に載せる(承認前の結果ではなく、記録後の状態)。
    try:
        binding_status = check_publication_binding(
            memory.path, project_id, campaign_id, WORKSPACE_ROOT)
    except (OSError, ValueError, TypeError, AttributeError):
        binding_status = {"status": "unreadable", "reasons": ["束縛の照合を読めない"]}
    safe_status = str(binding_status.get("status") or "unreadable")
    if safe_status not in ("ok", "stale", "no_binding", "blocked", "unreadable"):
        safe_status = "unreadable"
    binding_payload = {"binding_status": safe_status,
                       "reasons": list(binding_status.get("reasons") or []),
                       "binding_id": int(approved_binding.get("id") or 0),
                       "binding_sha256": str(approved_binding.get("binding_sha256") or ""),
                       "approved_binding_id": int(approved_binding.get("id") or 0),
                       "approved_binding_sha256": str(
                           approved_binding.get("binding_sha256") or "")}
    memory.add_event(
        project_id, "social_publication_approved",
        f"SNS投稿文案を承認: {campaign['title']}",
        detail=json.dumps({"campaign_id": campaign_id, "channels": [
            str(item.get("channel") or "") for item in shares
            if str(item.get("channel") or "")],
            **binding_payload}, ensure_ascii=False),
    )
    return {"shares": shares, **binding_payload}


@app.post("/api/projects/{project_id}/premarketing/campaigns/{campaign_id}/social/{channel}/open")
async def open_social_composer(project_id: str, campaign_id: str, channel: str):
    require_project(project_id)
    if channel not in CHANNELS:
        raise HTTPException(404, "対応していないSNSです")
    # B2: 公開前の束縛照合。ok のときだけ進める。
    # 照合の前に承認済み行を書き換えない。stale 検出時は承認の無効化を残す。
    from app.publication_binding import check_publication_binding
    try:
        binding_status = check_publication_binding(
            memory.path, project_id, campaign_id, WORKSPACE_ROOT)
    except (OSError, ValueError):
        binding_status = {"status": "unreadable",
                          "reasons": ["束縛の照合を読めない"]}
    safe_status = str(binding_status.get("status") or "unreadable")
    if safe_status not in ("ok", "stale", "no_binding", "blocked", "unreadable"):
        safe_status = "unreadable"
    if safe_status != "ok":
        raise _publication_binding_block(
            memory, project_id, campaign_id, safe_status,
            list(binding_status.get("reasons") or []))
    try:
        share = memory.mark_social_opened(project_id, campaign_id, channel)
    except ValueError as exc:
        raise HTTPException(409, "承認済みのSNS文案だけを利用できます") from exc
    memory.add_event(
        project_id, "social_composer_opened",
        f"{share['label']}の投稿操作を開始（投稿成功は未確認）",
        detail=json.dumps({"campaign_id": campaign_id, "channel": channel,
                           "open_count": share["open_count"],
                           "binding_status": "ok", "reasons": []}, ensure_ascii=False),
    )
    response = {key: share[key] for key in (
        "channel", "label", "mode", "post_text", "tracking_url", "compose_url",
        "status", "open_count",
    )}
    response.update({
        "binding_status": "ok", "reasons": [],
        "approved_binding_id": int(binding_status.get("approved_binding_id") or 0),
        "approved_binding_sha256": str(binding_status.get("approved_binding_sha256") or ""),
    })
    return response


@app.post("/api/projects/{project_id}/premarketing/campaigns/{campaign_id}/social/{channel}/evidence")
async def register_social_evidence(project_id: str, campaign_id: str, channel: str,
                                   payload: SocialEvidencePayload):
    require_project(project_id)
    # B2: 公開前の束縛照合と証跡URLの検証。すべて満たすときだけ投稿済みにする。
    # (a)承認→登録の順序、(b)openだけでは投稿済みにしない、(c)httpsと許可ホスト、
    # (d)選択媒体の共有行だけ、(e)束縛がok。失敗は409。書込前に照合を済ませる。
    from app.publication_binding import check_publication_binding
    try:
        binding_status = check_publication_binding(
            memory.path, project_id, campaign_id, WORKSPACE_ROOT)
    except (OSError, ValueError):
        binding_status = {"status": "unreadable",
                          "reasons": ["束縛の照合を読めない"]}
    safe_status = str(binding_status.get("status") or "unreadable")
    if safe_status not in ("ok", "stale", "no_binding", "blocked", "unreadable"):
        safe_status = "unreadable"
    binding_reasons = list(binding_status.get("reasons") or [])
    approved_binding_id = 0
    approved_binding_sha256 = ""
    try:
        approved = (binding_status.get("approved_binding")
                    if isinstance(binding_status, dict) else None)
        if isinstance(approved, dict):
            approved_binding_id = int(approved.get("id") or 0)
            approved_binding_sha256 = str(approved.get("binding_sha256") or "")
        if not approved_binding_sha256:
            approved_binding_sha256 = str(
                binding_status.get("approved_binding_sha256") or "")
            approved_binding_id = int(
                binding_status.get("approved_binding_id") or approved_binding_id)
    except (TypeError, ValueError, AttributeError):
        approved_binding_id = 0
        approved_binding_sha256 = ""
    if safe_status != "ok":
        raise _publication_binding_block(
            memory, project_id, campaign_id, safe_status, binding_reasons)
    try:
        evidence_url = validate_evidence_url(channel, payload.public_post_url)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    # (d)選択媒体の共有行だけ。選択外の媒体へのURLは拒否する。
    try:
        from app.social_premarketing import resolve_channel_selection
        selection = resolve_channel_selection(memory.path, project_id, campaign_id)
        selection_status = str(selection.get("status") or "")
        selection_channels = list(selection.get("channels") or [])
    except (OSError, ValueError, TypeError, AttributeError, KeyError, ImportError):
        raise HTTPException(409, "媒体の選択を読めないため証跡を登録できません")
    if selection_status == "selected" and str(channel) not in {
            str(item) for item in selection_channels}:
        raise HTTPException(409, "選択された媒体の共有行だけに登録できます")
    if selection_status == "unreadable":
        raise HTTPException(409, "媒体の選択を読めないため証跡を登録できません")
    if selection_status == "none_needed":
        raise HTTPException(409, "投稿不要として登録済みのため証跡を登録できません")
    # 既存の検査(承認・open・URL)は弱めない。足りない分だけ足す。
    # (a)承認時刻があり承認後に登録。(b)openだけでは投稿済みにしない。
    # (c)httpsと許可ホストは validate_evidence_url で検査済み。
    try:
        previous = memory.get_social_share(project_id, campaign_id, channel)
        if previous is None:
            raise HTTPException(409, "対象のSNS文案がありません")
        if str(previous.get("status") or "") not in ("approved", "composer_opened",
                                                      "evidence_registered"):
            raise HTTPException(409, "承認済みのSNS文案だけに登録できます")
        approved_at = str(previous.get("approved_at") or "")
        if not approved_at:
            raise HTTPException(409, "承認時刻が無いため証跡を登録できません")
        if str(previous.get("status") or "") == "approved" and int(
                previous.get("open_count") or 0) <= 0 and not previous.get(
                    "last_opened_at"):
            # openしていない承認直後は進めるが、証跡の成立は登録後の照合で行う。
            # ここでは拒否しない(投稿画面を開かず直接URL登録する運用を許す)。
            pass
        share = memory.register_social_evidence(
            project_id, campaign_id, channel, evidence_url,
            publication_binding_id=approved_binding_id,
            publication_binding_sha256=approved_binding_sha256,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    # 冪等の再送は既存のまま(証跡は変えない)。新規登録だけイベントと束縛を結ぶ。
    if not (previous and previous.get("status") == "evidence_registered"
            and previous.get("evidence_url") == evidence_url):
        memory.add_event(
            project_id, "social_evidence_registered",
            f"{share['label']}の公開投稿URLを登録",
            detail=json.dumps({"campaign_id": campaign_id, "channel": channel,
                               "public_post_url": evidence_url,
                               "binding_status": "ok", "reasons": [],
                               "binding_id": approved_binding_id,
                               "binding_sha256": approved_binding_sha256},
                              ensure_ascii=False),
        )
    response = dict(share)
    response["binding_status"] = "ok"
    response["reasons"] = []
    response["binding_id"] = approved_binding_id
    response["binding_sha256"] = approved_binding_sha256
    response["approved_binding_id"] = approved_binding_id
    response["approved_binding_sha256"] = approved_binding_sha256
    return response


@app.get("/api/projects/{project_id}/premarketing/campaigns/{campaign_id}/publication-binding")
async def get_publication_binding(project_id: str, campaign_id: str):
    require_project(project_id)
    from app.publication_binding import check_publication_binding
    try:
        result = check_publication_binding(
            memory.path, project_id, campaign_id, WORKSPACE_ROOT)
    except (OSError, ValueError) as exc:
        raise HTTPException(409, "束縛の照合を読めません") from exc
    safe_status = str(result.get("status") or "unreadable")
    if safe_status not in ("ok", "stale", "no_binding", "blocked", "unreadable"):
        safe_status = "unreadable"
    return {"project_id": project_id, "campaign_id": campaign_id,
            "binding_status": safe_status,
            "reasons": list(result.get("reasons") or []),
            "approved_binding_id": int(result.get("approved_binding_id") or 0),
            "approved_binding_sha256": str(
                result.get("approved_binding_sha256") or ""),
            "read_only": True}


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



def _record_manual_preview_manifests(project_id: str, project: dict,
                                     audit_entries: list[dict]) -> None:
    """P1-1: 手動操作/アップロードで保存された成果フォルダ配下は manual_preview を記録。

    実行工程の manifest を手動操作が上書きしたら origin が manual_preview に
    変わる(逆も同様に project_manager 側で execution に変わる)。
    記録の失敗は握りつぶさず artifact_manifest_failed として残すが、
    本体の保存結果(呼び出し側の戻り値)は変えない。
    """
    if not audit_entries or workspace is None or memory is None:
        return
    try:
        from app.artifact_manifest import build_manifest as _build_manifest
    except (ImportError, AttributeError, ValueError):
        return
    for item in audit_entries:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action", "")).strip().lower()
        if action == "upload":
            relative = str(item.get("path", "")).replace("\\", "/").lstrip("/")
        elif action in {"write_text", "append_text", "copy", "move", "write_bytes"}:
            relative = str(item.get("path", "")).replace("\\", "/").lstrip("/")
        else:
            continue
        if not relative:
            continue
        if not relative.startswith(("成果フォルダ/", "output/", "result/")):
            continue
        try:
            _, _, target = workspace.resolve_file(
                project.get("workspace_path", ""), project_id, relative,
                must_exist=True,
            )
            if not target.is_file():
                continue
            manifest = _build_manifest(
                project_id=project_id, plan_signature="", task_id="",
                path=relative, origin="manual_preview",
                content_bytes=target.read_bytes(), source_hashes=[],
                validation_state="unverified",
            )
            workspace.record_artifact_manifest(
                project.get("workspace_path", ""), project_id, manifest,
            )
        except (OSError, ValueError, RuntimeError, AttributeError,
                KeyError, TypeError) as exc:
            try:
                memory.add_event(
                    project_id, "artifact_manifest_failed",
                    f"手動成果物の manifest 記録に失敗しました: {relative}",
                    detail=type(exc).__name__[:2000],
                )
            except (OSError, ValueError, RuntimeError, AttributeError,
                    KeyError, TypeError):
                pass
            continue

@app.get("/api/projects/{project_id}/workspace")
async def list_project_workspace(project_id: str):
    project = require_project(project_id)
    try:
        listing = workspace.list_entries(project["workspace_path"], project_id)
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        # P1-1: 画面JSは変更せず、API応答に origin/validation_state/draft を足す。
        from app.artifact_manifest import summarize_manifest as _summarize

        enriched: list[dict] = []
        for entry in listing.get("entries", []) or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("kind") != "file":
                enriched.append(entry)
                continue
            manifest = workspace.read_artifact_manifest(
                project["workspace_path"], project_id, str(entry.get("path", "")))
            summary = _summarize(manifest)
            item = dict(entry)
            item["origin"] = summary["origin"]
            item["validation_state"] = summary["validation_state"]
            item["draft"] = summary["draft"]
            enriched.append(item)
        listing = {**listing, "entries": enriched}
    except (ValueError, OSError, AttributeError, KeyError, TypeError):
        listing = {**listing, "entries": [
            {**entry, "origin": "unknown", "validation_state": "unverified", "draft": True}
            if isinstance(entry, dict) and entry.get("kind") == "file" else entry
            for entry in listing.get("entries", []) or []
        ]}
    return listing


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
    _record_manual_preview_manifests(project_id, project, audit)
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
    _record_manual_preview_manifests(
        project_id, project, [{**saved, "action": "upload"}] if isinstance(saved, dict) else [])
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
    # P1-1: ZIP内の既存ファイル出力は壊さない(ファイル名も変えない)。
    # MANIFEST.json(各ファイルのmanifest要約。本文なし)と
    # README_試験用ドラフト.txt(draft一覧)を追加する。
    manifest_entries: dict[str, dict] = {}
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
                try:
                    from app.artifact_manifest import (
                        summarize_manifest as _zip_summarize,
                        verify_artifact as _zip_verify,
                    )

                    stored = workspace.read_artifact_manifest(
                        project["workspace_path"], project_id, relative)
                    summary = _zip_summarize(stored)
                    if stored is not None:
                        try:
                            checked = _zip_verify(stored, path.read_bytes())
                            summary = {**summary,
                                       "verified": checked.get("status"),
                                       "verify_reason": checked.get("reason", "")}
                            if checked.get("status") != "valid":
                                summary["validation_state"] = checked.get("status") or "unverified"
                                summary["draft"] = True
                        except (OSError, ValueError, AttributeError,
                                KeyError, TypeError):
                            summary = {**summary, "validation_state": "unverified",
                                       "verified": "unverified", "verify_reason": "verify_error",
                                       "draft": True}
                    manifest_entries[relative.replace("\\", "/")] = summary
                except (OSError, ValueError, AttributeError, KeyError, TypeError):
                    manifest_entries[relative.replace("\\", "/")] = {
                        "origin": "unknown", "validation_state": "unverified",
                        "artifact_sha256": "", "plan_signature": "",
                        "task_id": "", "draft": True,
                    }
        if manifest_entries:
            bundle.writestr("MANIFEST.json", json.dumps(
                {"version": "p11-artifact-manifest/v1",
                 "project_id": project_id, "files": manifest_entries},
                ensure_ascii=False, indent=2, sort_keys=True))
            drafts = sorted(path for path, info in manifest_entries.items()
                            if info.get("draft"))
            if drafts:
                bundle.writestr(
                    "README_試験用ドラフト.txt",
                    "このZIPには試験用ドラフトが含まれます。\n"
                    "試験用ドラフト(manual_preview/manifest無し)は達成条件の"
                    "自動合格に算入されません。\n\n"
                    + "".join(f"- {name}: 試験用ドラフト\n" for name in drafts),
                )
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
    # Legacy immediate deletion bypasses backup, restore, and cross-store cleanup.
    # Refuse it; the lifecycle endpoints require an explicit preview and confirmation.
    raise HTTPException(
        409,
        "従来の即時削除は無効です。プロジェクト管理の削除プレビューから"
        "退避・復元可能な削除を実行してください。",
    )


def _lifecycle_backup_root() -> Path:
    return DATA_DIR / "project_lifecycle_backups"


class LifecycleBackupPayload(BaseModel):
    actor: str = Field(min_length=1, max_length=100)


@app.get("/api/projects/{project_id}/lifecycle/delete-preview")
async def lifecycle_delete_preview(project_id: str):
    require_project(project_id)
    from app.project_lifecycle_registry import build_delete_preview
    return build_delete_preview(memory.path, project_id, WORKSPACE_ROOT, executions)


@app.post("/api/projects/{project_id}/lifecycle/backup")
async def lifecycle_backup(project_id: str, payload: LifecycleBackupPayload):
    require_project(project_id)
    actor = payload.actor.strip()
    if not actor:
        raise HTTPException(422, "actor is required")
    from app.project_lifecycle_backup import create_backup
    from app.project_lifecycle_registry import has_running_jobs
    jobs = has_running_jobs(memory.path, project_id, executions)
    if jobs["has_running"]:
        raise HTTPException(409, "実行中ジョブがあるため退避を拒否します。停止/取消後に再実行してください")
    result = create_backup(memory.path, project_id, _lifecycle_backup_root(),
                           workspace_root=WORKSPACE_ROOT, actor=actor)
    if not result.get("ok"):
        raise HTTPException(409, str(result.get("reason") or "backup failed"))
    memory.add_event(
        project_id, "lifecycle_backup_created",
        f"PJ退避を作成: {result['backup_id']}",
        detail=json.dumps({"backup_id": result["backup_id"],
                           "total_rows": result["total_rows"]}, ensure_ascii=False),
    )
    return result


@app.get("/api/projects/{project_id}/lifecycle/backups")
async def lifecycle_backups(project_id: str):
    require_project(project_id)
    from app.project_lifecycle_backup import list_backups
    return {"project_id": project_id,
            "backups": list_backups(_lifecycle_backup_root(), project_id)}


@app.post("/api/projects/{project_id}/lifecycle/backups/{backup_id}/verify")
async def lifecycle_backup_verify(project_id: str, backup_id: str):
    require_project(project_id)
    safe = "".join(ch if (ch.isalnum() or ch in ("-", "_")) else "_" for ch in backup_id)[:128]
    if not safe or safe != backup_id:
        raise HTTPException(400, "invalid backup_id")
    manifest_path = _lifecycle_backup_root() / backup_id / "manifest.json"
    if not manifest_path.is_file():
        raise HTTPException(404, "backup not found")
    from app.project_lifecycle_backup import verify_backup
    result = verify_backup(manifest_path)
    if str(result.get("project_id") or "") != project_id:
        raise HTTPException(409, "backup belongs to another project")
    return result


class LifecycleInitPayload(BaseModel):
    mode: str = Field(pattern="^(replan|rerun|fresh)$")
    preview_token: str = Field(min_length=1, max_length=256)
    project_name: str = Field(min_length=1, max_length=120)
    actor: str = Field(min_length=1, max_length=100)
    reason: str = Field(default="", max_length=2000)
    idempotency_key: str = Field(default="", max_length=128)


class LifecycleDeleteRequestPayload(BaseModel):
    preview_token: str = Field(min_length=1, max_length=256)
    project_name: str = Field(min_length=1, max_length=120)
    actor: str = Field(min_length=1, max_length=100)
    reason: str = Field(default="", max_length=2000)
    idempotency_key: str = Field(default="", max_length=128)
    retention_days: int | None = Field(default=None, ge=0, le=365)


class LifecycleDeleteRestorePayload(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(default="", max_length=128)


class LifecyclePurgePayload(BaseModel):
    purge_token: str = Field(min_length=1, max_length=256)
    project_name: str = Field(min_length=1, max_length=120)
    actor: str = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(default="", max_length=128)


@app.get("/api/projects/{project_id}/lifecycle/delete-request-preview")
async def lifecycle_delete_request_preview(project_id: str):
    require_project_including_deleted(project_id)
    from app.project_delete import build_delete_request_preview
    return build_delete_request_preview(memory.path, project_id, WORKSPACE_ROOT,
                                        executions)


@app.post("/api/projects/{project_id}/lifecycle/delete-request")
async def lifecycle_delete_request(project_id: str, payload: LifecycleDeleteRequestPayload):
    require_project_including_deleted(project_id)
    if not payload.actor.strip():
        raise HTTPException(422, "actor is required")
    from app.project_delete import request_delete
    result = request_delete(
        memory.path, project_id, preview_token=payload.preview_token,
        project_name=payload.project_name.strip(), actor=payload.actor.strip(),
        reason=payload.reason.strip(),
        idempotency_key=payload.idempotency_key.strip(),
        workspace_root=WORKSPACE_ROOT, backup_root=_lifecycle_backup_root(),
        executions=executions, retention_days=payload.retention_days)
    if not result.get("ok"):
        code = result.get("code") or ""
        if code in ("LEASE_BUSY",):
            raise HTTPException(409, str(result.get("reason") or "busy"))
        raise HTTPException(409, str(result.get("reason") or "delete-request failed"))
    return result


@app.post("/api/projects/{project_id}/lifecycle/delete-restore")
async def lifecycle_delete_restore(project_id: str, payload: LifecycleDeleteRestorePayload):
    require_project_including_deleted(project_id)
    if not payload.actor.strip():
        raise HTTPException(422, "actor is required")
    from app.project_delete import restore_deleted
    result = restore_deleted(
        memory.path, project_id, actor=payload.actor.strip(),
        idempotency_key=payload.idempotency_key.strip(),
        workspace_root=WORKSPACE_ROOT, backup_root=_lifecycle_backup_root())
    if not result.get("ok"):
        raise HTTPException(409, str(result.get("reason") or "restore failed"))
    return result


@app.get("/api/projects/{project_id}/lifecycle/purge-preview")
async def lifecycle_purge_preview(project_id: str):
    require_project_including_deleted(project_id)
    from app.project_delete import build_purge_preview
    return build_purge_preview(memory.path, project_id, WORKSPACE_ROOT)


@app.post("/api/projects/{project_id}/lifecycle/purge")
async def lifecycle_purge(project_id: str, payload: LifecyclePurgePayload):
    require_project_including_deleted(project_id)
    if not payload.actor.strip():
        raise HTTPException(422, "actor is required")
    from app.project_delete import purge_deleted
    result = purge_deleted(
        memory.path, project_id, purge_token=payload.purge_token,
        project_name=payload.project_name.strip(), actor=payload.actor.strip(),
        idempotency_key=payload.idempotency_key.strip(),
        workspace_root=WORKSPACE_ROOT, backup_root=_lifecycle_backup_root())
    if not result.get("ok"):
        raise HTTPException(409, str(result.get("reason") or "purge failed"))
    return result


@app.get("/api/projects/deleted/list")
async def lifecycle_deleted_list():
    from app.project_delete import list_deleted
    states = list_deleted(memory.path)
    try:
        projects = memory.list_projects_including_deleted()
    except Exception:
        projects = memory.list_projects()
    names = {p["id"]: p.get("name", "") for p in projects}
    for st in states:
        st["project_name"] = names.get(st.get("project_id", ""), "")
    return {"deleted": states}


class LifecycleRestorePayload(BaseModel):
    backup_id: str = Field(min_length=1, max_length=128)
    actor: str = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(default="", max_length=128)


@app.get("/api/projects/{project_id}/lifecycle/reset-preview")
async def lifecycle_reset_preview(project_id: str, mode: str = "replan"):
    require_project(project_id)
    if mode not in ("replan", "rerun", "fresh"):
        raise HTTPException(400, "unknown mode")
    from app.project_generation import build_reset_preview
    preview = build_reset_preview(memory.path, project_id, mode, WORKSPACE_ROOT,
                                  executions)
    # 秘密値は返さない: トークン自体は確定に必要だが、ログには残さない。
    return preview


@app.post("/api/projects/{project_id}/lifecycle/initialize")
async def lifecycle_initialize(project_id: str, payload: LifecycleInitPayload):
    require_project(project_id)
    if not payload.actor.strip():
        raise HTTPException(422, "actor is required")
    from app.project_generation import initialize_project
    result = initialize_project(
        memory.path, project_id, payload.mode,
        preview_token=payload.preview_token,
        project_name=payload.project_name.strip(),
        actor=payload.actor.strip(), reason=payload.reason.strip(),
        idempotency_key=payload.idempotency_key.strip(),
        workspace_root=WORKSPACE_ROOT, backup_root=_lifecycle_backup_root(),
        executions=executions)
    if not result.get("ok"):
        code = result.get("code") or ""
        if code in ("LEASE_BUSY",):
            raise HTTPException(409, str(result.get("reason") or "busy"))
        if "preview token" in str(result.get("reason") or "") or "PJ名" in str(result.get("reason") or ""):
            raise HTTPException(409, str(result.get("reason")))
        if "既定PJ" in str(result.get("reason") or ""):
            raise HTTPException(409, str(result.get("reason")))
        if "実行中" in str(result.get("reason") or "") or "未確定" in str(result.get("reason") or "") or "送信待ち" in str(result.get("reason") or ""):
            raise HTTPException(409, str(result.get("reason")))
        raise HTTPException(409, str(result.get("reason") or "initialize failed"))
    return result


@app.post("/api/projects/{project_id}/lifecycle/restore-generation")
async def lifecycle_restore_generation(project_id: str, payload: LifecycleRestorePayload):
    require_project(project_id)
    if not payload.actor.strip():
        raise HTTPException(422, "actor is required")
    from app.project_generation import restore_generation
    result = restore_generation(
        memory.path, project_id, payload.backup_id.strip(),
        actor=payload.actor.strip(),
        idempotency_key=payload.idempotency_key.strip(),
        workspace_root=WORKSPACE_ROOT, backup_root=_lifecycle_backup_root())
    if not result.get("ok"):
        raise HTTPException(409, str(result.get("reason") or "restore failed"))
    return result


@app.get("/api/projects/{project_id}/lifecycle/generations")
async def lifecycle_generations(project_id: str):
    require_project(project_id)
    from app.project_generation import list_generations
    return {"project_id": project_id,
            "generations": list_generations(memory.path, project_id)}


@app.get("/static/project_reset.js")
async def project_reset_js():
    return FileResponse(ROOT / "app" / "static" / "project_reset.js", media_type="text/javascript", headers={"Cache-Control": "no-store"})


@app.get("/static/project_delete.js")
async def project_delete_js():
    return FileResponse(ROOT / "app" / "static" / "project_delete.js", media_type="text/javascript", headers={"Cache-Control": "no-store"})


@app.get("/static/capability_gap.js")
async def capability_gap_js():
    return FileResponse(ROOT / "app" / "static" / "capability_gap.js", media_type="text/javascript", headers={"Cache-Control": "no-store"})

@app.get("/static/domain_pack.js")
async def domain_pack_js():
    return FileResponse(ROOT / "app" / "static" / "domain_pack.js", media_type="text/javascript", headers={"Cache-Control": "no-store"})

@app.get("/static/project_management.js")
async def project_management_js():
    return FileResponse(ROOT / "app" / "static" / "project_management.js", media_type="text/javascript", headers={"Cache-Control": "no-store"})


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


@app.get('/api/projects/{project_id}/stop-class')
async def get_stop_class(project_id: str):
    require_project(project_id)
    from app.stop_classifier import classify_stop
    from app.workflow_readiness import build_readiness, build_stop_class
    # 読み取り専用・副作用なし: build_readiness は読むだけ、履歴は書かない。
    built = build_readiness(orchestrator, project_id)
    record = build_stop_class(orchestrator, project_id, built)
    # 防衛: classify 出力の形を保証する (既存キーは触らない)。
    if not isinstance(record, dict) or not record.get("stop_class"):
        record = classify_stop({})
        record.setdefault("preflight_status", "unknown")
        record.setdefault("preflight_reason", "empty_record")
    return {
        "project_id": project_id,
        "plan_signature": built.get("plan_signature"),
        "phase": built.get("phase"),
        "stop_class": record,
        "preflight_status": str(record.get("preflight_status") or "unknown"),
        "external_sends": int(record.get("external_sends") or 0),
        "read_only": True,
    }


@app.get('/api/projects/{project_id}/execution-funnel')
async def get_execution_funnel(project_id: str):
    require_project(project_id)
    from app.execution_funnel import project_funnel
    return project_funnel(orchestrator, project_id)


@app.get('/api/execution-funnel')
async def get_execution_funnel_portfolio():
    from app.execution_funnel import portfolio_funnel
    return portfolio_funnel(orchestrator)


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


class AutopilotSettingPayload(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2000)
    gate_override_reason: str = Field(default="", max_length=2000)


class AutopilotTickPayload(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    dry_run: bool = False


class AutopilotRunPayload(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    max_ticks: int = Field(default=10, ge=1, le=10)


@app.get('/api/projects/{project_id}/autopilot')
async def get_autopilot(project_id: str):
    require_project(project_id)
    from app import autopilot as _auto
    # 読み取り専用: 状態・設定・直近のティックのみ。DB・ファイルを変更しない。
    config = _auto.get_config(orchestrator, project_id)
    ticks = _auto.list_ticks(orchestrator, project_id)
    return {'project_id': project_id, 'config': config, 'ticks': ticks,
            'read_only': True, 'external_sends': 0}


@app.post('/api/projects/{project_id}/autopilot/enable')
async def enable_autopilot(project_id: str, payload: AutopilotSettingPayload):
    require_project(project_id)
    actor = payload.actor.strip()
    reason = payload.reason.strip()
    if not actor:
        raise HTTPException(422, 'actor is required')
    if not reason:
        raise HTTPException(422, 'reason is required')
    from app import autopilot as _auto
    from app import shadow_mode as _shadow
    # C-4: 有効化の関門 (Web経路だけ。set_enabled 本体は変更しない)。
    # 関門の確認に失敗したら有効化しない (409、理由=確認できず)。
    gate_failed = False
    gate_result: dict = {}
    try:
        gate_result = await asyncio.to_thread(_shadow.gate, orchestrator,
                                              project_id)
    except Exception as exc:
        gate_failed = True
        gate_result = {"passed": False,
                       "reasons": ["確認できず:" + type(exc).__name__],
                       "metrics": {}, "thresholds": dict(_shadow.THRESHOLDS)}
    if (not isinstance(gate_result, dict) or
            type(gate_result.get("passed")) is not bool):
        gate_failed = True
        gate_result = {"passed": False, "reasons": ["確認できず:TypeError"],
                       "metrics": {}, "thresholds": dict(_shadow.THRESHOLDS)}
    reasons = gate_result.get("reasons")
    if not isinstance(reasons, list) or any(
            not isinstance(item, str) for item in reasons):
        gate_failed = True
    elif any(item.startswith("確認できず") for item in reasons):
        gate_failed = True
    if gate_failed:
        raise HTTPException(409, detail={
            "gate": gate_result,
            "hint": "影モードの関門を確認できないため、有効化できません",
        })
    if not gate_result["passed"]:
        # 関門が未達でも、担当者名と理由 (10字以上) があればオーバーライド
        # として記録して有効化する。
        override_reason = (payload.gate_override_reason or "").strip()
        if not override_reason or len(override_reason) < 10:
            raise HTTPException(
                409, detail={
                    "gate": gate_result,
                    "hint": "影モードの記録と判定を進めるか、理由を付けて関門を無視してください",
                })
        try:
            await asyncio.to_thread(_shadow.record_override, orchestrator,
                                    project_id, actor, override_reason,
                                    gate_result)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    try:
        return _auto.set_enabled(orchestrator, project_id, True,
                                 actor=actor, reason=reason)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post('/api/projects/{project_id}/autopilot/disable')
async def disable_autopilot(project_id: str, payload: AutopilotSettingPayload):
    require_project(project_id)
    actor = payload.actor.strip()
    reason = payload.reason.strip()
    if not actor:
        raise HTTPException(422, 'actor is required')
    if not reason:
        raise HTTPException(422, 'reason is required')
    from app import autopilot as _auto
    try:
        return _auto.set_enabled(orchestrator, project_id, False,
                                 actor=actor, reason=reason)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post('/api/projects/{project_id}/autopilot/tick')
async def tick_autopilot(project_id: str, payload: AutopilotTickPayload):
    require_project(project_id)
    actor = payload.actor.strip()
    if not actor:
        raise HTTPException(422, 'actor is required')
    from app import autopilot as _auto
    try:
        return await asyncio.to_thread(_auto.autopilot_tick, orchestrator,
                                       project_id, actor=actor,
                                       dry_run=bool(payload.dry_run))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post('/api/projects/{project_id}/autopilot/run')
async def run_autopilot(project_id: str, payload: AutopilotRunPayload):
    require_project(project_id)
    actor = payload.actor.strip()
    if not actor:
        raise HTTPException(422, 'actor is required')
    from app import autopilot as _auto
    try:
        return await asyncio.to_thread(_auto.run_until_stop, orchestrator,
                                       project_id, actor=actor,
                                       max_ticks=int(payload.max_ticks))
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


@app.get('/static/plan_repair_cards.js')
async def plan_repair_cards_js():
    return FileResponse(ROOT/'app'/'static'/'plan_repair_cards.js',media_type='text/javascript',headers={'Cache-Control':'no-store'})


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
    from app.plan_feedback import (
        AwaitingDecisionError, current, issues_for, propose, raise_if_unconfirmed,
    )
    # 判断待ちはOllamaへの到達確認より先に返す。版と保存済み指摘は
    # propose側でも再確認し、判断待ちを生成成功に置き換えない。
    try:
        _mission, snapshot, _detail = current(
            orchestrator, project_id, payload.signature, payload.task_id,
        )
        issues = issues_for(orchestrator, project_id, payload.signature)
        if issues:
            raise_if_unconfirmed(orchestrator, project_id, payload.signature, issues, snapshot)
    except AwaitingDecisionError as exc:
        raise HTTPException(409, detail=exc.to_response()) from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(409, str(exc)) from exc
    if not engine_enabled or not await llm.health():
        raise HTTPException(503, 'ローカルLLMが利用できません')
    try:
        return await propose(
            orchestrator, project_id, payload.signature, payload.task_id,
            idempotency_key=payload.idempotency_key or None,
        )
    except AwaitingDecisionError as exc:
        raise HTTPException(409, detail=exc.to_response()) from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, '修正案を生成できませんでした: ' + str(exc)[:500]) from exc


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


class ContactRoutePayload(BaseModel):
    type: str = Field(min_length=1, max_length=16)
    public_value: str = Field(min_length=1, max_length=2000)
    display_label: str = Field(default="", max_length=500)
    source_kind: str = Field(min_length=1, max_length=16)
    source_ref: str = Field(default="", max_length=500)
    campaign_id: str = Field(default="", max_length=128)
    route_id: str = Field(default="", max_length=128)
    actor: str = Field(min_length=1, max_length=100)
    expires_at: str | float | None = Field(default=None)


class ChannelSelectionPayload(BaseModel):
    platform: str = Field(min_length=1, max_length=32)
    enabled: bool = False
    purpose: str = Field(default="", max_length=500)
    priority: int = Field(default=0)
    account_ref: str = Field(default="", max_length=200)
    campaign_id: str = Field(default="", max_length=128)
    actor: str = Field(min_length=1, max_length=100)


class PublicIdentityPayload(BaseModel):
    platform: str = Field(min_length=1, max_length=32)
    handle: str = Field(default="", max_length=200)
    profile_url: str = Field(default="", max_length=2000)
    source_kind: str = Field(min_length=1, max_length=16)
    source_ref: str = Field(default="", max_length=500)
    owner_label: str = Field(default="", max_length=500)
    campaign_id: str = Field(default="", max_length=128)
    identity_id: str = Field(default="", max_length=128)
    actor: str = Field(min_length=1, max_length=100)
    expires_at: str | float | None = Field(default=None)


class ContactVerifyPayload(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    memo: str = Field(min_length=1, max_length=2000)


class OperatingModePayload(BaseModel):
    mode: str = Field(min_length=1, max_length=16)
    actor: str = Field(min_length=1, max_length=100)
    reason: str = Field(default="", max_length=2000)


class ContactRetirePayload(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2000)


def _contact_http_error(exc: BaseException) -> HTTPException:
    from app.contact_registry import RegistryConflictError, RegistryInputError
    if isinstance(exc, RegistryInputError):
        return HTTPException(422, str(exc))
    if isinstance(exc, RegistryConflictError):
        return HTTPException(409, str(exc))
    return HTTPException(409, str(exc))


def _contact_read_error(exc: BaseException) -> HTTPException:
    from app.contact_registry import RegistryReadError
    if isinstance(exc, RegistryReadError):
        return HTTPException(409, detail={
            "code": "CONTACT_REGISTRY_READ_FAILED",
            "message": "問い合わせ・媒体・使用IDの登録を読めません",
            "failure_kind": exc.kind,
            "path_kind": exc.path_kind,
            "sqlite_error": exc.sqlite_error,
        })
    return _contact_http_error(exc)


def _operating_mode_http_error(exc: BaseException) -> HTTPException:
    from app.contact_registry import RegistryConflictError, RegistryInputError
    if isinstance(exc, RegistryInputError):
        return HTTPException(422, str(exc))
    if isinstance(exc, RegistryConflictError):
        return HTTPException(409, str(exc))
    return HTTPException(409, str(exc))


@app.get("/api/projects/{project_id}/contact-registry/operating-mode")
async def get_project_operating_mode(project_id: str):
    require_project(project_id)
    from app.contact_registry import list_operating_modes
    try:
        return list_operating_modes(memory.path, project_id)
    except Exception as exc:
        raise _contact_read_error(exc) from exc


@app.post("/api/projects/{project_id}/contact-registry/operating-mode", status_code=201)
async def save_project_operating_mode(project_id: str, payload: OperatingModePayload):
    require_project(project_id)
    from app.contact_registry import save_operating_mode
    try:
        return save_operating_mode(
            memory.path, project_id=project_id,
            mode=payload.mode.strip(),
            actor=payload.actor, reason=payload.reason)
    except Exception as exc:
        raise _operating_mode_http_error(exc) from exc


@app.get("/api/projects/{project_id}/contact-routes")
async def list_project_contact_routes(project_id: str):
    require_project(project_id)
    from app.contact_registry import list_contact_routes
    try:
        return list_contact_routes(memory.path, project_id)
    except Exception as exc:
        raise _contact_read_error(exc) from exc


@app.post("/api/projects/{project_id}/contact-routes", status_code=201)
async def create_project_contact_route(project_id: str, payload: ContactRoutePayload):
    require_project(project_id)
    from app.contact_registry import register_contact_route
    try:
        return register_contact_route(
            memory.path, project_id=project_id, type=payload.type.strip(),
            public_value=payload.public_value,
            display_label=payload.display_label,
            source_kind=payload.source_kind.strip(),
            source_ref=payload.source_ref,
            campaign_id=payload.campaign_id.strip(),
            route_id=payload.route_id.strip(),
            actor=payload.actor, expires_at=payload.expires_at)
    except Exception as exc:
        raise _contact_http_error(exc) from exc


@app.post("/api/projects/{project_id}/contact-routes/{route_id}/verify")
async def verify_project_contact_route(project_id: str, route_id: str,
                                       payload: ContactVerifyPayload):
    require_project(project_id)
    from app.contact_registry import verify_contact_route
    try:
        return verify_contact_route(
            memory.path, project_id, route_id,
            actor=payload.actor, memo=payload.memo)
    except Exception as exc:
        raise _contact_http_error(exc) from exc


@app.post("/api/projects/{project_id}/contact-routes/{route_id}/retire")
async def retire_project_contact_route(project_id: str, route_id: str,
                                       payload: ContactRetirePayload):
    require_project(project_id)
    from app.contact_registry import retire_contact_route
    try:
        return retire_contact_route(
            memory.path, project_id, route_id,
            actor=payload.actor, reason=payload.reason)
    except Exception as exc:
        raise _contact_http_error(exc) from exc


@app.get("/api/projects/{project_id}/channel-selections")
async def list_project_channel_selections(project_id: str):
    require_project(project_id)
    from app.contact_registry import list_channel_selections
    try:
        return list_channel_selections(memory.path, project_id)
    except Exception as exc:
        raise _contact_read_error(exc) from exc


@app.post("/api/projects/{project_id}/channel-selections", status_code=201)
async def create_project_channel_selection(project_id: str,
                                           payload: ChannelSelectionPayload):
    require_project(project_id)
    from app.contact_registry import register_channel_selection
    try:
        return register_channel_selection(
            memory.path, project_id=project_id,
            platform=payload.platform.strip(),
            enabled=bool(payload.enabled), purpose=payload.purpose,
            priority=payload.priority,
            account_ref=payload.account_ref.strip(),
            campaign_id=payload.campaign_id.strip(),
            actor=payload.actor)
    except Exception as exc:
        raise _contact_http_error(exc) from exc


@app.get("/api/projects/{project_id}/public-identities")
async def list_project_public_identities(project_id: str):
    require_project(project_id)
    from app.contact_registry import list_public_identities
    try:
        return list_public_identities(memory.path, project_id)
    except Exception as exc:
        raise _contact_read_error(exc) from exc


@app.post("/api/projects/{project_id}/public-identities", status_code=201)
async def create_project_public_identity(project_id: str,
                                         payload: PublicIdentityPayload):
    require_project(project_id)
    from app.contact_registry import register_public_identity
    try:
        return register_public_identity(
            memory.path, project_id=project_id,
            platform=payload.platform.strip(),
            handle=payload.handle.strip(),
            profile_url=payload.profile_url.strip(),
            source_kind=payload.source_kind.strip(),
            source_ref=payload.source_ref,
            owner_label=payload.owner_label,
            campaign_id=payload.campaign_id.strip(),
            identity_id=payload.identity_id.strip(),
            actor=payload.actor, expires_at=payload.expires_at)
    except Exception as exc:
        raise _contact_http_error(exc) from exc


@app.post("/api/projects/{project_id}/public-identities/{identity_id}/verify")
async def verify_project_public_identity(project_id: str, identity_id: str,
                                         payload: ContactVerifyPayload):
    require_project(project_id)
    from app.contact_registry import verify_public_identity
    try:
        return verify_public_identity(
            memory.path, project_id, identity_id,
            actor=payload.actor, memo=payload.memo)
    except Exception as exc:
        raise _contact_http_error(exc) from exc


@app.post("/api/projects/{project_id}/public-identities/{identity_id}/retire")
async def retire_project_public_identity(project_id: str, identity_id: str,
                                         payload: ContactRetirePayload):
    require_project(project_id)
    from app.contact_registry import retire_public_identity
    try:
        return retire_public_identity(
            memory.path, project_id, identity_id,
            actor=payload.actor, reason=payload.reason)
    except Exception as exc:
        raise _contact_http_error(exc) from exc


@app.get("/api/projects/{project_id}/contact-registry/preflight")
async def get_project_contact_preflight(project_id: str,
                                        campaign_id: str | None = None,
                                        purposes: str | None = None):
    require_project(project_id)
    from app.contact_registry import preflight
    if purposes is None or not str(purposes).strip():
        wanted = None
    else:
        wanted = [item.strip() for item in str(purposes).split(",")
                  if item.strip()]
    try:
        return preflight(memory.path, project_id,
                         campaign_id=campaign_id, purposes=wanted)
    except Exception as exc:
        raise _contact_read_error(exc) from exc


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


class CandidateReviewPayload(BaseModel):
    reviewer: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=4000)
    expires: float | None = None
    input_version: str | None = Field(default=None, max_length=200)


class RequirementItemConfirmPayload(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2000)
    quote: str = Field(min_length=1, max_length=1000)


class ExperienceReindexPayload(BaseModel):
    reviewer: str = Field(min_length=1, max_length=100)
    actor: str = Field(default='', max_length=100)
    reason: str = Field(default='', max_length=4000)


def _experience_setting_or_409(project_id: str):
    from app.experience_memory import configured_memory
    setting = configured_memory(DB_PATH, project_id)
    if setting is None:
        raise HTTPException(409, detail={"code": "EXPERIENCE_OFF", "message": "このプロジェクトのRAGが無効です"})
    return setting


def _registered_original_hash(project_id: str, source_ref: str) -> str:
    import hashlib
    ref = str(source_ref or '').strip()
    if not ref:
        return ''
    try:
        from app.experience_memory import source_original_filename
        resolved = source_original_filename(ref)
    except Exception:
        resolved = ''
    for summary in memory.list_context_files(project_id):
        if summary.get('source') == 'memo':
            continue
        item = memory.get_context_file(project_id, summary['id'])
        if not item:
            continue
        # 既存の挙動(同名ファイルでの照合)を保ち、URL形式のsource_refは
        # 取込時と同じ決定的な安全名でも解決できるようにする。
        if ref not in (str(item.get('filename') or ''), str(summary.get('id') or ''), str(item.get('id') or '')) \
                and (not resolved or str(item.get('filename') or '') != resolved):
            continue
        original = item.get('original_data')
        if original is None and item.get('content') is not None:
            original = str(item.get('content') or '').encode('utf-8')
        # 同名ファイルが複数ある場合はSHA-256一致のものを優先せず、
        # 最初に見つかった原本のハッシュを返す(既存の挙動を変えない)。
        # 取込時に同名異内容の上書きを禁止しているため、通常は一意である。
        return hashlib.sha256(original).hexdigest() if original else ''
    return ''


def _candidate_source_view(project_id: str, item: dict) -> dict:
    expected = str(item.get('source_hash') or '').strip().lower()
    matched = bool(expected and _registered_original_hash(project_id, item.get('source_ref')) == expected)
    item['source_verified'] = matched
    if not matched:
        item['approvable'] = False
        item['blocked'] = list(item.get('blocked') or []) + ['registered_original_unavailable_or_hash_mismatch']
    return item


@app.post('/api/projects/{project_id}/plan/requirement-items/{item_id}/confirm')
async def confirm_requirement_item(project_id: str, item_id: str,
                                   payload: RequirementItemConfirmPayload):
    # 人が内容を確認した記録。実行は人が押したときだけ。成果物の承認を代行しない。
    # 文意の項目(llm_semantic)にだけ適用でき、数・見出し・語句は拒否する。
    require_project(project_id)
    actor = payload.actor.strip()
    reason = payload.reason.strip()
    quote = payload.quote.strip()
    if not actor or not reason or not quote:
        raise HTTPException(422, 'actor, reason and quote are required')
    safe_item = str(item_id or '').strip()
    if not safe_item or len(safe_item) > 64:
        raise HTTPException(400, 'item_id が不正です')
    from app.requirement_item_ledger import (
        LedgerReadError, open_ledger_readonly, record_human_confirmation,
    )
    try:
        saved = open_ledger_readonly(memory.path, project_id)
        hashes: dict[str, str] = {}
        if isinstance(saved, dict):
            try:
                stored = json.loads(str(saved.get("artifact_hashes_json") or "{}"))
            except (TypeError, ValueError):
                stored = {}
            if isinstance(stored, dict):
                hashes = {str(k): str(v) for k, v in stored.items()}
        # 記録時に現行計画・成果物を台帳と照合する。古い台帳への
        # 確認や成果物と一致しない引用は受け付けない。
        if not isinstance(saved, dict):
            raise ValueError("項目別検証台帳がありません")
        from app.goal_review import plan_snapshot as _plan_snapshot
        _, current_signature = _plan_snapshot(orchestrator, project_id)
        if str(saved.get("plan_signature") or "") != current_signature:
            raise ValueError("計画が変わりました。項目別検証をやり直してください")
        project = require_project(project_id)
        try:
            mission = memory.get_mission(project_id)
        except (ValueError, KeyError, AttributeError):
            mission = None
        if isinstance(mission, dict):
            import hashlib as _hashlib

            for task in mission.get("tasks") or []:
                if not isinstance(task, dict):
                    continue
                try:
                    from app.structured_planning import contract_of as _contract_of

                    contract = _contract_of(task)
                except (ValueError, TypeError, AttributeError, KeyError):
                    continue
                if not isinstance(contract, dict):
                    continue
                for row in contract.get("requirement_matrix") or []:
                    if not isinstance(row, dict):
                        continue
                    if str(row.get("item_id") or "") != safe_item:
                        continue
                    path = str(row.get("artifact_path") or "")
                    if not path:
                        continue
                    try:
                        _, _, target = workspace.resolve_file(
                            project.get("workspace_path", ""), project_id, path,
                            must_exist=True)
                        content = target.read_bytes()
                        digest = _hashlib.sha256(content).hexdigest()
                        if digest != str(hashes.get(path) or ""):
                            raise ValueError("成果物が変わりました。項目別検証をやり直してください")
                        if quote not in content.decode("utf-8-sig"):
                            raise ValueError("引用が現行成果物と一致しません")
                        hashes[path] = digest
                    except (OSError, UnicodeError):
                        raise ValueError("成果物を確認できません")
        result = record_human_confirmation(
            memory.path, project_id, safe_item, actor, reason, quote, hashes)
    except LedgerReadError as exc:
        raise HTTPException(409, detail={
            "code": "LEDGER_READ_FAILED",
            "message": "項目別台帳を読めないため確認を記録できません",
            "failure_kind": exc.kind,
            "path_kind": exc.path_kind,
            "sqlite_error": exc.sqlite_error,
        }) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    memory.add_event(
        project_id, "requirement_item_human_confirmed",
        f"文意の項目を人が確認: {safe_item}",
        detail=json.dumps({"item_id": safe_item, "actor": actor}, ensure_ascii=False),
    )
    result["auth_note"] = '確認者名は本人確認を保証しません(認証は未実装)。'
    result["note"] = '文意の項目の確認記録であり、成果物の承認・RAG登録を代行しません。'
    return result


@app.get('/api/projects/{project_id}/experience/candidates')
async def list_experience_candidates(project_id: str):
    require_project(project_id)
    memory_service, _mode = _experience_setting_or_409(project_id)
    return {
        'candidates': [_candidate_source_view(project_id, item) for item in memory_service.store.list_candidates(project_id)],
        'note': '確認者名は本人確認を保証しません(認証は未実装)。候補はRAG検索・計画・TRIZへ渡りません。',
    }


@app.get('/api/projects/{project_id}/experience/candidates/{candidate_id}')
async def get_experience_candidate(project_id: str, candidate_id: str):
    require_project(project_id)
    memory_service, _mode = _experience_setting_or_409(project_id)
    detail = memory_service.store.get_candidate(project_id, candidate_id)
    if detail is None:
        raise HTTPException(404, '候補が見つかりません')
    detail = _candidate_source_view(project_id, detail)
    detail['note'] = '確認者名は本人確認を保証しません(認証は未実装)。原本参照と要約の比較に必要な情報を表示します。'
    return detail


@app.post('/api/projects/{project_id}/experience/candidates/{candidate_id}/approve')
async def approve_experience_candidate(project_id: str, candidate_id: str, payload: CandidateReviewPayload):
    require_project(project_id)
    memory_service, _mode = _experience_setting_or_409(project_id)
    if not payload.reviewer.strip() or not payload.reason.strip():
        raise HTTPException(422, 'reviewer and reason are required')
    from app.experience_store import CandidateNotApprovable, InputVersionConflict, InputVersionError, InputVersionRequired
    detail = memory_service.store.get_candidate(project_id, candidate_id)
    if detail is None:
        raise HTTPException(404, '候補が見つかりません')
    if not detail.get('approvable'):
        raise HTTPException(409, detail={
            'code': 'CANDIDATE_NOT_APPROVABLE',
            'message': '候補の証拠または適用条件が不足しています。',
            'missing': detail.get('missing') or [],
            'blocked': detail.get('blocked') or [],
            'needs_at_approval': detail.get('needs_at_approval') or [],
        })
    if not _candidate_source_view(project_id, detail).get('source_verified'):
        raise HTTPException(409, detail={
            'code': 'SOURCE_RECHECK_REQUIRED',
            'message': '登録原本を再取得できないか、SHA-256が不一致です。原本を案件へ登録して再確認してください。',
        })
    try:
        result = memory_service.store.approve_candidate(
            project_id, candidate_id, payload.reviewer, payload.reason, payload.expires,
            payload.input_version,
        )
    except (InputVersionRequired, InputVersionError) as exc:
        raise HTTPException(409, detail={
            'code': getattr(exc, 'code', 'INPUT_VERSION_INVALID'),
            'message': str(exc),
            'needs_at_approval': ['input_version'],
        }) from exc
    except InputVersionConflict as exc:
        raise HTTPException(409, detail={
            'code': exc.code,
            'message': str(exc),
            'existing_input_version': exc.existing,
            'requested_input_version': exc.requested,
        }) from exc
    except CandidateNotApprovable as exc:
        raise HTTPException(409, detail={
            'code': 'CANDIDATE_NOT_APPROVABLE', 'message': str(exc),
            'missing': exc.missing, 'blocked': exc.blocked,
        }) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    # 承認後(verified化)は対象案件の索引更新を冪等ジョブで実行する(P1-A)。
    # 既存の reindex 呼び出しは壊さない。失敗しても承認自体は維持し failed として返す。
    # 索引失敗時は「検索可能」と表示しない。
    index_result = {'indexed': False, 'index_status': 'pending', 'reindexed': None, 'reindex_error': ''}
    try:
        memory_service.store.set_index_state(project_id, candidate_id, 'pending')
        index_result = await asyncio.to_thread(
            memory_service.reindex_verified_case, project_id, candidate_id)
        state = memory_service.store.get_index_state(project_id, candidate_id) or {}
        status = state.get('status') or ('indexed' if index_result.get('indexed') else 'pending')
        index_result = {**index_result, 'index_status': status,
                        'index_identity': state.get('index_identity', ''),
                        'fail_reason': state.get('fail_reason', '')}
        result['reindexed'] = 1 if index_result.get('indexed') else 0
        result['reindex_error'] = '' if index_result.get('indexed') else state.get('fail_reason', '')
    except Exception as exc:
        state = memory_service.store.get_index_state(project_id, candidate_id) or {}
        result['reindexed'] = None
        result['reindex_error'] = state.get('fail_reason') or (type(exc).__name__ + ': ' + str(exc)[:300])
        index_result = {'indexed': False, 'index_status': state.get('status') or 'failed',
                        'fail_reason': result['reindex_error'],
                        'index_identity': state.get('index_identity', '')}
    result['index'] = index_result
    result['index_status'] = index_result.get('index_status', 'pending')
    result['searchable'] = bool(index_result.get('index_status') == 'indexed')
    result['auth_note'] = '確認者名は本人確認を保証しません(認証は未実装)。'
    return result


@app.post('/api/projects/{project_id}/experience/candidates/{candidate_id}/reject')
async def reject_experience_candidate(project_id: str, candidate_id: str, payload: CandidateReviewPayload):
    require_project(project_id)
    memory_service, _mode = _experience_setting_or_409(project_id)
    if not payload.reviewer.strip() or not payload.reason.strip():
        raise HTTPException(422, 'reviewer and reason are required')
    try:
        result = memory_service.store.reject_candidate(
            project_id, candidate_id, payload.reviewer, payload.reason,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    result['auth_note'] = '確認者名は本人確認を保証しません(認証は未実装)。'
    return result


@app.post('/api/projects/{project_id}/experience/candidates/{candidate_id}/return')
async def return_experience_candidate(project_id: str, candidate_id: str, payload: CandidateReviewPayload):
    require_project(project_id)
    memory_service, _mode = _experience_setting_or_409(project_id)
    if not payload.reviewer.strip() or not payload.reason.strip():
        raise HTTPException(422, 'reviewer and reason are required')
    try:
        result = memory_service.store.return_candidate(
            project_id, candidate_id, payload.reviewer, payload.reason,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    result['auth_note'] = '確認者名は本人確認を保証しません(認証は未実装)。'
    return result


@app.get('/api/projects/{project_id}/experience/index-status')
async def get_experience_index_status(project_id: str):
    require_project(project_id)
    memory_service, _mode = _experience_setting_or_409(project_id)
    view = memory_service.index_status_view(project_id)
    view['note'] = ('verified のままでも failed は検索可能と表示しない。'
                    '索引は再生成可能なキャッシュであり正本の承認状態より強い権限を持たない。')
    return view


@app.post('/api/projects/{project_id}/experience/reindex')
async def reindex_experience_failures(project_id: str, payload: ExperienceReindexPayload):
    """失敗分のみ再試行する安全な冪等操作。reviewer/actor 必須。管理経路のみ(48G許可リストには追加しない)。"""
    require_project(project_id)
    memory_service, _mode = _experience_setting_or_409(project_id)
    reviewer = payload.reviewer.strip()
    actor = (payload.actor or reviewer).strip()
    if not reviewer or not actor:
        raise HTTPException(422, 'reviewer and actor are required')
    view = memory_service.index_status_view(project_id)
    targets = [item['id'] for item in view['items'] if item['status'] == 'failed']
    # pending で記録が無い verified も対象にする(冪等: 何度実行しても同じ結果)。
    recorded = {item['id'] for item in view['items']}
    for row in memory_service.store.list(project_id, True):
        if row['id'] not in recorded:
            targets.append(row['id'])
    targets = list(dict.fromkeys(targets))
    results = []
    for rid in targets:
        try:
            outcome = await asyncio.to_thread(
                memory_service.reindex_verified_case, project_id, rid)
            results.append({'id': rid, 'indexed': bool(outcome.get('indexed')), 'error': ''})
        except Exception as exc:
            state = memory_service.store.get_index_state(project_id, rid) or {}
            results.append({'id': rid, 'indexed': False,
                            'error': state.get('fail_reason') or type(exc).__name__})
    if targets:
        memory_service.store.log_candidate_event(
            project_id, targets[0], 'recheck_requested', reviewer,
            '索引の再試行を実行(対象=%d件)。%s' % (len(targets), (payload.reason or '').strip() or '失敗分の再試行'))
    return {'retried': len(targets), 'results': results,
            'status': memory_service.index_status_view(project_id),
            'auth_note': '確認者名は本人確認を保証しません(認証は未実装)。'}


class NeedsReviewRecheckPayload(BaseModel):
    reviewer: str = Field(min_length=1, max_length=100)
    source_ref: str = Field(default='', max_length=2000)
    observed_source_hash: str = Field(default='', max_length=128)


@app.get('/api/projects/{project_id}/experience/needs-review')
async def list_experience_needs_review(project_id: str):
    require_project(project_id)
    memory_service, _mode = _experience_setting_or_409(project_id)
    items = memory_service.store.list_needs_review(project_id)
    return {
        'count': len(items),
        'records': items,
        'note': '確認者名は本人確認を保証しません(認証は未実装)。needs_reviewはRAG検索・計画・TRIZへ渡りません。',
    }


@app.get('/api/projects/{project_id}/experience/needs-review/{record_id}')
async def get_experience_needs_review(project_id: str, record_id: str):
    require_project(project_id)
    memory_service, _mode = _experience_setting_or_409(project_id)
    detail = memory_service.store.get_needs_review(project_id, record_id)
    if detail is None:
        raise HTTPException(404, '再審査待ちの事例が見つかりません')
    detail['note'] = '確認者名は本人確認を保証しません(認証は未実装)。原本再取得のSHA-256一致でのみcandidateへ進めます。'
    return detail


@app.post('/api/projects/{project_id}/experience/needs-review/{record_id}/recheck')
async def recheck_experience_needs_review(
    project_id: str, record_id: str, payload: NeedsReviewRecheckPayload,
):
    require_project(project_id)
    memory_service, _mode = _experience_setting_or_409(project_id)
    if not payload.reviewer.strip():
        raise HTTPException(422, 'reviewer is required')
    if not payload.source_ref.strip() or not payload.observed_source_hash.strip():
        raise HTTPException(422, 'source_ref and observed_source_hash are required')
    # P0-B: 保存済みsource_hashが無い旧取込は自動で再承認しない。原本の再取得
    # (ここでは登録済みコンテキスト原本の再読込)による新しいsource_hashを記録し、
    # candidateに進めるだけ。承認はP0-AのレビューAPI経由で行う。
    # 取込時の決定的な安全名(source-*.md)でも解決できるようにする。
    def _fetch_source(source_ref: str):
        try:
            from app.experience_memory import source_original_filename
            resolved_names = {source_ref, source_original_filename(source_ref)}
        except Exception:
            resolved_names = {source_ref}
        for summary in memory.list_context_files(project_id):
            if summary.get('source') == 'memo':
                continue
            item = memory.get_context_file(project_id, summary['id'])
            if not item:
                continue
            name = str(item.get('filename') or '')
            if source_ref in (name, summary.get('id'), str(item.get('id') or '')) or name in resolved_names:
                data = item.get('original_data')
                if data is None and item.get('content') is not None:
                    data = str(item.get('content') or '').encode('utf-8')
                return data or b''
        raise ValueError('原本を再取得できませんでした: ' + source_ref[:200])

    try:
        result = memory_service.store.recheck_needs_review(
            project_id, record_id, payload.reviewer.strip(),
            {'source_ref': payload.source_ref.strip(),
             'observed_source_hash': payload.observed_source_hash.strip()},
            fetch_source=_fetch_source,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    result['auth_note'] = '確認者名は本人確認を保証しません(認証は未実装)。'
    result['approval_note'] = 'candidate化のみであり承認ではありません。承認はP0-AのレビューAPI経由です。'
    return result


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


@app.get('/api/projects/{project_id}/plan/case-references')
async def get_plan_case_references(project_id: str):
    require_project(project_id)
    from app.plan_case_reference import current_view
    mission = memory.get_mission(project_id)
    return current_view(memory.path, project_id, mission.get('plan_version') or 0)


class RepairRunStartPayload(BaseModel):
    actor: str = Field(min_length=1, max_length=100)


class RepairRunAdvancePayload(BaseModel):
    next_state: str = Field(min_length=1, max_length=40)
    actor: str = Field(min_length=1, max_length=100)
    payload: dict = Field(default_factory=dict)


@app.get('/api/projects/{project_id}/plan/repair-runs')
async def list_plan_repair_runs(project_id: str):
    require_project(project_id)
    from app.plan_repair_loop import list_runs, public_view
    return {'runs': [public_view(x) for x in list_runs(orchestrator, project_id)]}


@app.get('/api/projects/{project_id}/plan/repair-runs/cards')
async def get_plan_repair_cards(project_id: str):
    require_project(project_id)
    from app.plan_repair_loop import repair_cards
    return repair_cards(orchestrator, project_id)


class RepairRunAnswerPayload(BaseModel):
    run_id: str = Field(min_length=1, max_length=128)
    issue_id: str = Field(min_length=1, max_length=128)
    answer: str = Field(min_length=1, max_length=2000)
    actor: str = Field(min_length=1, max_length=100)


@app.post('/api/projects/{project_id}/plan/repair-runs/answer')
async def answer_plan_repair_fact(project_id: str, body: RepairRunAnswerPayload):
    require_project(project_id)
    actor = body.actor.strip()
    if not actor:
        raise HTTPException(422, 'actor is required')
    from app.plan_repair_loop import answer_business_fact
    try:
        return answer_business_fact(orchestrator, project_id, body.run_id,
                                    body.issue_id, body.answer, actor)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post('/api/projects/{project_id}/plan/repair-runs/start')
async def start_plan_repair_run(project_id: str, body: RepairRunStartPayload):
    require_project(project_id)
    actor = body.actor.strip()
    if not actor:
        raise HTTPException(422, 'actor is required')
    from app.plan_repair_loop import start
    try:
        return start(orchestrator, project_id, actor)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post('/api/projects/{project_id}/plan/repair-runs/{run_id}/advance')
async def advance_plan_repair_run(project_id: str, run_id: str, body: RepairRunAdvancePayload):
    require_project(project_id)
    actor = body.actor.strip()
    if not actor:
        raise HTTPException(422, 'actor is required')
    from app.plan_repair_loop import advance
    import asyncio as _asyncio

    try:
        result = advance(orchestrator, project_id, run_id, body.next_state, actor, body.payload or {})
        if _asyncio.iscoroutine(result):
            result = await result
        return result
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


class ResolutionDetectPayload(BaseModel):
    actor: str = Field(min_length=1, max_length=100)


class ResolutionAdvancePayload(BaseModel):
    next_state: str = Field(min_length=1, max_length=40)
    actor: str = Field(min_length=1, max_length=100)
    reason: str = Field(default="", max_length=2000)
    evidence: dict = Field(default_factory=dict)


@app.get("/api/projects/{project_id}/resolutions")
async def list_project_resolutions(project_id: str):
    require_project(project_id)
    from app.resolution_coordinator import list_resolutions, resolution_cards, summary
    return {
        "resolutions": list_resolutions(orchestrator, project_id),
        "cards": resolution_cards(orchestrator, project_id)["cards"],
        "summary": summary(orchestrator, project_id),
    }


@app.post("/api/projects/{project_id}/resolutions/detect")
async def detect_project_resolutions(project_id: str, body: ResolutionDetectPayload):
    require_project(project_id)
    actor = body.actor.strip()
    if not actor:
        raise HTTPException(422, "actor is required")
    from app.resolution_coordinator import detect
    try:
        return detect(orchestrator, project_id, actor)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/projects/{project_id}/resolutions/{resolution_id}")
async def get_project_resolution(project_id: str, resolution_id: str):
    require_project(project_id)
    from app.resolution_coordinator import get
    try:
        return get(orchestrator, project_id, resolution_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/projects/{project_id}/resolutions/{resolution_id}/candidates")
async def get_project_resolution_candidates(project_id: str, resolution_id: str):
    require_project(project_id)
    from app.resolution_coordinator import get as get_resolution
    from app.resolution_p3_context import attach_p3_context, collect_p3_context
    try:
        stored = get_resolution(orchestrator, project_id, resolution_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    try:
        context = collect_p3_context(orchestrator, project_id, stored)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    view = attach_p3_context(stored, context)
    return {
        "id": stored.get("id"),
        "project_id": stored.get("project_id"),
        "criterion_id": stored.get("criterion_id"),
        "task_key": stored.get("task_key"),
        "cause": stored.get("cause"),
        "cause_ja": stored.get("cause_ja"),
        "state": stored.get("state"),
        "state_ja": stored.get("state_ja"),
        "rag_references": view.get("rag_references") or {},
        "triz_candidates": view.get("triz_candidates") or {},
        "business_note_ja": "候補・試験は業務達成ではない。業務達成は条件PASSと人間確認で別に判定する",
    }


@app.post("/api/projects/{project_id}/resolutions/{resolution_id}/advance")
async def advance_project_resolution(project_id: str, resolution_id: str,
                                     body: ResolutionAdvancePayload):
    require_project(project_id)
    actor = body.actor.strip()
    if not actor:
        raise HTTPException(422, "actor is required")
    from app.resolution_coordinator import advance
    try:
        return advance(orchestrator, project_id, resolution_id, body.next_state,
                       actor, body.reason, body.evidence or {})
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/static/resolution_cards.js")
async def resolution_cards_js():
    return FileResponse(ROOT / "app" / "static" / "resolution_cards.js", media_type="text/javascript", headers={"Cache-Control": "no-store"})


@app.get("/static/plan_review_loop.js")
async def plan_review_loop_js():
    return FileResponse(ROOT / "app" / "static" / "plan_review_loop.js", media_type="text/javascript", headers={"Cache-Control": "no-store"})


class AutoLoopPreviewPayload(BaseModel):
    providers: list[str] = Field(default_factory=list, max_length=5)
    public_summary: str = Field(default="", max_length=12000)


class AutoLoopStartPayload(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    providers: list[str] = Field(min_length=1, max_length=5)
    public_summary: str = Field(default="", max_length=12000)
    idempotency_key: str = Field(default="", max_length=128)
    expected_packet_hash: str = Field(default="", max_length=64)


class AutoLoopCancelPayload(BaseModel):
    actor: str = Field(min_length=1, max_length=100)


class AutoLoopRemapPayload(BaseModel):
    actor: str = Field(default="", max_length=100)
    idempotency_key: str = Field(default="", max_length=128)
    binding_version: str = Field(default="", max_length=80)
    planner_version: str = Field(default="", max_length=80)
    resume_cause: str = Field(default="", max_length=300)
    binding_decision: bool = Field(default=False)


class AutoLoopBindingConfirmPayload(BaseModel):
    issue_id: str = Field(min_length=1, max_length=128)
    actor: str = Field(min_length=1, max_length=100)
    task_key: str = Field(default="", max_length=100)
    criterion_id: str = Field(default="", max_length=100)
    reason: str = Field(default="", max_length=2000)
    decision_option: str = Field(default="", max_length=64)
    review_fingerprint: str = Field(default="", max_length=128)
    confirmed_preview: bool = False


class AutoLoopBindingPreviewPayload(BaseModel):
    issue_id: str = Field(min_length=1, max_length=128)
    task_key: str = Field(default="", max_length=100)
    criterion_id: str = Field(default="", max_length=100)
    decision_option: str = Field(default="", max_length=64)


@app.post('/api/projects/{project_id}/goal-review/auto-loop/preview')
async def preview_auto_loop(project_id: str, payload: AutoLoopPreviewPayload):
    require_project(project_id)
    from app.plan_review_loop import preview_run
    try:
        return preview_run(orchestrator, project_id, payload.providers,
                           explicit_summary=payload.public_summary or "")
    except (ValueError, KeyError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post('/api/projects/{project_id}/goal-review/auto-loop/start')
async def start_auto_loop(project_id: str, payload: AutoLoopStartPayload):
    require_project(project_id)
    from app.plan_review_loop import start_run
    try:
        return start_run(orchestrator, project_id, payload.actor, payload.providers,
                         payload.public_summary, payload.idempotency_key,
                         expected_packet_hash=payload.expected_packet_hash)
    except (ValueError, KeyError) as exc:
        text = str(exc)
        # P0-A: 必須照合の不一致は409で返し、再プレビューへ誘導する。
        if text.startswith("409:"):
            raise HTTPException(409, text[4:].strip()) from exc
        raise HTTPException(409, text) from exc


@app.post('/api/projects/{project_id}/goal-review/auto-loop/{run_id}/run')
async def run_auto_loop(project_id: str, run_id: str):
    require_project(project_id)
    from app.plan_review_loop import run_loop
    try:
        return await run_loop(orchestrator, project_id, run_id)
    except (ValueError, KeyError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get('/api/projects/{project_id}/goal-review/auto-loop/runs')
async def list_auto_loop_runs(project_id: str):
    require_project(project_id)
    from app.plan_review_loop import list_runs, public_view
    return {'runs': [public_view(x) for x in list_runs(orchestrator, project_id)]}


@app.get('/api/projects/{project_id}/goal-review/auto-loop/{run_id}')
async def get_auto_loop_run(project_id: str, run_id: str):
    require_project(project_id)
    from app.plan_review_loop import get_run, public_view
    try:
        return public_view(get_run(orchestrator, project_id, run_id))
    except (ValueError, KeyError) as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post('/api/projects/{project_id}/goal-review/auto-loop/{run_id}/cancel')
async def cancel_auto_loop(project_id: str, run_id: str, payload: AutoLoopCancelPayload):
    require_project(project_id)
    from app.plan_review_loop import cancel_run
    try:
        return cancel_run(orchestrator, project_id, run_id, payload.actor.strip())
    except (ValueError, KeyError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get('/api/projects/{project_id}/goal-review/auto-loop/{run_id}/remap-preview')
async def preview_auto_loop_remap(project_id: str, run_id: str):
    require_project(project_id)
    from app.plan_review_loop import remap_preview
    try:
        return remap_preview(orchestrator, project_id, run_id)
    except (ValueError, KeyError) as exc:
        text = str(exc)
        if text.startswith("409:"):
            raise HTTPException(409, text[4:].strip()) from exc
        if "見つかりません" in text or "not found" in text:
            raise HTTPException(404, text) from exc
        raise HTTPException(409, text) from exc


@app.post('/api/projects/{project_id}/goal-review/auto-loop/{run_id}/binding-confirm')
async def confirm_auto_loop_binding(project_id: str, run_id: str,
                                    payload: AutoLoopBindingConfirmPayload):
    require_project(project_id)
    from app.plan_review_loop import confirm_binding
    try:
        return await confirm_binding(
            orchestrator, project_id, run_id, payload.issue_id,
            actor=payload.actor.strip(),
            task_key=payload.task_key.strip(),
            criterion_id=payload.criterion_id.strip(),
            reason=payload.reason.strip(),
            decision_option=payload.decision_option.strip(),
            review_fingerprint=payload.review_fingerprint.strip(),
            confirmed_preview=bool(payload.confirmed_preview),
        )
    except (ValueError, KeyError) as exc:
        text = str(exc)
        if text.startswith("409:"):
            raise HTTPException(409, text[4:].strip()) from exc
        raise HTTPException(409, text) from exc


@app.post('/api/projects/{project_id}/goal-review/auto-loop/{run_id}/binding-snapshot-retry')
async def retry_auto_loop_binding_snapshot(project_id: str, run_id: str):
    require_project(project_id)
    from app.plan_review_loop import retry_binding_snapshot_save
    try:
        return await retry_binding_snapshot_save(orchestrator, project_id, run_id)
    except (ValueError, KeyError) as exc:
        text = str(exc)
        if text.startswith("409:"):
            raise HTTPException(409, text[4:].strip()) from exc
        raise HTTPException(409, text) from exc


@app.post('/api/projects/{project_id}/goal-review/auto-loop/{run_id}/binding-preview')
async def preview_auto_loop_binding(project_id: str, run_id: str,
                                    payload: AutoLoopBindingPreviewPayload):
    require_project(project_id)
    from app.plan_review_loop import binding_selection_preview_for_run, get_run
    try:
        run = get_run(orchestrator, project_id, run_id)
    except (ValueError, KeyError) as exc:
        raise HTTPException(404, str(exc)) from exc
    try:
        return binding_selection_preview_for_run(
            run, payload.issue_id,
            task_key=payload.task_key.strip(),
            criterion_id=payload.criterion_id.strip(),
            decision_option=payload.decision_option.strip(),
        )
    except (ValueError, KeyError) as exc:
        text = str(exc)
        if text.startswith("409:"):
            raise HTTPException(409, text[4:].strip()) from exc
        raise HTTPException(409, text) from exc


@app.post('/api/projects/{project_id}/goal-review/auto-loop/{run_id}/remap')
async def remap_auto_loop(project_id: str, run_id: str, payload: AutoLoopRemapPayload):
    require_project(project_id)
    from app.plan_review_loop import remap_run
    try:
        return await remap_run(orchestrator, project_id, run_id,
                               payload.actor.strip(), payload.idempotency_key.strip(),
                               binding_version=payload.binding_version.strip(),
                               planner_version=payload.planner_version.strip(),
                               resume_cause=payload.resume_cause.strip(),
                               binding_decision=bool(payload.binding_decision))
    except (ValueError, KeyError) as exc:
        text = str(exc)
        if text.startswith("409:"):
            raise HTTPException(409, text[4:].strip()) from exc
        raise HTTPException(409, text) from exc


class AutoLoopResumePayload(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    binding_version: str = Field(default="", max_length=80)
    planner_version: str = Field(default="", max_length=80)
    resume_cause: str = Field(default="", max_length=300)


@app.get('/api/projects/{project_id}/goal-review/auto-loop/{run_id}/resume-eligibility')
async def resume_eligibility_auto_loop(project_id: str, run_id: str,
                                       binding_version: str = "",
                                       planner_version: str = ""):
    require_project(project_id)
    from app.plan_review_loop import get_run, resume_eligibility
    try:
        run = get_run(orchestrator, project_id, run_id)
    except (ValueError, KeyError) as exc:
        raise HTTPException(404, str(exc)) from exc
    return resume_eligibility(run, binding_version=binding_version,
                              planner_version=planner_version)


@app.post('/api/projects/{project_id}/goal-review/auto-loop/{run_id}/resume')
async def resume_auto_loop(project_id: str, run_id: str, payload: AutoLoopResumePayload):
    require_project(project_id)
    from app.plan_review_loop import remap_run
    try:
        return await remap_run(orchestrator, project_id, run_id,
                               payload.actor.strip(), "",
                               binding_version=payload.binding_version.strip(),
                               planner_version=payload.planner_version.strip(),
                               resume_cause=payload.resume_cause.strip())
    except (ValueError, KeyError) as exc:
        text = str(exc)
        if text.startswith("409:"):
            raise HTTPException(409, text[4:].strip()) from exc
        raise HTTPException(409, text) from exc


class CapabilityGapProposePayload(BaseModel):
    actor: str = Field(min_length=1, max_length=100)


class CapabilityGapReviewPayload(BaseModel):
    reviewer: str = Field(default='', max_length=100)
    actor: str = Field(default='', max_length=100)
    reason: str = Field(default='', max_length=2000)


class CapabilityGapExportPayload(BaseModel):
    actor: str = Field(default='', max_length=100)


class CapabilityGapSubmittedPayload(BaseModel):
    actor: str = Field(default='', max_length=100)
    memo: str = Field(default='', max_length=2000)


class CapabilityGapDeliveryPayload(BaseModel):
    actor: str = Field(default='', max_length=100)
    delivery: dict = Field(default_factory=dict)


class CapabilityGapVerifyPayload(BaseModel):
    actor: str = Field(default='', max_length=100)


class CapabilityGapReverifyPayload(BaseModel):
    actor: str = Field(default='', max_length=100)


class DomainPackSelectPayload(BaseModel):
    domain_id: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=16)
    actor: str = Field(default='', max_length=100)
    reason: str = Field(default='', max_length=2000)


class DomainPackClearPayload(BaseModel):
    actor: str = Field(default='', max_length=100)
    reason: str = Field(default='', max_length=2000)


def _domain_pack_id_or_400(value: str, field: str) -> str:
    import re as _re
    text = str(value or '')
    pattern = r'^[A-Za-z0-9_-]{1,64}$' if field == 'domain_id' else r'^v[0-9]+$'
    if not _re.fullmatch(pattern, text):
        raise HTTPException(400, f'{field} が不正です')
    if text in ('.', '..') or '/' in text or '\\' in text:
        raise HTTPException(400, f'{field} が不正です')
    return text


def _capability_gap_proposal_id_or_400(proposal_id: str) -> str:
    from app.capability_export import is_safe_proposal_id as _is_safe
    text = str(proposal_id or '')
    if not _is_safe(text):
        raise HTTPException(400, 'proposal_id が不正です')
    return text


@app.get('/api/projects/{project_id}/capability-gaps')
async def get_capability_gaps(project_id: str):
    require_project(project_id)
    from app import capability_gap as _gap
    # 読み取り専用: 事前照合と一覧のみ。DB・ファイルを変更しない。
    preflight = _gap.capability_preflight(orchestrator, project_id)
    proposals = _gap.list_proposals(orchestrator, project_id)
    return {'project_id': project_id, 'preflight': preflight,
            'proposals': proposals, 'read_only': True,
            'external_sends': 0}


@app.post('/api/projects/{project_id}/capability-gaps/propose')
async def propose_capability_gaps(project_id: str, payload: CapabilityGapProposePayload):
    require_project(project_id)
    actor = payload.actor.strip()
    if not actor:
        raise HTTPException(422, 'actor is required')
    from app import capability_gap as _gap
    # ローカルで事前照合し提案を生成。外部送信なし。冪等。
    preflight = _gap.capability_preflight(orchestrator, project_id)
    try:
        result = _gap.generate_proposals(orchestrator, project_id, preflight, actor=actor)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    result['external_sends'] = 0
    # 確認者名は本人確認を保証しない (認証は未実装)。
    result['auth_note'] = '確認者名は本人確認を保証しません(認証は未実装)。'
    return result


@app.get('/api/projects/{project_id}/capability-gaps/proposals/{proposal_id}')
async def get_capability_gap_proposal(project_id: str, proposal_id: str):
    require_project(project_id)
    safe_id = _capability_gap_proposal_id_or_400(proposal_id)
    from app import capability_gap as _gap
    row = _gap.get_proposal(orchestrator, safe_id)
    if row is None or str(row.get('project_id') or '') != project_id:
        raise HTTPException(404, '提案が見つかりません')
    return row


@app.post('/api/projects/{project_id}/capability-gaps/proposals/{proposal_id}/review')
async def review_capability_gap_proposal(project_id: str, proposal_id: str,
                                         payload: CapabilityGapReviewPayload):
    require_project(project_id)
    safe_id = _capability_gap_proposal_id_or_400(proposal_id)
    # 確認者名は本人確認を保証しない (認証は未実装)。作業記録用の表示名である。
    actor = (payload.reviewer or payload.actor or '').strip()
    if not actor:
        raise HTTPException(422, 'reviewer is required')
    reason = (payload.reason or '').strip()
    if not reason:
        raise HTTPException(422, 'reason is required')
    from app import capability_gap as _gap
    row = _gap.get_proposal(orchestrator, safe_id)
    if row is None or str(row.get('project_id') or '') != project_id:
        raise HTTPException(404, '提案が見つかりません')
    try:
        updated = _gap.set_proposal_status(orchestrator, project_id, safe_id,
                                           'reviewed', actor=actor, reason=reason)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    updated = dict(updated)
    updated['auth_note'] = '確認者名は本人確認を保証しません(認証は未実装)。'
    return updated


@app.post('/api/projects/{project_id}/capability-gaps/proposals/{proposal_id}/export')
async def export_capability_gap_proposal(project_id: str, proposal_id: str,
                                         payload: CapabilityGapExportPayload):
    require_project(project_id)
    safe_id = _capability_gap_proposal_id_or_400(proposal_id)
    # 確認者名は本人確認を保証しない (認証は未実装)。作業記録用の表示名である。
    actor = (payload.actor or '').strip()
    if not actor:
        raise HTTPException(422, 'actor is required')
    from app import capability_gap as _gap
    from app import capability_export as _export
    row = _gap.get_proposal(orchestrator, safe_id)
    if row is None or str(row.get('project_id') or '') != project_id:
        raise HTTPException(404, '提案が見つかりません')
    try:
        base_ref = _export.get_base_ref()
    except Exception:
        base_ref = 'unknown'
    try:
        result = await asyncio.to_thread(_export.export_proposal, orchestrator,
                                         project_id, safe_id, actor=actor,
                                         base_ref=base_ref)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not result.get('ok'):
        raise HTTPException(409, str(result.get('reason') or 'export failed'))
    # 応答にファイルの中身そのものは含めない (相対名とSHA-256のみ)。
    out = {k: result.get(k) for k in (
        'proposal_id', 'project_id', 'status', 'content_hash', 'base_ref',
        'package_rel', 'instruction_sha256', 'request_sha256',
        'manifest_sha256', 'idempotent', 'superseded')}
    out['ok'] = True
    out['auth_note'] = '確認者名は本人確認を保証しません(認証は未実装)。'
    out['note'] = '承認前のローカル保存のみであり、送信はしません。'
    return out


@app.post('/api/projects/{project_id}/capability-gaps/proposals/{proposal_id}/submitted')
async def submit_capability_gap_proposal(project_id: str, proposal_id: str,
                                         payload: CapabilityGapSubmittedPayload):
    require_project(project_id)
    safe_id = _capability_gap_proposal_id_or_400(proposal_id)
    # 人がパッケージをJenkinsへ渡したことの記録。実際には何も送信しない。
    actor = (payload.actor or '').strip()
    if not actor:
        raise HTTPException(422, 'actor is required')
    from app import capability_gap as _gap
    from app import capability_delivery as _delivery
    row = _gap.get_proposal(orchestrator, safe_id)
    if row is None or str(row.get('project_id') or '') != project_id:
        raise HTTPException(404, '提案が見つかりません')
    try:
        result = await asyncio.to_thread(_delivery.record_submitted, orchestrator,
                                         project_id, safe_id, actor=actor,
                                         memo=payload.memo or '')
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    result = dict(result)
    result['auth_note'] = '確認者名は本人確認を保証しません(認証は未実装)。'
    result['note'] = '人がパッケージを渡したことの記録であり、実際には何も送信していません。'
    return result


@app.post('/api/projects/{project_id}/capability-gaps/proposals/{proposal_id}/delivery')
async def deliver_capability_gap_proposal(project_id: str, proposal_id: str,
                                          payload: CapabilityGapDeliveryPayload):
    require_project(project_id)
    safe_id = _capability_gap_proposal_id_or_400(proposal_id)
    actor = (payload.actor or '').strip()
    if not actor:
        raise HTTPException(422, 'actor is required')
    from app import capability_gap as _gap
    from app import capability_delivery as _delivery
    row = _gap.get_proposal(orchestrator, safe_id)
    if row is None or str(row.get('project_id') or '') != project_id:
        raise HTTPException(404, '提案が見つかりません')
    if not isinstance(payload.delivery, dict) or not payload.delivery:
        raise HTTPException(422, 'delivery is required')
    try:
        result = await asyncio.to_thread(_delivery.record_delivery, orchestrator,
                                         project_id, safe_id, payload.delivery,
                                         actor=actor)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    result = dict(result)
    if not result.get('ok'):
        raise HTTPException(409, str(result.get('reason') or 'delivery rejected'))
    # 応答に失敗ログ全文・差分全文は含めない (短い理由と状態のみ)。
    out = {k: result.get(k) for k in (
        'proposal_id', 'project_id', 'status', 'verdict', 'delivery_id',
        'reason', 'idempotent')}
    out['ok'] = True
    out['auth_note'] = '確認者名は本人確認を保証しません(認証は未実装)。'
    if out.get('verdict') == 'delivered':
        out['note'] = '実装が納品されました。まだ能力は有効ではありません。'
    return out


@app.post('/api/projects/{project_id}/capability-gaps/proposals/{proposal_id}/verify')
async def verify_capability_gap_proposal(project_id: str, proposal_id: str,
                                         payload: CapabilityGapVerifyPayload):
    require_project(project_id)
    safe_id = _capability_gap_proposal_id_or_400(proposal_id)
    actor = (payload.actor or '').strip()
    if not actor:
        raise HTTPException(422, 'actor is required')
    from app import capability_gap as _gap
    from app import capability_delivery as _delivery
    row = _gap.get_proposal(orchestrator, safe_id)
    if row is None or str(row.get('project_id') or '') != project_id:
        raise HTTPException(404, '提案が見つかりません')
    try:
        result = await asyncio.to_thread(_delivery.verify_proposal, orchestrator,
                                         project_id, safe_id, actor=actor)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    result = dict(result)
    # 残件の全文は返さない (短い形に整える)。
    remaining = []
    for item in (result.get('remaining') or []):
        if isinstance(item, dict):
            remaining.append({
                'criterion_id': str(item.get('criterion_id') or ''),
                'gap_code': str(item.get('gap_code') or ''),
                'required_capability_id': str(item.get('required_capability_id') or ''),
                'unmet_states': [str(x) for x in (item.get('unmet_states') or [])][:8],
            })
    result['remaining'] = remaining
    result['auth_note'] = '確認者名は本人確認を保証しません(認証は未実装)。'
    if result.get('verified'):
        result['note'] = '元の停止が再判定で解消しました。'
    elif result.get('stale'):
        result['note'] = '計画が更新されたためこの提案は古い版です。'
    elif not result.get('verified'):
        result['note'] = '元の停止が残っています。'
    return result


@app.get('/api/projects/{project_id}/capability-gaps/proposals/{proposal_id}/package')
async def get_capability_gap_package(project_id: str, proposal_id: str):
    # D-1b 指示パッケージの取得 (読み取り専用。actor 不要)。運搬の受け口であり、
    # ジェンキンス側へは接続しない (人が起動するローカル脚本が読むだけ)。
    # 状態が export_ready/submitted のときだけ全文と base_ref・SHA-256 を返す。
    # 検証に失敗したら409と理由 (日本語・型名まで)。何も書き込まない。
    require_project(project_id)
    safe_id = _capability_gap_proposal_id_or_400(proposal_id)
    from app import capability_export as _export
    from app import capability_gap as _gap
    row = _gap.get_proposal(orchestrator, safe_id)
    if row is None or str(row.get('project_id') or '') != project_id:
        raise HTTPException(404, '提案が見つかりません')
    result = _export.read_package(orchestrator, project_id, safe_id)
    if not isinstance(result, dict) or not result.get('ok'):
        reason = ''
        if isinstance(result, dict):
            reason = str(result.get('reason') or '確認できず')
        raise HTTPException(409, reason or '指示パッケージを確認できません')
    # 応答に秘密値・内部の絶対パスを入れない (相対名とハッシュのみ)。
    return {
        'ok': True,
        'proposal_id': str(result.get('proposal_id') or safe_id),
        'status': str(result.get('status') or ''),
        'base_ref': str(result.get('base_ref') or ''),
        'files': {
            'instruction.json': str((result.get('files') or {}).get('instruction.json') or ''),
            'REQUEST.md': str((result.get('files') or {}).get('REQUEST.md') or ''),
            'manifest.json': str((result.get('files') or {}).get('manifest.json') or ''),
        },
        'sha256': {
            'instruction.json': str((result.get('sha256') or {}).get('instruction.json') or ''),
            'REQUEST.md': str((result.get('sha256') or {}).get('REQUEST.md') or ''),
            'manifest.json': str((result.get('sha256') or {}).get('manifest.json') or ''),
        },
        'read_only': True,
        'external_sends': 0,
    }


@app.post('/api/capability-gaps/reverify')
async def reverify_capability_gaps(payload: CapabilityGapReverifyPayload):
    # D-4 反映後の再判定 (人が押したときだけ動く。threadpool 経由)。
    # アプリ内の定期実行は作らない。結果の変化は見張りの通知パネルで表示する。
    actor = (payload.actor or '').strip()
    if not actor:
        raise HTTPException(422, 'actor is required')
    from app import capability_delivery as _delivery
    try:
        result = await asyncio.to_thread(_delivery.reverify_delivered,
                                         orchestrator, actor)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not isinstance(result, dict) or not result.get('ok'):
        reason = ''
        if isinstance(result, dict):
            reason = str(result.get('reason') or '確認できず')
        raise HTTPException(409, reason or '再判定を確認できません')
    result = dict(result)
    result['auth_note'] = '確認者名は本人確認を保証しません(認証は未実装)。'
    result['note'] = ('基準版が変わった納品済み提案を再判定しました。'
                      '結果は見張りの通知パネルで確認できます。')
    return result


@app.get('/api/domain-packs')
async def list_domain_packs():
    from app import domain_spec as _packs
    # 登録簿の読取りのみ。DB・ファイルを変更しない。
    return {'packs': _packs.list_specs(), 'schema': _packs.SCHEMA,
            'read_only': True, 'external_sends': 0}


@app.get('/api/projects/{project_id}/domain-packs')
async def get_project_domain_packs(project_id: str):
    require_project(project_id)
    from app import domain_spec as _packs
    # 読み取り専用: 適用候補と現在の選択のみ。DB・ファイルを変更しない。
    candidates = _packs.applicable_packs(orchestrator, project_id)
    selection = _packs.get_selection(orchestrator, project_id)
    return {'project_id': project_id, 'candidates': candidates,
            'selection': selection, 'read_only': True, 'external_sends': 0}


@app.get('/api/projects/{project_id}/domain-packs/{domain_id}/conformance')
async def get_domain_pack_conformance(project_id: str, domain_id: str,
                                      version: str = 'v1'):
    require_project(project_id)
    safe_id = _domain_pack_id_or_400(domain_id, 'domain_id')
    safe_version = _domain_pack_id_or_400(version, 'version')
    from app import domain_spec as _packs
    # 読み取り専用: 適合検査のみ。DB・ファイルを変更しない。
    if _packs.get_spec(safe_id, safe_version) is None:
        raise HTTPException(404, 'パックが見つかりません')
    return _packs.conformance_report(orchestrator, project_id,
                                     safe_id, safe_version)


@app.post('/api/projects/{project_id}/domain-packs/select')
async def select_project_domain_pack(project_id: str,
                                     payload: DomainPackSelectPayload):
    require_project(project_id)
    safe_id = _domain_pack_id_or_400(payload.domain_id, 'domain_id')
    safe_version = _domain_pack_id_or_400(payload.version, 'version')
    # 確認者名は本人確認を保証しない (認証は未実装)。作業記録用の表示名である。
    actor = (payload.actor or '').strip()
    if not actor:
        raise HTTPException(422, 'actor is required')
    reason = (payload.reason or '').strip()
    if not reason:
        raise HTTPException(422, 'reason is required')
    from app import domain_spec as _packs
    if _packs.get_spec(safe_id, safe_version) is None:
        raise HTTPException(404, 'パックが見つかりません')
    try:
        return _packs.select_pack(orchestrator, project_id, safe_id,
                                  safe_version, actor=actor, reason=reason)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post('/api/projects/{project_id}/domain-packs/clear')
async def clear_project_domain_pack(project_id: str,
                                    payload: DomainPackClearPayload):
    require_project(project_id)
    # 確認者名は本人確認を保証しない (認証は未実装)。作業記録用の表示名である。
    actor = (payload.actor or '').strip()
    if not actor:
        raise HTTPException(422, 'actor is required')
    reason = (payload.reason or '').strip()
    if not reason:
        raise HTTPException(422, 'reason is required')
    from app import domain_spec as _packs
    try:
        return _packs.clear_pack(orchestrator, project_id,
                                 actor=actor, reason=reason)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get('/api/projects/{project_id}/goal-progress')
async def get_goal_progress(project_id: str):
    require_project(project_id)
    from app.goal_state_model import build_goal_state
    # 読み取り専用: 進捗モデルだけを返す。DB・ファイルを変更しない。
    return await asyncio.to_thread(build_goal_state, orchestrator, project_id)


@app.get('/static/goal_state.js')
async def goal_state_js():
    return FileResponse(ROOT / 'app' / 'static' / 'goal_state.js', media_type='text/javascript', headers={'Cache-Control':'no-store'})


@app.get('/static/watch_panel.js')
async def watch_panel_js():
    return FileResponse(ROOT / 'app' / 'static' / 'watch_panel.js', media_type='text/javascript', headers={'Cache-Control':'no-store'})


class WatchRefreshPayload(BaseModel):
    actor: str = Field(min_length=1, max_length=100)


class WatchAckPayload(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    note: str = Field(default='', max_length=500)


@app.post('/api/watch/refresh')
async def post_watch_refresh(payload: WatchRefreshPayload):
    # 人が押したときだけ動く。定期スレッド・タイマーは作らない。
    actor = payload.actor.strip()
    if not actor:
        raise HTTPException(422, 'actor is required')
    from app import watchtower as _watch
    try:
        return await asyncio.to_thread(_watch.refresh, orchestrator, actor)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get('/api/watch/events')
async def get_watch_events(unread: int = 0, limit: int = 50):
    from app import watchtower as _watch
    try:
        count = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        raise HTTPException(400, 'limit が不正です')
    try:
        events = await asyncio.to_thread(
            _watch.list_events, orchestrator, bool(int(unread)), count)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {'events': events, 'read_only': True, 'external_sends': 0}


@app.post('/api/watch/events/{event_id}/ack')
async def post_watch_event_ack(event_id: str, payload: WatchAckPayload):
    actor = payload.actor.strip()
    if not actor:
        raise HTTPException(422, 'actor is required')
    from app import watchtower as _watch
    try:
        return await asyncio.to_thread(
            _watch.ack_event, orchestrator, event_id, actor,
            payload.note or '')
    except ValueError as exc:
        text = str(exc)
        if '見つかりません' in text:
            raise HTTPException(404, text) from exc
        raise HTTPException(409, text) from exc


@app.get('/api/watch/status')
async def get_watch_status():
    from app import watchtower as _watch
    try:
        status = await asyncio.to_thread(_watch.watch_status, orchestrator)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {**status, 'read_only': True, 'external_sends': 0}


@app.get('/api/watch/decisions')
async def get_watch_decisions():
    from app import watchtower as _watch
    try:
        decisions = await asyncio.to_thread(_watch.collect_decisions,
                                            orchestrator)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {'decisions': decisions, 'read_only': True, 'external_sends': 0}


@app.get('/static/night_run_panel.js')
async def night_run_panel_js():
    return FileResponse(ROOT / 'app' / 'static' / 'night_run_panel.js', media_type='text/javascript', headers={'Cache-Control':'no-store'})


@app.get('/static/shadow_panel.js')
async def shadow_panel_js():
    return FileResponse(ROOT / 'app' / 'static' / 'shadow_panel.js', media_type='text/javascript', headers={'Cache-Control':'no-store'})


@app.get('/static/ledger_panel.js')
async def ledger_panel_js():
    return FileResponse(ROOT / 'app' / 'static' / 'ledger_panel.js', media_type='text/javascript', headers={'Cache-Control':'no-store'})


@app.get('/static/contact_channel_panel.js')
async def contact_channel_panel_js():
    return FileResponse(ROOT / 'app' / 'static' / 'contact_channel_panel.js', media_type='text/javascript', headers={'Cache-Control':'no-store'})


@app.get('/static/planning_guard_panel.js')
async def planning_guard_panel_js():
    return FileResponse(ROOT / 'app' / 'static' / 'planning_guard_panel.js', media_type='text/javascript', headers={'Cache-Control':'no-store'})


@app.get('/static/planning_guard_settings.js')
async def planning_guard_settings_js():
    return FileResponse(ROOT / 'app' / 'static' / 'planning_guard_settings.js', media_type='text/javascript', headers={'Cache-Control':'no-store'})

@app.get('/static/planning_guard_packets.js')
async def planning_guard_packets_js():
    return FileResponse(ROOT / 'app' / 'static' / 'planning_guard_packets.js', media_type='text/javascript', headers={'Cache-Control':'no-store'})


@app.get('/static/planning_guard_jobs.js')
async def planning_guard_jobs_js():
    return FileResponse(ROOT / 'app' / 'static' / 'planning_guard_jobs.js', media_type='text/javascript', headers={'Cache-Control':'no-store'})

@app.get('/static/planning_guard_drafts.js')
async def planning_guard_drafts_js():
    return FileResponse(ROOT / 'app' / 'static' / 'planning_guard_drafts.js', media_type='text/javascript', headers={'Cache-Control':'no-store'})

@app.get('/api/ledger/metrics')
async def get_ledger_metrics():
    # 読み取り専用: 現在の指標を算出するだけ。記録しない。
    from app import outcome_ledger as _ledger
    try:
        return await asyncio.to_thread(_ledger.compute_metrics, orchestrator)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


class LedgerSnapshotPayload(BaseModel):
    actor: str = Field(min_length=1, max_length=100)


@app.post('/api/ledger/snapshot')
async def post_ledger_snapshot(payload: LedgerSnapshotPayload):
    # 人が押したときだけ記録する。threadpool 経由。
    actor = payload.actor.strip()
    if not actor:
        raise HTTPException(422, 'actor is required')
    from app import outcome_ledger as _ledger
    try:
        result = await asyncio.to_thread(_ledger.record_snapshot,
                                         orchestrator, actor)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not isinstance(result, dict) or not bool(result.get("ok")):
        reason = ""
        if isinstance(result, dict):
            reason = str(result.get("reason") or "確認できず")
        raise HTTPException(409, reason or "台帳の記録に失敗しました")
    return result


@app.get('/api/ledger/snapshots')
async def get_ledger_snapshots(limit: int = 10):
    # 読み取り専用: 直近の記録と前回との差。DB・ファイルを変更しない。
    from app import outcome_ledger as _ledger
    try:
        count = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        raise HTTPException(400, 'limit が不正です')
    try:
        return await asyncio.to_thread(_ledger.list_snapshots, orchestrator,
                                       count)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get('/api/ledger/suggestions')
async def get_ledger_suggestions():
    # 読み取り専用: 方針の改善提案。DB・ファイルを変更しない。
    from app import outcome_ledger as _ledger
    try:
        suggestions = await asyncio.to_thread(_ledger.suggest, orchestrator)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {'suggestions': suggestions, 'read_only': True,
            'external_sends': 0}


class LedgerSuggestionDecisionPayload(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    decision: str = Field(min_length=1, max_length=16)
    reason: str = Field(min_length=1, max_length=2000)


@app.post('/api/ledger/suggestions/{suggestion_id}/decision')
async def post_ledger_suggestion_decision(suggestion_id: str,
                                          payload: LedgerSuggestionDecisionPayload):
    # 採否の記録だけ。何も実行・変更しない。
    actor = payload.actor.strip()
    if not actor:
        raise HTTPException(422, 'actor is required')
    decision = (payload.decision or '').strip()
    if decision not in ("adopted", "rejected", "deferred"):
        raise HTTPException(422, 'decision を確認できません')
    reason = (payload.reason or '').strip()
    if not reason:
        raise HTTPException(422, 'reason is required')
    safe_id = str(suggestion_id or '').strip()
    if safe_id == "" or len(safe_id) > 200:
        raise HTTPException(400, 'suggestion_id が不正です')
    from app import outcome_ledger as _ledger
    result = await asyncio.to_thread(_ledger.record_decision, orchestrator,
                                     safe_id, decision, actor, reason)
    if not isinstance(result, dict) or not bool(result.get("ok")):
        reason_text = ""
        if isinstance(result, dict):
            reason_text = str(result.get("reason") or "確認できず")
        raise HTTPException(409, reason_text or "採否の記録に失敗しました")
    return result


class ShadowObservePayload(BaseModel):
    actor: str = Field(min_length=1, max_length=100)


class ShadowLabelPayload(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=16)
    note: str = Field(default='', max_length=2000)
    danger: bool = False


@app.post('/api/projects/{project_id}/shadow/observe')
async def post_shadow_observe(project_id: str, payload: ShadowObservePayload):
    # 人が押したときだけ動く。定期スレッド・スケジューラは作らない。
    require_project(project_id)
    actor = payload.actor.strip()
    if not actor:
        raise HTTPException(422, 'actor is required')
    from app import shadow_mode as _shadow
    try:
        result = await asyncio.to_thread(_shadow.observe, orchestrator,
                                         project_id, actor)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not isinstance(result, dict) or not bool(result.get("ok")):
        reason = ""
        if isinstance(result, dict):
            reason = str(result.get("reason") or "確認できず")
        raise HTTPException(409, reason or "影の記録に失敗しました")
    return result


@app.post('/api/shadow/observe-all')
async def post_shadow_observe_all(payload: ShadowObservePayload):
    # 人が押したときだけ動く。定期スレッド・スケジューラは作らない。
    actor = payload.actor.strip()
    if not actor:
        raise HTTPException(422, 'actor is required')
    from app import shadow_mode as _shadow
    try:
        # threadpool 経由で実行する (既存の流儀。重い読取を分離)。
        return await asyncio.to_thread(_shadow.observe_all, orchestrator,
                                       actor)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get('/api/projects/{project_id}/shadow/records')
async def get_shadow_records(project_id: str, limit: int = 50):
    # 読み取り専用。DB・ファイルを変更しない。
    require_project(project_id)
    from app import shadow_mode as _shadow
    try:
        count = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        raise HTTPException(400, 'limit が不正です')
    try:
        records = await asyncio.to_thread(_shadow.list_records, orchestrator,
                                          project_id, count)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {'project_id': project_id, 'records': records,
            'read_only': True, 'external_sends': 0}


@app.post('/api/shadow/records/{record_id}/label')
async def post_shadow_label(record_id: str, payload: ShadowLabelPayload):
    actor = payload.actor.strip()
    if not actor:
        raise HTTPException(422, 'actor is required')
    label_value = (payload.label or "").strip()
    if label_value not in ("correct", "wrong", "unsure"):
        raise HTTPException(422, 'label を確認できません')
    if bool(payload.danger) and label_value != "wrong":
        raise HTTPException(422, 'danger は誤りのときだけ付けられます')
    from app import shadow_mode as _shadow
    result = await asyncio.to_thread(_shadow.label, orchestrator, record_id,
                                     label_value, actor,
                                     payload.note or "",
                                     bool(payload.danger))
    if not isinstance(result, dict) or not bool(result.get("ok")):
        reason = ""
        if isinstance(result, dict):
            reason = str(result.get("reason") or "確認できず")
        if "見つかりません" in reason:
            raise HTTPException(404, reason)
        raise HTTPException(409, reason or "ラベルの記録に失敗しました")
    return result


@app.get('/api/projects/{project_id}/shadow/metrics')
async def get_shadow_metrics(project_id: str):
    # 読み取り専用。DB・ファイルを変更しない。
    require_project(project_id)
    from app import shadow_mode as _shadow
    try:
        data = await asyncio.to_thread(_shadow.metrics, orchestrator,
                                       project_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {**data, 'read_only': True, 'external_sends': 0}


@app.get('/api/projects/{project_id}/shadow/gate')
async def get_shadow_gate(project_id: str):
    # 読み取り専用。DB・ファイルを変更しない。
    require_project(project_id)
    from app import shadow_mode as _shadow
    try:
        result = await asyncio.to_thread(_shadow.gate, orchestrator,
                                         project_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {**result, 'read_only': True, 'external_sends': 0}


@app.get('/api/shadow/summary')
async def get_shadow_summary():
    # 読み取り専用。DB・ファイルを変更しない。
    from app import shadow_mode as _shadow
    try:
        result = await asyncio.to_thread(_shadow.summary, orchestrator)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {**result, 'read_only': True, 'external_sends': 0}


class NightRunStartPayload(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    budget: dict = Field(default_factory=dict)


class NightRunTickPayload(BaseModel):
    token: str = Field(min_length=1, max_length=256)


class NightRunFinishPayload(BaseModel):
    token: str = Field(min_length=1, max_length=256)
    actor: str = Field(min_length=1, max_length=100)
    end_reason: str = Field(default='', max_length=500)


class AutopilotDisableAllPayload(BaseModel):
    actor: str = Field(min_length=1, max_length=100)


class KillSwitchReleasePayload(BaseModel):
    actor: str = Field(min_length=1, max_length=100)


@app.post('/api/night-run/start')
async def post_night_run_start(payload: NightRunStartPayload):
    # 人が起動したときだけ動く。実際のティックは回さず、実行を running として
    # 登録しIDとトークンを返す (外部ランナー用の受付)。すでに実行中なら409。
    actor = payload.actor.strip()
    if not actor:
        raise HTTPException(422, 'actor is required')
    from app import night_run as _night
    try:
        return await asyncio.to_thread(_night.start_run, orchestrator, actor,
                                       payload.budget or None)
    except _night.NightRunBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post('/api/night-run/{run_id}/tick-next')
async def post_night_run_tick_next(run_id: str, payload: NightRunTickPayload):
    # ランナーが呼ぶ。次に回すPJをサーバ側で選び、1ティックだけ既存の
    # autopilot_tick を実行して結果を記録する。重い処理はスレッドプール。
    from app import night_run as _night
    try:
        return await asyncio.to_thread(_night.tick_next, orchestrator,
                                       run_id, payload.token or '')
    except ValueError as exc:
        text = str(exc)
        if '見つかりません' in text:
            raise HTTPException(404, text) from exc
        raise HTTPException(409, text) from exc


@app.post('/api/night-run/{run_id}/finish')
async def post_night_run_finish(run_id: str, payload: NightRunFinishPayload):
    actor = payload.actor.strip()
    if not actor:
        raise HTTPException(422, 'actor is required')
    from app import night_run as _night
    try:
        return await asyncio.to_thread(_night.finish_run, orchestrator,
                                       run_id, payload.token or '', actor,
                                       payload.end_reason or '')
    except ValueError as exc:
        text = str(exc)
        if '見つかりません' in text:
            raise HTTPException(404, text) from exc
        raise HTTPException(409, text) from exc


@app.get('/api/night-run/latest')
async def get_night_run_latest():
    # 読み取り専用: 最新の実行 (報告つき)。DB・ファイルを変更しない。
    from app import night_run as _night
    try:
        return await asyncio.to_thread(_night.get_latest, orchestrator)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get('/api/night-run/{run_id}')
async def get_night_run(run_id: str):
    # 読み取り専用。DB・ファイルを変更しない。
    from app import night_run as _night
    try:
        return await asyncio.to_thread(_night.get_run, orchestrator,
                                       run_id)
    except ValueError as exc:
        text = str(exc)
        if '見つかりません' in text:
            raise HTTPException(404, text) from exc
        raise HTTPException(409, text) from exc


@app.post('/api/autopilot/disable-all')
async def post_autopilot_disable_all(payload: AutopilotDisableAllPayload):
    # 全PJの自走を無効化し (各PJの監査に記録)、同時にキルスイッチを active にする。
    actor = payload.actor.strip()
    if not actor:
        raise HTTPException(422, 'actor is required')
    from app import night_run as _night
    try:
        return await asyncio.to_thread(_night.disable_all, orchestrator,
                                       actor)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post('/api/autopilot/kill-switch/release')
async def post_kill_switch_release(payload: KillSwitchReleasePayload):
    # 解除は人が行う (actor必須)。
    actor = payload.actor.strip()
    if not actor:
        raise HTTPException(422, 'actor is required')
    from app import night_run as _night
    try:
        return await asyncio.to_thread(_night.kill_switch_release,
                                       orchestrator, actor)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get('/api/autopilot/kill-switch')
async def get_kill_switch():
    # 読み取り専用。DB・ファイルを変更しない。
    from app import night_run as _night
    try:
        state = await asyncio.to_thread(_night.kill_switch_state,
                                        orchestrator)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {'state': state, 'read_only': True, 'external_sends': 0}


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


class RunEvidencePayload(BaseModel):
    assignee: str = Field(min_length=1, max_length=100)
    evidence: str = Field(min_length=1, max_length=2000)
    reason: str = Field(min_length=1, max_length=2000)


class RunNextPayload(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    plan_signature: str = Field(default="", max_length=500)
    source_hash: str = Field(default="", max_length=256)
    reason: str = Field(default="", max_length=500)


class ChannelSkillVerificationPayload(BaseModel):
    verification_kind: str = Field(min_length=1, max_length=16)
    actor: str = Field(min_length=1, max_length=100)
    memo: str = Field(min_length=1, max_length=2000)
    action: str = Field(default="verify", max_length=16)


class WebSearchRunPayload(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    plan_item_id: str = Field(min_length=1, max_length=64)
    purpose: str = Field(min_length=1, max_length=2000)
    max_results: int = Field(default=4, ge=1, le=6)


class MailReceiveConfigPayload(BaseModel):
    account_label: str = Field(min_length=1, max_length=200)
    allowed_folders: list[str] = Field(min_length=1, max_length=20)
    max_period_days: int = Field(default=31, ge=1, le=90)
    purpose: str = Field(min_length=1, max_length=2000)
    retention_days: int = Field(default=90, ge=0)
    allowed_viewers: list[str] = Field(min_length=1, max_length=20)
    credential_env_prefix: str = Field(min_length=1, max_length=41)
    actor: str = Field(min_length=1, max_length=100)


class MailReceiveRunPayload(BaseModel):
    folder: str = Field(min_length=1, max_length=128)
    since: str = Field(min_length=8, max_length=32)
    until: str = Field(min_length=8, max_length=32)
    purpose: str = Field(min_length=1, max_length=2000)


class MailCandidateConfirmPayload(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    memo: str = Field(min_length=1, max_length=2000)


@app.post('/api/projects/{project_id}/channel-skills/web.search_public/run')
async def post_project_web_search_run(project_id: str, payload: WebSearchRunPayload):
    # C2: 既存の公開Web収集を共通契約で包む。許可なし・検索語の拒否・
    # 不明な項目IDは分かる理由つきで409/422。研究器は web.py で既に
    # 生成されているものを使う(新たな外部接続を作らない)。
    require_project(project_id)
    import app.channel_skill_web as _web_skill
    import app.channel_skills as _skills
    safe_capability = "web.search_public"
    query = (payload.query or "")
    item_id = (payload.plan_item_id or "").strip()
    purpose = (payload.purpose or "").strip()
    if not query.strip():
        raise HTTPException(422, "query is required")
    if not item_id:
        raise HTTPException(422, "plan_item_id is required")
    if not purpose:
        raise HTTPException(422, "purpose is required")
    try:
        limit = max(1, min(int(payload.max_results), 6))
    except (TypeError, ValueError):
        raise HTTPException(422, "max_results is invalid")
    # operating_mode は B3の運用区分から決める。未設定・読取不能は
    # production 相当の厳しい側。
    try:
        from app.contact_registry import current_operating_mode
        current = current_operating_mode(memory.path, project_id)
        mode = str((current or {}).get("mode") or "") if isinstance(current, dict) else ""
    except Exception:
        mode = ""
    operating_mode = mode if mode in ("demo_test", "production") else "production"
    # 研究器は既に生成されているものを使う(新たな外部接続を作らない)。
    researcher = None
    try:
        if orchestrator is not None:
            researcher = getattr(orchestrator, "public_web_researcher", None)
    except (AttributeError, TypeError):
        researcher = None
    if researcher is None:
        try:
            if orchestrator is not None:
                researcher = getattr(orchestrator, "researcher", None)
        except (AttributeError, TypeError):
            researcher = None
    if researcher is None:
        raise HTTPException(409, detail={
            "code": "WEB_SEARCH_NOT_CONFIGURED",
            "message": "公開Web調査の研究器が設定されていません",
        })
    try:
        result = await _web_skill.run_web_search(
            memory.path, project_id, query=query, plan_item_id=item_id,
            purpose=purpose, operating_mode=operating_mode,
            researcher=researcher, max_results=limit)
    except _web_skill.WebSearchNotAllowed as exc:
        raise HTTPException(409, detail={
            "code": "WEB_SEARCH_NOT_ALLOWED",
            "message": str(exc.reason or exc),
        }) from exc
    except _web_skill.WebSearchRefused as exc:
        text = str(exc)
        if "plan item" in text:
            raise HTTPException(422, text) from exc
        if "query rejected" in text:
            raise HTTPException(422, text) from exc
        raise HTTPException(422, text) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    status = str(result.get("status") or "unknown")
    evidence = result.get("evidence") or []
    if not isinstance(evidence, list):
        evidence = []
    # 応答は status・証拠の一覧(伏せた抜粋・ハッシュ・URL・取得時刻)・拒否の理由。
    # 本文そのものは返さない(証拠の伏せ抜粋のみ)。
    public_rows = []
    for row in evidence:
        if not isinstance(row, dict):
            continue
        public_rows.append({
            "url": str(row.get("url") or ""),
            "fetched_at": str(row.get("fetched_at") or ""),
            "body_sha256": str(row.get("body_sha256") or ""),
            "plan_item_id": str(row.get("plan_item_id") or ""),
            "untrusted": True,
            "excerpt": str(row.get("excerpt") or ""),
            "excerpt_redacted": bool(row.get("excerpt_redacted")),
            "redact_reason": str(row.get("redact_reason") or ""),
            "provider": str(row.get("provider") or ""),
            "run_id": str(row.get("run_id") or ""),
        })
    run_view = result.get("run")
    if not isinstance(run_view, dict):
        run_view = {}
    return {"project_id": project_id, "capability_id": safe_capability,
            "status": status,
            "reason": str(result.get("reason") or ""),
            "run": run_view, "evidence": public_rows,
            "operating_mode": operating_mode,
            "external_sends": 0, "read_only": False}


@app.get('/api/projects/{project_id}/plan-items/{plan_item_id}/channel-evidence')
async def get_project_plan_item_channel_evidence(project_id: str,
                                                 plan_item_id: str):
    # C2: その計画項目に結び付いた全スキルの証拠(他PJの行を返さない)。
    # 読取失敗は『実行なし』にならず409。
    require_project(project_id)
    import app.channel_skills as _skills
    item = str(plan_item_id or "").strip()
    if not item:
        raise HTTPException(422, "plan_item_id is required")
    try:
        rows = await asyncio.to_thread(
            _skills.list_evidence_by_plan_item, memory.path,
            project_id, item)
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except _skills.ChannelSkillReadError as exc:
        raise HTTPException(409, detail={
            "code": "CHANNEL_SKILL_READ_FAILED",
            "message": "チャネルスキルの台帳を読めません",
            "failure_kind": exc.kind,
            "path_kind": exc.path_kind,
            "sqlite_error": exc.sqlite_error,
        }) from exc
    except Exception as exc:
        raise HTTPException(409, detail={
            "code": "CHANNEL_SKILL_READ_FAILED",
            "message": "チャネルスキルの台帳を読めません",
            "error_type": type(exc).__name__,
        }) from exc
    return {"project_id": project_id, "plan_item_id": item,
            "evidence": rows, "read_only": True}


@app.get('/api/projects/{project_id}/runs/latest')
async def get_project_latest_run(project_id: str):
    # P0-1: 実行サイクルの最小読み取りAPI。PJの状態を書き換えない。
    # 記録が無ければ『実行履歴未判定』。読取失敗は読取不能として明示する。
    require_project(project_id)
    import app.run_cycles as _runs
    import logging as _logging
    import sqlite3 as _sqlite3
    try:
        viewed = await asyncio.to_thread(_runs.get_latest_run, memory.path, project_id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except _sqlite3.Error as exc:
        _logging.getLogger(__name__).warning(
            "run cycle read unavailable: %s", type(exc).__name__)
        raise HTTPException(409, detail={
            "code": "RUN_CYCLE_READ_FAILED",
            "message": "実行サイクル記録を読めません",
            "error_type": type(exc).__name__,
        }) from exc
    except _runs.RunCycleReadError as exc:
        raise HTTPException(409, detail={
            "code": "RUN_CYCLE_READ_FAILED",
            "message": "実行サイクル記録を読めません",
            "failure_kind": exc.kind,
            "path_kind": exc.path_kind,
            "sqlite_error": exc.sqlite_error,
        }) from exc
    except Exception as exc:
        _logging.getLogger(__name__).warning(
            "run cycle read unavailable: %s", type(exc).__name__)
        raise HTTPException(409, detail={
            "code": "RUN_CYCLE_READ_FAILED",
            "message": "実行サイクル記録を読めません",
            "error_type": type(exc).__name__,
        }) from exc
    # 読取失敗は run_cycles 側で例外になるため、ここでは『空』に倒さない。
    # 読み取り前後でmissionの状態を変えない(読むだけ)。
    return viewed


@app.get('/api/projects/{project_id}/runs')
async def list_project_runs(project_id: str, limit: int = 50):
    # B4: 履歴一覧を新しい順で返す(読み取り専用)。読取失敗は409。
    require_project(project_id)
    import app.run_cycles as _runs
    import logging as _logging
    import sqlite3 as _sqlite3
    try:
        count = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        raise HTTPException(400, "limit が不正です")
    try:
        items = await asyncio.to_thread(_runs.list_project_runs, memory.path,
                                        project_id, count)
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except _sqlite3.Error as exc:
        _logging.getLogger(__name__).warning(
            "run cycle read unavailable: %s", type(exc).__name__)
        raise HTTPException(409, detail={
            "code": "RUN_CYCLE_READ_FAILED",
            "message": "実行サイクル記録を読めません",
            "error_type": type(exc).__name__,
        }) from exc
    except _runs.RunCycleReadError as exc:
        raise HTTPException(409, detail={
            "code": "RUN_CYCLE_READ_FAILED",
            "message": "実行サイクル記録を読めません",
            "failure_kind": exc.kind,
            "path_kind": exc.path_kind,
            "sqlite_error": exc.sqlite_error,
        }) from exc
    except Exception as exc:
        _logging.getLogger(__name__).warning(
            "run cycle read unavailable: %s", type(exc).__name__)
        raise HTTPException(409, detail={
            "code": "RUN_CYCLE_READ_FAILED",
            "message": "実行サイクル記録を読めません",
            "error_type": type(exc).__name__,
        }) from exc
    return {"project_id": project_id, "runs": items, "read_only": True}


@app.get('/api/projects/{project_id}/runs/{run_id}/result')
async def get_project_run_result(project_id: str, run_id: str):
    # B4: 当時の最小結果(読み取り専用・保存済み)。他PJの run_id は404。
    require_project(project_id)
    import app.run_cycles as _runs
    import logging as _logging
    import sqlite3 as _sqlite3
    safe_run = str(run_id or "").strip()
    if not safe_run:
        raise HTTPException(422, "run_id is required")
    try:
        viewed = await asyncio.to_thread(_runs.get_run_result, memory.path,
                                         project_id, safe_run)
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except _sqlite3.Error as exc:
        _logging.getLogger(__name__).warning(
            "run cycle read unavailable: %s", type(exc).__name__)
        raise HTTPException(409, detail={
            "code": "RUN_CYCLE_READ_FAILED",
            "message": "実行サイクル記録を読めません",
            "error_type": type(exc).__name__,
        }) from exc
    except _runs.RunCycleReadError as exc:
        raise HTTPException(409, detail={
            "code": "RUN_CYCLE_READ_FAILED",
            "message": "実行サイクル記録を読めません",
            "failure_kind": exc.kind,
            "path_kind": exc.path_kind,
            "sqlite_error": exc.sqlite_error,
        }) from exc
    except Exception as exc:
        _logging.getLogger(__name__).warning(
            "run cycle read unavailable: %s", type(exc).__name__)
        raise HTTPException(409, detail={
            "code": "RUN_CYCLE_READ_FAILED",
            "message": "実行サイクル記録を読めません",
            "error_type": type(exc).__name__,
        }) from exc
    if viewed is None:
        raise HTTPException(404, "対象の実行記録がありません")
    return viewed


def _fetch_run_and_open_item(memory_path, project_id: str, run_id: str,
                             open_item_id: int) -> tuple[dict, dict]:
    """証拠登録前の束縛確認。読取失敗は RunCycleReadError(空に倒さない)。"""
    import app.run_cycles as _runs
    import sqlite3 as _sqlite3

    path = _runs.item_ledger_db_path(memory_path)
    if not path.exists():
        raise _runs.RunCycleReadError("table_missing", detail="no ledger db")
    try:
        connection = _runs._connect_ro(memory_path)
        db = connection.__enter__()
    except _runs.RunCycleReadError:
        raise
    try:
        try:
            names = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        except _sqlite3.Error as exc:
            raise _runs.RunCycleReadError(
                "table_unreadable",
                sqlite_error=type(exc).__name__) from exc
        if "project_run_cycles" not in names or "project_run_open_items" not in names:
            raise _runs.RunCycleReadError("table_missing", detail="run tables")
        try:
            run_row = db.execute(
                "SELECT * FROM project_run_cycles WHERE run_id=?", (run_id,)).fetchone()
            item_row = db.execute(
                "SELECT * FROM project_run_open_items WHERE id=?", (open_item_id,)).fetchone()
            replacement = db.execute(
                "SELECT id FROM project_run_open_items WHERE run_id=? AND supersedes_id=? LIMIT 1",
                (run_id, open_item_id)).fetchone()
        except _sqlite3.Error as exc:
            raise _runs.RunCycleReadError(
                "row_unreadable", sqlite_error=type(exc).__name__) from exc
        if run_row is None:
            raise ValueError("run not found")
        if item_row is None:
            raise ValueError("open item not found")
        if replacement is not None:
            raise ValueError("open item is not the current version")
        return dict(run_row), dict(item_row)
    finally:
        connection.__exit__(None, None, None)


@app.post('/api/projects/{project_id}/runs/{run_id}/open-items/{open_item_id}/evidence')
async def post_run_open_item_evidence(project_id: str, run_id: str,
                                      open_item_id: int, payload: RunEvidencePayload):
    # 証拠登録は新しい版の残件として追記し、解決済み・達成にしない。再判定は新しいrun_idで行う。
    require_project(project_id)
    assignee = (payload.assignee or "").strip()
    evidence = (payload.evidence or "").strip()
    reason = (payload.reason or "").strip()
    if not assignee or not evidence or not reason:
        raise HTTPException(422, "assignee, evidence and reason are required")
    safe_run = str(run_id or "").strip()
    if not safe_run:
        raise HTTPException(422, "run_id is required")
    try:
        target_id = int(open_item_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise HTTPException(422, "open_item_id is required") from None
    import app.run_cycles as _runs
    try:
        run_row, item_row = await asyncio.to_thread(
            _fetch_run_and_open_item, memory.path, project_id, safe_run, target_id)
    except _runs.RunCycleReadError as exc:
        raise HTTPException(409, detail={
            "code": "RUN_CYCLE_READ_FAILED",
            "message": "実行サイクル記録を読めません",
            "failure_kind": exc.kind,
            "path_kind": exc.path_kind,
            "sqlite_error": exc.sqlite_error,
        }) from exc
    except ValueError as exc:
        if str(exc) == "open item is not the current version":
            raise HTTPException(409, detail={
                "code": "OPEN_ITEM_SUPERSEDED",
                "message": "対象の残件は現在の版ではありません",
            }) from exc
        raise HTTPException(404, str(exc)) from exc
    if str(run_row.get("project_id") or "") != project_id:
        raise HTTPException(409, "対象の実行はこのPJのものではありません")
    if str(item_row.get("project_id") or "") != project_id:
        raise HTTPException(409, "対象の残件はこのPJのものではありません")
    if str(item_row.get("run_id") or "") != safe_run:
        raise HTTPException(409, "対象の残件は指定の実行のものではありません")
    prev_evidence = str(item_row.get("evidence_needed") or "")
    prev_action = str(item_row.get("next_action") or "")
    status_text = (
        "証拠登録済み・再判定待ち: "
        f"担当={assignee} / 証拠={evidence} / 理由={reason}"
    )[:2000]
    new_evidence = (prev_evidence + " / 登録証拠: " + evidence)[:2000] if prev_evidence else (
        "登録証拠: " + evidence)[:2000]
    if prev_action:
        new_action = (prev_action + " / 証拠登録後は新しいrun_idで再判定する")[:2000]
    else:
        new_action = "証拠登録後は新しいrun_idで再判定する"
    try:
        saved = await asyncio.to_thread(
            _runs.correct_open_item, memory.path, target_id,
            status_text=status_text, evidence_needed=new_evidence,
            next_action=new_action,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "id": int(saved.get("id") or 0),
        "run_id": str(saved.get("run_id") or safe_run),
        "project_id": project_id,
        "supersedes_id": int(saved.get("supersedes_id") or target_id),
        "version": int(saved.get("version") or 0),
        "state": "証拠登録済み・再判定待ち",
        "resolved": False,
        "goal_achieved": False,
        "note": "証拠の登録だけでは残件を解決済み・目標達成にしない。再判定は新しいrun_idで行う。",
    }


@app.post('/api/projects/{project_id}/runs/next')
async def post_project_next_run(project_id: str, payload: RunNextPayload):
    # 次の実行を開始できるか事前確認するだけで、実行サイクルは作らない。
    # 実行中があるときは拒否し、計画版・入力ハッシュの食い違いも拒否する。
    require_project(project_id)
    actor = (payload.actor or "").strip()
    if not actor:
        raise HTTPException(422, "actor is required")
    plan_sig = (payload.plan_signature or "").strip()
    src_hash = (payload.source_hash or "").strip()
    if not plan_sig or not src_hash:
        raise HTTPException(422, "plan_signature and source_hash are required")
    import app.run_cycles as _runs
    try:
        latest = await asyncio.to_thread(_runs.get_latest_run, memory.path, project_id)
    except _runs.RunCycleReadError as exc:
        raise HTTPException(409, detail={
            "code": "RUN_CYCLE_READ_FAILED",
            "message": "実行サイクル記録を読めません",
            "failure_kind": exc.kind,
            "path_kind": exc.path_kind,
            "sqlite_error": exc.sqlite_error,
        }) from exc
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    if not isinstance(latest, dict) or latest.get("found") is not True:
        raise HTTPException(409, detail={
            "code": "RUN_NOT_FOUND",
            "message": "対象の実行記録がありません",
        })
    if isinstance(latest, dict) and latest.get("found") is True:
        if str(latest.get("execution_state") or "") == "running":
            raise HTTPException(409, detail={
                "code": "RUNNING_EXISTS",
                "message": "実行中のサイクルがあるため開始できません",
                "run_id": str(latest.get("run_id") or ""),
            })
        prev_sig = str(latest.get("plan_signature") or "")
        prev_hash = str(latest.get("source_hash") or "")
        if (prev_sig or prev_hash) and (plan_sig != prev_sig or src_hash != prev_hash):
            raise HTTPException(409, detail={
                "code": "PLAN_BINDING_MISMATCH",
                "message": "計画版・入力ハッシュが直前の実行と食い違うため開始できません",
                "expected_plan_signature": prev_sig,
                "expected_source_hash": prev_hash,
            })
    # 現在の計画・原本との照合(確認できなければ開始しない。成功に見せない)。
    try:
        from app.goal_review import plan_snapshot as _snap
        _, cur_sig = _snap(orchestrator, project_id)
        from app.vehicle_workflow import digest as _digest, sources as _sources
        cur_hash = _digest(_sources(orchestrator, project_id))
    except (OSError, ValueError, RuntimeError, AttributeError, KeyError, TypeError) as exc:
        raise HTTPException(409, detail={
            "code": "RUN_BINDING_UNREADABLE",
            "message": "現在の計画・原本を確認できないため開始できません",
            "error_type": type(exc).__name__,
        }) from exc
    if not str(cur_sig or "") or not str(cur_hash or ""):
        raise HTTPException(409, detail={
            "code": "RUN_BINDING_UNREADABLE",
            "message": "現在の計画・原本を確認できないため開始できません",
        })
    if plan_sig != str(cur_sig or "") or src_hash != str(cur_hash or ""):
        raise HTTPException(409, detail={
            "code": "PLAN_BINDING_MISMATCH",
            "message": "計画版・入力ハッシュが現在の計画・原本と食い違うため開始できません",
        })
    prev_id = str(latest.get("run_id") or "")
    return {
        "can_start": True,
        "previous_run_id": prev_id,
        "plan_signature": str(cur_sig),
        "source_hash": str(cur_hash),
        "reason": None,
        "read_only": True,
        "note": "事前確認のみ。実行サイクルは作成していない。既存の開始操作へ進むこと。",
    }


@app.get('/api/projects/{project_id}/channel-skills')
async def get_project_channel_skills(project_id: str):
    # C1: 4スキルの定義・状態・成熟度4区分・直近の実行の要約(読み取り専用)。
    # 読取失敗は『読取不能』として409にし、『接続なし』『実行なし』に倒さない。
    require_project(project_id)
    import app.channel_skills as _skills
    try:
        items = await asyncio.to_thread(_skills.list_skills, memory.path,
                                        project_id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except _skills.ChannelSkillReadError as exc:
        raise HTTPException(409, detail={
            "code": "CHANNEL_SKILL_READ_FAILED",
            "message": "チャネルスキルの台帳を読めません",
            "failure_kind": exc.kind,
            "path_kind": exc.path_kind,
            "sqlite_error": exc.sqlite_error,
        }) from exc
    except Exception as exc:
        raise HTTPException(409, detail={
            "code": "CHANNEL_SKILL_READ_FAILED",
            "message": "チャネルスキルの台帳を読めません",
            "error_type": type(exc).__name__,
        }) from exc
    return {"project_id": project_id, "skills": items, "read_only": True}


@app.get('/api/projects/{project_id}/channel-skills/{capability_id}/runs')
async def get_project_channel_skill_runs(project_id: str, capability_id: str,
                                         limit: int = 50):
    # C1: 実行履歴と証拠(読み取り専用・当該PJの行だけ)。読取失敗は409。
    require_project(project_id)
    import app.channel_skills as _skills
    safe_capability = str(capability_id or "").strip()
    if safe_capability not in _skills.SKILL_DEFINITIONS:
        raise HTTPException(404, "対象のスキルがありません")
    try:
        count = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        raise HTTPException(400, "limit が不正です")
    try:
        runs = await asyncio.to_thread(_skills.list_runs, memory.path,
                                       project_id, safe_capability, count)
        evidence = await asyncio.to_thread(_skills.list_evidence, memory.path,
                                           project_id, safe_capability)
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except _skills.ChannelSkillReadError as exc:
        raise HTTPException(409, detail={
            "code": "CHANNEL_SKILL_READ_FAILED",
            "message": "チャネルスキルの台帳を読めません",
            "failure_kind": exc.kind,
            "path_kind": exc.path_kind,
            "sqlite_error": exc.sqlite_error,
        }) from exc
    except Exception as exc:
        raise HTTPException(409, detail={
            "code": "CHANNEL_SKILL_READ_FAILED",
            "message": "チャネルスキルの台帳を読めません",
            "error_type": type(exc).__name__,
        }) from exc
    return {"project_id": project_id, "capability_id": safe_capability,
            "runs": runs, "evidence": evidence, "read_only": True}


@app.post('/api/projects/{project_id}/channel-skills/{capability_id}/verification',
          status_code=201)
async def post_project_channel_skill_verification(
        project_id: str, capability_id: str,
        payload: ChannelSkillVerificationPayload):
    # C1: 権限確認・実機検証の人による確認記録(追記型)。担当者・メモ必須。
    # 定義や接続だけでは確認済みにしない。取消は action=revoke の行を追加する。
    require_project(project_id)
    import app.channel_skills as _skills
    safe_capability = str(capability_id or "").strip()
    if safe_capability not in _skills.SKILL_DEFINITIONS:
        raise HTTPException(404, "対象のスキルがありません")
    kind = (payload.verification_kind or "").strip()
    if kind not in ("permission", "field"):
        raise HTTPException(422, "verification_kind は permission/field です")
    action = (payload.action or "verify").strip() or "verify"
    if action not in ("verify", "revoke"):
        raise HTTPException(422, "action は verify/revoke です")
    actor = (payload.actor or "").strip()
    memo = (payload.memo or "").strip()
    if not actor or not memo:
        raise HTTPException(422, "actor and memo are required")
    try:
        saved = await asyncio.to_thread(
            _skills.record_verification, memory.path,
            project_id=project_id, capability_id=safe_capability,
            verification_kind=kind, actor=actor, memo=memo, action=action)
        maturity = await asyncio.to_thread(
            _skills.maturity, memory.path, project_id, safe_capability)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except _skills.ChannelSkillReadError as exc:
        raise HTTPException(409, detail={
            "code": "CHANNEL_SKILL_READ_FAILED",
            "message": "チャネルスキルの台帳を読めません",
            "failure_kind": exc.kind,
            "path_kind": exc.path_kind,
            "sqlite_error": exc.sqlite_error,
        }) from exc
    return {"verification": saved, "maturity": maturity,
            "auth_note": "確認者名は本人確認を保証しません(認証は未実装)。",
            "project_id": project_id, "capability_id": safe_capability}


def _mail_http_error(exc: BaseException) -> HTTPException:
    import app.channel_skill_mail as _mail
    if isinstance(exc, _mail.MailReceiveReadError):
        return HTTPException(409, detail={
            "code": "MAIL_RECEIVE_READ_FAILED",
            "message": "メール受信の台帳を読めません",
            "failure_kind": exc.kind,
            "path_kind": exc.path_kind,
            "sqlite_error": exc.sqlite_error,
        })
    return HTTPException(409, str(exc))


@app.post('/api/projects/{project_id}/channel-skills/mail.receive/config',
          status_code=201)
async def post_project_mail_receive_config(project_id: str,
                                           payload: MailReceiveConfigPayload):
    # C3: 接続設定の登録は人の明示操作。担当者・目的が必須。資格情報なし。
    require_project(project_id)
    import app.channel_skill_mail as _mail
    body = {
        "account_label": payload.account_label.strip(),
        "allowed_folders": [str(x).strip() for x in (payload.allowed_folders or [])],
        "max_period_days": int(payload.max_period_days),
        "purpose": payload.purpose.strip(),
        "retention_days": int(payload.retention_days),
        "allowed_viewers": [str(x).strip() for x in (payload.allowed_viewers or [])],
        "credential_env_prefix": payload.credential_env_prefix.strip(),
        "actor": payload.actor.strip(),
    }
    try:
        current = await asyncio.to_thread(
            _mail.get_mail_settings, memory.path, project_id)
    except _mail.MailReceiveReadError as exc:
        raise _mail_http_error(exc) from exc
    if not isinstance(body["account_label"], str) or not body["account_label"]:
        raise HTTPException(422, "account_label is required")
    if not isinstance(body["purpose"], str) or not body["purpose"]:
        raise HTTPException(422, "purpose is required")
    if not isinstance(body["actor"], str) or not body["actor"]:
        raise HTTPException(422, "actor is required")
    try:
        saved = await asyncio.to_thread(
            _mail.save_mail_settings, memory.path, project_id, body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except _mail.MailReceiveReadError as exc:
        raise _mail_http_error(exc) from exc
    try:
        matured = await asyncio.to_thread(
            _mail.mail_connection_view, memory.path, project_id)
    except _mail.MailReceiveReadError as exc:
        raise _mail_http_error(exc) from exc
    return {"project_id": project_id, "capability_id": "mail.receive",
            "settings": saved, "connection": {
                "configured": bool(matured.get("configured")),
                "reason": str(matured.get("reason") or ""),
            },
            "auth_note": "確認者名は本人確認を保証しません(認証は未実装)。",
            "read_only": False, "external_sends": 0}


@app.get('/api/projects/{project_id}/channel-skills/mail.receive/config')
async def get_project_mail_receive_config(project_id: str):
    # C3: 現在と履歴(追記型)。読取不能は『設定なし』に倒さず409。
    require_project(project_id)
    import app.channel_skill_mail as _mail
    try:
        viewed = await asyncio.to_thread(
            _mail.get_mail_settings, memory.path, project_id)
        matured = await asyncio.to_thread(
            _mail.mail_connection_view, memory.path, project_id)
    except _mail.MailReceiveReadError as exc:
        raise _mail_http_error(exc) from exc
    return {"project_id": project_id, "capability_id": "mail.receive",
            "current": viewed.get("current"), "history": viewed.get("history"),
            "status": viewed.get("status"),
            "connection": {
                "configured": bool(matured.get("configured")),
                "reason": str(matured.get("reason") or ""),
            },
            "read_only": True, "external_sends": 0}


@app.post('/api/projects/{project_id}/channel-skills/mail.receive/run')
async def post_project_mail_receive_run(project_id: str,
                                        payload: MailReceiveRunPayload):
    # C3: 人の明示操作。operating_mode はB3の運用区分から決める。
    # 未設定・読取不能は production 相当の厳しい側。
    require_project(project_id)
    import app.channel_skill_mail as _mail
    folder = (payload.folder or "").strip()
    since = (payload.since or "").strip()
    until = (payload.until or "").strip()
    purpose = (payload.purpose or "").strip()
    if not folder:
        raise HTTPException(422, "folder is required")
    if not since or not until:
        raise HTTPException(422, "since and until are required")
    if not purpose:
        raise HTTPException(422, "purpose is required")
    try:
        from app.contact_registry import current_operating_mode
        current = current_operating_mode(memory.path, project_id)
        mode = str((current or {}).get("mode") or "") if isinstance(current, dict) else ""
    except Exception:
        mode = ""
    operating_mode = mode if mode in ("demo_test", "production") else "production"
    try:
        current_settings = await asyncio.to_thread(
            _mail.get_current_settings, memory.path, project_id)
    except _mail.MailReceiveReadError as exc:
        raise _mail_http_error(exc) from exc
    if not isinstance(current_settings, dict) or not current_settings:
        raise HTTPException(409, detail={
            "code": "MAIL_RECEIVE_NOT_CONFIGURED",
            "message": "メール受信の接続設定がありません",
        })
    # アダプタは本番の保持先(標準IMAP)から作る。試験はその保持先を偽に差し替える。
    try:
        adapter = await asyncio.to_thread(
            _mail.build_standard_adapter, dict(current_settings))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(409, detail={
            "code": "MAIL_RECEIVE_NOT_CONFIGURED",
            "message": "メール受信の接続設定を確認できません",
            "error_type": type(exc).__name__,
        }) from exc
    try:
        result = await asyncio.to_thread(
            _mail.run_mail_receive, memory.path, project_id,
            folder=folder, since=since, until=until, purpose=purpose,
            operating_mode=operating_mode, adapter=adapter)
    except _mail.MailReceiveNotConfigured as exc:
        raise HTTPException(409, detail={
            "code": "MAIL_RECEIVE_NOT_CONFIGURED",
            "message": str(exc.reason or exc),
        }) from exc
    except _mail.MailReceiveRefused as exc:
        reason = str(exc.reason or "")
        if reason in ("folder_invalid", "period_invalid", "purpose_invalid"):
            raise HTTPException(422, f"{reason}: {exc.detail}") from exc
        raise HTTPException(409, detail={
            "code": "MAIL_RECEIVE_REFUSED",
            "message": f"{reason}: {exc.detail}" if reason else str(exc),
            "reason": reason,
        }) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    status = str(result.get("status") or "unknown")
    messages = result.get("messages") or []
    if not isinstance(messages, list):
        messages = []
    public_messages = []
    for row in messages:
        if not isinstance(row, dict):
            continue
        public_messages.append({
            "provider_id": str(row.get("provider_id") or ""),
            "message_id": str(row.get("message_id") or ""),
            "in_reply_to": str(row.get("in_reply_to") or ""),
            "references": list(row.get("references") or []),
            "from_masked": str(row.get("from_masked") or ""),
            "from_sha256": str(row.get("from_sha256") or ""),
            "to_masked": str(row.get("to_masked") or ""),
            "to_sha256": str(row.get("to_sha256") or ""),
            "subject_masked": str(row.get("subject_masked") or ""),
            "body_sha256": str(row.get("body_sha256") or ""),
            "excerpt": str(row.get("excerpt") or ""),
            "attachment_sha256s": list(row.get("attachment_sha256s") or []),
            "folder": str(row.get("folder") or ""),
            "fetched_at": str(row.get("fetched_at") or ""),
        })
    candidates = result.get("candidates") or []
    if not isinstance(candidates, list):
        candidates = []
    run_view = result.get("run")
    if not isinstance(run_view, dict):
        run_view = {}
    return {"project_id": project_id, "capability_id": "mail.receive",
            "status": status,
            "reason": str(result.get("reason") or ""),
            "run": run_view, "messages": public_messages,
            "candidates": candidates,
            "operating_mode": operating_mode,
            "external_sends": 0, "read_only": False}


@app.get('/api/projects/{project_id}/channel-skills/mail.receive/messages')
async def get_project_mail_receive_messages(project_id: str):
    # C3: 保存したメッセージの伏せた表示。他PJの行を返さない。読取失敗は409。
    require_project(project_id)
    import app.channel_skill_mail as _mail
    try:
        rows = await asyncio.to_thread(
            _mail.list_messages, memory.path, project_id)
    except _mail.MailReceiveReadError as exc:
        raise _mail_http_error(exc) from exc
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    public_rows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        public_rows.append({
            "provider_id": str(row.get("provider_id") or ""),
            "message_id": str(row.get("message_id") or ""),
            "in_reply_to": str(row.get("in_reply_to") or ""),
            "references": list(row.get("references") or []),
            "from_masked": str(row.get("from_masked") or ""),
            "from_sha256": str(row.get("from_sha256") or ""),
            "to_masked": str(row.get("to_masked") or ""),
            "to_sha256": str(row.get("to_sha256") or ""),
            "subject_masked": str(row.get("subject_masked") or ""),
            "body_sha256": str(row.get("body_sha256") or ""),
            "excerpt": str(row.get("excerpt") or ""),
            "attachment_sha256s": list(row.get("attachment_sha256s") or []),
            "folder": str(row.get("folder") or ""),
            "fetched_at": str(row.get("fetched_at") or ""),
        })
    return {"project_id": project_id, "capability_id": "mail.receive",
            "messages": public_rows, "read_only": True, "external_sends": 0}


@app.get('/api/projects/{project_id}/channel-skills/mail.receive/mail-reply-candidates')
async def get_project_mail_reply_candidates(project_id: str):
    # C3: 返信の候補(確定にしない)。他PJの行を返さない。読取失敗は409。
    require_project(project_id)
    import app.channel_skill_mail as _mail
    try:
        rows = await asyncio.to_thread(
            _mail.list_reply_candidates, memory.path, project_id)
        confirmations = await asyncio.to_thread(
            _mail.list_candidate_confirmations, memory.path, project_id)
    except _mail.MailReceiveReadError as exc:
        raise _mail_http_error(exc) from exc
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"project_id": project_id, "capability_id": "mail.receive",
            "candidates": rows, "confirmations": confirmations,
            "read_only": True, "external_sends": 0}


@app.post('/api/projects/{project_id}/channel-skills/mail.receive/mail-reply-candidates/{candidate_id}/confirm',
          status_code=201)
async def post_project_mail_reply_candidate_confirm(
        project_id: str, candidate_id: int,
        payload: MailCandidateConfirmPayload):
    # C3: 確認APIは人の担当者・メモ必須の追記型。確定にせず記録だけ残す。
    require_project(project_id)
    import app.channel_skill_mail as _mail
    actor = (payload.actor or "").strip()
    memo = (payload.memo or "").strip()
    if not actor or not memo:
        raise HTTPException(422, "actor and memo are required")
    try:
        target = int(candidate_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise HTTPException(422, "candidate_id is required") from None
    try:
        saved = await asyncio.to_thread(
            _mail.confirm_reply_candidate, memory.path, project_id, int(target),
            actor=actor, memo=memo)
    except ValueError as exc:
        text = str(exc)
        if text == "candidate not found":
            raise HTTPException(404, text) from exc
        raise HTTPException(422, text) from exc
    except _mail.MailReceiveReadError as exc:
        raise _mail_http_error(exc) from exc
    return {"project_id": project_id, "capability_id": "mail.receive",
            "confirmation": saved,
            "auth_note": "確認者名は本人確認を保証しません(認証は未実装)。",
            "read_only": False, "external_sends": 0}


def _social_http_error(exc: BaseException) -> HTTPException:
    import app.channel_skill_social as _social
    if isinstance(exc, _social.SocialObserveReadError):
        return HTTPException(409, detail={
            "code": "SOCIAL_OBSERVE_READ_FAILED",
            "message": "SNS観測の台帳を読めません",
            "failure_kind": exc.kind,
            "path_kind": exc.path_kind,
            "sqlite_error": exc.sqlite_error,
        })
    return HTTPException(409, str(exc))


def _mail_send_http_error(exc: BaseException) -> HTTPException:
    import app.channel_skill_mail_send as _send
    if isinstance(exc, _send.MailSendReadError):
        return HTTPException(409, detail={
            "code": "MAIL_SEND_READ_FAILED",
            "message": "承認済みメール送信の台帳を読めません",
            "failure_kind": exc.kind,
            "path_kind": exc.path_kind,
            "sqlite_error": exc.sqlite_error,
        })
    return HTTPException(409, str(exc))


def _mail_send_refused_http(exc: BaseException) -> HTTPException:
    import app.channel_skill_mail_send as _send
    if isinstance(exc, _send.MailSendNotConfigured):
        return HTTPException(409, detail={
            "code": "MAIL_SEND_NOT_CONFIGURED",
            "message": str(exc.reason or exc),
            "reason": str(exc.reason or "needs_configuration"),
        })
    if isinstance(exc, _send.MailSendRefused):
        reason = str(exc.reason or "")
        if reason in ("action_invalid", "project_invalid"):
            raise HTTPException(422, f"{reason}: {exc.detail}" if exc.detail else reason)
        return HTTPException(409, detail={
            "code": "MAIL_SEND_REFUSED",
            "message": f"{reason}: {exc.detail}" if reason else str(exc),
            "reason": reason,
        })
    if isinstance(exc, ValueError):
        raise HTTPException(422, str(exc))
    return HTTPException(409, str(exc))


class MailSendOutcomePayload(BaseModel):
    outcome: str = Field(min_length=1, max_length=16)
    actor: str = Field(min_length=1, max_length=100)
    memo: str = Field(min_length=1, max_length=2000)


@app.get('/api/projects/{project_id}/channel-skills/mail.send_approved/attempts')
async def get_project_mail_send_attempts(project_id: str, action_id: str = ""):
    # C5: 送信の試行の一覧(読み取り専用・当該PJだけ)。宛先・本文を出さず、
    # ハッシュ・ドメイン・状態だけ返す。読取失敗は409。
    require_project(project_id)
    import app.channel_skill_mail_send as _send
    try:
        rows = await asyncio.to_thread(
            _send.list_attempts, memory.path, project_id,
            str(action_id or "").strip())
        outcomes = await asyncio.to_thread(
            _send.list_outcomes, memory.path, project_id,
            str(action_id or "").strip())
    except _send.MailSendReadError as exc:
        raise _mail_send_http_error(exc) from exc
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"project_id": project_id, "capability_id": "mail.send_approved",
            "attempts": rows, "outcomes": outcomes,
            "read_only": True, "external_sends": 0}


@app.post('/api/projects/{project_id}/actions/{action_id}/send-outcome',
          status_code=201)
async def post_project_mail_send_outcome(project_id: str, action_id: str,
                                        payload: MailSendOutcomePayload):
    # C5: 人が結果不明の送信の結果を記録する(追記型)。担当者・メモ必須。
    # 再送の許可ではない。再送は新しいアクションの承認が必要。
    require_project(project_id)
    import app.channel_skill_mail_send as _send
    outcome = (payload.outcome or "").strip()
    actor = (payload.actor or "").strip()
    memo = (payload.memo or "").strip()
    if outcome not in ("delivered", "not_delivered", "unconfirmed"):
        raise HTTPException(422, "outcome を確認できません")
    if not actor or not memo:
        raise HTTPException(422, "actor and memo are required")
    try:
        saved = await asyncio.to_thread(
            _send.record_send_outcome, memory.path, project_id,
            str(action_id or "").strip(), outcome=outcome,
            actor=actor, memo=memo)
    except (_send.MailSendNotConfigured,
            _send.MailSendRefused,
            ValueError) as exc:
        raise _mail_send_refused_http(exc) from exc
    except _send.MailSendReadError as exc:
        raise _mail_send_http_error(exc) from exc
    except _send.MailSendWriteError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"project_id": project_id, "action_id": str(action_id or ""),
            "outcome": saved,
            "auth_note": "確認者名は本人確認を保証しません(認証は未実装)。",
            "note": "人の判断の記録であり、再送の許可ではない。再送は新しいアクションの承認が必要。",
            "read_only": False, "external_sends": 0}


def _social_refused_http(exc: BaseException) -> HTTPException:
    import app.channel_skill_social as _social
    if isinstance(exc, _social.SocialObserveNotConfigured):
        return HTTPException(409, detail={
            "code": "SOCIAL_OBSERVE_NOT_CONFIGURED",
            "message": str(exc.reason or exc),
            "reason": str(exc.reason or ""),
        })
    if isinstance(exc, _social.SocialObserveRefused):
        reason = str(exc.reason or "")
        if reason in _social.INPUT_422_REASONS:
            raise HTTPException(422, f"{reason}: {exc.detail}" if exc.detail else reason)
        return HTTPException(409, detail={
            "code": "SOCIAL_OBSERVE_REFUSED",
            "message": f"{reason}: {exc.detail}" if reason else str(exc),
            "reason": reason,
        })
    if isinstance(exc, ValueError):
        raise HTTPException(422, str(exc))
    return HTTPException(409, str(exc))


class SocialObserveConfigPayload(BaseModel):
    platform: str = Field(min_length=1, max_length=16)
    public_identity_id: str = Field(min_length=1, max_length=128)
    purpose: str = Field(min_length=1, max_length=2000)
    retention_days: int = Field(default=90, ge=0)
    allowed_viewers: list[str] = Field(min_length=1, max_length=20)
    credential_env_prefix: str = Field(min_length=1, max_length=41)
    actor: str = Field(min_length=1, max_length=100)


class SocialObserveRunPayload(BaseModel):
    platform: str = Field(min_length=1, max_length=16)
    campaign_id: str = Field(min_length=1, max_length=128)
    since: str = Field(min_length=8, max_length=32)
    until: str = Field(min_length=8, max_length=32)
    purpose: str = Field(min_length=1, max_length=2000)


class SocialManualObservationPayload(BaseModel):
    platform: str = Field(min_length=1, max_length=16)
    post_url: str = Field(min_length=8, max_length=2000)
    observed_at: str = Field(min_length=8, max_length=100)
    metrics: dict | None = Field(default=None)
    evidence_note: str = Field(min_length=1, max_length=2000)
    screenshot_sha256: str = Field(default="", max_length=128)
    actor: str = Field(min_length=1, max_length=100)
    memo: str = Field(min_length=1, max_length=2000)


@app.post('/api/projects/{project_id}/channel-skills/social.observe/config',
          status_code=201)
async def post_project_social_observe_config(project_id: str,
                                             payload: SocialObserveConfigPayload):
    # C4: 接続設定の登録は人の明示操作。担当者・目的が必須。資格情報なし。
    require_project(project_id)
    import app.channel_skill_social as _social
    body = {
        "platform": payload.platform.strip().lower(),
        "public_identity_id": payload.public_identity_id.strip(),
        "purpose": payload.purpose.strip(),
        "retention_days": int(payload.retention_days),
        "allowed_viewers": [str(x).strip() for x in (payload.allowed_viewers or [])],
        "credential_env_prefix": payload.credential_env_prefix.strip(),
        "actor": payload.actor.strip(),
    }
    if not isinstance(body["public_identity_id"], str) or not body["public_identity_id"]:
        raise HTTPException(422, "public_identity_id is required")
    if not isinstance(body["purpose"], str) or not body["purpose"]:
        raise HTTPException(422, "purpose is required")
    if not isinstance(body["actor"], str) or not body["actor"]:
        raise HTTPException(422, "actor is required")
    try:
        current = await asyncio.to_thread(
            _social.get_social_settings, memory.path, project_id)
    except _social.SocialObserveReadError as exc:
        raise _social_http_error(exc) from exc
    try:
        saved = await asyncio.to_thread(
            _social.save_social_settings, memory.path, project_id, body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except _social.SocialObserveReadError as exc:
        raise _social_http_error(exc) from exc
    try:
        matured = await asyncio.to_thread(
            _social.social_connection_view, memory.path, project_id)
    except _social.SocialObserveReadError as exc:
        raise _social_http_error(exc) from exc
    return {"project_id": project_id, "capability_id": "social.observe",
            "settings": saved, "connection": {
                "configured": bool(matured.get("configured")),
                "reason": str(matured.get("reason") or ""),
            },
            "auth_note": "確認者名は本人確認を保証しません(認証は未実装)。",
            "read_only": False, "external_sends": 0}


@app.get('/api/projects/{project_id}/channel-skills/social.observe/config')
async def get_project_social_observe_config(project_id: str):
    # C4: 現在と履歴(追記型)。読取不能は『設定なし』に倒さず409。
    require_project(project_id)
    import app.channel_skill_social as _social
    try:
        viewed = await asyncio.to_thread(
            _social.get_social_settings, memory.path, project_id)
        matured = await asyncio.to_thread(
            _social.social_connection_view, memory.path, project_id)
    except _social.SocialObserveReadError as exc:
        raise _social_http_error(exc) from exc
    return {"project_id": project_id, "capability_id": "social.observe",
            "current": viewed.get("current"), "history": viewed.get("history"),
            "status": viewed.get("status"),
            "connection": {
                "configured": bool(matured.get("configured")),
                "reason": str(matured.get("reason") or ""),
            },
            "read_only": True, "external_sends": 0}


@app.post('/api/projects/{project_id}/channel-skills/social.observe/run')
async def post_project_social_observe_run(project_id: str,
                                          payload: SocialObserveRunPayload):
    # C4: 人の明示操作。operating_mode はB3の運用区分から決める。
    # 未設定・読取不能は production 相当の厳しい側。
    require_project(project_id)
    import app.channel_skill_social as _social
    platform = (payload.platform or "").strip().lower()
    campaign_id = (payload.campaign_id or "").strip()
    since = (payload.since or "").strip()
    until = (payload.until or "").strip()
    purpose = (payload.purpose or "").strip()
    if not platform:
        raise HTTPException(422, "platform is required")
    if not campaign_id:
        raise HTTPException(422, "campaign_id is required")
    if not since or not until:
        raise HTTPException(422, "since and until are required")
    if not purpose:
        raise HTTPException(422, "purpose is required")
    try:
        from app.contact_registry import current_operating_mode
        current = current_operating_mode(memory.path, project_id)
        mode = str((current or {}).get("mode") or "") if isinstance(current, dict) else ""
    except Exception:
        mode = ""
    operating_mode = mode if mode in ("demo_test", "production") else "production"
    try:
        current_settings = await asyncio.to_thread(
            _social.get_current_settings, memory.path, project_id)
    except _social.SocialObserveReadError as exc:
        raise _social_http_error(exc) from exc
    if not isinstance(current_settings, dict) or not current_settings:
        raise HTTPException(409, detail={
            "code": "SOCIAL_OBSERVE_NOT_CONFIGURED",
            "message": "SNS観測の接続設定がありません",
        })
    # アダプタは本番の保持先から作る。試験はその保持先を偽に差し替える。
    try:
        adapter = await asyncio.to_thread(
            _social.build_social_adapter, dict(current_settings))
    except _social.SocialObserveNotConfigured as exc:
        raise HTTPException(409, detail={
            "code": "SOCIAL_OBSERVE_NOT_CONFIGURED",
            "message": str(exc.reason or exc),
        }) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(409, detail={
            "code": "SOCIAL_OBSERVE_NOT_CONFIGURED",
            "message": "SNS観測の接続設定を確認できません",
            "error_type": type(exc).__name__,
        }) from exc
    try:
        result = await asyncio.to_thread(
            _social.run_social_observe, memory.path, project_id,
            platform=platform, campaign_id=campaign_id,
            since=since, until=until, purpose=purpose,
            operating_mode=operating_mode, adapter=adapter)
    except (_social.SocialObserveNotConfigured,
            _social.SocialObserveRefused,
            ValueError) as exc:
        raise _social_refused_http(exc) from exc
    status = str(result.get("status") or "unknown")
    evidence = result.get("evidence") or []
    if not isinstance(evidence, list):
        evidence = []
    observations = result.get("observations") or []
    if not isinstance(observations, list):
        observations = []
    run_view = result.get("run")
    if not isinstance(run_view, dict):
        run_view = {}
    return {"project_id": project_id, "capability_id": "social.observe",
            "status": status,
            "reason": str(result.get("reason") or ""),
            "run": run_view, "evidence": evidence,
            "observations": observations,
            "account_mismatch": int(result.get("account_mismatch") or 0),
            "mismatch_details": list(result.get("mismatch_details") or []),
            "next_actions": list(result.get("next_actions") or []),
            "operating_mode": operating_mode,
            "external_sends": 0, "read_only": False}


@app.get('/api/projects/{project_id}/channel-skills/social.observe/observations')
async def get_project_social_observe_observations(project_id: str):
    # C4: 保存した観測の伏せた表示。他PJの行を返さない。読取失敗は409。
    require_project(project_id)
    import app.channel_skill_social as _social
    try:
        rows = await asyncio.to_thread(
            _social.list_observations, memory.path, project_id)
    except _social.SocialObserveReadError as exc:
        raise _social_http_error(exc) from exc
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"project_id": project_id, "capability_id": "social.observe",
            "observations": rows, "read_only": True, "external_sends": 0}


@app.post('/api/projects/{project_id}/channel-skills/social.observe/manual-observation',
          status_code=201)
async def post_project_social_observe_manual_observation(
        project_id: str, payload: SocialManualObservationPayload):
    # C4: 人手の観測の登録。担当者・メモ必須。自動の観測と区別する。
    require_project(project_id)
    import app.channel_skill_social as _social
    try:
        saved = await asyncio.to_thread(
            _social.record_manual_observation, memory.path, project_id,
            platform=(payload.platform or "").strip().lower(),
            post_url=(payload.post_url or "").strip(),
            observed_at=(payload.observed_at or "").strip(),
            metrics=payload.metrics,
            evidence_note=(payload.evidence_note or "").strip(),
            screenshot_sha256=(payload.screenshot_sha256 or "").strip(),
            actor=(payload.actor or "").strip(),
            memo=(payload.memo or "").strip())
    except (_social.SocialObserveNotConfigured,
            _social.SocialObserveRefused,
            ValueError) as exc:
        raise _social_refused_http(exc) from exc
    except _social.SocialObserveReadError as exc:
        raise _social_http_error(exc) from exc
    return {"project_id": project_id, "capability_id": "social.observe",
            "observation": saved,
            "auth_note": "確認者名は本人確認を保証しません(認証は未実装)。",
            "read_only": False, "external_sends": 0}
