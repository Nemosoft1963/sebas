# 2026-10-25 本番反映: 問い合わせ窓口・発信媒体・公開IDの登録

## 適用内容

受領ZIP `LOCALSAPORTER_deploy_20261025_contact_channel_registry.zip`（SHA-256 `BFCBD8014D164BBF37CA5D259B1FAD3368C8B938D4CFA21817EE4FB26C90D40B`）を審査し、27ファイルを本番ソースへ反映した。内訳は既存16ファイルの変更と新規11ファイル。ZIP内の27件は添付 `hashes.txt` とすべて一致した。運用データ、既存プロジェクト、Ollamaモデル、Compose設定は置き換えていない。

- 問い合わせ方法、SNS選択、公開IDを出所・版・失効状態とともに追記型台帳へ保存。認証情報に見える値は拒否する。
- 運用区分を架空試験と実運用で分け、未設定時は公開用途として厳しく判定する。登録した架空の問い合わせ先は架空試験でのみ使用可能。
- 選択媒体だけのSNS文案を生成し、公開承認時の登録版・ファイルハッシュに束縛する。変更後は再承認を要する。
- 実行サイクル当時の判定・残件を保存し、現在の状態とは分けて表示する。
- UIの「発信・問い合わせ設定」で運用区分、窓口、SNS、公開ID、事前確認、束縛差分を操作できる。

## 本番審査時の追加補正

1. `app/workspace_files.py`: Windowsの深い試験パスで再生成時のバックアップ名が長くなり、`WinError 3` で失敗する問題を修正。元ファイルの相対パスSHA-256で短い退避名を作り、復元に使う退避先は戻り値で保持する。`tests/test_workspace_files.py` に深いパスの回帰試験を追加。
2. `app/requirement_matrix.py`: 実Qwenの架空案件で「終了後確認を含める」が抽出漏れ疑義となる事象を確認。要求動詞を除いた核が同じ見出しで検査されている場合、抽出済みと認識するようにした。核語化で行数が変わっても他の要求「場所」「対象」を消さず、残った命令文の必須語句だけ核へ置換する。`tests/test_a1_requirement_contract.py` に再現試験を追加。P0-Bの修復ループ試験も維持。

3. `app/publication_binding.py`: 文案の人間承認と、公開用の値の確認済み状態は別とし、投稿画面を開く直前・公開URL登録の直前に公開用途の事前確認を再実行する。未確認の連絡先・公開IDが残る場合は `blocked`、台帳を読めなければ `unreadable` として止める。`tests/test_b2_publication_binding.py` で未確認の窓口・公開IDの両方を受入検査。

## 退避と復旧

- ソース・テスト・稼働データ: `backups/20261025_contact_channel_registry_final46/`。DB3件のバックアップは `PRAGMA integrity_check=ok`。
- 旧Webイメージ: `localsaporter-web:before-contact-registry-20261025`（`sha256:0f73678d0d4e8911204086fd442e8908779fa848ca3a44c42f274bc9fb451692`）。
- 問題時はサービスを停止し、退避したソースとデータを戻し、旧イメージでWebを起動する。`docker compose down -v` や orphan コンテナの削除は行わない。新表は旧版から参照されない。

## 確認結果

- 関連テスト: 137 passed、1 skipped。新規の深いパス試験と要求抽出試験を含む。
- 全体テスト（`PYTHONHASHSEED=3`）: **2464 passed / 12 failed / 5 skipped**。失敗12件の内訳は前回final45と同じ7件の `test_generic_goal_checks`、2件の `test_l2_project_generation`、各1件の `test_plan_item_runtime_repairs`・`test_s4a_execution_funnel`・`test_section8_integration`。全体合格ではないが、今回の変更に起因する新規失敗は確認されなかった。全ログ: `output/local_llm_autonomy_20261025/postdeploy_final46/full_pytest.txt`。
- `scripts/healthcheck.ps1`: 全項目PASS。Webとプロキシは healthy、選択モデルは `qwen3.5:9b`。
- 実ブラウザ: `/` と `/cowork` がHTTP 200。既存の公開デモPJを読み取り専用で開き、「概要」→「状態・判断根拠の詳細」内の「発信・問い合わせ設定」を確認。未設定時の案内、各入力欄、使用SNS、公開前の不足を表示した。スクリーンショット: `output/local_llm_autonomy_20261025/postdeploy_final46/ui_contact_panel.png`。
- 実Qwen架空PJ: ローカルの隔離領域で運用区分「架空試験」、架空メール、Xのみ、架空IDを登録して再走。計画生成・承認・3タスク実行・案内文とチェックリストと最終検証書の保存を確認。実行サイクルは `execution_state=finished`、目標は `undetermined`、`pending_kind=human_decision`。当時の検証はPASS 8 / FAIL 0 / UNTESTABLE 4、残件4（出典登録1、人判断3）を追記型に保存し、現在の残件と分離表示した。目標達成ではない。詳細: `output/local_llm_autonomy_20261025/postdeploy_final46/results.json`。外部AI、SNS、メールへは送信していない。

## 残課題

架空PJの計画契約で `SC01-R01` と `SC01-R03` の項目ID重複を観測した。前回final45の隔離PJにも `SC01-R02` の重複があり、今回導入固有ではない。検証台帳と残件をIDで参照するときに曖昧さが残るため、全項目追加・正規化後の一意な採番と指摘対応付けの回帰試験を次の改修対象とする。今回の残件4件を自動承認しない。

## 運用上の注意

既存PJの運用区分が未設定なら公開用途とみなされ、架空の問い合わせ先は検証に使えない。架空試験には区分と架空窓口を登録する。既存のSNS承認は新しい束縛がないため、投稿画面を開く前にキット再生成と再承認が必要。SNSのアカウント作成・ログイン・自動投稿は今回の範囲外。
