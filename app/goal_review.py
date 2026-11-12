"""Version-bound external plan review and explicit human-approved local lessons."""
import asyncio
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from app.experience_store import ExperienceStore, fingerprint, canonical
from app.structured_planning import contract_of, verify_outputs

RUNNING_JOB = {'running', 'generating'}
ORCHESTRATION_STAGES = (
    'feedback_import', 'local_proposal', 'human_confirmation', 'apply',
    'public_packet', 'send_approval', 'external_review', 'plan_approval', 'execution_start',
)
_AFTER_APPLY = {
    'apply', 'public_packet', 'send_approval', 'external_review', 'plan_approval', 'execution_start',
}


class ReviewStore:
    def __init__(self,memory_path):
        self.path=Path(memory_path).parent/'goal_reviews.sqlite3'
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS reviews(project TEXT, kind TEXT, signature TEXT, payload TEXT, PRIMARY KEY(project,kind,signature))')
    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=15)
        try:
            with db:yield db
        finally:db.close()
    def get(self,pid,kind,signature):
        with self.connect() as db:r=db.execute('SELECT payload FROM reviews WHERE project=? AND kind=? AND signature=?',(pid,kind,signature)).fetchone()
        return json.loads(r[0]) if r else None
    def put(self,pid,kind,signature,payload):
        with self.connect() as db:db.execute('INSERT OR REPLACE INTO reviews VALUES(?,?,?,?)',(pid,kind,signature,canonical(payload)))
    def list(self,pid,kind):
        with self.connect() as db:
            rows=db.execute('SELECT signature,payload FROM reviews WHERE project=? AND kind=?',(pid,kind)).fetchall()
        return [(signature, json.loads(payload)) for signature, payload in rows]


def save_orchestration(store, pid, **fields):
    row = store.get(pid, 'orchestration', pid) or {}
    row.update(fields)
    row['updated_at'] = time.time()
    store.put(pid, 'orchestration', pid, row)
    return row


def load_orchestration(store, pid):
    return store.get(pid, 'orchestration', pid) or {}


def orchestration_view(manager, pid):
    store = ReviewStore(manager.memory.path)
    row = dict(load_orchestration(store, pid))
    _, signature = plan_snapshot(manager, pid)
    if row.get('plan_signature') and row.get('plan_signature') != signature and row.get('last_completed_stage') in _AFTER_APPLY:
        row['resume_from'] = 'public_packet'
        row['hash_changed'] = True
    return row


def begin_job(store, pid, kind, idempotency_key, extra=None):
    extra = extra or {}
    unique = extra.pop('unique', False)
    for _, row in store.list(pid, 'job'):
        if row.get('idempotency_key') != idempotency_key:
            continue
        if row.get('status') in RUNNING_JOB:
            raise ValueError('同じ処理を実行中です')
        if unique and row.get('status') == 'succeeded':
            raise ValueError('同じ処理は完了済みです')
    job_id = uuid.uuid4().hex
    row = {
        'id': job_id, 'kind': kind,
        'status': 'generating' if kind == 'propose' else 'running',
        'idempotency_key': idempotency_key, 'started': time.time(), 'updated_at': time.time(),
        'last_completed_stage': '', 'blocking_error': '', 'resume_from': extra.get('resume_from') or '',
    }
    row.update(extra)
    store.put(pid, 'job', job_id, row)
    return row


def finish_job(store, pid, job_id, status, **updates):
    row = store.get(pid, 'job', job_id) or {'id': job_id}
    row.update(updates)
    row['status'] = status
    row['updated_at'] = time.time()
    if status in {'failed', 'needs_attention'}:
        row['blocking_error'] = str(updates.get('blocking_error') or row.get('blocking_error') or '')[:1000]
    store.put(pid, 'job', job_id, row)
    return row


def active_jobs(store, pid):
    return [row for _, row in store.list(pid, 'job') if row.get('status') in RUNNING_JOB]


def development_blockers(manager, pid, detail=None):
    _, signature = plan_snapshot(manager, pid, detail)
    row = ReviewStore(manager.memory.path).get(pid, 'revision', signature) or {}
    return [b for b in row.get('blockers') or [] if isinstance(b, dict)
            and b.get('disposition') in {'development', 'business_fact', 'unresolved'}]


def load_review_policy(manager):
    path=Path(manager.memory.path).parent/'goal_review_policy.json'
    if not path.exists():
        return {}
    try:
        config=json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    return config if isinstance(config, dict) else {}


def enabled(manager,pid):
    config=load_review_policy(manager)
    if not config:
        return False
    return config.get('projects',{}).get(pid,config.get('required',False)) is True


def _policy_section(config, pid):
    projects=config.get('projects') or {}
    row=projects.get(pid)
    if isinstance(row, dict):
        return row
    return config


def review_pass_policy(manager, pid, provider_count=None):
    """最低合格数と必須provider。未設定時は全社成功が必要（既存テスト互換）。"""
    config=load_review_policy(manager)
    section=_policy_section(config, pid)
    raw_required=section.get('required_providers', config.get('required_providers') or [])
    if isinstance(raw_required, str):
        raw_required=[raw_required]
    required=[str(x).strip() for x in (raw_required or []) if str(x).strip()]
    raw_min=section.get('min_success_count', config.get('min_success_count'))
    if raw_min is None:
        min_count=provider_count
    else:
        try:
            min_count=int(raw_min)
        except (TypeError, ValueError):
            min_count=provider_count
    if provider_count is not None and min_count is not None:
        min_count=max(0, min(int(min_count), int(provider_count)))
    return {
        'min_success_count': min_count,
        'required_providers': list(dict.fromkeys(required)),
    }


def provider_review_outcomes(parsed, providers):
    by={x.get('provider'): x for x in parsed or []}
    rows=[]
    for provider in providers:
        row=by.get(provider) or {}
        status=row.get('status')
        if not row:
            outcome,label='not_run','未実施'
        elif status=='connection_error':
            outcome,label='connection_error','接続エラー'
        elif status=='pass' and not row.get('issues'):
            outcome,label='success','成功'
        elif status in {'conditional','fail','unverifiable'} or (status=='pass' and row.get('issues')):
            outcome,label='content_fail','計画内容の指摘'
        elif status=='unverified':
            response=row.get('response') or {}
            if response.get('ok'):
                outcome,label='failed','解析不能'
            elif response:
                outcome,label='connection_error' if response.get('outcome')=='connection_error' else 'failed','接続エラー' if response.get('outcome')=='connection_error' else '未取得'
            else:
                outcome,label='not_run','未実施'
        else:
            outcome,label='failed','失敗'
        item={
            'id': provider, 'provider': provider, 'outcome': outcome, 'label': label,
            'status': status or '',
        }
        if status=='connection_error' or outcome=='connection_error':
            conn=row.get('connection_error') if isinstance(row.get('connection_error'), dict) else {}
            response=row.get('response') if isinstance(row.get('response'), dict) else {}
            item['error']=str(conn.get('error') or response.get('error') or '')[:500]
            item['status_code']=conn.get('status_code') or response.get('status_code')
            item['category']=conn.get('category') or response.get('error_category') or 'connection_error'
        rows.append(item)
    return rows


def evaluate_external_review_status(parsed, providers, policy):
    selected=list(dict.fromkeys(providers or []))
    required=list(dict.fromkeys(policy.get('required_providers') or []))
    needed=policy.get('min_success_count')
    if needed is None:
        needed=len(selected)
    try:
        needed=int(needed)
    except (TypeError, ValueError):
        needed=len(selected)
    if selected:
        needed=max(0, min(needed, len(selected)))
    else:
        needed=max(0, needed)
    outcomes={row['id']: row['outcome'] for row in provider_review_outcomes(parsed, selected)}
    success_count=sum(1 for provider in selected if outcomes.get(provider)=='success')
    has_content=any(outcomes.get(provider)=='content_fail' for provider in selected)
    required_connection=[]
    for provider in required:
        outcome=outcomes.get(provider)
        if provider not in selected or outcome in {'connection_error','not_run','failed'}:
            required_connection.append(provider)
    required_connection=list(dict.fromkeys(required_connection))
    if required_connection:
        names='、'.join(required_connection)
        return {
            'status': 'connection_failed',
            'stop_kind': 'connection_settings',
            'stop_reason': (
                f'必須の外部AI（{names}）の接続に失敗したため停止しました。'
                '計画内容の欠陥ではなく接続設定の問題です。APIキー・認証・レート制限を確認してください。'
            ),
            'success_count': success_count,
            'required_count': needed,
        }
    required_unmet=[p for p in required if outcomes.get(p)!='success']
    if success_count>=needed and not required_unmet:
        return {
            'status': 'passed', 'stop_kind': '', 'stop_reason': '',
            'success_count': success_count, 'required_count': needed,
        }
    by={x.get('provider'): x for x in parsed or []}
    all_unverified_no_ok = bool(selected) and all(
        (by.get(provider) or {}).get('status') in {'unverified', 'connection_error'}
        and not ((by.get(provider) or {}).get('response') or {}).get('ok')
        for provider in selected
    )
    only_connection_shortfall = (
        not has_content
        and any(outcomes.get(provider)=='connection_error' for provider in selected)
        and not any(
            (by.get(provider) or {}).get('status')=='unverified'
            and ((by.get(provider) or {}).get('response') or {}).get('ok')
            for provider in selected
        )
    )
    if (all_unverified_no_ok or only_connection_shortfall) and not has_content:
        return {
            'status': 'awaiting_external', 'stop_kind': 'connection_settings',
            'stop_reason': '外部AIの接続に失敗したため検証を完了できません。計画内容への指摘ではありません。',
            'success_count': success_count, 'required_count': needed,
        }
    return {
        'status': 'not_passed', 'stop_kind': 'plan', 'stop_reason': '',
        'success_count': success_count, 'required_count': needed,
    }


def plan_snapshot(manager,pid,detail=None):
    mission=manager.memory.get_mission(pid)
    value={k:mission.get(k) for k in ['goal','success_criteria','constraints_text','plan_version']}
    value['instructions']=[x['message'] for x in mission.get('instruction_messages',[]) if x['kind']=='mission_instruction_user']
    value['review_policy_version']=2
    from app.vehicle_auto import REVISION
    value['vehicle_engine_revision']=REVISION if any((contract_of(t) or {}).get('execution_kind','').startswith('vehicle_') for t in mission['tasks']) else None
    value['external_providers']=mission.get('external_providers',[])
    value['sources']=[{'id':x['id'],'sha256':x.get('sha256'),'content_hash':fingerprint(x.get('content',''))} for x in manager.memory.list_context_files(pid,include_content=True) if x.get('source')!='memo']
    value['tasks']=[{k:t.get(k) for k in ['id','task_key','title','description','acceptance_criteria','depends_on']} for t in mission['tasks']]
    if detail is not None:value['detail']=detail
    return value,fingerprint(value)


def require_review(manager,pid,detail=None):
    if development_blockers(manager, pid, detail):
        raise ValueError('追加開発または業務事実の確認が必要な指摘があるため実行を開始できません')
    if not enabled(manager,pid):return
    _,signature=plan_snapshot(manager,pid,detail)
    row=ReviewStore(manager.memory.path).get(pid,'plan',signature)
    if row and row.get('status')=='passed':
        return
    if row and row.get('status')=='connection_failed':
        raise ValueError(row.get('stop_reason') or '必須の外部AIの接続に失敗したため実行を開始できません。計画内容の問題ではなく接続設定の問題です。')
    raise ValueError('外部AIによる'+('詳細' if detail else '全体')+'計画の目標適合性検証が未完了または不合格です。「目標検証・RAG」で現行版を確認してください')


def detail_payloads(manager,pid):
    from app.detail_store import DetailStore
    from app.detailed_planning import episode_key
    mission=manager.memory.get_mission(pid);store=DetailStore(manager.memory.path);out=[]
    for task in mission['tasks']:
        row=store.latest_plan(pid,task['id'],episode_key(task,mission))
        if row:out.append({'task_id':task['id'],'plan_id':row['id'],'payload':row['payload'],'signature':row['signature']})
    return out


def selected_detail(manager,pid,tid):
    if not tid:return None
    found=next((x for x in detail_payloads(manager,pid) if x['task_id']==tid),None)
    if not found:raise ValueError('詳細計画がありません')
    from app.detail_service import view
    if view(manager,pid,tid).get('stale'):raise ValueError('詳細計画の入力が変更されています。詳細計画を再生成してください')
    return found['payload']


PUBLIC_STRUCTURE_VERSION = "public-structure-v3"

FINAL_VERIFICATION_MODEL = {
    "report_generated_by": "system",
    "human_approval_gate": "separate_human_acceptance",
    "completes_without_human_approval": False,
    "unverifiable_policy": "never_pass_without_evidence",
    "note": "approval_required=falseは外部操作の追加承認が不要の意味であり、人間の内容承認ゲートとは別。未承認では最終完了しない。非公開原本の実在・内容はローカル照合の責任であり、外部評価のunverifiableを合格に変えない。",
}

VERIFICATION_AGGREGATION_RULE = "all_required_items_must_pass_for_task_pass"

VERIFICATION_METHODS_PUBLIC = ("count", "artifact_count", "section_char_count", "approval_stage_matrix", "headings", "phrase", "llm_semantic")


# P0-A: 公開packetを評価可能にする(項目1〜3)。原本・秘密・連絡先・内部パス・
# 個人情報は自動転送しない。公開可の情報が足りないときは推測で補わない。
P0A_PUBLIC_PURPOSE_VERSION = "public-purpose-v1"
P0A_PACKET_BINDING_VERSION = "packet-binding-v1"
P0A_GENERIC_MARKERS = (
    "登録済みの目標と達成条件を実行する汎用計画",
    "構造情報だけから生成",
)
_P0A_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def is_generic_public_summary(text: object) -> bool:
    """汎用文だけか。既定の汎用説明の文言が残っていれば具体性不足とみなす。"""
    blob = str(text) if isinstance(text, str) else ""
    if not blob.strip():
        return True
    for marker in P0A_GENERIC_MARKERS:
        if marker in blob:
            return True
    return False


def is_valid_packet_hash(value: object) -> bool:
    """欠落・空・不正な形式は『一致』にしないための厳格な形式検査。"""
    if not isinstance(value, str):
        return False
    if len(value) != 64:
        return False
    if _P0A_HASH_RE.fullmatch(value) is None:
        return False
    return True


def build_public_purpose_summary(snapshot: dict, explicit_summary: str = "") -> dict:
    """公開可の情報だけから目的要約を作る。推測で補わない。"""
    tasks = (snapshot or {}).get("tasks") if isinstance(snapshot, dict) else []
    if not isinstance(tasks, list):
        tasks = []
    contracts = [contract_of(t) or {} for t in tasks if isinstance(t, dict)]
    deliverable = 0
    internal_records = 0
    kinds: dict[str, int] = {}
    criterion_ids: list[str] = []
    for contract in contracts:
        role = str(contract.get("role") or "")
        final = bool(contract.get("final_verification"))
        is_deliverable = (role not in {"preparation", "final_verification"}) and (not final)
        outputs = contract.get("outputs") if isinstance(contract.get("outputs"), list) else []
        count = sum(1 for row in outputs if isinstance(row, dict))
        if is_deliverable:
            deliverable += count
        else:
            internal_records += count
        for row in outputs:
            if not isinstance(row, dict):
                continue
            category = str(row.get("artifact_category") or "document")
            kinds[category] = kinds.get(category, 0) + 1
        for cid in contract.get("criterion_ids") or []:
            if isinstance(cid, str) and re.fullmatch(r"SC[0-9]{2}", cid):
                if cid not in criterion_ids:
                    criterion_ids.append(cid)
    criterion_ids.sort()
    kind_text = "、".join(f"{name} {num}件" for name, num in sorted(kinds.items())) if kinds else "種類不明"
    purpose_text = (
        f"利用者向け完成成果物{deliverable}件（{kind_text}）と"
        f"内部記録・検証報告{internal_records}件を含む計画。"
        f"達成条件ID{criterion_ids.__len__()}件"
        f"（{ '、'.join(criterion_ids) if criterion_ids else 'なし'}）。"
    )
    explicit = str(explicit_summary) if isinstance(explicit_summary, str) else ""
    explicit_stripped = explicit.strip()
    secret_suspected = ""
    if explicit_stripped:
        try:
            from app.plan_review_loop import contains_secret as _contains_secret
            secret_suspected = _contains_secret(explicit_stripped)
        except (ValueError, TypeError, AttributeError):
            secret_suspected = "確認不能"
    missing: list[str] = []
    if not explicit_stripped or is_generic_public_summary(explicit_stripped):
        missing.append("concrete_purpose_description")
    if not criterion_ids:
        missing.append("criterion_ids")
    if deliverable <= 0 and internal_records <= 0:
        missing.append("artifact_counts")
    complete = (not missing) and (not secret_suspected)
    return {
        "version": P0A_PUBLIC_PURPOSE_VERSION,
        "purpose_text": purpose_text,
        "audience": "利用者向け完成成果物と内部記録を区別して示す",
        "artifact_kinds": [{"category": name, "count": num} for name, num in sorted(kinds.items())],
        "deliverable_count": deliverable,
        "internal_record_count": internal_records,
        "criterion_ids": list(criterion_ids),
        "criterion_count": len(criterion_ids),
        "explicit_summary_used": bool(explicit_stripped) and not is_generic_public_summary(explicit_stripped) and not secret_suspected,
        "secret_suspected": secret_suspected,
        "missing": missing,
        "complete": bool(complete),
        "source": "public_structure_only",
    }


def check_public_consistency(snapshot: dict, structure: dict, explicit_summary: str = "") -> dict:
    """送信前の整合検査。内部契約と一致しなければ送信しない。"""
    reasons: list[str] = []
    tasks = (snapshot or {}).get("tasks") if isinstance(snapshot, dict) else []
    if not isinstance(tasks, list):
        tasks = []
    struct_tasks = (structure or {}).get("tasks") if isinstance(structure, dict) else []
    if not isinstance(struct_tasks, list):
        struct_tasks = []
    if len(tasks) != len(struct_tasks):
        reasons.append("工程数が内部契約と一致しません")
    per_task_ok = True
    for position, task in enumerate(tasks):
        if position >= len(struct_tasks):
            per_task_ok = False
            break
        row = struct_tasks[position]
        if not isinstance(task, dict) or not isinstance(row, dict):
            per_task_ok = False
            reasons.append(f"工程{position + 1}の形式が不正です")
            continue
        contract = contract_of(task) or {}
        expected_cids = [x for x in (contract.get("criterion_ids") or [])
                         if isinstance(x, str) and re.fullmatch(r"SC[0-9]{2}", x)]
        actual_cids = [x for x in (row.get("criterion_ids") or []) if isinstance(x, str)]
        if sorted(expected_cids) != sorted(actual_cids):
            per_task_ok = False
            reasons.append(f"工程{position + 1}の達成条件IDが一致しません")
        expected_outputs = contract.get("outputs") if isinstance(contract.get("outputs"), list) else []
        expected_n = sum(1 for x in expected_outputs if isinstance(x, dict))
        actual_n = row.get("output_count")
        if not isinstance(actual_n, int) or actual_n != expected_n:
            per_task_ok = False
            reasons.append(f"工程{position + 1}の成果物数が一致しません")
        expected_checks = contract.get("exit_checks") if isinstance(contract.get("exit_checks"), list) else []
        if row.get("exit_check_count") != len(expected_checks):
            per_task_ok = False
            reasons.append(f"工程{position + 1}の検査項目数が一致しません")
        needs = bool(contract.get("approval_required") or contract.get("action_requirements"))
        expected_approval = bool(needs) and (not bool(contract.get("final_verification")))
        if bool(row.get("approval_required")) != expected_approval:
            per_task_ok = False
            reasons.append(f"工程{position + 1}の承認境界が一致しません")
    try:
        from app.structured_planning import normalize_p0a_artifact_requirement as _norm_slots
        slot_ok = True
        slot_detail: list[dict] = []
        for task in tasks:
            if not isinstance(task, dict):
                continue
            contract = contract_of(task) or {}
            if bool(contract.get("final_verification")):
                continue
            if str(contract.get("role") or "") in {"preparation", "final_verification"}:
                continue
            cids = list(contract.get("criterion_ids") or [])
            if not cids:
                continue
            criterion_text = str(contract.get("criterion") or "")
            if not criterion_text.strip():
                slot_ok = False
                reasons.append("成果物スロットの判定材料がありません")
                slot_detail.append({"criterion_ids": list(cids), "consistent": False})
                continue
            norm = _norm_slots(criterion_text)
            if not norm.get("consistent"):
                slot_ok = False
                reasons.append("成果物スロットの要求が矛盾しているため送信しません")
                slot_detail.append({"criterion_ids": list(cids), "consistent": False})
                continue
            required = norm.get("required")
            if not isinstance(required, int) or not 1 <= required <= 9:
                slot_ok = False
                reasons.append("成果物スロット数が判定不能のため送信しません")
                slot_detail.append({"criterion_ids": list(cids), "consistent": False})
                continue
            outputs = contract.get("outputs") if isinstance(contract.get("outputs"), list) else []
            actual = sum(1 for x in outputs if isinstance(x, dict))
            item_counts = norm.get("item_counts")
            explicit_total = norm.get("explicit_total")
            count_is_explicit = bool(item_counts) or explicit_total is not None
            slot_detail.append({"criterion_ids": list(cids), "required": required, "actual": actual,
                                "count_is_explicit": count_is_explicit})
            if actual < required or (actual > required and count_is_explicit):
                slot_ok = False
                reasons.append("成果物スロット数と成果物数が一致しません")
    except ValueError:
        slot_ok = False
        slot_detail = [{"consistent": False, "error": "undetermined"}]
        reasons.append("成果物スロット数が判定不能のため送信しません")
    withheld_exit = 0
    withheld_items = 0
    disclosure = (structure or {}).get("exit_check_disclosure") if isinstance(structure, dict) else {}
    if isinstance(disclosure, dict) and isinstance(disclosure.get("withheld_count"), int):
        withheld_exit = int(disclosure.get("withheld_count"))
    vdisc = (structure or {}).get("verification_disclosure") if isinstance(structure, dict) else {}
    if isinstance(vdisc, dict) and isinstance(vdisc.get("withheld_item_count"), int):
        withheld_items = int(vdisc.get("withheld_item_count"))
    nonpublic = withheld_exit + withheld_items
    unevaluable = ""
    if nonpublic > 0:
        unevaluable = f"非公開{nonpublic}件（公開できない検査・条件）は外部AIが評価できません。内容は送りません。"
    prep_disc = (structure or {}).get("preparation_gate_disclosure") if isinstance(structure, dict) else {}
    disclosure_incomplete = bool(isinstance(prep_disc, dict) and prep_disc.get("disclosure_incomplete"))
    generic = is_generic_public_summary(explicit_summary)
    status = "ok"
    if generic:
        status = "packet_incomplete"
        reasons.append("目的説明が汎用文だけのため送信しません。具体的な公開要約を入力・確認してください。")
    if disclosure_incomplete:
        # 内部契約の不足は外部評価で発見すべき対象でもあるため、送信自体は止めない。
        # 評価不能範囲としてpacket・preview・承認記録へ残し、外部判定では合格にしない。
        status = "unverifiable"
    passed = (not reasons) and per_task_ok and slot_ok and (not generic)
    if not passed and status == "ok":
        status = "mismatch"
    return {
        "version": P0A_PACKET_BINDING_VERSION,
        "passed": bool(passed),
        "status": status,
        "reasons": list(reasons),
        "per_task_ok": bool(per_task_ok),
        "slot_ok": bool(slot_ok),
        "slot_detail": slot_detail,
        "withheld_exit_checks": withheld_exit,
        "withheld_verification_items": withheld_items,
        "nonpublic_count": nonpublic,
        "unevaluable_scope": unevaluable,
        "disclosure_incomplete": bool(disclosure_incomplete),
        "generic_summary": bool(generic),
    }


def build_final_packet(public_summary: str, snapshot: dict) -> tuple:
    """最終送信packetとハッシュ。previewと実送信で同じ作り方を使う。"""
    structure = public_structure(snapshot)
    disclosure = structure.get("preparation_gate_disclosure") if isinstance(structure, dict) else {}
    packet = {
        "public_goal_and_plan": str(public_summary),
        "structure": structure,
        "evaluation_limitations": {
            "status": "unverifiable" if isinstance(disclosure, dict) and disclosure.get("disclosure_incomplete") else "ok",
            "unevaluable_scope": str((disclosure or {}).get("unevaluable_scope") or "") if isinstance(disclosure, dict) else "",
            "disclosure_incomplete": bool(isinstance(disclosure, dict) and disclosure.get("disclosure_incomplete")),
        },
    }
    return packet, fingerprint(packet)


def verify_packet_hash(expected_hash: object, packet: dict) -> dict:
    """決定的な照合。欠落・空・不正な形式は『一致』にしない。"""
    actual = fingerprint(packet)
    if not is_valid_packet_hash(expected_hash):
        return {"ok": False, "reason": "missing_or_invalid_hash",
                "guidance": "再プレビューしてください。プレビューで示したハッシュをそのまま送ってください。",
                "actual_hash": actual}
    if not is_valid_packet_hash(actual):
        return {"ok": False, "reason": "internal_hash_invalid",
                "guidance": "再プレビューしてください。",
                "actual_hash": actual}
    if str(expected_hash) != str(actual):
        return {"ok": False, "reason": "mismatch",
                "guidance": "送信内容がプレビュー後に変わりました。再プレビューしてください。",
                "actual_hash": actual}
    return {"ok": True, "reason": "", "guidance": "", "actual_hash": actual}


def _public_exit_checks(contract, safe_codes):
    """公開可能な検査コードと件数を同じ出所から作る。非公開は件数のみ示す。"""
    raw = contract.get("exit_checks") if isinstance(contract, dict) else None
    checks = raw if isinstance(raw, list) else []
    public = [x for x in checks if isinstance(x, str) and x in safe_codes]
    total = len(checks)
    withheld = total - len(public)
    if withheld < 0:
        withheld = 0
    return public, withheld, total


def _public_verification_items(contract):
    """成果物ごとの項目ID・検査方法分類・集約条件を公開範囲で示す。本文・キーワード・原本参照は送らない。"""
    if not isinstance(contract, dict):
        return [], 0
    raw_outputs = contract.get("outputs")
    outputs = raw_outputs if isinstance(raw_outputs, list) else []
    paths = []
    for row in outputs:
        if isinstance(row, dict) and isinstance(row.get("path"), str):
            paths.append(str(row.get("path")))
    raw_matrix = contract.get("requirement_matrix")
    if not isinstance(raw_matrix, list):
        return [], 0
    public = []
    withheld = 0
    for row in raw_matrix:
        if not isinstance(row, dict):
            withheld += 1
            continue
        item_id = row.get("item_id")
        method = row.get("method")
        required = row.get("required")
        artifact_path = row.get("artifact_path")
        if not isinstance(item_id, str) or not item_id:
            withheld += 1
            continue
        if method not in VERIFICATION_METHODS_PUBLIC:
            withheld += 1
            continue
        if required is not True and required is not False:
            withheld += 1
            continue
        if not isinstance(artifact_path, str):
            withheld += 1
            continue
        index = -1
        for pos, path in enumerate(paths):
            if path == artifact_path:
                index = pos
                break
        if index < 0:
            withheld += 1
            continue
        row_public = {"item_id": item_id, "method": method, "required": bool(required)}
        if method == "artifact_count":
            row_public["artifact_indices"] = list(range(len(paths)))
            row_public["scope"] = "all_declared_outputs"
        else:
            row_public["artifact_index"] = index
            row_public["scope"] = "listed_artifact"
        if method == "section_char_count":
            row_public["measurement"] = "heading_section_paragraph_characters_excluding_bullets_and_spaces"
        public.append(row_public)
    public.sort(key=lambda r: (str(r.get("item_id") if isinstance(r.get("item_id"), str) else ""),
                               int(r.get("artifact_index") if isinstance(r.get("artifact_index"), int) else 0)))
    return public, withheld


def _public_approval_model(contract):
    """最終検証の矛盾しない状態モデル。システム報告・人間承認別ゲート・未承認では完了しない。"""
    if not isinstance(contract, dict):
        return {"approval_required": False, "separate_human_approval_gate": False,
                "completes_without_human_approval": False}
    needs = bool(contract.get("approval_required"))
    acts = bool(contract.get("action_requirements"))
    is_final = bool(contract.get("final_verification"))
    approval_required = (needs or acts) and (not is_final)
    separate = contract.get("human_approval_gate") == "separate_human_acceptance"
    return {"approval_required": bool(approval_required),
            "separate_human_approval_gate": bool(separate),
            "completes_without_human_approval": False}


def public_structure(snapshot):
    # Whitelist structural facts only. No source text, filenames, original goals or names.
    safe_exit_codes={
        'source_evidence_linked', 'criterion_content_verified', 'unknowns_and_failures_reported',
        'faq_items_individually_verified', 'procedure_steps_individually_verified',
        'approval_points_verified', 'fictional_marking_verified', 'no_real_processing_claimed',
        'each_output_contract_verified', 'all_criteria_decided', 'evidence_paths_verified',
        'unmet_and_pending_reported', 'per_criterion_verdicts_recorded',
        'registered_sources_classified', 'missing_inputs_listed', 'validation_method_defined',
    }
    purpose_catalog={
        'input_readiness':'Verify registered sources, missing inputs, constraints, and validation methods.',
        'multi_channel_workflow_design':'Design the execution workflow for forms, landing pages, social support, and lead capture.',
        'execution_monitoring_evidence':'Use monitor progress, errors, publication records, and lead counts as evidence.',
        'manual_social_posting_guard':'Prevent automatic social posting and require a human-controlled posting screen.',
        'external_approval_boundaries':'Require separate human approval for publication, copy approval, posting, and prospect contact.',
        'evidence_based_completion':'Judge completion using URLs, responses, approvals, and other execution evidence.',
        'campaign_execution_sequence':'Execute publication, posting-kit preparation, approval, manual posting, response sync, and lead evaluation in order.',
        'failure_recovery_policy':'Classify failures, stop for authentication or approval, and safely retry temporary failures.',
        'market_and_customer_definition':'Define markets, priority sectors, ideal customers, and customer problems.',
        'service_package_design':'Design multiple consulting service packages.',
        'commercial_terms':'Define scope, duration, prerequisites, price proposals, and exclusions.',
        'menu_document':'Create menu and product explanations with provisional prices marked as unconfirmed.',
        'sales_assets':'Create introduction, proposal, interview, proof-of-concept, and quotation materials.',
        'sales_plan':'Create a sales plan with channels, activity, KPIs, and financial outlook.',
        'prospect_prioritization':'Evaluate prospects and define priorities and proposal hypotheses.',
        'outreach_content':'Create approved outreach copy, meeting scripts, and follow-up copy.',
        'pipeline_tracking':'Create structured tracking for meetings, requirements, next actions, and losses.',
        'delivery_process':'Define the post-order process through requirements, proof-of-concept, acceptance, education, and support.',
        'execution_status':'Report executed, approval-pending, not-started, failed, and next-action states.',
        'approved_customer_engagement':'Execute only approved customer engagement and register external evidence.',
        'final_goal_verification':'Verify every criterion against artifacts and execution evidence; any unmet criterion prevents PASS.',
        'criterion_delivery':'Create and verify the artifact required by its criterion.',
    }
    contracts=[contract_of(task) or {} for task in snapshot['tasks']]
    aggregate_item_count=sum(
        len(contract.get('requirement_matrix') or [])
        for contract in contracts if not contract.get('final_verification')
    )
    criterion_ids=sorted({
        cid for contract in contracts for cid in (contract.get('criterion_ids') or [])
        if isinstance(cid,str) and __import__('re').fullmatch(r'SC[0-9]{2}',cid)
    })
    tasks=[]
    for i,t in enumerate(snapshot['tasks'],1):
        c=contract_of(t) or {}
        kind=c.get('execution_kind','document_or_legacy')
        role=('preparation' if c.get('role')=='preparation' else
              'final_verification' if c.get('final_verification') else
              'external_action' if c.get('action_requirements') else 'execution')
        public_codes, withheld_checks, total_checks = _public_exit_checks(c, safe_exit_codes)
        public_items, withheld_items = _public_verification_items(c)
        approval_model = _public_approval_model(c)
        withheld_note = ('非公開%d件' % withheld_checks) if withheld_checks else '非公開0件'
        output_class=('internal_input_record' if role=='preparation' else
                      'internal_verification_report' if role=='final_verification' else 'deliverable')
        tasks.append({'step':i,'type':kind,'role':role,
                      'output_class':output_class,
                      'aggregates_earlier_item_count':aggregate_item_count if role=='final_verification' else 0,
                      'aggregates_criterion_ids':criterion_ids if role=='final_verification' else [],
                      'preparation_gate_required':role=='final_verification' and any((x.get('role')=='preparation') for x in contracts),
                      'upstream_failure_action':c.get('upstream_failure_action','') if role=='final_verification' else '',
                      'internal_preparation_gate_ids':['PREP00'] if role in {'preparation','final_verification'} else [],
                      'parents':[next((j for j,x in enumerate(snapshot['tasks'],1) if x['task_key']==p),0) for p in t.get('depends_on',[])],
                      'outputs':[Path(x.get('path','')).suffix for x in c.get('outputs',[])],
                      'output_count':sum(1 for x in (c.get('outputs') or []) if isinstance(x,dict)),
                      'criterion_ids':[x for x in c.get('criterion_ids',[]) if isinstance(x,str) and __import__('re').fullmatch(r'SC[0-9]{2}',x)],
                      'public_purpose_code':c.get('public_purpose_code','unspecified'),
                      'public_purpose_summary':purpose_catalog.get(c.get('public_purpose_code'),'Purpose summary unavailable.'),
                      'artifact_category':c.get('artifact_category','unspecified'),
                      'responsible_role':c.get('responsible_role','unspecified'),
                      'source_reference_count':len(c.get('source_refs',[])),
                      'completion_evidence':c.get('completion_evidence','unspecified'),
                      'failure_policy':c.get('failure_policy','unspecified'),
                      'input_count':len(c.get('inputs',[])) + (len(c.get('source_refs',[])) if c.get('role')=='preparation' else 0),
                      'required_heading_count':sum(len(x.get('required_headings',[])) for x in c.get('outputs',[]) if isinstance(x,dict)),
                      'action_kinds':[x.get('kind') for x in c.get('action_requirements',[]) if isinstance(x,dict) and x.get('kind')],
                      'approval_required':bool(c.get('approval_required') or c.get('action_requirements')) and not c.get('final_verification'),
                      'approval_model':approval_model,
                      'separate_human_approval_gate':c.get('human_approval_gate')=='separate_human_acceptance',
                      'retry_limit':c.get('max_retries') if type(c.get('max_retries')) is int and 0 <= c['max_retries'] <= 3 else None,
                      'evidence_required':bool(c.get('evidence_required')) or any(bool(x.get('evidence_required')) for x in c.get('action_requirements',[]) if isinstance(x,dict)),
                      'human_confirmation_required':bool(c.get('human_confirmation_required')),
                      'semantic_review_required':bool(c.get('semantic_review_required')),
                      'exit_check_count':total_checks,
                      'exit_check_codes':public_codes,
                      'exit_check_withheld_count':withheld_checks,
                      'exit_check_withheld_note':withheld_note,
                      'verification_items':public_items,
                      'verification_items_withheld_count':withheld_items,
                      'verification_aggregation':VERIFICATION_AGGREGATION_RULE,
                      'estimated_days':c.get('estimated_days'),
                      'period_months':[m for m in c.get('months',[]) if isinstance(m,str) and __import__('re').fullmatch(r'20\d{2}-(0[1-9]|1[0-2])',m)],'criteria_count':len(c.get('criterion_ids',[]))})
    used={x['public_purpose_code'] for x in tasks}
    public_exit_total=sum(int(x.get('exit_check_count') or 0) for x in tasks)
    public_exit_codes=sum(len(x.get('exit_check_codes') or []) for x in tasks)
    public_exit_withheld=sum(int(x.get('exit_check_withheld_count') or 0) for x in tasks)
    public_item_total=sum(len(x.get('verification_items') or []) for x in tasks)
    public_item_withheld=sum(int(x.get('verification_items_withheld_count') or 0) for x in tasks)
    # P1(修正C): 準備ゲートの構造情報だけを公開する。件数・要否・失敗時の扱い・
    # 合否チェックの件数と対象種別のみ。原本本文・氏名・ファイル名・秘密情報・
    # 内部の詳細条件は送らない(公開packet生成前の機密検査と明示許可は維持)。
    prep_gate_ids=sorted({str(gid) for t in tasks for gid in (t.get('internal_preparation_gate_ids') or []) if isinstance(gid,str)})
    prep_required=any(bool(t.get('preparation_gate_required')) for t in tasks if t.get('role')=='final_verification')
    prep_failure_action=''
    for t in tasks:
        if t.get('role')=='final_verification' and str(t.get('upstream_failure_action') or ''):
            prep_failure_action=str(t.get('upstream_failure_action'))
            break
    prep_method_kinds=sorted({str(item.get('method') or '') for t in tasks for item in (t.get('verification_items') or []) if isinstance(item,dict) and str(item.get('method') or '')})
    prep_check_kinds=(['exit_check'] if public_exit_total else [])+['verification:'+m for m in prep_method_kinds]
    # 段階2: 件数の意味を固定する。同じ内部契約から数えたtasks集計値を再利用し、別経路で数え直さない。
    # 準備工程だけの検査数と、全工程の公開検査数の合計を別のフィールドで示す。旧check_countは置かない。
    prep_tasks=[t for t in tasks if t.get('role')=='preparation']
    preparation_check_count=sum(int(t.get('exit_check_count') or 0) for t in prep_tasks)
    all_task_check_count=public_exit_total+public_item_total
    non_public_count=public_exit_withheld+public_item_withheld
    if non_public_count>0:
        unevaluable_scope='非公開%d件(公開できない検査・条件)は外部AIが評価できません。内容は送りません。' % non_public_count
    else:
        unevaluable_scope=''
    # 段階2: 判定情報を内部契約と一致させる。無い値は補完せずunknownとし、不完全を明示する。
    has_final=any(t.get('role')=='final_verification' for t in tasks)
    incomplete_reasons=[]
    if not tasks or not contracts:
        required_out='unknown'
        failure_out='unknown'
        aggregation_out='unknown'
        incomplete_reasons.append('内部契約を確認できません')
    elif not has_final:
        required_out='unknown'
        failure_out='unknown'
        aggregation_out='unknown'
        incomplete_reasons.append('最終検証の内部契約がありません')
    else:
        required_out=bool(prep_required)
        if prep_failure_action:
            failure_out=prep_failure_action
        else:
            failure_out='unknown'
            incomplete_reasons.append('準備失敗時の遷移が内部契約にありません')
        if public_item_total>0 or aggregate_item_count>0:
            aggregation_out=VERIFICATION_AGGREGATION_RULE
        else:
            aggregation_out='unknown'
            incomplete_reasons.append('最終検証の集約対象が内部契約にありません')
    packet={'goal_category':'controlled_business_execution','structure_version':PUBLIC_STRUCTURE_VERSION,
            'tasks':tasks,
            'purpose_catalog':{key:value for key,value in purpose_catalog.items() if key in used},
            'exit_check_disclosure':{'public_code_count':public_exit_codes,
                                     'total_count':public_exit_total,
                                     'withheld_count':public_exit_withheld,
                                     'withheld_note':('非公開%d件' % public_exit_withheld) if public_exit_withheld else '非公開0件',
                                     'meaning':'exit_check_countは全検査数、exit_check_codesは公開可能な種別だけ。差分は非公開であり内容は送らない。'},
            'verification_disclosure':{'public_item_count':public_item_total,
                                       'withheld_item_count':public_item_withheld,
                                       'withheld_note':('非公開%d件' % public_item_withheld) if public_item_withheld else '非公開0件',
                                       'aggregation':VERIFICATION_AGGREGATION_RULE,
                                       'methods':list(VERIFICATION_METHODS_PUBLIC),
                                       'meaning':'verification_itemsは項目ID・成果物番号・検査方法分類・必須/任意だけ。要求文・キーワード・原本参照は送らない。'},
            'preparation_gate_disclosure':{'internal_gate_count':len(prep_gate_ids),
                                           'preparation_gate_required':required_out,
                                           'preparation_failure_action':failure_out,
                                           'final_aggregation_rule':aggregation_out,
                                           'disclosure_incomplete':bool(incomplete_reasons),
                                           'incomplete_reasons':list(incomplete_reasons),
                                           'preparation_check_count':preparation_check_count,
                                           'preparation_check_count_scope':'準備工程のみ',
                                           'all_task_check_count':all_task_check_count,
                                           'all_task_check_count_scope':'全工程の合計(準備工程を含む)',
                                           'check_kinds':prep_check_kinds,
                                           'non_public_check_count':non_public_count,
                                           'unevaluable_scope':unevaluable_scope,
                                           'meaning':'準備工程の内部ゲートは構造情報(件数・要否・失敗時の扱い・合否チェックの件数と対象種別)だけ。原本本文・氏名・ファイル名・秘密情報・内部の詳細条件は送らない。criteria_count=0は「検証がない」の意味ではない。'},
            'final_verification_model':{**FINAL_VERIFICATION_MODEL,
                                        'aggregates_earlier_item_count':aggregate_item_count,
                                        'aggregates_criterion_ids':criterion_ids,
                                        'meaning':'最終検証は先行工程の項目別台帳を集約する。最終工程自身に新たなrequirement_matrixを複製しない。'}, 
            'output_classes':{'deliverable_count':sum(t['output_count'] for t in tasks if t['output_class']=='deliverable'),
                              'internal_record_count':sum(t['output_count'] for t in tasks if t['output_class']!='deliverable')},

            'external_evaluation_policy':{'unverifiable_is_not_pass':True,
                                          'local_originals_are_local_responsibility':True,
                                          'meaning':'非公開原本の実在・内容はローカル照合の責任。外部評価のunverifiableを合格に変えない。'},
            'limitations':['文書生成と実処理は別。機械検査は人間の意味確認を代替しない。']}
    if any(x['type']=='vehicle_calculate' for x in tasks):
        packet['limitations'].append('対応するExcel・CSV・請求PDFを原本参照付き明細へ自動抽出する。未対応形式・曖昧な配賦・税区分混在は暫定Excelに表示し、確定合格にしない。')
    if snapshot.get('detail'):
        packet['detail']=[{'step':x.get('id'),'operation':x.get('operation'),'parents':x.get('depends_on'),'validation':x.get('validation')} for x in snapshot['detail'].get('steps',[])]
    return packet


def send_allowed(manager, pid):
    mission = manager.memory.get_mission(pid)
    providers = list(dict.fromkeys(mission.get('external_providers', [])))
    configured = {x['id'] for x in manager.provider_statuses() if x.get('configured')}
    return bool(mission.get('allow_external_ai') and providers and set(providers) <= configured and manager.plan_review_runner)


def record_send_approval(manager, pid, signature, public_summary, providers=None):
    store = ReviewStore(manager.memory.path)
    snapshot, _current_signature = plan_snapshot(manager, pid)
    structure = public_structure(snapshot)
    check = check_public_consistency(snapshot, structure, public_summary)
    if not check.get("passed"):
        raise ValueError("送信前検査に失敗したため送信しません: " + " / ".join(check.get("reasons") or ["不一致"]))
    summary = build_public_purpose_summary(snapshot, public_summary)
    if not summary.get("complete"):
        raise ValueError("公開目的要約が不完全なため送信しません: " + ",".join(summary.get("missing") or ["不足"]))
    packet, packet_hash = build_final_packet(public_summary, snapshot)
    packet_check = verify_packet_hash(packet_hash, packet)
    if not packet_check.get("ok"):
        raise ValueError("送信packetのハッシュを確認できません: " + str(packet_check.get("reason") or "不明"))
    approval = {
        'approved': True, 'packet_hash': packet_hash, 'public_summary': public_summary,
        'providers': list(providers or []), 'approved_at': time.time(), 'signature': signature,
        'public_purpose': {k: summary.get(k) for k in ("purpose_text", "audience", "artifact_kinds", "deliverable_count", "internal_record_count", "criterion_ids", "criterion_count", "version")},
        'consistency': check,
        'packet_status': str(check['status']),
        'unevaluable_scope': check.get('unevaluable_scope') or '',
        'disclosure_incomplete': bool(check.get('disclosure_incomplete')),
    }
    store.put(pid, 'send_approval', signature, approval)
    save_orchestration(store, pid, last_completed_stage='send_approval', plan_signature=signature,
                       resume_from='external_review')
    return approval


def approved_packet(store, pid, signature, packet):
    approval = store.get(pid, 'send_approval', signature) or {}
    return bool(approval.get('approved') and approval.get('packet_hash') == fingerprint(packet))


async def review_plan(manager,pid,signature,public_summary,safe_to_send,tid=None,idempotency_key=None,providers_override=None,expected_packet_hash=None):
    if not safe_to_send or not 20<=len(public_summary.strip())<=12000:raise ValueError('秘密・個人情報を含まない公開用の目標・達成条件・工程説明を確認してください')
    # P0-A: 汎用文だけの目的説明は外部AIを呼ぶ前に止める(public_summaryの旧挙動は温存しない)。
    if is_generic_public_summary(public_summary):
        raise ValueError('packet_incomplete: 目的説明が汎用文だけのため送信しません。具体的な公開要約を入力・確認してください。外部AIは呼びません')
    detail=selected_detail(manager,pid,tid);snapshot,current=plan_snapshot(manager,pid,detail)
    if current!=signature:raise ValueError('計画が変わりました。再読込してください')
    structure = public_structure(snapshot)
    check = check_public_consistency(snapshot, structure, public_summary)
    if not check.get("passed"):
        raise ValueError('送信前検査に失敗したため送信しません: ' + " / ".join(check.get("reasons") or ["不一致"]))
    summary = build_public_purpose_summary(snapshot, public_summary)
    if not summary.get("complete"):
        raise ValueError('公開目的要約が不完全なため送信しません: ' + ",".join(summary.get("missing") or ["不足"]))
    packet, packet_hash = build_final_packet(public_summary, snapshot)
    if expected_packet_hash is None:
        # 旧経路は期待値が無いため照合不能。一致とは扱わず監査結果に明示する。
        hash_binding = "not_provided"
    else:
        verified = verify_packet_hash(expected_packet_hash, packet)
        if not verified.get("ok"):
            raise ValueError('packet_previewと実送信のハッシュが一致しません: ' + str(verified.get("reason") or "不明") + '。' + str(verified.get("guidance") or "再プレビューしてください。"))
        hash_binding = "matched"
    mission=manager.memory.get_mission(pid)
    allowed_providers=list(dict.fromkeys(mission.get('external_providers',[])))
    providers=(list(dict.fromkeys(providers_override)) if providers_override is not None else allowed_providers)
    if providers_override is not None:
        if not providers or not set(providers) <= set(allowed_providers):
            raise ValueError('検証AIはプロジェクトで許可済みのAIから選択してください')
        policy_config=load_review_policy(manager)
        section=_policy_section(policy_config,pid)
        required=section.get('required_providers',policy_config.get('required_providers') or [])
        if isinstance(required,str):required=[required]
        if not set(required or []) <= set(providers):
            raise ValueError('必須の検証AIを外せません')
        raw_min=section.get('min_success_count',policy_config.get('min_success_count'))
        try:minimum=int(raw_min) if raw_min is not None else None
        except (TypeError,ValueError):minimum=None
        if minimum is not None and len(providers)<minimum:
            raise ValueError('検証AIの数が最低合格数を下回ります')
    configured={x['id'] for x in manager.provider_statuses() if x.get('configured')}
    if not mission.get('allow_external_ai') or not providers or not set(providers)<=configured or not manager.plan_review_runner:
        raise ValueError('選択した外部AIの許可・接続が揃っていません。未検証のまま実行できません')
    guard = manager._outbound_guard(pid, "goal_gate_review", 'GOAL_GATE_V1\n'+canonical(packet), providers)
    if not guard.get("ok"):
        raise ValueError('送信前検査に失敗したため送信しません: privacy_check_failed:' + ','.join(str(c) for c in (guard.get("codes") or [])))
    store=ReviewStore(manager.memory.path)
    previous=store.get(pid,'plan',signature)
    if previous and previous.get('status')=='running' and time.time()-previous.get('started',0)<240:raise ValueError('同じ計画を検証中です')
    # packet_preview と実送信のバイト内容を一致させる(同じ作り方の値を再利用する)。
    # safe_to_send is the explicit send approval (step 6). Never send without it.
    record_send_approval(manager, pid, signature, public_summary, providers)
    job=None
    key=idempotency_key or fingerprint(['review_plan', pid, signature, packet])
    try:
        job=begin_job(store, pid, 'review_plan', key, extra={'plan_signature':signature,'task_id':tid})
    except ValueError:
        raise
    # Reuse only parsed API responses for the exact same reviewed packet and version.
    retained=[r for r in (previous or {}).get('reviews',[]) if r.get('status') in {'pass','conditional','fail','unverifiable'}] if (previous or {}).get('packet')==packet else []
    pending=[p for p in providers if not any(r.get('provider')==p for r in retained)]
    budget=review_budget(manager,pid)
    if pending and budget.get('managed') and budget.get('remaining_calls',0)<len(pending):
        queue={'status':'waiting_budget','signature':signature,'public_summary':public_summary,'providers':providers,
               'task_id':tid,'next_at':budget.get('reset_at',time.time()+3600),'expires':time.time()+7*86400,'attempts':0,
               'packet_hash':fingerprint(packet)}
        store.put(pid,'plan_queue',signature,queue)
        result={'status':'waiting_budget','packet':packet,'hash_binding':hash_binding,'reviews':retained,'pending_providers':pending,'required_calls':len(pending),'resume_at':queue['next_at'],'job_id':job['id'],'called_providers':[],'reused_providers':[r.get('provider') for r in retained]}
        store.put(pid,'plan',signature,result)
        finish_job(store,pid,job['id'],'succeeded',last_completed_stage='external_review')
        save_orchestration(store,pid,last_completed_stage='external_review',plan_signature=signature,resume_from='external_review')
        return result
    store.put(pid,'plan',signature,{'status':'running','started':time.time(),'packet':packet,'reviews':retained,'job_id':job['id']})
    try:
        from contextlib import nullcontext
        from app.experience_memory import configured_memory,memory_scope
        setting=configured_memory(manager.memory.path,pid)
        scope=memory_scope(setting[0],pid,signature,mode=setting[1],external_allowed=True) if setting else nullcontext()
        with scope:
            responses=await asyncio.wait_for(manager.plan_review_runner('GOAL_GATE_V1\n'+canonical(packet),pending),timeout=180) if pending else []
        parsed=[]
        for provider in providers:
            saved=next((r for r in retained if r.get('provider')==provider),None)
            if saved:
                parsed.append(saved);continue
            r=next((x for x in responses if x.get('id')==provider),{})
            from app.external_ai import connection_error_from_payload
            conn=connection_error_from_payload(r)
            verdict={'provider':provider,'status':'unverified','issues':[]}
            if conn:
                verdict.update(status='connection_error',connection_error=conn,issues=[])
            elif r.get('ok'):
                try:
                    from app.structured_planning import decode_object
                    body=decode_object(r.get('review',''))
                    if body.get('verdict') in {'pass','conditional','fail','unverifiable'} and isinstance(body.get('issues'),list):
                        verdict.update(status=body['verdict'],issues=body['issues'])
                except (ValueError,TypeError):pass
            verdict['response']=r;parsed.append(verdict)
        policy=review_pass_policy(manager,pid,len(providers))
        judged=evaluate_external_review_status(parsed,providers,policy)
        if plan_snapshot(manager,pid,selected_detail(manager,pid,tid))[1]!=signature:
            judged={'status':'not_passed','stop_kind':'plan','stop_reason':'','success_count':0,'required_count':policy.get('min_success_count')}
        connection_errors=[{
            'provider':x['provider'],
            'status_code':(x.get('connection_error') or {}).get('status_code') or (x.get('response') or {}).get('status_code'),
            'category':(x.get('connection_error') or {}).get('category') or (x.get('response') or {}).get('error_category') or 'connection_error',
            'error':((x.get('connection_error') or {}).get('error') or (x.get('response') or {}).get('error') or '')[:2000],
        } for x in parsed if x.get('status')=='connection_error']
        result={'status':judged['status'],'reviews':parsed,'packet':packet,'hash_binding':hash_binding,'finished':time.time(),
                'called_providers':list(pending),'reused_providers':[r.get('provider') for r in retained],
                'pending_providers':[x['provider'] for x in parsed if x['status'] in {'unverified','connection_error'}],
                'job_id':job['id'] if job else '','provider_outcomes':provider_review_outcomes(parsed,providers),
                'connection_errors':connection_errors,'pass_policy':policy,
                'success_count':judged.get('success_count',0),'required_count':judged.get('required_count'),
                'stop_kind':judged.get('stop_kind') or '','stop_reason':judged.get('stop_reason') or ''}
    except BaseException as exc:
        store.put(pid,'plan',signature,{'status':'unverified','error':type(exc).__name__,'packet':packet,'reviews':retained})
        if job:finish_job(store,pid,job['id'],'failed',blocking_error=type(exc).__name__)
        raise
    store.put(pid,'plan',signature,result)
    queued=store.get(pid,'plan_queue',signature)
    if queued and queued.get('status')!='cancelled' and result['status']!='waiting_budget':
        queued['status']='finished';store.put(pid,'plan_queue',signature,queued)
    job_status='succeeded' if result['status']=='passed' else ('needs_attention' if result['status']=='connection_failed' else 'failed')
    if job:finish_job(store,pid,job['id'],job_status,last_completed_stage='external_review',
                      blocking_error=result.get('stop_reason') or '')
    save_orchestration(store,pid,last_completed_stage='external_review',plan_signature=signature,
                       resume_from='plan_approval' if result['status']=='passed' else 'external_review',
                       blocking_error=result.get('stop_reason') or '',
                       stop_kind=result.get('stop_kind') or '')
    if result['status']=='passed':
        revision=store.get(pid,'revision',signature) or {}
        if revision.get('lifecycle')=='revalidation_pending' or revision.get('status')=='applied':
            for action in revision.get('actions') or []:
                if action.get('disposition') in {'amend','rebuild_vehicle'}:
                    action['lifecycle']='validated'
            revision['lifecycle']='validated'
            store.put(pid,'revision',signature,revision)
    if result['status']=='passed':
        current=manager.memory.get_mission(pid)
        for task in current['tasks']:
            message='外部AIによる詳細計画' if tid else '外部AIによる全体計画'
            if (not tid or task['id']==tid) and task['status']=='needs_review' and message in task.get('error',''):
                manager.memory.update_task(task['id'],'pending',error='')
    manager.memory.add_event(pid,'goal_plan_review','外部AIによる目標適合性検証: '+result['status'],detail=canonical({'signature':signature,'task_id':tid,'status':result['status']}))
    return result


def execution_snapshot(manager,pid):
    mission=manager.memory.get_mission(pid);project=manager.memory.get_project(pid);artifacts=[];failures=[]
    if mission['status']!='completed':failures.append('全体目標が未達成です')
    if not mission['tasks']:failures.append('実行タスクがありません')
    def resolve(path):return manager.workspace.resolve_file(project.get('workspace_path',''),pid,path,must_exist=True)[2]
    for task in mission['tasks']:
        if task['status']!='completed':failures.append(task['task_key']+': 未完了です')
        c=contract_of(task)
        if not c:failures.append(task['task_key']+': 検証可能な成果物契約がありません');continue
        failures.extend(verify_outputs(task,resolve))
        for output in c.get('outputs',[]):
            try:artifacts.append({'path':output['path'],'hash':fingerprint(resolve(output['path']).read_bytes().hex())})
            except (ValueError,OSError):failures.append(output['path']+': 成果物がありません')
    if not artifacts:failures.append('検証できる成果物がありません')
    from app.vehicle_workflow import applicable,goal_failures
    if applicable(mission):failures.extend(goal_failures(manager,pid))
    inputs=[{'id':x['id'],'sha256':x.get('sha256'),'content_hash':fingerprint(x.get('content',''))} for x in manager.memory.list_context_files(pid,include_content=True) if x.get('source')!='memo']
    value={'plan':plan_snapshot(manager,pid)[1],'artifacts':artifacts,'sources':sorted(inputs,key=lambda x:x['id']),
           'workspace_path':project.get('workspace_path',''),'workspace_root':str(manager.workspace.root)}
    return value,fingerprint(value),list(dict.fromkeys(failures))


def evidence_valid(memory_path,pid,evidence):
    try:
        from app.memory.short_term import ShortTermMemory
        from app.workspace_files import WorkspaceSandbox
        row=ReviewStore(memory_path).get(pid,'result',evidence['human_result'])
        if not row or row['status']!='approved' or row.get('expires',0)<=time.time() or row.get('experience_id')!=evidence.get('experience_id'):return False
        proof=row['snapshot'];memory=ShortTermMemory(memory_path)
        project=memory.get_project(pid)
        if project.get('workspace_path','')!=proof['workspace_path']:return False
        workspace=WorkspaceSandbox(Path(proof['workspace_root']))
        for f in proof['artifacts']:
            path=workspace.resolve_file(proof['workspace_path'],pid,f['path'],must_exist=True)[2]
            if fingerprint(path.read_bytes().hex())!=f['hash']:return False
        current=[{'id':x['id'],'sha256':x.get('sha256'),'content_hash':fingerprint(x.get('content',''))} for x in memory.list_context_files(pid,include_content=True) if x.get('source')!='memo']
        # New independent sources are allowed; reviewed originals must remain identical.
        by_id={x['id']:x for x in current}
        return all(by_id.get(x['id'])==x for x in proof['sources'])
    except (ValueError,KeyError,TypeError,OSError):return False


async def approve_result(manager,pid,signature,reviewer,notes,lesson,conditions,checks,days=30):
    if not reviewer.strip() or not notes.strip() or len(lesson.strip())<20 or not conditions.strip() or checks!=[True,True,True] or not 1<=days<=366:
        raise ValueError('確認者・確認記録・再利用手順・適用条件と3項目の確認が必要です')
    require_review(manager,pid)
    snapshot,current,failures=execution_snapshot(manager,pid)
    if signature!=current or failures:raise ValueError('現行結果を承認できません: '+' / '.join(failures or ['結果が変わりました']))
    store=ReviewStore(manager.memory.path);prior=store.get(pid,'result',signature)
    if prior and prior.get('status')=='approved' and prior.get('expires',0)>time.time():return prior
    root=Path(manager.memory.path).parent/'experience_memory';experiences=ExperienceStore(root/'experience.sqlite3')
    rid=experiences.add(pid,'success',lesson+'\n適用条件: '+conditions,{}, {'human_result':signature})
    # Bind the authoritative row to this exact experience; an orphan cannot be retrieved.
    with experiences.connect() as db:db.execute('UPDATE experiences SET evidence=? WHERE id=? AND project=?',(canonical({'human_result':signature,'experience_id':rid}),rid,pid))
    experiences.review(pid,rid,'verified',reviewer,notes,time.time()+days*86400)
    row={'status':'approved','snapshot':snapshot,'experience_id':rid,'reviewer':reviewer,'notes':notes,'lesson':lesson,'conditions':conditions,'approved_at':time.time(),'expires':time.time()+days*86400,'index_status':'pending'}
    store.put(pid,'result',signature,row)
    from app.experience_memory import configured_memory
    setting=configured_memory(manager.memory.path,pid)
    try:
        if setting:
            await asyncio.to_thread(setting[0].reindex,pid);row['index_status']='indexed'
        else:row['index_status']='rag_disabled'
    except Exception as exc:row['index_status']='index_failed';row['index_error']=type(exc).__name__
    store.put(pid,'result',signature,row)
    from app.executable_recipes import register
    register(manager,pid,signature,row)
    manager.memory.add_event(pid,'human_result_rag_approved','人間の結果確認に基づくRAG登録',detail=canonical({'signature':signature,'experience_id':rid,'reviewer':reviewer,'index_status':row['index_status']}))
    return row


def revoke_result(manager,pid,signature,reviewer,notes):
    if not reviewer.strip() or not notes.strip():raise ValueError('取消者と理由が必要です')
    store=ReviewStore(manager.memory.path);row=store.get(pid,'result',signature)
    if not row:raise ValueError('確認記録がありません')
    row['status']='revoked';row['revoked_by']=reviewer;row['revocation_reason']=notes
    store.put(pid,'result',signature,row)
    recipe=store.get(pid,'executable_recipe',signature)
    if recipe:
        recipe=dict(recipe);recipe['status']='revoked'
        store.put(pid,'executable_recipe',signature,recipe)
    ExperienceStore(Path(manager.memory.path).parent/'experience_memory'/'experience.sqlite3').review(pid,row['experience_id'],'revoked',reviewer,notes,0)
    # P1-A: 撤回時は正本で即時不採用とし索引から削除する。検索結果の再照合は維持される。
    # F2a fail-closed: 索引削除の失敗を握りつぶさず記録する(取消成功と区別できる)。
    try:
        from app.experience_memory import configured_memory
        setting=configured_memory(manager.memory.path,pid)
        if setting:
            setting[0].remove_from_index(pid,row['experience_id'],reviewer,notes)
            row['index_removal'] = 'removed'
        else:
            row['index_removal'] = 'rag_disabled'
    except (OSError, ValueError, RuntimeError, AttributeError, KeyError, TypeError) as exc:
        row['index_removal'] = f'failed:{type(exc).__name__}'
        store.put(pid,'result',signature,row)
    return row


def review_budget(manager,pid):
    from app.experience_memory import configured_memory
    setting=configured_memory(manager.memory.path,pid)
    if not setting:return {'managed':False}
    memory,mode=setting;budget=memory.config.get('budget',{})
    period=time.strftime('%Y-%m-%d',time.gmtime())
    with memory.store.connect() as db:
        calls,reserved=db.execute('SELECT COUNT(*),COALESCE(SUM(reserved_micro),0) FROM requests WHERE project=? AND period=?',(pid,period)).fetchone()
    call_limit=int(budget.get('daily_calls',0));money_limit=int(budget.get('daily_micro_usd',0));per_call=int(budget.get('per_call_micro_usd',0))
    remaining=max(0,min(call_limit-calls,(money_limit-reserved)//per_call)) if per_call>0 else 0
    return {'managed':True,'mode':mode,'period_utc':period,'used_calls':calls,'daily_calls':call_limit,'remaining_calls':remaining,
            'reset_at':(int(time.time())//86400+1)*86400}


def all_reviews_history(manager, pid):
    """履歴表示専用: legacy review を含めた全レビュー・指摘の閲覧用履歴を取得する。"""
    store = ReviewStore(manager.memory.path)
    mission = manager.memory.get_mission(pid)
    legacy = [dict(x, origin='legacy_mission', source_plan_version=None) for x in mission.get('plan_reviews', []) if isinstance(x, dict)]
    legacy_store = [payload for _, payload in store.list(pid, 'legacy_feedback')]
    plan_reviews = [{'signature': sig, **payload} for sig, payload in store.list(pid, 'plan')]
    user_feedbacks = [{'signature': sig, **payload} for sig, payload in store.list(pid, 'feedback')]
    return {
        'mission_legacy_reviews': legacy,
        'legacy_feedback_store': legacy_store,
        'plan_reviews': plan_reviews,
        'user_feedbacks': user_feedbacks,
    }


def execution_gate(manager,pid):
    try:
        from app.plan_repair_loop import approval_gate
        approval_gate(manager, pid)
        require_review(manager,pid)
        # Stage 2-A (追加のみ): missing_capability が残る計画は実行承認不可。
        # 既存のゲートを弱めない (不可にしかしない)。
        try:
            from app.capability_gap import blocks_execution_approval as _gap_gate
            _gap = _gap_gate(manager, pid)
            if isinstance(_gap, dict) and _gap.get("blocked"):
                return {"blocked": True,
                        "reason": _gap.get("reason") or "不足機能の提案が残っているため実行承認できません"}
        except Exception:
            # F2a fail-closed: 不足機能の確認に失敗したら不明=停止側に倒す(通過させない)。
            return {"blocked": True,
                    "reason": "不足機能の確認に失敗したため実行承認できません"}
        return {'blocked':False,'reason':''}
    except ValueError as exc:
        text=str(exc)
        if '修復ループ' in text:
            return {'blocked':True,'reason':text}
        if '追加開発' in text or '業務事実' in text:
            return {'blocked':True,'reason':text}
        if '接続設定' in text or '接続に失敗' in text:
            return {'blocked':True,'reason':text,'stop_kind':'connection_settings'}
        return {'blocked':True,'reason':'計画の人間承認は記録できます。外部AIによる現行版の検証が未完了または未合格のため、実行開始はできません。「目標検証・人間確認・RAG」で指摘対応と再検証を行ってください。'}


def require_human_final_confirmation_ready(gate_result: dict) -> None:
    """Reject human final confirmation until every criterion is independently PASS."""
    from app.completion_gate import criteria_ready_for_human_confirmation

    if not criteria_ready_for_human_confirmation(gate_result):
        raise ValueError("COMPLETION_GATE_FAILED: 全達成条件がPASSした後にだけ最終確認できます")
