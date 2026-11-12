# 2026-11-01 P3-1 適用パッチの残存確認（解決確認台帳）本番反映

## 反映内容
- 提供ZIP `LOCALSAPORTER_deploy_20261101_p3_1_resolution_ledger.zip`（SHA-256: `DCE0470D573C4A13CB900615ECCE622922929226A2EB01103ACD690CDD06DCE8`）の2ファイルを `hashes.txt` と照合し、差分を確認して反映。
- `app/plan_repair_loop.py`: 適用した指摘ごとに、現行計画にパッチの追記が残るかを判定する `resolution_ledger` を追加。再差分評価時に台帳を保存し、`passed` 遷移時には現行計画で再計算して、残存しないパッチがあれば `PATCH_NOT_RETAINED` で止める。案件カードには「解決確認」を追加し、未適用・残存なし・定型文のみ・修正案の残存・確認不可を区別する。
- `tests/test_p3_1_resolution_ledger.py` を追加。既存の修復ランも、適用済みなら `passed` 時に再検査する。既存計画やランは再生成しない。
- ホスト/コンテナSHA-256: `app/plan_repair_loop.py` = `716E58B0C5C8247A9992E6332468BA8F17DFB84657F16125A4B71A71476FD088`。試験ファイル = `3794397F17D4102C538E50CCB7BEC57E758B06B0080EB45537DB75F0392E0E06`。
- 稼働Webイメージ: `sha256:98ba7799c21d483bca7c9008c737fe9d37489df6192ce0a0b8feb227965930d7`。Web healthy。

## 検証
- 新規・関連試験（`test_p3_1_resolution_ledger.py`、`test_p2_1_issue_remedy_patch.py`、`test_p1c_plan_repair_loop.py`）: **23 passed**。
- 本番全体試験: **2607 passed / 10 failed / 5 skipped**。失敗10件の試験名は直前の2026-11-01 P2-1本番全体試験の再実行と完全一致（新規失敗0）。全体合格とは記載しない。ログ: `output/p3_1_resolution_ledger_full_20261101.txt`。
- `scripts/healthcheck.ps1`: 全項目PASS。Ollama選択モデルは `qwen3.5:9b`。
- 台帳の `retained` は計画への文字列の残存を示すだけで、指摘の意味的な解決ではない。人の確認と再評価が必要。
- 同じ工程に複数指摘を承認すると現行の適用処理は最初のパッチだけを入れ、2件目以降を適用済みと記録する。今回の台帳は残存しない指摘を検出して `passed` を止めるが、適用処理そのものは未修正。次の改善対象。
- 実PJでの候補生成・承認・適用・再評価の通し実行は今回未実施。
- OCR Phase 5、車両損益の実案件受入、業務確認事項36項目は未完了のまま。OCR・RAG・承認の安全原則は変更していない。

## 保全・復旧
- 旧ソース、停止時の `/data/memory`、ZIP展開済みファイル、審査差分は `backups/20261101_p3_1_resolution_ledger_final55/` に保全。旧イメージ `localsaporter-web:before-p3-1-resolution-ledger-20261101` = `sha256:f95e8205ffd620df953499e49ed2b2582d82874f14253f6555c6a84a46c0f4e4`。
- 復旧が必要ならWebを停止し、退避した `app/plan_repair_loop.py` を戻し、追加試験ファイルを今回の反映対象から外し、旧イメージでWebを再作成する。停止時データは必要性と影響を確認するまで巻き戻さない。
