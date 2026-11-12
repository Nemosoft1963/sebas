# 2026-10-28 D1 指摘対応付けの確定継承

## 反映
- 提供ZIP `LOCALSAPORTER_deploy_20261028_d1_binding_fingerprint.zip`（SHA-256: `F6F2E6DC754E38BD568889D0C94A9EEC5B51C70AD0625A1EE388D91389A92209`）を審査し、3ファイルの添付ハッシュを照合して反映した。
- `app/plan_feedback.py` と `app/plan_review_loop.py` を更新し、`tests/test_d1_binding_review_fingerprint.py` を追加。レビュー指紋を provider/status/issues で統一し、版なし旧正本の継承と安全な診断コードを追加。
- 追加審査で、同じ計画署名の過去ランに異なる指摘がある場合でも旧確定を継承し得ることを発見。現在のレビュー内容と一致する候補だけを継承し、未知の指紋版を拒否する補正と2試験を追加した。任意の旧指紋引数では照合を迂回できない。
- 最終ホスト/コンテナの `app/plan_feedback.py` SHA-256 は `A188C295695E5FDED8A3541F1E8387E09EF20D8F75B42EDE1A5FE55AA33B10E9`。`app/plan_review_loop.py` は `956EC6B5BC69C5FD6FAE9CADA93BC259FBBD5EE5A5CFEC6129E3138B3C343A00`。最終Webイメージは `sha256:0d612c0a21b68b4ea63a6d823017caeae03d3cadff2c4b701ccd7004bd07f8ae`。

## 検証
- D1受入試験: **9 passed**（添付7件＋安全補正2件）。
- 周辺の安全な再開・対応付け・整合性試験: **22 passed**。
- Python構文検査、稼働イメージ内の異なる旧レビュー拒否検査: PASS。
- `scripts/healthcheck.ps1`: 全項目PASS。Webは healthy、Ollama選択モデルは `qwen3.5:9b`。ログは `output/deploy_d1_binding_20261028/healthcheck.txt`。
- 全体試験は本番で再実施していない。提供側の全体結果（2528 passed / 10 known failed / 17 skipped）は本番結果として数えない。
- 保存済みの架空PJ `54838352d41f486fad655bb30c6007fd` の手動確定3件と外部呼出1回は保持。旧ラン `0c89ab2481254cb39df818409c05ea29` は `needs_attention` 停止で、再対応付けAPIは `human_required` に限定されるため、そのままの実ラン再試行は不可。強制変更や外部AI再送はしていない。旧ランの復旧・完走を確認したとは記載しない。

## 保全・復旧
- 旧ソースと停止時の `/data/memory` は `backups/20261028_d1_binding_fingerprint_final48/` に退避。旧イメージのタグは `localsaporter-web:before-d1-binding-fingerprint-20261028`。
- 復旧が必要な場合は、Webを停止し、退避ソース2件を戻して旧イメージタグを `localsaporter-web:latest` に戻し、`docker compose -f docker-compose.yml -f docker-compose.lan-off.yml -f docker-compose.proxy.yml up -d --no-build --no-deps web` で再作成する。データの差戻しは必要性と影響を確認してから行う。
- OCR・車両損益・RAGの安全原則は変更していない。OCR Phase 5、実案件受入、業務確認事項36項目は未完了のまま。