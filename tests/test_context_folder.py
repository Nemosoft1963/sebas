import pytest

from app.context_files import normalize_context_filename


def test_context_folder_path_preserves_safe_relative_structure():
    assert normalize_context_filename("note.md", "project/docs/note.md") == "project/docs/note.md"
    assert normalize_context_filename("note.md", "project\\docs\\note.md") == "project/docs/note.md"
    assert normalize_context_filename("single.md") == "single.md"


@pytest.mark.parametrize("relative_path", [
    "../secret.md",
    "folder/../../secret.md",
    "/absolute/note.md",
    "C:/private/note.md",
])
def test_context_folder_path_rejects_unsafe_or_non_markdown_paths(relative_path):
    with pytest.raises(ValueError):
        normalize_context_filename("note.md", relative_path)
