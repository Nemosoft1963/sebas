# CHANGELOG 2026-09-25 Goal Completion 第2段階-C

prepare 実行器の接続・予算の時間回復・既回答の適用・blocked 統合。flag 既定 OFF。本番未反映。

## 予算回復の方式と理由

- `RecoveryBudget` 自体は変更しない（他呼び出し元の意味を維持する）。
- NAC 側 (`app/next_action_controller.py`) で、**時間予算(300秒)を使い切ったあと 600秒のクールダウンが過ぎたときだけ**、その (project, criterion) を新しい `RecoveryBudget` に置き換える。
- **cycle / tool / llm の回数上限超過は時間では回復させない。** 暴走を止め続けるため。明示 `reset_budget` のみ回数側を消せる。
- 予算超過・`RecoveryStopped` は従来どおり chain を `needs_approval` で停止し、成功扱いにしない。

保守側を選んだ理由: 時間切れは「枠の寿命」であり一定時間後に再試行してよい。回数超過は同一原因の反復なので、時間経過だけで自動再開すると連鎖が再発する。

## 既回答の適用条件と skipped

- 新実行器 `apply_prior_answers`。`compute` が `confirm_allocation` のとき、条件一致の既回答が1件でもあれば人手より前に `local_safe` / `auto_executable` として返す。
- 適用は **subject_key と期間と入力版(input_hash) のすべてが一致**した項目だけ。金額・比率の推測・類似一致・0円埋めはしない。
- 一致しない項目は適用せず `skipped: [{issue_id, reason}]` に残し、人手の `confirm_allocation` へ回す。
- reason 例: `subject_key_mismatch` / `period_mismatch` / `input_hash_mismatch` / `invalid` / `stale_decision` / `no_prior_answer`。
- 適用結果は `applied` に適用元の rule/decision id を残す。全項目適用後も `achieved`・人間承認は書かない。

## blocked 統合

- `UNIMPLEMENTED_ACTIONS` (`triz_adopt` / `triz_rerun` / `recipe_apply`) を NAC `compute` の `blocked_actions` に既存理由のまま出す。
- これらは `executable=False`。`execute` は常に拒否（API 409）。TRIZ adopt を有効化する経路は作らない。
- `workflow_readiness` の `blocked_actions` 構造は変えない。

## バックアップ確認（コード変更なし）

- `GoalCompletionStore` は `Path(memory_path).parent / "goal_completion.sqlite3"`。
- コンテナは `DATA_DIR=/data`、会話DBは `/data/memory/conversations.db`。よって `goal_completion.sqlite3` は **memory.sqlite3 と同じ `/data/memory/`**。
- `docker-compose.yml` の `app_data` ボリューム名は `localsaporter_app_data`（`/data` を丸ごとマウント）。
- `scripts/backup.ps1` は `localsaporter_app_data` を tar するため、`goal_completion.sqlite3` は既存バックアップ対象に含まれる。PowerShell は編集していない。

## 変更ファイル

- `app/next_action_controller.py` — 実行器登録、時間予算クールダウン置換、blocked 統合、既回答優先
- `app/vehicle_service.py` — `run_prepare` / `preview_prior_answers` / `apply_prior_answers`（既存 `reuse_decisions` の条件を薄く接続。既存関数の挙動は変更しない）
- `app/web.py` — `prepare_vehicle_profit` は `run_prepare` を呼ぶだけ。レスポンス・409 は維持
- `tests/test_next_action_completion.py` — NAC-10〜15
- 本ファイル

## 検証

開発環境で `pytest -q` を実行: **722 passed, 1 skipped**（基準 714 passed, 1 skipped から新規 NAC-10〜15 の 8 件だけ増加）。本番イメージでは未実証。
