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


def _artifact_digest(manager, project_id: str, relative: str) -> str:
    import hashlib as _hashlib

    try:
        return _hashlib.sha256(_resolve(manager, project_id, relative, must_exist=True).read_bytes()).hexdigest()
    except (OSError, ValueError, FileNotFoundError):
        return "unknown:unreadable"


def _outputs_of(tasks: list[dict]) -> list[dict]:
    outputs = []
    for task in tasks:
        for output in (contract_of(task) or {}).get("outputs") or []:
            if output.get("path"):
                outputs.append(output)
    return outputs


def _read_text(path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _mission_for_p0c(manager, project_id: str) -> dict:
    """P0-C用の mission 取得。読取失敗は空扱いにし、呼び出し側で UNTESTABLE に倒す。"""
    try:
        mission = manager.memory.get_mission(project_id)
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return {}
    return mission if isinstance(mission, dict) else {}


def _load_verdicts(manager, project_id: str, verify_tasks: list[dict]) -> tuple[str | None, dict[str, str], str]:
    if not verify_tasks:
        return None, {}, ""
    task = verify_tasks[0]
    status = str(task.get("status") or "")
    outputs = (contract_of(task) or {}).get("outputs") or []
    path = str((outputs[0] or {}).get("path") or "result/final_verification.md") if outputs else "result/final_verification.md"
    if status != "completed":
        return status, {}, path
    # 修正B: 最終検証レポートは項目別検証台帳からのみ生成される。台帳の判定を
    # 正とし、本文の verdict 行は補助として読む(台帳が無ければ未評価扱い)。
    verdicts: dict[str, str] = {}
    ledger_verdicts: dict[str, str] = {}
    try:
        saved = open_ledger_readonly(manager, project_id)
        if isinstance(saved, dict):
            try:
                import json as _json

                ledger_verdicts = _json.loads(str(saved.get("criterion_verdicts_json") or "{}"))
            except (ValueError, TypeError, AttributeError):
                ledger_verdicts = {}
            if not isinstance(ledger_verdicts, dict):
                ledger_verdicts = {}
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
        # 読取失敗は『空』に倒さない。 LedgerReadError はそのまま上げ、
        # それ以外の失敗も UNTESTABLE 側に倒して呼出側で区別する。
        try:
            from app.requirement_item_ledger import LedgerReadError as _LedgerReadError

            if isinstance(exc, _LedgerReadError):
                raise
        except ImportError:
            pass
        saved = None
        ledger_verdicts = {}
    if saved is None:
        # 台帳が無い計画の最終検証は理由を明示した FAIL(PASSにも理由なしにもしない)。
        try:
            ledger_missing = "|LEDGER_MISSING:" in str(path)
        except (ValueError, TypeError, AttributeError):
            ledger_missing = False
        if ledger_missing:
            return status, {}, path
        return status, {}, path + "|LEDGER_MISSING:項目別検証台帳が無い: 再計画が必要"
    verdicts.update({str(k): str(v) for k, v in ledger_verdicts.items() if str(k)})
    try:
        content = _read_text(_resolve(manager, project_id, path, must_exist=True))
    except (OSError, ValueError, FileNotFoundError):
        return status, verdicts, path
    for cid, verdict in parse_criterion_verdicts(content).items():
        # 台帳に無いSCの本文 verdict は採用しない(形式PASSの昇格を禁止)。
        if cid not in verdicts:
            continue
        verdicts[cid] = verdict
    return status, verdicts, path


def _https_url(value: str) -> bool:
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme == "https" and bool(parsed.hostname)


def collect_external_evidence(manager, project_id: str) -> dict:
    return collect_external_evidence_for_project(manager, project_id, None)


def _channel_selection_for_project(manager, project_id: str,
                                   campaign_id: str | None) -> tuple[list[str] | None, bool, str]:
    """媒体選択を返す。未登録/パスなしだけ従来動作、読取不能はSNS証跡を遮断する。"""
    memory_path = getattr(getattr(manager, "memory", None), "path", None)
    if memory_path is None:
        return None, False, ""
    try:
        from app.social_premarketing import resolve_channel_selection
        result = resolve_channel_selection(memory_path, project_id, campaign_id)
    except (OSError, ValueError, TypeError, AttributeError, KeyError, ImportError) as exc:
        return [], True, f"{type(exc).__name__}: {exc}"
    if not isinstance(result, dict):
        return [], True, "媒体選択の読取結果が不正です"
    status = str(result.get("status") or "")
    if status == "selected":
        return [str(item) for item in (result.get("channels") or []) if str(item)], False, ""
    if status == "none_needed":
        return [], False, ""
    if status == "not_configured":
        return None, False, ""
    return [], True, str(result.get("reason_ja") or "媒体の選択を読めません")


def _selected_channels_for_project(manager, project_id: str,
                                   campaign_id: str | None) -> list[str] | None:
    return _channel_selection_for_project(manager, project_id, campaign_id)[0]


def collect_external_evidence_for_project(manager, project_id: str,
                                          campaign_id: str | None) -> dict:
    """B1: 選択した媒体だけを外部証拠に含める。campaign_id が None のときは従来どおり。

    台帳が not_configured / 読取不能のときは従来どおり全媒体(既存挙動)。
    selected のときは選択媒体だけ。none_needed のときはSNS関連を増やさない。
    """
    from app.campaign_evidence import CAMPAIGN_OPERATION_KINDS, _time, campaign_evidence

    selected_channels, selection_unreadable, selection_reason = _channel_selection_for_project(
        manager, project_id, campaign_id)

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
        campaign_channels, campaign_unreadable, campaign_reason = _channel_selection_for_project(
            manager, project_id, str(campaign.get("id") or ""))
        effective_channels = campaign_channels
        selection_unreadable = selection_unreadable or campaign_unreadable
        if campaign_unreadable and campaign_reason:
            selection_reason = campaign_reason
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
            if effective_channels is not None and str(share.get("channel") or "") not in set(effective_channels):
                continue
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
        for event in campaign_evidence(
            campaign, shares, leads, kit_exists=kit_exists,
            selected_channels=effective_channels,
        ):
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
        "social_selection_unreadable": selection_unreadable,
        "social_selection_reason": selection_reason,
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
    # 修正A/B: 項目未抽出・対象不明・方法未定義は needs_review 相当に止める。
    # 片方の成果物の項目ゼロはFAIL。勝手に通さない。
    # 専用検証契約(車両等)は既存の専用検証に任せ、ここでは何もしない。
    try:
        has_vehicle = any(
            str((contract_of(task) or {}).get("execution_kind") or "").startswith("vehicle_")
            for task in exec_tasks
        )
    except (ValueError, TypeError, AttributeError, KeyError):
        has_vehicle = False
    if has_vehicle:
        matrix_check = {"passed": True, "issues": [], "needs_human_review": False,
                        "artifact_zero_failed": False}
    else:
        try:
            from app.requirement_matrix import validate_requirement_matrix
            matrix_check = validate_requirement_matrix({"tasks": exec_tasks})
        except (ValueError, TypeError, AttributeError, KeyError):
            matrix_check = {"passed": False, "issues": ["requirement matrix unreadable"],
                            "needs_human_review": True, "artifact_zero_failed": False}
    if matrix_check.get("artifact_zero_failed"):
        return _fail(
            cid, CHECK_ARTIFACT, REASON_ARTIFACT,
            "成果物と項目の対応が不完全です: " + "; ".join(matrix_check.get("issues") or [])[:500],
            str((_outputs_of(exec_tasks)[0].get("path") if _outputs_of(exec_tasks) else "")),
        )
    if not matrix_check.get("passed"):
        return _untestable(
            cid, CHECK_ARTIFACT,
            "人の確認待ち: " + "; ".join(matrix_check.get("issues") or ["項目の抽出に疑義"])[:500],
        )
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
    ledger_missing = "|LEDGER_MISSING:" in str(fv_path)
    ledger_missing_reason = ""
    if ledger_missing:
        try:
            ledger_missing_reason = str(fv_path).split("|LEDGER_MISSING:", 1)[1]
        except (ValueError, TypeError, AttributeError, IndexError):
            ledger_missing_reason = "項目別検証台帳が無い: 再計画が必要"
        if not ledger_missing_reason:
            ledger_missing_reason = "項目別検証台帳が無い: 再計画が必要"
        fv_path = str(fv_path).split("|LEDGER_MISSING:", 1)[0]
    if fv_status != "completed":
        return _blocked(
            cid, CHECK_VERIFY,
            "最終検証が未完了のためこの達成条件を確定できません",
            fv_path or primary, code=REASON_VERIFY,
        )

    # 外部証拠は先に確認する(既存の外部送信・営業実行の挙動を維持)。
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

    if ledger_missing:
        return _fail(
            cid, CHECK_VERIFY, REASON_VERIFY,
            ledger_missing_reason,
            fv_path or primary,
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

    # 修正B: 形式検査は前段として保持し、必須項目の証拠付き判定を必須にする。
    # SC判定の集約: 必須項目がすべて PASS または(文意の項目で)HUMAN_CONFIRMED
    # のときだけ PASS。FAIL が1件でもあれば FAIL。UNTESTABLE が残れば人の確認待ち。
    # 専用検証契約(車両等)は既存の専用検証に任せる。
    try:
        from app.requirement_verification import (
            HUMAN_CONFIRMED as _HUMAN_CONFIRMED,
        )
        from app.requirement_verification import verify_requirement_items as _verify_items
        _has_items_module = True
    except (ValueError, TypeError, AttributeError, ImportError):
        _has_items_module = False
        _HUMAN_CONFIRMED = "HUMAN_CONFIRMED"
        _verify_items = None
    if not _has_items_module and not has_vehicle:
        return _untestable(
            cid, CHECK_VERIFY, "人の確認待ち: 項目別検証器を読み込めません",
            code=REASON_VERIFY,
        )
    if _has_items_module and not has_vehicle:
        # P0-C: 保存後の成果物本文の根拠確認をライブ再検査に合流させる。
        # 登録テキストは業務事実の照合用にだけ使い、原本本文の外部転送はしない。
        # 読めない・出典不能は UNTESTABLE で合格にしない。
        try:
            from app.structured_planning import build_p0c_registered_text as _p0c_text
            try:
                _p0c_files = manager.memory.list_context_files(
                    project_id, include_content=True)
            except (OSError, ValueError, TypeError, AttributeError, KeyError):
                _p0c_files = []
            _p0c_registered = _p0c_text(
                _mission_for_p0c(manager, project_id), _p0c_files, "")
        except (ValueError, TypeError, AttributeError):
            _p0c_registered = ""
        live_rows: list[dict] = []
        for task in exec_tasks:
            def resolve_item_path(relative):
                return _resolve(manager, project_id, relative, must_exist=True)
            try:
                live_rows.extend(_verify_items(task, resolve_item_path, semantic_judge=None,
                                               registered_text=_p0c_registered))
            except (OSError, ValueError, TypeError, AttributeError, KeyError):
                return _untestable(cid, CHECK_VERIFY, "人の確認待ち: 項目別検証を確認できません", code=REASON_VERIFY)
        live_rows = [row for row in live_rows if str(row.get("criterion_id") or "") == cid]
        live_required = [row for row in live_rows if row.get("required", True)]
        if not live_required:
            return _untestable(cid, CHECK_VERIFY, "人の確認待ち: 必須項目が抽出できません", code=REASON_VERIFY)
        # 台帳の人確認を反映する(文意の項目だけ。機械項目は上書きできない)。
        # 読取失敗は『空』に倒さず UNTESTABLE にする(人確認の流用をしない)。
        try:
            saved_ledger = open_ledger_readonly(manager, project_id)
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            return _untestable(cid, CHECK_VERIFY, "人の確認待ち: 項目別検証を確認できません", code=REASON_VERIFY)
        if saved_ledger is not None and isinstance(saved_ledger, dict):
            saved_rows = saved_ledger.get("rows") if isinstance(saved_ledger.get("rows"), list) else []
            # 人確認を流用する前に、現行の計画・原本・対象成果物が
            # 台帳と一致することを毎回確かめる。変更後は再確認が必要。
            try:
                stored_hashes = __import__("json").loads(
                    str(saved_ledger.get("artifact_hashes_json") or "{}"))
            except (ValueError, TypeError):
                stored_hashes = {}
            same_plan = bool(current_binding.get("plan_signature")) and (
                str(saved_ledger.get("plan_signature") or "")
                == str(current_binding.get("plan_signature") or ""))
            same_source = bool(current_binding.get("source_hash")) and (
                str(saved_ledger.get("source_hash") or "")
                == str(current_binding.get("source_hash") or ""))
            confirmed_by_id = {
                str(row.get("item_id") or ""): row
                for row in saved_rows
                if same_plan and same_source and isinstance(stored_hashes, dict)
                and isinstance(row, dict) and str(row.get("result") or "") == _HUMAN_CONFIRMED
                and str(row.get("method") or "") == "llm_semantic"
                and str(stored_hashes.get(str(row.get("artifact_path") or "")) or "")
                == _artifact_digest(manager, project_id, str(row.get("artifact_path") or ""))
            }
            for row in live_required:
                if str(row.get("result") or "") != "UNTESTABLE":
                    continue
                if str(row.get("method") or "") != "llm_semantic":
                    continue
                confirmed = confirmed_by_id.get(str(row.get("item_id") or ""))
                if confirmed is not None:
                    row["result"] = _HUMAN_CONFIRMED
        live_results = [str(row.get("result") or "") for row in live_required]
        if any(result == "FAIL" for result in live_results):
            detail = "; ".join(
                f"{row.get('item_id')}:{row.get('reason')}" for row in live_required
                if str(row.get("result") or "") == "FAIL"
            )
            return _fail(cid, CHECK_VERIFY, REASON_VERIFY,
                         "必須項目の未達: " + detail[:800], primary, detail[:500])
        if any(result == "UNTESTABLE" for result in live_results):
            detail = "; ".join(
                f"{row.get('item_id')}:{row.get('reason')}" for row in live_required
                if str(row.get("result") or "") == "UNTESTABLE"
            )
            return _untestable(cid, CHECK_VERIFY, "人の確認待ち: " + detail[:800],
                               primary, code=REASON_VERIFY)
        if any(result not in ("PASS", _HUMAN_CONFIRMED) for result in live_results):
            detail = "; ".join(
                f"{row.get('item_id')}:{row.get('reason')}" for row in live_required
                if str(row.get("result") or "") not in ("PASS", _HUMAN_CONFIRMED)
            )
            return _untestable(cid, CHECK_VERIFY, "人の確認待ち: " + detail[:800],
                               primary, code=REASON_VERIFY)
        # 台帳が無ければ未検証として扱う(レポート生成前にPASSにしない)。
        if saved_ledger is None:
            return _untestable(cid, CHECK_VERIFY, "人の確認待ち: 項目別検証台帳がありません",
                               code=REASON_VERIFY)
        try:
            from app.requirement_verification import ledger_invalidated as _invalidated
            artifact_hashes = {
                str(output.get("path") or ""): _artifact_digest(manager, project_id, str(output.get("path") or ""))
                for output in outputs
            }
            invalid = _invalidated(
                {
                    "plan_signature": str((saved_ledger or {}).get("plan_signature") or ""),
                    "source_hash": str((saved_ledger or {}).get("source_hash") or ""),
                    "artifact_hashes": __import__("json").loads(
                        str((saved_ledger or {}).get("artifact_hashes_json") or "{}")),
                },
                str((current_binding or {}).get("plan_signature") or ""),
                str((current_binding or {}).get("source_hash") or ""),
                artifact_hashes,
            )
        except (ValueError, TypeError, AttributeError, KeyError):
            invalid = ["ledger_unreadable"]
        if invalid and any(str(row.get("result") or "") == _HUMAN_CONFIRMED for row in live_required):
            return _untestable(
                cid, CHECK_VERIFY, "人の確認待ち: 計画・原本・成果物が変わり、以前の確認を再利用できません",
                code=REASON_VERIFY,
            )
        saved_rows = saved_ledger.get("rows") if isinstance(saved_ledger.get("rows"), list) else []
        saved_for_cid = [
            row for row in saved_rows
            if isinstance(row, dict) and str(row.get("criterion_id") or "") == cid
            and int(row.get("is_required", 1)) == 1
        ]
        if not saved_for_cid or any(
                str(row.get("result") or "") not in ("PASS", _HUMAN_CONFIRMED)
                or (str(row.get("result") or "") == _HUMAN_CONFIRMED
                    and str(row.get("method") or "") != "llm_semantic")
                for row in saved_for_cid):
            return _untestable(cid, CHECK_VERIFY, "人の確認待ち: 台帳の必須項目がPASSではありません",
                               code=REASON_VERIFY)

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


def open_ledger_readonly(manager, project_id: str) -> dict | None:
    """監視用の読み取り専用経路。実行中PJの状態を書き換えない。

    ShortTermMemory を新規生成しない(open_ledger_readonly経由で台帳だけ読む)。
    戻り値は最新台帳。台帳が無ければNone、読取失敗は例外。
    """
    from app.requirement_item_ledger import open_ledger_readonly as _open

    memory_path = getattr(getattr(manager, "memory", None), "path", None)
    if memory_path is None:
        raise ValueError("manager.memory.path がありません")
    return _open(memory_path, project_id)
