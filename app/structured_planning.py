"""Small planning responses compiled into persistent, executable contracts."""
from __future__ import annotations

import json
import re
import csv
import io
from copy import deepcopy

SCHEMA = "local-cowork-plan/v1"
MAX_CRITERIA = 18  # Reserve one preparation task and one final verification task.


def extract_criteria(goal: str, success: str) -> list[str]:
    match = re.search(r"(?m)^#{1,4}\s*達成条件\s*$", goal)
    section = goal[match.end():] if match else ""
    if section:
        section = re.split(r"(?m)^#{1,4}\s", section, maxsplit=1)[0]
    lines = re.findall(r"(?m)^\s*\d+[.）)]\s*(.+)$", section or success)
    criteria = list(dict.fromkeys(line.strip() for line in lines if line.strip()))
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
    }
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
    if requires_google_site_publication(criterion):
        contract['action_requirements'] = [{
            'kind': 'google_site_publication',
            'minimum_executed': 1,
            'evidence_required': True,
        }]
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
    preparation['acceptance_criteria'] = json.dumps(prep_contract, ensure_ascii=False)
    web_tasks = [
        current for current in tasks
        if (contract_of(current) or {}).get("public_web_research", {}).get("required")
    ]
    regular_tasks = [current for current in tasks if current not in web_tasks]
    tasks = [preparation, *web_tasks, *regular_tasks]
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
    for position, current in enumerate(tasks):
        current_contract = contract_of(current)
        if not requires_google_site_publication(str(current_contract.get('criterion') or '')):
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
    contract = {
        "schema": SCHEMA, "criterion_ids": [], "inputs": paths, "source_refs": [],
        "outputs": [{"path": "result/final_verification.md", "required_headings": ["達成条件別判定", "成果物検証", "未達条件と承認待ち"], "minimum_characters": 600}],
        "external_actions": "approval_required", "final_verification": True,
    }
    if any(re.search(
        r"(?:実際の営業活動|顧客(?:へ|に).{0,20}(?:送信|連絡|提案)|商談.{0,10}実施|"
        r"PoC.{0,10}実施|契約締結|仮説検証.{0,20}実行)", criterion, re.I,
    ) for criterion in criteria):
        contract["action_requirements"] = [{
            "kind": "approved_external_action",
            "minimum_executed": 1,
            "evidence_required": True,
        }]
    tasks.append({
        "task_key": "final_verification", "depends_on": [task["task_key"] for task in tasks],
        "title": "最終検証・達成条件別の判定", "mode": "local",
        "description": "全先行成果物と実操作の証拠を確認し、各達成条件のPASS/FAIL、根拠パス、未達・承認待ちをresult/final_verification.mdへ保存する。資料作成と実処理を区別し、未実施の処理を合格にしない。",
        "acceptance_criteria": json.dumps(contract, ensure_ascii=False),
    })
    return {"summary": "原本と入力条件の確認から始める達成条件別の構造化計画。成果物作成と実処理の達成を区別し、最終判定で未達・承認待ちを明示する。", "tasks": tasks}


def validate_plan(plan: dict, expected_ids: set[str], existing: set[str] | None = None) -> dict:
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
