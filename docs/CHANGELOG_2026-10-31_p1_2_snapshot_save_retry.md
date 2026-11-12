# 2026-10-31 P1-2 正本スナップショット保存のローカル再試行 本番反映

## 反映内容
- 提供ZIP `LOCALSAPORTER_deploy_20261031_p1_2_snapshot_save_retry.zip`（SHA-256: `F24791218518853B0C1B796AC2955C815F618F4B11447A8F56385C60A3F70357`）の3ファイルを `hashes.txt` と照合し、差分を確認して反映。
- `app/plan_review_loop.py`: 人が確定した指摘の正本保存に失敗した場合、現行計画・契約・packet・保存済みレビューを再照合し、保存だけを1回再試行する。保存失敗の対象がない場合は拒否し、古い確定を現行の正本へ移さない。外部AIの再送や利用枠の消費は行わない。再試行回数と成否を公開ビューに追加。
- `app/web.py`: `POST /api/projects/{project_id}/goal-review/auto-loop/{run_id}/binding-snapshot-retry` を追加。UIボタンは今回の対象外。
- `tests/test_p1_2_snapshot_save_retry.py` を追加。既存の確定・未確定の内容は再試行で書き換えない。
- ホスト/コンテナSHA-256: `app/plan_review_loop.py` = `EAED51BAF8072CC1F56DBF9D41A9ACEF7FD26B04AB2CD2F0E1B255B258C93674`、`app/web.py` = `6DC11F3A6B8792B32B40BC4BA82D40E2DBE26667EB7D893C8A7D4D90A61FEA03`。試験ファイル = `86571BCB9B9D1D7E83B7E81E8F8862A1981C44FABAC4F397E732704685BB8160`。
- 稼働Webイメージ: `sha256:bfa7dbeae2dddfaaa1c8a2dfd7c42202764aed5d303f5083629a62336f3ee0a2`。Web healthy。

## 検証
- 新規・関連試験（`test_p1_2_snapshot_save_retry.py`、`test_plan_feedback.py`、`test_p1_1_confirmed_binding_repair.py`）: **30 passed**。
- 本番全体試験: **2580 passed / 10 failed / 5 skipped**。失敗10件の試験名は直前の2026-10-30 P1-1本番全体試験と完全一致（新規失敗0）。全体合格とは記載しない。ログ: `output/p1_2_snapshot_save_retry_full_20261031.txt`。
- `scripts/healthcheck.ps1`: 全項目PASS。Ollama選択モデルは `qwen3.5:9b`。
- 実PJで保存失敗を起こしてからAPI再試行する通し実行、外部AIの実評価は今回未実施。
- 提供手順書にある限界: `confirm_binding` のレビュー指紋計算が保存ブロックの外に移ったため、そこで想定外の例外が起きると保存失敗として記録されず伝播する。通常経路では未再現だが、次の改善対象として残す。
- OCR Phase 5、車両損益の実案件受入、業務確認事項36項目は未完了のまま。OCR・RAG・承認の安全原則は変更していない。

## 保全・復旧
- 旧ソース、停止時の `/data/memory`、ZIP展開済みファイル、審査差分は `backups/20261031_p1_2_snapshot_save_retry_final52/` に保全。旧イメージ `localsaporter-web:before-p1-2-snapshot-save-retry-20261031` = `sha256:865802e408f8611619ce6eda6543cafba08dd048823c266c6d09ccdfc0c5abbb`。
- 復旧が必要ならWebを停止し、退避した2ソースを戻し、追加試験ファイルを今回の反映対象から外し、旧イメージでWebを再作成する。停止時データは必要性と影響を確認するまで巻き戻さない。
