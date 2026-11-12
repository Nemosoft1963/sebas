# 2026-11-05 G5-a 計画方式・外部AI送信の閲覧パネル 本番反映

## 反映内容

- 受領ZIP `LOCALSAPORTER_deploy_20261105_g5a_planning_guard_panel.zip`（SHA-256 `DA8F6C6F23B9562CE541EF34936D03959DDC479355ACEA377887AF12E9A26516`）の3ファイルを `hashes.txt` と照合した。
- `app/static/planning_guard_panel.js` を追加し、PJ別の計画方式、実行可否、外部計画ジョブ、独立評価ジョブ、送付監査をGETのみで表示。入力欄・ボタン・外部送信は追加していない。`app/static/index.html` に読込タグを追加。
- 配布物の `local_only` 表示「外部へ送らない」は現行G4-aと矛盾する。従来経路は別条件で外部送信できるため、「ローカル計画方式（外部送信の可否は別設定）」へ訂正した。評価指摘の分類、監査の用途、未知の内部コードも日本語表示または「確認できず」に補正した。
- 配布物には新JSのサーバールートがなく、本番HTTPでは404だった。既存の静的ファイルと同じ `GET /static/planning_guard_panel.js` を `app/web.py` に追加。配信の回帰試験を追加。
- 配布物の初期配置では、パネルが非表示の「概要」詳細内に挿入され、実画面に出なかった。`#workflow` の直後に配置するよう補正した。実画面で可視・内容あり・パネル内の操作要素0件を確認した。
- 最終SHA-256: `app/static/planning_guard_panel.js` = `5DB9E83EFF5F91BFB1EF808C44279C9E0A5D2CA5521A5922E8F69F89FA462FA4`、`app/static/index.html` = `36C79579AA31974257CDB2759288AD8B192C931AD1B4A1F1839ABA516A07AB98`、`app/web.py` = `897FEFDB2CEF2FD6CA1EDA172E427882E66ACD59F6E27B66372C4FAF22BFDF70`、`tests/test_g5a_planning_guard_panel.py` = `75E3E03647DCB69144303BE0F79C65481467E17677BBC7BE4451747DDCC3311F`。ZIPと異なるハッシュは上記補正による。ホストとコンテナの主要3ファイルは一致。

## 検証

- `scripts/healthcheck.ps1` 全項目PASS。Web/プロキシ healthy、選択Ollamaモデル `qwen3.5:9b`。新Webイメージ `sha256:5b8301dec0a7ffa61a325f83eec780d81f9779bac9c8b83e906fda4057b77288`。
- G5-aと既存問い合わせ画面の関連試験: **22 passed、警告1**。Node.jsのDOM描画・PJ切替試験を含み、スキップなし。`output/g5a_planning_guard_panel_focused_20261105.txt`。
- 本番HTTP: `/` と `/static/planning_guard_panel.js` が200。実ブラウザでパネルの表示と読込完了を確認。初期選択PJは開始不可、ジョブなし、監査記録なしと表示された。
- 全体回帰試験 (`PYTHONUTF8=1`, `PYTHONHASHSEED=3`): **2810 passed / 10 failed / 5 skipped、警告1、702.17秒**。前回G4-cとの失敗試験名の差分は0。新規失敗なし、全体合格ではない。`output/g5a_planning_guard_panel_full_20261105.txt`。
- 最初のヘルスチェックでは、Docker/Ollama CLIの一時的なメモリ不足によりモデル一覧3項目が失敗。その後Docker Desktopが停止し、既存Desktopを再起動して復旧した。再実行のヘルスチェックは全項目PASS。データ/ボリュームの削除・初期化はしていない。

## 限界

- ジョブ一覧のGET APIはフラグ未設定でも空一覧を返すため、画面の「ジョブなし」は機能有効の証明ではない。外部計画ジョブ・独立評価ジョブのフラグは未設定のまま。
- 「計画方式」はPJ別設定の表示であり、従来の外部AI許可や送信経路をすべて代表しない。
- この変更は閲覧専用。計画方式の切替・packet承認・ジョブ作成はG5-b以降。
- OCR Phase 5、車両損益の実案件受入、業務確認事項36項目は未完了。

## 保全・復旧

- 旧 `app/static/index.html`、旧 `app/web.py`、停止時の `/data/memory`、隔離展開物を `backups/20261105_g5a_planning_guard_panel_final67/` に退避。旧イメージ `localsaporter-web:before-g5a-panel-20261105` = `sha256:40e1f1b9f727d68ba52c38e609d10a9813b1688b6b548163b8d27ed09e582f4f`。
- 復旧時はWebのみ停止して退避2ファイルを戻し、新規パネルJSと試験を除外して旧イメージで再作成。保存データは影響を調べずに巻き戻さない。

