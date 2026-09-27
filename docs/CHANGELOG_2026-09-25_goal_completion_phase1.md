# CHANGELOG 2026-09-25 Goal Completion 第1段階

## 追加

- GoalContract / 要求保持検査 / 計画被覆表 / Completion Gate
- 車両12条件テンプレ `vehicle_monthly_pnl/v1`
- API: `/goal-contract`, `/plan/coverage`, `/completion-gate`, `/next-action`
- feature flag `goal_completion.json`（既定オフ）
- 未配賦分類 `classify_unresolved` と `goal_failure_records`

## 挙動

- flag OFF: 現行の計画生成・承認は変わらない。新APIと readiness の追加フィールドだけ有効。
- flag ON: 長文1条件は `RETENTION_FAILED` で計画生成拒否。被覆穴は承認拒否。
- 暫定Excel / mission completed だけでは `achieved` にならない。
- `next_action.endpoint` を埋めた（idle/running は None のまま）。

## 検証

開発環境で次を再実行し合格:

- tests/test_goal_contract.py
- tests/test_plan_coverage.py
- tests/test_completion_gate.py
- tests/test_workflow_readiness.py
- tests/test_vehicle_workflow.py
- tests/test_structured_planning.py
- tests/test_project_manager.py
- tests/test_goal_review.py
- tests/test_plan_feedback.py
- tests/test_vehicle_allocation.py

合計 131 passed（上記集合）。本番イメージでは未実証。
