"""C-3 ランナーのテスト (注入したHTTP関数・サーバなし)。

疑似データのみ。外部通信なし。実案件に触れない。
"""
import datetime as _dt
import json

import pytest

import scripts.night_run as runner


def test_localhost_only_rejects_external():
    for bad in ("http://example.com", "https://192.168.1.1:8099/x",
                "http://[::ffff:1.2.3.4]/", "ftp://127.0.0.1/x",
                "http://user:pass@127.0.0.1/"):
        with pytest.raises(ValueError):
            runner.ensure_local_url(bad)
    assert runner.ensure_local_url("http://127.0.0.1:8099") == \
        "http://127.0.0.1:8099"
    assert runner.ensure_local_url("http://localhost:8099/") == \
        "http://localhost:8099"


def test_parse_until_format_and_next_day():
    now = _dt.datetime(2026, 10, 15, 23, 0, tzinfo=_dt.timezone.utc)
    assert runner.parse_until("05:00", now) > now.timestamp()
    with pytest.raises(ValueError):
        runner.parse_until("25:00", now)
    with pytest.raises(ValueError):
        runner.parse_until("not-a-time", now)


def _ok_post_factory(log, ticks=3, final_state="finished"):
    state = {"n": 0}

    def _post(base_url, path, payload, timeout=30.0):
        log.append((path, dict(payload or {})))
        if path == "/api/night-run/start":
            return 200, {"run_id": "nr_架空1", "token": "架空token",
                         "budget": {}}
        if path.endswith("/tick-next"):
            state["n"] += 1
            if state["n"] < ticks:
                return 200, {"done": False, "reason": "tick_recorded",
                             "step": {"project_id": "架空PJ",
                                      "status": "advanced"},
                             "state": "running"}
            return 200, {"done": True, "reason": "no_candidates",
                         "step": None, "state": "running"}
        if path.endswith("/finish"):
            return 200, {"run_id": "nr_架空1", "state": final_state,
                         "report": {"total_projects": 1, "total_ticks": ticks,
                                    "state": final_state},
                         "report_status": "ok"}
        raise AssertionError(path)

    return _post


def test_runner_normal_finish_zero():
    log = []
    code = runner.run_night(http_post=_ok_post_factory(log, ticks=3),
                            actor="架空担当", base_url="http://127.0.0.1:8099",
                            max_ticks=10, per_project=5,
                            until_epoch=9999999999.0, dry_run=False)
    assert code == 0
    paths = [p for p, _ in log]
    assert paths[0] == "/api/night-run/start"
    assert paths[-1] == "/api/night-run/架空1/finish" or \
        paths[-1].endswith("/finish")
    assert sum(1 for p in paths if p.endswith("/tick-next")) == 3


def test_runner_ctrl_c_calls_finish_nonzero():
    log = []

    def _post(base_url, path, payload, timeout=30.0):
        log.append((path, dict(payload or {})))
        if path == "/api/night-run/start":
            return 200, {"run_id": "nr_架空2", "token": "架空token",
                         "budget": {}}
        if path.endswith("/tick-next"):
            raise KeyboardInterrupt()
        if path.endswith("/finish"):
            return 200, {"run_id": "nr_架空2", "state": "finished",
                         "report": {"total_projects": 0, "total_ticks": 0},
                         "report_status": "ok"}
        raise AssertionError(path)

    code = runner.run_night(http_post=_post, actor="架空担当",
                            base_url="http://127.0.0.1:8099",
                            max_ticks=10, per_project=5,
                            until_epoch=9999999999.0, dry_run=False)
    assert code == 130
    assert any(p.endswith("/finish") for p, _ in log)


def test_runner_until_calls_finish():
    log = []
    code = runner.run_night(http_post=_ok_post_factory(log, ticks=99),
                            actor="架空担当", base_url="http://127.0.0.1:8099",
                            max_ticks=10, per_project=5,
                            until_epoch=1.0, dry_run=False,
                            now_fn=lambda: 2.0)
    assert code in (0, 1)
    assert any(p.endswith("/finish") for p, _ in log)


def test_runner_server_done_calls_finish():
    log = []
    code = runner.run_night(http_post=_ok_post_factory(log, ticks=1),
                            actor="架空担当", base_url="http://127.0.0.1:8099",
                            max_ticks=10, per_project=5,
                            until_epoch=9999999999.0, dry_run=False)
    assert code == 0
    assert any(p.endswith("/finish") for p, _ in log)


def test_runner_unexpected_response_aborts_nonzero():
    log = []

    def _post(base_url, path, payload, timeout=30.0):
        log.append((path, dict(payload or {})))
        if path == "/api/night-run/start":
            return 200, {"run_id": "nr_架空3", "token": "架空token",
                         "budget": {}}
        if path.endswith("/tick-next"):
            return 200, {"unexpected": True}
        if path.endswith("/finish"):
            return 200, {"run_id": "nr_架空3", "state": "finished",
                         "report": None, "report_status": "missing"}
        raise AssertionError(path)

    code = runner.run_night(http_post=_post, actor="架空担当",
                            base_url="http://127.0.0.1:8099",
                            max_ticks=10, per_project=5,
                            until_epoch=9999999999.0, dry_run=False)
    assert code == 1
    assert any(p.endswith("/finish") for p, _ in log)


def test_runner_dry_run_writes_nothing():
    calls = []

    def _post(base_url, path, payload, timeout=30.0):
        calls.append((path, dict(payload or {})))
        raise AssertionError("dry-run must not POST")

    def _get(base_url, path, timeout=30.0):
        calls.append(("GET " + path, {}))
        return 200, {"has_run": False, "reason": "実行なし"}

    code = runner.run_night(http_post=_post, http_get=_get,
                            actor="架空担当",
                            base_url="http://127.0.0.1:8099",
                            max_ticks=10, per_project=5,
                            until_epoch=9999999999.0, dry_run=True)
    assert code == 0
    assert calls == [("GET /api/night-run/latest", {})]


def test_runner_external_url_rejected():
    code = runner.run_night(actor="架空担当",
                            base_url="http://example.com:8099",
                            max_ticks=10, per_project=5,
                            until_epoch=9999999999.0, dry_run=False)
    assert code == 2
