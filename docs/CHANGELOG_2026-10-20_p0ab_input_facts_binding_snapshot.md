# 2026-10-20 P0-A/P0-B 本番反映記録

## 結果

受領ZIP LOCALSAPORTER_diff_20261020_p0ab_input_facts_binding_snapshot.zip（SHA-256 23C0A26DC9B8A3D4A1B6C581FD7EEED4D2A68A57AA42BBAB4CAAFAC49A72DDA2）の6ファイルを反映した。対象は app/project_manager.py、app/structured_planning.py、app/plan_feedback.py、app/plan_review_loop.py、新規 tests/test_p0ab_multi_llm_recovery.py、更新 tests/test_p1c2_issue_guidance_packet.py。ZIPにはこれ以外のエントリ、パストラバーサル、設定・データの上書きはなかった。

P0-Aは登録コンテキストをローカル計画へ渡し、計画保存前に日付・時刻・電話番号・金額の明示事実との矛盾と、複数ファイル成果物の統合を検査する。P0-Bは人が確定した指摘対応を修正案と被覆判定へ伝え、保存失敗時は未確定として停止する。

受領版への追加安全補正として、app/plan_feedback.py で、既にbindingが付いた指摘でも正本照合を省略せず、同一計画署名でも公開パケットまたは保存レビューが変われば古い確定を拒否するようにした。tests/test_p0ab_multi_llm_recovery.py に失効の回帰試験を追加した。受領ZIPと本番の最終ソースは、この補正の分だけ異なる。

## 復元点と適用

- 反映前ソース: backups/p0ab_preapply_20261020_3045772999ed437ba634bf19e65ed393/app と同 tests
- 停止中の /data コピー: 同バックアップフォルダーの app_data
- 旧Webイメージ: localsaporter-web:pre-p0ab-20261020（sha256:d8acbe4669ddbde06b1d70cacaa6429d88c2b1e30bd9e147c642cdcf7a59cbbc）
- 新Webイメージ: sha256:3484b0af7b4654ad3ce18b1c2d59838f991cc75af592fa3dfb4496bc8e02d8d1
- 3ファイルCompose指定でWebのみビルド・再作成。既存ボリューム、他サービス、Ollamaモデル、既存PJは削除・初期化していない。--remove-orphans は使っていない。

## 検証

- 関係テスト: 100 passed。追加した実経路の失効試験を含む重点再実行: 12 passed。
- ホスト全体テスト: openpyxl と pypdf が仮想環境にないため、無除外では9ファイルで収集停止。9ファイルを除く実行は 2083 passed / 37 failed / 4 skipped。失敗にはパッケージ手順書記載の旧fixture10件、依存不足、Windows文字コード・Node子プロセス・ワークスペース差が含まれる。全体合格とは記載しない。受領手順書の「2235 passed / 10 failed / 12 skipped」は提供者側の作業コピーでの結果であり、本番ホストで再現した値ではない。
- 新イメージの更新モジュールのimport成功。新Webコンテナ内で登録日付と計画日付の不一致が passed=False・矛盾1件となることを確認。
- scripts/healthcheck.ps1: 全項目PASS。Webコンテナ running/healthy、統合フロント HTTP 200、LANプロキシ healthy、ポート制限PASS。

## 残件と戻し方

P1（公開評価パケット・2回の修正閉ループ）、P2（モデル切替・判断画面）、P3（架空3件の実機再試験）は未実施。今回の反映だけで完走したとは判定しない。実案件・OCR実PDF受入・業務確認36項目も未完了。外部AIへの新規送信、人間承認、経験RAG登録は今回行っていない。

問題時はWebを停止し、旧ソースと旧イメージを上記復元点から戻し、同じ3ファイルCompose指定でWebのみ再作成する。データ復元が必要な場合は現在のデータを別途退避し、停止中に保存した app_data と照合してから戻す。Docker volumeを削除しない。

## 2026-10-20 実機再試験と補正

架空3案件で再試験。成果物件数の誤判定（質問5件を5ファイルと解釈）を修正し、LlamaでFAQ計画v1保存を確認。重点テスト19 passed、healthcheck全PASS。3案件ともClaude評価は条件付きで、改訂案は未成立。青空ひろばは対応先1件をSC00へ確定したが残る曖昧な指摘のため修正案生成409。カフェ・FAQは準備ゲートで原本未検証を成功扱いする改訂案を独立検査が拒否。未承認計画の実行開始も409で拒否。詳細は output/retest_p0ab_20261020/RETEST_REPORT.md。修正前ソースは backups/p0ab_before_count_fix_20261020、旧イメージは localsaporter-web:pre-p0ab-count-fix-20261020。
