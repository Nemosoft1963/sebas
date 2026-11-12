# 2026-10-20 段階2「公開評価の正確化」本番反映記録

## 結果

受領ZIP LOCALSAPORTER_diff_20261020_stage2_public_disclosure.zip（SHA-256 710CF849B2D7F0AF9F875F79F479F91034097766A8DC64F0C040CA3CF02F4CFD）を確認し、app/goal_review.py、tests/test_p1c2d_safe_binding_resume.py、新規 tests/test_stage2_public_disclosure.py の3ファイルを反映した。ZIPに余分なエントリ、重複、パストラバーサル、設定・データの上書きはなかった。現行ソースとの差分は公開評価パケットの disclosure とテストだけである。

公開評価パケットの preparation_gate_disclosure で、曖昧だった check_count を削除し、preparation_check_count（準備工程のみ）と all_task_check_count（全工程合計）を分けた。内部契約から判定できない遷移・最終集約は unknown、disclosure_incomplete、理由で表し、非公開検査は内容を送らず件数と評価不能範囲を示す。外部送信、PJの計画・履歴、画面、OCR・車両損益の処理は変更していない。

## 復元点と反映

- 反映前ソース・テスト: backups/stage2_preapply_20261020_225513/app と tests
- Web停止中の /data コピー: 同フォルダーの app_data
- 反映前Webイメージ: localsaporter-web:pre-stage2-20261020、sha256:c70938315dfd2f04ecc7218bb968dbb91f7d542da915b5d207eefccbd4171123
- 反映後Webイメージ: sha256:54a9b32f2a2d22f3c08a8b17e1de4576abf4c832b73aad463e004dbc84961a0c
- 既存3ファイルCompose構成（docker-compose.yml、docker-compose.lan-off.yml、docker-compose.proxy.yml）でWebのみビルド・再作成。他のコンテナ、Docker volume、Ollamaモデル、既存PJを削除・初期化していない。--remove-orphans は使っていない。

## 検証

- 構文検査PASS、段階2・IssueBinding・P0ABの重点pytest 33 passed。
- ホスト全体pytestは openpyxl と pypdf がないため9ファイルの収集で停止（記録: temp/stage2_full_pytest_20261020.txt）。9ファイルを除いた広範囲試験は 2089 passed / 37 failed / 4 skipped / 5 warnings（temp/stage2_pytest_excluding_missing_deps_20261020.txt）。反映前も失敗数は37件であり、今回の変更に関係する重点33件はPASS。失敗一覧の完全一致や全体合格は主張しない。
- 新Webイメージ内で、架空計画の公開パケットに準備工程3件・全工程13件が別フィールドで現れ、旧 check_count がないことを確認。
- 本番APIの架空PJプレビュー: 準備工程3件・全工程32件、preparation_gate_required=true、disclosure_incomplete=false、外部送信なし。証跡は output/stage2_public_disclosure_20261020/preview_summary.json。パケットハッシュは変更され、旧評価・旧対応付けをそのまま現行版として使わない。
- scripts/healthcheck.ps1 全項目PASS。Web running/healthy、ポート制限PASS、選択モデル qwen3.5:9b。

## 限界と次の作業

外部AIに新しいパケットを実際に送った評価の改善は未検証。段階3の指摘→計画差分、段階4の実行到達、段階5の結果品質はこのZIPに含まれない。再試験で残った青空ひろばの対応先未確定、カフェ・FAQの改訂案独立検査不合格も解消していない。

観察点: preparation_gate_disclosure.check_kinds は従来どおり全工程の種別から作られている。今回の2つの件数とは範囲が異なるため、評価者が準備工程専用の種別と誤読しないか、次の公開パケット改善・外部実機評価で確認する。安全ゲートを緩めて数値を合わせない。

問題時はWebを停止し、上記バックアップの対象3ファイル（新規テストは削除せず退避）と旧Webイメージへ戻し、Webのみ再作成して healthcheck を実行する。/dataを戻す必要が生じた場合は現在のデータを別途保全し、停止中コピーと照合してから判断する。ボリュームを削除しない。

