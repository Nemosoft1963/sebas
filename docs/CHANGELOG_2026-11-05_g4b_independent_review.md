# 2026-11-05 G4-b 独立評価ジョブ 本番反映

## 反映

- 受領物: `LOCALSAPORTER_deploy_20261104_g4b_independent_review.zip` (SHA-256 `6E9A020CD6EC9FC477D9430984FC9141721BA65A2A4D0FEA6B7BF688DDAA80B1`)。ZIP内の3ファイルを `hashes.txt` と個別照合した。
- `app/independent_review.py` を追加し、承認済み公開packetを評価AIへ一度だけ送る独立評価ジョブを実装。方式、評価先、提供元の相違、packet、冪等キー、日次上限、送信許可、送信直前の漏えい検査、応答形式と実モデルを確認する。
- `app/web.py` に独立評価ジョブの作成・一覧・個別取得・取消APIを追加。`tests/test_g4b_independent_review.py` を追加。既存の実行ゲート・計画判定には接続していない。
- Webは本番で再構築・起動済み。新イメージ `sha256:f2a9fb332f931c6418d8744172f1e7e91618df16f3e3c74f226faf31ce5851b5`。コンテナ内の新モジュールとWebコードのSHA-256は配布物と一致。
- `SEBAS_INDEPENDENT_REVIEW_PROJECTS` と `SEBAS_EXTERNAL_PLAN_JOBS_PROJECTS` は未設定のまま。今回、実際の外部AI送信・実PJの承認や実行は行っていない。

## 検証

- `scripts/healthcheck.ps1`: 全項目PASS。Web・プロキシともhealthy、Ollamaモデルは `qwen3.5:9b`。
- G4-b/G4-a/G3-a/G2-2/G1関連試験: **77 passed、警告1**。`output/g4b_independent_review_focused_20261105.txt`。
- 全体回帰試験 (`PYTHONUTF8=1`, `PYTHONHASHSEED=3`): **2768 passed / 10 failed / 5 skipped、警告1、744.06秒**。前回G4-aとの失敗試験名の差分は0。新規失敗はないが、全体合格ではない。`output/g4b_independent_review_full_20261105.txt`。

## 限界

- 独立性は設定上の提供元IDと応答に記録された実モデル名で判断する。学習データや運営主体の独立性までは保証しない。
- `review_passed` は人間の承認や実行可能を意味せず、実行ゲート接続はG4-cの範囲。
- 送信直前の拒否リスト型検査は安全保証ではない。機能フラグを有効にした実送信試験は未実施。
- OCR Phase 5、車両損益の実案件受入、業務確認事項36項目は未完了。

## 保全・復旧

- 旧 `app/web.py`、停止時の `/data/memory`、展開物を `backups/20261105_g4b_independent_review_final65/` に退避。旧Webイメージ `localsaporter-web:before-g4b-independent-review-20261105` = `sha256:99b8794becfd8933ff762d0be2fea218a024e390bf31d4a997c4cca991c20fe3`。
- 復旧時はWebのみ停止し、退避した旧 `web.py` を戻し、新規 `independent_review.py` とその試験を除外して旧イメージで再作成する。データは影響確認なしに巻き戻さない。

