# 2026-11-04 G4-a 既存外部AI送信経路の方針と検査 本番反映

## 反映内容

- 受領ZIP `LOCALSAPORTER_deploy_20261104_g4a_route_policy.zip`（SHA-256 `9EF9886D8294D7F06DF202E499657A6AFDE0643A39C72FE35EB19152ED434373`）の14ファイルを `hashes.txt` と照合し、差分を審査して適用。
- `app/project_manager.py::_outbound_guard` にPJ別の計画方式を確認する関門を追加。未設定・`local_only` は既存の漏えい検査へ進み、`external_guarded` は旧4経路を `method_external_guarded` で止める。方式の読取不能・不正値は `check_error` で止める。
- `app/goal_review.py` の目標達成ゲートの計画評価と `app/plan_review_loop.py` のGemini構造化支援に、送信前の `_outbound_guard` を追加。通らなければ送信承認保存・外部呼出回数加算より前に停止。既存の10試験ファイルには偽マネージャー用関門を追加し、新規 `tests/test_g4a_route_policy.py` で実関門を検証。
- 配布物のままでは、G1の `get_settings` が破損した保存方式値を `local_only` に補正してしまい、誤送信し得た。本番反映前に保存値を直接検査して不正なら停止するよう `project_manager.py` を補正し、実際に壊れた保存値を入れる回帰試験を追加。配布物と最終本番の2ファイルのハッシュ相違はこの安全補正による。
- 最終ホスト/コンテナSHA-256: `project_manager.py` = `4100E63675C9C07624217624917D612463BFBA99B04B842ED7751A89B0E3FD72`、`goal_review.py` = `BAE8EC9DC278A90951BA0BB97F0C3DC6F01F0FB581F050DF92C041C3802649F1`、`plan_review_loop.py` = `04E39757E799C3247C6195DDA8C7146FCA2D006DFC0D0AD0DDE7C3BF791E540E`。補正済み試験 = `BB20D98FAE99B8D7ABB5E2CBA96140ED0C910EC9C143B0A5F43EC99F5031D38F`。新Webイメージ `sha256:99b8794becfd8933ff762d0be2fea218a024e390bf31d4a997c4cca991c20fe3` はhealthy。

## 検証

- G4-a、G0-1、G1、目標達成ゲート、Gemini auto-loop等の関連試験: **170 passed**。ログ: `output/g4a_route_policy_focused_20261104.txt`。
- 全体試験（`PYTHONUTF8=1`、`PYTHONHASHSEED=3`、pytestプラグイン有効）: **2753 passed / 10 failed / 5 skipped**、711.09秒。失敗10件は直前G3-b本番試験と試験名が完全一致し、新規失敗0。全体合格ではない。ログ: `output/g4a_route_policy_full_20261104.txt`。
- `scripts/healthcheck.ps1` 全項目PASS。選択Ollamaモデル `qwen3.5:9b`。Web healthy。`SEBAS_EXTERNAL_PLAN_JOBS_PROJECTS` は空のまま。
- 実外部AI送信、実PJでの新関門・計画実行は行っていない。

## 運用影響・限界

- G4-aは機能フラグに関係なく有効。既存PJの `local_only` は従来の外部AI許可条件を維持し、本文がG2-1の拒否検査を通れば外部へ送信され得る。今回追加した2経路では、原本引用や禁止語が混じると送信保留になる。
- `external_guarded` に切り替えたPJでは旧4経路、目標達成ゲートの旧評価、Gemini auto-loopの外部呼出を停止する。新方式の独立評価はG4-b予定。既存の4経路へのpacket個別許可の接続は今回行っていない。
- 拒否リスト検査の通過は安全保証ではない。OCR Phase 5、車両損益の実案件受入、業務確認事項36項目は未完了。

## 保全・復旧

- 旧3アプリファイル、旧10試験ファイル、停止時の `/data/memory`、展開物を `backups/20261104_g4a_route_policy_final64/` に退避。旧Webイメージ `localsaporter-web:before-g4a-route-policy-20261104` = `sha256:151dcfba9836fbcf7265064ea22a95f02437d454ef65bd057b16679f2f1cea5c`。
- 復旧時はWebだけを停止して退避済みファイルを戻し、新規 `tests/test_g4a_route_policy.py` を除外して旧イメージで再作成する。データは影響確認なしに巻き戻さない。
