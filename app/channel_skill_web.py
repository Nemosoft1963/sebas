"""C2: web.search_public の実行関数。既存の公開Web収集を共通契約で包む。

- 入力検査(検索語・許可・項目ID実在)を書込・外部呼び出しの前に済ませる。
- researcher.collect(query, max_results) だけを呼び、実通信は試験では注入する
  偽の研究器に差し替える(本番の web.py では既存の生成済み研究器を使う)。
- 結果は一次資料ごとに build_evidence/record_evidence で証拠にし、
  record_run で結果を記録する。
- 例外・取得失敗 -> failed、正常0件 -> no_results、1件以上 -> succeeded、
  結果の形が不正・読めない -> unknown。取得失敗を no_results に倒さない。
- 本文は未信頼データ。命令文を実行せず、LLMのプロンプトへ入れない。
  証拠の抜粋は伏せ処理済みのみ。本文そのものは保存しない。
- 検索語そのものは、拒否した場合も台帳に保存しない。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app import channel_skills as skills

CAPABILITY_ID = "web.search_public"


class WebSearchRefused(ValueError):
    """書込・外部呼び出しの前の拒否(検索語・許可・項目ID)。"""


class WebSearchNotAllowed(WebSearchRefused):
    """公開Web調査の許可が無い。"""

    def __init__(self, reason: str = "public web research is not allowed") -> None:
        super().__init__(reason)
        self.reason = str(reason or "")


def _checked_query(query: str, *, project_sources: list[str] | None) -> str:
    try:
        viewed = skills.validate_search_query(
            query, project_sources=project_sources)
    except ValueError as exc:
        raise WebSearchRefused(f"query rejected: {exc}") from exc
    text = str((viewed or {}).get("query") or "")
    if not text:
        raise WebSearchRefused("query rejected: empty")
    return text


def _checked_plan_item(memory_path: str | Path, project_id: str,
                       plan_item_id: str) -> str:
    try:
        return skills.validate_plan_item_id(memory_path, project_id,
                                            plan_item_id)
    except ValueError as exc:
        raise WebSearchRefused(f"plan item rejected: {exc}") from exc


def _checked_permission(memory_path: str | Path, project_id: str) -> None:
    viewed = skills.web_search_permission(memory_path, project_id)
    reason = str((viewed or {}).get("reason") or "")
    if bool((viewed or {}).get("allowed")):
        return
    if reason == "unreadable":
        raise WebSearchRefused(
            "web search permission is unreadable: cannot confirm plan")
    raise WebSearchNotAllowed(
        f"public web research is not allowed for this project: {reason or 'denied'}")


def _checked_mode(operating_mode: str | None) -> str:
    mode = str(operating_mode or "").strip()
    # 未設定・読取不能は production 相当の厳しい側。
    if mode not in ("demo_test", "production"):
        return "production"
    return mode


def _source_is_valid(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    url = str(item.get("url") or "").strip()
    if not url:
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return False
    body = str(item.get("content") or "")
    if not isinstance(body, str):
        return False
    fetched = str(item.get("retrieved_at") or "").strip()
    sha = str(item.get("sha256") or "").strip().lower()
    if not fetched or len(sha) != 64:
        return False
    return True


async def _validate_url_public_https(url: str) -> str:
    """検証済みの公開HTTPS URL。検証不能は例外(unknown 側へ倒す)。

    DNS解決を伴う厳密な検証は本番の PublicWebResearcher が既に行っている。
    ここでは形式(https・認証情報なし・標準ポート)だけを確認し、内部遮断の
    二重検査は研究器の収集時に任せる。試験の偽研究器は外部通信しない。
    """
    text = str(url or "").strip()
    parsed = urlsplit(text)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError(f"not public https url: {text[:120]}")
    if parsed.username or parsed.password:
        raise ValueError("url must not contain credentials")
    if parsed.port not in (None, 443):
        raise ValueError("url must use https standard port")
    return text


async def run_web_search(memory_path: str | Path, project_id: str, *,
                         query: str, plan_item_id: str, purpose: str,
                         operating_mode: str | None = None,
                         researcher: Any = None,
                         max_results: int = 4) -> dict:
    """公開Web調査を実行し、一次資料を証拠に結ぶ。

    戻り値は {"status", "run", "evidence", "reason"}。
    status は succeeded/no_results/failed/unknown のいずれか。
    拒否(検索語・許可・項目ID)は WebSearchRefused 例外(呼出側が409/422にする)。
    研究器の例外は failed として記録して返す(送出しない)。
    結果の形が不正・読めないときは unknown として記録して返す。
    """
    pid = str(project_id or "").strip()
    if not pid:
        raise WebSearchRefused("project_id is required")
    item_id = str(plan_item_id or "").strip()
    if not item_id:
        raise WebSearchRefused("plan_item_id is required")
    reason_text = str(purpose or "").strip()
    if not reason_text:
        raise WebSearchRefused("purpose is required")
    try:
        limit = max(1, min(int(max_results), 6))
    except (TypeError, ValueError):
        raise WebSearchRefused("max_results is invalid") from None
    mode = _checked_mode(operating_mode)

    # 書込・外部呼び出しの前に済ませる検査(順序: 検索語 -> 許可 -> 項目ID)。
    sources_for_quote = skills.project_registered_context_texts(
        memory_path, pid)
    checked = _checked_query(query, project_sources=sources_for_quote)
    _checked_permission(memory_path, pid)
    checked_item = _checked_plan_item(memory_path, pid, item_id)
    if researcher is None:
        raise WebSearchRefused("researcher is required")

    started = time.time()
    adapter = "public_web"
    try:
        adapter_name = type(researcher).__name__ or "researcher"
    except (AttributeError, TypeError):
        adapter_name = "public_web"
    adapter = str(adapter_name)[:200] or "public_web"

    # collect を呼ぶ。例外は failed として記録し、代替値で続行しない。
    try:
        collected = await researcher.collect(checked, limit)
    except Exception as exc:
        finished = time.time()
        saved = skills.record_run(
            memory_path, project_id=pid, capability_id=CAPABILITY_ID,
            status="failed", purpose=reason_text[:2000],
            operating_mode=mode, adapter=adapter,
            params_summary={"plan_item_id": checked_item},
            started_at=started, finished_at=finished,
            error_kind=type(exc).__name__ or "Error",
            evidence_count=0, completed_ok=False,
        )
        return {"status": "failed", "run": saved, "evidence": [],
                "reason": f"collection failed: {type(exc).__name__}"}

    # 結果の形を検査する。不正・読めないときは unknown(0件にしない)。
    if not isinstance(collected, dict):
        finished = time.time()
        saved = skills.record_run(
            memory_path, project_id=pid, capability_id=CAPABILITY_ID,
            status="unknown", purpose=reason_text[:2000],
            operating_mode=mode, adapter=adapter,
            params_summary={"plan_item_id": checked_item},
            started_at=started, finished_at=finished,
            error_kind="malformed_result", evidence_count=0,
            completed_ok=False,
        )
        return {"status": "unknown", "run": saved, "evidence": [],
                "reason": "result is unreadable"}
    raw_sources = collected.get("sources")
    if raw_sources is None:
        finished = time.time()
        saved = skills.record_run(
            memory_path, project_id=pid, capability_id=CAPABILITY_ID,
            status="unknown", purpose=reason_text[:2000],
            operating_mode=mode, adapter=adapter,
            params_summary={"plan_item_id": checked_item},
            started_at=started, finished_at=finished,
            error_kind="malformed_result", evidence_count=0,
            completed_ok=False,
        )
        return {"status": "unknown", "run": saved, "evidence": [],
                "reason": "result.sources is unreadable"}
    if not isinstance(raw_sources, list):
        finished = time.time()
        saved = skills.record_run(
            memory_path, project_id=pid, capability_id=CAPABILITY_ID,
            status="unknown", purpose=reason_text[:2000],
            operating_mode=mode, adapter=adapter,
            params_summary={"plan_item_id": checked_item},
            started_at=started, finished_at=finished,
            error_kind="malformed_result", evidence_count=0,
            completed_ok=False,
        )
        return {"status": "unknown", "run": saved, "evidence": [],
                "reason": "result.sources is not a list"}

    # 正常に完了して0件 -> no_results(取得失敗と区別する)。
    if not raw_sources:
        finished = time.time()
        saved = skills.record_run(
            memory_path, project_id=pid, capability_id=CAPABILITY_ID,
            status="no_results", purpose=reason_text[:2000],
            operating_mode=mode, adapter=adapter,
            params_summary={"plan_item_id": checked_item},
            started_at=started, finished_at=finished,
            error_kind="", evidence_count=0, completed_ok=True,
        )
        return {"status": "no_results", "run": saved, "evidence": [],
                "reason": "no sources"}

    # 1件ずつ検証し、一次資料ごとに証拠にする。
    # 本文は未信頼データ。本文中の命令文は実行しない。
    evidence_rows: list[dict] = []
    for entry in raw_sources[:limit]:
        if not _source_is_valid(entry):
            finished = time.time()
            saved = skills.record_run(
                memory_path, project_id=pid, capability_id=CAPABILITY_ID,
                status="unknown", purpose=reason_text[:2000],
                operating_mode=mode, adapter=adapter,
                params_summary={"plan_item_id": checked_item},
                started_at=started, finished_at=finished,
                error_kind="malformed_source", evidence_count=0,
                completed_ok=False,
            )
            return {"status": "unknown", "run": saved,
                    "evidence": list(evidence_rows),
                    "reason": "source is unreadable"}
        url = str(entry.get("url") or "").strip()
        try:
            verified_url = await _validate_url_public_https(url)
        except ValueError as exc:
            finished = time.time()
            saved = skills.record_run(
                memory_path, project_id=pid, capability_id=CAPABILITY_ID,
                status="unknown", purpose=reason_text[:2000],
                operating_mode=mode, adapter=adapter,
                params_summary={"plan_item_id": checked_item},
                started_at=started, finished_at=finished,
                error_kind="invalid_url", evidence_count=0,
                completed_ok=False,
            )
            return {"status": "unknown", "run": saved,
                    "evidence": list(evidence_rows),
                    "reason": f"url rejected: {exc}"}
        body = str(entry.get("content") or "")
        fetched_at = str(entry.get("retrieved_at") or "")
        try:
            built = skills.build_evidence(
                provider="public_web",
                account_or_public_id="public_web",
                url=verified_url,
                fetched_at=fetched_at,
                period_start=fetched_at,
                period_end=fetched_at,
                body=body,
                project_id=pid,
                plan_item_id=checked_item,
                purpose=reason_text[:2000],
                retention_days=90,
            )
        except ValueError as exc:
            finished = time.time()
            saved = skills.record_run(
                memory_path, project_id=pid, capability_id=CAPABILITY_ID,
                status="unknown", purpose=reason_text[:2000],
                operating_mode=mode, adapter=adapter,
                params_summary={"plan_item_id": checked_item},
                started_at=started, finished_at=finished,
                error_kind="evidence_rejected", evidence_count=0,
                completed_ok=False,
            )
            return {"status": "unknown", "run": saved,
                    "evidence": list(evidence_rows),
                    "reason": f"evidence rejected: {exc}"}
        saved_evidence = skills.record_evidence(
            memory_path, built, capability_id=CAPABILITY_ID)
        evidence_rows.append(saved_evidence)

    finished = time.time()
    saved = skills.record_run(
        memory_path, project_id=pid, capability_id=CAPABILITY_ID,
        status="succeeded", purpose=reason_text[:2000],
        operating_mode=mode, adapter=adapter,
        params_summary={"plan_item_id": checked_item},
        started_at=started, finished_at=finished,
        error_kind="", evidence_count=len(evidence_rows),
        completed_ok=True,
    )
    return {"status": "succeeded", "run": saved,
            "evidence": list(evidence_rows), "reason": ""}
