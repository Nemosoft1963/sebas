# 2026-10-23 実行サイクル完走 P0-3 / P0-1 / P0-2 / P1-1 / P1-2 本番反映

## 反映結果

受領した `LOCALSAPORTER_diff_20261023_execution_cycle_P03_P01_P02_P11_P12.zip` の21ファイル（既存15、新規6）を本番ソースへ適用し、各ファイルのSHA-256一致を確認した。ZIPのSHA-256は `AD29D37655B5E80A9E2166D81DA82D2AED6A38AA3958936B00EAAC2917B561CD`。反映中はセバスの5コンテナを停止したままにし、3ファイルCompose構成でWebイメージを再ビルドして起動した。`--remove-orphans`、ボリューム削除、PJ初期化は行っていない。外部AI送信は行っていない。

- 新Webイメージ: `sha256:e7865f2e2a20dc6ba73a85ab8815b789fc64b893a0e2cda5ba2e4addd769f31b`
- 旧Webイメージ: `localsaporter-web:before-execution-cycle-20261023`（`sha256:e46ae53e0ce617037e6a154d51203aa9a3aa21d16b6a7c79ff2a985faabd092d`）
- 変更前ソース15ファイルと停止時の全`/data`（約68 MB、項目別台帳DBを含む）: `backups/20261023_execution_cycle_final44/`
- バックアップ台帳DBの `PRAGMA integrity_check`: `ok`。

## 検証

- ZIP内Pythonの構文検査PASS、画面JavaScriptの `node --check` PASS。
- 関係テストは、Windowsの文字コード差を避けるため `PYTHONUTF8=1` で **51 passed**。既定CP932での初回はNode試験用一時ファイルの文字化けにより1件失敗したが、同一ソースをUTF-8で再実行するとPASS。
- ホスト全体テスト（`PYTHONUTF8=1`、`PYTHONHASHSEED=3`）は **2360 passed / 12 failed / 5 skipped**。12件の失敗を変更前ソースの隔離コピーで個別再実行し、**同じ12件すべてを再現**。今回の21ファイルに起因する新規失敗は確認されなかった。提供者側の「2353 passed / 10 failed / 14 skipped」と数値は一致しないため、全体テスト合格とは記さない。
- 新API `GET /api/projects/{project_id}/runs/latest` は、既存PJに `no_run_history` を返すことを確認。旧PJを推測で完走扱いにしていない。
- `scripts/healthcheck.ps1` は、起動直後の一時的なOpen WebUI・プロキシ未準備を除き、再実行と架空PJ試験後の最終実行で **全PASS**。Web、プロキシ、Open WebUI、Computer、Publisher Browserの5コンテナが稼働中。

## 実Ollamaでの架空PJ受入

既存試験結果を上書きしない `output/local_llm_autonomy_20261023/postdeploy_final44/` に、架空「青空ひろば」PJを隔離して新規作成し、`qwen3.5:9b` で計画生成、架空PJ内の試験承認、SC00/SC01実行、最終検証まで実走した。実PJ・本番PJのデータは変更していない。

- 実行サイクル: **`finished`**。目標判定: **`unmet`**。残件: **9件**。時間切れなし。
- 最新項目別台帳: 24行中 **PASS 15 / FAIL 3 / UNTESTABLE 6**。台帳は絶対UNCパスから読取成功、隔離DBの `PRAGMA integrity_check=ok`。
- 旧`mission.status`は`paused`、最終報告ステータスは未設定。画面の新しい「今回の実行」と目標判定は別軸で解釈する必要がある。
- 未達・要確認を達成、承認、RAG登録へ読み替えていない。実ブラウザ上でこの隔離PJを表示する試験は行っていない。

## 残る課題

1. 日時の`00分`・`30分`を原本外数値とする誤検出が残る。原本にない年齢条件は正しく要確認のまま。
2. 準備時刻の対象成果物と、`「架空の問い合わせ先を含め」`・`「終了後確認を含める」`という要求文の語句照合でFAILが残る。計画・検証契約生成をさらに確認する必要がある。
3. 文意判定で引用不一致の人判断待ちが残る。実走中に`ProjectOrchestrator._local_complete`の未待機コルーチン警告が2か所で発生した。検証・修復の非同期呼出しを追跡する。
4. 実走では`verification_ref`・`verification_hash`が空のまま。完走の記録はできたが、検証書との束縛は別途確認が必要。

この反映の主目的「目標未達でも安全に実行できる処理を終え、残件を残して実行サイクルを閉じる」は、架空PJの実走で確認した。目標達成や成果物品質の合格は確認していない。

