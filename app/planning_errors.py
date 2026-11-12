"""P0-4 local planning failure normalization.

Pure, deterministic helpers only. No state read or write.
No side effects. No broad handlers. Narrow handlers only.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass


CODE_TITLE_SCOPE_MISSING = "title_scope_missing"
CODE_REQUIRED_OUTPUT_UNKNOWN = "required_output_unknown"
CODE_ARTIFACT_CONTRADICTORY = "artifact_requirement_contradictory"
CODE_PATH_COUNT_MISMATCH = "required_path_count_mismatch"
CODE_HEADINGS_INVALID = "headings_invalid"
CODE_PROPOSAL_TOO_LONG = "proposal_too_long"
CODE_SCOPE_FORBIDDEN = "scope_forbidden_content"
CODE_MARKDOWN_COUNT = "markdown_count_mismatch"
CODE_OUTPUTS_TOO_MANY = "outputs_too_many"
CODE_OUTPUTS_NOT_ARRAY = "outputs_not_array"
CODE_OUTPUT_PATH_NOT_EXPLICIT = "outputs_path_not_explicit"
CODE_CSV_CONTRACT_MISSING = "csv_contract_missing"
CODE_OUTPUT_PATH_DUPLICATE = "outputs_path_duplicate"
CODE_REQUIRED_HEADINGS_INVALID = "required_headings_invalid"
CODE_OUTPUT_PATH_FORMAT = "outputs_path_format_invalid"
CODE_OUTPUTS_PATHS_MISMATCH = "outputs_paths_mismatch"
CODE_MISSING_CHECKLIST = "missing_checklist_output"
CODE_MISSING_PROCEDURE = "missing_procedure_output"
CODE_DUP_CHECKLIST = "duplicate_checklist_heading"
CODE_DUP_PROCEDURE = "duplicate_procedure_heading"
CODE_EXIT_CHECKS_INVALID = "exit_checks_invalid"
CODE_ARTIFACT_UNDETERMINED = "artifact_undetermined"
CODE_ARTIFACT_INVARIANT = "artifact_invariant_broken"
CODE_DUPLICATE_TITLE = "duplicate_title"
CODE_UNKNOWN = "unknown"


@dataclass(frozen=True)
class PlanningError:
    code: str
    output_index: int | None
    field: str | None
    expected: str
    observed: str


_EXPECTED: dict[str, str] = {
    CODE_TITLE_SCOPE_MISSING: "title and scope are required",
    CODE_REQUIRED_OUTPUT_UNKNOWN: "explicit required path is needed",
    CODE_ARTIFACT_CONTRADICTORY: "artifact count must be consistent",
    CODE_PATH_COUNT_MISMATCH: "path count must match artifact count",
    CODE_HEADINGS_INVALID: "headings must be 2-8 items",
    CODE_PROPOSAL_TOO_LONG: "proposal must fit length limits",
    CODE_SCOPE_FORBIDDEN: "scope must describe content only",
    CODE_MARKDOWN_COUNT: "markdown count must match criterion",
    CODE_OUTPUTS_TOO_MANY: "at most 4 contracts",
    CODE_OUTPUTS_NOT_ARRAY: "outputs must be an array",
    CODE_OUTPUT_PATH_NOT_EXPLICIT: "path must equal required path",
    CODE_CSV_CONTRACT_MISSING: "csv needs table contract",
    CODE_OUTPUT_PATH_DUPLICATE: "paths must be unique",
    CODE_REQUIRED_HEADINGS_INVALID: "required headings must be 2-8 items",
    CODE_OUTPUT_PATH_FORMAT: "path must be result sc form",
    CODE_OUTPUTS_PATHS_MISMATCH: "outputs must match required paths",
    CODE_MISSING_CHECKLIST: "separate checklist is required",
    CODE_MISSING_PROCEDURE: "separate procedure is required",
    CODE_DUP_CHECKLIST: "checklist headings must be separate",
    CODE_DUP_PROCEDURE: "procedure headings must be separate",
    CODE_EXIT_CHECKS_INVALID: "exit checks must be 1-8 items",
    CODE_ARTIFACT_UNDETERMINED: "artifact count must be determined",
    CODE_ARTIFACT_INVARIANT: "artifact count must be valid",
    CODE_DUPLICATE_TITLE: "title must be unique",
    CODE_UNKNOWN: "format and constraints must be reviewed",
}

_FIELD: dict[str, str | None] = {
    CODE_TITLE_SCOPE_MISSING: "title",
    CODE_REQUIRED_OUTPUT_UNKNOWN: "criterion",
    CODE_ARTIFACT_CONTRADICTORY: "artifact_requirement",
    CODE_PATH_COUNT_MISMATCH: "artifact_requirement",
    CODE_HEADINGS_INVALID: "headings",
    CODE_PROPOSAL_TOO_LONG: "scope",
    CODE_SCOPE_FORBIDDEN: "scope",
    CODE_MARKDOWN_COUNT: "outputs",
    CODE_OUTPUTS_TOO_MANY: "outputs",
    CODE_OUTPUTS_NOT_ARRAY: "outputs",
    CODE_OUTPUT_PATH_NOT_EXPLICIT: "outputs.path",
    CODE_CSV_CONTRACT_MISSING: "outputs.path",
    CODE_OUTPUT_PATH_DUPLICATE: "outputs.path",
    CODE_REQUIRED_HEADINGS_INVALID: "outputs.required_headings",
    CODE_OUTPUT_PATH_FORMAT: "outputs.path",
    CODE_OUTPUTS_PATHS_MISMATCH: "outputs",
    CODE_MISSING_CHECKLIST: "outputs",
    CODE_MISSING_PROCEDURE: "outputs",
    CODE_DUP_CHECKLIST: "outputs.required_headings",
    CODE_DUP_PROCEDURE: "outputs.required_headings",
    CODE_EXIT_CHECKS_INVALID: "exit_checks",
    CODE_ARTIFACT_UNDETERMINED: "artifact_requirement",
    CODE_ARTIFACT_INVARIANT: "artifact_requirement",
    CODE_DUPLICATE_TITLE: "title",
    CODE_UNKNOWN: None,
}

_OBSERVED: dict[str, str] = {
    CODE_TITLE_SCOPE_MISSING: "missing",
    CODE_REQUIRED_OUTPUT_UNKNOWN: "undetermined",
    CODE_ARTIFACT_CONTRADICTORY: "contradictory",
    CODE_PATH_COUNT_MISMATCH: "count_mismatch",
    CODE_HEADINGS_INVALID: "invalid",
    CODE_PROPOSAL_TOO_LONG: "too_long",
    CODE_SCOPE_FORBIDDEN: "forbidden_content",
    CODE_MARKDOWN_COUNT: "count_mismatch",
    CODE_OUTPUTS_TOO_MANY: "too_many",
    CODE_OUTPUTS_NOT_ARRAY: "invalid_type",
    CODE_OUTPUT_PATH_NOT_EXPLICIT: "not_explicit",
    CODE_CSV_CONTRACT_MISSING: "missing_table_contract",
    CODE_OUTPUT_PATH_DUPLICATE: "duplicate",
    CODE_REQUIRED_HEADINGS_INVALID: "invalid",
    CODE_OUTPUT_PATH_FORMAT: "invalid_format",
    CODE_OUTPUTS_PATHS_MISMATCH: "mismatch",
    CODE_MISSING_CHECKLIST: "missing",
    CODE_MISSING_PROCEDURE: "missing",
    CODE_DUP_CHECKLIST: "duplicate",
    CODE_DUP_PROCEDURE: "duplicate",
    CODE_EXIT_CHECKS_INVALID: "invalid",
    CODE_ARTIFACT_UNDETERMINED: "undetermined",
    CODE_ARTIFACT_INVARIANT: "invariant_broken",
    CODE_DUPLICATE_TITLE: "duplicate",
    CODE_UNKNOWN: "unknown",
}

_FIX: dict[str, str] = {
    CODE_TITLE_SCOPE_MISSING: "題名と内容設計を1文字以上の文字列で入れ直す。",
    CODE_REQUIRED_OUTPUT_UNKNOWN: "達成条件の成果物パス指定が不確定なため、明示パスに書き直してから再試行する。",
    CODE_ARTIFACT_CONTRADICTORY: "達成条件の件数指定の矛盾を解消し、内訳と合計を一致させてから再試行する。",
    CODE_PATH_COUNT_MISMATCH: "指定パス数と成果物数指定を一致させ、件数を合わせる。",
    CODE_HEADINGS_INVALID: "見出しを2件以上8件以内で入れ直し、空文字や100文字超を避ける。",
    CODE_PROPOSAL_TOO_LONG: "題名は120文字以内、内容設計は1600文字以内に短縮する。",
    CODE_SCOPE_FORBIDDEN: "内容設計から参照記法や操作名を除き、内容説明だけにする。",
    CODE_MARKDOWN_COUNT: "指定された点数どおりにMarkdownの件数を合わせる。",
    CODE_OUTPUTS_TOO_MANY: "出力契約は4件以内に減らす。",
    CODE_OUTPUTS_NOT_ARRAY: "出力契約の配列で返す。",
    CODE_OUTPUT_PATH_NOT_EXPLICIT: "出力パスを必須の明示パスと完全一致させる。",
    CODE_CSV_CONTRACT_MISSING: "表出力には必須列と最小行数の表契約を付ける。",
    CODE_OUTPUT_PATH_DUPLICATE: "出力パスの重複を解消し、1パス1契約にする。",
    CODE_REQUIRED_HEADINGS_INVALID: "各出力の必須見出しを2件以上8件以内で入れ直す。",
    CODE_OUTPUT_PATH_FORMAT: "出力パスを定型の形式に直す。",
    CODE_OUTPUTS_PATHS_MISMATCH: "出力を必須パス一覧と順序まで完全一致させる。",
    CODE_MISSING_CHECKLIST: "チェックリスト出力を別契約として追加する。",
    CODE_MISSING_PROCEDURE: "手順書出力を別契約として追加する。",
    CODE_DUP_CHECKLIST: "文書側の見出しからチェックリストの見出しを除き、チェックリスト出力にだけ置く。",
    CODE_DUP_PROCEDURE: "文書側の見出しから手順の見出しを除き、手順書出力にだけ置く。",
    CODE_EXIT_CHECKS_INVALID: "終了検査を1件以上8件以内の非空文字で入れ直す。",
    CODE_ARTIFACT_UNDETERMINED: "成果物数指定が不確定なため、件数表現を明確にしてから再試行する。",
    CODE_ARTIFACT_INVARIANT: "成果物数正規化の不変条件が壊れたため、件数指定を見直して再試行する。",
    CODE_DUPLICATE_TITLE: "先行タスクと重複しない題名に変え、今回の達成条件固有の題名にする。",
    CODE_UNKNOWN: "JSON形式と制約を再確認して修正する。",
}


def _extract_output_index(text: str) -> int | None:
    match = re.search(r"outputs\[(\d+)\]", text)
    if match is None:
        return None
    try:
        value = int(match.group(1))
    except (ValueError, TypeError, AttributeError):
        return None
    if 0 <= value <= 64:
        return value
    return None


def _extract_markdown_count(text: str) -> str:
    match = re.search(r"goal requires (\d+) Markdown", text)
    if match is None:
        return _OBSERVED[CODE_MARKDOWN_COUNT]
    try:
        value = int(match.group(1))
    except (ValueError, TypeError, AttributeError):
        return _OBSERVED[CODE_MARKDOWN_COUNT]
    if 1 <= value <= 9:
        return str(value)
    return _OBSERVED[CODE_MARKDOWN_COUNT]


def normalize_planning_error(exc_or_text: object) -> PlanningError:
    if isinstance(exc_or_text, str):
        text = exc_or_text
    else:
        try:
            text = str(exc_or_text)
        except (ValueError, TypeError, AttributeError):
            text = ""
    if not isinstance(text, str):
        text = ""
    blob = text[:2000]
    code = CODE_UNKNOWN
    if "title and scope are required strings" in blob:
        code = CODE_TITLE_SCOPE_MISSING
    elif "required_output_path_unknown" in blob:
        code = CODE_REQUIRED_OUTPUT_UNKNOWN
    elif "artifact requirement is contradictory" in blob:
        code = CODE_ARTIFACT_CONTRADICTORY
    elif "required paths" in blob and "conflict with artifact count" in blob:
        code = CODE_PATH_COUNT_MISMATCH
    elif "outputs[].required_headings must contain 2-8 nonempty strings" in blob:
        code = CODE_REQUIRED_HEADINGS_INVALID
    elif "headings must contain 2-8 nonempty strings" in blob:
        code = CODE_HEADINGS_INVALID
    elif "proposal too long" in blob:
        code = CODE_PROPOSAL_TOO_LONG
    elif "scope must describe content" in blob:
        code = CODE_SCOPE_FORBIDDEN
    elif "goal requires" in blob and "Markdown output(s)" in blob:
        code = CODE_MARKDOWN_COUNT
    elif "outputs must contain at most 4 contracts" in blob:
        code = CODE_OUTPUTS_TOO_MANY
    elif "outputs must be an array of output contracts" in blob:
        code = CODE_OUTPUTS_NOT_ARRAY
    elif "outputs[].path must be an explicit required output path" in blob:
        code = CODE_OUTPUT_PATH_NOT_EXPLICIT
    elif "csv path requires table contract" in blob:
        code = CODE_CSV_CONTRACT_MISSING
    elif "outputs[].path must be unique within one task" in blob:
        code = CODE_OUTPUT_PATH_DUPLICATE
    elif "outputs[].path must be result/sc" in blob:
        code = CODE_OUTPUT_PATH_FORMAT
    elif "outputs must exactly match required paths" in blob:
        code = CODE_OUTPUTS_PATHS_MISMATCH
    elif "goal requires a separate checklist output" in blob:
        code = CODE_MISSING_CHECKLIST
    elif "goal requires a separate procedure output" in blob:
        code = CODE_MISSING_PROCEDURE
    elif "document output must not duplicate checklist headings" in blob:
        code = CODE_DUP_CHECKLIST
    elif "document output must not duplicate procedure headings" in blob:
        code = CODE_DUP_PROCEDURE
    elif "exit_checks must contain 1-8 nonempty strings" in blob:
        code = CODE_EXIT_CHECKS_INVALID
    elif "artifact requirement is undetermined" in blob:
        code = CODE_ARTIFACT_UNDETERMINED
    elif "artifact requirement invariant broken" in blob:
        code = CODE_ARTIFACT_INVARIANT
    elif "既存タスクと同じ題名です" in blob:
        code = CODE_DUPLICATE_TITLE
    else:
        code = CODE_UNKNOWN
    index = _extract_output_index(blob)
    field = _FIELD.get(code)
    expected = _EXPECTED.get(code, _EXPECTED[CODE_UNKNOWN])
    if code == CODE_MARKDOWN_COUNT:
        observed = _extract_markdown_count(blob)
    else:
        observed = _OBSERVED.get(code, _OBSERVED[CODE_UNKNOWN])
    return PlanningError(
        code=code,
        output_index=index,
        field=field,
        expected=expected,
        observed=observed,
    )


def fix_instruction(err: PlanningError) -> str:
    try:
        code = err.code
    except (ValueError, TypeError, AttributeError):
        return _FIX[CODE_UNKNOWN]
    if not isinstance(code, str):
        return _FIX[CODE_UNKNOWN]
    result = _FIX.get(code)
    if isinstance(result, str) and result:
        return result
    return _FIX[CODE_UNKNOWN]


def candidate_hash(proposal: dict) -> str:
    if not isinstance(proposal, dict):
        try:
            raw_fallback = json.dumps(
                {"empty": True},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        except (ValueError, TypeError, AttributeError):
            raw_fallback = '{"empty":true}'
        return hashlib.sha256(raw_fallback.encode("utf-8")).hexdigest()[:16]
    try:
        raw = json.dumps(
            proposal,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except (ValueError, TypeError, AttributeError):
        raw = '{"unserializable":true}'
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
