# 2026-11-09 AG1 評価ゲート統合の本番反映

## 対象と結果

- 配布元: `LOCALSAPORTER_deploy_20261109_ag1_plan_gate_decision.zip`。ZIP SHA-256: `66767DD0F557D5C7862C101FEBD67F7C9322327587C1E1177EE889195D2A8F88`。同梱 `hashes.txt` の5件と展開内容を照合した。
- 本番Webを停止し、旧コード3件と `/data` を `backups/20261109_ag1_plan_gate_decision/` に保全してから、5ファイルを反映した。Webイメージを再ビルドして起動した。
- `SEBAS_PLAN_GATE_V2_PROJECTS` は未設定。既存PJの判定は従来経路のまま。新しいv2は有効化していない。
- 変更対象: `app/plan_gate_decision.py`、`app/project_manager.py`、`app/safe_auto_resume.py`、`app/workflow_readiness.py`、`tests/test_ag1_plan_gate_decision.py`。

## 配布物を監査して補った修正

1. 必須評価AIが複数設定された場合、独立評価AIが1社だけ一致しても方針を満たしたとみなさない。全必須提供元を1件の独立評価で満たせない場合は `review_policy_unmet` で停止する。
2. v2有効PJの準備画面は新しい判定を使う。旧ゲートの停止理由を表示し続ける不整合を修正した。
3. v2有効PJの安全な自動再開は、新しい判定が合格した場合に旧レビューの合格を重複要求しない。人間承認などの既存条件は維持し、判定が不明・失敗なら停止する。

このため、本番コードのハッシュは配布元 `hashes.txt` と一部異なる。最終配置のSHA-256は次のとおり。本番コンテナ内でも一致を確認した。

| ファイル | 最終SHA-256 |
|---|---|
| `app/plan_gate_decision.py` | `4D50A4D406C72311BC8746C697759A7A3F2E74F7251ED8F3712AA1CB27AA72DE` |
| `app/project_manager.py` | `FAF59317DAA188A98AF55D9507AE2CDA178E245F6F5D2DA82FF57532144398F3` |
| `app/safe_auto_resume.py` | `E86CABD62A8CB2CCDF09996B675195DCC8FE90EB9C683C972885523D9B75F3FB` |
| `app/workflow_readiness.py` | `3E1F1CE8C030CB120709BFE9E7D7C9A676BC18BDDBC60386D00600206DA9589E` |
| `tests/test_ag1_plan_gate_decision.py` | `EF83D15357A290039EFA18EDDBB4FA1D09C15EBDB4FFB11080E763BAA1D65728` |

## 検証

- AG1と関連ゲート・自動再開・準備画面・fail-open検査: **93 passed**。隔離コンテナで実施。
- 全体: **2889 passed / 10 failed / 83 skipped**。失敗10件は `output/full_suite_20261107.txt` の既知10件と試験名が完全一致。全体合格ではない。Node.jsなどが無い隔離環境で83件スキップした。記録: `backups/20261109_ag1_plan_gate_decision/full_suite_complete_20261109.txt`。
- `scripts/healthcheck.ps1`: 再起動直後のProxy起動待ちで一度FAIL、その後再実行で**全項目PASS**。Web、Proxyともhealthy。
- 本番コンテナ内で4コードファイルのSHA-256一致を確認。AG1 v2フラグ未設定を確認。

## 限界と次の確認

- v2の実PJ有効化・実走は未実施。影モードの記録を見てから架空PJに限定して確認する。
- P0-2の計画packet拡張はこの配布物に含まれない。独立評価が未作成の成果物を要求して停止する可能性が残る。
- 理由コードの日本語画面表示は未実装。
- OCR実PDF受入、車両損益の実案件受入、業務確認事項36項目は引き続き未完了。

## 差し戻し

1. `docker compose stop web` でWebだけ停止する。
2. `backups/20261109_ag1_plan_gate_decision/old/app/` の3ファイルを `app/` に戻し、新規の `app/plan_gate_decision.py` と `tests/test_ag1_plan_gate_decision.py` を除く。差し戻し前に現行コードを別途保全する。
3. `docker compose up -d --build --no-deps web` で再ビルド・起動し、`scripts/healthcheck.ps1` を実行する。運用データを無条件に上書きせず、必要な場合だけ `data_before_ag1` から個別に復元する。

