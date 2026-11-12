# 2026-10-30 P0-2 原本読取の計画への結線 本番反映

## 反映内容
- 提供ZIP `LOCALSAPORTER_deploy_20261030_p0_2_source_read_wiring.zip`（SHA-256: `D439518A030598ABAC20D1EF4EBD6476B808F31ECEB23702205F056593F3E12E`）の3ファイルを `hashes.txt` と照合し、差分を確認して反映。
- `app/structured_planning.py`: 原本抽出の達成条件で、登録済み原本がある場合だけ計画工程に `source.read_excerpt` を宣言する。原本未登録時は入力不足として扱う。
- `app/capability_gap.py`: 原本抽出で既存実行器の5条件がすべて真なのに工程宣言がない場合だけ `plan_wiring_gap` とし、次操作を `revise_plan` にする。実行承認時にも事前照合を計算し、結線不足・不正な照合・署名不一致を拒否する。ほかの操作の分類は変更しない。
- `tests/test_p0_2_source_read_wiring.py` を追加。既存計画は自動で再生成しないため、該当する既存PJは計画の作り直しが必要。
- 最終ホスト/コンテナSHA-256: `app/capability_gap.py` = `0A316378C383B42BFBC8FCBCF0F562D3D9BE3AD17F106B4FA692BD91AD3B1990`、`app/structured_planning.py` = `6E151BD90FB26BF9DD01831533B8D3F66F20DFCF4BAE3D7B2F0D233FB95F3557`。試験ファイル = `05318214C94CA7A8E8C6AFABD093DF8752FEE5A64D42F7D4A086A30EA8ED616D`。
- 稼働Webイメージ: `sha256:3c803e135afbc32f87cdfffb75e4e6a1151e61bfb87402afff1e7bac84a9e381`、Web healthy。

## 検証
- 新規・関連試験（`test_p0_2_source_read_wiring.py`、`test_plan_feedback.py`、`test_p0_1_required_output_paths.py`）: **40 passed**。
- 本番全体試験: **2566 passed / 10 failed / 5 skipped**。失敗10件の試験名は2026-10-27の本番全体試験と完全一致（新規失敗0）。全体合格とは記載しない。ログ: `output/p0_2_source_read_wiring_full_20261030.txt`。
- `scripts/healthcheck.ps1`: 全項目PASS。選択モデルは `qwen3.5:9b`。
- 実ローカルLLMによるPJ再計画・通し実行、実原本の読取受入は今回未実施。外部AIへの送信なし。
- OCR Phase 5、車両損益の実案件受入、業務確認事項36項目は未完了のまま。OCR・RAG・承認の安全原則は変更していない。

## 保全・復旧
- 旧ソース、停止時の `/data/memory`、ZIP展開済みファイル、審査差分は `backups/20261030_p0_2_source_read_wiring_final50/` に保全。旧イメージ `localsaporter-web:before-p0-2-source-read-wiring-20261030` = `sha256:d2c54318118482c374501e7d883eb0f7116f19164c2672d662c4ec0a689837f6`。
- 復旧が必要ならWebを停止し、退避した2ソースを戻し、追加した試験ファイルを今回の反映対象から外し、旧イメージでWebを再作成する。停止時データは必要性と影響を確認するまで巻き戻さない。
