import io
import zipfile

import pytest

from app.context_files import extract_context_file, normalize_context_filename
from app.memory.short_term import ShortTermMemory


def test_safe_relative_path_accepts_non_markdown_references():
    assert normalize_context_filename(
        "image.png", "evidence/images/image.png"
    ) == "evidence/images/image.png"


def test_extracts_text_with_legacy_japanese_encoding():
    extracted = extract_context_file(
        "reference.csv", "項目,値\n部署,開発".encode("cp932")
    )
    assert extracted.file_kind == "text"
    assert "部署,開発" in extracted.content
    assert "cp932" in extracted.note


def test_office_document_text_is_extracted_without_external_processes():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="urn:test"><w:body><w:p><w:r>'
            '<w:t>調査対象の本文</w:t></w:r></w:p></w:body></w:document>',
        )
    extracted = extract_context_file("evidence.docx", data.getvalue())
    assert extracted.file_kind == "document"
    assert "調査対象の本文" in extracted.content


@pytest.mark.parametrize("filename,expected_kind", [
    ("photo.png", "image"),
    ("interview.wav", "audio"),
    ("recording.mp4", "video"),
    ("materials.zip", "archive"),
    ("unknown.bin", "binary"),
])
def test_binary_reference_is_classified(filename, expected_kind):
    extracted = extract_context_file(filename, b"binary-data")
    assert extracted.file_kind == expected_kind
    assert extracted.content == ""
    assert "原本を保存" in extracted.note


def test_original_binary_and_generated_memo_are_stored_and_downloadable(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("調査資料")
    original = b"\x89PNG\r\nreference"
    saved = memory.add_context_file(
        project["id"], "evidence/photo.png", "", len(original),
        data=original, mime_type="image/png", file_kind="image",
        extraction_note="原本を保存しました。", sha256="test",
    )
    memory.upsert_context_memo(
        project["id"], "__project_memory__/00_goal.md", "# 目標\n\n確認する",
    )

    rows = memory.list_context_files(project["id"])
    assert rows[0]["source"] == "memo"
    assert rows[1]["file_kind"] == "image"
    loaded = memory.get_context_file(project["id"], saved["id"])
    assert loaded["original_data"] == original
    assert loaded["filename"] == "evidence/photo.png"
