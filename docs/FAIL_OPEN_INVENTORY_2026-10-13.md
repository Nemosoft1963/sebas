# fail-open 棚卸し報告書 (A-3 / 2026-10-13)

対象: `scripts/fail_open_lint.py` (`lint_project`) の現コード全体の検出。
基準: `tests/fail_open_baseline.json`。製品コードは修正しない
(A-1 の `app/autopilot.py` 修正、および F1 の自走 17件の fail-closed 化を除く)。
既存の違反の修正は結果を見て人が判断する。

## 要約

- 検出の総数: **163件** (当初 180件。F1 で `app/autopilot.py` の自走 17件を直して一覧から外した)
- 型別: **P1 99件 / P2 2件 / P3 14件 / P4 48件**
- 分類別: **high_risk 67件 / review 92件 / allow 4件**
- `high_risk` の定義: `scripts/fail_open_lint.py` の `CORE_MODULE_FILES`
  (コードの1か所) に載る安全の中核モジュール
  (承認・完了ゲート・外部送信・削除・RAG登録・OCR・公開・事前照合・
  自走・停止分類・証拠・漏斗) の検出は `high_risk`、それ以外は `review` が自動初期値。
  `allow` は実コードを読んで理由を書けるものだけ (3→4件。下の allow 一覧参照)。
  迷ったものは `review` に残し、分類を捏造しない。

## 型別・ファイル別の件数

型別の内訳:

- P1 (握りつぶし: 広い except の本体が pass/continue/定数 return のみ): 99件
- P2 (失敗を肯定で返す: 広い except で return True / 肯定的な既定値): 2件
- P3 (`or True` / `or 1` の既定値補完): 14件
- P4 (JS: 空フォールバック/空catch/returnだけのcatch): 48件

ファイル別の上位 (全量は `tests/fail_open_baseline.json` 参照):

- `app/plan_feedback.py`: P1 19件 (apply 周りの pass 群。承認・適用の周辺のため要確認)
- `app/autopilot.py`: P1 0件 (F1 で自走 17件を fail-closed 化済み)
- `app/static/ocr_review.js`: P4 8件 (OCR画面の空フォールバック)
- `app/project_delete.py`: P1 7件 + P2 2件 (削除。P2 は下の high_risk 一覧参照)
- `app/static/workflow_readiness.js`: P4 6件 (進行状態画面)
- `app/plan_review_loop.py`: P1 11件 + P3 1件 (承認・外部評価)
- `app/plan_case_reference.py`: P1 9件 (事例参照の pass 群)
- `app/static/project_delete.js`: P4 5件 (削除画面)

## high_risk の一覧 (67件)

直す場合の方針は共通: まず当該箇所が合否・承認・送信の判定に使われるか確認し、
使われる場合は (1) 狭い例外に絞るか、(2) ログ＋再送出/失敗として記録するか、
(3) 既定値をやめて unknown/失敗で止める、のいずれかにする。
製品コードの一括修正は今回はしない。以下はファイル・関数・型と「どう危険か」。

### 自走 (`app/autopilot.py`: 0件)

F1 で直した (ベースラインから外した) 17件:

- `_action_remap_stored_review`: プレビュー全件失敗は例外 (ティック失敗)。次候補へ
  握りつぶして進めない。
- `_budget_snapshot` / `_budget_unchanged`: 読取不能・比較不能は不明/False (停止側)。
  空辞書で通さない。
- `_count_external_calls`: 復元失敗は `counts["_restore_failed"]` に記録し、
  呼び出し側が計数不能=ティック失敗にする (増分0と見なさない)。
- `_count_review_rounds`: 読取不能は None (不明)。呼び出し側が 0 差分と同一視せず停止。
- `autopilot_tick`: 空転ガード評価失敗・履歴追記失敗・記録動作失敗はティック失敗。
  二重失敗でも成功は返さない。

直したあとの静的検出は 0件。残る広い except は本体が代入・再送出のため検査対象外。

### 事前照合 (`app/capability_gap.py`: 4件、`app/capability_export.py`: 1件)

- `_active_contract` / `_contract_of` P1 (`return {}`)、`_context_files_of` P1 (`return []`):
  契約・原本情報の読取不能を空扱い。不足の見落とし (fail-open) にならないか要確認。
- `_audit_event` P1 (`pass`): 監査の書込失敗を握りつぶす。監査欠落の恐れ。
- `_connect` (`capability_export`) P1 (`pass`): 接続表の作成失敗を握りつぶす。要確認。

### 完了ゲート (`app/completion_gate.py`: 2件、`app/goal_completion_store.py`: 1件)

- `_current_hashes` / `_generic_artifact_hash` P1 (`continue`):
  ハッシュ計算の失敗項目を飛ばす。証拠不足の見落としにならないか要確認。
- `put_contract` P3 (`payload.get('version') or 1`): 版欠落を 1 で補完。
  真偽・合否・件数の判定ではない (版番号の既定値) ため実害は小さい見込みだが要確認。

### 承認・外部評価 (`app/goal_review.py`: 2件、`app/goal_review_queue.py`: 2件、`app/plan_review_loop.py`: 12件、`app/project_manager.py`: 6件)

- `execution_gate` / `revoke_result` / `loop` / `tick` / `_call_gemini` /
  `_lease_set` / `_remap_base` / `_run_locked` / `_stop` / `_stop_guard_tick` /
  `remap_run` / `start_run` / `_generate_plan_impl` / `_generate_structured_plan` P1:
  いずれも広い except の握りつぶし。承認・外部評価・計画生成の周辺のため、
  失敗を見落として進行扱いにならないか要確認。
- `_lease_get` P1 (`return None`): lease 読取不能を None 扱い。競合時の二重実行に注意。
- `_execute_task` P1×3 (`except (Exception, asyncio.TimeoutError): pass`):
  実行失敗を握りつぶす。失敗タスクの成功扱いにならないか要確認 (最優先で人が見ること)。
- `remap_preview` P3 (`first.get('round') or 1`): ラウンド欠落を 1 で補完。
  件数の判定に使う箇所のため要確認 (実害は小さい見込み)。

### 外部送信・契約 (`app/goal_contract.py`: 1件)

- `from_mission` P3 (`item.get('weight') or 1`): 重み欠落を 1 で補完。
  件数・配分に影響しうるため要確認。

### 削除 (`app/project_delete.py`: P1 7件 + P2 2件)

- `delete_state_status` P1 (`pass` in `except Exception`):
  実コードを読んだ。`finally: db.close()` の close 失敗を握りつぶすだけで、
  状態判定自体は `unknown` 方向 (fail-closed) のため実害は小さい。残りは要確認。
- `delete_state_status` P2 (`return 'active'`):
  実コードを読んだ。DB無し・行無しのときの `'active'` (未削除) であり、
  読取不能時は `'unknown'` を返す fail-closed 設計。検出器は `return 'active'`
  の文面だけ見るため誤検出に近いが、棚卸しとして残し、人が再確認すること。
- `is_deleted_or_unknown` P2 (`return True`):
  実コードを読んだ。例外時は True (削除扱いで止める fail-closed) のため
  fail-open ではない。検出器の文面一致による検出として残し、人が再確認すること。
- `purge_deleted` P1×4、`request_delete` P1、`restore_deleted` P1:
  いずれも監査 (`add_event`) 失敗の握りつぶしの見込み。監査欠落の恐れ。要確認。

### RAG登録 (`app/experience_memory.py`: 4件)

- `configured_memory` P1 (`return None`)、`import_success_cases` P1 (`pass`)、
  `remove_from_index` P1 (`pass`): 設定・取込・索引削除の失敗を握りつぶす/空扱い。
  未登録を登録済み扱いにしないか要確認。
- `convert_success_case_item` P3 (`item.get('kind') or 'success'`):
  種別欠落を `'success'` で補完。真偽・合否ではないが、種別の誤分類に注意。

### OCR (`app/ocr_fallback.py`: 2件、`app/ocr_invoice_adapter.py`: 2件、`app/ocr_usami_adapter.py`: 2件)

- `run_ocr_fallback` P3×2 (`page_no or 1`、`page_count or 1`)、
  `payload_from_ocr_bundle` P3×2、`_iter_table_blocks` P3 (`page.get('page') or 1`):
  ページ番号欠落を 1 で補完。ページ照合のずれに注意 (合否判定ではない)。
- `parse_table_rows` P1 (`return []`): 表解析の失敗を 0 行扱い。
  読取不能を「空の成功」にしないか要確認。

### 停止分類 (`app/stop_classifier.py`: 2件、`app/workflow_readiness.py`: 2件、`app/resolution_coordinator.py`: 5件)

- `_evidence_refs` P1 (`continue`)、`_vehicle_view` P1×2 (`continue`):
  証拠ハッシュ計算の失敗項目を飛ばす。証拠不足の見落としにならないか要確認。
- `_next_for` P1 (`pass`): 実コードを読んだ。`IMPLEMENTED_ACTIONS` 読取失敗時に
  既定の `none` に倒す前の pass であり、fail-closed 方向。残りは要確認。
- `_safe_sources_readable` P1 (`return False`)、`_statements` P1 (`return {}`)、
  `_trial_evidence_server_side` P1×3: 原本・証拠の読取不能を False/空扱い。
  不足の見落としにならないか要確認。

### 漏斗 (`app/execution_funnel.py`: 6件)

- `_requires_external` P1×2 (`pass`) + P1 (`continue`)、
  `portfolio_funnel` P1 (`continue`)、`project_funnel` P1 (`pass`):
  外部要件の判定失敗を握りつぶす。未達を通過扱いにしないか要確認。
- `_external_satisfied` P3 (`req.get('minimum_executed') or 1`):
  最小実行件数の欠落を 1 で補完。件数の判定に直結するため要確認。

### JS の high_risk 相当

JS は `CORE_MODULE_FILES` が Python 基準のため自動分類は `review` だが、
次の画面は安全の中核 (OCR・削除) に関わるため人が優先して見ること:

- `app/static/ocr_review.js` P4×8 (空フォールバック群)
- `app/static/project_delete.js` P4×5 (削除画面の空フォールバック)

## allow の一覧と理由 (4件)

実コードを読んで確認し、理由を書けるものだけ。判定に影響しない表示用の補助に限る。

- `app/static/presenters.js:rawJson` P4×2
  (`catch(e){return value;}` / `catch(e){return String(value);}`):
  表示整形のフォールバック。JSON pretty 表示に失敗したら元の値をそのまま返すだけで、
  合否・承認の判定には使わない (presenters は表示用)。
- `app/static/artifact_shelf.js:load` P4 (`catch(e){}`):
  現行経路集合 (`currentPaths`) への追加を諦めるだけの表示フォールバック。
  読めない工程は現行扱いにせず、階級表示は既定で「下書き」
  (検証済みにはならない fail-closed 方向)。
- `app/static/workflow_readiness.js:describe` P4 (`catch(_){return String(value);}`):
  進行状態の表示整形フォールバック。JSON 化に失敗したら文字列化して返すだけで、
  停止分類・合否の判定には使わない (describe は表示用)。

## 検出の限界

- P3 で `.get(k, 0)` を対象外にした理由:
  `.get(k, 0)` は辞書の件数・版・索引などの非判定用途で広く使われており、
  機械的に検出すると誤検出が大量に出て棚卸しが読めなくなる。
  真偽・合否・件数の判定に使う箇所での肯定補完 (`or True` / `or 1` / `or 'ok'` 等)
  だけを P3 の対象に絞った。`.get(k, 0)` のうち判定に使うものは人の目視で確認すること。
- 動的な書き方の見落とし:
  `getattr(x, 'y', True)` / `os.getenv(..., 'active')` / 三項演算子
  (`a if cond else True`) / デコレータ経由の握りつぶし /
  文字列組み立て後の `eval` 等は検出しない。
  広い except でも本体に `log` があるものは握りつぶしとみなさないため、
  ログだけで失敗を返さない箇所は見落とす。
  JS は正規表現のため、複数行にまたがる catch や `.then().catch()` 以外の
  経路 (async 関数の暗黙の resolve 等) を見落とす。
  新しい書き方が入ったら検査の拡張とベースラインの更新を人が判断すること。

## 再現方法

- 全体検査: `python _scratch.py --lint` 相当
  (実際は `scripts/fail_open_lint.py` を `lint_project` 経由で実行。
  一時ファイル `_scratch.py` は完了前に削除済み)
- ベースライン照合テスト: `tests/test_a3_fail_open_lint.py`
- 差分検査 (D-3 再利用): `lint_changed_files(paths)`
  (変更行を含む関数だけ検査し、ベースラインに無い新規違反だけ返す)
