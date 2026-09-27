# 再開 manifest — 実装単位9（2026-09-24）

文書のみの単位。コード変更なし。本ファイルは現行ワークスペースの再開記録であり、旧再開ZIPの代替ではない。

## 識別

- datetime_utc: 2026-09-23 20:22:50 UTC
- datetime_jst: 2026-09-24 05:22:50 JST
- source_revision: `26f4f17a7e0f6320b3ee843df6188e299fb676d9`（2026-09-23 20:13:40 +0000）
- extractor REVISION (`app/vehicle_auto.py`): `20260923.3`
- DB schema/version: **未確認**（本セッションは本番DB未読。schema dump なし）
- container_image: **該当なし（Jenkinsワークスペースのみ）**。本番未反映。
- 設定 hash: **未確認**
- Workspace 成果物 hash 一覧: **未作成**（本セッションは文書と pytest のみ）

## 検証（本セッション実測）

- scope: full
- command: `pytest -q`
- result: **528 passed, 0 failed, 1 skipped, 1 warning**（25.18秒）
- runtime environment: Jenkinsコンテナ内。本番Dockerではない
- artifact log path: 本セッションのテスト出力のみ、ログファイル未保存
- `scripts/healthcheck.ps1`: **未実施**
- Docker build/deploy/起動: **未実施**
- 実原本受入（単位6）: **未実施**

## 単位0〜8のワークスペース成果物

存在確認済み。git の単位別差分までは本セッションで再走査していない。設計書の対象とディレクトリ確認に基づく。

### 単位0〜8で新規作成された主なファイル

- `app/vehicle_reconciliation.py`
- `app/workflow_readiness.py`
- `app/static/workflow_readiness.js`
- `tests/test_vehicle_reconciliation.py`
- `tests/test_workflow_readiness.py`
- `tests/test_vehicle_allocation.py`
- `tests/test_triz_vehicle.py`
- `tests/test_recipe_vehicle.py`

### 単位0〜8で変更された主なファイル

- `app/vehicle_auto.py`
- `app/vehicle_workflow.py`
- `app/vehicle_profit.py`
- `app/vehicle_service.py`
- `app/automatic_triz.py`
- `app/executable_recipes.py`
- `app/project_manager.py`
- `app/web.py`
- `app/plan_feedback.py`
- `app/goal_review.py`
- `app/goal_review_queue.py`
- `app/static/goal_review.js`
- `app/static/artifact_shelf.js`
- `tests/test_vehicle_auto.py`
- `tests/test_vehicle_workflow.py`
- `tests/test_vehicle_integration.py`
- `tests/test_goal_review.py`
- `tests/test_plan_feedback.py`
- `BUILD_REPORT.md`（追記のみ）
- `docs/CHANGELOG_2026-09-21_triz_import.md`（訂正節の追記のみ、単位0）

### 単位9で追加したファイル

- 本ファイル `docs/verification/unit9_resume_manifest_2026-09-24.md`
- `BUILD_REPORT.md` への追記（既存節は削除・改変しない）

## 旧再開ZIP（2026-09-21時点）との差分

- 旧再開ZIPは **2026-09-21時点** のソース・文書・（あれば）DBコピーであり、**その後の単位0〜8の実装・テストを含まない**。
- 旧ZIPには少なくとも次が含まれない: P0-1〜P0-3（金額状態・独立原本照合・請求単位）、P1-1〜P1-3（workflow-readiness / 永続ジョブ / 月別配賦）、P2-1（TRIZ→業務回復）、P2-2（レシピ再利用）、および本単位の検証記録。
- 旧ZIPのテスト件数記録（403/414/434等）は当時の値であり、本セッションの **528 passed, 1 skipped** ではない。

## 一括復元禁止

**旧再開ZIPへ丸ごと戻してはならない。** 一括復元すると単位0〜8のコード・試験・文書追記が失われる。再開は現行ソース（`source_revision` 上記）と最新確認文書（本 manifest および `BUILD_REPORT.md` の最新検証サマリー）を正とする。不足分（本番 image、DB backup、設定 hash、実原本受入結果）は本 manifest に「未確認／未実施」と書いてある項目であり、旧ZIPで補完しない。

## 未達・未実施（合格と書かない）

- healthcheck.ps1: 未実施
- Docker build/deploy/起動確認: 未実施
- 単位6 実原本受入: 未実施
- 36項目の業務確認事項: 未回答（安全側既定: 推測しない・issueにして止める）
- 本番反映: 未実施
