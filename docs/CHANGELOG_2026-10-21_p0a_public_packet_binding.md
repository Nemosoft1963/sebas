# 2026-10-21 P0-A 公開packetの束縛を本番反映

## 目的と反映範囲

公開用の具体的な目的説明を入力・確認した後、その説明と公開構造から作る最終packetのSHA-256を外部評価の開始時および送信直前に照合する。空の説明や汎用文、秘密混入、構造・成果物スロット不整合は送信前に停止する。非公開情報による評価不能範囲は `unverifiable` として残し、成功扱いにしない。

受領ZIP `LOCALSAPORTER_diff_20261021_p0a_public_packet_binding.zip`（SHA-256: `5D065312B9EFF8E478C3CB10E632146EF0B87BA053BD8E0CCC66EA99A3EBD493`）の本体4ファイル・テスト8ファイルを反映。差分には以下を追加補正した。

- `app/plan_review_loop.py`: 自動評価の各回で外部AI呼出直前のpacketハッシュを `review_plan` に渡して照合。初回はプレビュー済みハッシュとの一致を要求。2回目は自動修正後の計画署名とpacketを結び、実送信ハッシュを記録する。
- `app/static/plan_review_loop.js`: 開始不可理由の表示を修正。公開説明の編集時に前回プレビューを無効化して再確認を案内する。
- `app/static/index.html`: 新JSを既存ブラウザが読み直すようクエリ版番号を `20261021-p0a-public-packet` に変更。
- `tests/test_p0a_public_packet_binding.py`: 開始後にpacketが変わった場合に外部AIへ送らない回帰試験を追加。
- `tests/e2e/test_ui_smoke.py`: 新しい説明欄と目標計画タブでの再プレビュー案内を読み取り専用で確認。

## 退避と配備

- ソース・テスト・旧報告書: `backups/p0a_public_packet_preapply_20261021/`
- 反映前イメージ: `localsaporter-web:pre-p0a-public-packet-20261021`（`sha256:ac247b197536d3af11345b6f0da5de83558c848d448daa1f0c681e15c89604b5`）
- 反映前データボリューム: `localsaporter_app_data_pre_p0a_20261021`（退避確認時72.8MB）
- 本番イメージ: `localsaporter-web:p0a-public-packet-20261021`（`sha256:28b6a873b921e14f3193f5dbc87f7dcebfec7b48cc07bc61a5af12ab1b654a6c`）。既存イメージを土台に変更ファイルだけを載せ、Python依存を変更していない。
- 起動構成: `docker-compose.yml`、`docker-compose.lan-off.yml`、`docker-compose.proxy.yml` の3ファイルを維持し、Webのみ再作成。OCRコンテナ・データ・Ollamaモデルは変更していない。

## 検証

- ZIP関連の分離環境試験: 236 passed。送信直前の追加照合後、P0-Aとauto-loopの重点試験35 passed。
- 本番ルート全体: **2269 passed / 12 failed / 5 skipped**（`output/p0a_public_packet_full_20261021.txt`）。前回P0-0の失敗12件とテスト名が完全一致し、新規失敗0件。全体合格ではない。
- 反映後 `scripts/healthcheck.ps1`: PASS。Web healthy、トップ画面とJSはHTTP 200。コンテナ内の変更本体4ファイルのSHA-256は反映ソースと一致。
- 実ブラウザの読み取り専用E2E: 1 passed。公開説明欄の表示、タブ切替、編集時の再プレビュー案内、開始ボタン無効を確認。外部送信とPJ変更は行っていない。

## 残る制限

- 既知の12失敗（目標判定7、PJ生成・バックアップ2、項目検証1、実行漏斗1、統合1）は未修正。
- 実PJの公開packetが送信前整合検査を通るか、および外部AIの評価品質が改善するかは未検証。外部AIへの新規送信は行っていない。
- 手動の単発 `review_plan` は期待ハッシュ未指定時に `hash_binding: not_provided` と記録する既存仕様。プレビュー必須の束縛はauto-loop経路で適用。
- P0-Aの項目4（指摘分類）・P0-B以降はこの反映に含まない。

## 復旧方針

問題が出た場合はWebだけを停止し、退避した旧イメージを `localsaporter-web:latest` に戻して同じ3ファイルComposeでWebだけを再作成する。ソースは上記バックアップの対応ファイルから復元する。データの巻き戻しが必要な場合だけ退避ボリュームを照合してから個別に復元する。`docker compose down -v`、全体pruneは使わない。