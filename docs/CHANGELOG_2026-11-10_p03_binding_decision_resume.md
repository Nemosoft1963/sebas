# 2026-11-10 P0-3 確定決定による再開

## 本番反映

- 配布元: `LOCALSAPORTER_deploy_20261110_p03_binding_decision_resume.zip`。ZIP SHA-256: `8D5AB3652A8823CF73463EFED0685B2DD69921BD36F5E3B9AC66708826AC7691`。同梱 `hashes.txt` の3件と展開内容を照合。
- 現行AG2コードとの差分を監査し、Webだけを停止。旧コード2件と `/data` を `backups/20261110_p03_binding_decision_resume/` に保全した後、3ファイルを配置してWebを再ビルド・起動。
- 新経路は再開APIに `binding_decision: true` を指定した場合だけ使用する。画面は未変更。v2フラグは未設定のまま。外部AIへの送信や実PJの再開は行っていない。

## 変更内容

- 人が確定した指摘対応付けから決定ハッシュを作成し、同じ計画署名でも新しい決定を1回の再開入力として受け付ける。
- 計画署名・レビュー指紋・packet・GoalContract・確定状況を照合。不明、未確定、矛盾、同じ決定の二度目は停止する。
- 保存済みの指摘をローカル修正案生成へ渡す。候補は自動適用せず、差分確認待ちで停止する。

## 配布物の監査で補った修正

1. packetまたはGoalContractが欠ける場合に照合を省略して再開できる経路を閉じた。欠落・空ハッシュは停止する。
2. 確定決定を変えてもローカル修正案の再利用キーが同じとなる問題を修正。決定ごとのガードキーをジョブの冪等キーと候補生成キーに含める。
3. ガードキーを8件で破棄すると古い決定を再実行できるため、キーを保持する。確定行だけをハッシュに含める試験、入力欠落、異なる決定で異なる生成キーを使う試験を追加した。

上記補正のため、2ファイルの最終ハッシュは配布元の `hashes.txt` と異なる。

| ファイル | 本番配置SHA-256 |
|---|---|
| `app/plan_review_loop.py` | `D60AEDB6AB93491FB58754E7707819C1F3157A50CBBDCF4876FBB6BFE89A42B6` |
| `app/web.py` | `228550A976A354A79B024CE4B9B2DA8A54B920668574C8512F78B9C88BAE70BE` |
| `tests/test_p03_binding_resume.py` | `AB962B53A1E2D23C265B56EDA64FDD3544EE63FB1DD70F28F790A398AF782148` |

## 検証

- 隔離コンテナのP0-3・計画修正・安全再開・fail-open関係試験: **73 passed**。
- 全体試験: **2919 passed / 10 failed / 83 skipped**。失敗10件はAG2反映後と試験名が完全一致。全体合格ではない。記録: `backups/20261110_p03_binding_decision_resume/full_suite_20261110.txt`。
- 本番コンテナ内の2コードファイルのSHA-256一致を確認。`scripts/healthcheck.ps1` 全項目PASS。WebとLANプロキシはhealthy。

## 未実施・限界

- 実PJの確定→再開→ローカル修正案生成は未試験。隔離環境の疑似PJでの受入まで。
- 画面側に `binding_decision` の操作はなく、API指定が必要。修正候補の自動適用は行わない。
- P0-4、P1-1、P1-2は未実装。OCR実PDF受入、車両損益の実案件受入、業務確認事項36項目も未完了。

## 差し戻し

1. `docker compose stop web` でWebだけ停止し、現行コードを別途保全する。
2. `backups/20261110_p03_binding_decision_resume/old/` の2コードファイルを元の位置へ戻し、新規の `tests/test_p03_binding_resume.py` を除く。
3. `docker compose up -d --build --no-deps web` で起動し、`scripts/healthcheck.ps1` を実行する。`/data` は無条件に上書きせず、必要な場合にだけ `data_before_p03` から個別に復元する。

