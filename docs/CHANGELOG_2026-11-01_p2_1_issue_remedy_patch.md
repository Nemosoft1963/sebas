# 2026-11-01 P2-1 指摘の修正案を修復候補へ反映 本番反映

## 反映内容
- 提供ZIP `LOCALSAPORTER_deploy_20261101_p2_1_issue_remedy_patch.zip`（SHA-256: `AE4F09E6AD28A87BCE5A00B6671942F39A2500583BADF55C46C613F354F75C58`）の3ファイルを `hashes.txt` と照合し、差分を確認して反映。
- `app/local_patch_preview.py`: 指摘の `remedy` を不信入力として正規化・長さ制限・禁止語判定し、反映可能な場合だけ既存工程の説明末尾に `指摘対応: <修正案>` を追記する。重複追記は避ける。候補には `patch_basis` と理由コードを付ける。業務事実・開発必須の分類や人間承認条件は変更しない。
- `app/plan_repair_loop.py`: 修正案の追記の構造確認を独立候補検査に加え、適用直前の再検査にも反映する。候補の公開ビューに根拠と不採用理由を出す。
- `tests/test_p2_1_issue_remedy_patch.py` を追加。外部AIの実送信や自動適用は行わない。
- ホスト/コンテナSHA-256: `app/local_patch_preview.py` = `92E79680CFB8EB86050C1CAC912797DDD0984904269256F0BFA2E2B881AB1800`、`app/plan_repair_loop.py` = `A5A44D0D5EF89C53F498DF9F91FA48D3C0E654A42E89451EFBD51E6859CADE1B`。試験ファイル = `9A29AD1398D1238496647EC7B32672718B0C44CB952762CE3C2E90EB3D1A8E84`。
- 稼働Webイメージ: `sha256:f95e8205ffd620df953499e49ed2b2582d82874f14253f6555c6a84a46c0f4e4`。Web healthy。

## 検証
- 新規・関連試験（`test_p2_1_issue_remedy_patch.py`、`test_p1_3_candidate_check.py`、`test_p1c_plan_repair_loop.py`）: **23 passed**。
- 本番全体試験の最終再実行: **2598 passed / 10 failed / 5 skipped**。失敗10件の試験名は直前の2026-10-31 P1-3本番全体試験と完全一致（新規失敗0）。全体合格とは記載しない。ログ: `output/p2_1_issue_remedy_patch_full_retry_20261101.txt`。
- 最初の全体実行は **2597 passed / 11 failed / 5 skipped** で、TRIZ試験 `test_time_and_cancel_checks_preserve_unadopted_state` が追加で409を返した。単独再実行では合格。TRIZファイル全体の実行では別の試験が待機時間超過となり、今回変更した2ファイルを反映前版に戻した隔離コピーでも同じ待機時間超過を再現した。TRIZ試験に実行順・時間依存の不安定さが残る。初回ログ: `output/p2_1_issue_remedy_patch_full_20261101.txt`。
- `scripts/healthcheck.ps1`: 全項目PASS。Ollama選択モデルは `qwen3.5:9b`。
- 禁止語は拒否リストで網羅的ではない。`check.passed=True` は構造上の安全確認であり、修正案が指摘を意味的に解決した証明ではない。人の承認と適用後の再評価が必要。
- 実PJでの外部AI指摘から修復候補適用までの通し実行は今回未実施。
- OCR Phase 5、車両損益の実案件受入、業務確認事項36項目は未完了のまま。OCR・RAG・承認の安全原則は変更していない。

## 保全・復旧
- 旧ソース、停止時の `/data/memory`、ZIP展開済みファイル、審査差分は `backups/20261101_p2_1_issue_remedy_patch_final54/` に保全。旧イメージ `localsaporter-web:before-p2-1-issue-remedy-patch-20261101` = `sha256:4d0379073e205e44b67916c872c663de2432c7415273f088198645529be6415a`。
- 復旧が必要ならWebを停止し、退避した2ソースを戻し、追加試験ファイルを今回の反映対象から外し、旧イメージでWebを再作成する。停止時データは必要性と影響を確認するまで巻き戻さない。
