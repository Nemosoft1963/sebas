"""Convert approved TRIZ export items into learning/general values. No I/O besides verify/load."""
import hashlib
import json
from pathlib import Path

from app.triz_adapters import DOMAINS
from app.triz_common import FIELDS, validate_problem

# 自動生成であることを明示し、「人の確認済み」と読めない定型文。
DEFAULT_HUMAN_CHECKS = (
    '【自動生成】この文は取り込みスクリプトが機械的に付与した定型の確認項目であり、'
    '人が内容を確認済みであることや承認済みであることを意味しません。'
    '採用・実験の前に、出典URLおよび根拠引用が原本と完全一致すること、'
    '未実施の効果や検証を成功と述べていないことを、人が別途確認してください。'
)

EMPTY_PHYSICAL_STRINGS = frozenset({'', 'false'})


def normalize_physical(value):
    """None / 空 / False / "False" は空文字。通常の説明文はそのまま。切り詰めない。"""
    if value is None or value is False:
        return ''
    text = str(value).strip()
    if text.lower() in EMPTY_PHYSICAL_STRINGS:
        return ''
    return text


def source_url_from_evidence(evidence):
    """evidence 先頭付近の『出典URL: …』を取り出す。無ければ空文字。"""
    text = '' if evidence is None else str(evidence)
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('出典URL:'):
            return stripped.split(':', 1)[1].strip()
    return ''


def source_urls_from_sessions(sessions):
    """GET /general の sessions から、登録済み出典URLを集める。"""
    urls = set()
    for session in sessions or []:
        problem = session.get('problem') or {}
        url = source_url_from_evidence(problem.get('evidence', ''))
        if url:
            urls.add(url)
    return urls


def export_item_to_values(item, human_checks=DEFAULT_HUMAN_CHECKS, max_seconds=300):
    """エクスポート1件を POST .../learning/general の values に変換する。切り詰めない。"""
    if not isinstance(item, dict):
        raise ValueError('課題定義がオブジェクトではありません')
    domain = item.get('domain')
    if domain not in DOMAINS:
        raise ValueError('未知のdomainです: %r' % (domain,))
    values = {}
    for key in FIELDS:
        raw = item.get(key)
        if key == 'physical':
            values[key] = normalize_physical(raw)
        elif raw is None:
            values[key] = ''
        else:
            values[key] = str(raw)
    problem = validate_problem(values)
    human = str(human_checks or '').strip()
    if not human or len(human) > 3000:
        raise ValueError('人が確認する内容・合格基準を3000文字以内で指定してください')
    try:
        limit = float(max_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError('時間基準は1〜900秒です') from exc
    if not 1 <= limit <= 900:
        raise ValueError('時間基準は1〜900秒です')
    return {
        **problem,
        'domain': domain,
        'human_checks': human,
        'max_seconds': limit,
    }


def load_export_items(export_path):
    data = json.loads(Path(export_path).read_text(encoding='utf-8'))
    items = (data.get('export') or {}).get('items')
    if not isinstance(items, list):
        raise ValueError('export.items がありません')
    return items


def verify_export_file(export_path, meta_path):
    """メタの sha256 と実ファイルの SHA-256 が違えば ValueError。"""
    meta = json.loads(Path(meta_path).read_text(encoding='utf-8'))
    expected = str(meta.get('sha256') or '').strip().lower()
    if len(expected) != 64 or any(c not in '0123456789abcdef' for c in expected):
        raise ValueError('メタファイルのsha256が不正です')
    digest = hashlib.sha256(Path(export_path).read_bytes()).hexdigest()
    if digest != expected:
        raise ValueError('SHA-256が一致しません: ファイル=%s メタ=%s' % (digest, expected))
