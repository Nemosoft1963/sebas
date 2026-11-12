# 2026-11-04 G3-a2 送信直前の漏えい再検査 本番反映

## 反映内容

- 受領ZIP `LOCALSAPORTER_deploy_20261104_g3a2_privacy_recheck.zip`（SHA-256 `9633861A0B9FD9BBB2E85695EE2573E1B0F73DC2CFA68C4C91C6D0448D8EE1BE`）の3ファイルを `hashes.txt` と照合し、差分を審査して適用。
- `app/external_plan_jobs.py::prepare_job` に最新の登録原本と禁止語を用いたG2-1検査を追加。承認済みpacketの確認後、ジョブ保存・許可消費より前に実施する。検査不合格・検査不能・引数欠落は `privacy_recheck_failed` で止め、ジョブを作らず許可も消費しない。本文・原本・検出値は監査に残さない。
- `app/web.py` は、機能フラグ無効なら原本を読まずに403を返し、有効な場合だけ最新原本と禁止語を取得してジョブへ渡す。既存ジョブには再送しない。前回G3-aで追加したDB読取失敗時の送信停止処理と試験を維持。
- ホストとコンテナのSHA-256: `external_plan_jobs.py` = `E507F9CE85B880285BCB972DAAE71F28534433217604B338349BEDAB01478F5B`、`web.py` = `16C8534D2A002304139E97DAFE49337AAFAA419665901DEF7C7357A29D548445`。試験ファイル = `D621907D4BE1D04E7DDB0F09791D90AD9A0F29DA4E362BB5872C4CC03A76C3CD`。新Webイメージ `sha256:f5c59f2edef9997ba071c389584cd401883954f65933fa35a7b2c351b96d8b7f` はhealthy。

## 検証

- G3-a2を含む関連試験: **112 passed**。ログ: `output/g3a2_privacy_recheck_focused_20261104.txt`。
- 全体試験（`PYTHONUTF8=1`、`PYTHONHASHSEED=3`、pytestプラグイン有効）: **2725 passed / 10 failed / 5 skipped**、724.36秒。失敗10件は直前G3-a本番試験と試験名が完全一致し、新規失敗0。全体合格ではない。ログ: `output/g3a2_privacy_recheck_full_20261104.txt`。
- `scripts/healthcheck.ps1` 全項目PASS。選択Ollamaモデル `qwen3.5:9b`。Web healthy。コンテナ内の `SEBAS_EXTERNAL_PLAN_JOBS_PROJECTS` は空で全PJ無効。
- 実外部AI送信、実PJでのジョブ実行・下書き統合は行っていない。

## 限界・次工程

- 再検査は送信**準備時**であり、準備後から非同期ランナーの実送信までに原本が変わる短い窓は残る。拒否リスト方式の通過も安全保証ではない。
- G3-aの画面と下書きのローカル統合は未実装。既存4つの外部送信経路にはpacket許可が未接続。機能フラグは有効化していない。
- OCR Phase 5、車両損益の実案件受入、業務確認事項36項目は未完了。

## 保全・復旧

- 旧 `app/external_plan_jobs.py`、旧 `app/web.py`、旧試験ファイル、停止時の `/data/memory`、展開済みファイルを `backups/20261104_g3a2_privacy_recheck_final62/` に退避。旧Webイメージ `localsaporter-web:before-g3a2-privacy-recheck-20261104` = `sha256:8dd43b9a2c324539675f705eaa27cd966f728387629166ea8a64b67326538a7c`。
- 復旧時はWebだけを停止して退避済み3ファイルを戻し、旧イメージで再作成する。データは影響確認なしに巻き戻さない。
