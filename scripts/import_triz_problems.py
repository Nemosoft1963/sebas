#!/usr/bin/env python3
"""Import approved TRIZ problem definitions through the existing HTTP API.

冪等の根拠:
  GET /api/projects/{pid}/agent-examples/{eid}/learning/general は
  sessions[].problem.evidence を返す（app/triz_general_api.py の get、
  create() が validate_problem 済みの problem を保存する）。
  evidence にはエクスポートの『出典URL: …』が残るので、同じURLがあれば
  POST せずスキップする。課題セッション自体に重複排除は無い。
  実行例は ExampleStore.import_text が同一本文ハッシュなら既存行を返す。
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.triz_import import (  # noqa: E402
    export_item_to_values,
    load_export_items,
    source_url_from_evidence,
    source_urls_from_sessions,
    verify_export_file,
)

DEFAULT_FILE = ROOT / 'inputs' / 'triz-problems-export.json'
DEFAULT_META = ROOT / 'inputs' / 'triz-problems-export.meta.json'
EXAMPLE_AGENT = 'triz-problems-import'
MAX_RETRIES = 20
RETRY_WAIT = 0.5


def build_parser():
    parser = argparse.ArgumentParser(description='承認済みTRIZ課題を learning/general へ取り込む')
    parser.add_argument('--base-url', default='http://127.0.0.1:8099')
    parser.add_argument('--project', required=True)
    parser.add_argument('--file', default=str(DEFAULT_FILE))
    parser.add_argument('--meta', default=str(DEFAULT_META))
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--limit', type=int, default=None)
    return parser


def detail_text(body):
    if not isinstance(body, dict):
        return str(body)
    detail = body.get('detail', body)
    if isinstance(detail, list):
        return json.dumps(detail, ensure_ascii=False)
    return str(detail)


def urllib_request(base_url, method, path, json_body=None, timeout=60):
    url = base_url.rstrip('/') + path
    data = None
    headers = {}
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json; charset=utf-8'
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            payload = json.loads(raw.decode('utf-8')) if raw else {}
            return resp.status, payload
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw.decode('utf-8')) if raw else {}
        except json.JSONDecodeError:
            payload = {'detail': raw.decode('utf-8', 'replace')}
        return exc.code, payload


def bind_request(base_url, request):
    if request is not None:
        return request

    def wrapped(method, path, json_body=None):
        return urllib_request(base_url, method, path, json_body)

    return wrapped


def item_label(index, item):
    goal = str((item or {}).get('goal') or '')[:60]
    return 'item[%d] %s' % (index, goal)


def convert_items(items):
    converted = []
    failed = []
    for index, item in enumerate(items):
        try:
            values = export_item_to_values(item)
        except (ValueError, TypeError) as exc:
            failed.append((item_label(index, item), str(exc)))
            continue
        converted.append((index, item, values))
    return converted, failed


def ensure_example(request, project, export_path):
    source = Path(export_path).read_text(encoding='utf-8')
    status, body = request('POST', '/api/projects/%s/agent-examples' % project, {
        'filename': Path(export_path).name,
        'source': source,
        'agent': EXAMPLE_AGENT,
    })
    if status != 200 or not isinstance(body, dict) or not body.get('id'):
        raise RuntimeError('実行例の作成に失敗しました (%s): %s' % (status, detail_text(body)))
    return body['id']


def fetch_general(request, project, eid):
    path = '/api/projects/%s/agent-examples/%s/learning/general' % (project, eid)
    status, body = request('GET', path, None)
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError('課題一覧の取得に失敗しました (%s): %s' % (status, detail_text(body)))
    return body


def post_general(request, project, eid, revision, values):
    path = '/api/projects/%s/agent-examples/%s/learning/general' % (project, eid)
    return request('POST', path, {'revision': revision, 'values': values})


def import_converted(request, project, eid, converted, sleep):
    success, skipped, failed = [], [], []
    snapshot = fetch_general(request, project, eid)
    known = source_urls_from_sessions(snapshot.get('sessions'))
    for index, item, values in converted:
        label = item_label(index, item)
        url = source_url_from_evidence(values.get('evidence', ''))
        if url and url in known:
            skipped.append((label, '同じ出典URLが登録済み: %s' % url))
            continue
        last_error = '未送信'
        posted = False
        for attempt in range(MAX_RETRIES):
            snapshot = fetch_general(request, project, eid)
            known |= source_urls_from_sessions(snapshot.get('sessions'))
            if url and url in known:
                skipped.append((label, '同じ出典URLが登録済み: %s' % url))
                posted = True
                break
            status, body = post_general(request, project, eid, snapshot.get('revision'), values)
            if status == 200:
                success.append(label)
                if url:
                    known.add(url)
                posted = True
                break
            if status == 409:
                last_error = detail_text(body)
                sleep(RETRY_WAIT)
                continue
            if status == 422:
                failed.append((label, '422 %s' % detail_text(body)))
                posted = True
                break
            last_error = '%s %s' % (status, detail_text(body))
            failed.append((label, last_error))
            posted = True
            break
        if not posted:
            failed.append((label, '409が続き打ち切り: %s' % last_error))
    return success, skipped, failed


def print_report(success, skipped, failed, dry_run=False):
    prefix = 'dry-run: ' if dry_run else ''
    print('%s成功 %d件' % (prefix, len(success)))
    print('%sスキップ %d件' % (prefix, len(skipped)))
    print('%s失敗 %d件' % (prefix, len(failed)))
    for label, reason in skipped:
        print('  スキップ: %s — %s' % (label, reason))
    for label, reason in failed:
        print('  失敗: %s — %s' % (label, reason))


def main(argv=None, request=None, sleep=time.sleep):
    args = build_parser().parse_args(argv)
    try:
        verify_export_file(args.file, args.meta)
        items = load_export_items(args.file)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print('停止: %s' % exc)
        return 1
    if args.limit is not None:
        if args.limit < 0:
            print('停止: --limit は0以上です')
            return 1
        items = items[:args.limit]
    converted, convert_failed = convert_items(items)
    if args.dry_run:
        print('変換結果 %d件（対象 %d件）' % (len(converted), len(items)))
        print_report([], [], convert_failed, dry_run=True)
        return 1 if convert_failed else 0
    sender = bind_request(args.base_url, request)
    try:
        eid = ensure_example(sender, args.project, args.file)
        success, skipped, failed = import_converted(sender, args.project, eid, converted, sleep)
    except (OSError, RuntimeError, ValueError, urllib.error.URLError) as exc:
        print('停止: %s' % exc)
        return 1
    failed = convert_failed + failed
    print_report(success, skipped, failed)
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
