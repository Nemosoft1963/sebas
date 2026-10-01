"""Small planning responses compiled into persistent, executable contracts."""
from __future__ import annotations

import json
import re
import csv
import io
import unicodedata
from copy import deepcopy

SCHEMA = "local-cowork-plan/v1"
MAX_CRITERIA = 18  # Reserve one preparation task and one final verification task.
EVAL_TASK_RE = re.compile(
    r"計画草案の評価と改善提案|計画(?:案|草案)?の評価(?:と改善提案)?|改善提案のみ",
)
FINAL_TASK_RE = re.compile(r"最終検証|final[_\s-]*verification", re.I)


def extract_criteria(goal: str, success: str) -> list[str]:
    extra_match = re.search(r'\u3010\u8ffd\u52a0\u8981\u4ef6\u3011', goal)
    extra_section = goal[extra_match.end():] if extra_match else ''
    if extra_section:
        extra_section = re.split(r'\u3010\u518d\u8a08\u753b\u6761\u4ef6\u3011|^#{1,4}\s', extra_section, maxsplit=1, flags=re.M)[0]
    extra_lines = re.findall(r'(?m)^\s*\d+[.\uff09)]\s*(.+)$', extra_section)
    match = re.search(r"(?m)^#{1,4}\s*達成条件\s*$", goal)
    section = goal[match.end():] if match else ""
    if section:
        section = re.split(r"(?m)^#{1,4}\s", section, maxsplit=1)[0]
    lines = re.findall(r"(?m)^\s*\d+[.）)]\s*(.+)$", section or success)
    criteria = list(dict.fromkeys(line.strip() for line in [*extra_lines, *lines] if line.strip()))
    if section and success.strip() and success.strip() not in criteria:
        criteria.append(success.strip())
    if not criteria:
        criteria = list(dict.fromkeys(re.sub(r"^[-・●\s]+", "", line).strip()
                    for line in (section or success).splitlines() if line.strip()))
    return criteria


def decode_object(response: str) -> dict:
    start, end = response.find("{"), response.rfind("}")
    value = json.loads(response[start:end + 1]) if start >= 0 and end > start else None
    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    return value


def sanitize_proposal(proposal: dict) -> dict:
    '''Remove backend-owned locations and operations from model-authored scope.'''
    value = deepcopy(proposal)
    scope = value.get('scope')
    if not isinstance(scope, str):
        return value
    replacements = (
        (r'https?://[^\s、。)）]+', '運営会社の公開情報'),
        (r'workspace:[^\s、。)）]+', '登録済み資料'),
        (r'result/[^\s、。)）]+', '成果物'),
        (r'[^\s、。)）]+\.(?:pdf|docx|xlsx)', '資料'),
        (r'\b(?:shell|pandas)\b', '安全な処理方法'),
    )
    for pattern, replacement in replacements:
        scope = re.sub(pattern, replacement, scope, flags=re.I)
    value['scope'] = re.sub(r'\s+', ' ', scope).strip()
    return value


def requires_google_site_publication(criterion: str) -> bool:
    return bool(re.search(
        r'(?:Google\s*Sites|ランディングページ|公開LP).{0,80}(?:公開|公開URL)',
        criterion,
        re.I | re.S,
    ))


EXTERNAL_ACTION_RE = re.compile(
    r"(?:顧客(?:へ|に).{0,20}(?:送信|連絡|提案)|商談.{0,10}実施|"
    r"PoC.{0,10}実施|契約締結|仮説検証.{0,20}実行|"
    r"Google\s*Forms|Google\s*Sites|SNS.{0,40}(?:投稿|公開)|リード取得)",
    re.I | re.S,
)
VERDICT_HEADING_RE = re.compile(
    r"(?m)^#{2,6}\s+((?:SC|C)\d{2}(?:\s*[,、]\s*(?:SC|C)\d{2})*)\s*[—\-ー–−]\s*(PASS|FAIL|BLOCKED|UNTESTABLE)\b",
    re.I,
)
VERDICT_LINE_RE = re.compile(
    r"(?m)^\s*[-*]\s*((?:SC|C)\d{2})\s*[:：]\s*(PASS|FAIL|BLOCKED|UNTESTABLE)\b",
    re.I,
)


def is_external_control_requirement(criterion: str) -> bool:
    return bool(re.search(
        r'実行工程.{0,20}追加|自動投稿せず|個別.{0,20}人間承認|証拠.{0,20}達成判定|失敗時.{0,20}分類',
        str(criterion or ""), re.I | re.S,
    ))

def criterion_requires_external_action(criterion: str) -> bool:
    text = str(criterion or "")
    return bool(EXTERNAL_ACTION_RE.search(text) or requires_google_site_publication(text))


def parse_criterion_verdicts(content: str) -> dict[str, str]:
    """Read explicit per-criterion verdicts from a final-verification document."""
    verdicts: dict[str, str] = {}
    for match in VERDICT_HEADING_RE.finditer(content or ""):
        status = match.group(2).upper()
        for cid in re.findall(r"(?:SC|C)\d{2}", match.group(1)):
            verdicts[cid] = status
    for match in VERDICT_LINE_RE.finditer(content or ""):
        verdicts[match.group(1)] = match.group(2).upper()
    return verdicts


def requires_public_web_research(criterion: str) -> bool:
    return bool(re.search(
        r"(?:(?:web|ウェブ|インターネット|オンライン).{0,40}(?:検索|調査|収集|取得|確認)|"
        r"(?:検索|調査|収集|取得|確認).{0,40}(?:web|ウェブ|インターネット|オンライン)|"
        r"(?:外部の)?情報検索(?:スキル|タスク)?.{0,30}(?:最優先|優先)|"
        r"(?:情報検索|検索スキル).{0,60}(?:使|利用|検索|調査|収集|取得|反映)|"
        r"(?:ものづくり|モノづくり|補助金|公募要領).{0,40}(?:検索|調査|収集|取得)|"
        r"(?:最新|現行|公式).{0,30}(?:情報|公募要領|制度|法令|仕様).{0,40}(?:検索|調査|収集|取得|確認|反映)|"
        r"(?:公募要領|公式(?:サイト|ページ|PDF|資料|情報)).{0,40}(?:検索|調査|収集|取得|確認))",
        criterion,
        re.I | re.S,
    ))


def public_web_query(criterion: str) -> str:
    """Build a public-only query instead of forwarding the full project instruction."""
    if re.search(r"ものづくり|モノづくり", criterion):
        return "ものづくり補助金 最新 公募要領 公式 PDF"
    if re.search(r"補助金|公募要領", criterion):
        return "補助金 最新 公募要領 公式 PDF"
    if re.search(r"法令|法律|省令|告示", criterion):
        return "最新 法令 公式"
    if re.search(r"仕様|規格", criterion):
        return "最新 公式 仕様 規格"
    compact = re.sub(
        r"(?:個人情報|認証情報|試算表|社内資料|秘密|機密).{0,80}", "",
        criterion, flags=re.I,
    )
    compact = re.sub(r"\s+", " ", compact).strip()
    return compact[:180]


def normalize_title(title: str) -> str:
    """Comparison form that absorbs whitespace and common notation differences."""
    text = unicodedata.normalize("NFKC", str(title or ""))
    text = text.casefold()
    text = re.sub(r"[\s　]+", "", text)
    text = re.sub(r"[「」『』【】\[\]()（）〔〕〈〉<>・,，、。.\-‐–—_/\\:：;；]+", "", text)
    return text


def is_evaluation_task(task: dict) -> bool:
    blob = " ".join(str(task.get(key) or "") for key in ("task_key", "title"))
    if FINAL_TASK_RE.search(str(task.get("title") or "")):
        return False
    return bool(EVAL_TASK_RE.search(blob))


def is_final_verification_task(task: dict) -> bool:
    if str(task.get("task_key") or "") == "final_verification":
        return True
    contract = contract_of(task)
    if contract and contract.get("final_verification"):
        return True
    return bool(FINAL_TASK_RE.search(str(task.get("title") or "")))


def _uniquify_task_titles(tasks: list[dict]) -> None:
    seen: set[str] = set()
    for task in tasks:
        original = str(task.get("title") or "").strip() or str(task.get("task_key") or "task")
        title = original
        norm = normalize_title(title)
        extra = 2
        while norm and norm in seen:
            title = f"{original} ({task.get('task_key')})" if extra == 2 else f"{original} ({extra})"
            extra += 1
            norm = normalize_title(title)
        task["title"] = title
        if norm:
            seen.add(norm)


def inspect_plan_structure(
    plan: dict,
    expected_ids: set[str] | None = None,
    review_count: int = 0,
) -> dict:
    """Post-generation structural checks that keep review text from exploding the plan."""
    errors: list[str] = []
    tasks = plan.get("tasks") or []
    if not isinstance(tasks, list):
        return {"passed": False, "issues": ["tasks_missing"], "criterion_ids": []}
    seen_keys: set[str] = set()
    seen_titles: set[str] = set()
    eval_keys: list[str] = []
    final_keys: list[str] = []
    exec_by_criterion: dict[str, list[str]] = {}
    artifacts_by_criterion: dict[str, list[str]] = {}
    verify_keys: list[str] = []
    for task in tasks:
        if not isinstance(task, dict):
            errors.append("invalid task")
            continue
        key = str(task.get("task_key") or "")
        if not key:
            errors.append("missing task_key")
            continue
        if key in seen_keys:
            errors.append(f"{key}: duplicate task_key")
        seen_keys.add(key)
        norm = normalize_title(task.get("title") or "")
        if norm:
            if norm in seen_titles:
                errors.append(f"{key}: duplicate normalized title")
            seen_titles.add(norm)
        evaluation = is_evaluation_task(task)
        final = is_final_verification_task(task)
        if evaluation:
            eval_keys.append(key)
        if final:
            final_keys.append(key)
            verify_keys.append(key)
        contract = contract_of(task)
        owned = list((contract or {}).get("criterion_ids") or [])
        if final or evaluation:
            continue
        for cid in owned:
            exec_by_criterion.setdefault(cid, []).append(key)
            for output in (contract or {}).get("outputs") or []:
                path = output.get("path") if isinstance(output, dict) else None
                if path:
                    artifacts_by_criterion.setdefault(cid, []).append(str(path))
    if len(eval_keys) > 1:
        errors.append("evaluation_task_limit")
    if review_count >= 2 and len(eval_keys) > 1:
        errors.append("evaluation_tasks_scale_with_reviews")
    if len(final_keys) > 1:
        errors.append("final_verification_duplicate")
    for cid, owners in exec_by_criterion.items():
        unique_owners = list(dict.fromkeys(owners))
        if len(unique_owners) > 1:
            errors.append(f"{cid}: multiple primary execution tasks")
    if expected_ids:
        for cid in sorted(expected_ids):
            owners = list(dict.fromkeys(exec_by_criterion.get(cid) or []))
            if not owners:
                errors.append(f"{cid}: missing execution task")
            elif not artifacts_by_criterion.get(cid):
                errors.append(f"{cid}: missing artifact")
            elif not verify_keys:
                errors.append(f"{cid}: missing verification")
    return {
        "passed": not errors,
        "issues": errors,
        "criterion_ids": sorted(exec_by_criterion),
        "evaluation_tasks": eval_keys,
        "final_verification_tasks": final_keys,
        "coverage": {
            cid: {
                "exec_task_keys": list(dict.fromkeys(exec_by_criterion.get(cid) or [])),
                "artifact_paths": list(dict.fromkeys(artifacts_by_criterion.get(cid) or [])),
                "verify_task_keys": list(final_keys),
            }
            for cid in sorted(set(exec_by_criterion) | set(expected_ids or []))
        },
    }


def contract_of(task: dict) -> dict | None:
    try:
        value = json.loads(task.get("acceptance_criteria", ""))
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get("schema") == SCHEMA else None


def compile_task(index: int, criterion: str, proposal: dict, source_ids: list[str]) -> dict:
    key = f"SC{index:02d}"
    title = proposal.get("title")
    scope = proposal.get("scope")
    headings = proposal.get("headings")
    if not isinstance(title, str) or not title.strip() or not isinstance(scope, str) or not scope.strip():
        raise ValueError("title and scope are required strings")
    if not isinstance(headings, list) or not 2 <= len(headings) <= 8 or any(
        not isinstance(item, str) or not item.strip() or len(item) > 100 for item in headings
    ):
        raise ValueError("headings must contain 2-8 nonempty strings")
    if len(scope) > 1600 or len(title) > 120:
        raise ValueError("proposal too long")
    # External actions are never granted by the model's proposal.
    if re.search(r"(?:https?://|workspace:|result/|\.pdf|\.docx|\.xlsx|shell|pandas)", scope, re.I):
        raise ValueError("scope must describe content; paths and operations are backend-owned")
    path = f"result/sc{index:02d}.md"
    dependencies = proposal.get("depends_on", [])
    allowed_dependencies = {f'SC{i:02d}' for i in range(1, index)}
    dependencies = (
        list(dict.fromkeys(
            dep for dep in dependencies
            if isinstance(dep, str) and dep in allowed_dependencies
        ))
        if isinstance(dependencies, list) else []
    )
    headings = list(dict.fromkeys([item.strip() for item in headings] + ["根拠と未確認事項", "実施状態と次の行動"]))
    contract = {
        "schema": SCHEMA, "criterion_ids": [key], "criterion": criterion,
        "inputs": [f"result/{dep.lower()}.md" for dep in dependencies],
        "source_refs": ["context:" + value for value in source_ids],
        "outputs": [{"path": path, "required_headings": headings, "minimum_characters": 600}],
        "external_actions": "approval_required",
        "role": "execution", "responsible_role": "system", "estimated_days": 2,
        "completion_evidence": "validated_artifact", "failure_policy": "stop_and_report",
        "evidence_required": True,
        "exit_checks": ["source_evidence_linked", "criterion_content_verified", "unknowns_and_failures_reported"],
        "artifact_category": "decision_document",
    }
    purpose_code = 'criterion_delivery'
    for pattern, code in [
        (r'Google.{0,20}SNS.{0,40}実行工程.{0,20}追加', 'multi_channel_workflow_design'),
        (r'実行モニター|投稿実績|リード獲得数', 'execution_monitoring_evidence'),
        (r'SNS.{0,20}自動投稿せず', 'manual_social_posting_guard'),
        (r'個別.{0,20}人間承認', 'external_approval_boundaries'),
        (r'実行証拠.{0,20}達成判定', 'evidence_based_completion'),
        (r'公開後.{0,80}リード評価', 'campaign_execution_sequence'),
        (r'失敗時.{0,30}原因.{0,20}分類', 'failure_recovery_policy'),
        (r'市場|顧客像|顧客課題', 'market_and_customer_definition'),
        (r'商品|契約プラン', 'service_package_design'),
        (r'提供範囲|価格|除外事項', 'commercial_terms'),
        (r'紹介資料|提案書|ヒアリング|見積', 'sales_assets'),
        (r'販売計画|営業チャネル|KPI|収支', 'sales_plan'),
        (r'営業文面|商談台本|フォロー', 'outreach_content'),
        (r'見込み客|優先順位', 'prospect_prioritization'),
        (r'案件管理', 'pipeline_tracking'),
        (r'受注後|標準手順|導入', 'delivery_process'),
        (r'実行報告|状態報告|実際の営業活動.{0,40}報告', 'execution_status'),
    ]:
        if re.search(pattern, criterion, re.I):
            purpose_code = code
            break
    contract['public_purpose_code'] = purpose_code
    web_research = requires_public_web_research(criterion)
    if web_research:
        contract["public_web_research"] = {
            "required": True,
            "query": public_web_query(criterion),
            "official_sources_required": bool(re.search(r"公募要領|法令|制度|補助金", criterion)),
        }
    if re.search(r'案件管理|商談.{0,20}管理|(?:営業|顧客).{0,20}(?:管理表|CSV)', criterion, re.I):
        contract['outputs'].append({
            'path': f'result/sc{index:02d}_tracker.csv',
            'required_columns': ['案件ID', '顧客要件', '商談結果', '状態', '次回行動', '失注理由'],
            'minimum_rows': 1,
        })
    if purpose_code == 'campaign_execution_sequence':
        ordered_actions = [
            'google_site_publication', 'social_posting_kit_generation',
            'social_copy_approval', 'manual_social_post',
            'post_url_registration', 'form_response_sync', 'lead_evaluation',
        ]
        contract['execution_kind'] = 'campaign_execution_sequence'
        contract['ordered_actions'] = ordered_actions
        contract['action_requirements'] = [
            {'kind': kind, 'sequence': position, 'minimum_executed': 1, 'evidence_required': True}
            for position, kind in enumerate(ordered_actions, 1)
        ]
        contract.update({'role':'external_action','responsible_role':'human_approver',
                         'estimated_days':2,'completion_evidence':'registered_external_evidence',
                         'artifact_category':'external_action_record','human_confirmation_required':True,
                         'semantic_review_required':True})
    elif not is_external_control_requirement(criterion) and requires_google_site_publication(criterion):
        contract['execution_kind'] = 'google_site_publication'
        contract['action_requirements'] = [{
            'kind': 'google_site_publication',
            'minimum_executed': 1,
            'evidence_required': True,
        }]
        contract.update({'role':'external_action','responsible_role':'human_approver',
                         'estimated_days':1,'completion_evidence':'registered_external_evidence',
                         'artifact_category':'external_action_record','human_confirmation_required':True,
                         'semantic_review_required':True})
    elif not is_external_control_requirement(criterion) and criterion_requires_external_action(criterion):
        kind = 'approved_external_action'
        if re.search(r'Google\s*Forms', criterion, re.I):
            kind = 'google_form_publication'
        elif re.search(r'SNS', criterion, re.I):
            kind = 'social_post'
        elif re.search(r'リード取得', criterion, re.I):
            kind = 'lead_capture'
        elif re.search(r'公開', criterion, re.I):
            kind = 'approved_publication'
        elif re.search(r'送信|連絡', criterion, re.I):
            kind = 'approved_outbound_communication'
        elif re.search(r'契約', criterion, re.I):
            kind = 'approved_contract_confirmation'
        elif re.search(r'営業|商談|面談|提案|実行', criterion, re.I):
            kind = 'approved_customer_engagement'
        contract['public_purpose_code'] = kind
        contract['execution_kind'] = kind
        contract['action_requirements'] = [{
            'kind': kind,
            'minimum_executed': 1,
            'evidence_required': True,
        }]
        contract.update({'role':'external_action','responsible_role':'human_approver',
                         'estimated_days':1,'completion_evidence':'registered_external_evidence',
                         'artifact_category':'external_action_record','human_confirmation_required':True,
                         'semantic_review_required':True})
    if purpose_code in {'commercial_terms', 'sales_assets', 'outreach_content'}:
        contract.update({
            'approval_required': True,
            'human_confirmation_required': True,
            'semantic_review_required': True,
            'reviewer_role': 'human_approver',
            'completion_evidence': 'validated_artifact_and_human_confirmation',
        })
    return {
        "task_key": key, "depends_on": dependencies, "title": title.strip(),
        "mode": "research" if web_research else "local",
        "description": (
            f"達成条件 {key}: {criterion}\n内容設計: {scope}\n"
            f"operations/write_textで{path}へ完成した日本語Markdownを保存する。\n"
            "根拠資料を確認し、事実・仮説・要確認を区別する。価格や成果の保証を捏造しない。"
            "外部への送信や契約など承認対象の操作は、未承認なら承認待ちと明記する。"
            "必要なデータがない場合は不足資料と検証手順を記載し、数値・単位・配賦基準を推測で補わない。"
            "計画やテンプレートの作成を実処理の完了と判定しない。\n"
            + "登録原本: " + ", ".join(contract["source_refs"])
            + ("\n公開Web検索はこのタスクに限り許可済み。HTTPSの公開情報だけを取得し、公式一次資料を優先する。"
               "プロジェクト原本・試算表・個人情報・認証情報を検索語や外部AIへ送らない。"
               "取得URL、取得日時、本文ハッシュを根拠として保存し、ローカルLLMが本文を分析する。"
               if web_research else "")
            + ('\n案件管理CSVもtable_operations/create_tableで保存する。実データがない場合は状態を「未入力テンプレート」とした記入用の1行を作り、顧客名・実績・面談を捏造しない。' if len(contract['outputs']) > 1 else '')
        ),
        "acceptance_criteria": json.dumps(contract, ensure_ascii=False),
    }


def compile_plan(criteria: list[str], tasks: list[dict], goal: str = "") -> dict:
    if not 1 <= len(criteria) <= MAX_CRITERIA or len(tasks) != len(criteria):
        raise ValueError("criterion count must be 1-18 and all criteria need tasks")
    tasks = deepcopy(tasks)
    # Domain decisions use the user mission, never a model-authored proposal.
    financial = bool(re.search(r"損益|利益|収支|原価", goal))
    sales = not financial and bool(re.search(r"営業活動|商談|ニーズ探索|顧客開拓|販売促進", goal))
    if sales:
        criterion = '登録原本の確認とニーズ・シーズ検証の初期設計'
        title = '原本確認・ニーズ探索の準備'
        scope = '登録資料の確認済み事実と不足資料を整理し、ニーズを探索する質問票・検証手順・承認待ち活動を設計する。実施していない面談結果を捏造しない。'
        headings = ['原本の確認結果', '不足資料', 'ニーズ探索の質問票', '検証手順と承認待ち']
    else:
        criterion = '登録原本・入力条件の確認と検証手順の初期設計'
        title = '原本確認・入力条件と検証の準備'
        scope = '登録資料の確認済み事実、対象範囲、必要な入力条件、不足資料、検証手順を整理する。未実施の処理を完了と記載しない。'
        headings = ['原本の確認結果', '不足資料', '入力条件と対象範囲', '検証手順と承認待ち']
        if financial:
            scope += '金額単位、税込・税抜、対象期間、集計キー、配賦基準は原本の記載を確認し、不明なら未確認とする。税込の明記がある金額を再度税込変換しない。売上と原価の一致を一般的な検証条件にせず、原本と各集計の整合性を検証する。'
    preparation = compile_task(0, criterion, {
        'title': title, 'scope': scope, 'headings': headings, 'depends_on': [],
    }, [ref.removeprefix('context:') for ref in contract_of(tasks[0])['source_refs']])
    prep_contract = contract_of(preparation)
    prep_contract['criterion_ids'] = []
    prep_contract['document_mode'] = 'planning'
    prep_contract.update({
        'role':'preparation','responsible_role':'system','estimated_days':1,
        'completion_evidence':'validated_artifact','failure_policy':'stop_and_report',
        'evidence_required':True,'artifact_category':'preparation_record','public_purpose_code':'input_readiness',
        'exit_checks':['registered_sources_classified','missing_inputs_listed','validation_method_defined'],
    })
    preparation['acceptance_criteria'] = json.dumps(prep_contract, ensure_ascii=False)
    priority = {
        'multi_channel_workflow_design': 10, 'execution_monitoring_evidence': 11,
        'manual_social_posting_guard': 12, 'external_approval_boundaries': 13,
        'evidence_based_completion': 14, 'failure_recovery_policy': 15,
        'market_and_customer_definition': 20, 'service_package_design': 21,
        'commercial_terms': 22, 'sales_assets': 23, 'sales_plan': 24,
        'prospect_prioritization': 25, 'outreach_content': 26,
        'pipeline_tracking': 27, 'delivery_process': 28,
        'campaign_execution_sequence': 30, 'approved_customer_engagement': 31,
        'execution_status': 40,
    }
    indexed_tasks = list(enumerate(tasks))
    indexed_tasks.sort(key=lambda pair: (
        0 if (contract_of(pair[1]) or {}).get('public_web_research', {}).get('required')
        else priority.get((contract_of(pair[1]) or {}).get('public_purpose_code'), 29),
        pair[0],
    ))
    tasks = [preparation, *(task for _, task in indexed_tasks)]
    web_tasks = [
        current for current in tasks[1:]
        if (contract_of(current) or {}).get("public_web_research", {}).get("required")
    ]
    keys_by_purpose = {}
    for current in tasks[1:]:
        purpose = (contract_of(current) or {}).get('public_purpose_code')
        keys_by_purpose.setdefault(purpose, []).append(current['task_key'])
    semantic_parents = {
        'sales_assets': ['market_and_customer_definition', 'service_package_design', 'commercial_terms'],
        'sales_plan': ['market_and_customer_definition', 'service_package_design', 'commercial_terms', 'sales_assets'],
        'prospect_prioritization': ['market_and_customer_definition', 'sales_plan'],
        'outreach_content': ['prospect_prioritization', 'sales_assets'],
        'pipeline_tracking': ['prospect_prioritization', 'outreach_content'],
        'delivery_process': ['service_package_design', 'commercial_terms'],
        'campaign_execution_sequence': [
            'failure_recovery_policy', 'external_approval_boundaries',
            'manual_social_posting_guard', 'evidence_based_completion',
            'market_and_customer_definition', 'service_package_design',
            'commercial_terms', 'sales_assets', 'outreach_content',
        ],
        'approved_customer_engagement': [
            'external_approval_boundaries', 'prospect_prioritization',
            'outreach_content', 'pipeline_tracking', 'campaign_execution_sequence',
        ],
        'execution_status': [
            'campaign_execution_sequence', 'approved_customer_engagement',
            'pipeline_tracking',
        ],
    }
    for current in tasks[1:]:
        purpose = (contract_of(current) or {}).get('public_purpose_code')
        required = [key for p in semantic_parents.get(purpose, []) for key in keys_by_purpose.get(p, [])]
        if required:
            current['depends_on'] = list(dict.fromkeys(required))
    outputs_by_key = {
        current["task_key"]: [
            output["path"] for output in (contract_of(current) or {}).get("outputs", [])
        ]
        for current in tasks
    }
    web_keys = [current["task_key"] for current in web_tasks]
    for current in tasks[1:]:
        current_contract = contract_of(current)
        if current in web_tasks:
            dependencies = ["SC00"]
        else:
            dependencies = list(dict.fromkeys([
                *current.get("depends_on", []),
                *web_keys,
            ]))
            dependencies = [value for value in dependencies if value != current["task_key"]]
            if not dependencies:
                dependencies = ["SC00"]
        current["depends_on"] = dependencies
        current_contract["inputs"] = [
            path for dependency in dependencies
            for path in outputs_by_key.get(dependency, [])
        ]
        current["acceptance_criteria"] = json.dumps(current_contract, ensure_ascii=False)
    limited_external_purposes = {'campaign_execution_sequence', 'approved_customer_engagement'}
    for position, current in enumerate(tasks):
        current_contract = contract_of(current)
        if (
            not current_contract.get('action_requirements')
            or current_contract.get('public_purpose_code') in limited_external_purposes
        ):
            continue
        parents = tasks[:position]
        current['depends_on'] = [parent['task_key'] for parent in parents]
        current_contract['inputs'] = [
            output['path']
            for parent in parents
            for output in contract_of(parent)['outputs']
        ]
        current['acceptance_criteria'] = json.dumps(current_contract, ensure_ascii=False)
    paths = [output['path'] for task in tasks for output in contract_of(task)['outputs']]
    source_refs = list(dict.fromkeys(
        ref for task in tasks for ref in (contract_of(task) or {}).get('source_refs', [])
    ))
    contract = {
        "schema": SCHEMA,
        "criterion_ids": [f"SC{i:02d}" for i in range(1, len(criteria) + 1)],
        "inputs": paths, "source_refs": source_refs, "execution_kind": "final_verification",
        "outputs": [{"path": "result/final_verification.md", "required_headings": ["達成条件別判定", "成果物検証", "未達条件と承認待ち"], "minimum_characters": 600}],
        "external_actions": "approval_required", "final_verification": True,
        "role": "final_verification", "responsible_role": "human_approver",
        "estimated_days": 1, "completion_evidence": "human_semantic_confirmation",
        "failure_policy": "stop_and_report", "human_confirmation_required": True,
        "semantic_review_required": True,
        "evidence_required": True, "artifact_category": "verification_report",
        "public_purpose_code": "final_goal_verification",
        "exit_checks": ["all_criteria_decided", "evidence_paths_verified", "unmet_and_pending_reported"],
    }
    tasks.append({
        "task_key": "final_verification", "depends_on": [task["task_key"] for task in tasks],
        "title": "最終検証・達成条件別の判定", "mode": "local",
        "description": "全先行成果物と実操作の証拠を確認し、各達成条件のPASS/FAIL、根拠パス、未達・承認待ちをresult/final_verification.mdへ保存する。資料作成と実処理を区別し、未実施の処理を合格にしない。",
        "acceptance_criteria": json.dumps(contract, ensure_ascii=False),
    })
    _uniquify_task_titles(tasks)
    return {"summary": "原本と入力条件の確認から始める達成条件別の構造化計画。成果物作成と実処理の達成を区別し、最終判定で未達・承認待ちを明示する。", "tasks": tasks}


def validate_plan(plan: dict, expected_ids: set[str], existing: set[str] | None = None, review_count: int = 0) -> dict:
    errors = []
    producers = {}
    ancestors = {}
    covered = set()
    tasks = plan.get("tasks", [])
    if not 2 <= len(tasks) <= 20:
        errors.append("task_count")
    for task in tasks:
        key = task["task_key"]
        contract = contract_of(task)
        if key in ancestors or contract is None:
            errors.append(f"{key}: missing contract or duplicate task")
            continue
        parents = set(task.get("depends_on", []))
        if not parents <= ancestors.keys():
            errors.append(f"{key}: unknown/forward dependency")
        ancestry = set(parents)
        for parent in parents:
            ancestry.update(ancestors.get(parent, set()))
        ancestors[key] = ancestry
        covered.update(contract["criterion_ids"])
        for path in contract["inputs"]:
            if path not in (existing or set()) and producers.get(path) not in ancestry:
                errors.append(f"{key}: missing dependency for {path}")
        for output in contract["outputs"]:
            path = output["path"]
            pattern = r"result/(?:plan_[1-9][0-9]*/)?[a-z0-9_]+\.(?:md|csv)"
            if contract.get('execution_kind', '').startswith('vehicle_'):
                pattern = rf"result/vehicle/v{contract.get('workflow_version', 0)}/(?:sources|normalization|calculation|verification|profit)\.(?:json|xlsx)"
            if not re.fullmatch(pattern, path) or path in producers:
                errors.append(f"{key}: invalid/duplicate output {path}")
            if path.endswith('.md') and (not output.get('required_headings') or output.get('minimum_characters', 0) < 200):
                errors.append(f"{key}: incomplete output contract")
            if path.endswith('.csv') and (not output.get('required_columns') or output.get('minimum_rows', 0) < 1):
                errors.append(f"{key}: incomplete table contract")
            producers[path] = key
    if covered != expected_ids:
        errors.append("criterion coverage mismatch")
    if not tasks or not (contract_of(tasks[-1]) or {}).get("final_verification"):
        errors.append("final verification missing")
    elif set(ancestors) - {tasks[-1]["task_key"]} != ancestors[tasks[-1]["task_key"]]:
        errors.append("final verification must depend on every task")
    structure = inspect_plan_structure(plan, expected_ids, review_count=review_count)
    errors.extend(structure["issues"])
    return {"passed": not errors, "issues": errors, "criterion_ids": sorted(covered), "artifact_count": len(producers), "assessment": "structural_only"}


def verify_outputs(task: dict, resolve) -> list[str]:
    contract = contract_of(task)
    if contract is None:
        return []
    if contract.get('execution_kind', '').startswith('vehicle_'):
        from app.vehicle_workflow import verify_task_outputs
        return verify_task_outputs(task, resolve)
    failures = []
    for output in contract["outputs"]:
        try:
            content = resolve(output["path"]).read_text(encoding="utf-8-sig")
            if output['path'].endswith('.csv'):
                rows = list(csv.reader(io.StringIO(content)))
                if not rows or rows[0] != output['required_columns']:
                    failures.append(f"{output['path']}: 必須列の不一致")
                if len(rows) - 1 < output['minimum_rows']:
                    failures.append(f"{output['path']}: 最低行数未達")
                continue
            headings = {re.sub(r"\s+#+\s*$", "", value).strip() for value in re.findall(r"(?m)^#{1,6}\s+(.+)$", content)}
            normalized_headings = {
                re.sub(r"^[0-9０-９]+\s*[.．、)）:]\s*", "", value).strip()
                for value in headings
            }
            if len(content.strip()) < output["minimum_characters"]:
                failures.append(f"{output['path']}: minimum_characters未達")
            for heading in output["required_headings"]:
                normalized_required = re.sub(
                    r"^[0-9０-９]+\s*[.．、)）:]\s*", "", heading
                ).strip()
                if heading not in headings and normalized_required not in normalized_headings:
                    failures.append(f"{output['path']}: 必須見出し不足 {heading}")
        except (OSError, ValueError) as exc:
            failures.append(f"{output['path']}: 読み取り失敗 {exc}")
    return failures


def validate_rebuild_generic_candidate(
    plan: dict,
    goal_contract: dict | None = None,
    mission: dict | None = None,
) -> dict:
    """
    rebuild_generic の保存前厳格バリデーション（6項目検査）:
    1. 目標要求（達成条件）の保持率100%（既存の達成条件が候補計画から漏れていない）
    2. 計画被覆（coverage）がPASS（app/plan_coverage.pyの既存の被覆判定を利用）
    3. task keyおよび出力パスの重複なし
    4. 依存関係に循環なし（閉路なし、前方参照なし）
    5. 成果物契約が検証可能な形になっている（schema, path, headings/chars or columns/rows）
    6. 外部アクション（公開・送信等）を含むタスクに承認点（approval_required 等）がある
    1つでも不合格なら例外を発生させる。
    """
    if not isinstance(plan, dict) or not isinstance(plan.get("tasks"), list):
        raise ValueError("INVALID_PLAN_STRUCTURE: tasks リストが必要です")
    tasks = plan["tasks"]
    if not tasks:
        raise ValueError("INVALID_PLAN_STRUCTURE: タスクが空です")

    # 1. 目標要求保持率 100%
    if goal_contract and goal_contract.get("criteria"):
        expected_cids = {c["criterion_id"] for c in goal_contract.get("criteria", []) if c.get("criterion_id")}
    elif mission:
        extracted = extract_criteria(mission.get("goal", ""), mission.get("success_criteria", ""))
        expected_cids = {f"SC{i:02d}" for i in range(1, len(extracted) + 1)}
    else:
        expected_cids = set()

    covered_cids = set()
    for task in tasks:
        contract = contract_of(task)
        # A final verifier can assess all criteria, but it cannot replace the
        # execution task that actually produces evidence for each criterion.
        if contract and not contract.get("final_verification") and contract.get("criterion_ids"):
            covered_cids.update(contract["criterion_ids"])

    if expected_cids:
        missing = expected_cids - covered_cids
        if missing:
            raise ValueError(f"RETENTION_INCOMPLETE: 目標要求の保持率が100%ではありません (欠落: {sorted(missing)})")

    # 2. 計画被覆 (coverage) PASS
    if goal_contract:
        from app.plan_coverage import build as build_coverage
        dummy_mission = {
            "tasks": tasks,
            "plan_version": (mission.get("plan_version", 0) if mission else 0),
        }
        cov = build_coverage(dummy_mission, goal_contract)
        if not cov.get("passed"):
            issues_str = "; ".join(cov.get("issues", [])) or "被覆判定不合格"
            raise ValueError(f"COVERAGE_INCOMPLETE: 計画被覆が不合格です ({issues_str})")

    # 3. task key および 出力パスの重複なし
    seen_keys = set()
    seen_paths = set()
    for task in tasks:
        key = str(task.get("task_key") or "").strip()
        if not key:
            raise ValueError("EMPTY_TASK_KEY: task_key が空のタスクがあります")
        if key in seen_keys:
            raise ValueError(f"DUPLICATE_KEY: task_key '{key}' が重複しています")
        seen_keys.add(key)

        contract = contract_of(task)
        if not contract:
            raise ValueError(f"MISSING_CONTRACT: タスク '{key}' に契約がありません")
        outputs = contract.get("outputs") or []
        for out in outputs:
            path = str(out.get("path") or "").strip()
            if not path:
                raise ValueError(f"EMPTY_OUTPUT_PATH: タスク '{key}' に空の出力パスがあります")
            if path in seen_paths:
                raise ValueError(f"DUPLICATE_OUTPUT_PATH: 出力パス '{path}' が重複しています")
            seen_paths.add(path)

    # 4. 依存関係に循環なし（閉路なし、前方参照・未定義参照なし）
    task_keys_set = set(seen_keys)
    adj = {task["task_key"]: list(task.get("depends_on") or []) for task in tasks}
    for k, deps in adj.items():
        for dep in deps:
            if dep not in task_keys_set:
                raise ValueError(f"UNKNOWN_DEPENDENCY: タスク '{k}' が未定義のタスク '{dep}' に依存しています")
            if dep == k:
                raise ValueError(f"CIRCULAR_DEPENDENCY: タスク '{k}' が自身に依存しています")

    visited = {}  # 0: unvisited, 1: visiting, 2: visited
    def dfs(node):
        visited[node] = 1
        for neighbor in adj.get(node, []):
            if visited.get(neighbor) == 1:
                return True
            if visited.get(neighbor) != 2:
                if dfs(neighbor):
                    return True
        visited[node] = 2
        return False

    for k in task_keys_set:
        if visited.get(k) != 2:
            if dfs(k):
                raise ValueError("CIRCULAR_DEPENDENCY: タスク依存関係に循環があります")

    # 5. 成果物契約が検証可能な形になっている
    for task in tasks:
        key = task["task_key"]
        contract = contract_of(task)
        if not contract or contract.get("schema") != SCHEMA:
            raise ValueError(f"INVALID_ARTIFACT_CONTRACT: タスク '{key}' のスキーマが不正です")
        outputs = contract.get("outputs")
        if not isinstance(outputs, list) or len(outputs) == 0:
            raise ValueError(f"INVALID_ARTIFACT_CONTRACT: タスク '{key}' に出力契約がありません")
        for output in outputs:
            path = str(output.get("path") or "")
            pattern = r"result/(?:plan_[1-9][0-9]*/)?[a-z0-9_]+\.(?:md|csv)"
            if not re.fullmatch(pattern, path):
                raise ValueError(f"INVALID_ARTIFACT_CONTRACT: タスク '{key}' の出力パス '{path}' が規約外です")
            if path.endswith(".md"):
                headings = output.get("required_headings")
                min_chars = output.get("minimum_characters", 0)
                if not isinstance(headings, list) or len(headings) < 2 or any(not isinstance(h, str) or not h.strip() for h in headings):
                    raise ValueError(f"INVALID_ARTIFACT_CONTRACT: タスク '{key}' のMarkdown必須見出しが不足しています")
                if not isinstance(min_chars, int) or min_chars < 200:
                    raise ValueError(f"INVALID_ARTIFACT_CONTRACT: タスク '{key}' の最小文字数が不正です (>=200必須)")
            elif path.endswith(".csv"):
                cols = output.get("required_columns")
                min_rows = output.get("minimum_rows", 0)
                if not isinstance(cols, list) or len(cols) < 1 or any(not isinstance(c, str) or not c.strip() for c in cols):
                    raise ValueError(f"INVALID_ARTIFACT_CONTRACT: タスク '{key}' のCSV必須列が不足しています")
                if not isinstance(min_rows, int) or min_rows < 1:
                    raise ValueError(f"INVALID_ARTIFACT_CONTRACT: タスク '{key}' の最小行数が不正です (>=1必須)")

    # 6. 外部アクションを含むタスクに承認点がある
    for task in tasks:
        key = task["task_key"]
        contract = contract_of(task)
        criterion_text = str(contract.get("criterion") or task.get("description") or task.get("title") or "")
        has_external_action = (
            requires_google_site_publication(criterion_text)
            or bool(contract.get("action_requirements"))
            or criterion_requires_external_action(criterion_text)
        )
        if has_external_action:
            has_approval = (
                contract.get("external_actions") == "approval_required"
                or any(bool(r.get("evidence_required")) for r in (contract.get("action_requirements") or []) if isinstance(r, dict))
            )
            if not has_approval:
                raise ValueError(f"MISSING_APPROVAL_GATE: 外部アクションを含むタスク '{key}' に人間の承認点がありません")

    return {"passed": True, "task_count": len(tasks), "artifact_count": len(seen_paths)}


def build_rebuild_generic_plan(
    mission: dict,
    snapshot: dict,
    actions: list[dict],
    issues: list[dict],
    goal_contract: dict | None = None,
) -> dict:
    """
    GoalContract、既存成果物、未達criterion、採用済み指摘を入力にして、
    既存のplanner構造で汎用再構成計画候補を作る。
    """
    mission_criteria = extract_criteria(mission.get('goal', ''), mission.get('success_criteria', ''))
    contract_criteria = ([c['statement'] for c in goal_contract['criteria'] if c.get('statement')]
                         if goal_contract and goal_contract.get('criteria') else [])
    criteria = mission_criteria if len(mission_criteria) >= len(contract_criteria) else contract_criteria
    if not criteria:
        criteria = [mission.get("goal") or "プロジェクト目標の達成"]

    source_ids = [x["id"] for x in snapshot.get("sources", []) if isinstance(x, dict) and x.get("id")]
    existing_tasks = {t.get("task_key"): t for t in snapshot.get("tasks", []) if t.get("task_key")}

    # 採用された指摘の要約・反映差分を整理
    generic_actions = [a for a in actions if a.get("disposition") in {"rebuild_generic", "amend"}]
    action_changes_by_target = {}
    for a in generic_actions:
        target = a.get("target") or ""
        change = a.get("change") or ""
        if target and change:
            action_changes_by_target.setdefault(target, []).append(change.strip())

    tasks = []
    for index, criterion in enumerate(criteria, 1):
        key = f"SC{index:02d}"
        old_task = existing_tasks.get(key)
        target_changes = action_changes_by_target.get(key, []) + action_changes_by_target.get("execution_pipeline", [])

        if old_task and (contract_of(old_task) or {}).get('criterion') == criterion:
            old_contract = contract_of(old_task) or {}
            title = old_task.get("title") or f"達成条件 SC{index:02d} の実行"
            scope = f"達成条件 {key}: {criterion} を満たす成果物を作成・検証する。"
            if target_changes:
                scope += " " + " ".join(target_changes)
            headings = []
            for out in old_contract.get("outputs", []):
                if out.get("required_headings"):
                    headings = [h for h in out["required_headings"] if h not in {"根拠と未確認事項", "実施状態と次の行動"}]
                    break
            if not headings or len(headings) < 2:
                headings = ["現状と前提確認", "具体的設計・作成手順"]
            proposal = {
                "title": title,
                "scope": scope[:1500],
                "headings": headings[:6],
                "depends_on": [d for d in old_task.get("depends_on", []) if d != key and d.startswith("SC")],
            }
        else:
            scope = f"達成条件 {key}: {criterion} を満たす成果物を作成・検証する。"
            if target_changes:
                scope += " " + " ".join(target_changes)
            headings = ["現状と前提確認", "具体的設計・作成手順"]
            proposal = {
                "title": f"達成条件 SC{index:02d} の実行設計",
                "scope": scope[:1500],
                "headings": headings,
                "depends_on": [],
            }
        task = compile_task(index, criterion, proposal, source_ids)
        tasks.append(task)

    compiled = compile_plan(criteria, tasks, goal=mission.get("goal", ""))
    return compiled
