# セバス 全体状況・再開指針

- 記録日: 2026-09-29 JST
- 呼称: セバス（Sebas）
- 公開リポジトリ: https://github.com/Nemosoft1963/sebas
- 公開版記録コミット: `cf73ce3f49d06ace991e3a9b1c886ba9463e2836`
- 本番ルート: `Z:\AI_Automater\LOCALSAPORTER`
- ワークスペース: `C:\Users\kanto\LocalCowork\workspace`

## 1. 現在の結論

セバスは稼働中で、目標・計画・実行・検証・成果物、OCR、車両別月次損益、経験RAG、手順学習、TRIZ、外部AIレビューに必要な主要部品を持つ。2026-09-29のhealthcheckは全項目PASSである。

ただし、全案件を「目標分解から未達差分解消まで自動で一本化する制御」は完成していない。機能実装済みと実案件の目標達成済みを区別する必要がある。車両別月次損益の実案件一連受入、業務確認事項のfact台帳との照合、代表的な実PDFによるOCR受入は完了と記録しない。

## 2. 配備と版管理

| 対象 | 状況 |
|---|---|
| GitHub公開版 | `main`、記録時HEAD `cf73ce3`、Apache-2.0、公開中 |
| 公開版CI | pytest成功、CodeQL成功、Trivyコンテナ検査成功 |
| 本番 | Dockerサービス稼働、healthcheck PASS |
| 本番Git | 本番ルートはGitリポジトリではない |
| 公開版と本番の一致 | 自動証明できない。デプロイmanifestまたは配備コミット記録が必要 |

公開版のセキュリティ修正は本番未反映として管理する。`cf73ce3` は履歴記録コミット、最終セキュリティ実装コミットは `1469336` である。

## 3. 稼働サービス

2026-09-29の `docker compose ps` と `scripts/healthcheck.ps1` による観測。

| サービス | 状況 | 接続・備考 |
|---|---|---|
| Integrated Front / web | healthy / HTTP 200 | `127.0.0.1:8099` |
| Open WebUI | healthy / HTTP 200 | `127.0.0.1:3000` |
| Computer | 稼働 / HTTP 200 | `127.0.0.1:8000` |
| Google Publisher Browser | healthy | `127.0.0.1:8010`、setup=False、version 0.9.21 |
| PaddleOCR document parser | 稼働 | Docker内部サービス |
| PaddleOCR VLM | 稼働 | Docker内部サービス、GPU利用構成 |
| Ollama | PASS | `127.0.0.1:11434` |

外部公開ポートは確認範囲でlocalhostに限定されている。

## 4. ローカルモデル

| 項目 | 状況 |
|---|---|
| 現在の設定モデル | `mistral-small3.2:24b-instruct-2506-q4_K_M` |
| `gpt-oss:20b` | model list確認PASS |
| `qwen3.5:9b` | model list確認PASS |

2026-09-25以前の資料にある「既定 `gpt-oss:20b`」は現在値ではない。

## 5. 機能別状況

| 領域 | 実装状況 | 実運用・受入状況 |
|---|---|---|
| プロジェクト・原本 | 登録、抽出、Workspace隔離、履歴を実装 | 稼働中 |
| 目標・計画 | 目標、条件、制約、構造化計画、依存関係を実装 | 長い目標の条件分解品質に課題 |
| 外部AI計画検証 | 指摘取得、ローカル統合、再計画を実装 | 外部送信はユーザー明示時のみ。秘密・個人情報禁止 |
| 実行制御 | タスク、queue、job、checkpoint、再開を実装 | 複数状態系の完全統合は未完了 |
| 完了判定 | readiness、GoalContract、Gate、確定/暫定表示を実装 | 全領域共通の単一Completion Gateは発展途上 |
| 成果物 | 棚、ダウンロード、manifest、改ざん検査を実装 | 生成済みと目標達成済みを分離表示 |
| 車両別月次損益 | 抽出、配賦、月次計算、Excel、独立照合、証拠追跡を実装 | 実案件で全車両・全月・未解決0・独立照合PASSの一連受入は未完了 |
| OCR | PaddleOCR配線、実行、証拠、レビュー、採用、RAG取消を実装 | 改善工程は終了扱い。代表実PDF受入の記録は未完了 |
| 経験RAG | 人間承認、期限、取消、project隔離、成功事例JSON取込UIを実装 | `confirm_rag=true` 必須。未承認・失敗・改ざん結果は登録禁止 |
| 手順学習・模倣 | 成功手順の保存、再適用、条件検査を実装 | 再利用後も元の受入条件を再検証する |
| TRIZ | 発明候補、分野横断分類、実験、採否、限定再実行を実装 | 入力不足・業務判断・既知不具合へは使わない。候補だけで完了しない |
| UI | 進行状態、停止理由、次行動、質問、OCR、成果物、RAG UIを実装 | 単一の次行動制御と画面統合は継続課題 |
| 公開・配布 | README、画面画像、構成図、SBOM、ライセンスを整備 | preview公開中 |

## 6. 目標達成制御

望ましい処理順は次のとおり。

1. 目標と達成条件をGoalContractへ分解する。
2. 計画が全条件を覆うか検査する。
3. 必要なら外部AIの指摘を取得し、ローカルで反映する。
4. 人間承認後に実行する。
5. 原本、変換、成果物、検査証拠を結ぶ。
6. 未達差分を分類する。
7. 入力不足・業務判断・既知不具合を先に処理する。
8. 承認済みRAGが適用可能なら限定再利用する。
9. 未知の方法不足または反復失敗だけTRIZへ送る。
10. 元の達成条件を再検証し、人間確認後だけ確定完了にする。

現状はmission、review、job、vehicle、TRIZ、RAGが別の状態を持つ。`workflow_readiness.py` が統合表示するが、完全な単一状態機械ではない。

## 7. 車両別月次損益

### 実装済み

- 給与、社会保険、燃料、リース、その他費用の構造化。
- 車両・月・人物・会社の対応と配賦。
- 車両別・月別計算と車両別シートを含むExcel生成。
- 統制値、未配賦、対象期間、車両件数、独立照合の判定。
- セルから入力明細、原本位置、配賦根拠へ戻る読み取り専用証拠グラフ。
- 未解決事項を影響順に出す質問カードと、回答者必須の版付きfact保存。

### 未完了・再確認事項

- 2026-09-25資料の案件スナップショットは `accuracy_blocked`、未配賦225件、独立照合unavailableだった。これは過去時点の値であり、現在DBで再計測が必要。
- 業務確認36項目について、会話上は対象者・会社・車両配布等の回答が追加されたが、全回答が版付きfact台帳へ保存・適用済みかは今回未監査。
- 完了条件は全対象車両×全対象月、未解決0、原本統制値一致、独立照合PASS、人間確認済みである。

## 8. OCR

- `LOCALSAPORTER_OCR_ENABLED` は安全上、既定無効の原則を維持する。
- 現在はPaddleOCRの2コンテナが稼働しているが、稼働と案件ごとの採用可能状態は同義ではない。
- `failed`、`needs_review`、未承認、manifest/SHA-256不一致を後続計算や経験RAGへ渡さない。
- 読めない値を0円にしない。空欄・0・読取不能は別状態にする。
- OCRは入力取得機能であり、元業務の目標達成判定ではない。
- OCR改善工程は終了扱いとし、新規改善提案より不具合切分け・回帰防止・実案件受入を優先する。

## 9. 経験RAG・手順学習

- 人間が結果を確認し、実行者・証拠・適用条件を確認した場合だけ登録する。
- `confirm_rag=true` を必須とする。
- project、入力版、期限、取消、原本hashを適用時に再検査する。
- RAGで手順が見つかっただけでは成功にしない。適用後に現案件の達成条件を再検証する。
- ChromaDBは標準公開コンテナで `INSTALL_EXPERIENCE_RAG=0` とし、既定では導入しない。

## 10. TRIZ

- 未知の方法不足または同一条件での反復失敗に使う。
- `SOURCE_READ_FAILED`、原本照合失敗、統制値不足、未配賦、業務fact不足、既知開発課題はTRIZ対象外。
- 候補、試験条件、影響、再現試験、転用試験、採否を記録する。
- 再現と転用の試験に合格した手順だけ限定採用する。
- TRIZ候補生成やユーザーへの質問だけで処理完了にしない。

## 11. セキュリティ・公開版

2026-09-29のGitHub確認結果。

| 項目 | 状況 |
|---|---|
| pytest | success、run `36524127966` |
| security workflow | success、run `36524127937` |
| CodeQL Critical | 0 |
| CodeQL High | 0 |
| CodeQL Medium | 11（継続確認） |
| Trivy Critical/High | 0 |
| Dependabot | ChromaDB関連 Critical 2、High 2。修正版未公開 |
| ライセンス | Apache License 2.0 |
| SBOM | ReleaseごとのCycloneDX生成を設定 |

Google Sites公開確認のSSRF、公開Web URL境界判定、ReDoS、コンテナ依存脆弱性は公開版で修正済み。OCRパス警告High 3件は、許可名・ルート境界・manifest・SHA-256・拒否テストを確認し、根拠付きfalse positiveとして整理済み。

## 12. 安全原則

- 読取不能・不明・未承認を0円・成功・承認済みに変えない。
- 前月請求、入金、繰越を当月費用へ配賦しない。
- OCRの失敗、確認待ち、未承認、改ざん結果を後続処理へ渡さない。
- 人間承認前に経験RAGへ登録しない。
- 外部AIへ秘密・個人情報・プロジェクト原本を送らない。
- 成果物が存在しても達成条件が不合格なら終了しない。
- 入力不足や既知不具合をTRIZへ逃がさない。
- 大量削除、Docker volume削除、環境初期化を行わない。

## 13. 主要な未完了事項

優先順。

1. 公開版と本番配備内容をmanifestまたはコミットで照合可能にする。
2. 車両案件の現在値を再計測し、36項目回答のfact台帳保存・適用漏れを監査する。
3. 全車両・全月、未解決0、独立照合PASSまで実案件の一連受入を完了する。
4. GoalContract、計画被覆、Next Action Controller、共通Completion Gateを単一ループとして完成させる。
5. 代表的な実PDFでOCRの受入記録を確定する。OCR機能追加ではなく実証・回帰確認として扱う。
6. CodeQL Medium 11件を内容別に確認する。
7. ChromaDBの修正版公開を監視し、Experience RAG有効化前に再検査する。
8. 古い `BUILD_REPORT.md` の「最新」表記と新しい履歴の時点差を整理する。

## 14. 次に再開するときの手順

1. このファイルを読む。
2. `AGENTS.md` と `docs/CHANGELOG_2026-09-25_vehicle_pnl_and_ocr.md` を読む。
3. `docker compose ps` と `scripts/healthcheck.ps1` を実行する。
4. 公開版 `main` と本番ファイルmanifestの差を確認する。
5. feature flag、OCR flag、現在の案件状態を読み取りで確認する。
6. 車両案件のreadiness、未配賦、統制値、独立照合、質問factを再取得する。
7. 未解決の業務事実だけを人へ質問する。
8. 変更後は関係テストとhealthcheckを実施し、`BUILD_REPORT.md` と変更履歴を更新する。

## 15. 記録の限界

- 本番ルートがGit管理されていないため、ファイル単位の公開版一致は今回証明していない。
- Dockerサービスの稼働とHTTP healthは確認したが、各案件の実データを変更する受入試験は実施していない。
- OCRコンテナの稼働は確認したが、新しい実PDFを投入していない。
- 36項目の回答内容そのものや個人情報は、この全体状況ファイルへ複写していない。
- 外部AIへデータを送信していない。

## 16. 参照資料

- `docs/SEBAS_CURRENT_SYSTEM_GUIDE_FOR_AI_2026-09-25.md`
- `docs/SEBAS_GOAL_COMPLETION_REORGANIZATION_2026-09-25.md`
- `docs/CHANGELOG_2026-09-25_vehicle_pnl_and_ocr.md`
- `docs/CHANGELOG_2026-09-26_goal_completion_phase3a.md` ～ `phase3d.md`
- `docs/CHANGELOG_2026-09-26_goal_completion_phase5a.md`、`phase5b.md`
- `docs/CHANGELOG_2026-09-26_ocr_phase5a.md`
- `docs/CHANGELOG_2026-09-26_ocr_unit_b.md`
- `docs/CHANGELOG_2026-09-28_agent_archive_experience_import.md`
- `docs/CHANGELOG_2026-09-28_experience_import_ui.md`
- `docs/CHANGELOG_2026-09-29_security_scanning_hardening.md`
- `BUILD_REPORT.md`
