# 2026-10-30 P1-1 確定済み対応の修復候補への反映 本番反映

## 反映内容
- 提供ZIP `LOCALSAPORTER_deploy_20261030_p1_1_confirmed_binding_repair.zip`（SHA-256: `0F433C324E5FD4154482A78AF62B5980DAA71BF815D1146D00CC2ADE1028BDBD`）の3ファイルを `hashes.txt` と照合し、差分を確認して反映。
- `app/local_patch_preview.py`: 現行計画署名・目標契約・packet・レビュー指紋に一致する正本スナップショットの確定済み指摘だけを、確定先の達成条件と工程へ結合する。無効・欠落・重複・不在の対応先は確定結合せず、従来のテキスト照合に戻す。分類・自動適用可否は変更しない。
- `app/plan_repair_loop.py`: 修復候補の公開・保存ビューへ `binding_source` を追加。`confirmed_snapshot`、`text_match`、`unbound` の区別を伝える。
- `tests/test_p1_1_confirmed_binding_repair.py` を追加。書き込みや外部AI送信はこのプレビュー処理から行わない。
- ホスト/コンテナSHA-256: `app/local_patch_preview.py` = `B3385DDAA24697034BEC5145C99A826D175020DB8C5EED0C62378EDAD2FB1C16`、`app/plan_repair_loop.py` = `1E8A22AC7EE6FD8C252F343F511B59843200FBC9E3DF6EB602689614C02344DA`。試験ファイル = `7E20D5F066EC99C544C37BB858D779BEA4A87756BFA0334295C5F51AD1B5C580`。
- 稼働Webイメージ: `sha256:865802e408f8611619ce6eda6543cafba08dd048823c266c6d09ccdfc0c5abbb`。Web healthy。

## 検証
- 新規・関連試験（`test_p1_1_confirmed_binding_repair.py`、`test_plan_feedback.py`、`test_p0_2_source_read_wiring.py`）: **36 passed**。
- 本番全体試験: **2571 passed / 10 failed / 5 skipped**。失敗10件の試験名は直前の2026-10-30 P0-2本番全体試験と完全一致（新規失敗0）。全体合格とは記載しない。ログ: `output/p1_1_confirmed_binding_repair_full_20261030.txt`。
- `scripts/healthcheck.ps1`: 全項目PASS。Ollama選択モデルは `qwen3.5:9b`。
- 実PJでの指摘確定から修復候補適用までの通し実行、外部AIの実評価は今回未実施。既存の修復候補は自動再生成しない。
- OCR Phase 5、車両損益の実案件受入、業務確認事項36項目は未完了のまま。OCR・RAG・承認の安全原則は変更していない。

## 保全・復旧
- 旧ソース、停止時の `/data/memory`、ZIP展開済みファイル、審査差分は `backups/20261030_p1_1_confirmed_binding_repair_final51/` に保全。旧イメージ `localsaporter-web:before-p1-1-confirmed-binding-repair-20261030` = `sha256:3c803e135afbc32f87cdfffb75e4e6a1151e61bfb87402afff1e7bac84a9e381`。
- 復旧が必要ならWebを停止し、退避した2ソースを戻し、追加試験ファイルを今回の反映対象から外し、旧イメージでWebを再作成する。停止時データは必要性と影響を確認するまで巻き戻さない。
