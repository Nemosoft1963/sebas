"""C-3 人が起動する夜間実行の外部ランナー (人が起動するコマンド)。

アプリには含まれず、定期実行もしない。使い方:
  python scripts/night_run.py --actor 山田 --until 05:00 --max-ticks 60 \
      --per-project 5 [--dry-run]

ローカルのセバスAPI (既定 http://127.0.0.1:8099、引数で変更可。外部ホストへの
送信は拒否=ローカルホスト以外はエラー) を urllib で呼び、start → tick-next を
繰り返し → finish の順に動く。--dry-run は何も書かず、回す予定のPJと理由だけを
表示する。Ctrl+C・時間枠の終了・サーバ側の done で、必ず finish (または中止の
通知) を呼んで終わる。ネットワーク失敗・想定外の応答はリトライせず中止して
報告 (成功扱いにしない)。終了コードは、正常終了=0、中止/失敗=非0。
HTTP部分は注入可能な関数にして、テストでサーバを使わずに検証できるようにする。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "http://127.0.0.1:8099"

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def ensure_local_url(base_url: str) -> str:
    """外部ホストへの送信は拒否。ローカルホスト以外はエラー。"""
    text = str(base_url or "").strip()
    if not text:
        raise ValueError("base_url を確認できません")
    try:
        parts = urllib.parse.urlparse(text)
    except (ValueError, AttributeError) as exc:
        raise ValueError("base_url を確認できません:" + type(exc).__name__)
    if parts.scheme not in ("http", "https"):
        raise ValueError("http/https のみ対応します")
    host = str(parts.hostname or "").strip().lower()
    if host not in LOCAL_HOSTS:
        raise ValueError("ローカルホスト以外への送信は拒否します:" + host)
    if parts.username or parts.password:
        raise ValueError("認証付きURLは受け付けません")
    return text.rstrip("/")


def parse_until(text: str, now: _dt.datetime | None = None) -> float:
    """HH:MM を当夜の終了epochに変換する。過ぎた時刻は翌日扱い。"""
    raw = str(text or "").strip()
    try:
        hour_text, minute_text = raw.split(":")
        hour = int(hour_text)
        minute = int(minute_text)
    except (ValueError, AttributeError):
        raise ValueError("--until は HH:MM 形式で指定してください")
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("--until は HH:MM 形式で指定してください")
    base = now or _dt.datetime.now().astimezone()
    target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= base:
        target = target + _dt.timedelta(days=1)
    return target.timestamp()


def _http_post_json(base_url: str, path: str, payload: dict,
                    timeout: float = 30.0) -> tuple[int, dict]:
    """既定のHTTP実装。urllibのみ。戻り値 (status, body)。"""
    url = str(base_url).rstrip("/") + path
    data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200) or 200)
            raw = response.read()
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except (OSError, ValueError):
            raw = b""
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            body = {"detail": "HTTP " + str(int(getattr(exc, "code", 0) or 0))}
        if not isinstance(body, dict):
            body = {"detail": "応答の形式を確認できません"}
        return int(getattr(exc, "code", 0) or 0), body
    try:
        body = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        raise ValueError("応答を読み取れませんでした")
    if not isinstance(body, dict):
        raise ValueError("応答の形式を確認できません")
    return status, body


def _http_get_json(base_url: str, path: str,
                   timeout: float = 30.0) -> tuple[int, dict]:
    url = str(base_url).rstrip("/") + path
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200) or 200)
            raw = response.read()
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except (OSError, ValueError):
            raw = b""
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            body = {"detail": "HTTP " + str(int(getattr(exc, "code", 0) or 0))}
        if not isinstance(body, dict):
            body = {"detail": "応答の形式を確認できません"}
        return int(getattr(exc, "code", 0) or 0), body
    try:
        body = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        raise ValueError("応答を読み取れませんでした")
    if not isinstance(body, dict):
        raise ValueError("応答の形式を確認できません")
    return status, body


def _check_start_response(status: int, body: dict) -> dict:
    if status != 200:
        raise ValueError("start が失敗しました:HTTP " + str(status))
    if not isinstance(body, dict):
        raise ValueError("start の応答を確認できません")
    run_id = body.get("run_id")
    token = body.get("token")
    if not run_id or not token:
        raise ValueError("start の応答を確認できません")
    return {"run_id": str(run_id), "token": str(token),
            "budget": body.get("budget") if isinstance(
                body.get("budget"), dict) else {}}


def _check_tick_response(status: int, body: dict) -> dict:
    if status != 200:
        raise ValueError("tick-next が失敗しました:HTTP " + str(status))
    if not isinstance(body, dict):
        raise ValueError("tick-next の応答を確認できません")
    if "done" not in body or "reason" not in body:
        raise ValueError("tick-next の応答を確認できません")
    done = body.get("done")
    if not isinstance(done, bool):
        raise ValueError("tick-next の応答を確認できません")
    return {"done": done, "reason": str(body.get("reason") or ""),
            "step": body.get("step"), "state": str(body.get("state") or "")}


def _check_finish_response(status: int, body: dict) -> dict:
    if status != 200:
        raise ValueError("finish が失敗しました:HTTP " + str(status))
    if not isinstance(body, dict):
        raise ValueError("finish の応答を確認できません")
    return body


def plan_dry_run(base_url: str, actor: str,
                 http_get=None, http_post=None) -> dict:
    """--dry-run 用。何も書かず、回す予定のPJと理由だけを返す。"""
    _ = actor
    get = http_get or _http_get_json
    post = http_post or _http_post_json
    _ = post
    status, body = get(str(base_url).rstrip("/"), "/api/night-run/latest")
    if status != 200:
        raise ValueError("latest の取得に失敗しました:HTTP " + str(status))
    return body if isinstance(body, dict) else {}


def run_night(http_post=None, http_get=None, actor: str = "",
              base_url: str = DEFAULT_BASE_URL, max_ticks: int = 60,
              per_project: int = 5, until_epoch: float = 0,
              dry_run: bool = False,
              now_fn=None) -> int:
    """start → tick-next 繰返し → finish。戻り値は終了コード。

    ネットワーク失敗・想定外の応答はリトライせず中止して報告
    (成功扱いにしない)。finish は必ず呼ぶ。
    """
    post = http_post or _http_post_json
    get = http_get or _http_get_json
    actor_name = str(actor or "").strip()
    if not actor_name:
        print("actor を指定してください", file=sys.stderr)
        return 2
    try:
        base = ensure_local_url(base_url)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    now = now_fn or __import__("time").time
    try:
        limit_ticks = int(max_ticks)
        limit_per = int(per_project)
    except (TypeError, ValueError):
        print("max-ticks/per-project を確認できません", file=sys.stderr)
        return 2
    if limit_ticks < 1 or limit_per < 1:
        print("max-ticks/per-project は1以上が必要です", file=sys.stderr)
        return 2
    if dry_run:
        try:
            latest = plan_dry_run(base, actor_name, http_get=get,
                                  http_post=post)
        except Exception as exc:
            print("dry-run の確認に失敗しました:"
                  + type(exc).__name__ + ":" + str(exc)[:300],
                  file=sys.stderr)
            return 1
        print("dry-run: 何も書き込みません")
        report = latest.get("report") if isinstance(latest, dict) else None
        print("latest: " + json.dumps(latest, ensure_ascii=False,
                                      sort_keys=True)[:2000])
        _ = report
        return 0
    budget = {"max_ticks": limit_ticks, "per_project": limit_per,
              "until_epoch": float(until_epoch)}
    try:
        status, body = post(base, "/api/night-run/start",
                            {"actor": actor_name, "budget": budget})
        started = _check_start_response(status, body)
    except KeyboardInterrupt:
        print("中断しました (start前)", file=sys.stderr)
        return 130
    except Exception as exc:
        print("start に失敗しました:" + type(exc).__name__ + ":"
              + str(exc)[:300], file=sys.stderr)
        return 1
    run_id = started["run_id"]
    token = started["token"]
    print("開始しました run_id=" + run_id)
    aborted = False
    abort_reason = ""
    interrupted = False
    try:
        while True:
            if until_epoch and now() >= float(until_epoch):
                abort_reason = "時間枠の終了のため finish します"
                break
            try:
                status, body = post(
                    base, "/api/night-run/" + run_id + "/tick-next",
                    {"token": token})
                ticked = _check_tick_response(status, body)
            except KeyboardInterrupt:
                interrupted = True
                abort_reason = "中断(Ctrl+C)のため finish します"
                break
            except Exception as exc:
                aborted = True
                abort_reason = ("tick-next の失敗のため中止します:"
                                + type(exc).__name__ + ":"
                                + str(exc)[:300])
                break
            step = ticked.get("step")
            if isinstance(step, dict) and step.get("project_id"):
                print("tick: " + str(step.get("project_id")) + "/"
                      + str(step.get("status") or ""))
            else:
                print("tick: " + str(ticked.get("reason") or ""))
            if ticked.get("done"):
                abort_reason = str(ticked.get("reason") or "done")
                break
    except KeyboardInterrupt:
        interrupted = True
        abort_reason = "中断(Ctrl+C)のため finish します"
    try:
        status, body = post(base, "/api/night-run/" + run_id + "/finish",
                            {"token": token, "actor": actor_name,
                             "end_reason": abort_reason[:500]})
        finished = _check_finish_response(status, body)
    except KeyboardInterrupt:
        print("中断しました (finish中)", file=sys.stderr)
        return 130
    except Exception as exc:
        print("finish に失敗しました:" + type(exc).__name__ + ":"
              + str(exc)[:300], file=sys.stderr)
        return 1
    report = finished.get("report") if isinstance(finished, dict) else None
    if isinstance(report, dict):
        print("終了 state=" + str(finished.get("state") or "") + " "
              + "PJ" + str(report.get("total_projects")) + "件 "
              + "ティック" + str(report.get("total_ticks")) + "件")
    else:
        print("終了 state=" + str(finished.get("state") or "")
              + " (報告なし)")
    if interrupted:
        return 130
    if aborted:
        return 1
    state = str(finished.get("state") or "")
    if state in ("finished",):
        return 0
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="人が起動する夜間実行ランナー (常駐しない)")
    parser.add_argument("--actor", required=True,
                        help="開始者名 (必須)")
    parser.add_argument("--until", required=True,
                        help="終了時刻 HH:MM (例 05:00)")
    parser.add_argument("--max-ticks", type=int, default=60,
                        help="総ティック数 (既定60)")
    parser.add_argument("--per-project", type=int, default=5,
                        help="PJあたりのティック数 (既定5、上限10)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help="ローカルのセバスAPI (既定 "
                        + DEFAULT_BASE_URL + ")")
    parser.add_argument("--dry-run", action="store_true",
                        help="何も書かず、回す予定のPJと理由だけを表示する")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        until_epoch = parse_until(args.until)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return run_night(actor=args.actor, base_url=args.base_url,
                     max_ticks=args.max_ticks,
                     per_project=args.per_project,
                     until_epoch=until_epoch,
                     dry_run=bool(args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
