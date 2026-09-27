from __future__ import annotations

import io
import mimetypes
import re
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

MAX_EXTRACTED_CHARS = 200_000
MAX_OFFICE_XML_BYTES = 50 * 1024 * 1024
TEXT_EXTENSIONS = {
    ".md", ".txt", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".xml",
    ".html", ".htm", ".log", ".ini", ".conf", ".cfg", ".toml", ".rtf", ".eml",
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".css", ".scss",
    ".sql", ".ps1", ".sh", ".bat", ".cmd", ".java", ".c", ".h", ".cpp", ".hpp",
    ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".vue", ".svelte",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg"}
AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".wma"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".m4v"}
ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz"}


@dataclass(slots=True)
class ContextExtraction:
    content: str
    file_kind: str
    mime_type: str
    note: str
    quality: str | None = None
    page_count: int | None = None
    requires_ocr: bool | None = None
    ocr_reason_codes: list[str] | None = None
    extraction_run_id: str | None = None


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())


def normalize_context_filename(upload_name: str, relative_path: str = "") -> str:
    raw = (relative_path.strip() or upload_name.strip()).replace("\\", "/")
    if not raw or raw.startswith("/") or any(ord(char) < 32 for char in raw):
        raise ValueError("Context file path is invalid")
    parts = [part for part in raw.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts) or ":" in parts[0]:
        raise ValueError("Context file path must stay inside the selected folder")
    normalized = "/".join(parts)
    if len(normalized) > 500:
        raise ValueError("Context file path must be 500 characters or fewer")
    return normalized


def _limit(text: str, note: str) -> tuple[str, str]:
    text = text.replace("\x00", "").strip()
    if len(text) <= MAX_EXTRACTED_CHARS:
        return text, note
    return text[:MAX_EXTRACTED_CHARS], (
        note + f" 先頭{MAX_EXTRACTED_CHARS:,}文字だけをコンテキスト化しました。"
    ).strip()


def _decode(data: bytes) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "utf-8", "cp932", "shift_jis"):
        try:
            return data.decode(encoding), f"文字コード: {encoding}"
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "不正な文字を置換して抽出しました。"


def _xml_tokens(xml_data: bytes, element_names: set[str]) -> list[str]:
    root = ElementTree.fromstring(xml_data)
    return [
        node.text.strip() for node in root.iter()
        if node.tag.rsplit("}", 1)[-1] in element_names and node.text and node.text.strip()
    ]


def _office_text(data: bytes, pattern: str, label: str, names: set[str]) -> str:
    sections = []
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = sorted(
            info for info in archive.infolist() if re.match(pattern, info.filename)
        )
        for info in entries:
            total += info.file_size
            if total > MAX_OFFICE_XML_BYTES:
                raise ValueError("Office文書の展開後サイズが上限を超えています")
            tokens = _xml_tokens(archive.read(info), names)
            if tokens:
                sections.append(f"[{label}: {info.filename}]\n" + "\n".join(tokens))
    return "\n\n".join(sections)


def extraction_quality(text: str) -> str:
    body = re.sub(r"\[Page \d+\]", "", text).strip()
    if not body:
        return "unreadable"
    if re.search(r"[\u0080-\u009f\ufffd]", body):
        return "garbled"
    return "readable"


def reversible_pdf_candidate(text: str) -> tuple[str, int]:
    """Return a separate candidate; never replace or silently drop original bytes."""
    lines = []
    repaired = 0
    for line in text.splitlines():
        if re.search(r"[\u0080-\u009f]", line):
            try:
                raw = line.encode('latin1')
                candidate = raw.decode('cp932')
                if candidate.encode('cp932') == raw and extraction_quality(candidate) == 'readable':
                    line = candidate
                    repaired += 1
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
        lines.append(line)
    return "\n".join(lines), repaired


def _pdf_text(data: bytes) -> tuple[str, str]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return "", "PDF原本を保存しました。PDF文字抽出ライブラリが未導入です。"
    reader = PdfReader(io.BytesIO(data), strict=False)
    pages = [
        f"[Page {index}]\n{page.extract_text() or ''}"
        for index, page in enumerate(reader.pages[:500], 1)
    ]
    note = f"PDF {len(reader.pages)}ページから文字を抽出しました。"
    if len(reader.pages) > 500:
        note += " 先頭500ページまでを対象にしました。"
    text = "\n\n".join(pages)
    quality = extraction_quality(text)
    if quality != 'readable':
        note += " 読取品質: " + quality + "。原本との照合前は計算根拠に使用できません。"
        candidate, count = reversible_pdf_candidate(text)
        if count:
            note += f" 可逆復元候補 {count}行（未検証・原抽出保持）。"
            text += "\n\n[未検証の文字コード復元候補: 原本照合が必要]\n" + candidate
    return text, note


def extract_context_file(filename: str, data: bytes, mime_type: str = "") -> ContextExtraction:
    extension = Path(filename).suffix.lower()
    detected = mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    try:
        if extension in TEXT_EXTENSIONS or detected.startswith("text/"):
            text, note = _decode(data)
            if extension in {".html", ".htm"}:
                parser = _HTMLText()
                parser.feed(text)
                text = "\n".join(parser.parts)
                note += " HTMLタグを除去しました。"
            elif extension == ".xml":
                try:
                    text = "\n".join(_xml_tokens(data, {"t", "v", "name", "value"}))
                    note += " XML要素から文字を抽出しました。"
                except ElementTree.ParseError:
                    note += " XML解析に失敗したため原文を使用しました。"
            content, note = _limit(text, note)
            return ContextExtraction(
                content, "markdown" if extension == ".md" else "text", detected, note
            )
        if extension == ".pdf":
            text, note = _pdf_text(data)
            content, note = _limit(text, note)
            return ContextExtraction(
                content, "pdf", detected, note,
                quality=extraction_quality(content),
            )
        if extension in {".xlsx", ".xlsm"}:
            from app.yayoi_accounting import analyze_yayoi_workbook, workbook_to_markdown

            analysis = analyze_yayoi_workbook(filename, data)
            content, note = _limit(
                workbook_to_markdown(analysis),
                f"{analysis['label']}としてセル内容を抽出しました。{analysis['note']}",
            )
            return ContextExtraction(content, analysis["kind"], detected, note)
        office = {
            ".docx": (r"word/(document|header\d+|footer\d+)\.xml$", "Word", {"t"}),
            ".pptx": (r"ppt/slides/slide\d+\.xml$", "PowerPoint", {"t"}),
        }
        if extension in office:
            pattern, label, names = office[extension]
            text = _office_text(data, pattern, label, names)
            content, note = _limit(text, f"{label}文書から文字を抽出しました。")
            kinds = {".docx": "document", ".pptx": "presentation"}
            return ContextExtraction(content, kinds[extension], detected, note)
    except (ValueError, KeyError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        return ContextExtraction("", "binary", detected, f"原本を保存しました。文字抽出エラー: {exc}")
    if extension in IMAGE_EXTENSIONS:
        kind = "image"
    elif extension in AUDIO_EXTENSIONS:
        kind = "audio"
    elif extension in VIDEO_EXTENSIONS:
        kind = "video"
    elif extension in ARCHIVE_EXTENSIONS:
        kind = "archive"
    else:
        kind = "binary"
    return ContextExtraction("", kind, detected, "原本を保存しました。自動文字抽出は未対応です。")
