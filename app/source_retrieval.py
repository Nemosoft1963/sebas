"""Bounded, immutable source units. Never fetches URLs or accepts filesystem paths."""
import io
import re
from app.upgrade_store import digest

MAX_TEXT = 200000
UNIT_CHARS = 900


def build_units(project_id, source):
    if source.get('project_id') != project_id:
        raise ValueError('Source belongs to a different project')
    if source.get('source') == 'memo':
        return None, []
    text = str(source.get('content') or '')[:MAX_TEXT]
    raw = bytes(source.get('original_data') or b'')
    # Legacy web original_data contains our capture Markdown, not HTTP response bytes.
    original_available = bool(raw) and source.get('source') != 'web'
    version_id = digest(project_id + '|' + source['id'] + '|' + digest(raw) + '|' + digest(text) + '|units-v1')
    version = {'version_id': version_id, 'source_doc_id': source['id'], 'filename': source.get('filename', ''),
               'original_available': original_available, 'original_sha256': digest(raw) if original_available else None,
               'extracted_sha256': digest(text), 'extractor_version': 'units-v1',
               'extraction_state': 'extracted' if text else 'extraction_failed',
               'raw_pdf_available': original_available and raw.startswith(b'%PDF'),
               'note': source.get('extraction_note', '')}
    if len(str(source.get('content') or '')) > MAX_TEXT or '先頭' in version['note']:
        version['extraction_state'] = 'extraction_partial'
    units = []
    for start in range(0, len(text), UNIT_CHARS):
        content = text[start:start + UNIT_CHARS]
        units.append({'unit_id': digest(version_id + ':' + str(start)), 'version_id': version_id,
                      'source_doc_id': source['id'], 'project_id': project_id,
                      'filename': source.get('filename', ''), 'locator': {'kind': 'extracted_text', 'start': start, 'end': start + len(content)},
                      'content': content, 'sha256': digest(content)})
    return version, units


def extract_pdf_pages(project_id, source, pages):
    if source.get('project_id') != project_id or source.get('source') == 'memo':
        raise ValueError('Source scope rejected')
    raw = bytes(source.get('original_data') or b'')
    if not raw.startswith(b'%PDF') or source.get('source') == 'web':
        raise ValueError('original_not_available: saved text is not a PDF original')
    if len(raw) > 20 * 1024 * 1024 or not isinstance(pages, list) or not 1 <= len(pages) <= 20:
        raise ValueError('PDF read bounds exceeded')
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(raw), strict=False)
    if any(type(p) is not int or p < 1 or p > min(500, len(reader.pages)) for p in pages):
        raise ValueError('Invalid page selector')
    result = []
    for page in pages:
        text = reader.pages[page - 1].extract_text() or ''
        result.append({'page': page, 'text': text[:20000], 'partial': len(text) > 20000,
                       'state': 'extracted' if text.strip() else 'extraction_failed'})
    return result


def select_units(units, query, max_chars=12000):
    if not 1 <= max_chars <= 20000:
        raise ValueError('Context budget outside safe bounds')
    terms = [x for x in ('補助率', '上限', '対象経費', '事例', '構成', '点呼', '年休', 'システム', '承認', '手順', '期限') if x in query]
    terms += re.findall(r'[A-Za-z][A-Za-z0-9_]{2,24}', query)[:20]
    priority_terms = [t for t in ('ものづくり','補助金','公募要領') if t in query]
    def score(unit):
        domain = 3 * sum(min(3, unit['content'].count(t)) for t in priority_terms)
        return domain + sum(min(3, unit['content'].lower().count(t.lower())) for t in terms)
    ranked = sorted(units, key=lambda u: (-score(u), u['source_doc_id'], u['locator']['start']))
    selected, used, counts = [], 0, {}
    for unit in ranked:
        # Each source can contribute several ranked excerpts instead of only its prefix.
        if counts.get(unit['source_doc_id'], 0) >= 4:
            continue
        size = len(unit_block(unit))
        if used + size > max_chars:
            continue
        selected.append(unit)
        used += size
        counts[unit['source_doc_id']] = counts.get(unit['source_doc_id'], 0) + 1
    return selected


def unit_block(unit):
    return (f"<<SOURCE_UNIT {unit['unit_id']} SHA256={unit['sha256']}>>\n"
            f"ref=context:{unit['source_doc_id']}\n" + unit['content']
            + f"\n<<END_SOURCE_UNIT {unit['unit_id']}>>")


def render_units(units):
    return '\n\n'.join(unit_block(unit) for unit in units)


def presentation_manifest(units, messages):
    contents = [str(x.get('content', '')) for x in messages]
    result = []
    for unit in units:
        block = unit_block(unit)
        if any(block in text for text in contents):
            state = 'sent'
        elif any(unit['unit_id'] in text for text in contents):
            state = 'truncated'
        elif any(unit['content'] in text for text in contents):
            state = 'visibility_unknown'
        else:
            state = 'omitted'
        result.append({'unit_id': unit['unit_id'], 'version_id': unit['version_id'],
                       'source_doc_id': unit['source_doc_id'], 'locator': unit['locator'],
                       'sha256': unit['sha256'], 'state': state})
    return result