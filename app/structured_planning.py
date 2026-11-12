"""Small planning responses compiled into persistent, executable contracts."""
from __future__ import annotations

import hashlib
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
    if not isinstance(response, str):
        raise ValueError("JSON object required")
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", response):
        try:
            value, _ = decoder.raw_decode(response, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("JSON object required")


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


def _is_provisional_menu_price(text: str) -> bool:
    has_menu = bool(re.search(r'メニュー|飲食|料理|ドリンク', text))
    has_price = bool(re.search(r'価格|値段|料金|金額', text))
    has_provisional = bool(re.search(r'仮|想定|例示|架空|サンプル|目安|仮定|未定|要確認', text))
    return bool(has_menu and has_price and has_provisional)


def _is_menu_document(text: str) -> bool:
    return bool(re.search(r'メニュー|商品説明|飲食|料理|ドリンク', text))


def _classify_purpose(criterion: str, combined_text: str) -> str:
    provisional_menu_price = _is_provisional_menu_price(combined_text)
    menu_context = _is_menu_document(combined_text)
    if provisional_menu_price or menu_context:
        return 'menu_document'
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
        (r'提供範囲|除外事項|料金体系|商業条件', 'commercial_terms'),
        (r'価格.{0,20}(?:決定|改定|承認|契約|見積)', 'commercial_terms'),
        (r'紹介資料|提案書|ヒアリング|見積', 'sales_assets'),
        (r'販売計画|営業チャネル|KPI|収支', 'sales_plan'),
        (r'営業文面|商談台本|フォロー', 'outreach_content'),
        (r'見込み客|優先順位', 'prospect_prioritization'),
        (r'案件管理', 'pipeline_tracking'),
        (r'受注後|標準手順|導入', 'delivery_process'),
        (r'実行報告|状態報告|実際の営業活動.{0,40}報告', 'execution_status'),
    ]:
        if re.search(pattern, criterion, re.I):
            # 「価格」の語だけでは商業条件策定にしない。文脈込みで判定する。
            if code == 'commercial_terms' and re.search(r'価格|値段|料金|金額', criterion):
                if provisional_menu_price:
                    return 'menu_document'
                if not re.search(r'提供範囲|除外事項|料金体系|商業条件|決定|改定|承認|契約|見積', criterion):
                    continue
            return code
    if menu_context:
        return 'menu_document'
    return 'criterion_delivery'


def contract_of(task: dict) -> dict | None:
    try:
        value = json.loads(task.get("acceptance_criteria", ""))
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get("schema") == SCHEMA else None


# P0-1: 達成条件文中の必須成果物パス( result/ 配下の .md/.csv )の決定的抽出。
# 外部通信・LLM呼出なし。順序安定(出現順・重複除去)。
P0_1_VERSION = "p0-1-required-path-v1"
REQUIRED_OUTPUT_PATH_MISMATCH = "required_output_path_mismatch"
REQUIRED_OUTPUT_PATH_UNKNOWN = "required_output_path_unknown"

_P01_VALID_FIND_RE = re.compile(r"result/[A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-]+)*\.(?:md|csv)")
_P01_VALID_FULL_RE = re.compile(r"result/[A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-]+)*\.(?:md|csv)")
_P01_TILDE_RE = re.compile(r"result/[^\s]*~[^\s]*\.(?:md|csv)")
_P01_COLON_PATH_RE = re.compile(r"result/[^:\s]*:[^:\s]*\.(?:md|csv)")
_P01_RESULT_COLON_RE = re.compile(r"result\s*:[^\s]*\.(?:md|csv)")
_P01_WS_PATH_RE = re.compile(r"result/[A-Za-z0-9_\-]+\s+[A-Za-z0-9_\-]+\.(?:md|csv)")
_P01_DOTDOT_RE = re.compile(r"\.\.[\\/][^\s]*\.(?:md|csv)")
_P01_ABS_RE = re.compile(r"(?<![A-Za-z0-9_/:\.\-])/[A-Za-z0-9_\-]+\.(?:md|csv)")
_P01_URL_MD_RE = re.compile(r"https?://[^\s]*\.(?:md|csv)")
_P01_URL_RESULT_RE = re.compile(r"https?://[^\s]*result/")
_P01_WINABS_RE = re.compile(r"[A-Za-z]:[\\/][^\s]*\.(?:md|csv)")
_P01_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def extract_required_output_paths(criterion_text: str) -> dict:
    """達成条件文から明示された必須成果物パスを取り出す純粋関数。

    対象は `result/` 配下の相対パスで拡張子 `.md`/`.csv` のみ。厳密な正規表現で
    判定し、`..`・絶対パス・バックスラッシュ・コロン・空白・URL・チルダ・
    制御文字・連続スラッシュは不正として `unknown` にする。否定・例示・禁止の
    文脈、矛盾する指定、6件超の指定、不正形式の混在も `unknown` (推測で通さない)。
    明示が無ければ `none`。
    """
    text = str(criterion_text or "")
    if not text.strip():
        return {"status": "none", "paths": [], "reasons": ["no_explicit_path"]}
    candidates: list[str] = []
    invalid_preceding = False
    for match in _P01_VALID_FIND_RE.finditer(text):
        start = match.start()
        # Markdown inline code is an explicit path notation, not invalid.
        # Keep preceding / or . invalid (possible // or extension breakage).
        if start > 0 and text[start - 1] in "/.":
            invalid_preceding = True
            continue
        candidates.append(match.group(0))
    ordered: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        if path not in seen:
            seen.add(path)
            ordered.append(path)
    invalid = bool(invalid_preceding)
    if not invalid:
        if "result\\" in text:
            invalid = True
        elif "result//" in text:
            invalid = True
        elif _P01_TILDE_RE.search(text):
            invalid = True
        elif _P01_COLON_PATH_RE.search(text):
            invalid = True
        elif _P01_RESULT_COLON_RE.search(text):
            invalid = True
        elif _P01_WS_PATH_RE.search(text):
            invalid = True
        elif _P01_DOTDOT_RE.search(text):
            invalid = True
        elif _P01_ABS_RE.search(text):
            invalid = True
        elif _P01_URL_MD_RE.search(text):
            invalid = True
        elif _P01_URL_RESULT_RE.search(text):
            invalid = True
        elif _P01_WINABS_RE.search(text):
            invalid = True
        elif _P01_CTRL_RE.search(text):
            invalid = True
        else:
            for token_match in re.finditer(r"result/[^\s\"'、。)）\]]*", text):
                token = token_match.group(0).rstrip("。、.,;:：)）」』】\"'`")
                if ".md" in token or ".csv" in token:
                    if not _P01_VALID_FULL_RE.fullmatch(token):
                        invalid = True
                        break
    if invalid:
        return {"status": "unknown", "paths": [], "reasons": ["invalid_path_format"]}
    if not ordered:
        return {"status": "none", "paths": [], "reasons": ["no_explicit_path"]}
    if len(ordered) > 5:
        return {"status": "unknown", "paths": [], "reasons": ["too_many_paths"]}
    has_example = any(key in text for key in ("例:", "例：", "たとえば", "例えば", "e.g.", "例として"))
    if has_example:
        return {"status": "unknown", "paths": [], "reasons": ["example_context"]}
    if len(ordered) >= 2 and any(key in text for key in ("ではなく", "のではなく", "いずれか", "どちらか")):
        return {"status": "unknown", "paths": [], "reasons": ["contradictory_paths"]}
    has_negation = any(key in text for key in ("保存しない", "作成しない", "出力しない", "生成しない", "禁止", "しないで", "しないこと", "ではなく"))
    if has_negation:
        return {"status": "unknown", "paths": [], "reasons": ["negation_context"]}
    if ("以外" in text) or ("除く" in text):
        return {"status": "unknown", "paths": [], "reasons": ["exclusion_context"]}
    return {"status": "explicit", "paths": list(ordered), "reasons": ["explicit"]}


def compile_task(index: int, criterion: str, proposal: dict, source_ids: list[str], registered_text: str | None = None) -> dict:
    key = f"SC{index:02d}"
    title = proposal.get("title")
    scope = proposal.get("scope")
    headings = proposal.get("headings")
    if not isinstance(title, str) or not title.strip() or not isinstance(scope, str) or not scope.strip():
        raise ValueError("title and scope are required strings")
    # P0-1: 明示の必須成果物パスがあるときは出力契約を完全一致させる。
    # unknown は推測で通さない。準備タスク(index 0)は対象外としないが、
    # 通常は明示を持たないため none として従来動作になる。
    _p01_req = extract_required_output_paths(criterion)
    if _p01_req.get("status") == "unknown":
        raise ValueError(f"{REQUIRED_OUTPUT_PATH_UNKNOWN}: criterion has undetermined required output paths")
    _p01_required: list[str] | None = None
    if _p01_req.get("status") == "explicit":
        _p01_required = list(_p01_req.get("paths") or [])
        _p01_norm = normalize_p0a_artifact_requirement(criterion)
        if not _p01_norm.get("consistent"):
            raise ValueError(f"{REQUIRED_OUTPUT_PATH_MISMATCH}: artifact requirement is contradictory")
        if _p01_norm.get("item_counts") or _p01_norm.get("explicit_total") is not None:
            _p01_need = _p0a_required_or_raise(_p01_norm, where="compile_task:required_output")
            if len(_p01_required) != _p01_need:
                raise ValueError(
                    f"{REQUIRED_OUTPUT_PATH_MISMATCH}: required paths {len(_p01_required)} "
                    f"conflict with artifact count {_p01_need}"
                )
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
    base_headings = [item.strip() for item in headings]
    requested_outputs = proposal.get("outputs")
    markdown_count = None
    norm_slots = normalize_p0a_artifact_requirement(criterion)
    # P0-0: 明示の件数表現があるときだけ正規化件数を優先する。件数表現が無い
    # 達成条件(例:「案内文とチェックリスト」だけ)では従来どおり outputs の
    # 自由度を保ち、既存の2成果物テストを壊さない。
    # 欠落・None・不正値のときは1に倒さず判定不能(ValueError)にする。
    if (norm_slots.get("consistent")
            and (norm_slots.get("item_counts") or norm_slots.get("explicit_total") is not None)):
        norm_required = _p0a_required_or_raise(norm_slots, where="compile_task")
        if 1 <= norm_required <= 4:
            markdown_count = norm_required
    if markdown_count is None and re.search(r"(?:合計|計)(?:2|二)点.{0,20}Markdown|Markdown.{0,20}(?:合計|計)(?:2|二)点|Markdown\s*(?:2|二)点", criterion, re.I):
        markdown_count = 2
    elif markdown_count is None and re.search(r"(?:1|一)(?:点|つ|ファイル|文書).{0,20}Markdown|Markdown.{0,20}(?:1|一)(?:点|つ|ファイル|文書)", criterion, re.I):
        markdown_count = 1
    norm_check = normalize_p0a_artifact_requirement(criterion)
    # 不変条件: consistent=True のとき required は必ず整数。破れたら判定不能として拒否する。
    # 矛盾(consistent=False)のときはここでは強制せず、検査側で保存拒否する。
    if norm_check.get("consistent"):
        norm_expected = _p0a_required_or_raise(norm_check, where="compile_task")
    else:
        norm_expected = None
    if (markdown_count is not None and isinstance(requested_outputs, list) and requested_outputs
            and norm_check.get("consistent")
            and norm_expected == markdown_count
            and len(requested_outputs) != markdown_count):
        raise ValueError(f"goal requires {markdown_count} Markdown output(s)")
    if isinstance(requested_outputs, list) and requested_outputs:
        if len(requested_outputs) > 4:
            raise ValueError("outputs must contain at most 4 contracts")
        output_specs: list[dict] = []
        seen_paths: set[str] = set()
        for raw in requested_outputs:
            if not isinstance(raw, dict):
                raise ValueError("outputs must be an array of output contracts")
            output_path = str(raw.get("path") or "").strip()
            if not output_path:
                raw_kind = str(raw.get("kind") or raw.get("type") or "document").strip().lower()
                if raw_kind in {"checklist", "list"}:
                    output_path = f"result/sc{index:02d}_checklist.md"
                elif raw_kind in {"faq", "qa", "faq_document"}:
                    output_path = f"result/sc{index:02d}_faq.md"
                elif raw_kind in {"procedure", "manual", "guide", "procedure_document"}:
                    output_path = f"result/sc{index:02d}_procedure.md"
                else:
                    output_path = path if path not in seen_paths else f"result/sc{index:02d}_{len(output_specs) + 1:02d}.md"
            raw_kind = str(raw.get("kind") or raw.get("type") or "document").strip().lower()
            if _p01_required is not None:
                if not re.fullmatch(r"result/[A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-]+)*\.(?:md|csv)", output_path):
                    raise ValueError("outputs[].path must be an explicit required output path")
                if output_path.lower().endswith(".csv"):
                    raw_cols = raw.get("required_columns")
                    raw_rows = raw.get("minimum_rows", 1)
                    if (not isinstance(raw_cols, list) or not raw_cols
                            or any(not isinstance(col, str) or not col.strip() for col in raw_cols)):
                        raise ValueError(f"{REQUIRED_OUTPUT_PATH_MISMATCH}: csv path requires table contract")
                    if isinstance(raw_rows, bool) or not isinstance(raw_rows, int) or raw_rows < 1:
                        raise ValueError(f"{REQUIRED_OUTPUT_PATH_MISMATCH}: csv path requires table contract")
                    if output_path in seen_paths:
                        raise ValueError("outputs[].path must be unique within one task")
                    seen_paths.add(output_path)
                    output_specs.append({
                        "path": output_path,
                        "required_columns": [str(col).strip() for col in raw_cols],
                        "minimum_rows": int(raw_rows),
                        "artifact_category": str(raw.get("artifact_category") or "table").strip().lower() or "table",
                    })
                    continue
                category = str(raw.get("artifact_category") or "document").strip().lower() or "document"
            else:
                if raw_kind in {"checklist", "list"}:
                    suggested = f"result/sc{index:02d}_checklist.md"
                    category = "checklist"
                elif raw_kind in {"faq", "qa", "faq_document"}:
                    suggested = f"result/sc{index:02d}_faq.md"
                    category = "faq_document"
                elif raw_kind in {"procedure", "manual", "guide", "procedure_document"}:
                    suggested = f"result/sc{index:02d}_procedure.md"
                    category = "procedure_document"
                else:
                    suggested = path if path not in seen_paths else f"result/sc{index:02d}_{len(output_specs) + 1:02d}.md"
                    category = str(raw.get("artifact_category") or "document").strip().lower() or "document"
                if not output_path:
                    output_path = suggested
                if not re.fullmatch(r"result/sc\d{2}(?:_[a-z0-9_]+)?\.md", output_path):
                    raise ValueError("outputs[].path must be result/scNN[_suffix].md")
            raw_headings = raw.get("required_headings", base_headings)
            if not isinstance(raw_headings, list) or not 2 <= len(raw_headings) <= 8 or any(
                not isinstance(item, str) or not item.strip() or len(item) > 100 for item in raw_headings
            ):
                raise ValueError("outputs[].required_headings must contain 2-8 nonempty strings")
            if output_path in seen_paths:
                raise ValueError("outputs[].path must be unique within one task")
            seen_paths.add(output_path)
            output_specs.append({
                "path": output_path,
                "required_headings": list(dict.fromkeys(
                    [item.strip() for item in raw_headings]
                    + ["根拠と未確認事項", "実施状態と次の行動"]
                )),
                "minimum_characters": 600,
                "artifact_category": category,
            })
        if _p01_required is not None:
            if [spec["path"] for spec in output_specs] != list(_p01_required):
                raise ValueError(f"{REQUIRED_OUTPUT_PATH_MISMATCH}: outputs must exactly match required paths")
            _p01_has_csv = any(str(spec.get("path") or "").lower().endswith(".csv") for spec in output_specs)
            if _p01_has_csv and not any(
                    isinstance(spec.get("required_columns"), list) and spec.get("required_columns")
                    for spec in output_specs):
                raise ValueError(f"{REQUIRED_OUTPUT_PATH_MISMATCH}: csv path requires table contract")
        outputs = output_specs
        if len(outputs) > 1:
            categories = {str(output.get("artifact_category") or "") for output in outputs}
            if "チェックリスト" in criterion and "checklist" not in categories:
                raise ValueError("goal requires a separate checklist output")
            if "手順書" in criterion and "procedure_document" not in categories:
                raise ValueError("goal requires a separate procedure output")
            for output in outputs:
                if output.get("artifact_category") != "document":
                    continue
                headings_text = " ".join(output.get("required_headings") or [])
                if "checklist" in categories and "チェックリスト" in headings_text:
                    raise ValueError("document output must not duplicate checklist headings")
                if "procedure_document" in categories and "手順" in headings_text:
                    raise ValueError("document output must not duplicate procedure headings")
        primary_path = outputs[0]["path"]
    else:
        if _p01_required is not None:
            if len(_p01_required) != 1:
                raise ValueError(f"{REQUIRED_OUTPUT_PATH_MISMATCH}: outputs must exactly match required paths")
            req_path = _p01_required[0]
            if req_path.lower().endswith(".csv"):
                raise ValueError(f"{REQUIRED_OUTPUT_PATH_MISMATCH}: csv path requires table contract")
            headings = list(dict.fromkeys(base_headings + ["根拠と未確認事項", "実施状態と次の行動"]))
            outputs = [{"path": req_path, "required_headings": headings, "minimum_characters": 600,
                        "artifact_category": "document"}]
            primary_path = req_path
        else:
            headings = list(dict.fromkeys(base_headings + ["根拠と未確認事項", "実施状態と次の行動"]))
            outputs = [{"path": path, "required_headings": headings, "minimum_characters": 600,
                        "artifact_category": "document"}]
            if re.search(r"案内文", criterion) and re.search(r"チェックリスト", criterion):
                outputs.append({"path": f"result/sc{index:02d}_checklist.md",
                                "required_headings": ["準備", "受付", "終了後の確認", "根拠と未確認事項", "実施状態と次の行動"],
                                "minimum_characters": 600, "artifact_category": "checklist"})
            primary_path = path
    tail_checks = proposal.get("exit_checks")
    if tail_checks is not None and (
        not isinstance(tail_checks, list)
        or not 1 <= len(tail_checks) <= 8
        or any(not isinstance(item, str) or not item.strip() or len(item) > 120 for item in tail_checks)
    ):
        raise ValueError("exit_checks must contain 1-8 nonempty strings")
    review_notes = [item.strip() for item in tail_checks] if isinstance(tail_checks, list) else []
    explicit_checks: list[str] = []
    combined_text = f"{criterion}\n{title}\n{scope}"
    purpose_code = _classify_purpose(criterion, combined_text)
    needs_semantic_review = bool(re.search(
        r'整合|一貫|照合|検証|確認|比較|判断|評価|承認|手順書|マニュアル|FAQ|案内|メニュー|商品説明|仕様書|計画書',
        combined_text,
    ))
    semantic_override = proposal.get("semantic_review_required")
    if semantic_override is True:
        needs_semantic_review = True
    if re.search(r'案件管理|商談.{0,20}管理|(?:営業|顧客).{0,20}(?:管理表|CSV)', criterion, re.I):
        explicit_checks.append("tracker_columns_and_minimum_rows_verified")
    if re.search(r'FAQ|よくある質問|Q&A', combined_text):
        explicit_checks.append("faq_items_individually_verified")
    if re.search(r'手順|段階|ステップ|フロー', combined_text):
        explicit_checks.append("procedure_steps_individually_verified")
    if re.search(r'承認|人間確認|レビュー', combined_text):
        explicit_checks.append("approval_points_verified")
    if re.search(r'架空|仮|想定|例示|サンプル', combined_text):
        explicit_checks.append("fictional_marking_verified")
    if re.search(r'実処理|実データ|実績|本番|実施済', combined_text):
        explicit_checks.append("no_real_processing_claimed")
    if len(outputs) > 1 or len(base_headings) > 2:
        explicit_checks.append("each_output_contract_verified")
    base_exit_checks = ["source_evidence_linked", "criterion_content_verified", "unknowns_and_failures_reported"]
    # 明示の検査は既定に追加するだけで、黙って置換しない(固定3件に丸めない)。
    exit_checks = list(dict.fromkeys(base_exit_checks + explicit_checks))
    if len(outputs) > 1:
        main_category = "multi_output_document"
    elif (outputs[0].get("artifact_category") or "document") in {"checklist", "faq_document", "procedure_document"}:
        main_category = str(outputs[0].get("artifact_category"))
    elif needs_semantic_review or purpose_code in {'menu_document'}:
        main_category = "reviewed_document"
    else:
        main_category = "document"
    contract = {
        "schema": SCHEMA, "criterion_ids": [key], "criterion": criterion,
        "inputs": [f"result/{dep.lower()}.md" for dep in dependencies],
        "source_refs": ["context:" + value for value in source_ids],
        "outputs": outputs,
        "external_actions": "approval_required",
        "role": "execution", "responsible_role": "system", "estimated_days": 2,
        "completion_evidence": "validated_artifact", "failure_policy": "stop_and_report",
        "evidence_required": True,
        "exit_checks": exit_checks,
        "planned_review_notes": review_notes,
        "artifact_category": main_category,
    }
    # P0-2: 原本読取の工程宣言。両条件のときだけ source.read_excerpt を重複なく併記。
    # 登録原本が無いときは宣言しない。既存契約の他項目は変えない。
    if _p02_needs_source_read(criterion, source_ids):
        _p02_declared = contract.get("required_capabilities")
        if not isinstance(_p02_declared, list):
            _p02_declared = []
        _p02_merged = [str(x) for x in _p02_declared if str(x)]
        if P02_SOURCE_READ_CAPABILITY not in _p02_merged:
            _p02_merged.append(P02_SOURCE_READ_CAPABILITY)
        contract["required_capabilities"] = _p02_merged
    contract['public_purpose_code'] = _classify_purpose(criterion, combined_text)
    web_research = requires_public_web_research(criterion)
    if web_research:
        contract["public_web_research"] = {
            "required": True,
            "query": public_web_query(criterion),
            "official_sources_required": bool(re.search(r"公募要領|法令|制度|補助金", criterion)),
        }
    if _p01_required is None and re.search(r'案件管理|商談.{0,20}管理|(?:営業|顧客).{0,20}(?:管理表|CSV)', criterion, re.I):
        if not any(str(item.get("path") or "").lower().endswith("_tracker.csv") for item in outputs):
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
        kind = 'social_post' if re.search(r'SNS', criterion, re.I) else 'approved_external_action'
        if kind == 'approved_external_action' and re.search(r'Google\s*Forms', criterion, re.I):
            kind = 'google_form_publication'
        elif kind == 'approved_external_action' and re.search(r'リード取得', criterion, re.I):
            kind = 'lead_capture'
        elif kind == 'approved_external_action' and re.search(r'公開', criterion, re.I):
            kind = 'approved_publication'
        elif kind == 'approved_external_action' and re.search(r'送信|連絡', criterion, re.I):
            kind = 'approved_outbound_communication'
        elif kind == 'approved_external_action' and re.search(r'契約', criterion, re.I):
            kind = 'approved_contract_confirmation'
        elif kind == 'approved_external_action' and re.search(r'営業|商談|面談|提案|実行', criterion, re.I):
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
                         'approval_required': True, 'semantic_review_required':True,
                         'reviewer_role': 'human_approver'})
    if purpose_code in {
        'market_and_customer_definition', 'service_package_design', 'commercial_terms',
        'sales_assets', 'sales_plan', 'prospect_prioritization', 'outreach_content',
        'delivery_process', 'menu_document',
    } or needs_semantic_review or bool(contract.get("action_requirements")):
        contract.update({
            'approval_required': True,
            'human_confirmation_required': True,
            'semantic_review_required': True,
            'reviewer_role': 'human_approver',
            'completion_evidence': 'validated_artifact_and_human_confirmation',
        })
    elif needs_semantic_review is False and purpose_code == 'criterion_delivery':
        contract['semantic_review_required'] = False
    if needs_semantic_review and purpose_code in {'menu_document', 'criterion_delivery'}:
        contract.update({
            'semantic_review_required': True,
            'human_confirmation_required': True,
            'reviewer_role': 'human_approver',
        })
    output_paths = [item.get("path") for item in outputs if isinstance(item, dict) and item.get("path")]
    # 修正A: 達成条件と登録原本から原子的な検査項目を生成する。準備タスクは対象外。
    # P0-A: registered_text を渡すと要求文と検査対象の分離(具体値化)・対象パスの
    # 照合を行う。渡さない場合は従来どおり(旧呼び出しの互換を維持)。
    from app.requirement_matrix import extract_requirement_items
    if index == 0:
        contract["requirement_matrix"] = []
    else:
        contract["requirement_matrix"] = extract_requirement_items(
            key, criterion, contract.get("outputs") or [], list(source_ids or []),
            require_semantic=bool(contract.get("semantic_review_required")),
            registered_text=registered_text,
        )
    assumptions_note = ""
    if re.search(r'仮|想定|例示|架空|サンプル|目安|仮定|未定|要確認', combined_text):
        assumptions_note = "仮定・想定・例示は事実として扱わず、要確認として明記する。"
    food_safety_note = ""
    if re.search(r'食品|衛生|アレルギ|賞味|消費期限|保存|加熱|調理', combined_text):
        food_safety_note = "食品安全の仮定は事実として扱わず、確認手順と要確認を明記する。"
    return {
        "task_key": key, "depends_on": dependencies, "title": title.strip(),
        "mode": "research" if web_research else "local",
        "description": (
            f"達成条件 {key}: {criterion}\n内容設計: {scope}\n"
            f"operations/write_textで{primary_path}へ完成した日本語Markdownを保存する。\n"
            + (
                f"このタスクは出力契約{len(outputs)}件を持つ。各出力パスへ完成した日本語Markdownを保存する: "
                + "、".join(output_paths) + "。\n"
                if len(outputs) > 1 else ""
            )
            + "根拠資料を確認し、事実・仮説・要確認を区別する。価格や成果の保証を捏造しない。"
            + assumptions_note
            + food_safety_note
            + ("レビュー観点（自動退出判定ではない）: " + "、".join(review_notes) + "。" if review_notes else "")
            + "外部への送信や契約など承認対象の操作は、未承認なら承認待ちと明記する。"
            "必要なデータがない場合は不足資料と検証手順を記載し、数値・単位・配賦基準を推測で補わない。"
            "計画やテンプレートの作成を実処理の完了と判定しない。\n"
            + "登録原本: " + ", ".join(contract["source_refs"])
            + ("\n公開Web検索はこのタスクに限り許可済み。HTTPSの公開情報だけを取得し、公式一次資料を優先する。"
               "プロジェクト原本・試算表・個人情報・認証情報を検索語や外部AIへ送らない。"
               "取得URL、取得日時、本文ハッシュを根拠として保存し、ローカルLLMが本文を分析する。"
               if web_research else "")
            + ('\n案件管理CSVもtable_operations/create_tableで保存する。実データがない場合は状態を「未入力テンプレート」とした記入用の1行を作り、顧客名・実績・面談を捏造しない。' if any(str(item.get('path') or '').endswith('_tracker.csv') for item in contract['outputs']) else '')
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
    prep_contract['internal_preparation_gate_ids'] = ['PREP00']
    prep_contract.update({
        'role':'preparation','responsible_role':'system','estimated_days':1,
        'completion_evidence':'validated_artifact','failure_policy':'stop_and_report',
        'evidence_required':True,'artifact_category':'preparation_record','public_purpose_code':'input_readiness',
        'exit_checks':['registered_sources_classified','missing_inputs_listed','validation_method_defined'],
        'approval_required':False,'human_confirmation_required':False,'semantic_review_required':False,
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
        'menu_document': 28,
        'campaign_execution_sequence': 30, 'approved_customer_engagement': 31,
        'execution_status': 40,
    }
    indexed_tasks = list(enumerate(tasks))
    remaining = list(indexed_tasks)
    ordered_tasks = []
    while remaining:
        remaining_keys = {task['task_key'] for _, task in remaining}
        ready = [
            pair for pair in remaining
            if (contract_of(pair[1]) or {}).get('public_web_research', {}).get('required')
            or not any(dep in remaining_keys for dep in pair[1].get('depends_on', []))
        ]
        if not ready:
            ready = [remaining[0]]
        selected = min(ready, key=lambda pair: (
            0 if (contract_of(pair[1]) or {}).get('public_web_research', {}).get('required')
            else priority.get((contract_of(pair[1]) or {}).get('public_purpose_code'), 29),
            pair[0],
        ))
        remaining.remove(selected)
        ordered_tasks.append(selected[1])
    tasks = [preparation, *ordered_tasks]
    web_tasks = [
        current for current in tasks[1:]
        if (contract_of(current) or {}).get("public_web_research", {}).get("required")
    ]
    keys_by_purpose = {}
    for current in tasks[1:]:
        purpose = (contract_of(current) or {}).get('public_purpose_code')
        keys_by_purpose.setdefault(purpose, []).append(current['task_key'])
    semantic_parents = {
        'service_package_design': ['market_and_customer_definition'],
        'commercial_terms': ['market_and_customer_definition', 'service_package_design'],
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
    # Semantic dependencies are added after the model proposal order is chosen.
    # Reorder again so every dependency is guaranteed to precede its consumer.
    pending_tasks = list(tasks[1:])
    semantic_order = []
    resolved_keys = {'SC00'}
    while pending_tasks:
        ready_tasks = [
            item for item in pending_tasks
            if (contract_of(item) or {}).get('public_web_research', {}).get('required')
            or set(item.get('depends_on', [])) <= resolved_keys
        ]
        ready_tasks.sort(key=lambda item: (
            0 if (contract_of(item) or {}).get('public_web_research', {}).get('required') else 1
        ))
        if not ready_tasks:
            # Validation below reports an actual cycle or missing key with details.
            semantic_order.extend(pending_tasks)
            break
        for item in ready_tasks:
            pending_tasks.remove(item)
            semantic_order.append(item)
            resolved_keys.add(item['task_key'])
    tasks = [tasks[0], *semantic_order]
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
    # External-action dependency normalization above can change the graph again.
    # Perform one final topological ordering and synchronize every input contract.
    remaining_final = list(tasks[1:])
    final_order = []
    final_resolved = {'SC00'}
    while remaining_final:
        final_ready = [
            item for item in remaining_final
            if set(item.get('depends_on', [])) <= final_resolved
        ]
        if not final_ready:
            # Model-authored dependencies may conflict with backend semantic ordering.
            # Keep only already resolved edges on the safest next domain step.
            selected = min(remaining_final, key=lambda item: (
                priority.get((contract_of(item) or {}).get('public_purpose_code'), 29),
                item['task_key'],
            ))
            selected['depends_on'] = [
                dependency for dependency in selected.get('depends_on', [])
                if dependency in final_resolved
            ] or ['SC00']
            final_ready = [selected]
        for item in final_ready:
            remaining_final.remove(item)
            final_order.append(item)
            final_resolved.add(item['task_key'])
    tasks = [tasks[0], *final_order]
    outputs_by_key = {
        item['task_key']: [output['path'] for output in contract_of(item).get('outputs', [])]
        for item in tasks
    }
    for item in tasks[1:]:
        item_contract = contract_of(item)
        item_contract['inputs'] = [
            path for dependency in item.get('depends_on', [])
            for path in outputs_by_key.get(dependency, [])
        ]
        item['acceptance_criteria'] = json.dumps(item_contract, ensure_ascii=False)
    paths = [output['path'] for task in tasks for output in contract_of(task)['outputs']]
    source_refs = list(dict.fromkeys(
        ref for task in tasks for ref in (contract_of(task) or {}).get('source_refs', [])
    ))
    contract = {
        "schema": SCHEMA,
        "criterion_ids": [f"SC{i:02d}" for i in range(1, len(criteria) + 1)],
        "inputs": paths, "source_refs": source_refs, "execution_kind": "final_verification",
        "outputs": [{"path": "result/final_verification.md", "required_headings": ["達成条件別判定", "成果物検証", "準備段階の検査結果", "未達条件と承認待ち", "検証レポートの作成責任と証拠", "条件別PASS/FAIL"], "minimum_characters": 600}],
        "external_actions": "approval_required", "final_verification": True,
        "role": "final_verification", "responsible_role": "system",
        "estimated_days": 1, "completion_evidence": "validated_artifact",
        "failure_policy": "retry_bounded_then_stop", "max_retries": 2,
        "upstream_failure_action": "reopen_producing_task_on_item_fail",
        "human_confirmation_required": True,
        "human_approval_gate": "separate_human_acceptance",
        "approval_gate": "human_acceptance_required",
        "semantic_review_required": True,
        "evidence_required": True, "artifact_category": "verification_report",
        "public_purpose_code": "final_goal_verification",
        "verification_report": {
            "generated_by": "system",
            "requires_evidence_paths": True,
            "requires_per_criterion_verdicts": True,
        },
        "exit_checks": ["all_criteria_decided", "evidence_paths_verified", "unmet_and_pending_reported", "per_criterion_verdicts_recorded"],
    }
    tasks.append({
        "task_key": "final_verification", "depends_on": [task["task_key"] for task in tasks],
        "title": "最終検証・達成条件別の判定", "mode": "local",
        "description": "セバスが準備工程の完了状態・成果物の形式検査と全先行成果物・実操作の証拠を確認し、各達成条件のPASS/FAIL、根拠パス、未達・承認待ちをresult/final_verification.mdへ保存する。検証レポートに不備がある場合は最大2回まで再作成し、先行成果物の不備は元の工程へ戻す。それでも不合格なら停止する。資料作成と実処理を区別し、未実施の処理を合格にしない。人間の承認は別の承認ゲートで行い、未承認を完了扱いにしない。",
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
        # P0-1: 明示の必須成果物パスがある達成条件は outputs を完全一致で照合する。
        # unknown のときは通さず reserved 扱いとし、理由コードを返す(成功扱いにしない)。
        _p01_criterion = str(contract.get("criterion") or "")
        _p01_exec = bool(contract.get("criterion_ids")) and not contract.get("final_verification")
        if _p01_exec and str(contract.get("role") or "") not in ("preparation", "final_verification"):
            _p01_req = extract_required_output_paths(_p01_criterion)
            if _p01_req.get("status") == "unknown":
                errors.append(f"{key}: {REQUIRED_OUTPUT_PATH_UNKNOWN}")
            elif _p01_req.get("status") == "explicit":
                _p01_actual = [str(item.get("path") or "") for item in (contract.get("outputs") or [])
                               if isinstance(item, dict) and str(item.get("path") or "")]
                if _p01_actual != list(_p01_req.get("paths") or []):
                    errors.append(f"{key}: {REQUIRED_OUTPUT_PATH_MISMATCH}")
                    errors.append(f"{key}: incomplete required output contract")
                _p01_norm = normalize_p0a_artifact_requirement(_p01_criterion)
                if not _p01_norm.get("consistent"):
                    errors.append(f"{key}: {REQUIRED_OUTPUT_PATH_MISMATCH}")
                elif _p01_norm.get("item_counts") or _p01_norm.get("explicit_total") is not None:
                    _p01_need = _p0a_required_or_raise(_p01_norm, where="validate_plan:required_output")
                    if len(_p01_actual) != _p01_need:
                        errors.append(f"{key}: {REQUIRED_OUTPUT_PATH_MISMATCH}")
        for output in contract["outputs"]:
            path = output["path"]
            pattern = r"result/(?:plan_[1-9][0-9]*/)?[a-z0-9_]+(?:_[a-z0-9_]+)?\.(?:md|csv)"
            if contract.get('execution_kind', '').startswith('vehicle_'):
                pattern = rf"result/vehicle/v{contract.get('workflow_version', 0)}/(?:sources|normalization|calculation|verification|profit)\.(?:json|xlsx)"
            _p01_explicit_ok = (
                _p01_exec and _p01_req.get("status") == "explicit"
                and str(path) in list(_p01_req.get("paths") or [])
            )
            if (not _p01_explicit_ok and not re.fullmatch(pattern, path)) or path in producers:
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
    # 修正A: 成果物と項目の対応を構造検証する。片方ゼロは不合格、未抽出・不明・
    # 未定義は needs_review 相当の不合格(勝手に通さない)。
    from app.requirement_matrix import validate_requirement_matrix
    matrix_check = validate_requirement_matrix(plan)
    errors.extend(matrix_check.get("issues") or [])
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
    """rebuild_generic の保存前厳格バリデーション（P0-1照合を含む）。"""
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
        _p01_exec_rebuild = (
            bool(contract.get("criterion_ids")) and not contract.get("final_verification")
            and str(contract.get("role") or "") not in ("preparation", "final_verification")
        )
        _p01_req_rebuild = extract_required_output_paths(str(contract.get("criterion") or "")) if _p01_exec_rebuild else {"status": "none", "paths": []}
        if _p01_req_rebuild.get("status") == REQUIRED_OUTPUT_PATH_UNKNOWN or _p01_req_rebuild.get("status") == "unknown":
            raise ValueError(f"{REQUIRED_OUTPUT_PATH_UNKNOWN}: タスク '{key}' の必須成果物パスが不定です")
        if _p01_req_rebuild.get("status") == "explicit":
            actual = [str(item.get("path") or "") for item in outputs
                      if isinstance(item, dict) and str(item.get("path") or "")]
            if actual != list(_p01_req_rebuild.get("paths") or []):
                raise ValueError(f"{REQUIRED_OUTPUT_PATH_MISMATCH}: タスク '{key}' の出力が指定パスと一致しません")
        for output in outputs:
            path = str(output.get("path") or "")
            pattern = r"result/(?:plan_[1-9][0-9]*/)?[a-z0-9_]+(?:_[a-z0-9_]+)?\.(?:md|csv)"
            _p01_explicit_rebuild = (
                _p01_req_rebuild.get("status") == "explicit"
                and path in list(_p01_req_rebuild.get("paths") or [])
            )
            if not _p01_explicit_rebuild and not re.fullmatch(pattern, path):
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

    # Coverage by ID alone can pass while an SC task describes a different goal.
    if goal_contract and goal_contract.get("criteria"):
        statements = {
            item["criterion_id"]: item.get("statement")
            for item in goal_contract["criteria"] if item.get("criterion_id")
        }
        for task in tasks:
            contract = contract_of(task) or {}
            if contract.get("final_verification"):
                continue
            for criterion_id in contract.get("criterion_ids") or []:
                if criterion_id in statements and contract.get("criterion") != statements[criterion_id]:
                    raise ValueError(
                        f"CRITERION_MISMATCH: {task['task_key']} does not implement {criterion_id}"
                    )
            if re.fullmatch(r"達成条件 SC[0-9]+ の実行設計", str(task.get("title") or "")):
                raise ValueError(f"ABSTRACT_TASK: {task['task_key']} has no concrete operation")

    return {"passed": True, "task_count": len(tasks), "artifact_count": len(seen_paths)}


def _contract_digest(contract: dict | None) -> str:
    """構造フィールドの決定的な短い要約。差分の有無だけを判定する。"""
    if not isinstance(contract, dict):
        return hashlib.sha256(b"missing").hexdigest()[:16]
    material = {
        "role": contract.get("role"),
        "responsible_role": contract.get("responsible_role"),
        "outputs": contract.get("outputs"),
        "exit_checks": contract.get("exit_checks"),
        "requirement_matrix": contract.get("requirement_matrix"),
        "semantic_review_required": contract.get("semantic_review_required"),
        "human_confirmation_required": contract.get("human_confirmation_required"),
        "public_purpose_code": contract.get("public_purpose_code"),
        "artifact_category": contract.get("artifact_category"),
        "completion_evidence": contract.get("completion_evidence"),
    }
    raw = json.dumps(material, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _structural_change_kinds(old: dict | None, new: dict) -> list[str]:
    kinds: list[str] = []
    old = old or {}
    if (old.get("role") != new.get("role")
            or old.get("responsible_role") != new.get("responsible_role")):
        kinds.append("role")
    old_outputs = old.get("outputs") or []
    new_outputs = new.get("outputs") or []
    if json.dumps(old_outputs, ensure_ascii=False, sort_keys=True, default=str) != json.dumps(
        new_outputs, ensure_ascii=False, sort_keys=True, default=str
    ):
        kinds.append("outputs")
    if list(old.get("exit_checks") or []) != list(new.get("exit_checks") or []):
        kinds.append("exit_checks")
    for field in ("semantic_review_required", "human_confirmation_required",
                  "public_purpose_code", "artifact_category", "completion_evidence",
                  "requirement_matrix"):
        if old.get(field) != new.get(field):
            kinds.append(field)
            break
    return list(dict.fromkeys(kinds))


def _is_structural_issue_text(text: str) -> bool:
    blob = str(text or "")
    if not blob.strip():
        return False
    return bool(re.search(
        r'役割|成果物|検査|検証|exit_checks|acceptance_criteria|artifact|契約|構造|被覆|coverage|'
        r'criteria_count|document_or_legacy|parents|purpose|semantic|承認.{0,12}(?:工程|点|操作)|'
        r'最終.{0,16}(?:工程|検証|成果)|完了判定|依存.{0,20}(?:不足|無い|ない|欠落)',
        blob, re.I,
    ))


def build_rebuild_diff(old_snapshot: dict, new_plan: dict) -> dict:
    """再構成の前後差分。構造フィールドの変化だけを記録する。"""
    old_contracts = {}
    for task in (old_snapshot or {}).get("tasks") or []:
        if not isinstance(task, dict):
            continue
        key = str(task.get("task_key") or "")
        if not key:
            continue
        old_contracts[key] = contract_of(task)
    rows: list[dict] = []
    changed = 0
    for task in (new_plan or {}).get("tasks") or []:
        if not isinstance(task, dict):
            continue
        key = str(task.get("task_key") or "")
        if not key:
            continue
        new_contract = contract_of(task) or {}
        old_contract = old_contracts.get(key)
        kinds = _structural_change_kinds(old_contract, new_contract)
        if kinds:
            changed += 1
        rows.append({
            "task_key": key,
            "changed": bool(kinds),
            "change_kinds": kinds,
            "before_digest": _contract_digest(old_contract),
            "after_digest": _contract_digest(new_contract),
        })
    return {
        "version": "rebuild-diff-v1",
        "changed_tasks": changed,
        "rows": rows,
    }


def _rebuild_issue_texts(actions: list[dict], issues: list[dict]) -> list[str]:
    texts: list[str] = []
    for action in actions or []:
        if not isinstance(action, dict):
            continue
        for field in ("change", "reason"):
            value = str(action.get(field) or "").strip()
            if value:
                texts.append(value)
    for issue in issues or []:
        if not isinstance(issue, dict):
            continue
        for field in ("text", "reason", "unmet_goal", "remedy"):
            value = str(issue.get(field) or "").strip()
            if value:
                texts.append(value)
    return texts


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
    # The active GoalContract defines the criterion ID/order used by coverage.
    # Re-extracting mission prose can yield the same count in a different order.
    criteria = contract_criteria or mission_criteria
    if not criteria:
        criteria = [mission.get("goal") or "プロジェクト目標の達成"]

    source_ids = [x["id"] for x in snapshot.get("sources", []) if isinstance(x, dict) and x.get("id")]
    existing_by_criterion = {
        (contract_of(task) or {}).get("criterion"): task
        for task in snapshot.get("tasks", [])
        if (contract_of(task) or {}).get("criterion")
        and not (contract_of(task) or {}).get("final_verification")
    }

    # 採用された指摘の要約・反映差分を整理
    generic_actions = [a for a in actions if a.get("disposition") in {"rebuild_generic", "amend"}]
    structural_issue = any(
        _is_structural_issue_text(text) for text in _rebuild_issue_texts(generic_actions, issues)
    )
    action_changes_by_target = {}
    for a in generic_actions:
        target = a.get("target") or ""
        change = a.get("change") or ""
        if target and change:
            action_changes_by_target.setdefault(target, []).append(change.strip())

    tasks = []
    for index, criterion in enumerate(criteria, 1):
        key = f"SC{index:02d}"
        old_task = existing_by_criterion.get(criterion)
        # Feedback targets refer to the old plan; never attach an amendment to
        # a different criterion merely because its new SC index matches.
        target_key = old_task.get("task_key") if old_task else key
        target_changes = list(action_changes_by_target.get(target_key, []))
        for action in generic_actions:
            if action.get("target") != "execution_pipeline" or not action.get("change"):
                continue
            bound_ids = (action.get("binds") or {}).get("goal_criterion_ids") or []
            # A repaired action names current GoalContract criteria. Its text
            # must not be broadcast to unrelated criteria.
            if not bound_ids or key in bound_ids:
                target_changes.append(action["change"].strip())

        if old_task:
            old_contract = contract_of(old_task) or {}
            title = re.sub(r"^SC[0-9]+_", "", str(old_task.get("title") or "").strip())
            title = title or str(criterion).strip().rstrip("。")[:110]
            # P0-1: 再計画のscope文面はパス検証の対象外にするため、
            # 達成条件文中の result/ 表記を汎用語へ置換する(指定パスの削り落としではない)。
            safe_criterion = sanitize_proposal({"scope": str(criterion)}).get("scope", str(criterion))
            scope = f"達成条件 {key}: {safe_criterion} を満たす成果物を作成・検証する。"
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
                "outputs": [{"kind": out.get("artifact_category") or "document",
                             "path": out.get("path"),
                             "required_headings": [h for h in out.get("required_headings", [])
                                                   if h not in {"根拠と未確認事項", "実施状態と次の行動"}][:8]}
                            for out in old_contract.get("outputs", [])
                            if out.get("path") and len(out.get("required_headings", [])) >= 2],
                # Old SC IDs may belong to a different criterion order.
                "depends_on": [],
            }
        else:
            # P0-1: 新規再計画分も同様にscope文面の result/ 表記を無害化する。
            safe_criterion = sanitize_proposal({"scope": str(criterion)}).get("scope", str(criterion))
            scope = f"達成条件 {key}: {safe_criterion} を満たす成果物を作成・検証する。"
            if target_changes:
                scope += " " + " ".join(target_changes)
            headings = ["現状と前提確認", "具体的設計・作成手順"]
            proposal = {
                "title": str(criterion).strip().rstrip("。")[:110] or f"達成条件 {key} の実行",
                "scope": scope[:1500],
                "headings": headings,
                "depends_on": [],
            }
        task = compile_task(index, criterion, proposal, source_ids)
        tasks.append(task)

    compiled = compile_plan(criteria, tasks, goal=mission.get("goal", ""))
    if structural_issue:
        diff = build_rebuild_diff({"tasks": snapshot.get("tasks") or []}, compiled)
        if diff.get("changed_tasks", 0) <= 0:
            raise ValueError(
                "REBUILD_NOT_STRUCTURAL: 構造指摘に対する再構成ですが、"
                "acceptance_criteriaの役割・成果物・検査項目に差分がありません。"
                "scope文章への追記だけでは被覆を合格にしません"
            )
        compiled["rebuild_diff"] = diff
        compiled["rebuild_applied_issue_count"] = len(_rebuild_issue_texts(generic_actions, issues))
    compiled["rebuild_plan_signature"] = hashlib.sha256(
        json.dumps(compiled.get("tasks"), ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    return compiled


# P0-2: 原本読取の工程宣言。達成条件が source_extraction に対応し、
# 登録済み原本があるときだけ required_capabilities に source.read_excerpt を足す。
# 外部通信・LLM呼出なし。決定的。宣言の有無以外の契約内容は変えない。
P02_SOURCE_READ_CAPABILITY = "source.read_excerpt"


def _p02_needs_source_read(criterion_text: object, source_ids) -> bool:
    """原本読取の宣言が必要か。失敗・判定不能は宣言しない側に倒す。

    fail-open lint (P1) の対象にならないよう、広い try/except は使わない。
    型の確認と getattr の有無で判定し、例外を握りつぶさない。
    """
    if isinstance(source_ids, str) or not isinstance(source_ids, (list, tuple, set)):
        return False
    if not any(isinstance(item, str) and item.strip() for item in source_ids):
        return False
    from app import capability_gap as _p02_gap
    matcher = getattr(_p02_gap, "_match_vocab", None)
    if not callable(matcher):
        return False
    matched = matcher(str(criterion_text or ""))
    if not isinstance(matched, list):
        return False
    return "source_extraction" in matched
# 日付/時刻/電話番号/金額の正規表現による抽出と、登録コンテキストの同種の値との
# 照合だけを行う。原本本文の外部転送は行わない。読取失敗・根拠なし・曖昧・
# 判定不能は PASS にしない。不明は別フラグで残し、値で補完しない。
P0A_FACT_VERSION = "p0a-fact-check-v1"
P0A_ARTIFACT_VERSION = "p0a-artifact-check-v3"
P0A_SLOT_REGEN_MAX = 2

P0A_FACT_PATTERNS = (
    ("date", re.compile(r"\d{4}[年/\-]\d{1,2}[月/\-]\d{1,2}日?|\d{1,2}月\d{1,2}日")),
    ("time", re.compile(r"(?:午前|午後)?\d{1,2}:\d{2}|(?:午前|午後)?\d{1,2}時\d{1,2}分?|(?:午前|午後)?\d{1,2}時|正午")),
    ("phone", re.compile(r"0\d{1,4}[-\s\u3000]*\d{1,4}[-\s\u3000]*\d{3,4}|0[5789]0\d{7,8}")),
    ("amount", re.compile(r"[¥￥]\s*\d[\d,]*|\d[\d,]*円|\d+\s*万円")),
)

_KANJI_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
                 "六": 6, "七": 7, "八": 8, "九": 9}
_P0A_COUNT_RE = re.compile(r"(\d+)\s*(点|件|通|冊|ファイル|文書|資料|成果物)")
_P0A_KANJI_COUNT_RE = re.compile(r"([一二三四五六七八九])\s*(点|件|通|冊|ファイル|文書|資料|成果物)")
_P0A_TOTAL_TAIL_RE = re.compile(r"(?:合計|総計|計)\s*$")
_P0A_EACH_TAIL_RE = re.compile(r"各\s*$")


def _p0a_required_or_raise(norm: dict, *, where: str) -> int:
    """consistent=True の不変条件(整数 required)を強制する。

    欠落・None・不正値のときは1に倒さず ValueError(判定不能)にする。
    呼び出し側は保存しない/拒否する。矛盾する達成条件もここで拒否する。
    """
    if not isinstance(norm, dict) or not norm.get("consistent"):
        raise ValueError(f"{where}: artifact requirement is undetermined; refusing to save")
    required = norm.get("required")
    if isinstance(required, bool) or not isinstance(required, int):
        raise ValueError(f"{where}: artifact requirement invariant broken; refusing to save")
    if not 1 <= required <= 9:
        raise ValueError(f"{where}: artifact requirement invariant broken; refusing to save")
    return required


def _p0a_file_linked(prefix: str, sentence: str, unit: str) -> bool:
    """数値+単位が独立ファイルの件数か(内容項目の件数は除く)。"""
    if unit in ("ファイル", "文書", "資料", "成果物"):
        return True
    content_noun = re.compile(r"(?:質問|設問|FAQ|品目|商品|メニュー|項目|事例|候補|回答|手順)\s*$", re.I)
    if content_noun.search(prefix[-16:]):
        return False
    if re.search(
        r"(?:ファイル|文書|資料|成果物|Markdown|マークダウン)"
        r"(?:を|の|は|が|各|計|合計|\s){0,5}$", prefix, re.I
    ):
        return True
    if unit == "点" and re.search(r"合計\s*$", prefix) and re.search(
        r"ファイル|文書|資料|成果物|Markdown|マークダウン", sentence, re.I
    ):
        return True
    if unit == "点" and re.search(r"別(?:ファイル|文書|資料)", sentence):
        return True
    return False


def normalize_p0a_artifact_requirement(criterion_text: str) -> dict:
    """一つの達成条件内の内訳と明示合計を矛盾なく正規化する。

    内訳(各N件/A1ファイル+B1ファイル)と明示合計(合計M件)を別々に集計し、
    合計≡内訳合計のときだけ必要数を確定する。先行する『1ファイル』だけを
    合計にしない。合計と内訳が食い違うときは consistent=False の矛盾として
    返し、呼び出し側は保存しない(判定不能を1件に倒さない)。
    内容項目の件数(質問N件等)は数えない。決定的・順序安定。
    """
    text = str(criterion_text or "")
    if not text:
        return {"version": P0A_ARTIFACT_VERSION, "consistent": True,
                "required": 1, "item_counts": [], "item_total": 0,
                "explicit_total": None, "conflict": None}
    item_counts: list[dict] = []
    totals: list[int] = []
    for sentence in re.split(r"[。\n；;]", text):
        for pattern in (_P0A_COUNT_RE, _P0A_KANJI_COUNT_RE):
            for match in pattern.finditer(sentence):
                raw, unit = match.group(1), match.group(2)
                try:
                    number = int(raw) if raw.isdigit() else _KANJI_DIGITS.get(raw, 0)
                except (TypeError, ValueError):
                    continue
                if not 1 <= number <= 9:
                    continue
                prefix = sentence[:match.start()]
                if not _p0a_file_linked(prefix, sentence, unit):
                    continue
                if _P0A_TOTAL_TAIL_RE.search(prefix[-8:]):
                    totals.append(number)
                else:
                    item_counts.append(
                        {"count": number,
                         "each": bool(_P0A_EACH_TAIL_RE.search(prefix[-4:]))})
    totals_sorted = sorted(totals)
    if len(set(totals_sorted)) > 1:
        return {"version": P0A_ARTIFACT_VERSION, "consistent": False,
                "required": None, "item_counts": item_counts,
                "item_total": sum(row["count"] for row in item_counts),
                "explicit_total": totals_sorted,
                "conflict": {"reason": "multiple_totals", "totals": totals_sorted}}
    explicit_total = totals_sorted[0] if totals_sorted else None
    item_total = sum(row["count"] for row in item_counts)
    has_each = any(row["each"] for row in item_counts)
    if explicit_total is None:
        if not item_counts:
            # 件数表現が無くても「別ファイル」「それぞれ」とあれば2件要求とみなす(従来互換)。
            if (re.search(r"ファイル|文書|資料|成果物|Markdown|マークダウン", text, re.I)
                    and re.search(r"別(?:ファイル|資料|文書|々)|それぞれ", text)):
                return {"version": P0A_ARTIFACT_VERSION, "consistent": True,
                        "required": 2, "item_counts": [], "item_total": 0,
                        "explicit_total": None, "conflict": None}
            return {"version": P0A_ARTIFACT_VERSION, "consistent": True,
                    "required": 1, "item_counts": [], "item_total": 0,
                    "explicit_total": None, "conflict": None}
        required = item_total if item_total >= 1 else 1
        return {"version": P0A_ARTIFACT_VERSION, "consistent": True,
                "required": required, "item_counts": item_counts,
                "item_total": item_total,
                "explicit_total": None, "conflict": None}
    if not item_counts:
        return {"version": P0A_ARTIFACT_VERSION, "consistent": True,
                "required": explicit_total, "item_counts": [],
                "item_total": 0,
                "explicit_total": explicit_total, "conflict": None}
    if has_each:
        # 「各1点+合計2点」のように各付けは単品の内訳合計と一致しない。
        # 合計が最大の単品未満のときだけ矛盾とする。
        peak = max([row["count"] for row in item_counts] + [0])
        if explicit_total < peak:
            return {"version": P0A_ARTIFACT_VERSION, "consistent": False,
                    "required": None, "item_counts": item_counts,
                    "item_total": item_total,
                    "explicit_total": explicit_total,
                    "conflict": {"reason": "total_smaller_than_item",
                                 "item_total": item_total,
                                 "explicit_total": explicit_total}}
        return {"version": P0A_ARTIFACT_VERSION, "consistent": True,
                "required": explicit_total, "item_counts": item_counts,
                "item_total": item_total,
                "explicit_total": explicit_total, "conflict": None}
    if item_total != explicit_total:
        return {"version": P0A_ARTIFACT_VERSION, "consistent": False,
                "required": None, "item_counts": item_counts,
                "item_total": item_total,
                "explicit_total": explicit_total,
                "conflict": {"reason": "item_total_mismatch",
                             "item_total": item_total,
                             "explicit_total": explicit_total}}
    return {"version": P0A_ARTIFACT_VERSION, "consistent": True,
            "required": explicit_total, "item_counts": item_counts,
            "item_total": item_total,
            "explicit_total": explicit_total, "conflict": None}


def build_p0a_artifact_slots(criterion_text: str) -> list[dict]:
    """正規化した必要数だけ決定的な成果物スロットの枠を作る。

    矛盾(consistent=False)のときは空列を返す(判定不能を1件に倒さない)。
    スロットの kind_hint は達成条件文の直前名詞からの汎用抜粋か連番であり、
    特定案件の語彙を製品コードに持たない。
    """
    norm = normalize_p0a_artifact_requirement(criterion_text)
    if not norm.get("consistent"):
        return []
    # consistent=True のとき required は必ず整数。破れたら判定不能として拒否する。
    required = _p0a_required_or_raise(norm, where="build_p0a_artifact_slots")
    labels: list[str] = []
    for sentence in re.split(r"[。\n；;]", str(criterion_text or "")):
        for pattern in (_P0A_COUNT_RE, _P0A_KANJI_COUNT_RE):
            for match in pattern.finditer(sentence):
                prefix = sentence[:match.start()]
                if _P0A_TOTAL_TAIL_RE.search(prefix[-8:]):
                    continue
                if not _p0a_file_linked(prefix, sentence, match.group(2)):
                    continue
                tail = re.sub(r"[＋+＝=、，,・/／\s　]+$", "", prefix[-14:]).strip()
                tail = re.sub(r"^.*[＋+＝=、，,・/／\s　]+", "", tail).strip()
                tail = re.sub(r"(?:を|の|は|が|各|計|合計)$", "", tail).strip()
                labels.append(tail[:12] if tail else "")
    slots: list[dict] = []
    for position in range(1, required + 1):
        hint = labels[position - 1] if position - 1 < len(labels) and labels[position - 1] else f"artifact-{position}"
        slots.append({"slot_id": f"slot-{position:02d}",
                      "kind_hint": hint, "format": "markdown"})
    return slots


def _p0a_normalize_fact(kind: str, value: str) -> str:
    text = re.sub(r"\s+", "", str(value or "").strip())
    if kind == "phone":
        text = re.sub(r"[-\s\u3000()（）]", "", text)
    if kind == "amount":
        text = text.replace(",", "").replace("，", "")
        text = re.sub(r"\s+", "", text)
    return text


def extract_p0a_fact_values(text: str) -> dict:
    """汎用規則で事実候補を抽出する。特定案件の語彙を持たない。決定的・ソート済み。"""
    found: dict[str, list[str]] = {}
    blob = str(text or "")
    if not blob:
        return {"date": [], "time": [], "phone": [], "amount": []}
    for kind, pattern in P0A_FACT_PATTERNS:
        values = []
        for match in pattern.finditer(blob):
            raw = match.group(0)
            norm = _p0a_normalize_fact(kind, raw)
            if norm and norm not in values:
                values.append(norm)
        found[kind] = sorted(values)
    return found


def check_p0a_fact_consistency(plan_text: str, registered_text: str) -> dict:
    """登録事実との矛盾を検出する。保存・承認の前に停止するための材料。

    登録コンテキストに同種の値が明示的に存在する場合だけ矛盾と判定する。
    同種の値が無ければ矛盾と判定しない。出典のない値は「未確認」として記録し、
    拒否しない。正しい値を補完しない。分母0で率を作らない。
    """
    plan_facts = extract_p0a_fact_values(plan_text)
    reg_facts = extract_p0a_fact_values(registered_text)
    conflicts: list[dict] = []
    unconfirmed: list[dict] = []
    for kind in ("date", "time", "phone", "amount"):
        reg_set = set(reg_facts.get(kind) or [])
        plan_list = list(plan_facts.get(kind) or [])
        if not plan_list:
            continue
        if not reg_set:
            for value in plan_list:
                unconfirmed.append({"kind": kind, "value": value,
                                    "status": "unconfirmed"})
            continue
        for value in plan_list:
            if value not in reg_set:
                conflicts.append({"kind": kind, "value": value,
                                  "registered": sorted(reg_set),
                                  "status": "conflict"})
    conflicts.sort(key=lambda r: (str(r.get("kind") or ""), str(r.get("value") or "")))
    unconfirmed.sort(key=lambda r: (str(r.get("kind") or ""), str(r.get("value") or "")))
    return {"version": P0A_FACT_VERSION,
            "passed": not conflicts,
            "conflicts": conflicts,
            "unconfirmed": unconfirmed,
            "checked_kinds": ["date", "time", "phone", "amount"]}


P0A_UNSUPPORTED_VERSION = "p0a-unsupported-check-v1"

_P0A_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_P0A_NUMBER_UNIT_RE = re.compile(
    r"(?:[0-9０-９]+(?:\.[0-9]+)?|[一二三四五六七八九十百千]+)\s*"
    r"(?:人|名|台|個|回|時間|分|秒|日間|週間|ヶ月|か月|ヵ月|棟|戸)"
)
_P0A_PROPER_AFFIX_RE = re.compile(
    r"[\u3040-\u30ff\u4e00-\u9fafA-Za-z]{2,6}(?:様|氏|さん)"
    r"|(?:株式会社|有限会社|合同会社)[\u3040-\u30ff\u4e00-\u9fafA-Za-z]{1,12}"
    r"|[\u3040-\u30ff\u4e00-\u9fafA-Za-z]{2,12}"
    r"(?:株式会社|有限会社|合同会社|センター|病院|学校)"
    r"|[\u3040-\u30ff\u4e00-\u9fafA-Za-z]{2,12}(?:部|課|室)(?=$|[\s、。,:：/（）()]|[はがをにでのへと])"
)
_P0A_AMBIGUOUS_NAME_RE = re.compile(
    r"(?:担当|責任者|連絡先|管理者|代表)[は:：\s]*([\u4e00-\u9faf]{2,4})(?![様氏さん])"
)
# P1-1 P0-C: 年齢条件は原本外の確約として検出する(見逃さない)。数値の単位だけでは
# 拾えないため専用パターンで抽出する。特定案件の語彙は持たない。
_P0A_AGE_COUNT_RE = re.compile(r"\d{1,3}\s*歳\s*(?:以上|以下|未満)?")
_P0A_AGE_WORD_RE = re.compile(r"年齢制限なし|年齢制限あり|年齢不問|年齢制限|年齢確認")
# P1-1 P0-C: 時刻の別表記(午後3時/正午/15時)と数値を区別するための汎用判定。
_P0C_JP_TIME_RE = re.compile(
    r"^(?:午前|午後)?\d{1,2}時(?:\d{1,2}分)?$|^\d{1,2}:\d{2}$|^正午$")
_P0C_JP_TIME_FIND_RE = re.compile(
    r"(?:午前|午後)?\d{1,2}時(?:\d{1,2}分)?|\d{1,2}:\d{2}|正午")
_P0C_RELATIVE_MINUTES_RE = re.compile(r"([0-9０-９]{1,3})\s*分\s*(前|後)")


def _p0c_registered_time_minutes(text: str) -> set[int]:
    """登録文中の時刻を表記ゆれ込みで分へ正規化する。"""
    values: set[int] = set()
    for match in _P0C_JP_TIME_FIND_RE.finditer(str(text or "")):
        canonical = _p1_parse_time_generic(match.group(0))
        if not canonical:
            continue
        hour, minute = canonical.split(":", 1)
        values.add(int(hour) * 60 + int(minute))
    return values


def _p0c_derived_relative_minutes(text: str) -> set[int]:
    """一意な開始・準備開始の登録値から導ける「開始N分前」だけを返す。"""
    from app.requirement_matrix import _p1_extract_role_datetimes

    roles, _ = _p1_extract_role_datetimes(text)
    starts = {_p1_parse_time_generic(raw) for raw in roles.get("start_time", [])}
    preps = {_p1_parse_time_generic(raw) for raw in roles.get("prep_time", [])}
    starts.discard("")
    preps.discard("")
    if len(starts) != 1 or len(preps) != 1:
        return set()
    start_hour, start_minute = next(iter(starts)).split(":", 1)
    prep_hour, prep_minute = next(iter(preps)).split(":", 1)
    difference = (int(start_hour) * 60 + int(start_minute)
                  - int(prep_hour) * 60 - int(prep_minute))
    return {difference} if difference > 0 else set()

def _p1_is_time_jp(raw: str) -> bool:
    """汎用の日本語時刻表記か(午前/午後/時/分/正午/HH:MM)。特定案件の語彙なし。"""
    import unicodedata as _ud

    body = re.sub(r"[\s　]+", "", _ud.normalize("NFKC", str(raw or "")))
    return bool(_P0C_JP_TIME_RE.match(body))


def _p1_parse_time_generic(raw: str) -> str:
    """汎用の日本語時刻を正午換算で正規化する。時刻でなければ空文字。"""
    import unicodedata as _ud

    body = re.sub(r"[\s　]+", "", _ud.normalize("NFKC", str(raw or "")))
    if body == "正午":
        return "12:00"
    match = re.match(r"^(午前|午後)?(\d{1,2})時(\d{1,2})?分?$", body)
    if match:
        meridiem, hour, minute = match.group(1) or "", int(match.group(2)), int(match.group(3) or 0)
        if meridiem == "午後" and hour < 12:
            hour += 12
        if meridiem == "午前" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
    match = re.match(r"^(\d{1,2}):(\d{2})$", body)
    if match and 0 <= int(match.group(1)) <= 23 and 0 <= int(match.group(2)) <= 59:
        return f"{int(match.group(1)):02d}:{match.group(2)}"
    return ""
# P1-1 P0-C: 数量でない数詞・普通名詞の一部を固有名にしない narrow 除外。
# 十分/充分(副詞)、序数(〜目)、様態(〜人で)は人数・個数の確約にしない。
_P0C_NON_QUANTITY_NUMBERS = {"十分", "充分"}
# 部・課・室のうち普通名詞の一部(1文字の数量・方角語+部)は固有名にしない。
_P0C_COMMON_AFFIX_RE = re.compile(r"^[一二三四五六七八九十百千全部内外上下中前後]部$")
_P0C_GENERIC_HONORIFICS = ("お子様", "お客様", "皆様", "お子さま", "お客さま")
_P0A_MASK_RE = re.compile(
    r"(?:SC|C)\d{2}|result/[A-Za-z0-9_./()（）\-]+|context:[A-Za-z0-9_\-]+"
)
_KANJI_SINGLE = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5",
                 "六": "6", "七": "7", "八": "8", "九": "9"}


# P1-1: 年齢条件は原本外の確約として検出する(例: 20歳以上・年齢制限なし)。
def _p1_is_age_assertion(raw: str) -> str:
    """年齢条件の確約か。確約なら正規化値、違えば空文字。特定案件の語彙なし。"""
    import unicodedata as _ud

    body = re.sub(r"[\s　]+", "", _ud.normalize("NFKC", str(raw or "")))
    if _P0A_AGE_WORD_RE.fullmatch(body):
        return {"年齢制限なし": "no_age_limit", "年齢制限あり": "age_limited",
                "年齢不問": "no_age_limit"}.get(body, body)
    match = _P0A_AGE_COUNT_RE.fullmatch(body)
    if match:
        return re.sub(r"\s+", "", body)
    return ""


def _p0a_unsupported_canon(kind: str, value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"[\s　]+", "", text)
    if kind == "contact_email":
        return text.casefold()
    if kind == "contact_phone":
        return re.sub(r"[-\s　()（）]", "", text)
    if kind == "age":
        return text
    if kind == "datetime":
        body = text
        meridiem = ""
        if body in ("正午",):
            return "12:00"
        match = re.match(r"^(午前|午後)?(\d{1,2})時(\d{1,2})?分?$", body)
        if match:
            meridiem, hour, minute = match.group(1) or "", int(match.group(2)), int(match.group(3) or 0)
            if meridiem == "午後" and hour < 12:
                hour += 12
            if meridiem == "午前" and hour == 12:
                hour = 0
            return f"{hour:02d}:{minute:02d}"
        match = re.match(r"^(\d{1,2}):(\d{2})$", body)
        if match:
            return f"{int(match.group(1)):02d}:{match.group(2)}"
        body = body.replace("/", "-").replace(".", "-")
        body = re.sub(r"(\d)年", r"\1-", body)
        body = re.sub(r"(\d)月", r"\1-", body)
        body = body.rstrip("-").rstrip("日")
        parts = [part.zfill(2) if part.isdigit() else part for part in body.split("-") if part != ""]
        return "-".join(parts)
    if kind == "number":
        for source, target in _KANJI_SINGLE.items():
            text = text.replace(source, target)
        text = re.sub(r"(\d)\s*([人名前台個回時間分秒])", r"\1\2", text)
        return text
    return text


def _p0a_unsupported_candidates(text: str) -> tuple[list[dict], list[dict]]:
    """決定的に抽出できる候補と、検査範囲外の候補を分ける。順序安定。

    P1-1: 数量らしき語でも数量の確約でないものは number にしない(誤検出の修正)。
    十分/充分(副詞)の『分』、序数・慣用表現(〜目、〜人で)は抽出しない。普通名詞の
    一部(〜部)や汎用敬称は proper にしない。原本の登録時は narrow に外すだけ、
    判定側(P0-C)は既存の確約検出(保証・年齢条件・返却/無料など)を維持する。
    """
    blob = str(text or "")
    masked = _P0A_MASK_RE.sub(" ", blob)
    found: list[dict] = []
    scoped: list[dict] = []

    def _snippet(start: int, end: int) -> str:
        return blob[max(0, start - 12):end + 12].strip()

    def _is_non_quantity(match: "re.Match[str]") -> bool:
        raw = match.group(0)
        # P1-1(a): 時刻の別表記(午後3時/10時/正午/15時)は日時として扱い、
        # 人数・個数の確約にしない。
        if raw in ("正午",):
            return True
        if _p1_parse_time_generic(raw) != "":
            return True
        # 十分/充分 + 量(分)は副詞(十分な時間等)。数量にしない。
        head = raw[:2]
        if head in _P0C_NON_QUANTITY_NUMBERS and raw[2:].startswith("分"):
            return True
        start = match.start()
        before = masked[max(0, start - 6):start]
        after = masked[match.end():match.end() + 6]
        # 序数(〜目・〜つ目・〜番目)は順序であり数量の確約にしない。
        if after.startswith(("目", "つ目", "番目")):
            return True
        # 様態(二人で・三人で)は人数の確約にしない。ただし0人(0人で等)は
        # 「誰もいない」という数量の主張であり確約として残す(既存の回帰を維持)。
        # 既存試験:「参加者は0人で、詳細は不明。」は number として検出する。
        noun_after = re.match(r"^で([\u3040-\u30ff\u4e00-\u9fafA-Za-z]{1,12})", after)
        if noun_after and noun_after.group(1) in ("来場", "参加", "出席", "同行"):
            return False
        if after.startswith("で"):
            import unicodedata as _ud2

            head_norm = _ud2.normalize("NFKC", raw)
            number_head = re.match(r"^(\d+|[一二三四五六七八九十百千]+)", head_norm)
            if number_head:
                token = number_head.group(1)
                try:
                    value = int(token) if token.isdigit() else None
                except (TypeError, ValueError):
                    value = None
                if value == 0 or token in ("〇", "零"):
                    return False
            return True
        # 人数・個数の直前が数量でない慣用(約・およそ・ほぼ)は確約にしない
        # のではなく数量として残す(約30人は30人の言及)。ここでは外さない。
        # 直前の『各・毎・お・ご』は個別配分の数量であり外さない。
        _ = before
        return False

    for pattern in P0A_FACT_PATTERNS:
        kind, regex = pattern
        for match in regex.finditer(masked):
            raw = match.group(0)
            if kind == "time" and _p1_is_time_jp(raw):
                mapped = "datetime"
            else:
                mapped = {"date": "datetime", "time": "datetime",
                          "phone": "contact_phone", "amount": "number"}.get(kind, kind)
            found.append({"kind": mapped, "value": raw,
                          "location": _snippet(match.start(), match.end())})
    # P1-1: 年齢条件は原本外の確約として検出する(例: 20歳以上・年齢制限なし)。
    for match in _P0A_AGE_COUNT_RE.finditer(masked):
        raw = match.group(0)
        if not any(row["value"] == raw for row in found):
            found.append({"kind": "age", "value": raw,
                          "location": _snippet(match.start(), match.end())})
    for match in _P0A_AGE_WORD_RE.finditer(masked):
        raw = match.group(0)
        if not any(row["value"] == raw for row in found):
            found.append({"kind": "age", "value": raw,
                          "location": _snippet(match.start(), match.end())})
    for match in _P0A_EMAIL_RE.finditer(masked):
        found.append({"kind": "contact_email", "value": match.group(0),
                      "location": _snippet(match.start(), match.end())})
    for match in _P0C_JP_TIME_FIND_RE.finditer(masked):
        raw = match.group(0)
        if not any(row["value"] == raw for row in found):
            found.append({"kind": "datetime", "value": raw,
                          "location": _snippet(match.start(), match.end())})
    time_spans = [match.span() for match in _P0C_JP_TIME_FIND_RE.finditer(masked)]
    for match in _P0A_NUMBER_UNIT_RE.finditer(masked):
        # 「10時00分」「午後3時00分」の末尾NN分は時刻の一部であり数量ではない。
        if any(start <= match.start() and match.end() <= end for start, end in time_spans):
            continue
        if _is_non_quantity(match):
            continue
        found.append({"kind": "number", "value": match.group(0),
                      "location": _snippet(match.start(), match.end())})
    for match in _P0A_PROPER_AFFIX_RE.finditer(masked):
        raw = match.group(0)
        # P1-1: 普通名詞の一部(〜部)や汎用敬称は固有名にしない。
        if _P0C_COMMON_AFFIX_RE.match(raw):
            continue
        if raw in _P0C_GENERIC_HONORIFICS or any(
                raw == honor or raw.startswith(honor) or raw.endswith(honor)
                for honor in _P0C_GENERIC_HONORIFICS):
            continue
        found.append({"kind": "proper", "value": raw,
                      "location": _snippet(match.start(), match.end())})
    for match in _P0A_AMBIGUOUS_NAME_RE.finditer(masked):
        raw = match.group(1)
        if any(row["value"] == raw for row in found):
            continue
        scoped.append({"kind": "proper", "value": raw,
                       "location": _snippet(match.start(), match.end()),
                       "reason": "敬称・組織接辞がなく決定的に抽出できない固有名の可能性: 検査範囲外。要確認"})
    found.sort(key=lambda row: (str(row.get("kind") or ""), str(row.get("value") or "")))
    scoped.sort(key=lambda row: str(row.get("value") or ""))
    return found, scoped


def check_p0a_unsupported_values(plan_text: str, registered_text: str) -> dict:
    """原本外の値を決定的照合で列挙し、仮説/要確認として記録する材料を返す。

    完全一致・正規化一致(全角/半角・空白・日付表記ゆれ)に限り、文意の推測はしない。
    LLM出力の『出典あり』等の自己申告フラグは入力として読まない。
    出典なしは拒否せず unverified_assumptions に残す(計画本文は消さないが確定扱いにしない)。
    出典の読み取り不能は checked=False・passed=False(合格にしない)。失敗時に代替値で続行しない。
    """
    if not isinstance(registered_text, str) or not registered_text.strip():
        return {"version": P0A_UNSUPPORTED_VERSION, "checked": False,
                "passed": False, "checkable": False,
                "source_readable": False,
                "reason": "source_unreadable: registered_text is missing",
                "unsupported_values": [], "unverified_assumptions": [],
                "out_of_scope": [], "checked_kinds": ["datetime", "contact", "number", "proper"]}
    if not isinstance(plan_text, str) or not plan_text.strip():
        return {"version": P0A_UNSUPPORTED_VERSION, "checked": False,
                "passed": False, "checkable": False,
                "source_readable": True,
                "reason": "plan_unreadable: plan_text is missing",
                "unsupported_values": [], "unverified_assumptions": [],
                "out_of_scope": [], "checked_kinds": ["datetime", "contact", "number", "proper"]}
    plan_found, plan_scoped = _p0a_unsupported_candidates(plan_text)
    source_found, _ = _p0a_unsupported_candidates(registered_text)
    source_canons: set[tuple[str, str]] = set()
    for row in source_found:
        source_canons.add((str(row.get("kind") or ""),
                           _p0a_unsupported_canon(str(row.get("kind") or ""),
                                                  str(row.get("value") or ""))))
    unsupported: list[dict] = []
    for row in plan_found:
        kind = str(row.get("kind") or "")
        value = str(row.get("value") or "")
        if (kind, _p0a_unsupported_canon(kind, value)) in source_canons:
            continue
        unsupported.append({"kind": kind, "value": value,
                            "location": str(row.get("location") or "")})
    unsupported.sort(key=lambda row: (str(row.get("kind") or ""), str(row.get("value") or "")))
    assumptions = [{"kind": row["kind"], "value": row["value"],
                    "location": row["location"],
                    "reason": "出典なし: 確定事項にしない。要確認"}
                   for row in unsupported]
    scoped = [{"kind": row["kind"], "value": row["value"],
               "location": row["location"], "reason": row["reason"]}
              for row in plan_scoped]
    passed = not scoped and not unsupported
    reason = ("out_of_scope_and_unsupported_values_present" if scoped and unsupported else
              "out_of_scope_present" if scoped else
              "unsupported_values_present" if unsupported else "")
    return {"version": P0A_UNSUPPORTED_VERSION, "checked": True,
            "passed": passed, "checkable": True,
            "source_readable": True,
            "reason": reason,
            "unsupported_values": unsupported,
            "unverified_assumptions": assumptions,
            "out_of_scope": scoped,
            "checked_kinds": ["datetime", "contact", "number", "proper"]}


def count_p0a_required_files(criterion_text: str) -> int:
    """達成条件に明示された独立ファイル数だけ数える。内容項目の件数は数えない。

    P0-0: 内訳と明示合計を別々に集計する正規化に委譲する。先行する
    『1ファイル』だけで合計を1件にしない。欠落・None・不正値・矛盾の
    ときは1に倒さず ValueError(判定不能)にする。呼び出し側は保存しない。
    """
    norm = normalize_p0a_artifact_requirement(criterion_text)
    # consistent=True のとき required は必ず整数。破れたら判定不能として拒否する。
    # 矛盾(consistent=False)のときもここでは件数を返さず拒否する。
    return _p0a_required_or_raise(norm, where="count_p0a_required_files")


def check_p0a_separate_artifacts(criteria: list[str], tasks: list[dict]) -> dict:
    """2つ以上の別ファイルを要求する達成条件の分離検査。保存前の不合格用。

    P0-0: 正規化したスロットごとに別パス・別検証見出し集合を検査する。
    判定は達成条件文から抽出したスロット列と、計画案の outputs の対応で行う。
    1ファイル統合は不合格。別ファイル・別の検証手順を持つ計画案は合格。
    内訳と合計が矛盾する達成条件は保存しない(判定不能を1件に倒さない)。
    未確認・不明は合格にしない。
    """
    issues: list[str] = []
    slot_rows: list[dict] = []
    criteria = list(criteria or [])
    tasks = list(tasks or [])
    for index, criterion in enumerate(criteria, 1):
        norm = normalize_p0a_artifact_requirement(criterion)
        if not norm.get("consistent"):
            conflict = norm.get("conflict") or {}
            issues.append(
                f"SC{index:02d}: artifact requirement is contradictory "
                f"({conflict.get('reason', 'mismatch')}); refusing to save")
            slot_rows.append({"criterion_id": f"SC{index:02d}",
                              "consistent": False,
                              "slots": [],
                              "conflict": conflict})
            continue
        # consistent=True のとき required は必ず整数。破れたら判定不能として拒否する。
        required = _p0a_required_or_raise(norm, where="check_p0a_separate_artifacts")
        slots = build_p0a_artifact_slots(criterion)
        slot_rows.append({"criterion_id": f"SC{index:02d}",
                          "consistent": True,
                          "slots": slots,
                          "conflict": None})
        if required < 2:
            continue
        key = f"SC{index:02d}"
        paths: list[str] = []
        heading_sets: list[tuple] = []
        for task in tasks:
            if not isinstance(task, dict):
                continue
            contract = contract_of(task)
            if not contract:
                continue
            # 最終検証・準備工程は集約・記録であり、達成条件の別ファイル成果物に数えない。
            if contract.get("final_verification"):
                continue
            if str(contract.get("role") or "") in ("preparation", "final_verification"):
                continue
            cids = list(contract.get("criterion_ids") or [])
            if key not in cids:
                continue
            for output in contract.get("outputs") or []:
                if not isinstance(output, dict):
                    continue
                path = str(output.get("path") or "")
                if not path:
                    continue
                if path not in paths:
                    paths.append(path)
                    headings = tuple(sorted(str(h) for h in
                                            (output.get("required_headings") or [])
                                            if str(h)))
                    heading_sets.append(headings)
        paths.sort()
        if len(paths) < required:
            missing = [slot.get("slot_id") for slot in slots[len(paths):]] or [
                f"slot-{i:02d}" for i in range(len(paths) + 1, required + 1)]
            issues.append(
                f"{key}: requires {required} separate files but plan has {len(paths)} "
                f"(missing={','.join(missing)})")
            continue
        # 別の検証手順(見出し集合が同一のまま複写は不合格)。
        if len(heading_sets) >= 2 and len(set(heading_sets)) == 1:
            issues.append(f"{key}: outputs share identical verification headings")
    issues.sort()
    return {"version": P0A_ARTIFACT_VERSION,
            "passed": not issues,
            "issues": issues,
            "slots": slot_rows}


P0A_ITEM_REGEN_MAX = 2
P0A_ITEM_VERSION = "p0a-item-contract-v1"
P0A_INPUT_VERSION = "p0a-planning-input-v1"


def ensure_p0a_artifact_slots(
    criteria: list[str],
    tasks: list[dict],
    regenerate_fn=None,
    max_attempts: int = P0A_SLOT_REGEN_MAX,
) -> dict:
    """不足スロットを明示して最大2回だけ限定再生成する。

    モデル出力の成果物が正規化スロット数より少ない(1件に統合など)場合は、
    不足スロットIDを添えて regenerate_fn(missing_slots) を呼ぶ。回数は小さく
    固定(既定2回)。それでも不一致なら saved=False で保存しない(保存拒否を維持)。
    再生成の回数・結果をログ/応答用の dict で返す。矛盾する達成条件は
    再生成せず saved=False。
    """
    try:
        attempts = max(0, int(max_attempts))
    except (TypeError, ValueError):
        attempts = P0A_SLOT_REGEN_MAX
    attempts = min(attempts, P0A_SLOT_REGEN_MAX)
    log: list[dict] = []
    current = list(tasks or [])
    check = check_p0a_separate_artifacts(criteria, current)
    if check.get("passed"):
        return {"saved": True, "attempts": 0,
                "attempts_used": 0,
                "log": [{"attempt": 0, "result": "passed",
                         "missing": []}],
                "tasks": current, "check": check}
    if any(not str(issue).startswith("SC") or "contradictory" in str(issue)
           for issue in check.get("issues") or []):
        # 矛盾する達成条件は再生成せず保存しない。
        if not any("contradictory" in str(issue) for issue in check.get("issues") or []):
            pass
        else:
            return {"saved": False, "attempts": 0,
                    "attempts_used": 0,
                    "log": [{"attempt": 0, "result": "contradiction",
                             "missing": list(check.get("issues") or [])}],
                    "tasks": current, "check": check}
    if regenerate_fn is None:
        return {"saved": False, "attempts": 0,
                "attempts_used": 0,
                "log": [{"attempt": 0, "result": "missing_slots_no_regenerator",
                         "missing": list(check.get("issues") or [])}],
                "tasks": current, "check": check}
    missing_overall: list[str] = []
    for issue in check.get("issues") or []:
        match = re.search(r"missing=([A-Za-z0-9\-,_]+)", str(issue))
        if match:
            missing_overall.extend(part for part in match.group(1).split(",") if part)
    missing_overall = list(dict.fromkeys(missing_overall)) or ["slot-missing"]
    for attempt in range(1, attempts + 1):
        try:
            produced = regenerate_fn(list(missing_overall))
        except Exception as exc:  # noqa: BLE001 - 失敗は記録し判定は『確認できず』にする
            log.append({"attempt": attempt, "result": "regenerator_error",
                        "missing": list(missing_overall),
                        "error": type(exc).__name__})
            break
        if isinstance(produced, dict) and isinstance(produced.get("tasks"), list):
            current = list(produced["tasks"])
        elif isinstance(produced, list):
            current = list(produced)
        else:
            log.append({"attempt": attempt, "result": "regenerator_invalid",
                        "missing": list(missing_overall)})
            break
        check = check_p0a_separate_artifacts(criteria, current)
        if check.get("passed"):
            log.append({"attempt": attempt, "result": "passed",
                        "missing": list(missing_overall)})
            return {"saved": True, "attempts": attempt,
                    "attempts_used": attempt,
                    "log": log,
                    "tasks": current, "check": check}
        log.append({"attempt": attempt, "result": "still_missing",
                    "missing": list(check.get("issues") or [])})
        missing_overall = []
        for issue in check.get("issues") or []:
            match = re.search(r"missing=([A-Za-z0-9\-,_]+)", str(issue))
            if match:
                missing_overall.extend(part for part in match.group(1).split(",") if part)
        missing_overall = list(dict.fromkeys(missing_overall)) or ["slot-missing"]
    return {"saved": False, "attempts": len(log),
            "attempts_used": len(log),
            "log": log,
            "tasks": current, "check": check}


def validate_p0a_item_contract(plan: dict) -> dict:
    """P0-A 保存前検査。

    要求文自体の必須語句化・成果物スロット(item の対象パス)の取り違えを保存前に
    不合格にする。既存の具体的拒否(unknown/undefined/empty/zero/doubts)は
    validate_requirement_matrix で先に検査済みであり、ここでは P0-A 固有の検査
    だけを行う(検査の順序を守り、既存の拒否理由を隠さない)。
    文意項目(semantic_review_required)の挙動は変えない。
    """
    from app.requirement_matrix import p0a_contract_issues
    issues: list[str] = []
    tasks = plan.get("tasks") or []
    if not isinstance(tasks, list):
        return {"version": P0A_ITEM_VERSION, "passed": False,
                "issues": ["plan tasks missing"],
                "item_target": {"attempts_used": 0, "regenerated": False, "log": []}}
    for task in tasks:
        if not isinstance(task, dict):
            continue
        contract = contract_of(task)
        if not contract or contract.get("final_verification"):
            continue
        if str(contract.get("execution_kind") or "").startswith("vehicle_"):
            continue
        if not list(contract.get("criterion_ids") or []):
            continue
        criterion = str(contract.get("criterion") or "")
        matrix = contract.get("requirement_matrix")
        if not isinstance(matrix, list):
            continue
        outputs = [output for output in (contract.get("outputs") or [])
                   if isinstance(output, dict)
                   and str(output.get("path") or "").lower().endswith(".md")]
        for issue in p0a_contract_issues(criterion, matrix, outputs):
            issues.append(f"{task.get('task_key')}: {issue}")
    issues.sort()
    return {"version": P0A_ITEM_VERSION, "passed": not issues,
            "issues": issues,
            "item_target": {"attempts_used": 0, "regenerated": False, "log": []}}


def normalize_p0a_plan_items(criteria: list[str], tasks: list[dict],
                             registered_text: str | None) -> dict:
    """計画案の全 SC に P0-A 正規化を適用する。規則で LLM の指定を上書きする。"""
    from app.requirement_matrix import normalize_p0a_items
    log: list[dict] = []
    current = list(tasks or [])
    for task in current:
        if not isinstance(task, dict):
            continue
        contract = contract_of(task)
        if not contract or not contract.get("criterion_ids"):
            continue
        criterion = str(contract.get("criterion") or "")
        matrix = contract.get("requirement_matrix")
        if not isinstance(matrix, list):
            continue
        outputs = [output for output in (contract.get("outputs") or [])
                   if isinstance(output, dict)
                   and str(output.get("path") or "").lower().endswith(".md")]
        fixed, report = normalize_p0a_items(
            str((contract.get("criterion_ids") or [""])[0]),
            criterion, matrix, outputs, registered_text)
        contract["requirement_matrix"] = fixed
        entry = {"task_key": str(task.get("task_key") or "")}
        entry.update(report if isinstance(report, dict) else {})
        log.append(entry)
        task["acceptance_criteria"] = json.dumps(contract, ensure_ascii=False)
    return {"tasks": current, "log": log}


def ensure_p0a_item_target_consistency(
    criteria: list[str],
    tasks: list[dict],
    regenerate_fn=None,
    max_attempts: int = P0A_ITEM_REGEN_MAX,
) -> dict:
    """item の対象パス取り違えを最大2回だけ限定再生成する。2回でも不一致なら保存しない。"""
    try:
        attempts = max(0, int(max_attempts))
    except (TypeError, ValueError):
        attempts = P0A_ITEM_REGEN_MAX
    attempts = min(attempts, P0A_ITEM_REGEN_MAX)
    log: list[dict] = []
    current = list(tasks or [])
    plan = {"tasks": current}
    check = validate_p0a_item_contract(plan)
    if check.get("passed"):
        return {"saved": True, "attempts": 0, "attempts_used": 0,
                "log": [{"attempt": 0, "result": "passed", "missing": []}],
                "tasks": current, "check": check}
    # P0-A 固有の検査だけが残り、他に合格を阻む既知の拒否が無いときだけ
    # wrong_target の限定再生成を行う。要求文の語句化は除去でなく拒否の対象
    # (除去すると何が悪かったかが残らないため)。それ以外は再生成せず拒否する。
    separable = [issue for issue in check.get("issues") or []
                 if "wrong_target" in str(issue)
                 or "requirement sentence must not be a keyword" in str(issue)]
    only_separable = bool(separable) and len(separable) == len(check.get("issues") or [])
    if not only_separable or regenerate_fn is None:
        return {"saved": False, "attempts": 0, "attempts_used": 0,
                "log": [{"attempt": 0, "result": "p0a_contract_failed_no_regen",
                         "missing": list(check.get("issues") or [])}],
                "tasks": current, "check": check}
    missing_overall: list[str] = []
    for issue in separable:
        match = re.match(r"(SC\d+): (?:wrong_target: (SC\d+-R\d+)|requirement sentence must not be a keyword: (SC\d+-R\d+))", str(issue))
        if match:
            missing_overall.append(match.group(2) or match.group(3))
    missing_overall = list(dict.fromkeys(missing_overall)) or ["item-target-missing"]
    for attempt in range(1, attempts + 1):
        try:
            produced = regenerate_fn(list(missing_overall))
        except Exception as exc:  # noqa: BLE001 - 失敗は記録し判定は『確認できず』にする
            log.append({"attempt": attempt, "result": "regenerator_error",
                        "missing": list(missing_overall),
                        "error": type(exc).__name__})
            break
        if isinstance(produced, dict) and isinstance(produced.get("tasks"), list):
            current = list(produced["tasks"])
        elif isinstance(produced, list):
            current = list(produced)
        else:
            log.append({"attempt": attempt, "result": "regenerator_invalid",
                        "missing": list(missing_overall)})
            break
        plan = {"tasks": current}
        check = validate_p0a_item_contract(plan)
        if check.get("passed"):
            log.append({"attempt": attempt, "result": "passed",
                        "missing": list(missing_overall)})
            return {"saved": True, "attempts": attempt,
                    "attempts_used": attempt, "log": log,
                    "tasks": current, "check": check}
        log.append({"attempt": attempt, "result": "still_missing",
                    "missing": list(check.get("issues") or [])})
        missing_overall = []
        for issue in check.get("issues") or []:
            match = re.match(r"(?:SC\d+): (?:wrong_target: (SC\d+-R\d+)|requirement sentence must not be a keyword: (SC\d+-R\d+))", str(issue))
            if match:
                missing_overall.append(match.group(1) or match.group(2))
        missing_overall = list(dict.fromkeys(missing_overall)) or ["item-target-missing"]
    return {"saved": False, "attempts": len(log), "attempts_used": len(log),
            "log": log, "tasks": current, "check": check}


def build_p0a_planning_input(*, goal: str = "", success_criteria: str = "",
                             constraints_text: str = "", static_body: str = "",
                             source_files=None, per_item_limit: int = 2000,
                             total_limit: int = 6000) -> dict:
    """ローカル計画の入力を出典・長さ制限つきで束ねる。原本本文の外部転送はしない。

    各項目と全体に上限を設け、上限を超えた場合は切り、切ったことを記録する。
    出典のない値は補完しない。決定的・順序安定。戻り値は辞書のみ。
    """
    try:
        per_item = max(0, int(per_item_limit))
    except (TypeError, ValueError):
        per_item = 2000
    try:
        total = max(0, int(total_limit))
    except (TypeError, ValueError):
        total = 6000
    items: list[dict] = []

    def _clip(text: str, limit: int) -> tuple[str, bool]:
        blob = str(text or "")
        if limit <= 0:
            return "", bool(blob)
        if len(blob) <= limit:
            return blob, False
        return blob[:limit], True

    for source, label in ((goal, "goal"), (success_criteria, "success_criteria"),
                          (constraints_text, "constraints"),
                          (static_body, "registered_context")):
        blob = str(source or "")
        if not blob.strip():
            continue
        clipped, truncated = _clip(blob, per_item)
        items.append({"source": label, "text": clipped,
                      "truncated": bool(truncated),
                      "chars": len(blob), "kept": len(clipped)})
    files = list(source_files or [])
    for position, item in enumerate(files):
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "")
        if not content.strip():
            continue
        filename = str(item.get("filename") or f"source-{position}")
        clipped, truncated = _clip(content, per_item)
        items.append({"source": f"registered_file:{filename}",
                      "text": clipped, "truncated": bool(truncated),
                      "chars": len(content), "kept": len(clipped)})
    parts: list[str] = []
    used = 0
    truncated_any = False
    for entry in items:
        header = f"\n\n[出典:{entry['source']}]\n"
        body = str(entry.get("text") or "")
        if used + len(header) >= total:
            truncated_any = True
            entry["truncated"] = True
            continue
        room = total - used - len(header)
        if len(body) > room:
            body = body[:room]
            entry["text"] = body
            entry["kept"] = len(body)
            entry["truncated"] = True
            truncated_any = True
        parts.append(header + body)
        used += len(header) + len(body)
        if bool(entry.get("truncated")):
            truncated_any = True
    text = "".join(parts)[:total]
    return {"version": P0A_INPUT_VERSION, "text": text,
            "truncated": bool(truncated_any),
            "items": items, "chars": len(text)}


def build_p0a_registered_text(*, goal: str = "", success_criteria: str = "",
                              constraints_text: str = "", static_body: str = "",
                              source_files=None) -> str:
    """事実照合用の登録テキスト。出典の区別なく全文を結合する(照合専用)。"""
    chunks = [str(goal or ""), str(success_criteria or ""),
              str(constraints_text or ""), str(static_body or "")]
    for item in list(source_files or []):
        if isinstance(item, dict):
            content = str(item.get("content") or "")
            if content.strip():
                chunks.append(content)
    return "\n".join(chunk for chunk in chunks if str(chunk).strip())


# P0-C: 成果物本文の根拠確認用の登録テキスト抽出補助(汎用・決定的)。
# 生成メモ(memo)は登録事実に含めない。特定案件の語彙を持たない。
def build_p0c_registered_text(mission: dict | None = None,
                              context_files: list[dict] | None = None,
                              static_body: str = "") -> str:
    """保存後の成果物本文と照合する登録テキスト。原本・目標・制約のみを使う。"""
    mission = mission if isinstance(mission, dict) else {}
    files: list[dict] = []
    for item in list(context_files or []):
        if not isinstance(item, dict):
            continue
        if str(item.get("source") or "") == "memo":
            continue
        if not str(item.get("content") or "").strip():
            continue
        files.append(item)
    return build_p0a_registered_text(
        goal=str(mission.get("goal") or ""),
        success_criteria=str(mission.get("success_criteria") or ""),
        constraints_text=str(mission.get("constraints_text") or ""),
        static_body=str(static_body or ""),
        source_files=files,
    )
