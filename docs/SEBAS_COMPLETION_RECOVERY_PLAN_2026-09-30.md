# セバス 完走不能問題 修正計画案

- 作成日: 2026-09-30 JST
- 対象案件: 「本システムを販売実行」
- 現行計画: Ver.22
- 現在の扱い: 実行・承認しない
- 計画種別: コード改修、データ移行、回帰試験、本番反映の実施計画

## 1. 結論

完走不能の原因は、外部AI指摘の版管理、汎用計画の再構成、汎用完了判定の3機能がつながっていないことである。UI操作や再試行だけでは解消しない。

修正は次の順序で行う。

1. 旧レビューが新計画へ再注入される循環を止める。
2. 汎用案件をGoalContractから安全に再構成できるようにする。
3. 車両以外にも証拠付きの完了判定を実装する。
4. readiness、next-action、画面ボタンを同じ判定へ統一する。
5. Ver.22を保存したまま、新実装でVer.23候補を生成する。
6. 外部検証、人間承認、実行、証拠確認、最終承認まで一連受入を行う。

## 2. 確認された現象

| 現象 | 現在値 | 影響 |
|---|---|---|
| 重複タスク | 16件中14件が「計画草案の評価と改善提案」 | 実業務を実行できない |
| 旧レビュー再利用 | 旧保存形式のレビュー全文を現行版の指摘として取込 | 再計画のたびに同じ指摘が戻る |
| 修正案生成 | 販売案件で `rebuild_vehicle` を生成 | 車両専用検査で必ず拒否される |
| readiness | `plan_issues_open` | 修正案作成を要求 |
| 修正案 | `自動抽出工程のない車両全体計画だけ...` で失敗 | 汎用計画を再構成できない |
| 完了ゲート | SC01～SC11が全て `UNTESTABLE` | 成果物が揃っても完了不能 |
| 外部AI | Claude/ChatGPT/Grok成功、Gemini 429、Meta 401 | 一部失敗はあるが主原因ではない |

## 3. 根本原因

### R1. レビューの版境界が不十分

`app/plan_feedback.py::issues_for` が、`project_missions.plan_reviews` にある旧レビューを、現行計画署名に対する指摘として追加している。旧レビューには対象版の証明がないため、履歴表示には使えても現行計画の修正入力には使えない。

### R2. 指摘全文を計画生成プロンプトへ注入

`app/project_manager.py::generate_plan` が `issues_for` の結果を `PLANNING_FEEDBACK` へ設定し、全ローカルLLM呼出しへ追記する。長い評価レポートが実行工程の候補として解釈され、「計画を評価するタスク」が大量生成される。

### R3. 汎用計画の再構成経路がない

現在のdispositionは、既存説明の追記 `amend` と車両専用 `rebuild_vehicle` が中心である。外部AIが「タスク追加・依存関係変更・成果物契約変更」を要求した場合、汎用案件を安全に再構成できない。

### R4. 汎用Completion Gateが意図的に完了を禁止

`app/completion_gate.py` は車両案件以外を一律 `GENERIC_NOT_ACHIEVED` とする。これは安全な暫定実装だったが、現在は販売案件を永久に未達にする。

### R5. 状態表示の判定元が分散

`workflow-readiness` では `propose_feedback.allowed=true` でも、`next-action` は `executable=false` を返す場合がある。mission、review、revision、job、gateが別々の判定を持ち、画面上の操作可否が一致しない。

## 4. 改修目標

### G1. 計画の収束

- 同じ旧レビューを新しい計画へ自動再投入しない。
- 同じ達成条件・同じ役割のタスクを重複生成しない。
- 再計画後に指摘件数が増え続けない。

### G2. 汎用案件の再構成

- GoalContractの達成条件を親として計画を再構成する。
- 外部AI指摘は命令やタスク本文ではなく、差分要求として扱う。
- タスク追加、依存関係変更、成果物契約変更を構造検査する。

### G3. 証拠付き完了

- 各criterionを独立にPASS/FAIL/BLOCKED/UNTESTABLEで判定する。
- 成果物の存在だけでPASSにしない。
- 外部実行が必要なcriterionは、人間承認、実行証拠、結果確認を必須にする。
- 全criterion PASSと人間最終確認が揃った場合だけ `achieved=true` にする。

### G4. UIの一貫性

- 状態、停止理由、次の操作、ボタン可否を同じ判定結果から生成する。
- 操作不能な場合は、必要な入力または改修理由を具体的に表示する。

## 5. 変更しない安全原則

- 人間の承認をシステムが代行しない。
- 外部AIへ秘密、個人情報、原本、認証情報を送らない。
- 外部AIの回答を実行命令として扱わない。
- 未達、未承認、読取不能を成功や0へ変えない。
- 成果物が存在するだけで目標達成にしない。
- RAG登録には明示的な `confirm_rag=true` を要求する。
- TRIZを入力不足、業務判断不足、既知不具合の代替にしない。
- Ver.22と既存成果物を削除・上書きしない。

## 6. 実装単位

### P0-1. 旧レビュー循環の遮断

対象:

- `app/plan_feedback.py`
- `app/goal_review.py`
- `app/memory/short_term.py`
- 関連テスト

変更:

1. `mission.plan_reviews` は履歴表示専用とする。
2. 現行修正入力には、計画署名と送信packet hashが一致するReviewStoreのレビューだけを使う。
3. `source_plan_version=None` のlegacy reviewは `archived` とし、`issues_for` の戻り値へ入れない。
4. legacy reviewを採用する場合は、人間が対象版を選び「現行版へ取り込む」操作を行った場合だけ `user_import` として保存する。
5. 同一provider・同一正規化本文・同一対象criterionを重複排除する。

受入条件:

- Ver.22からVer.23を生成してもlegacy reviewが自動復活しない。
- 履歴画面では旧レビューを閲覧できる。
- 現行署名に結びつくレビューだけが修正対象になる。
- 旧レビューを自動無視しても監査履歴は失われない。

### P0-2. 計画生成入力の分離

対象:

- `app/project_manager.py`
- `app/plan_feedback.py`
- `app/structured_planning.py`
- 計画生成プロンプトとschema

変更:

1. `PLANNING_FEEDBACK` にレビュー全文を渡さない。
2. 指摘を次の構造へ正規化する。
   - `issue_id`
   - `criterion_ids`
   - `category`
   - `required_change`
   - `evidence`
   - `source_signature`
3. 通常の計画生成と指摘反映を別工程にする。
4. 指摘は「満たすべき差分」としてplannerへ渡し、タスク名候補には使わない。
5. 次の構造制約を追加する。
   - `task_key` 一意
   - 正規化title一意
   - 同一criterionの主実行タスクは原則1件
   - 評価専用タスクは外部レビュー件数に比例して増やさない
   - `final_verification` は1件
   - 全criterionに実行タスク、成果物、検証方法がある

受入条件:

- 3～5社の外部AIレビューを取り込んでもタスク数がレビュー社数分増えない。
- 「計画草案の評価と改善提案」は最大1件、通常は計画外のreview工程として保持する。
- SC01～SC11が計画被覆表と1対1または明示的な多対1で対応する。

### P0-3. 汎用計画全体の再構成

対象:

- `app/plan_feedback.py`
- `app/goal_contract.py`
- `app/plan_coverage.py`
- `app/structured_planning.py`
- API/UI

変更:

1. dispositionへ `rebuild_generic` を追加する。
2. `rebuild_generic` はLLMが任意計画を直接保存する方式にしない。
3. GoalContract、既存成果物、未達criterion、採用済み指摘を入力にし、既存plannerで計画候補を作る。
4. 保存前に次を検査する。
   - 目標要求保持率100%
   - 計画被覆PASS
   - task key・出力パス重複なし
   - 依存関係に循環なし
   - 成果物契約が検証可能
   - 外部アクションが承認点を持つ
5. 人間が差分表を確認してから新しいplan versionとして保存する。

受入条件:

- 販売案件で `rebuild_vehicle` を生成・適用しない。
- タスク追加が必要な指摘を `development` だけで停止せず、実装済み汎用能力の範囲で再構成できる。
- 未実装能力は `development_required` として残し、文章追記で解決済みにしない。

### P0-4. 汎用criterion評価器

対象:

- `app/completion_gate.py`
- `app/structured_planning.py`
- `app/goal_review.py`
- 新規 `app/generic_goal_checks.py` を推奨

変更:

1. `GENERIC_NOT_ACHIEVED` の一律判定を廃止する。
2. criterionごとに次を検査する。
   - 対応タスクがcompleted
   - 必須成果物が存在
   - 必須見出し、最低文字数、CSV列・行数が合格
   - 成果物hashが現行計画と入力版に一致
   - final verificationがcriterionを明示評価
3. 外部アクションを含むcriterionは次を追加で要求する。
   - 人間の事前承認
   - 実行日時、実行者、対象、結果の証拠
   - 必要なら公開URLまたは取得結果の検証
4. 判定不能は `UNTESTABLE`、入力待ちは `BLOCKED`、不合格は `FAIL` とする。
5. 全件PASS後にだけ最終人間確認を受け付ける。

販売案件の特別条件:

- SC01～SC09: 成果物と内容検証。
- SC10: 実際の営業活動の状態と証拠。資料作成だけではPASSにしない。
- SC11: 仮説検証計画に加え、少なくとも承認済み試行結果と学習内容を要求する。
- Google Forms、Google Sites、SNS、リード取得は、それぞれの承認・公開・結果証拠を評価する。

受入条件:

- 成果物不足時は該当criterionだけFAIL/BLOCKEDになる。
- 外部行動未実施時にSC10/SC11をPASSにしない。
- 全criterion PASS、人間最終確認、hash一致時だけ `achieved=true`。
- 入力や成果物変更後は既存承認を無効化する。

### P0-5. readinessとUIの単一判定

対象:

- `app/workflow_readiness.py`
- `app/static/workflow_readiness.js`
- `app/static/goal_review.js`
- `/next-action` API

変更:

1. `allowed_actions` を次アクションAPIと画面ボタンの唯一の判定元にする。
2. `next_action.id` に対応する `allowed_actions[id]` がallowedなら `executable=true` にする。
3. 非自動操作でも「実行可能」と「自動実行可能」を分離する。
4. 次の状態遷移を明示する。
   - issues_open → propose
   - proposal_ready → human_confirm
   - confirmed → apply
   - applied → external_review
   - review_passed → plan_approval
   - approved → execution_start
5. 修正失敗時は再計画ボタンを繰り返させず、失敗分類と必要な開発を表示する。

受入条件:

- readinessで許可された操作が画面で無効にならない。
- active jobなしで「実行中」と表示しない。
- stop reasonとnext actionが矛盾しない。

### P0-6. 外部AI部分失敗の扱い

対象:

- `app/external_ai.py`
- `app/goal_review.py`
- UI

変更:

1. providerごとの成功、失敗、未実施を分けて表示する。
2. 最低合格数と必須providerを設定可能にする。
3. Gemini 429とMeta 401を計画内容の指摘として扱わない。
4. 接続失敗の再試行はprovider単位に限定する。

受入条件:

- API接続失敗から実行タスクを生成しない。
- 合格ポリシーを満たす成功レビューがあれば次工程へ進める。
- 必須provider失敗時は、接続設定の問題として停止する。

## 7. 既存案件の安全な回復

### 保存するもの

- 目標、達成条件、制約、追加指示。
- Ver.20以前の完成済み成果物。
- 外部AIレビュー原文と取得元。
- 人間承認、公開、外部実行の証拠。
- 全イベント履歴。

### 引き継がないもの

- Ver.22の重複タスク構造。
- 対象版を証明できない旧レビューの「現行指摘」扱い。
- 旧成果物に対する古い合格・承認状態。
- 販売案件に対する `rebuild_vehicle` 判定。

### 回復手順

1. DB、Workspace、設定、成果物manifestをバックアップする。
2. Ver.22をread-onlyの履歴として固定する。
3. 改修後のpreview APIでVer.23候補を生成する。
4. SC01～SC11の被覆表、タスク差分、再利用成果物、無効化される承認を表示する。
5. 人間確認後にVer.23として保存する。
6. 現行署名に対して外部AI検証を実施する。
7. 指摘が0件または全件対応済みになった後、計画を人間承認する。
8. ローカルタスクを実行する。
9. 外部実行は承認点で停止し、人が実施または明示承認した連携だけ実行する。
10. 証拠登録後にcriterion別検査と最終人間確認を行う。

## 8. テスト計画

### 単体試験

- legacy reviewが現行`issues_for`へ入らない。
- exact signature reviewだけが入る。
- 同一指摘が重複しない。
- 販売案件で`rebuild_vehicle`を拒否するだけでなく`rebuild_generic`候補を作れる。
- title、task_key、output path、criterion mappingの重複を拒否する。
- 汎用criterionのPASS/FAIL/BLOCKED/UNTESTABLEを判定できる。
- 外部行動証拠なしでSC10/SC11をPASSにしない。
- hash変更後に人間承認を無効化する。
- readiness、next-action、ボタン可否が一致する。

### 統合試験

1. 販売案件の複製DBでVer.22を読み込む。
2. legacy reviewを保持したままVer.23候補を生成する。
3. 重複タスク0件、SC01～SC11被覆PASSを確認する。
4. 外部レビュー部分失敗を再現し、接続失敗が指摘へ混入しないことを確認する。
5. 人間承認前に実行開始できないことを確認する。
6. ローカル成果物生成後、SC10/SC11が外部証拠待ちになることを確認する。
7. 証拠と最終確認後だけachievedになることを確認する。
8. 再起動後も同じnext actionへ収束することを確認する。

### 回帰試験

- 車両損益のCompletion Gateを変更しない。
- OCRの未承認・改ざん遮断を維持する。
- 経験RAGの`confirm_rag=true`を維持する。
- TRIZの採用条件を緩和しない。
- 外部AIへ原本が送られないことを確認する。
- `scripts/healthcheck.ps1` を実行する。

## 9. 実装順と完了条件

| 順番 | 実装単位 | 完了条件 |
|---:|---|---|
| 1 | P0-1 レビュー版境界 | 旧レビューが現行指摘へ自動混入しない |
| 2 | P0-2 計画生成入力分離 | 重複評価タスクを生成しない |
| 3 | P0-3 汎用再構成 | 販売案件の安全なVer.23候補を作れる |
| 4 | P0-4 汎用Gate | criterion別に証拠判定できる |
| 5 | P0-5 状態/UI統一 | next actionとボタンが一致する |
| 6 | P0-6 provider部分失敗 | 429/401が計画指摘へ混入しない |
| 7 | 既存案件移行 | Ver.22保持、Ver.23候補を人間確認可能 |
| 8 | 一連受入 | 実行→証拠→最終承認まで完走または正しい承認待ちで停止 |

## 10. 巻き戻し

- コードは改修前の配備一式へ戻す。
- DBは改修直前バックアップへ戻す。
- Ver.22および既存成果物は削除しないため、参照可能な状態を維持する。
- 新しいVer.23候補、revision、evaluationだけを無効化する。
- 公開・外部実行・RAG登録が発生している場合は、コードrollbackだけで状態を戻さず、各証拠と取消手順を確認する。

## 11. この計画で解決しないもの

- Geminiのクレジット購入。
- Meta APIキーの再発行。
- 人間による計画承認、外部公開、営業実行、最終承認。
- 実際の顧客反応や売上成果の捏造。
- 車両損益案件の未配賦225件と原本統制値不足。

## 12. 最終判定

現行Ver.22をUI操作だけで完走させることはできない。P0-1～P0-5を先に実装し、販売案件をVer.23へ安全に移行した後に実行を再開する。改修後も、外部公開や営業実行は人間承認点で停止する。それは未完走ではなく、意図した安全停止として表示する。
