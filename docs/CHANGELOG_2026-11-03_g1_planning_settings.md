# 2026-11-03 G1 PJ別の計画作成設定と用途別許可 本番反映

## 反映内容

- `LOCALSAPORTER_deploy_20261102_g1_planning_settings.zip`（SHA-256 `4CE96CA68EBED4FAE3332CC2A7EEA1E976BE287A3E7F0CB550A1F74260E65641`）の3ファイルを `hashes.txt` と照合し、`app/web.py` の差分（35行追加）を審査して適用。
- `app/planning_settings.py` にPJ別設定をReviewStoreの `planning_settings` 種別で保存する処理を追加。方式は既定 `local_only`。用途別許可は計画署名、packet SHA-256、提供元、用途、設定版に束縛する。方式・提供元の変更時には全許可を失効させる。
- `app/web.py` に `GET/PUT /api/projects/{id}/planning-settings` を追加。接続済み提供元だけ選択可能。実行中の更新を拒否する。許可付与APIは今回追加していない。
- `tests/test_g1_planning_settings.py` を追加。G0-1の送信前検査、既存の外部AI送信経路、目標検証、OCR、経験RAGは変更していない。
- ホストとWebコンテナで `planning_settings.py` SHA-256 `B6917BD8E6E36F2FDB3A8766FC8584D05FFFC6DDFA25D2DDEA942D382C0837E6`、`web.py` SHA-256 `32BF0DA349BE140650598AE2E3EEDCE71177E4CCBB6954FDC8A9842CB6319C63` が一致。新Webイメージ `sha256:252a936e2d4c1794e085749734802b4153afc091e1c677c1724441818c390deb` は healthy。

## 検証

- G1とG0-1の関連試験: **49 passed**。ログ: `output/g1_planning_settings_focused_20261103.txt`。
- 全体試験（`PYTHONUTF8=1`、`PYTHONHASHSEED=3`、pytestプラグイン有効）: **2662 passed / 10 failed / 5 skipped**。失敗10件は直前G0-1の本番試験と試験名が完全一致し、新規失敗0。全体合格ではない。ログ: `output/g1_planning_settings_full_20261103.txt`。
- `scripts/healthcheck.ps1` 全項目PASS。選択Ollamaモデル `qwen3.5:9b`。Web healthy。
- 実PJの設定変更、実外部AI送信、実PJ通し実行は行っていない。

## 限界・次工程

- G1は設定と許可の**保存・検査基盤のみ**。`project_manager.py` の送信経路には未接続で、設定を保存しても現時点の送信可否は変わらない。個別packetの提示・許可付与と送信直前の許可強制はG2-2/G3の対象。G0-1の拒否リスト検査も完全な情報流出防止ではない。
- OCR Phase 5、車両損益の実案件受入、業務確認事項36項目は未完了。

## 保全・復旧

- 旧 `app/web.py`、停止時の `/data/memory`、展開済みZIP内ファイルを `backups/20261103_g1_planning_settings_final58/` に退避。旧Webイメージ `localsaporter-web:before-g1-planning-settings-20261103` = `sha256:53044119a7f63c692b90b2a5527d35e61c9be0a5d2e7e1be1ce84f38d03e87d9`。
- 復旧時はWebだけを停止し、退避した `app/web.py` を戻し、新規 `app/planning_settings.py` と試験ファイルを除外して旧イメージで再作成する。データは影響確認なしに巻き戻さない。
