# 2026-11-04 G3-a 外部計画作成ジョブ 本番反映

## 反映内容

- 受領ZIP `LOCALSAPORTER_deploy_20261104_g3a_external_plan_jobs.zip`（SHA-256 `AE708E172A5CDEC7C92AEE7045A74D8DFA8E3DBC35526E91CAAAF2DE94FA6C16`）の3ファイルを `hashes.txt` と照合し、差分を審査。
- `app/external_plan_jobs.py` と `app/web.py` で、承認済みの計画生成packetを選択済み外部AIへ1回送る非同期ジョブAPIを追加。返答は検査後に信用しない下書きとして保存し、既存計画への採用・承認・実行開始にはつなげない。PJごとの機能フラグ `SEBAS_EXTERNAL_PLAN_JOBS_PROJECTS` は未設定で、全PJで無効。
- 配布物の `prepare_job` では既存ジョブや日次件数のDB読取失敗を空と扱い、再送防止・送信上限を迂回し得た。反映前に両箇所を `check_error` で停止するよう補正し、`tests/test_g3a_external_plan_jobs.py` にDB読取障害の回帰試験を追加。配布物のハッシュと最終本番ハッシュの相違はこの安全補正による。
- 本番ホスト/コンテナの最終SHA-256は `external_plan_jobs.py` = `202ADA1C6BCA3DE4F9669CAC0DC96CF99079D07165E6356FFE44B7CD0C38E403`、`web.py` = `35A8A0DFEAAD7EA141B8F50D1D25D3D1ED182B1D536B7FCCF387D37815AEAB07` で一致。補正済み試験ファイル = `C59B1451182611C99FE83E01311CDBD16A69F73B6730994AC31495BB82674252`。Webイメージ `sha256:8dd43b9a2c324539675f705eaa27cd966f728387629166ea8a64b67326538a7c` はhealthy。

## 検証

- G3-a、G2-2、G1、G2-1、G0-1関連試験: **105 passed**。ログ: `output/g3a_external_plan_jobs_focused_20261104.txt`。
- 全体試験（`PYTHONUTF8=1`、`PYTHONHASHSEED=3`、pytestプラグイン有効）: **2718 passed / 10 failed / 5 skipped**、703.26秒。失敗10件は直前G2-2本番試験と試験名が完全一致し、新規失敗0。全体合格ではない。ログ: `output/g3a_external_plan_jobs_full_20261104.txt`。
- `scripts/healthcheck.ps1` 全項目PASS。選択Ollamaモデル `qwen3.5:9b`。Web healthy。コンテナ内の機能フラグは空であることを確認。
- 実外部AI送信、実PJでのジョブ実行・下書き統合は行っていない。

## 限界・次工程

- G3-aはAPIのみ。画面と下書きのローカル統合は未実装。既存4送信経路にはpacket許可がまだ接続されず、G0-1/G2-1検査を通れば従来どおり自動送信され得る。
- G3-aの送信準備では承認済みpacketと計画署名・許可を検査するが、承認後に登録原本が変わった場合のDLP再検査は行わない。機能フラグを有効化する前に、この再検査と実通信での受入を検討する。拒否リスト検査の通過自体も安全保証ではない。
- OCR Phase 5、車両損益の実案件受入、業務確認事項36項目は未完了。

## 保全・復旧

- 旧 `app/web.py`、停止時の `/data/memory`、受領ZIPの展開物（安全補正済み2ファイルを含む）を `backups/20261104_g3a_external_plan_jobs_final61/` に退避。旧Webイメージ `localsaporter-web:before-g3a-external-plan-jobs-20261104` = `sha256:5e082dada78d84c1f5ea4654863ea7a97bf1d0e2627d1cdb9fa9fd0d547ca017`。
- 復旧時はWebだけを停止して退避した `app/web.py` を戻し、新規 `app/external_plan_jobs.py` と試験ファイルを除外して旧イメージで再作成する。データは影響確認なしに巻き戻さない。
