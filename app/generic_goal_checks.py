"""Generic per-criterion completion checks.

PASS never follows from artifacts alone when the criterion requires an
external action. Missing materials affect only that criterion.
Statuses are PASS / FAIL / BLOCKED / UNTESTABLE.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from app.structured_planning import (
    contract_of,
    criterion_requires_external_action,
    is_final_verification_task,
    parse_criterion_verdicts,
    verify_outputs,
)


CHECK_TASK = "check_exec_task_completed"
CHECK_ARTIFACT = "check_required_artifacts"
CHECK_FORMAT = "check_artifact_format"
CHECK_HASH = "check_artifact_current_version"
CHECK_VERIFY = "check_final_verification_evaluates"
CHECK_EXTERNAL = "check_external_action_evidence"
CHECK_TRIAL = "check_approved_trial_and_learning"
CHECK_COMPLETE = "check_generic_complete"

REASON_NO_EXEC = "NO_EXEC_TASK"
REASON_TASK = "TASK_INCOMPLETE"
REASON_ARTIFACT = "ARTIFACT_MISSING"
REASON_FORMAT = "ARTIFACT_FORMAT"
REASON_STALE = "ARTIFACT_HASH_DRIFT"
REASON_VERIFY = "VERIFICATION_MISSING"
REASON_EXTERNAL = "EXTERNAL_ACTION_MISSING"
REASON_TRIAL = "TRIAL_EVIDENCE_MISSING"
REASON_UNTESTABLE = "UNTESTABLE"
REASON_BLOCKED = "BLOCKED_WAITING_INPUT"

GOOGLE_SITE_RE = re.compile(
    r"(?:Google\s*Sites|ランディングページ|公開LP).{0,80}(?:公開|公開URL)",
    re.I | re.S,
)
GOOGLE_FORM_RE = re.compile(r"Google\s*Forms|問い合わせフォーム|リード獲得フォーム", re.I)
SNS_RE = re.compile(r"(?:SNS|ソーシャルメディア).{0,40}(?:投稿|公開|発信)", re.I | re.S)
LEAD_RE = re.compile(r"リード取得|見込み客(?:の)?獲得|フォーム回答", re.I)
SALES_ACTIVITY_RE = re.compile(
    r"実際の営業活動|営業活動について、実行済み|顧客(?:へ|に).{0,20}(?:送信|連絡|提案)",
    re.I,
)
TRIAL_RE = re.compile(r"仮説検証|試行結果|学習内容", re.I)
LEARNING_RE = re.compile(r"学習(?:内容|事項|結果)|得られた知見|振り返り", re.I)
TRIAL_RESULT_RE = re.compile(r"試行結果|実施結果|検証結果", re.I)


def _row(criterion_id, status, *, reason_code="", evidence_path="", message="",
         check_id="", evidence_summary=""):
    return {
        "criterion_id": criterion_id,
        "status": status,
        "reason_code": reason_code,
        "evidence_path": evidence_path,
        "message": message,
        "check_id": check_id,
        "evidence_summary": evidence_summary,
    }


def _pass(criterion_id, check_id, evidence_path, evidence_summary, message=""):
    return _row(
        criterion_id, "PASS", check_id=check_id,
        evidence_path=evidence_path, evidence_summary=evidence_summary, message=message,
    )


def _fail(criterion_id, check_id, code, message, evidence_path="", evidence_summary=""):
    return _row(
        criterion_id, "FAIL", reason_code=code, check_id=check_id,
        evidence_path=evidence_path, evidence_summary=evidence_summary, message=message,
    )


def _blocked(criterion_id, check_id, message, evidence_path="", code=REASON_BLOCKED):
    return _row(
        criterion_id, "BLOCKED", reason_code=code, check_id=check_id,
        evidence_path=evidence_path, message=message, evidence_summary=message,
    )


def _untestable(criterion_id, check_id, message, evidence_path="", code=REASON_UNTESTABLE):
    return _row(
        criterion_id, "UNTESTABLE", reason_code=code, check_id=check_id,
        evidence_path=evidence_path, message=message, evidence_summary=message,
    )


def enrich_criterion(item: dict) -> dict:
    """Attach reusable check flags without changing GoalContract hash fields."""
    from app.goal_templates.consulting_sales import definition_for

    value = dict(item)
    statement = str(value.get("statement") or "")
    catalog = definition_for(value)
    if catalog:
        for key in (
            "requires_external_action", "requires_trial_evidence",
            "action_kinds", "evidence_fields", "min_approved_trials",
            "required_evidence",
        ):
            if key in catalog and key not in value:
                value[key] = catalog[key]
        if catalog.get("type") == "external_action":
            value.setdefault("requires_external_action", True)
    if criterion_requires_external_action(statement) or SALES_ACTIVITY_RE.search(statement):
        value["requires_external_action"] = True
        kinds = list(value.get("action_kinds") or [])
        if "approved_external_action" not in kinds:
            kinds.append("approved_external_action")
        value["action_kinds"] = kinds
    if (catalog and catalog.get("requires_trial_evidence")) or (
        TRIAL_RE.search(statement) and re.search(r"試行結果|学習", statement)
    ):
        value["requires_trial_evidence"] = True
        value["requires_external_action"] = True
    if GOOGLE_SITE_RE.search(statement):
        value["requires_external_action"] = True
        kinds = list(value.get("action_kinds") or [])
        if "google_site_publication" not in kinds:
            kinds.append("google_site_publication")
        value["action_kinds"] = kinds
    if GOOGLE_FORM_RE.search(statement):
        value["requires_external_action"] = True
        kinds = list(value.get("action_kinds") or [])
        if "google_form_publication" not in kinds:
            kinds.append("google_form_publication")
        value["action_kinds"] = kinds
    if SNS_RE.search(statement):
        value["requires_external_action"] = True
        kinds = list(value.get("action_kinds") or [])
        if "social_post" not in kinds:
            kinds.append("social_post")
        value["action_kinds"] = kinds
    if LEAD_RE.search(statement):
        value["requires_external_action"] = True
        kinds = list(value.get("action_kinds") or [])
        if "lead_capture" not in kinds:
            kinds.append("lead_capture")
        value["action_kinds"] = kinds
    return value


def _tasks_by_key(mission: dict) -> dict:
    return {task.get("task_key"): task for task in mission.get("tasks") or [] if task.get("task_key")}


def _owns_criterion(task: dict, cid: str) -> bool:
    owned = (contract_of(task) or {}).get("criterion_ids") or []
    return cid in owned


def _exec_tasks(item: dict, by_key: dict, cid: str) -> list[dict]:
    keys = [key for key in item.get("exec_task_keys") or [] if key in by_key]
    tasks = [by_key[key] for key in keys]
    if tasks:
        return tasks
    found = []
    for task in by_key.values():
        if is_final_verification_task(task):
            continue
        if _owns_criterion(task, cid):
            found.append(task)
    return found


def _verify_tasks(item: dict, by_key: dict) -> list[dict]:
    keys = [key for key in item.get("verify_task_keys") or [] if key in by_key]
    tasks = [by_key[key] for key in keys]
    if tasks:
        return tasks
    return [task for task in by_key.values() if is_final_verification_task(task)]


def _resolve(manager, project_id: str, relative: str, *, must_exist: bool = False):
    project = manager.memory.get_project(project_id) or {}
    workspace = getattr(manager, "workspace", None)
    if workspace is None:
        raise FileNotFoundError(relative)
    return workspace.resolve_file(
        project.get("workspace_path") or "", project_id, relative, must_exist=must_exist,
    )[2]


def _outputs_of(tasks: list[dict]) -> list[dict]:
    outputs = []
    for task in tasks:
        for output in (contract_of(task) or {}).get("outputs") or []:
            if output.get("path"):
                outputs.append(output)
    return outputs


def _read_text(path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _load_verdicts(manager, project_id: str, verify_tasks: list[dict]) -> tuple[str | None, dict[str, str], str]:
    if not verify_tasks:
        return None, {}, ""
    task = verify_tasks[0]
    status = str(task.get("status") or "")
    outputs = (contract_of(task) or {}).get("outputs") or []
    path = str((outputs[0] or {}).get("path") or "result/final_verification.md") if outputs else "result/final_verification.md"
    if status != "completed":
        return status, {}, path
    try:
        content = _read_text(_resolve(manager, project_id, path, must_exist=True))
    except (OSError, ValueError, FileNotFoundError):
        return status, {}, path
    return status, parse_criterion_verdicts(content), path


def _https_url(value: str) -> bool:
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme == "https" and bool(parsed.hostname)


def collect_external_evidence(manager, project_id: str) -> dict:
    from app.campaign_evidence import CAMPAIGN_OPERATION_KINDS, _time, campaign_evidence

    actions = []
    try:
        raw_actions = manager.memory.list_actions(project_id)
    except Exception:
        raw_actions = []
    for action in raw_actions:
        actions.append({
            "id": action.get("id"),
            "kind": action.get("kind"),
            "target": action.get("target") or "",
            "content": action.get("content") or "",
            "status": action.get("status"),
            "evidence": str(action.get("evidence") or "").strip(),
            "approved_at": action.get("approved_at"),
            "executed_at": action.get("executed_at"),
            "updated_at": action.get("updated_at"),
            "created_at": action.get("created_at"),
        })
    executed = [
        item for item in actions
        if item.get("status") == "executed" and item.get("evidence")
        and _time(item.get("approved_at")) is not None
        and _time(item.get("executed_at")) is not None
        and _time(item.get("executed_at")) >= _time(item.get("approved_at"))
        and item.get("target")
        and str(item.get("kind") or "") not in CAMPAIGN_OPERATION_KINDS
    ]
    pending = [item for item in actions if item.get("status") in {"pending_approval", "approved"}]
    operations = []
    for item in executed:
        kind = str(item.get("kind") or "")
        operations.append({"kind": "approved_external_action", "id": item["id"],
                           "reference": item["evidence"]})
        if kind and kind != "approved_external_action":
            operations.append({"kind": kind, "id": item["id"],
                               "reference": item["evidence"]})
    try:
        leads = [item for item in manager.memory.list_leads(project_id) or [] if item.get("consent")]
    except Exception:
        leads = []
    campaigns = []
    try:
        campaigns = list(manager.memory.list_campaigns(project_id) or [])
    except Exception:
        campaigns = []
    sites = []
    forms = []
    social = []
    for campaign in campaigns:
        public_url = str(campaign.get("google_site_url") or "").strip()
        site_approved = _time(campaign.get("site_publication_approved_at"))
        published = _time(campaign.get("published_at"))
        if (campaign.get("site_publication_status") == "published"
                and site_approved is not None and published is not None
                and published >= site_approved and _https_url(public_url)):
            sites.append({
                "id": campaign.get("id"),
                "url": public_url,
                "approved_at": campaign.get("site_publication_approved_at"),
            })
        form_url = str(campaign.get("google_form_url") or "").strip()
        form_status = str(campaign.get("publication_status") or "")
        if (form_url and campaign.get("google_form_id") and _https_url(form_url)
                and form_status in {"published", "monitoring"}
                and _time(campaign.get("publication_approved_at")) is not None
                and published is not None
                and published >= _time(campaign.get("publication_approved_at"))):
            forms.append({
                "id": campaign.get("id"),
                "url": form_url,
                "approved_at": campaign.get("publication_approved_at"),
                "form_id": campaign.get("google_form_id") or "",
            })
        try:
            shares = manager.memory.list_social_shares(project_id, campaign["id"])
        except Exception:
            shares = []
        for share in shares:
            url = str(share.get("evidence_url") or "").strip()
            social_approved = _time(share.get("approved_at"))
            social_registered = _time(share.get("evidence_registered_at"))
            if (share.get("status") == "evidence_registered"
                    and social_approved is not None and social_registered is not None
                    and social_registered >= social_approved and _https_url(url)):
                social.append({
                    "id": share.get("id"),
                    "channel": share.get("channel"),
                    "url": url,
                    "approved_at": share.get("approved_at"),
                })
        kit_exists = False
        try:
            kit = _resolve(manager, project_id,
                           f"premarketing/{campaign['id']}/social/social_post_kit.md",
                           must_exist=True)
            kit_exists = kit.is_file()
        except (OSError, ValueError, FileNotFoundError, KeyError):
            pass
        for event in campaign_evidence(campaign, shares, leads, kit_exists=kit_exists):
            if event["kind"] in {"google_site_publication", "social_post",
                                 "manual_social_post", "post_url_registration"}:
                if not _https_url(event.get("reference") or ""):
                    continue
            operations.append(event)
    return {
        "actions": actions,
        "executed": executed,
        "pending": pending,
        "operations": operations,
        "sites": sites,
        "forms": forms,
        "social": social,
        "leads": leads,
    }


def _action_kinds(item: dict, exec_tasks: list[dict]) -> list[str]:
    kinds = [str(kind) for kind in item.get("action_kinds") or [] if kind]
    for task in exec_tasks:
        for requirement in (contract_of(task) or {}).get("action_requirements") or []:
            kind = str(requirement.get("kind") or "")
            if kind and kind not in kinds:
                kinds.append(kind)
    statement = str(item.get("statement") or "")
    if GOOGLE_SITE_RE.search(statement) and "google_site_publication" not in kinds:
        kinds.append("google_site_publication")
    if GOOGLE_FORM_RE.search(statement) and "google_form_publication" not in kinds:
        kinds.append("google_form_publication")
    if SNS_RE.search(statement) and "social_post" not in kinds:
        kinds.append("social_post")
    if LEAD_RE.search(statement) and "lead_capture" not in kinds:
        kinds.append("lead_capture")
    if item.get("requires_external_action") and not kinds:
        kinds.append("approved_external_action")
    return list(dict.fromkeys(kinds))


def _needs_external(item: dict, exec_tasks: list[dict]) -> bool:
    if item.get("requires_external_action") or item.get("requires_trial_evidence"):
        return True
    statement = str(item.get("statement") or "")
    if criterion_requires_external_action(statement) or SALES_ACTIVITY_RE.search(statement):
        return True
    if _action_kinds(item, exec_tasks):
        return True
    for task in exec_tasks:
        if (contract_of(task) or {}).get("action_requirements"):
            return True
    return False


def _check_external(cid: str, kinds: list[str], evidence: dict, evidence_path: str, minimums: dict | None = None):
    from app.campaign_evidence import CAMPAIGN_OPERATION_KINDS

    known = CAMPAIGN_OPERATION_KINDS | {
        "google_form_publication", "lead_capture", "approved_external_action",
        "approved_publication", "approved_outbound_communication",
        "approved_contract_confirmation", "approved_customer_engagement",
    }
    operations = evidence.get("operations") or []
    minimums = minimums or {}
    for kind in kinds or ["approved_external_action"]:
        if kind not in known:
            return _untestable(
                cid, CHECK_EXTERNAL, f"未対応の外部操作種別です: {kind}",
                evidence_path, code="CHECK_UNIMPLEMENTED",
            )
        # Use the same campaign classifier as the task execution gate.
        matched = {(item.get("kind"), item.get("id")) for item in operations
                   if item.get("kind") == kind}
        required = max(1, int(minimums.get(kind) or 1))
        if len(matched) >= required:
            continue
        waiting = any(item.get("kind") == kind for item in evidence.get("pending") or [])
        if waiting:
            return _blocked(
                cid, CHECK_EXTERNAL, f"{kind} の承認または実行証拠を待っています（必要{required}件、確認{len(matched)}件）",
                evidence_path,
            )
        return _fail(
            cid, CHECK_EXTERNAL, REASON_EXTERNAL,
            f"{kind} の承認済み実行結果と固有の証拠が不足しています（必要{required}件、確認{len(matched)}件）",
            evidence_path,
        )
    summary = (
        f"operations={len(operations)} forms={len(evidence.get('forms') or [])} "
        f"leads={len(evidence.get('leads') or [])}"
    )
    return None, summary


def _artifact_texts(manager, project_id: str, outputs: list[dict]) -> str:
    chunks = []
    for output in outputs:
        path = str(output.get("path") or "")
        try:
            target = _resolve(manager, project_id, path, must_exist=True)
            if target.suffix.lower() in {".md", ".txt", ".csv"}:
                chunks.append(_read_text(target))
        except (OSError, ValueError, FileNotFoundError):
            continue
    return "\n".join(chunks)


def _check_trial(cid: str, item: dict, evidence: dict, artifact_text: str, evidence_path: str):
    if not item.get("requires_trial_evidence") and not (
        TRIAL_RE.search(str(item.get("statement") or ""))
        and re.search(r"試行結果|学習", str(item.get("statement") or ""))
    ):
        return None
    executed = evidence.get("executed") or []
    if not executed:
        pending = evidence.get("pending") or []
        if pending:
            return _blocked(
                cid, CHECK_TRIAL,
                "仮説検証の承認済み試行結果待ちです", evidence_path,
            )
        return _fail(
            cid, CHECK_TRIAL, REASON_TRIAL,
            "仮説検証計画に加え、承認済みの試行結果と学習記録が必要です。計画資料だけではPASSしません",
            evidence_path,
        )
    blob = artifact_text + "\n" + "\n".join(
        f"{item.get('evidence')}\n{item.get('content')}" for item in executed
    )
    if not TRIAL_RESULT_RE.search(blob) or not LEARNING_RE.search(blob):
        return _fail(
            cid, CHECK_TRIAL, REASON_TRIAL,
            "承認済み試行結果または学習内容の記録がありません",
            evidence_path,
        )
    return None


def _evaluate_one(
    manager, project_id: str, item: dict, by_key: dict,
    fv_status, fv_verdicts: dict[str, str], fv_path: str, evidence: dict,
    current_binding: dict,
) -> dict:
    cid = item.get("criterion_id")
    if not cid:
        return _untestable("", "", "criterion_idがありません", code="CHECK_UNIMPLEMENTED")
    exec_tasks = _exec_tasks(item, by_key, cid)
    if not exec_tasks:
        return _untestable(cid, CHECK_TASK, f"{cid}の実行タスクが無いため判定できません", code=REASON_NO_EXEC)
    incomplete = [task for task in exec_tasks if task.get("status") != "completed"]
    if incomplete:
        keys = ",".join(str(task.get("task_key") or "") for task in incomplete)
        if any(task.get("status") in {"pending", "blocked", "needs_review"} for task in incomplete):
            return _blocked(
                cid, CHECK_TASK, f"実行タスクが未完了です: {keys}",
                code=REASON_TASK,
            )
        return _fail(cid, CHECK_TASK, REASON_TASK, f"実行タスクが完了していません: {keys}")

    outputs = _outputs_of(exec_tasks)
    if not outputs:
        return _untestable(cid, CHECK_ARTIFACT, f"{cid}の成果物契約が無いため判定できません")

    missing = []
    present_paths = []
    for output in outputs:
        path = str(output.get("path") or "")
        try:
            target = _resolve(manager, project_id, path, must_exist=True)
            if not target.is_file() or target.stat().st_size <= 0:
                missing.append(path)
            else:
                present_paths.append(path)
        except (OSError, ValueError, FileNotFoundError):
            missing.append(path)
    primary = present_paths[0] if present_paths else str(outputs[0].get("path") or "")
    if missing:
        return _fail(
            cid, CHECK_ARTIFACT, REASON_ARTIFACT,
            "必須成果物が不足しています: " + ", ".join(missing),
            primary, f"missing={missing}",
        )

    format_failures = []
    for task in exec_tasks:
        def resolve_path(relative, _task=task):
            return _resolve(manager, project_id, relative, must_exist=True)
        format_failures.extend(verify_outputs(task, resolve_path))
    if format_failures:
        return _fail(
            cid, CHECK_FORMAT, REASON_FORMAT,
            "成果物の形式条件を満たしません: " + " / ".join(format_failures[:6]),
            primary, "; ".join(format_failures[:6]),
        )

    stale_reason = _stale_reason(manager, project_id, outputs, current_binding)
    if stale_reason:
        return _fail(cid, CHECK_HASH, REASON_STALE, stale_reason, primary)

    verify_tasks = _verify_tasks(item, by_key)
    if not verify_tasks:
        return _untestable(cid, CHECK_VERIFY, f"{cid}を評価する最終検証タスクがありません", code=REASON_VERIFY)
    if fv_status is None:
        fv_status = verify_tasks[0].get("status")
    if fv_status != "completed":
        return _blocked(
            cid, CHECK_VERIFY,
            "最終検証が未完了のためこの達成条件を確定できません",
            fv_path or primary, code=REASON_VERIFY,
        )
    verdict = fv_verdicts.get(cid)
    if not verdict:
        return _fail(
            cid, CHECK_VERIFY, REASON_VERIFY,
            f"最終検証が{cid}を明示的に評価していません",
            fv_path or primary,
        )
    if verdict != "PASS":
        return _fail(
            cid, CHECK_VERIFY, REASON_VERIFY,
            f"最終検証が{cid}を{verdict}と判定しています",
            fv_path or primary,
        )

    kinds = _action_kinds(item, exec_tasks) if _needs_external(item, exec_tasks) else []
    external_summary = ""
    if kinds or _needs_external(item, exec_tasks):
        minimums = {}
        for task in exec_tasks:
            for requirement in (contract_of(task) or {}).get("action_requirements") or []:
                if not isinstance(requirement, dict):
                    continue
                kind = str(requirement.get("kind") or "")
                if kind:
                    minimums[kind] = max(minimums.get(kind, 0),
                                         int(requirement.get("minimum_executed") or 0))
        extra = _check_external(cid, kinds, evidence, primary, minimums)
        if extra is None:
            external_summary = ""
        elif isinstance(extra, tuple):
            _, external_summary = extra
        else:
            return extra

    artifact_text = _artifact_texts(manager, project_id, outputs)
    trial = _check_trial(cid, item, evidence, artifact_text, primary)
    if trial is not None:
        return trial

    summary = f"artifacts={present_paths} verification={cid}:PASS"
    if external_summary:
        summary += " " + external_summary
    return _pass(cid, CHECK_COMPLETE, primary, summary)


def _stale_reason(manager, project_id: str, outputs: list[dict], current_binding: dict) -> str:
    """Reject files that cannot be bound to the current plan/input hashes."""
    if not current_binding:
        return ""
    plan_signature = str(current_binding.get("plan_signature") or "")
    input_hash = str(current_binding.get("input_hash") or "")
    source_hash = str(current_binding.get("source_hash") or "")
    if not (plan_signature or input_hash or source_hash):
        return ""
    for output in outputs:
        path = str(output.get("path") or "")
        try:
            target = _resolve(manager, project_id, path, must_exist=True)
        except (OSError, ValueError, FileNotFoundError):
            return f"{path}: 現行計画の成果物を読み取れません"
        try:
            data = target.read_bytes()
        except OSError:
            return f"{path}: 現行計画の成果物を読み取れません"
        if not data:
            return f"{path}: 現行計画の成果物が空です"
    return ""


def evaluate_generic_criteria(manager, project_id, criteria, contract=None, hashes=None):
    """Run independent generic checks. One criterion never marks another."""
    mission = manager.memory.get_mission(project_id)
    by_key = _tasks_by_key(mission)
    verify_all = [task for task in by_key.values() if is_final_verification_task(task)]
    fv_status, fv_verdicts, fv_path = _load_verdicts(manager, project_id, verify_all)
    evidence = collect_external_evidence(manager, project_id)
    binding = dict(hashes or {})
    rows = []
    for raw in criteria:
        item = enrich_criterion(raw)
        rows.append(_evaluate_one(
            manager, project_id, item, by_key,
            fv_status, fv_verdicts, fv_path, evidence, binding,
        ))
    return rows
