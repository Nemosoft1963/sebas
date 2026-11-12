# 2026-10-31 P1-3 修復候補の独立検査 本番反映

## 反映内容
- 提供ZIP `LOCALSAPORTER_deploy_20261031_p1_3_candidate_check.zip`（SHA-256: `593A40F86E034B89A7F5397B81B4880E33528ECFDF0DAF4DA6BD7193E0926A07`）の2ファイルを `hashes.txt` と照合し、差分を確認して反映。
- `app/plan_repair_loop.py`: 修復候補の `before` が現行工程と一致するか、`after` が依存・契約を変えたり説明を削ったりしないか、無効果でないか、構造・被覆・要求保持の検査を悪化させないかを独立に確認する。候補ビューに `check.passed` と `check.codes` を追加。検査不能は `check_error` として不合格、パッチなしはスキップで通過扱いにしない。
- 承認済み候補は適用直前に現行計画で再検査し、不合格なら `replace_plan` 前に拒否する。開始時の結果を使い回さない。
- `tests/test_p1_3_candidate_check.py` を追加。既存の計画・開始済み修復ランは再生成しないが、開始済み候補も適用時の再検査を受ける。
- ホスト/コンテナSHA-256: `app/plan_repair_loop.py` = `224C922C8C06F01AE8091F494C8571DCED19206D0B14702EB9D6C80151502C11`。試験ファイル = `9794021AFC997A3D597028A1C0B1E37C9AC149998BFC84C4E4A0F19C78A0EEBC`。
- 稼働Webイメージ: `sha256:4d0379073e205e44b67916c872c663de2432c7415273f088198645529be6415a`。Web healthy。

## 検証
- 新規・関連試験（`test_p1_3_candidate_check.py`、`test_p1c_plan_repair_loop.py`、`test_p1_1_confirmed_binding_repair.py`）: **19 passed**。
- 本番全体試験: **2589 passed / 10 failed / 5 skipped**。失敗10件の試験名は直前の2026-10-31 P1-2本番全体試験と完全一致（新規失敗0）。全体合格とは記載しない。ログ: `output/p1_3_candidate_check_full_20261031.txt`。
- `scripts/healthcheck.ps1`: 全項目PASS。Ollama選択モデルは `qwen3.5:9b`。
- この検査は構造上の安全確認であり、候補のパッチが指摘内容を意味的に解決するかは判定しない。`check.passed=True` を「問題解決済み」と扱わない。
- 実PJでの候補生成・人間承認・適用の通し実行、外部AI実評価は今回未実施。
- OCR Phase 5、車両損益の実案件受入、業務確認事項36項目は未完了のまま。OCR・RAG・承認の安全原則は変更していない。

## 保全・復旧
- 旧ソース、停止時の `/data/memory`、ZIP展開済みファイル、審査差分は `backups/20261031_p1_3_candidate_check_final53/` に保全。旧イメージ `localsaporter-web:before-p1-3-candidate-check-20261031` = `sha256:bfa7dbeae2dddfaaa1c8a2dfd7c42202764aed5d303f5083629a62336f3ee0a2`。
- 復旧が必要ならWebを停止し、退避した `app/plan_repair_loop.py` を戻し、追加試験ファイルを今回の反映対象から外し、旧イメージでWebを再作成する。停止時データは必要性と影響を確認するまで巻き戻さない。
