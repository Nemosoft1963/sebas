# 目標達成 第3段階-A 変更履歴（2026-09-26）

## TRIZ発明対象からの除外

| code | family | next_owner | 判定根拠 |
|---|---|---|---|
| `SOURCE_READ_FAILED` | SOURCE | `source_fix` | `p0_unreadable_or_zero` |
| `SOURCE_RECONCILIATION_FAILED` | SOURCE | `source_fix` | `p0_reconciliation` |
| `SOURCE_CONTROLS_MISSING` | SOURCE | `source_fix` | `p0_empty_controls` |
| `SOURCE_INVOICE_INCOMPLETE` | SOURCE | `source_fix` | `p0_invoice_incomplete` |
| `ALLOCATION_UNRESOLVED` | ALLOCATION | `human_fact` | 構造化 issues の未解決 allocation |
| `FACT_MISSING` | FACT | `human_fact` | 既存の構造化 business_fact 分類 |
| `DEVELOPMENT_REQUIRED` | DEVELOPMENT | `development` | 既存状態・分類が development_required |

除外時は候補、実験、アダプター採用を生成せず、`development_required` と根拠を保存する。`business_passed=false` であり、業務回復表示にはしない。`unknown_format` と `vehicle_review_required` は従来経路を維持する。TRIZ adopt の既定拒否は変更しない。

## 再起動時の収束

対象は goal-completion flag がONの案件だけ。

- job: 最終checkpointがジョブ種別の最終段階と一致し、かつ対応する永続結果行（review_planでは同じjob_idの終端plan行）または明示された全成果物が存在する場合だけ `succeeded`。それ以外は `needs_attention` と `RESTART_ORPHANED`。
- plan: `running` を `unverified` へ戻す。`passed` / `verified` には進めない。
- mission: `running` を `paused`（`restart_reconciled`）へ変更し、タスクは変更しない。
- 案件ごとに例外を隔離し、イベントへ対象ID・理由を記録する。2回目は変更0件となる。
- 承認、確定、`achieved` は一切書かない。状態機械は直接変更せず、missionは既存 `set_mission_status` のdual writeを利用する。

## 試験

- `tests/test_triz_exclusion.py`: TZ-01〜TZ-04
- `tests/test_restart_convergence.py`: RC-01〜RC-06
- 実測結果は作業完了報告に記載する。
