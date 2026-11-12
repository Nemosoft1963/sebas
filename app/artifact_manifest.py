"""P1-1 成果物の正本化(manifest)。純関数中心。状態は呼び出し側が渡す。

- manifest 1件は dict として扱い、本モジュールは保存(永続化)を持たない。
- 本文そのものは manifest に保存しない(sha256 のみ)。
- 外部通信・生成モデルの呼び出し・ファイルIO・例外の握りつぶしは禁止。
- P1-1 の範囲外(計画生成・P0系)の既存動作は変えない。
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

ORIGIN_EXECUTION = "execution"
ORIGIN_MANUAL_PREVIEW = "manual_preview"
ORIGIN_UNKNOWN = "unknown"
ORIGINS = (ORIGIN_EXECUTION, ORIGIN_MANUAL_PREVIEW, ORIGIN_UNKNOWN)

VALIDATION_VALID = "valid"
VALIDATION_INVALID = "invalid"
VALIDATION_NOT_TESTED = "not_tested"
VALIDATION_UNVERIFIED = "unverified"
VALIDATION_STATES = (
    VALIDATION_VALID,
    VALIDATION_INVALID,
    VALIDATION_NOT_TESTED,
    VALIDATION_UNVERIFIED,
)

REASON_SHA_MISMATCH = "sha_mismatch"

MANIFEST_VERSION = "p11-artifact-manifest/v1"

# 意味検査で「実際に試さないと分からない」ため検査対象外にする語彙。
# これらを含む達成条件は決定的検査が確定できず not_tested になる。
NOT_TESTABLE_MARKERS = (
    "理解確認",
    "理解度確認",
    "実機操作",
    "実機確認",
    "実操作",
    "第三者確認",
    "他者確認",
    "レビュー承認",
    "承認を得る",
    "聞き取り",
    "ヒアリング",
    "面談",
    "商談",
    "顧客への送信",
    "顧客に送信",
    "送信済み",
    "契約締結",
    "受注",
    "面接",
    "口頭確認",
    "電話確認",
    "立会",
    "立合い",
    "立会い",
    "目視確認",
    "現地確認",
    "動作確認",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_hex(data: bytes) -> str:
    """本文バイト列の SHA-256 16進(小文字)。"""
    return hashlib.sha256(bytes(data)).hexdigest()


def normalize_origin(value: Any) -> str:
    text = str(value or "").strip()
    if text in ORIGINS:
        return text
    return ORIGIN_UNKNOWN


def normalize_validation_state(value: Any) -> str:
    text = str(value or "").strip()
    if text in VALIDATION_STATES:
        return text
    return VALIDATION_UNVERIFIED


def build_manifest(
    *,
    project_id: str,
    plan_signature: str,
    task_id: str,
    path: str,
    origin: str,
    content_bytes: bytes,
    source_hashes: list[str] | tuple[str, ...] | None = None,
    validation_state: str = VALIDATION_UNVERIFIED,
    created_at: str | None = None,
) -> dict:
    """本文の sha256 を計算して manifest 1件(dict)を作る。本文自体は保存しない。

    source_hashes は入力ファイル/原本の sha256 配列(無ければ空配列)。
    plan_signature / task_id が取れない箇所では呼び出し側が空文字を渡す
    (本モジュール側で推測しない)。
    """
    cleaned_hashes: list[str] = []
    for item in list(source_hashes or []):
        text = str(item or "").strip()
        if text and text not in cleaned_hashes:
            cleaned_hashes.append(text)
    return {
        "version": MANIFEST_VERSION,
        "project_id": str(project_id or ""),
        "plan_signature": str(plan_signature or ""),
        "task_id": str(task_id or ""),
        "path": str(path or ""),
        "origin": normalize_origin(origin),
        "source_hashes": cleaned_hashes,
        "artifact_sha256": sha256_hex(bytes(content_bytes or b"")),
        "validation_state": normalize_validation_state(validation_state),
        "created_at": str(created_at or "") or _utc_now_iso(),
    }


def summarize_manifest(manifest: dict | None) -> dict:
    """ZIP の MANIFEST.json / API 応答に載せる要約。本文を含まない。"""
    if not isinstance(manifest, dict) or not manifest:
        return {
            "origin": ORIGIN_UNKNOWN,
            "validation_state": VALIDATION_UNVERIFIED,
            "artifact_sha256": "",
            "plan_signature": "",
            "task_id": "",
            "draft": True,
        }
    origin = normalize_origin(manifest.get("origin"))
    state = normalize_validation_state(manifest.get("validation_state"))
    return {
        "origin": origin,
        "validation_state": state,
        "artifact_sha256": str(manifest.get("artifact_sha256") or ""),
        "plan_signature": str(manifest.get("plan_signature") or ""),
        "task_id": str(manifest.get("task_id") or ""),
        "draft": origin != ORIGIN_EXECUTION,
    }


def verify_artifact(
    manifest: dict | None, content_bytes: bytes | None
) -> dict:
    """現物バイト列と manifest の sha を突き合わせる。結果は dict で返す。

    - manifest 欠落・sha 欠落・現物欠落 → unverified。
    - sha 不一致 → invalid(理由コード sha_mismatch)。
    - 一致 → valid。validation_state は上書きせず参照用に返す。
    """
    if not isinstance(manifest, dict) or not manifest:
        return {
            "status": VALIDATION_UNVERIFIED,
            "reason": "manifest_missing",
            "expected_sha256": "",
            "actual_sha256": "",
        }
    if content_bytes is None:
        return {
            "status": VALIDATION_UNVERIFIED,
            "reason": "content_missing",
            "expected_sha256": str(manifest.get("artifact_sha256") or ""),
            "actual_sha256": "",
        }
    expected = str(manifest.get("artifact_sha256") or "").strip()
    if not expected:
        return {
            "status": VALIDATION_UNVERIFIED,
            "reason": "manifest_sha_missing",
            "expected_sha256": "",
            "actual_sha256": sha256_hex(bytes(content_bytes)),
        }
    actual = sha256_hex(bytes(content_bytes))
    if actual != expected:
        return {
            "status": VALIDATION_INVALID,
            "reason": REASON_SHA_MISMATCH,
            "expected_sha256": expected,
            "actual_sha256": actual,
        }
    return {
        "status": VALIDATION_VALID,
        "reason": "",
        "expected_sha256": expected,
        "actual_sha256": actual,
    }


def is_draft(manifest: dict | None) -> bool:
    """origin != execution なら真。manifest が無い場合も真(安全側)。"""
    if not isinstance(manifest, dict) or not manifest:
        return True
    return normalize_origin(manifest.get("origin")) != ORIGIN_EXECUTION


def counts_for_auto_pass(manifest: dict | None) -> bool:
    """origin == execution かつ validation_state == valid のときだけ真。

    manifest が無い・origin 不明・manual_preview・invalid・not_tested は偽。
    """
    if not isinstance(manifest, dict) or not manifest:
        return False
    if normalize_origin(manifest.get("origin")) != ORIGIN_EXECUTION:
        return False
    return normalize_validation_state(manifest.get("validation_state")) == VALIDATION_VALID


def semantic_checks(content_text: str, criterion: str) -> dict:
    """達成条件ごとの決定的な意味検査。生成モデルの呼び出しなし。

    - 見出し欠落・必須語句の欠落は fail の検査項目として返す。
    - 実際に試さないと分からない項目(理解確認・実機操作・第三者確認など)は
      検査せず status=not_tested の項目として返し、合格に数えない。
    - 同一入力には同一出力を返す(決定的。時刻・乱数・外部状態を使わない)。
    戻り値: {"status": "pass"|"fail"|"not_tested", "checks": [...], "reason": str}
    """
    text = str(content_text or "")
    rule = str(criterion or "")
    flat_rule = re.sub(r"\s+", " ", rule).strip()
    checks: list[dict] = []

    testable, skipped = _split_testable_markers(flat_rule)
    for marker in skipped:
        checks.append({
            "name": f"not_testable:{marker}",
            "result": VALIDATION_NOT_TESTED,
            "reason": "実際に試さないと分からない項目は検査せず合格に数えない",
        })

    headings = _extract_required_headings(testable)
    for heading in headings:
        found = _has_heading(text, heading)
        checks.append({
            "name": f"heading:{heading}",
            "result": "pass" if found else "fail",
            "reason": "" if found else f"必須見出しが無い: {heading}",
        })

    keywords = _extract_required_keywords(testable)
    for keyword in keywords:
        found = keyword in text
        checks.append({
            "name": f"keyword:{keyword}",
            "result": "pass" if found else "fail",
            "reason": "" if found else f"必須語句が無い: {keyword}",
        })

    testable_results = [
        item for item in checks if item.get("result") != VALIDATION_NOT_TESTED
    ]
    if any(item.get("result") == "fail" for item in testable_results):
        failed = [item["name"] for item in checks if item.get("result") == "fail"]
        return {
            "status": "fail",
            "checks": checks,
            "reason": "決定的検査で不備: " + ", ".join(failed[:10]),
        }
    not_tested = [
        item for item in checks
        if item.get("result") == VALIDATION_NOT_TESTED
    ]
    if not_tested:
        markers = [
            item["name"].removeprefix("not_testable:")
            for item in not_tested
        ]
        return {
            "status": VALIDATION_NOT_TESTED,
            "checks": checks,
            "reason": "未検査の語句: " + ", ".join(markers[:10]),
        }
    if not testable_results:
        return {
            "status": VALIDATION_NOT_TESTED,
            "checks": checks,
            "reason": "検査可能な決定的項目が無く not_tested とした",
        }
    return {"status": "pass", "checks": checks, "reason": ""}


def _split_testable_markers(rule: str) -> tuple[str, list[str]]:
    skipped: list[str] = []
    testable = str(rule or "")
    for marker in NOT_TESTABLE_MARKERS:
        if marker and marker in testable:
            skipped.append(marker)
    # 検査不能語句そのものは必須語句の照合対象にしない。
    for marker in skipped:
        testable = testable.replace(marker, " ")
    testable = re.sub(r"\s+", " ", testable).strip()
    return testable, skipped


def _extract_required_headings(rule: str) -> list[str]:
    headings: list[str] = []
    for match in re.finditer(r"見出し[「『\"']([^」』\"']+)[」』\"']", str(rule or "")):
        name = re.sub(r"\s+", " ", match.group(1)).strip()
        if name and name not in headings:
            headings.append(name)
    # 「## 見出し」のような記法も拾う。
    for match in re.finditer(r"#{1,6}\s*([^\n#]{1,80})", str(rule or "")):
        name = re.sub(r"\s+", " ", match.group(1)).strip()
        if name and name not in headings:
            headings.append(name)
    return headings[:20]


def _extract_required_keywords(rule: str) -> list[str]:
    keywords: list[str] = []
    patterns = [
        r"必須語句[はが:：\s]*[「『\"']([^」』\"']+)[」』\"']",
        r"キーワード[はが:：\s]*[「『\"']([^」』\"']+)[」』\"']",
        r"語句[「『\"']([^」』\"']+)[」』\"']を含む",
        r"「([^」]{1,40})」を含む",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, str(rule or "")):
            raw = re.sub(r"\s+", " ", match.group(1)).strip()
            if not raw:
                continue
            for piece in re.split(r"[、,，・/／|｜]", raw):
                word = piece.strip()
                if word and word not in keywords:
                    keywords.append(word)
    return keywords[:20]


def _has_heading(content: str, heading: str) -> bool:
    wanted = re.sub(r"\s+", " ", str(heading or "")).strip()
    if not wanted:
        return True
    for line in str(content or "").splitlines():
        match = re.match(r"^\s*#{1,6}\s+(.+?)\s*$", line)
        if not match:
            continue
        actual = re.sub(r"\s+", " ", match.group(1)).strip()
        if actual == wanted or wanted in actual:
            return True
    stripped = re.sub(r"^[0-9０-９]+\s*[.．、)）:]\s*", "", wanted)
    if stripped and stripped != wanted:
        for line in str(content or "").splitlines():
            match = re.match(r"^\s*#{1,6}\s+(.+?)\s*$", line)
            if not match:
                continue
            actual = re.sub(r"\s+", " ", match.group(1)).strip()
            if actual == stripped or stripped in actual:
                return True
    return False
