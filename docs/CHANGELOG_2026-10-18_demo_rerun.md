# 2026-10-18 公開デモ再実行と指摘同一性修正

- 完全架空の公開デモ3件を、既存履歴を残して新しいPJで開始した。各PJへ架空要件書を登録し、構造化計画を生成。詳細は output/note_sebas_demo_20261018/RUN_RECORD_20261018.md。
- 自動評価ループで、指摘へ対応先メタデータを付与すると生成中の全文比較が誤停止する不具合を修正した。指摘の本文・順序・件数の変化は引き続き拒否する。対象: app/plan_feedback.py。
- tests/test_plan_feedback_binding_integrity.py を追加。関連回帰テスト45件成功。Webを再構築し、変更ファイルのホスト・コンテナSHA-256一致、scripts/healthcheck.ps1 全PASSを確認。
- 修正前のコード、Docker /data、Webイメージを backups/demo_20261018_pre_loop_fix と localsaporter-web:pre-demo-loop-fix-20261018 に保全。
- 3件ともGemini・Claudeの評価が conditional で実行開始不可。イベントは構造変更不足、カフェは修正案の形式不一致、備品申請は対応先の曖昧さと final_verification の追加開発判定で停止した。成果物生成、結果承認、経験RAG登録は未実施。
- GitHubへの反映は行っていない。

