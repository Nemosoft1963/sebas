"""原本ハッシュ・成果物 manifest・実行キー・差し替え可能なページ画像化（Phase 1）。"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from app.ocr_schema import OcrErrorCode, OcrIssue, OcrOutcome, follow_up_kind


ALLOWED_ARTIFACT_NAMES = frozenset({
    "ocr_result.json",
    "ocr_result.md",
    "validation.json",
    "manifest.json",
})
PAGE_IMAGE_RE = re.compile(r"^page-(\d+)\.png$")
PAGE_LAYOUT_RE = re.compile(r"^page-(\d+)-layout\.png$")

Renderer = Callable[[bytes, int], bytes]


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize_sha256(value: str) -> str:
    text = (value or "").strip().lower()
    if text.startswith("sha256:"):
        text = text[7:]
    return text


def verify_source_hash(original_bytes: bytes, expected_sha256: str) -> list[OcrIssue]:
    """VAL-01。不一致は OCR_SOURCE_HASH_MISMATCH（強制停止）。"""
    actual = sha256_hex(original_bytes)
    expected = _normalize_sha256(expected_sha256)
    if actual != expected:
        return [OcrIssue(
            code=OcrErrorCode.OCR_SOURCE_HASH_MISMATCH.value,
            message="原本 SHA-256 が一致しません",
            path="source.sha256",
        )]
    return []


def make_execution_key(original_sha256: str, engine_id: str, parameters_hash: str) -> str:
    """OPS-04: 同一の(原本sha256, モデル/エンジン識別, parameters_hash)は同一キー。"""
    payload = json.dumps(
        {
            "original_sha256": _normalize_sha256(original_sha256),
            "engine_id": str(engine_id),
            "parameters_hash": _normalize_sha256(parameters_hash),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256_hex(payload.encode("utf-8"))


def default_page_renderer(_original_bytes: bytes, _page: int) -> bytes:
    """PDF→PNG ライブラリが無い既定実装。呼び出し側は失敗として扱う。"""
    raise RuntimeError("OCR_PAGE_RENDER_FAILED")


def render_pdf_page(
    original_bytes: bytes,
    page: int,
    renderer: Renderer | None = None,
) -> OcrOutcome:
    """差し替え可能な renderer。既定は OCR_PAGE_RENDER_FAILED。"""
    if page < 1:
        return OcrOutcome(
            ok=False,
            error_code=OcrErrorCode.OCR_PAGE_RENDER_FAILED.value,
            message="page は 1 以上である必要があります",
        )
    fn = renderer or default_page_renderer
    try:
        png = fn(original_bytes, page)
    except Exception as exc:  # 既定実装・偽rendererの失敗は強制停止コードへ
        return OcrOutcome(
            ok=False,
            error_code=OcrErrorCode.OCR_PAGE_RENDER_FAILED.value,
            message=str(exc) or "ページ画像化に失敗しました",
        )
    if not isinstance(png, (bytes, bytearray)) or not png:
        return OcrOutcome(
            ok=False,
            error_code=OcrErrorCode.OCR_PAGE_RENDER_FAILED.value,
            message="ページ画像化の出力が空です",
        )
    return OcrOutcome(ok=True, data=bytes(png))


def artifact_name_allowed(name: str) -> bool:
    return (
        name in ALLOWED_ARTIFACT_NAMES
        or PAGE_IMAGE_RE.match(name) is not None
        or PAGE_LAYOUT_RE.match(name) is not None
    )


def resolve_artifact_path(root: Path, relative: str) -> Path:
    """成果物は root 配下に限定。`..` と絶対パスは拒否する。"""
    if not relative or not isinstance(relative, str):
        raise ValueError("成果物パスが空です")
    raw = relative.replace("\\", "/")
    if raw.startswith("/") or raw.startswith("\\") or (len(raw) >= 2 and raw[1] == ":"):
        raise ValueError("成果物の絶対パスは拒否します")
    parts = [part for part in raw.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("成果物パスのパストラバーサルは拒否します")
    if len(parts) != 1 or not artifact_name_allowed(parts[0]):
        raise ValueError("成果物ファイル名が許可されていません")
    base = root.resolve()
    target = (base / parts[0]).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise ValueError("成果物パスがプロジェクト配下にありません") from exc
    return target


def write_artifact(root: Path, relative: str, data: bytes) -> Path:
    path = resolve_artifact_path(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _file_entry(path: Path, generated_at: str) -> dict[str, object]:
    data = path.read_bytes()
    return {
        "name": path.name,
        "sha256": sha256_hex(data),
        "size": path.stat().st_size,
        "generated_at": generated_at,
    }


def generate_manifest(root: Path, filenames: list[str] | None = None) -> dict[str, object]:
    """ocr_result.json / .md / page-N.png / page-N-layout.png / validation.json の証跡。"""
    base = root.resolve()
    if not base.is_dir():
        raise ValueError("成果物ディレクトリがありません")
    generated_at = utcnow()
    if filenames is None:
        names = sorted(
            path.name for path in base.iterdir()
            if path.is_file() and artifact_name_allowed(path.name) and path.name != "manifest.json"
        )
    else:
        names = []
        for name in filenames:
            resolve_artifact_path(base, name)
            names.append(Path(name.replace("\\", "/")).name)
    files = []
    for name in names:
        path = resolve_artifact_path(base, name)
        if not path.is_file():
            raise FileNotFoundError(name)
        files.append(_file_entry(path, generated_at))
    manifest = {
        "schema_version": "ocr-fallback/v1-manifest",
        "generated_at": generated_at,
        "files": files,
    }
    write_artifact(base, "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    return manifest


def verify_manifest(root: Path, manifest: Mapping[str, object] | None = None) -> list[OcrIssue]:
    """改ざん・欠落は OCR_ARTIFACT_TAMPERED（強制停止）。"""
    base = root.resolve()
    if manifest is None:
        path = resolve_artifact_path(base, "manifest.json")
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return [OcrIssue(
                code=OcrErrorCode.OCR_ARTIFACT_TAMPERED.value,
                message=f"manifest を読めません: {exc}",
                path="manifest.json",
            )]
    files = manifest.get("files") if isinstance(manifest, Mapping) else None
    if not isinstance(files, list):
        return [OcrIssue(
            code=OcrErrorCode.OCR_ARTIFACT_TAMPERED.value,
            message="manifest.files が不正です",
            path="manifest.files",
        )]
    issues: list[OcrIssue] = []
    for index, entry in enumerate(files):
        path_key = f"manifest.files[{index}]"
        if not isinstance(entry, Mapping):
            issues.append(OcrIssue(
                code=OcrErrorCode.OCR_ARTIFACT_TAMPERED.value,
                message="成果物エントリが不正です",
                path=path_key,
            ))
            continue
        name = str(entry.get("name") or "")
        try:
            path = resolve_artifact_path(base, name)
        except ValueError as exc:
            issues.append(OcrIssue(
                code=OcrErrorCode.OCR_ARTIFACT_TAMPERED.value,
                message=str(exc),
                path=path_key,
            ))
            continue
        if not path.is_file():
            issues.append(OcrIssue(
                code=OcrErrorCode.OCR_ARTIFACT_TAMPERED.value,
                message="成果物ファイルがありません",
                path=name,
            ))
            continue
        data = path.read_bytes()
        actual_hash = sha256_hex(data)
        actual_size = path.stat().st_size
        expected_hash = _normalize_sha256(str(entry.get("sha256") or ""))
        expected_size = entry.get("size")
        if actual_hash != expected_hash or expected_size != actual_size:
            issues.append(OcrIssue(
                code=OcrErrorCode.OCR_ARTIFACT_TAMPERED.value,
                message="成果物ハッシュまたはサイズが一致しません",
                path=name,
            ))
    return issues


def force_stop_code(issues: list[OcrIssue]) -> str | None:
    for issue in issues:
        if follow_up_kind(issue.code).value == "force_stop":
            return issue.code
    return issues[0].code if issues else None
