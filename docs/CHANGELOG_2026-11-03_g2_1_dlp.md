# 2026-11-03 G2-1 決定的な情報漏えい検査 本番反映

## 反映内容

- 受領ZIP `LOCALSAPORTER_deploy_20261102_g2_1_dlp.zip`（SHA-256 `4B61BEABE1C21CDF03AB95C735D5A7A88F04576E01D9ABBD2CE7AD4E15EC15BF`）の3ファイルを `hashes.txt` と照合し、差分を審査して適用。
- `app/planning_privacy.py` の送信前拒否検査を拡張。電話番号、郵便番号、住所、ラベル・敬称付き人名、口座番号、金額明細、長い数字、符号化文字列、登録原本からの24文字以上の引用を検出する。検出値や本文は監査詳細に保存しない。
- `app/project_manager.py` の `_outbound_guard` で登録原本の本文を読み、引用検査へ渡す。読取時に想定内エラーが生じたら送信を保留する。G0-1の4送信経路と監査イベントの形式は維持。
- `tests/test_g2_1_dlp.py` を追加。G1の設定API、外部送信の許可強制、目標検証、OCR、経験RAGは変更していない。
- ホストとWebコンテナで `planning_privacy.py` SHA-256 `89EAC43669CDCCAF4113C1A675F7F0253E4823FC779C2139B8A46791DE619065`、`project_manager.py` SHA-256 `200BE4EE5A8D03F09279B1E58A306CB3F0FEB779654C8C105440A1ADB4C6E2C0` が一致。新Webイメージ `sha256:04b0961b4530a1af917a3eceb36f3120d1c7a48a657be8f51f58d0a1aabdd0d0` はhealthy。

## 検証

- G2-1、G0-1、G1の関連試験: **78 passed**。ログ: `output/g2_1_dlp_focused_20261103.txt`。
- 全体試験（`PYTHONUTF8=1`、`PYTHONHASHSEED=3`、pytestプラグイン有効）: **2691 passed / 10 failed / 5 skipped**、689.06秒。失敗10件は直前G1本番試験と試験名が完全一致し、新規失敗0。全体合格ではない。ログ: `output/g2_1_dlp_full_20261103.txt`。
- `scripts/healthcheck.ps1` 全項目PASS。選択Ollamaモデル `qwen3.5:9b`。Web healthy。
- 実外部AI送信と実PJ通し実行は行っていない。

## 限界・次工程

- 拒否リスト方式なので、検査通過は情報漏えいがない保証ではない。言い換え、敬称なし人名、住所の別表記、原本の要約や短い引用などは見逃し得る。誤検出により送信保留になる場合もある。
- G1の用途別許可はまだ送信経路に接続されていない。G2-2のpacketプレビュー・明示許可と、その後の送信直前の許可強制が必要。現時点では検査を通った本文は従来どおり自動送信され得る。
- OCR Phase 5、車両損益の実案件受入、業務確認事項36項目は未完了。

## 保全・復旧

- 旧 `app/planning_privacy.py`、旧 `app/project_manager.py`、停止時の `/data/memory` と展開済みファイルを `backups/20261103_g2_1_dlp_final59/` に退避。旧Webイメージ `localsaporter-web:before-g2-1-dlp-20261103` = `sha256:252a936e2d4c1794e085749734802b4153afc091e1c677c1724441818c390deb`。
- 復旧時はWebだけを停止して退避済みの2ファイルを戻し、新規試験ファイルを除外して旧イメージで再作成する。データは影響確認なしに巻き戻さない。
