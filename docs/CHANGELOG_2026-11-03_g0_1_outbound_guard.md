# 2026-11-03 G0-1 外部AI送信前の決定的検査 本番反映

## 反映内容

- 受領ZIP `LOCALSAPORTER_deploy_20261102_g0_1_outbound_guard.zip`（SHA-256: `787EC7846F6B0E6784C4642CDB608E7762FEFC2694E9EA53F292367851FA2CB3`）の3ファイルを `hashes.txt` と照合して反映。
- 新規 `app/planning_privacy.py` の拒否リスト型検査を、`app/project_manager.py` の旧計画評価、失敗後の別AI再調整、能力ギャップ評価、調査工程の4か所の外部AIランナー直前へ接続。検査失敗時はランナーを呼ばず、保留・スキップ・ローカル処理の既存経路へ移る。
- 送信前監査は目的、送信先、本文SHA-256、文字数のみ、拒否時は目的・理由コード・ハッシュ先頭16桁のみを保存する。本文・検出値は監査イベントに入れない。
- 新規 `tests/test_g0_1_outbound_guard.py` を追加。現行の目標検証、承認、OCR、経験RAGの関門は変更していない。
- ホスト/コンテナのSHA-256は `planning_privacy.py` = `9BF95C4C8809D3EBF3703B9EC1BD1C56CC54274E25C0FCBC5E5FFB139D32C2EA`、`project_manager.py` = `15951026993A8CA22C4E4DE9C2B2EAEA2F03DC42BBC82ACE5BF61C615536CB10` で一致。試験ファイル = `E12B2B087B1C4D7A99D48AAA611059D7F9E805C107A26F04CDB0E338BD87C88F`。
- 新Webイメージ `sha256:53044119a7f63c692b90b2a5527d35e61c9be0a5d2e7e1be1ce84f38d03e87d9` は healthy。

## 検証

- G0-1、PJ管理、外部AIの関連試験: **73 passed**。
- `scripts/healthcheck.ps1`: 全項目PASS。選択モデル `qwen3.5:9b`。
- 本番全体試験（`PYTHONUTF8=1`、`PYTHONHASHSEED=3`、pytestプラグイン有効）: **2651 passed / 10 failed / 5 skipped**、636.46秒。失敗10件の項目名は直前P3-2本番試験と完全一致し、新規失敗0。全体合格ではない。ログ: `output/g0_1_outbound_guard_full_20261103.txt`。
- 外部AIの実APIへの試験送信は行っていない。新検査の通過は漏えいしない保証ではない。氏名・住所・金額明細・言い換え等は未検出になり得る。検査を通る旧計画評価は従来どおり自動送信し得るため、packet全文プレビューと個別の明示許可は次段階G1/G2で必要。外部AIによる計画生成・モデル選択・ローカル保護モードは今回の反映対象外。
- OCR Phase 5、車両損益の実案件受入、業務確認事項36項目は未完了のまま。

## 保全と復旧

- 旧 `app/project_manager.py`、停止時の `/data/memory`、展開済みファイル、審査差分は `backups/20261103_g0_1_outbound_guard_final57/` に保全。旧Webイメージ `localsaporter-web:before-g0-1-outbound-guard-20261103` = `sha256:ea4692d5a04cb462a6cd1854fea1f26d4f11abc529d1a93dc8d4669105abf2aa`。
- 復旧時はWebを停止して旧 `project_manager.py` を戻し、今回追加の `planning_privacy.py` と試験ファイルを除外し、旧イメージでWebを再作成する。停止時データは影響確認なしに巻き戻さない。