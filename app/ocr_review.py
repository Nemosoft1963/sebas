"""OCR-4A: 承認・公開・RAG登録の制御。人間承認前のRAG登録は 0 件。"""
from __future__ import annotations

import hashlib
import io
import json
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import unquote

from app.experience_store import ExperienceStore, canonical
from app.ocr_artifacts import (
    make_execution_key,
    resolve_artifact_path,
    sha256_hex,
    utcnow,
    verify_manifest,
    verify_source_hash,
)
from app.ocr_fallback import validate_ocr_checks
from app.ocr_invoice_adapter import payload_from_ocr_bundle
from app.ocr_schema import OcrErrorCode, OcrIssue, validate_ocr_result
from app.ocr_store import OcrStore, TERMINAL_STATUSES

APPROVE_DECISIONS = frozenset({"approve", "approved", "correct_and_approve"})
REJECT_DECISIONS = frozenset({"reject", "rejected"})
API_DECISIONS = frozenset({"approve", "correct_and_approve", "reject"})
ADOPTION_UNAPPROVED = "unapproved"
ADOPTION_APPROVED = "approved"
ADOPTION_REJECTED = "rejected"
ADOPTION_PUBLISHED = "published"
STORED_APPROVED = "approved"
STORED_CORRECTED = "correct_and_approve"
STORED_REJECTED = "rejected"
MAX_OCR_PAGES = 500
MAX_OCR_ATTEMPTS = 5
RETRYABLE_STATUSES = frozenset({"failed"})

# テスト差し替え用。本番の経験RAGや実OCRクライアントには触れない。
OCR_RUNTIME: dict[str, Any] = {
    "client_factory": None,
    "renderer": None,
    "feature_enabled": None,
    "artifact_base": None,
    "experience_store_factory": None,
    "job_hold": None,
    "page_count_fn": None,
}

_OCR_JOBS: dict[str, threading.Thread] = {}
_OCR_JOBS_GUARD = threading.Lock()


class OcrApiError(Exception):
    def __init__(self, status_code: int, error_code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message

    def as_detail(self) -> dict[str, str]:
        return {"error_code": self.error_code, "message": self.message}


def _json_load(value: Any, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _norm_sha(value: str) -> str:
    text = (value or "").strip().lower()
    if text.startswith("sha256:"):
        text = text[7:]
    return text


def _pypdf_page_count(original_bytes: bytes) -> int:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(original_bytes))
    return len(reader.pages)


def pdf_page_count(original_bytes: bytes | None) -> int:
    """保存済み原本PDFからページ数を求める。1 へのフォールバックはしない。"""
    override = OCR_RUNTIME.get("page_count_fn")
    try:
        if callable(override):
            count = int(override(original_bytes))
        else:
            if not original_bytes:
                raise OcrApiError(
                    422, OcrErrorCode.OCR_PAGE_RENDER_FAILED.value,
                    "原本PDFからページ数を求められません",
                )
            count = int(_pypdf_page_count(original_bytes))
    except OcrApiError:
        raise
    except (TypeError, ValueError, OSError, Exception) as exc:
        raise OcrApiError(
            422, OcrErrorCode.OCR_PAGE_RENDER_FAILED.value,
            "原本PDFを開けないためページ数を求められません",
        ) from exc
    if count < 1:
        raise OcrApiError(
            422, OcrErrorCode.OCR_PAGE_RENDER_FAILED.value,
            "原本PDFのページ数が不正です",
        )
    if count > MAX_OCR_PAGES:
        raise OcrApiError(
            409, OcrErrorCode.OCR_SCHEMA_INVALID.value,
            "ページ数が500を超えるため範囲選択が必要です。先頭500ページだけは処理しません",
        )
    return count


def unreadable_pages_quality(page_count: int) -> list[dict[str, Any]]:
    """ページ数が分かるが抽出情報が無いときは全ページ unreadable（既存 trigger 方針）。"""
    return [
        {"page": index, "quality": "unreadable", "text": "", "char_count": 0}
        for index in range(1, int(page_count) + 1)
    ]


def resolve_ocr_page_spec(
    original_bytes: bytes | None,
    claimed_page_count: int | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    """クライアントの page_count は信用せず、原本から求めたページ数を使う。"""
    actual = pdf_page_count(original_bytes)
    if claimed_page_count is not None:
        try:
            claimed = int(claimed_page_count)
        except (TypeError, ValueError) as exc:
            raise OcrApiError(
                409, OcrErrorCode.OCR_SCHEMA_INVALID.value,
                "指定ページ数が不正です",
            ) from exc
        if claimed != actual:
            raise OcrApiError(
                409, OcrErrorCode.OCR_SCHEMA_INVALID.value,
                "指定ページ数が原本のページ数と一致しません",
            )
    return actual, unreadable_pages_quality(actual)


def stored_decision(decision: str) -> str:
    raw = (decision or "").strip()
    if raw in {"approve", "approved"}:
        return STORED_APPROVED
    if raw == "correct_and_approve":
        return STORED_CORRECTED
    if raw in {"reject", "rejected"}:
        return STORED_REJECTED
    return raw


def decision_is_approved(decision: str | None) -> bool:
    return stored_decision(str(decision or "")) in {STORED_APPROVED, STORED_CORRECTED}


def decision_is_rejected(decision: str | None) -> bool:
    return stored_decision(str(decision or "")) == STORED_REJECTED


def adoption_status_of(run: Mapping[str, Any] | None, review: Mapping[str, Any] | None = None) -> str:
    if not run:
        return ADOPTION_UNAPPROVED
    stored = str(run.get("adoption_status") or "")
    if stored == ADOPTION_PUBLISHED or run.get("published_at"):
        return ADOPTION_PUBLISHED
    if stored in {ADOPTION_APPROVED, ADOPTION_REJECTED, ADOPTION_PUBLISHED}:
        if stored == ADOPTION_APPROVED and run.get("published_at"):
            return ADOPTION_PUBLISHED
        return stored
    if review and decision_is_rejected(review.get("decision")):
        return ADOPTION_REJECTED
    if review and decision_is_approved(review.get("decision")):
        return ADOPTION_APPROVED
    return ADOPTION_UNAPPROVED


def rag_registered_of(run: Mapping[str, Any] | None) -> bool:
    if not run:
        return False
    if run.get("rag_revoked"):
        return False
    return bool(run.get("rag_registered"))


def manifest_hash(artifact_root: Path | str | None) -> str:
    if not artifact_root:
        return ""
    root = Path(artifact_root)
    try:
        path = resolve_artifact_path(root, "manifest.json")
    except ValueError:
        return ""
    if not path.is_file():
        return ""
    return sha256_hex(path.read_bytes())


def review_signature_payload(
    *,
    run_id: str,
    decision: str,
    corrected_values: Mapping[str, Any] | None,
    reviewer: str,
    reviewed_at: str,
    source_sha256: str,
    manifest_sha256: str,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "decision": stored_decision(decision),
        "corrected_values": corrected_values or {},
        "reviewer": reviewer,
        "reviewed_at": reviewed_at,
        "source_sha256": _norm_sha(source_sha256),
        "manifest_hash": _norm_sha(manifest_sha256),
    }


def make_review_signature(payload: Mapping[str, Any]) -> str:
    blob = canonical(dict(payload))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def load_ocr_payload(artifact_root: Path | str | None, bundle: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    if artifact_root:
        try:
            path = resolve_artifact_path(Path(artifact_root), "ocr_result.json")
        except ValueError:
            path = None
        if path is not None and path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = None
            if isinstance(data, dict):
                return data
    if bundle:
        return payload_from_ocr_bundle(bundle)
    return None


def _page_missing(payload: Mapping[str, Any] | None, bundle: Mapping[str, Any] | None) -> bool:
    pages = []
    page_count = 0
    if payload and isinstance(payload.get("pages"), list):
        pages = [item for item in payload["pages"] if isinstance(item, Mapping)]
        source = payload.get("source") if isinstance(payload.get("source"), Mapping) else {}
        try:
            page_count = int(source.get("page_count") or 0)
        except (TypeError, ValueError):
            page_count = 0
    elif bundle:
        pages = list(bundle.get("pages") or [])
        run = bundle.get("run") or {}
        try:
            page_count = int((payload or {}).get("source", {}).get("page_count") or 0)
        except (TypeError, ValueError):
            page_count = 0
        if not page_count:
            page_count = len(pages)
    present = set()
    for item in pages:
        raw = item.get("page") if isinstance(item, Mapping) else None
        if raw is None and isinstance(item, Mapping):
            raw = item.get("page_no")
        try:
            if raw is not None:
                present.add(int(raw))
        except (TypeError, ValueError):
            continue
    if page_count >= 1:
        expected = set(range(1, page_count + 1))
        return bool(expected - present)
    return False


def ocr_review_guard(
    *,
    run: Mapping[str, Any],
    original_bytes: bytes | None,
    expected_sha256: str | None = None,
    artifact_root: Path | str | None = None,
    payload: Mapping[str, Any] | None = None,
    bundle: Mapping[str, Any] | None = None,
    allow_failed: bool = False,
) -> list[OcrIssue]:
    """承認・公開・RAG登録の直前に原本SHAと成果物manifestを再検証する。"""
    issues: list[OcrIssue] = []
    status = str(run.get("status") or "")
    if status == "failed" and not allow_failed:
        issues.append(OcrIssue(
            code=str(run.get("error_code") or OcrErrorCode.OCR_REVIEW_REQUIRED.value),
            message="failed の結果は承認・公開できません",
            path="status",
        ))
    if status in {"queued", "running"}:
        issues.append(OcrIssue(
            code=OcrErrorCode.OCR_REVIEW_REQUIRED.value,
            message="OCRが未完了のため承認できません",
            path="status",
        ))
    sha = _norm_sha(expected_sha256 or str(run.get("source_sha256") or ""))
    if original_bytes is None:
        issues.append(OcrIssue(
            code=OcrErrorCode.OCR_SOURCE_HASH_MISMATCH.value,
            message="原本バイトが無く原本同一性を再検証できません",
            path="source.sha256",
        ))
    else:
        issues.extend(verify_source_hash(original_bytes, sha))
    root = artifact_root if artifact_root is not None else run.get("artifact_root")
    if root:
        issues.extend(verify_manifest(Path(str(root))))
    else:
        issues.append(OcrIssue(
            code=OcrErrorCode.OCR_ARTIFACT_TAMPERED.value,
            message="成果物ディレクトリが無く manifest を再検証できません",
            path="manifest.json",
        ))
    body = payload if payload is not None else load_ocr_payload(root, bundle)
    if body is None:
        issues.append(OcrIssue(
            code=OcrErrorCode.OCR_SCHEMA_INVALID.value,
            message="OCR結果を再構成できません",
            path="ocr_result.json",
        ))
    else:
        issues.extend(validate_ocr_result(dict(body)))
        if _page_missing(body, bundle):
            issues.append(OcrIssue(
                code=OcrErrorCode.OCR_PAGE_RENDER_FAILED.value,
                message="対象ページが欠落しています",
                path="pages",
            ))
    return issues


def guard_blocks_approval(issues: Sequence[OcrIssue]) -> str | None:
    blocking = {
        OcrErrorCode.OCR_SOURCE_HASH_MISMATCH.value,
        OcrErrorCode.OCR_ARTIFACT_TAMPERED.value,
        OcrErrorCode.OCR_SCHEMA_INVALID.value,
        OcrErrorCode.OCR_PAGE_RENDER_FAILED.value,
        OcrErrorCode.OCR_REPETITION_DETECTED.value,
        OcrErrorCode.OCR_TOTAL_MISMATCH.value,
    }
    for issue in issues:
        if issue.code in blocking:
            return issue.code
        if issue.path == "status" and "failed" in issue.message:
            return issue.code
    return issues[0].code if issues else None


def apply_corrections(
    payload: Mapping[str, Any],
    corrected_values: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """訂正値を適用したコピー。元の OCR 値は payload 側に残す。"""
    overlay = dict(payload)
    fields = [dict(item) if isinstance(item, Mapping) else item for item in (payload.get("fields") or [])]
    corrections = corrected_values or {}
    by_id = {str(key): value for key, value in corrections.items()}
    for index, item in enumerate(fields):
        if not isinstance(item, dict):
            continue
        field_id = str(item.get("field_id") or f"f{index + 1:03d}")
        name = str(item.get("name") or "")
        raw = by_id.get(field_id, by_id.get(name))
        if raw is None:
            continue
        if isinstance(raw, Mapping):
            if "value" in raw:
                item["value"] = "" if raw.get("value") is None else str(raw.get("value"))
            if raw.get("value_state"):
                item["value_state"] = str(raw["value_state"])
            if raw.get("unreadable"):
                item["value_state"] = "unreadable"
        else:
            item["value"] = str(raw)
        fields[index] = item
    overlay["fields"] = fields
    return overlay


def _correction_check_error(checks: Sequence[Mapping[str, Any]]) -> str | None:
    by_id = {str(item.get("check_id")): item for item in checks}
    for check_id, code in (
        ("VAL-05", OcrErrorCode.OCR_TOTAL_MISMATCH.value),
        ("VAL-06", OcrErrorCode.OCR_TOTAL_MISMATCH.value),
        ("VAL-09", OcrErrorCode.OCR_FIELD_AMBIGUOUS.value),
    ):
        status = str((by_id.get(check_id) or {}).get("status") or "")
        if status == "failed":
            return code
        if check_id == "VAL-09" and status == "failed":
            return code
    val09 = by_id.get("VAL-09") or {}
    if str(val09.get("status") or "") == "failed":
        return OcrErrorCode.OCR_FIELD_AMBIGUOUS.value
    return None


def require_complete_run(run: Mapping[str, Any] | None, *, project_id: str, run_id: str) -> dict[str, Any]:
    if not run or str(run.get("project_id") or "") != str(project_id):
        raise OcrApiError(404, "not_found", "OCR実行が見つかりません")
    return dict(run)


def _require_reviewer_reason(reviewer: str, reason: str) -> tuple[str, str]:
    who = (reviewer or "").strip()
    why = (reason or "").strip()
    if not who or not why:
        raise OcrApiError(422, "invalid_review", "レビュー者名と理由は必須です")
    return who, why


def submit_ocr_review(
    store: OcrStore,
    *,
    project_id: str,
    run_id: str,
    decision: str,
    reviewer: str,
    reason: str,
    original_bytes: bytes | None,
    corrected_values: Mapping[str, Any] | None = None,
    correction_reason: str = "",
    artifact_root: Path | str | None = None,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    run = require_complete_run(store.get_run(run_id), project_id=project_id, run_id=run_id)
    action = (decision or "").strip()
    if action not in API_DECISIONS:
        raise OcrApiError(422, "invalid_review", "decision は approve / correct_and_approve / reject です")
    who, why = _require_reviewer_reason(reviewer, reason)
    stored = stored_decision(action)
    root = artifact_root if artifact_root is not None else run.get("artifact_root")
    bundle = store.get_bundle(run_id) or {"run": run}
    payload = load_ocr_payload(root, bundle)
    now = utcnow()
    corrections = dict(corrected_values or {})

    if stored == STORED_REJECTED:
        sig_body = review_signature_payload(
            run_id=run_id, decision=stored, corrected_values={},
            reviewer=who, reviewed_at=now,
            source_sha256=str(run.get("source_sha256") or ""),
            manifest_sha256=manifest_hash(root),
        )
        signature = make_review_signature(sig_body)
        store.save_review(
            run_id, decision=stored, reviewer=who, evidence=why,
            corrected_values={}, signature=signature, reviewed_at=now,
        )
        store.update_run(
            run_id, adoption_status=ADOPTION_REJECTED,
            rag_registered=False, clear_published_at=True,
        )
        review = store.get_review(run_id) or {}
        return {
            "run_id": run_id,
            "decision": stored,
            "signature": signature,
            "adoption_status": ADOPTION_REJECTED,
            "review": review,
        }

    if str(run.get("status") or "") == "failed":
        raise OcrApiError(409, OcrErrorCode.OCR_REVIEW_REQUIRED.value, "failed の結果は承認できません")

    issues = ocr_review_guard(
        run=run, original_bytes=original_bytes, expected_sha256=expected_sha256,
        artifact_root=root, payload=payload, bundle=bundle,
    )
    blocked = guard_blocks_approval(issues)
    if blocked:
        raise OcrApiError(409, blocked, issues[0].message if issues else "承認前提条件を満たしません")

    if stored == STORED_CORRECTED:
        corr_reason = (correction_reason or why).strip()
        if not corrections:
            raise OcrApiError(422, "invalid_review", "訂正して承認するには訂正値が必要です")
        if not corr_reason:
            raise OcrApiError(422, "invalid_review", "訂正理由は必須です")
        if payload is None:
            raise OcrApiError(409, OcrErrorCode.OCR_SCHEMA_INVALID.value, "訂正対象のOCR結果がありません")
        overlaid = apply_corrections(payload, corrections)
        schema_issues = validate_ocr_result(overlaid)
        if schema_issues:
            raise OcrApiError(
                409, OcrErrorCode.OCR_SCHEMA_INVALID.value,
                schema_issues[0].message or "訂正後のスキーマが不正です",
            )
        checks = validate_ocr_checks(
            overlaid,
            original_bytes=original_bytes,
            expected_sha256=_norm_sha(expected_sha256 or str(run.get("source_sha256") or "")),
        )
        check_error = _correction_check_error(checks)
        if check_error:
            raise OcrApiError(409, check_error, "訂正後の検算に不合格のため承認できません")
        why = corr_reason
    else:
        corrections = {}

    sig_body = review_signature_payload(
        run_id=run_id, decision=stored, corrected_values=corrections,
        reviewer=who, reviewed_at=now,
        source_sha256=str(run.get("source_sha256") or ""),
        manifest_sha256=manifest_hash(root),
    )
    signature = make_review_signature(sig_body)
    store.save_review(
        run_id, decision=stored, reviewer=who, evidence=why,
        corrected_values=corrections, signature=signature, reviewed_at=now,
    )
    store.update_run(run_id, adoption_status=ADOPTION_APPROVED)
    review = store.get_review(run_id) or {}
    original_fields = list((bundle.get("fields") or []))
    return {
        "run_id": run_id,
        "decision": stored,
        "signature": signature,
        "adoption_status": ADOPTION_APPROVED,
        "review": review,
        "original_fields": original_fields,
        "corrected_values": corrections,
    }


def publish_ocr_run(
    store: OcrStore,
    *,
    project_id: str,
    run_id: str,
    original_bytes: bytes | None,
    artifact_root: Path | str | None = None,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    run = require_complete_run(store.get_run(run_id), project_id=project_id, run_id=run_id)
    review = store.get_review(run_id)
    if str(run.get("status") or "") == "failed":
        raise OcrApiError(409, OcrErrorCode.OCR_REVIEW_REQUIRED.value, "failed の結果は公開できません")
    if not review or not decision_is_approved(review.get("decision")):
        code = OcrErrorCode.OCR_REVIEW_REQUIRED.value
        if review and decision_is_rejected(review.get("decision")):
            raise OcrApiError(409, code, "却下された結果は公開できません")
        raise OcrApiError(409, code, "未承認の結果は公開できません")
    root = artifact_root if artifact_root is not None else run.get("artifact_root")
    bundle = store.get_bundle(run_id)
    payload = load_ocr_payload(root, bundle)
    issues = ocr_review_guard(
        run=run, original_bytes=original_bytes, expected_sha256=expected_sha256,
        artifact_root=root, payload=payload, bundle=bundle,
    )
    blocked = guard_blocks_approval(issues)
    if blocked:
        raise OcrApiError(409, blocked, issues[0].message if issues else "公開直前の再検証に失敗しました")
    now = utcnow()
    store.update_run(run_id, adoption_status=ADOPTION_PUBLISHED, published_at=now)
    updated = store.get_run(run_id) or run
    return {
        "run_id": run_id,
        "adoption_status": ADOPTION_PUBLISHED,
        "published_at": updated.get("published_at") or now,
        "ocr_status": updated.get("status"),
    }


def ocr_rag_lesson(
    run: Mapping[str, Any],
    review: Mapping[str, Any],
    checks: Sequence[Mapping[str, Any]] | None = None,
    field_names: Sequence[str] | None = None,
) -> str:
    """金額の生値や個人情報を含めない再利用可能な知識。"""
    engine = _json_load(run.get("engine"), {})
    return canonical({
        "kind": "ocr_approved_procedure",
        "schema_version": "ocr-fallback/v1",
        "trigger": run.get("trigger") or "",
        "engine": {
            "pipeline": (engine or {}).get("pipeline") if isinstance(engine, Mapping) else "",
            "vlm_model": (engine or {}).get("vlm_model") if isinstance(engine, Mapping) else "",
            "layout_model": (engine or {}).get("layout_model") if isinstance(engine, Mapping) else "",
        },
        "checks": [
            {"check_id": item.get("check_id"), "status": item.get("status")}
            for item in (checks or [])
            if isinstance(item, Mapping)
        ],
        "field_names": list(field_names or []),
        "review_decision": review.get("decision"),
        "evidence": {
            "source_sha256": _norm_sha(str(run.get("source_sha256") or "")),
            "manifest_hash": "",
            "review_signature": str(review.get("signature") or ""),
        },
    })


def register_ocr_with_experience_store(
    experience_store: ExperienceStore,
    *,
    project_id: str,
    content: str,
    evidence: Mapping[str, Any],
    reviewer: str,
    proof: str,
    expires: float | None = None,
) -> str:
    """既存 ExperienceStore の追加のみ・承認状態を持つ流儀に乗る。"""
    import time
    rid = experience_store.add(
        project_id, "success", content,
        {"source": "ocr_approved", "kind": "ocr_fallback"},
        dict(evidence),
    )
    exp = expires if expires is not None else time.time() + 30 * 86400
    experience_store.review(project_id, rid, "verified", reviewer, proof, exp)
    return rid


def register_ocr_rag(
    store: OcrStore,
    *,
    project_id: str,
    run_id: str,
    original_bytes: bytes | None,
    confirm_rag: bool,
    reviewer: str,
    reason: str,
    experience_store: ExperienceStore | None = None,
    artifact_root: Path | str | None = None,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    if not confirm_rag:
        raise OcrApiError(409, "rag_not_confirmed", "RAG登録は明示指定(confirm_rag=true)のときだけ行います")
    run = require_complete_run(store.get_run(run_id), project_id=project_id, run_id=run_id)
    review = store.get_review(run_id)
    if str(run.get("status") or "") == "failed":
        raise OcrApiError(409, OcrErrorCode.OCR_REVIEW_REQUIRED.value, "failed の結果はRAG登録できません")
    if not review or not decision_is_approved(review.get("decision")):
        raise OcrApiError(409, OcrErrorCode.OCR_REVIEW_REQUIRED.value, "人間承認前のRAG登録はできません")
    if decision_is_rejected(review.get("decision")):
        raise OcrApiError(409, OcrErrorCode.OCR_REVIEW_REQUIRED.value, "却下された結果はRAG登録できません")
    if not str(review.get("signature") or "").strip():
        raise OcrApiError(409, OcrErrorCode.OCR_REVIEW_REQUIRED.value, "レビュー署名が無い結果はRAG登録できません")
    who, why = _require_reviewer_reason(reviewer, reason)
    root = artifact_root if artifact_root is not None else run.get("artifact_root")
    bundle = store.get_bundle(run_id)
    payload = load_ocr_payload(root, bundle)
    issues = ocr_review_guard(
        run=run, original_bytes=original_bytes, expected_sha256=expected_sha256,
        artifact_root=root, payload=payload, bundle=bundle,
    )
    blocked = guard_blocks_approval(issues)
    if blocked:
        raise OcrApiError(409, blocked, issues[0].message if issues else "RAG登録直前の再検証に失敗しました")
    if rag_registered_of(run) and run.get("rag_experience_id"):
        return {
            "run_id": run_id,
            "rag_registered": True,
            "experience_id": run.get("rag_experience_id"),
            "duplicate": True,
        }
    man_hash = manifest_hash(root)
    checks = list((bundle or {}).get("validations") or [])
    names = [str(item.get("name") or "") for item in ((bundle or {}).get("fields") or [])]
    lesson = ocr_rag_lesson(run, review, checks, names)
    lesson_obj = json.loads(lesson)
    lesson_obj["evidence"]["manifest_hash"] = man_hash
    lesson = canonical(lesson_obj)
    evidence = {
        "ocr_run_id": run_id,
        "source_sha256": _norm_sha(str(run.get("source_sha256") or "")),
        "manifest_hash": man_hash,
        "review_signature": str(review.get("signature") or ""),
        "review_decision": review.get("decision"),
    }
    if experience_store is None:
        factory = OCR_RUNTIME.get("experience_store_factory")
        if callable(factory):
            experience_store = factory()
    if experience_store is None:
        raise OcrApiError(409, "rag_store_unavailable", "経験RAGストアが設定されていません")
    rid = register_ocr_with_experience_store(
        experience_store, project_id=project_id, content=lesson,
        evidence=evidence, reviewer=who, proof=why,
    )
    store.update_run(run_id, rag_registered=True, rag_experience_id=rid, rag_revoked=False)
    return {"run_id": run_id, "rag_registered": True, "experience_id": rid, "duplicate": False}


def revoke_ocr_rag(
    store: OcrStore,
    *,
    project_id: str,
    run_id: str,
    reviewer: str,
    reason: str,
    experience_store: ExperienceStore | None = None,
) -> dict[str, Any]:
    run = require_complete_run(store.get_run(run_id), project_id=project_id, run_id=run_id)
    who, why = _require_reviewer_reason(reviewer, reason)
    rid = str(run.get("rag_experience_id") or "")
    if experience_store is None:
        factory = OCR_RUNTIME.get("experience_store_factory")
        if callable(factory):
            experience_store = factory()
    if rid and experience_store is not None:
        experience_store.review(project_id, rid, "revoked", who, why, 0)
    store.update_run(run_id, rag_registered=False, rag_revoked=True)
    return {
        "run_id": run_id,
        "rag_registered": False,
        "rag_revoked": True,
        "experience_id": rid,
    }


def list_ocr_artifacts(run: Mapping[str, Any], *, artifact_root: Path | str | None = None) -> list[dict[str, Any]]:
    root = Path(str(artifact_root if artifact_root is not None else run.get("artifact_root") or ""))
    if not str(root):
        return []
    try:
        path = resolve_artifact_path(root, "manifest.json")
    except ValueError as exc:
        raise OcrApiError(400, OcrErrorCode.OCR_ARTIFACT_TAMPERED.value, str(exc)) from exc
    if not path.is_file():
        return []
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OcrApiError(409, OcrErrorCode.OCR_ARTIFACT_TAMPERED.value, "manifest を読めません") from exc
    files = manifest.get("files") if isinstance(manifest, Mapping) else None
    if not isinstance(files, list):
        raise OcrApiError(409, OcrErrorCode.OCR_ARTIFACT_TAMPERED.value, "manifest.files が不正です")
    out: list[dict[str, Any]] = []
    for entry in files:
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("name") or "")
        try:
            resolve_artifact_path(root, name)
        except ValueError as exc:
            raise OcrApiError(400, OcrErrorCode.OCR_ARTIFACT_TAMPERED.value, str(exc)) from exc
        out.append({
            "name": name,
            "sha256": str(entry.get("sha256") or ""),
            "size": entry.get("size"),
        })
    return out


def field_evidence(
    store: OcrStore,
    *,
    project_id: str,
    run_id: str,
    field_id: str,
) -> dict[str, Any]:
    run = require_complete_run(store.get_run(run_id), project_id=project_id, run_id=run_id)
    bundle = store.get_bundle(run_id) or {"run": run, "fields": [], "blocks": []}
    fields = list(bundle.get("fields") or [])
    found = None
    for item in fields:
        if str(item.get("field_id") or "") == str(field_id):
            found = item
            break
    if found is None:
        payload = load_ocr_payload(run.get("artifact_root"), bundle)
        for index, item in enumerate((payload or {}).get("fields") or []):
            if not isinstance(item, Mapping):
                continue
            fid = str(item.get("field_id") or f"f{index + 1:03d}")
            if fid == str(field_id) or str(item.get("name") or "") == str(field_id):
                found = {
                    "field_id": fid,
                    "name": item.get("name"),
                    "value_text": item.get("value"),
                    "page_no": item.get("page"),
                    "block_id": item.get("block_id"),
                    "validation_status": item.get("validation") or item.get("value_state"),
                    "evidence_text": item.get("evidence_text") or "",
                    "bbox": item.get("bbox"),
                }
                break
    if found is None:
        raise OcrApiError(404, "not_found", "抽出項目が見つかりません")
    block_id = str(found.get("block_id") or "")
    page_no = found.get("page_no")
    bbox = found.get("bbox")
    evidence_text = str(found.get("evidence_text") or "")
    for block in bundle.get("blocks") or []:
        if str(block.get("block_id") or "") != block_id:
            continue
        if page_no is not None and block.get("page_no") not in {page_no, None}:
            if int(block.get("page_no") or 0) != int(page_no or 0):
                continue
        if not evidence_text:
            evidence_text = str(block.get("text") or "")
        if bbox is None:
            bbox = _json_load(block.get("bbox_json"), [0, 0, 0, 0])
        break
    if isinstance(bbox, str):
        bbox = _json_load(bbox, None)
    return {
        "field_id": str(found.get("field_id") or field_id),
        "name": found.get("name"),
        "value": found.get("value_text") if "value_text" in found else found.get("value"),
        "page": page_no,
        "block_id": block_id,
        "bbox": bbox,
        "evidence_text": evidence_text,
        "validation": found.get("validation_status") or "",
    }


def run_status_view(
    store: OcrStore,
    *,
    project_id: str,
    run_id: str,
) -> dict[str, Any]:
    run = require_complete_run(store.get_run(run_id), project_id=project_id, run_id=run_id)
    review = store.get_review(run_id)
    pages = store.list_pages(run_id)
    engine = _json_load(run.get("engine"), {})
    adoption = adoption_status_of(run, review)
    return {
        "run_id": run_id,
        "project_id": run.get("project_id"),
        "context_file_id": run.get("context_file_id"),
        "ocr_status": run.get("status"),
        "adoption_status": adoption,
        "rag_registered": rag_registered_of(run),
        "trigger": run.get("trigger") or "",
        "processed_pages": len(pages),
        "elapsed_ms": run.get("elapsed_ms"),
        "engine": engine if isinstance(engine, dict) else {},
        "engine_version": (engine or {}).get("pipeline") if isinstance(engine, dict) else "",
        "error_code": run.get("error_code") or "",
        "source_sha256": run.get("source_sha256") or "",
        "review": {
            "decision": (review or {}).get("decision"),
            "reviewer": (review or {}).get("reviewer"),
            "reviewed_at": (review or {}).get("reviewed_at"),
            "signature": (review or {}).get("signature") or "",
        } if review else {"decision": None, "reviewer": None, "reviewed_at": None, "signature": ""},
        "published_at": run.get("published_at"),
        "idempotency_key": run.get("idempotency_key") or "",
        "duplicate": False,
    }


def enqueue_ocr_run(
    store: OcrStore,
    *,
    project_id: str,
    context_file_id: str,
    original_bytes: bytes | None,
    expected_sha256: str,
    filename: str,
    idempotency_key: str,
    page_count: int | None = None,
    purpose: str = "context",
    user_requested_original_match: bool = False,
    pages_quality: Sequence[Mapping[str, Any]] | None = None,
    feature_enabled: bool | None = None,
) -> tuple[dict[str, Any], bool, int]:
    """ジョブを queued で作り、HTTP 中に OCR を完了させない。"""
    key = (idempotency_key or "").strip()
    if not key:
        raise OcrApiError(400, "idempotency_required", "冪等キーは必須です")
    enabled = OCR_RUNTIME["feature_enabled"] if feature_enabled is None and OCR_RUNTIME.get("feature_enabled") is not None else feature_enabled
    if enabled is None:
        from app.capability_registry import ocr_feature_enabled
        enabled = ocr_feature_enabled()
    if not enabled:
        raise OcrApiError(409, "feature_disabled", "OCR機能は無効です")

    actual_count, default_quality = resolve_ocr_page_spec(original_bytes, page_count)
    if pages_quality is None:
        pages_quality = default_quality
    page_count = actual_count

    existing = store.get_run_by_idempotency(project_id, context_file_id, key)
    if existing:
        view = run_status_view(store, project_id=project_id, run_id=str(existing["run_id"]))
        view["duplicate"] = True
        status_code = 202 if str(existing.get("status") or "") not in TERMINAL_STATUSES else 200
        return view, True, status_code

    sha = _norm_sha(expected_sha256)
    params: dict[str, Any] = {"dpi": 200}
    file_runs = store.list_runs_for_file(project_id, context_file_id)
    latest = file_runs[0] if file_runs else None
    if latest:
        latest_status = str(latest.get("status") or "")
        if latest_status not in RETRYABLE_STATUSES:
            view = run_status_view(store, project_id=project_id, run_id=str(latest["run_id"]))
            view["duplicate"] = True
            status_code = 202 if latest_status not in TERMINAL_STATUSES else 200
            return view, True, status_code
        if len(file_runs) >= MAX_OCR_ATTEMPTS:
            raise OcrApiError(409, "ocr_retry_limit", "OCR再実行の試行回数が上限に達しています")
        params = {"dpi": 200, "attempt": len(file_runs) + 1}
    parameters_hash = sha256_hex(
        json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    execution_key = make_execution_key(sha or "empty", "PaddleOCR-VL-1.6", parameters_hash)
    existing_exec = store.get_run_by_execution_key(execution_key)
    if existing_exec and str(existing_exec.get("project_id") or "") == str(project_id):
        if str(existing_exec.get("status") or "") not in RETRYABLE_STATUSES:
            view = run_status_view(store, project_id=project_id, run_id=str(existing_exec["run_id"]))
            view["duplicate"] = True
            status_code = 202 if str(existing_exec.get("status") or "") not in TERMINAL_STATUSES else 200
            return view, True, status_code

    base = OCR_RUNTIME.get("artifact_base")
    artifact_base = Path(base) if base else Path(store.path).parent / "ocr_artifacts"
    run_id = uuid.uuid4().hex
    artifact_root = artifact_base / project_id / run_id
    run, duplicate = store.create_run(
        project_id=project_id,
        context_file_id=context_file_id,
        source_sha256=sha,
        status="queued",
        trigger="manual" if user_requested_original_match else "unreadable",
        idempotency_key=key,
        artifact_root=str(artifact_root),
        run_id=run_id,
        execution_key=execution_key,
        parameters_hash=parameters_hash,
    )
    if duplicate:
        view = run_status_view(store, project_id=project_id, run_id=str(run["run_id"]))
        view["duplicate"] = True
        status_code = 202 if str(run.get("status") or "") not in TERMINAL_STATUSES else 200
        return view, True, status_code

    def _job() -> None:
        hold = OCR_RUNTIME.get("job_hold")
        if hold is not None:
            hold.wait(timeout=30)
        from app.ocr_fallback import run_ocr_fallback
        client_factory: Callable[[], Any] | None = OCR_RUNTIME.get("client_factory")
        client = client_factory() if callable(client_factory) else None
        renderer = OCR_RUNTIME.get("renderer")
        try:
            run_ocr_fallback(
                original_bytes=original_bytes,
                expected_sha256=sha,
                project_id=project_id,
                context_file_id=context_file_id,
                filename=filename,
                page_count=page_count,
                pages_quality=pages_quality,
                purpose=purpose,
                user_requested_original_match=user_requested_original_match,
                store=store,
                client=client,
                renderer=renderer,
                artifact_root=artifact_root,
                feature_enabled=True,
                parameters=params,
            )
        except Exception:
            store.update_run(
                run["run_id"], status="failed",
                error_code=OcrErrorCode.OCR_SERVICE_UNAVAILABLE.value, finished=True,
            )

    thread = threading.Thread(target=_job, name=f"ocr-{run['run_id']}", daemon=True)
    with _OCR_JOBS_GUARD:
        _OCR_JOBS[str(run["run_id"])] = thread
    thread.start()
    view = run_status_view(store, project_id=project_id, run_id=str(run["run_id"]))
    view["ocr_status"] = "queued"
    view["duplicate"] = False
    return view, False, 202


ARTIFACT_MEDIA_TYPES = {
    ".png": "image/png",
    ".md": "text/markdown; charset=utf-8",
    ".json": "application/json",
}


def sanitize_artifact_download_name(name: str) -> str:
    """成果物ダウンロード名。`..`・絶対パス・区切り文字・多重エンコードを拒否する。"""
    if not name or not isinstance(name, str):
        raise OcrApiError(400, "invalid_artifact", "成果物名が不正です")
    decoded = name.strip()
    for _ in range(4):
        nxt = unquote(decoded)
        if nxt == decoded:
            break
        decoded = nxt
    decoded = decoded.replace("\\", "/")
    if decoded.startswith("/") or decoded.startswith("\\") or (len(decoded) >= 2 and decoded[1] == ":"):
        raise OcrApiError(400, "invalid_artifact", "成果物の絶対パスは拒否します")
    parts = [part for part in decoded.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts) or ".." in decoded:
        raise OcrApiError(400, "invalid_artifact", "成果物パスのパストラバーサルは拒否します")
    if len(parts) != 1:
        raise OcrApiError(400, "invalid_artifact", "成果物パスの区切り文字は拒否します")
    safe = parts[0]
    if Path(safe).name != safe:
        raise OcrApiError(400, "invalid_artifact", "成果物名が不正です")
    suffix = Path(safe).suffix.lower()
    if suffix not in ARTIFACT_MEDIA_TYPES:
        raise OcrApiError(400, "invalid_artifact", "成果物の形式は png / md / json のみです")
    return safe


def list_file_ocr_runs(
    store: OcrStore,
    *,
    project_id: str,
    context_file_id: str,
) -> list[dict[str, Any]]:
    rows = store.list_runs_for_file(project_id, context_file_id)
    views: list[dict[str, Any]] = []
    for row in rows:
        view = run_status_view(store, project_id=project_id, run_id=str(row["run_id"]))
        payload = load_ocr_payload(row.get("artifact_root"))
        source = payload.get("source") if isinstance(payload, dict) else None
        if isinstance(source, Mapping) and source.get("page_count") is not None:
            view["page_count"] = source.get("page_count")
        else:
            view["page_count"] = view.get("processed_pages")
        views.append(view)
    return views


def load_ocr_artifact_file(
    run: Mapping[str, Any],
    name: str,
    *,
    artifact_root: Path | str | None = None,
) -> tuple[bytes, str, str]:
    """成果物を読み取り専用で返す。返す前に manifest の SHA-256 で改ざん検査する。"""
    safe = sanitize_artifact_download_name(name)
    media_type = ARTIFACT_MEDIA_TYPES[Path(safe).suffix.lower()]
    root = Path(str(artifact_root if artifact_root is not None else run.get("artifact_root") or ""))
    if not str(root):
        raise OcrApiError(404, "not_found", "成果物がありません")
    try:
        path = resolve_artifact_path(root, safe)
    except ValueError as exc:
        raise OcrApiError(400, "invalid_artifact", str(exc)) from exc
    listed = list_ocr_artifacts(run, artifact_root=root)
    entry = next((row for row in listed if str(row.get("name") or "") == safe), None)
    if entry is None:
        raise OcrApiError(404, "not_found", "成果物が見つかりません")
    if not path.is_file():
        raise OcrApiError(404, "not_found", "成果物ファイルがありません")
    data = path.read_bytes()
    expected = _norm_sha(str(entry.get("sha256") or ""))
    if sha256_hex(data) != expected:
        raise OcrApiError(
            409, OcrErrorCode.OCR_ARTIFACT_TAMPERED.value,
            "成果物ハッシュが一致しません",
        )
    return data, media_type, safe
