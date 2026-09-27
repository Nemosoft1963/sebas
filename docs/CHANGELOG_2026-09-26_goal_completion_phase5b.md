# Goal Completion Phase 5-B 変更履歴

## 変更範囲

- 旧 `mission.status === 'completed'` の「目標達成」表示を縮小した。
- 進行パネルへ読み取り専用の指標セクションを追加した（自動ポーリングには載せない）。

## 表示規則

| 条件 | 表示 |
|---|---|
| `completed` かつ進行パネル最新状態の `final_completed === true` | 目標達成(確定) |
| `completed` だが `final_completed` が偽、未取得、取得失敗 | 全工程完了・目標達成は未確定（「目標達成」とは書かない） |
| `paused` / `failed` など | 従来どおり（確認待ち・目標未達、失敗停止・目標未達 等） |

工程件数の `progress.completed / progress.total 完了(%)` と `missionArtifactsDownload` の有効条件は変更していない。

## 公開関数

- `window.workflowReadinessSnapshot()`: 進行パネルの最新状態。未取得時は `null`。
- `window.refreshGoalCompletionLabels()`: 案件状態ラベルの再評価。
- `window.refreshArtifactGoalLabels()`: 成果物棚の案件状態キャプション再評価。
- readiness 取得後（成功・失敗とも）に上記を呼び、進行パネル更新後へ追随する。
- 既存の `applyWorkflowReadiness` / `reloadWorkflowReadiness` の挙動は維持。

## 指標パネルの取得タイミング

- `#workflowReadiness` 内の「指標」セクション。
- `GET /api/projects/{id}/goal-metrics` は「指標を更新」ボタンと案件切替時のみ。
- 進行パネルの `schedule` / `load` ポーリングからは呼ばない。
- `null` は「不明」または「未計測」。0 にはしない。描画は `textContent` のみ。

## 安全境界

- バックエンド・API・flag は変更しない（既定OFFのまま）。
- 承認・確定・`achieved` を書く経路は作らない。
- `innerHTML` は新規追加していない。`workflow_readiness.js` は 0 件のまま。

## 対象外・未検証

- UIの5画面化、Git導入、本番反映、flag ON は対象外。
- 実ブラウザでの表示確認は未実施（この環境に Node が無く、構文はホスト側検査を想定）。
- 実データ（本番案件・DB）・OCR・GPU は未使用。
