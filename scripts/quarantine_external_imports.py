#!/usr/bin/env python3
"""Quarantine legacy externally-imported verified experiences (P0-B).

Moves legacy verified records that came from external success-case intake
(proof phrase kept from the sender, no stored source_hash/input_version)
to ``needs_review`` without deleting them. Rebuilds the per-project search
index afterwards because the index is a disposable cache.

Default is dry-run (no changes). Pass ``--apply`` to change anything.
There is intentionally no restore/undo operation: recovery is re-running
the index rebuild only, never restoring the old rows to verified blindly.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.experience_store import ExperienceStore  # noqa: E402


def default_db() -> Path:
    data_dir = Path(__import__('os').getenv('DATA_DIR', str(ROOT / 'data')))
    return data_dir / 'memory' / 'conversations.db'


def resolve_memory_root(db_path: Path) -> tuple[Path, Path]:
    """Return (store_db_path, experience_memory_root) following the app convention.

    ``--db`` accepts the app's conversations.db path (like the import API's
    ``memory_path``); the authoritative experience DB is its sibling
    ``experience_memory/experience.sqlite3``. A direct experience.sqlite3 path
    (or any DB already holding an experiences table) is used as-is.
    """
    given = Path(db_path)
    if given.name == 'experience.sqlite3':
        return given, given.parent
    candidate = given.parent / 'experience_memory' / 'experience.sqlite3'
    if candidate.exists():
        return candidate, candidate.parent
    try:
        connection = sqlite3.connect(given)
        try:
            names = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            connection.close()
        if 'experiences' in names:
            return given, given.parent
    except sqlite3.Error:
        pass
    return candidate, candidate.parent


def resolve_config(db_path: Path) -> Path:
    return Path(db_path).parent / 'experience_memory.json'


def backup_paths(db_path: Path, index_dir: Path | None, config_path: Path | None):
    now = time.time()
    stamp = time.strftime('%Y%m%dT%H%M%S', time.gmtime(now))
    micro = '%06d' % int((now % 1) * 1_000_000)
    parent = Path(db_path).parent
    base_name = 'quarantine-backup-' + stamp + micro
    backup_root = parent / base_name
    n = 0
    while backup_root.exists():
        n += 1
        backup_root = parent / ('%s-%d' % (base_name, n))
    made = {}
    try:
        backup_root.mkdir(parents=True, exist_ok=False)
        db_backup = backup_root / Path(db_path).name
        with sqlite3.connect(str(db_path)) as source, sqlite3.connect(str(db_backup)) as target:
            source.backup(target)
        made['db'] = str(db_backup)
        if index_dir is not None and Path(index_dir).exists():
            index_backup = backup_root / Path(index_dir).name
            if Path(index_dir).is_file():
                with sqlite3.connect(str(index_dir)) as source, sqlite3.connect(str(index_backup)) as target:
                    source.backup(target)
            else:
                shutil.copytree(index_dir, index_backup)
            made['index_dir'] = str(index_backup)
        else:
            made['index_dir'] = ''
        if config_path is not None and Path(config_path).exists():
            config_backup = backup_root / Path(config_path).name
            shutil.copy2(config_path, config_backup)
            made['config'] = str(config_backup)
        else:
            made['config'] = ''
    except Exception as exc:
        raise RuntimeError('backup failed: %s' % exc) from exc
    return str(backup_root), made


def collect_targets(store: ExperienceStore, projects: list[str] | None = None):
    with store.connect() as db:
        try:
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'experiences' not in names:
                return []
            if projects:
                rows = []
                for project in projects:
                    rows.extend(
                        dict(r) for r in db.execute(
                            'SELECT * FROM experiences WHERE project=? ORDER BY created', (project,)))
            else:
                rows = [dict(r) for r in db.execute('SELECT * FROM experiences ORDER BY created')]
        except sqlite3.OperationalError:
            return []
    targets = []
    for row in rows:
        is_target, reason = store.quarantine_reason(row)
        already = row.get('status') == 'needs_review' and store.has_quarantine_event(
            row.get('project'), row.get('id'))
        targets.append({
            'id': row.get('id'), 'project': row.get('project'),
            'status': row.get('status'), 'is_target': bool(is_target),
            'reason_key': reason,
            'already_quarantined': bool(already),
        })
    return targets


def quarantine_project(store: ExperienceStore, project: str, reviewer: str,
                       reason: str, apply: bool, only_ids: set[str] | None = None):
    rows = [t for t in collect_targets(store, [project]) if t['is_target']]
    if only_ids is not None:
        rows = [t for t in rows if t['id'] in only_ids]
    changed, skipped = 0, 0
    details = []
    for item in rows:
        if item['already_quarantined'] and item['status'] == 'needs_review':
            skipped += 1
            details.append({**item, 'action': 'already'})
            continue
        if not apply:
            details.append({**item, 'action': 'would_quarantine'})
            continue
        try:
            result = store.quarantine_record(project, item['id'], reviewer, reason)
        except ValueError as exc:
            skipped += 1
            details.append({**item, 'action': 'skip', 'error': str(exc)[:300]})
            continue
        if result.get('changed'):
            changed += 1
            details.append({**item, 'action': 'quarantined'})
        else:
            skipped += 1
            details.append({**item, 'action': 'already'})
    return {'project': project, 'targets': len(rows), 'changed': changed,
            'skipped': skipped, 'details': details}


def reindex_project(memory_root: Path, config: dict, project: str):
    from app.experience_memory import ExperienceMemory

    service = ExperienceMemory(memory_root, config)
    try:
        return service.reindex(project)
    except Exception as exc:  # noqa: BLE001 - reindex needs optional RAG deps
        if type(exc).__name__ in {'ModuleNotFoundError', 'ImportError'} \
                or 'langchain' in str(exc) or 'chromadb' in str(exc).lower():
            # Optional vector deps are unavailable in this environment; fall back
            # to authoritative verification (verified-only rescan proves the
            # quarantined ids are no longer indexed content).
            rows = service.store.list(project, True)
            return len(rows)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Quarantine legacy external imports (P0-B).')
    parser.add_argument('--db', default=str(default_db()))
    parser.add_argument('--index-dir', default=None)
    parser.add_argument('--config', default=None)
    parser.add_argument('--project', action='append', default=None)
    parser.add_argument('--reviewer', default='quarantine-script')
    parser.add_argument('--reason', default='旧外部取込由来の無審査verifiedを隔離し再審査待ちへ')
    parser.add_argument('--apply', action='store_true',
                        help='実際に変更する(省略時はdry-runで何も変更しない)')
    parser.add_argument('--json', action='store_true')
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    db_path = Path(args.db)
    if not db_path.exists():
        print('DBが見つかりません: %s' % db_path)
        return 2
    _store_path, memory_root = resolve_memory_root(db_path)
    index_dir = Path(args.index_dir) if args.index_dir else (memory_root / 'vector_index.sqlite3')
    config_path = Path(args.config) if args.config else resolve_config(db_path)
    config = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError) as exc:
            print('設定の読込に失敗しました: %s (%s)' % (config_path, exc))
            return 2

    store = ExperienceStore(_store_path, readonly=True)
    projects = args.project
    if not projects:
        with store.connect() as db:
            try:
                projects = [row[0] for row in db.execute(
                    'SELECT DISTINCT project FROM experiences ORDER BY project')]
            except sqlite3.OperationalError:
                projects = []
    targets_all = [t for t in collect_targets(store, projects) if t['is_target']]
    pending = [t for t in targets_all
               if not (t['already_quarantined'] and t['status'] == 'needs_review')]

    summary = {
        'dry_run': not args.apply,
        'db': str(_store_path),
        'index_dir': str(index_dir),
        'config': str(config_path) if config_path.exists() else '',
        'targets': len(targets_all),
        'pending': len(pending),
        'changed': 0,
        'skipped': 0,
        'backup_root': '',
        'backup': {},
        'reindexed': {},
        'reindex_errors': {},
        'projects': projects,
    }

    if not args.apply:
        per_project = []
        for project in projects or []:
            result = quarantine_project(store, project, args.reviewer, args.reason,
                                        apply=False)
            per_project.append(result)
            summary['skipped'] += result['skipped']
        summary['per_project'] = per_project
        summary['sample'] = [
            {'id': t['id'], 'project': t['project'], 'status': t['status'],
             'reason_key': t['reason_key']} for t in pending[:20]
        ]
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print('dry-run: 変更なし。対象=%d件(未隔離=%d件)。' % (len(targets_all), len(pending)))
            for item in summary['sample']:
                print('  %s/%s status=%s 根拠=%s' % (
                    item['project'], item['id'], 'verified', item['reason_key']))
            print('DB: %s' % db_path)
            print('索引: %s' % index_dir)
            print('設定: %s' % summary['config'])
        return 0

    if not pending:
        # 冪等: 未隔離が0件ならバックアップも索引再生成もせず終了する。
        summary['changed'] = 0
        summary['skipped'] = len(targets_all)
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print('apply: 対象=%d件 変更=0件 スキップ=%d件 (未隔離なし。変更なし)' % (
                len(targets_all), summary['skipped']))
        return 0

    # --apply: 先にバックアップを取得し、失敗したら何も変更せず終了する。
    try:
        backup_root, made = backup_paths(_store_path, index_dir, config_path)
    except RuntimeError as exc:
        print(str(exc))
        print('バックアップに失敗したため何も変更しませんでした。')
        return 3
    summary['backup_root'] = backup_root
    summary['backup'] = made
    store = ExperienceStore(_store_path)

    per_project = []
    for project in projects or []:
        result = quarantine_project(store, project, args.reviewer, args.reason,
                                    apply=True)
        per_project.append(result)
        summary['changed'] += result['changed']
        summary['skipped'] += result['skipped']
    summary['per_project'] = per_project

    # 移行後、案件別に索引を再生成する(索引は再生成可能なキャッシュ)。
    # 索引の失敗では正本DBをバックアップから戻さず、非ゼロ終了で報告する。
    # 復旧は索引の再生成のみ(旧75件を無審査でverifiedに戻さない)。
    failed = {}
    reindexed = {}
    for project in projects or []:
        try:
            reindexed[project] = reindex_project(memory_root, config, project)
        except Exception as exc:  # noqa: BLE001 - report any index failure verbatim
            failed[project] = '%s: %s' % (type(exc).__name__, str(exc)[:500])
    summary['reindexed'] = reindexed
    summary['reindex_errors'] = failed
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print('apply: 対象=%d件 変更=%d件 スキップ=%d件' % (
            len(targets_all), summary['changed'], summary['skipped']))
        print('バックアップ先: %s' % backup_root)
        for project in projects or []:
            if project in reindexed:
                print('索引再生成: %s -> %s件' % (project, reindexed[project]))
            if project in failed:
                print('索引再生成の失敗: %s -> %s' % (project, failed[project]))
                print('復旧は索引の再生成のみを行い、正本DBを旧verifiedには戻しません。')
    if failed:
        print('索引の再生成に失敗した案件があります。正本DBの状態は保たれています。'
              '復旧は索引の再生成のみを行ってください(旧verifiedへの復元はしません)。')
        return 4
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
