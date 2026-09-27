"""OCR実行の追加型永続化（仕様 10.2）。原本BLOBは複製しない。"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from app.memory.short_term import ensure_ocr_tables
from app.ocr_artifacts import utcnow

OCR_TABLES = (
    "ocr_runs",
    "ocr_pages",
    "ocr_blocks",
    "ocr_fields",
    "ocr_validations",
    "ocr_reviews",
)

TERMINAL_STATUSES = frozenset({"passed", "needs_review", "failed", "skipped"})
PAGE_DONE_STATUSES = frozenset({"done", "completed", "recognized"})


def _json_dump(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


class OcrStore:
    def __init__(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            ensure_ocr_tables(db)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def migrate(self) -> None:
        with self.connect() as db:
            ensure_ocr_tables(db)

    def table_names(self) -> set[str]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        return {row["name"] for row in rows}

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            return _row_dict(
                db.execute("SELECT * FROM ocr_runs WHERE run_id=?", (run_id,)).fetchone()
            )

    def get_run_by_execution_key(self, execution_key: str) -> dict[str, Any] | None:
        if not execution_key:
            return None
        with self.connect() as db:
            return _row_dict(
                db.execute(
                    "SELECT * FROM ocr_runs WHERE execution_key=?",
                    (execution_key,),
                ).fetchone()
            )

    def get_incomplete_run(self, project_id: str, context_file_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            return _row_dict(
                db.execute(
                    """SELECT * FROM ocr_runs
                       WHERE project_id=? AND context_file_id=?
                         AND status NOT IN ('passed','needs_review','failed','skipped')
                       ORDER BY started_at DESC""",
                    (project_id, context_file_id),
                ).fetchone()
            )

    def get_run_in_project(self, project_id: str, run_id: str) -> dict[str, Any] | None:
        run = self.get_run(run_id)
        if run is None or str(run.get("project_id") or "") != str(project_id):
            return None
        return run

    def get_run_by_idempotency(
        self, project_id: str, context_file_id: str, idempotency_key: str,
    ) -> dict[str, Any] | None:
        if not idempotency_key:
            return None
        with self.connect() as db:
            return _row_dict(
                db.execute(
                    """SELECT * FROM ocr_runs
                       WHERE project_id=? AND context_file_id=? AND idempotency_key=?""",
                    (project_id, context_file_id, idempotency_key),
                ).fetchone()
            )

    def list_runs_for_file(self, project_id: str, context_file_id: str) -> list[dict[str, Any]]:
        """同一プロジェクト・同一ファイルの OCR run を最新順で返す。"""
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM ocr_runs
                   WHERE project_id=? AND context_file_id=?
                   ORDER BY started_at DESC, rowid DESC""",
                (project_id, context_file_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_run(
        self,
        *,
        project_id: str,
        context_file_id: str,
        source_sha256: str,
        status: str = "queued",
        trigger: str = "",
        engine: Mapping[str, Any] | str | None = None,
        version: str = "ocr-fallback/v1",
        parameters_hash: str = "",
        error_code: str = "",
        execution_key: str = "",
        run_id: str | None = None,
        idempotency_key: str = "",
        artifact_root: str = "",
        adoption_status: str = "unapproved",
    ) -> tuple[dict[str, Any], bool]:
        """同一 execution_key があれば既存行を返す (duplicate=True)。原本BLOBは保存しない。"""
        now = utcnow()
        new_id = run_id or uuid.uuid4().hex
        engine_text = _json_dump(engine or {})
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if execution_key:
                existing = db.execute(
                    "SELECT * FROM ocr_runs WHERE execution_key=?",
                    (execution_key,),
                ).fetchone()
                if existing:
                    return dict(existing), True
            if idempotency_key:
                existing = db.execute(
                    """SELECT * FROM ocr_runs
                       WHERE project_id=? AND context_file_id=? AND idempotency_key=?""",
                    (project_id, context_file_id, idempotency_key),
                ).fetchone()
                if existing:
                    return dict(existing), True
            try:
                db.execute(
                    """INSERT INTO ocr_runs(
                        run_id, project_id, context_file_id, source_sha256, status, trigger,
                        engine, version, parameters_hash, started_at, finished_at, elapsed_ms,
                        error_code, execution_key, idempotency_key, adoption_status,
                        rag_registered, published_at, artifact_root, rag_experience_id, rag_revoked
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        new_id, project_id, context_file_id, source_sha256, status, trigger,
                        engine_text, version, parameters_hash, now, None, None,
                        error_code, execution_key, idempotency_key, adoption_status,
                        0, None, artifact_root, "", 0,
                    ),
                )
            except sqlite3.IntegrityError:
                if execution_key:
                    existing = db.execute(
                        "SELECT * FROM ocr_runs WHERE execution_key=?",
                        (execution_key,),
                    ).fetchone()
                    if existing:
                        return dict(existing), True
                if idempotency_key:
                    existing = db.execute(
                        """SELECT * FROM ocr_runs
                           WHERE project_id=? AND context_file_id=? AND idempotency_key=?""",
                        (project_id, context_file_id, idempotency_key),
                    ).fetchone()
                    if existing:
                        return dict(existing), True
                raise
        created = self.get_run(new_id)
        assert created is not None
        return created, False

    def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        trigger: str | None = None,
        error_code: str | None = None,
        engine: Mapping[str, Any] | str | None = None,
        finished: bool = False,
        elapsed_ms: int | None = None,
        source_sha256: str | None = None,
        adoption_status: str | None = None,
        rag_registered: bool | None = None,
        published_at: str | None = None,
        artifact_root: str | None = None,
        rag_experience_id: str | None = None,
        rag_revoked: bool | None = None,
        idempotency_key: str | None = None,
        clear_published_at: bool = False,
    ) -> dict[str, Any] | None:
        assignments: list[str] = []
        values: list[Any] = []
        if status is not None:
            assignments.append("status=?")
            values.append(status)
        if trigger is not None:
            assignments.append("trigger=?")
            values.append(trigger)
        if error_code is not None:
            assignments.append("error_code=?")
            values.append(error_code)
        if engine is not None:
            assignments.append("engine=?")
            values.append(_json_dump(engine))
        if source_sha256 is not None:
            assignments.append("source_sha256=?")
            values.append(source_sha256)
        if elapsed_ms is not None:
            assignments.append("elapsed_ms=?")
            values.append(elapsed_ms)
        if adoption_status is not None:
            assignments.append("adoption_status=?")
            values.append(adoption_status)
        if rag_registered is not None:
            assignments.append("rag_registered=?")
            values.append(1 if rag_registered else 0)
        if published_at is not None:
            assignments.append("published_at=?")
            values.append(published_at)
        if clear_published_at:
            assignments.append("published_at=?")
            values.append(None)
        if artifact_root is not None:
            assignments.append("artifact_root=?")
            values.append(artifact_root)
        if rag_experience_id is not None:
            assignments.append("rag_experience_id=?")
            values.append(rag_experience_id)
        if rag_revoked is not None:
            assignments.append("rag_revoked=?")
            values.append(1 if rag_revoked else 0)
        if idempotency_key is not None:
            assignments.append("idempotency_key=?")
            values.append(idempotency_key)
        if finished:
            assignments.append("finished_at=?")
            values.append(utcnow())
        if not assignments:
            return self.get_run(run_id)
        values.append(run_id)
        with self.connect() as db:
            db.execute(
                f"UPDATE ocr_runs SET {','.join(assignments)} WHERE run_id=?",
                values,
            )
        return self.get_run(run_id)

    def save_page(
        self,
        run_id: str,
        page_no: int,
        *,
        image_sha256: str = "",
        width: float | None = None,
        height: float | None = None,
        status: str = "pending",
    ) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO ocr_pages(run_id, page_no, image_sha256, width, height, status)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(run_id, page_no) DO UPDATE SET
                    image_sha256=excluded.image_sha256,
                    width=COALESCE(excluded.width, ocr_pages.width),
                    height=COALESCE(excluded.height, ocr_pages.height),
                    status=excluded.status""",
                (run_id, page_no, image_sha256, width, height, status),
            )

    def list_pages(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM ocr_pages WHERE run_id=? ORDER BY page_no",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def processed_page_nos(self, run_id: str) -> set[int]:
        return {
            int(row["page_no"])
            for row in self.list_pages(run_id)
            if row.get("status") in PAGE_DONE_STATUSES
        }

    def save_block(
        self,
        run_id: str,
        page_no: int,
        block_id: str,
        *,
        label: str = "text",
        bbox: Any = None,
        text: str = "",
        confidence: float | None = None,
    ) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO ocr_blocks(run_id, page_no, block_id, label, bbox_json, text, confidence)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(run_id, page_no, block_id) DO UPDATE SET
                    label=excluded.label,
                    bbox_json=excluded.bbox_json,
                    text=excluded.text,
                    confidence=excluded.confidence""",
                (run_id, page_no, block_id, label, _json_dump(bbox or []), text, confidence),
            )

    def list_blocks(self, run_id: str, page_no: int | None = None) -> list[dict[str, Any]]:
        with self.connect() as db:
            if page_no is None:
                rows = db.execute(
                    "SELECT * FROM ocr_blocks WHERE run_id=? ORDER BY page_no, block_id",
                    (run_id,),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM ocr_blocks WHERE run_id=? AND page_no=? ORDER BY block_id",
                    (run_id, page_no),
                ).fetchall()
        return [dict(row) for row in rows]

    def save_field(
        self,
        run_id: str,
        field_id: str,
        *,
        name: str,
        value_text: str = "",
        value_type: str = "string",
        page_no: int | None = None,
        block_id: str = "",
        validation_status: str = "",
    ) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO ocr_fields(
                    run_id, field_id, name, value_text, value_type, page_no, block_id, validation_status
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id, field_id) DO UPDATE SET
                    name=excluded.name,
                    value_text=excluded.value_text,
                    value_type=excluded.value_type,
                    page_no=excluded.page_no,
                    block_id=excluded.block_id,
                    validation_status=excluded.validation_status""",
                (run_id, field_id, name, value_text, value_type, page_no, block_id, validation_status),
            )

    def list_fields(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM ocr_fields WHERE run_id=? ORDER BY field_id",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_validation(
        self,
        run_id: str,
        check_id: str,
        *,
        status: str,
        expected: str = "",
        actual: str = "",
        detail: str = "",
    ) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO ocr_validations(run_id, check_id, status, expected, actual, detail)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(run_id, check_id) DO UPDATE SET
                    status=excluded.status,
                    expected=excluded.expected,
                    actual=excluded.actual,
                    detail=excluded.detail""",
                (run_id, check_id, status, expected, actual, detail),
            )

    def list_validations(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM ocr_validations WHERE run_id=? ORDER BY check_id",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_review(
        self,
        run_id: str,
        *,
        decision: str | None = None,
        reviewer: str | None = None,
        evidence: str = "",
        corrected_values: Mapping[str, Any] | str | None = None,
        signature: str = "",
        reviewed_at: str | None = None,
    ) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO ocr_reviews(
                    run_id, decision, reviewer, evidence, corrected_values_json, reviewed_at, signature
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(run_id) DO UPDATE SET
                    decision=excluded.decision,
                    reviewer=excluded.reviewer,
                    evidence=excluded.evidence,
                    corrected_values_json=excluded.corrected_values_json,
                    reviewed_at=excluded.reviewed_at,
                    signature=excluded.signature""",
                (
                    run_id,
                    decision,
                    reviewer,
                    evidence,
                    _json_dump(corrected_values or {}),
                    reviewed_at,
                    signature,
                ),
            )

    def get_review(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            return _row_dict(
                db.execute("SELECT * FROM ocr_reviews WHERE run_id=?", (run_id,)).fetchone()
            )

    def get_bundle(self, run_id: str) -> dict[str, Any] | None:
        run = self.get_run(run_id)
        if run is None:
            return None
        return {
            "run": run,
            "pages": self.list_pages(run_id),
            "blocks": self.list_blocks(run_id),
            "fields": self.list_fields(run_id),
            "validations": self.list_validations(run_id),
            "review": self.get_review(run_id),
        }

    def has_original_blob_column(self) -> bool:
        """ocr_* テーブルに原本BLOB列が無いことを検査する。"""
        with self.connect() as db:
            for table in OCR_TABLES:
                columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                if "original_data" in columns:
                    return True
        return False
