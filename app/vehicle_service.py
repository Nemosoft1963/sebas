"""Project-local extraction preparation and narrowly scoped human decisions."""
from app.vehicle_workflow import (
    load_input, resolve, read_json, write_json, digest, sources,
    requirements_hash, INPUT_PATH, AllocationRuleError, make_allocation_rule,
    validate_allocation_rule, APPLY_THIS_MONTH, APPLY_DATE_RANGE, subject_key_of,
)
from app.vehicle_auto import drop_assumed_zero_when_actual, compact, current_ocr_adoption_signature


def state(manager, pid):
    envelope = load_input(manager, pid)
    if not envelope or envelope.get('mode') != 'vehicle-auto-v1':
        return {'prepared': False, 'questions': []}
    data = envelope['data']
    groups = {}
    for issue in data['auto_extraction']['issues']:
        if issue['status'] != 'unresolved':
            continue
        if issue.get('kind') == 'read':
            continue
        key = issue.get('group', issue['id'])
        group = groups.setdefault(key, dict(
            id=issue['id'], kind=issue['kind'], message=issue['message'],
            source_ref=issue['source_ref'], locator=issue['locator'],
            candidates=issue.get('candidates', []), amount=0, count=0,
            month=issue.get('month') or '', allocation_subject=issue.get('allocation_subject') or '',
        ))
        group['amount'] += issue.get('amount', 0)
        group['count'] += 1
        if issue.get('month') and not group.get('month'):
            group['month'] = issue['month']
        if issue.get('allocation_subject') and not group.get('allocation_subject'):
            group['allocation_subject'] = issue['allocation_subject']
    return dict(
        prepared=True, input_hash=digest(envelope), vehicles=len(data['vehicles']), months=len(data['months']),
        actual_records=sum(r['quality'] == 'actual' for r in data['records']),
        assumed_zeros=sum(r['quality'] == 'assumed_zero' for r in data['records']),
        unallocated=sum(r['amount'] for r in data['records'] if not r.get('allocations')),
        questions=list(groups.values()),
        vehicle_choices=[dict(id=v['id'], company=v['company']) for v in data['vehicles']],
        recoveries=data['auto_extraction']['recoveries'],
        stale=envelope.get('extractor_revision') != __import__('app.vehicle_auto', fromlist=['REVISION']).REVISION
        or envelope['source_hash'] != digest(sources(manager, pid))
        or envelope.get('requirements_hash') != requirements_hash(manager.memory.get_mission(pid))
        or envelope.get('ocr_adoption_hash', '') != current_ocr_adoption_signature(manager, pid),
    )


def idle_current(manager, pid, version, input_hash=None):
    mission = manager.memory.get_mission(pid)
    if mission['status'] == 'running' or pid in manager.planning_projects or mission['plan_version'] != version:
        raise ValueError('実行中・計画生成中または計画版が変わりました')
    envelope = load_input(manager, pid)
    if input_hash is not None and (not envelope or digest(envelope) != input_hash):
        raise ValueError('計算入力が変わりました。更新してください')
    return mission, envelope


def _normalize_allocations(vehicle_id, allocations, exclude):
    if exclude:
        return []
    if allocations:
        return [{'vehicle_id': item['vehicle_id'], 'ratio': item['ratio']} for item in allocations]
    if vehicle_id:
        return [{'vehicle_id': vehicle_id, 'ratio': 1}]
    raise ValueError('配賦先の車両を指定してください')


def _apply_months(issue, records, apply_to, date_from, date_to):
    months = sorted({r.get('month') for r in records if r.get('month')})
    issue_month = issue.get('month') or (months[0] if months else None)
    if apply_to in (None, '', APPLY_THIS_MONTH):
        target = issue_month
        return [target] if target else months[:1]
    if apply_to == APPLY_DATE_RANGE:
        start = date_from or issue_month
        end = date_to or issue_month
        if not start or not end:
            raise ValueError('期間指定には開始月と終了月が必要です')
        return [m for m in months if start <= m <= end] or ([issue_month] if issue_month else [])
    raise ValueError('apply_to は this_month または date_range です')


def _subject_of(issue, records):
    employee = compact(issue.get('allocation_subject') or '')
    company = next((r.get('company') for r in records if r.get('company')), '') or ''
    return dict(company=company, employee=employee)


def decide(manager, pid, version, input_hash, issue_id, vehicle_id, exclude, reason,
           apply_to=APPLY_THIS_MONTH, allocations=None, date_from=None, date_to=None):
    mission, envelope = idle_current(manager, pid, version, input_hash)
    if not envelope or envelope.get('mode') != 'vehicle-auto-v1':
        raise ValueError('自動抽出を先に実行してください')
    if len(reason.strip()) < 3 or len(reason) > 1000:
        raise ValueError('選択の理由を入力してください')
    if envelope['source_hash'] != digest(sources(manager, pid)) or envelope['requirements_hash'] != requirements_hash(mission):
        raise ValueError('原本または要求が変わりました。再抽出してください')
    data = envelope['data']
    issue = next((x for x in data['auto_extraction']['issues'] if x['id'] == issue_id and x['status'] == 'unresolved'), None)
    if not issue or issue['kind'] not in ('allocation', 'adapter', 'insurance_basis'):
        raise ValueError('配賦または原本の確認事項を選択してください')
    if issue['kind'] == 'adapter' and not exclude:
        raise ValueError('原本の対象外判断には理由と除外指定が必要です')
    apply_to = apply_to or APPLY_THIS_MONTH
    employee = compact(issue.get('allocation_subject') or '')
    same_subject = [
        x for x in data['auto_extraction']['issues']
        if x['status'] == 'unresolved'
        and x.get('kind') == issue['kind']
        and compact(x.get('allocation_subject') or '') == employee
    ]
    if issue['kind'] == 'allocation' and employee:
        selected = [
            x for x in same_subject
            if (x.get('month') or '') == (issue.get('month') or '')
        ]
        if apply_to == APPLY_DATE_RANGE:
            start = date_from or issue.get('month')
            end = date_to or issue.get('month')
            selected = [x for x in same_subject if x.get('month') and start <= x['month'] <= end]
        if issue not in selected:
            selected = [issue] + [x for x in selected if x['id'] != issue['id']]
    else:
        selected = [issue] if issue['kind'] != 'adapter' else [
            x for x in data['auto_extraction']['issues']
            if x.get('group', x['id']) == issue.get('group', issue['id']) and x['status'] == 'unresolved'
        ]
    ids = {x['record_id'] for x in selected if x.get('record_id')}
    records = [r for r in data['records'] if r['id'] in ids]
    if issue['kind'] == 'allocation' and employee:
        extra = [
            r for r in data['records']
            if compact(r.get('allocation_subject') or '') == employee
            and r.get('month') == issue.get('month')
            and not r.get('allocations')
            and r['id'] not in ids
        ]
        records.extend(extra)
        ids.update(r['id'] for r in extra)
    target_months = _apply_months(issue, records, apply_to, date_from, date_to)
    records = [r for r in records if r.get('month') in target_months] if target_months else records
    ids = {r['id'] for r in records}
    selected = [x for x in selected if not x.get('record_id') or x.get('record_id') in ids] or [issue]
    allocs = _normalize_allocations(vehicle_id, allocations, exclude)
    if not exclude:
        try:
            rule = make_allocation_rule(
                subject_key=_subject_of(issue, records),
                effective_from=min(target_months) if target_months else issue.get('month'),
                effective_to=max(target_months) if target_months else issue.get('month'),
                allocations=allocs, reason=reason, source_hash=envelope['source_hash'],
                created_by='human',
            )
            validate_allocation_rule(rule, data['vehicles'], envelope.get('assignments') or [], records)
        except AllocationRuleError:
            raise
        vehicle_ids = {a['vehicle_id'] for a in allocs}
        vehicles = [v for v in data['vehicles'] if v['id'] in vehicle_ids]
        if len(vehicles) != len(vehicle_ids):
            raise AllocationRuleError('配賦先の車両が対象集合にないか重複しています')
        if any(r['company'] not in ('未特定', *(v['company'] for v in vehicles)) for r in records):
            raise AllocationRuleError('配賦先の会社が明細と一致しません')
    write_json(resolve(manager, pid, 'vehicle_profit/history/' + digest(envelope) + '.json'), envelope)
    for r in records:
        if exclude:
            data.setdefault('excluded_records', []).append(dict(r, exclusion_reason=reason))
        else:
            r['allocations'] = [dict(vehicle_id=a['vehicle_id'], ratio=a['ratio']) for a in allocs]
            r['allocation_evidence'] = reason
    if exclude:
        data['records'] = [r for r in data['records'] if r['id'] not in ids]
    if issue['kind'] == 'adapter':
        if any(r['source_ref'] == issue['source_ref'] for r in data['records']):
            raise ValueError('抽出済み明細のある原本は一括除外できません')
        for d in data['source_dispositions']:
            if d['source_ref'] == issue['source_ref']:
                d.update(status='excluded', evidence=reason)
        data.setdefault('source_decisions', []).append(dict(source_ref=issue['source_ref'], reason=reason))
    for x in selected:
        x.update(status='resolved', resolution='outside_vehicle_scope' if exclude else (vehicle_id or allocs), reason=reason)
    decision = dict(
        issue_ids=[x['id'] for x in selected], vehicle_id=vehicle_id, exclude=exclude, reason=reason,
        source_hash=envelope['source_hash'], apply_to=apply_to, allocations=allocs,
        months=target_months, subject_key=_subject_of(issue, records),
    )
    envelope.setdefault('decisions', []).append(decision)
    if not exclude:
        rule = make_allocation_rule(
            subject_key=decision['subject_key'],
            effective_from=min(target_months) if target_months else issue.get('month'),
            effective_to=max(target_months) if target_months else issue.get('month'),
            allocations=allocs, reason=reason, source_hash=envelope['source_hash'],
            created_by='human', decision_id=len(envelope['decisions']) - 1,
        )
        envelope.setdefault('assignments', []).append(rule)
        data.setdefault('assignments', []).append(rule)
    log = data.setdefault('decision_log', [])
    log.append(dict(action='decide', decision=decision))
    removed = drop_assumed_zero_when_actual(data['records'], log, allocated_only=True)
    if removed:
        log.append(dict(action='drop_assumed_zero_after_allocation', count=len(removed)))
    write_json(resolve(manager, pid, INPUT_PATH), envelope)
    write_json(
        resolve(manager, pid, 'vehicle_profit/automatic_proof.json'),
        dict(input_hash=digest(envelope), source_hash=envelope['source_hash'], requirements_hash=envelope['requirements_hash']),
    )
    from app.structured_planning import contract_of
    for task in mission['tasks']:
        if (contract_of(task) or {}).get('execution_kind') in ('vehicle_extract', 'vehicle_calculate', 'vehicle_verify'):
            manager.memory.update_task(task['id'], 'pending', error='')
    manager.memory.set_mission_status(pid, 'paused', '配賦の回答を保存しました。未完工程から再開できます', 'vehicle_decision')
    return state(manager, pid)


def _history_envelopes(old_envelope):
    if not old_envelope:
        return []
    items = [old_envelope]
    return items


def _rule_covers_month(rule, month):
    start = rule.get('effective_from') or '0000-01'
    end = rule.get('effective_to') or '9999-12'
    return bool(month) and start <= month <= end


def _vehicles_valid(allocations, vehicles):
    by_id = {v['id']: v for v in vehicles or []}
    for item in allocations or []:
        vid = item.get('vehicle_id')
        if vid not in by_id:
            return False
    return True


def reuse_decisions(envelope, old_envelope, snapshot=None):
    """再抽出時に旧判断を条件付きで再適用する。無条件再適用はしない。"""
    data = envelope['data']
    current_hash = envelope.get('source_hash')
    vehicles = data.get('vehicles') or []
    reused = []
    stale = []
    if not old_envelope:
        return reused
    history = _history_envelopes(old_envelope)
    rules = []
    decisions = []
    for item in history:
        rules.extend(item.get('assignments') or [])
        decisions.extend(item.get('decisions') or [])
        nested = (item.get('data') or {})
        rules.extend(nested.get('assignments') or [])
    issues = [x for x in data.get('auto_extraction', {}).get('issues', []) if x.get('status') == 'unresolved' and x.get('kind') == 'allocation']
    for issue in issues:
        month = issue.get('month')
        employee = compact(issue.get('allocation_subject') or '')
        record = next((r for r in data['records'] if r['id'] == issue.get('record_id')), None)
        company = (record or {}).get('company') or ''
        subject = ('employee', company, employee)
        matched_rule = None
        reason_code = 'needs_revalidation'
        for rule in rules:
            if rule.get('status') not in (None, 'active'):
                continue
            if subject_key_of(rule) != subject:
                continue
            if not _rule_covers_month(rule, month):
                continue
            if not _vehicles_valid(rule.get('allocations'), vehicles):
                reason_code = 'invalid'
                continue
            if rule.get('source_hash') and rule.get('source_hash') != current_hash:
                reason_code = 'stale_decision'
                continue
            matched_rule = rule
            break
        if not matched_rule:
            for decision in decisions:
                if decision.get('exclude'):
                    continue
                dsubject = decision.get('subject_key') or {}
                if (dsubject.get('company') or '', compact(dsubject.get('employee') or '')) != (company, employee):
                    continue
                months = decision.get('months') or []
                if month and months and month not in months:
                    continue
                allocs = decision.get('allocations') or (
                    [{'vehicle_id': decision['vehicle_id'], 'ratio': 1}] if decision.get('vehicle_id') else []
                )
                if not allocs or not _vehicles_valid(allocs, vehicles):
                    reason_code = 'invalid'
                    continue
                if decision.get('source_hash') and decision.get('source_hash') != current_hash:
                    reason_code = 'stale_decision'
                    continue
                matched_rule = dict(allocations=allocs, source_hash=decision.get('source_hash'), subject_key=dsubject)
                break
        if not matched_rule:
            issue['reuse_status'] = 'unresolved'
            issue['reuse_reason'] = reason_code
            stale.append(issue)
            continue
        targets = [r for r in data['records'] if r['id'] == issue.get('record_id') or (
            compact(r.get('allocation_subject') or '') == employee and r.get('month') == month and not r.get('allocations')
        )]
        allocs = [dict(vehicle_id=a['vehicle_id'], ratio=a['ratio']) for a in matched_rule.get('allocations') or []]
        for record in targets:
            record['allocations'] = allocs
            record['allocation_reused'] = True
        issue.update(status='resolved', resolution='reused', reuse_status='reused')
        reused.append(issue)
    envelope.setdefault('assignments', [])
    for rule in rules:
        if rule.get('source_hash') and rule.get('source_hash') != current_hash:
            copied = dict(rule, status='stale')
            envelope['assignments'].append(copied)
            continue
        if not _vehicles_valid(rule.get('allocations'), vehicles):
            envelope['assignments'].append(dict(rule, status='invalid'))
            continue
        envelope['assignments'].append(dict(rule, status=rule.get('status') or 'active'))
    data.setdefault('decision_log', []).append(dict(action='reuse_decisions', reused=len(reused), unresolved=len(stale)))
    drop_assumed_zero_when_actual(data['records'], data['decision_log'], allocated_only=True)
    return reused


def run_prepare(manager, pid, version=None, idempotency_key=''):
    """Synchronous prepare used by the web handler and the NAC executor.

    Same safety checks and order as POST /vehicle-profit/prepare.
    """
    from app.vehicle_auto import prepare, REVISION, current_ocr_adoption_signature
    from app.vehicle_workflow import applicable, sources, digest, requirements_hash
    from app.goal_review import ReviewStore, begin_job, finish_job, save_orchestration
    from app.experience_store import fingerprint

    store = ReviewStore(manager.memory.path)
    job = None
    try:
        mission = manager.memory.get_mission(pid)
        if version is None:
            version = mission['plan_version']
        mission, previous = idle_current(manager, pid, version)
        if not applicable(mission):
            raise ValueError('車両別損益の目標が必要です')
        key = idempotency_key or fingerprint(['prepare', pid, version, digest(sources(manager, pid))])
        job = begin_job(store, pid, 'prepare', key, extra={'unique': bool(idempotency_key), 'resume_from': 'prepare'})
        current_ocr_hash = current_ocr_adoption_signature(manager, pid)
        if (
            previous and previous.get('mode') == 'vehicle-auto-v1'
            and previous.get('extractor_revision') == REVISION
            and previous.get('source_hash') == digest(sources(manager, pid))
            and previous.get('requirements_hash') == requirements_hash(mission)
            and previous.get('ocr_adoption_hash', '') == current_ocr_hash
        ):
            finish_job(store, pid, job['id'], 'succeeded', last_completed_stage='prepare')
            result = state(manager, pid)
            result['job_id'] = job['id']
            return result
        manager.planning_projects.add(pid)
        try:
            prepare(manager, pid, mission)
        finally:
            manager.planning_projects.discard(pid)
        finish_job(store, pid, job['id'], 'succeeded', last_completed_stage='prepare')
        save_orchestration(store, pid, last_completed_stage='prepare', resume_from='')
        result = state(manager, pid)
        result['job_id'] = job['id']
        return result
    except ValueError as exc:
        if job:
            finish_job(store, pid, job['id'], 'failed', blocking_error=str(exc)[:1000])
        raise


def _issue_subject_key(issue, records):
    record = next((r for r in records if r.get('id') == issue.get('record_id')), None)
    employee = compact(issue.get('allocation_subject') or '')
    company = (record or {}).get('company') or ''
    return ('employee', company, employee)


def _prior_input_hash(item):
    value = item.get('input_hash')
    return str(value) if value else None


def envelope_input_hash(envelope):
    """入力版。判断・配賦の再適用結果は含めない（循環を避ける）。"""
    return digest({
        'data': envelope.get('data'),
        'mode': envelope.get('mode'),
        'source_hash': envelope.get('source_hash'),
        'requirements_hash': envelope.get('requirements_hash'),
        'extractor_revision': envelope.get('extractor_revision'),
    })


def preview_prior_answers(envelope):
    """Read-only match of unresolved allocation issues to prior answers.

    Apply only when subject_key, period, and input_hash all match.
    Never invent amounts or ratios.
    """
    if not envelope or envelope.get('mode') != 'vehicle-auto-v1':
        return {'applied': [], 'skipped': []}
    data = envelope.get('data') or {}
    records = data.get('records') or []
    vehicles = data.get('vehicles') or []
    current_input_hash = envelope_input_hash(envelope)
    rules = list(envelope.get('assignments') or [])
    rules.extend((data.get('assignments') or []))
    decisions = list(envelope.get('decisions') or [])
    applied = []
    skipped = []
    issues = [
        x for x in (data.get('auto_extraction') or {}).get('issues') or []
        if x.get('status') == 'unresolved' and x.get('kind') == 'allocation'
    ]
    for issue in issues:
        month = issue.get('month')
        subject = _issue_subject_key(issue, records)
        match = None
        reason = 'no_prior_answer'
        source = None
        for rule in rules:
            if rule.get('status') not in (None, 'active'):
                continue
            if subject_key_of(rule) != subject:
                reason = 'subject_key_mismatch'
                continue
            if not _rule_covers_month(rule, month):
                reason = 'period_mismatch'
                continue
            prior_hash = _prior_input_hash(rule)
            if prior_hash is None or prior_hash != current_input_hash:
                reason = 'input_hash_mismatch'
                continue
            if not _vehicles_valid(rule.get('allocations'), vehicles):
                reason = 'invalid'
                continue
            if rule.get('source_hash') and rule.get('source_hash') != envelope.get('source_hash'):
                reason = 'stale_decision'
                continue
            match = rule
            source = dict(
                kind='rule',
                id=rule.get('id'),
                decision_id=rule.get('decision_id'),
                reason=rule.get('reason') or '',
            )
            break
        if match is None:
            for decision in decisions:
                if decision.get('exclude'):
                    continue
                dsubject = decision.get('subject_key') or {}
                employee = compact(issue.get('allocation_subject') or '')
                record = next((r for r in records if r.get('id') == issue.get('record_id')), None)
                company = (record or {}).get('company') or ''
                if (dsubject.get('company') or '', compact(dsubject.get('employee') or '')) != (company, employee):
                    reason = 'subject_key_mismatch'
                    continue
                months = decision.get('months') or []
                if month and months and month not in months:
                    reason = 'period_mismatch'
                    continue
                prior_hash = _prior_input_hash(decision)
                if prior_hash is None or prior_hash != current_input_hash:
                    reason = 'input_hash_mismatch'
                    continue
                allocs = decision.get('allocations') or (
                    [{'vehicle_id': decision['vehicle_id'], 'ratio': 1}] if decision.get('vehicle_id') else []
                )
                if not allocs or not _vehicles_valid(allocs, vehicles):
                    reason = 'invalid'
                    continue
                if decision.get('source_hash') and decision.get('source_hash') != envelope.get('source_hash'):
                    reason = 'stale_decision'
                    continue
                match = dict(allocations=allocs, source_hash=decision.get('source_hash'), subject_key=dsubject)
                source = dict(
                    kind='decision',
                    decision_id=decision.get('decision_id'),
                    issue_ids=list(decision.get('issue_ids') or []),
                    reason=decision.get('reason') or '',
                )
                break
        if match is None:
            skipped.append({'issue_id': issue.get('id'), 'reason': reason})
            continue
        allocs = [dict(vehicle_id=a['vehicle_id'], ratio=a['ratio']) for a in match.get('allocations') or []]
        if not allocs:
            skipped.append({'issue_id': issue.get('id'), 'reason': 'invalid'})
            continue
        applied.append({
            'issue_id': issue.get('id'),
            'allocations': allocs,
            'source': source,
            'subject_key': {'company': subject[1], 'employee': subject[2]},
            'month': month,
            'input_hash': current_input_hash,
        })
    return {'applied': applied, 'skipped': skipped, 'input_hash': current_input_hash}


def apply_prior_answers(manager, pid):
    """Apply prior answers only on exact subject_key + period + input_hash match.

    Does not write achieved or human acceptance. Unmatched items stay unresolved.
    """
    from app.vehicle_workflow import applicable

    mission = manager.memory.get_mission(pid)
    mission, envelope = idle_current(manager, pid, mission['plan_version'])
    if not applicable(mission):
        raise ValueError('車両別損益の目標が必要です')
    if not envelope or envelope.get('mode') != 'vehicle-auto-v1':
        raise ValueError('自動抽出を先に実行してください')
    preview = preview_prior_answers(envelope)
    data = envelope['data']
    issues_by_id = {
        x.get('id'): x for x in (data.get('auto_extraction') or {}).get('issues') or []
    }
    applied_rows = []
    for item in preview['applied']:
        issue = issues_by_id.get(item['issue_id'])
        if not issue or issue.get('status') != 'unresolved':
            continue
        employee = compact(issue.get('allocation_subject') or '')
        month = issue.get('month')
        targets = [
            r for r in data['records']
            if r['id'] == issue.get('record_id') or (
                compact(r.get('allocation_subject') or '') == employee
                and r.get('month') == month
                and not r.get('allocations')
            )
        ]
        allocs = [dict(vehicle_id=a['vehicle_id'], ratio=a['ratio']) for a in item['allocations']]
        for record in targets:
            record['allocations'] = allocs
            record['allocation_reused'] = True
        issue.update(
            status='resolved', resolution='reused', reuse_status='reused',
            reuse_source=item.get('source') or {},
        )
        applied_rows.append(item)
    data.setdefault('decision_log', []).append(dict(
        action='apply_prior_answers',
        reused=len(applied_rows),
        unresolved=len(preview['skipped']),
        applied=[x['issue_id'] for x in applied_rows],
        skipped=list(preview['skipped']),
    ))
    drop_assumed_zero_when_actual(data['records'], data['decision_log'], allocated_only=True)
    if applied_rows:
        write_json(resolve(manager, pid, INPUT_PATH), envelope)
        write_json(
            resolve(manager, pid, 'vehicle_profit/automatic_proof.json'),
            dict(
                input_hash=digest(envelope),
                source_hash=envelope.get('source_hash'),
                requirements_hash=envelope.get('requirements_hash'),
            ),
        )
    return {
        'applied': applied_rows,
        'skipped': list(preview['skipped']),
        'input_hash': digest(envelope) if envelope else preview.get('input_hash'),
        'achieved': False,
        'human_accepted': False,
    }
