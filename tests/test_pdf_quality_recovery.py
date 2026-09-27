from app.context_files import extraction_quality, reversible_pdf_candidate


def test_page_markers_are_not_readable_content():
    assert extraction_quality('[Page 1]\n') == 'unreadable'


def test_reversible_candidate_preserves_unrecoverable_lines():
    garbled = '車両番号'.encode('cp932').decode('latin1')
    original = garbled + '\n' + '\x81' + '\n123,456'
    candidate, count = reversible_pdf_candidate(original)
    assert count == 1
    assert candidate == '車両番号\n\x81\n123,456'
    assert extraction_quality(candidate) == 'garbled'
    assert extraction_quality(original) == 'garbled'


def test_clean_content_not_reinterpreted():
    text = '車両番号 1234 金額 5678'
    assert reversible_pdf_candidate(text) == (text, 0)
    assert extraction_quality(text) == 'readable'
