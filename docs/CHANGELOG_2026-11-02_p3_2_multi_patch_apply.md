# 2026-11-02 P3-2 同一工程の複数パッチ適用 本番反映

## 反映内容

- 提供ZIP `LOCALSAPORTER_deploy_20261102_p3_2_multi_patch_apply.zip`（SHA-256: `EA4E93C523413D48CDD61DDEC7E1F0278CC60C3DDBA8BADA179EC3C000ACFBF8`）の3ファイルを `hashes.txt` と照合し、差分を確認して反映。
- `app/plan_repair_loop.py`: 同じ工程で承認された複数指摘の追記を承認順に統合する。元の工程説明を保てない候補は `PATCH_MERGE_FAILED` で適用前に止める。単一パッチの従来動作は維持。適用記録に指摘ごとの承認者・理由を `per_issue` として追加。
- `tests/test_p3_1_resolution_ledger.py` の3試験を修正し、`tests/test_p3_2_multi_patch_apply.py` を追加。残存確認台帳と再評価の実装は変更していない。
- ホストとコンテナの `app/plan_repair_loop.py` SHA-256は `2716CD588ED17FAA092E8187CEB7BC30C69A592E21EE33C27D2C63DABF73044E` で一致。試験ファイルは順に `B756ED55BFCD2BCA55974630393D99305C16E116687742465E944C17E9E1629E`、`63D37732B2914AF98AC9AB1CA8DE768DBA05210B3D73AAFE85C952D703794D5A`。
- Webイメージ `sha256:ea4692d5a04cb462a6cd1854fea1f26d4f11abc529d1a93dc8d4669105abf2aa` は healthy。

## 検証

- P3-2、P3-1、P2-1、修復ループの関連試験: **29 passed**。
- 本番全体試験: **2613 passed / 10 failed / 5 skipped**。失敗10件は直前P3-1本番全体試験と試験名が完全一致し、新規失敗は0。全体合格ではない。ログ: `output/p3_2_multi_patch_apply_full_20261102.txt`。
- `scripts/healthcheck.ps1`: 全項目PASS。選択モデルは `qwen3.5:9b`。
- 構造上の追記統合と残存を検査している。パッチが指摘を意味的に解決したかは人の確認と再評価が必要。既存の修復ランと計画は書き換えていない。実PJの通し実行は未実施。
- OCR Phase 5、車両損益の実案件受入、業務確認事項36項目は未完了のまま。OCR・RAG・承認の安全原則は変更していない。

## 保全・復旧

- 旧ソース、停止時の `/data/memory`、展開済みファイル、審査差分は `backups/20261102_p3_2_multi_patch_apply_final56/` に保全。旧イメージ `localsaporter-web:before-p3-2-multi-patch-apply-20261102` = `sha256:98ba7799c21d483bca7c9008c737fe9d37489df6192ce0a0b8feb227965930d7`。
- 復旧時はWebを停止し、退避した `app/plan_repair_loop.py` と `tests/test_p3_1_resolution_ledger.py` を戻し、今回追加の試験ファイルを除外して旧イメージで再作成する。停止時データは必要性と影響を確認するまで巻き戻さない。