# 2026-11-05 G5-b1 計画方式の変更画面

## 反映内容

- 配布元: `C:/Users/kanto/Short-video-local/stage4/final68/LOCALSAPORTER_deploy_20261105_g5b1_planning_settings_ui.zip`
- ZIP SHA-256: `731E8C3A98DF2810F78AAB8DB50F2BAF4BBD586BF63148BED7EF47E388927A74`
- `hashes.txt` にある4ファイルのハッシュを展開直後に照合した。`app/web.py` は静的配信ルート1件、`app/static/index.html` はスクリプトタグ1件の追加だけだった。
- G5-aパネル内へ、計画方式・生成AI・評価AIの設定変更欄を追加。保存は確認画面を経由し、方式変更時の注意を確認する。サーバーの既存 `PUT /api/projects/{id}/planning-settings` が保存と検証を担う。G5-b2の送信・送付内容承認・ジョブ作成は追加していない。

## 反映前の補強

1. 保存のHTTP 200応答でも、`settings` と `revoked` の形式が不正なら成功表示しない。変更結果を確認できない旨を表示する。
2. 保存済みの提供元が現在未接続なら、画面には「接続なし」と表示するが選択を無効にし、接続済みの提供元を選び直すまで変更ボタンを無効にする。確定ハンドラでも再確認する。
3. この条件と形式不正時の表示に合わせて、新規試験の期待値・試験用提供元を修正した。

## 検証

- 最終ファイルSHA-256: `planning_guard_settings.js` = `A6E052C4E9EC199C095823B499A7559C9C8DF757B24D27FDF96E608A198B0A16`、`index.html` = `99E55674F3A0352B87531F024D40F66E368CF969C3B3CCC72AA81FA26C144969`、`web.py` = `BDCED381192872BBA3A30903A81E7FE579457636FF87A2107AE526C6090B5402`、新規試験 = `16B521E2FBE0A687AAA1B90D4E0C01F5CC3BC13157D34C2F0D3FDFBDF698103C`。
- 本番と同じWebイメージの使い捨てコンテナでNode.js付き試験: `tests/test_g5b1_planning_guard_settings.py` と `tests/test_g5a_planning_guard_panel.py`、**37 passed / 1 warning / skipなし**。初回は試験用PJの提供元が未接続という試験前提で1件失敗し、前提修正後に全件合格。記録: `output/g5b1_planning_settings_focused_20261105.txt`。
- `scripts/healthcheck.ps1`: **全項目PASS**。記録: `output/g5b1_healthcheck_20261105.txt`。
- Web: `running / healthy`、新イメージ `sha256:db197f0512f09cf9ac10bdb874c777dd8824696eaa712a2a23c2768c0f439a0e`。`/static/planning_guard_settings.js` はHTTP 200。
- 全体試験と実ブラウザでの目視確認は今回未実施。前回の全体試験では既知の10件が失敗しているため、全体合格とは扱わない。

## 保全と巻き戻し

- 旧 `app/web.py` と `app/static/index.html` は `backups/20261105_g5b1_planning_settings_final68/old/` に保全。原配布物の展開版は同階層の `stage/` に保全。
- 旧Webイメージ: `localsaporter-web:before-g5b1-20261105`。
- 巻き戻す場合は旧2ファイルを保全先から戻し、新規JSのスクリプトタグが無いことを確認して旧イメージへ戻す。データの削除やボリュームの初期化は行わない。
