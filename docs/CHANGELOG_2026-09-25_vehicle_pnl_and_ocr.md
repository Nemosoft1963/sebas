# 変更履歴 2026-09-25: 車両損益Excel生成の修正(単位0〜9)+ フォールバックPDF読取(OCR)スキル(Phase 1〜4)

対象システム: LOCALSAPORTER(ユーザーの呼称「セバス」)
作業ツール: ジェンキンス(自作コーディングエージェント。実装は Grok、検証・是正・本番反映は Claude)
本番反映日: 2026-09-25(`localsaporter-web:vehicle-ocr-20260925`)
最終テスト: 全体 **656 passed**(本番イメージ内で実測。ジェンキンス側は 655 passed, 1 skipped)

> **他のAIへ(最初に読むこと)**
> 1. 下の「8. 壊してはいけない安全原則」を弱める変更をしない。過去に、Grok が安全側の処理を「簡素化」の名目で何度も削除しようとした。
> 2. 本番反映は本書「9. 本番反映と巻き戻し」の手順に従う。`docker system prune`・`docker volume prune`・`docker compose down -v` は禁止(`AGENTS.md`)。
> 3. 実データ・実GPU・実PDFでの受入(Phase 5)と、業務確認事項36項目の回答は**未完了**。「完了」と書かない(第7・10章)。

## 1. 何のための変更か

ユーザーが提出した修正提案(`docs/Local_Cowork_修正提案_2026-09-22.md`)への対応。中心の問題は、車両別損益Excelの生成で
**「読めなかった」「原本と一致しない」「二重に数えた」が、黙って0円や確定扱いになりうる**ことだった。
GrokとChatGPT 5.6 Solに独立に設計させ、突き合わせて統合した設計書が `docs/対応設計_最終版_2026-09-23.md`(全13章。実装単位0〜9と、業務確認事項36項目を含む)。
あわせて、通常のPDF抽出が失敗したときのローカルOCR(PaddleOCR-VL-1.6 + PP-DocLayoutV3)を仕様書 `docs/OCR仕様_フォールバックPDF読取スキル_2026-09-24.md` に従って実装した。

## 2. 実装単位と結果(設計書 第11章の単位)

| 単位 | 内容 | 主な変更先 | 全体テスト |
|---|---|---|---|
| 0 | 保全と記録(文書のみ) | `docs/CHANGELOG_2026-09-21_triz_import.md` に訂正節 | — |
| 1 P0-1 | 金額の状態を分離: 値 / 空欄 / 読取不能 / 範囲外。実額0と読取不能を区別。`parse_money` | `app/vehicle_auto.py` | (487 まで) |
| 2A P0-2/3 | 原本合計との独立照合の純粋関数 `reconcile_sources` | `app/vehicle_reconciliation.py`(新規) | |
| 2B | 請求書の「鑑」を除外せず照合の基準(control)にする。請求の重複判定 `classify_invoice_relation` | `app/vehicle_auto.py` | |
| 2C | 確定ゲートを独立照合に接続。`source_controls` が空でも合格する経路を廃止 | `app/vehicle_workflow.py`, `app/vehicle_profit.py`(「原本照合」「配賦照合」シート) | 487 |
| 3 P1-1 | 進捗と停止理由の判定 `GET /api/projects/{id}/workflow-readiness` | `app/workflow_readiness.py`(新規), `app/static/workflow_readiness.js` | |
| 4 P1-2 | 計画修正→再検証→承認→実行の接続。永続ジョブ・冪等キー・外部送信の明示承認 | `app/plan_feedback.py`, `app/goal_review*.py`, `app/web.py` | 497 |
| 5 P1-3 | 月別配賦(`AllocationRule`)、社員の車両乗替、判断の継承、社会保険は明示額のみ | `app/vehicle_auto.py`, `app/vehicle_service.py`, `app/vehicle_workflow.py` | 509 |
| 7 P2-1 | TRIZ課題→業務回復。`AdapterSpec`、状態機械、`VehicleFailure`、限定再実行と撤回 | `app/automatic_triz.py` | 519 |
| 8 P2-2 | 人間承認済み手順の再利用。`RecipeSpec`。金額は再利用しない | `app/executable_recipes.py` | 528 |
| 9 | 検証記録(文書のみ) | `BUILD_REPORT.md`, `docs/verification/unit9_resume_manifest_2026-09-24.md` | 528 |
| OCR-1 | OCR Phase 1: スキーマ、原本ハッシュとmanifest、localhostだけのクライアント、能力登録、起動しない compose 定義 | `app/ocr_schema.py`, `ocr_artifacts.py`, `ocr_client.py`, `docker-compose.ocr.yml` | 568 |
| OCR-2 | Phase 2: 起動判定TRG-01〜09、検算VAL-01〜12、強制停止、追加型DB6表、ページ単位チェックポイントで再開できるジョブ | `app/ocr_fallback.py`, `app/ocr_store.py`, `app/memory/short_term.py` | 612 |
| OCR-3 | Phase 3: 請求書アダプター(前月請求・入金・当月買上を分離)、車両損益へのOCR解決ゲート | `app/ocr_invoice_adapter.py`, `app/vehicle_workflow.py`, `app/vehicle_auto.py` | 620 |
| OCR-4A | Phase 4: OCR要求/状態/成果物/根拠/承認/公開/RAG登録のAPI | `app/ocr_review.py`, `app/web.py`, `app/experience_memory.py` | 638 |
| OCR-4B | Phase 4: ファイル一覧とレビュー画面(原本とbboxの左右比較) | `app/static/ocr_review.js`, `ocr_review.css`, `index.html` | 655 |

`extractor` の版 `REVISION` は `20260923.3`(`app/vehicle_auto.py`)。版が変わると、既存案件の抽出は「再抽出が必要」になる。

## 3. 動作の要点(利用者から見えるもの)

- 実案件を新しい判定で見ると、原本合計が無いものは**確定扱いにならず「原本の正確性を確認してください」(`accuracy_blocked`)**になる。Excelは暫定扱い(`provisional=True`、`final_completed=False`)。
  本番の「車両別評価データの作成」案件は、この状態(未配賦225件)だった。これは不具合ではなく設計どおり。
- 画面のボタンの可否は、サーバーの `allowed_actions` で決まる。未実装の機能は選べない。
- 「車両の配賦」は、既定では**その月だけ**に適用する(`apply_to='this_month'`)。全月を1台に一括で振らない。
- TRIZの74件は「定義済み」の参照ライブラリであり、回復件数・採用数に数えない。文字コードの復元は `encoding_repaired` と表示し、業務合格とは扱わない。
- **OCRは既定で無効**(環境変数 `LOCALSAPORTER_OCR_ENABLED` が真のときだけ動く)。無効のとき、OCR要求APIは拒否し、OCRを起動しない。

## 4. 追加・変更したAPI

- `GET /api/projects/{id}/workflow-readiness`(進捗・停止理由・次の操作・許可/不許可の操作)
- `POST|GET /api/projects/{id}/context-files/{file_id}/ocr`(OCR要求は冪等キー必須で202。GETはそのファイルのrun一覧)
- `GET /api/projects/{id}/ocr/{run_id}`(状態。`ocr_status` / `adoption_status` / `rag_registered` を**別々に**返す)
- `GET .../ocr/{run_id}/artifacts` と `.../artifacts/{name}`(成果物。パストラバーサル拒否、manifestのSHA-256検査、png/md/jsonのみ、`nosniff`)
- `GET .../ocr/{run_id}/evidence/{field_id}`(値の根拠のページ・領域)
- `POST .../ocr/{run_id}/review`(approve / correct_and_approve / reject)、`POST .../publish`、`POST .../rag`(`confirm_rag` が明示されたときだけ登録)
- 既存の `goal-review` 系APIに、修正案の取り込み・提案・適用と、ジョブ(冪等キー・409重複・再開)を追加

## 5. データベース

追加のみ(既存テーブルは変更していない)。`app/memory/short_term.py` の `ensure_ocr_tables` が冪等に作る。
`ocr_runs` / `ocr_pages` / `ocr_blocks` / `ocr_fields` / `ocr_validations` / `ocr_reviews`。**原本のバイト列は複製しない**(`context_file_id` と `source_sha256` で参照)。
`ocr_runs` には追加列 `idempotency_key` / `adoption_status` / `rag_registered` / `published_at` / `artifact_root` / `rag_experience_id` / `rag_revoked`。

## 6. OCRサービス(定義のみ。未導入・未起動)

`docker-compose.ocr.yml`(`profiles: ["ocr"]`。既定の `docker compose up` では起動しない)。公開ポートは `127.0.0.1:18118` のみ。
イメージ(digest固定。**実機の `docker images` が正**。仕様書にはdigestしか無く、イメージ名は実機で確認した):
- `ccr-2vdh3abv-pub.cnc.bj.baidubce.com/paddlepaddle/paddleocr-genai-vllm-server@sha256:5713fd30ab76094b7b6a20d95fd8e26fa9dc452bcc90ccb16f1fb056bd2a0f4d`
- `ccr-2vdh3abv-pub.cnc.bj.baidubce.com/paddlepaddle/paddleocr-vl@sha256:ad0b1f056a76967f9191cd06398e8babb21b49a4673a28c3de5fd31f481884db`
Docker socket・`C:\` 全体・`Users` 全体はマウントしない。OCRの能力(`source.ocr_pdf_fallback` ほか5つ)は登録済みだが `validated=false`(「設定がある」ことを「実行検証済み」としない)。

## 7. 未完了・未検証(完了と書かないこと)

- **Phase 5 未実施**: 実PDF 50文書・200ページ以上・5帳票種以上での精度測定(ACC-01〜10)、PaddleOCRサービスの実起動とGPUでの確認、OCR経路を使った車両損益の月別計算の受入、ロールバックの実証。
- **業務確認事項36項目(設計書 第13章、`業務確認事項36項目.md`)は未回答**。未回答の間は「足りない業務事実は推測せず、issueにして止める」既定で実装している(料率・日割り・均等配賦・税丸め・許容差の例外を推測しない)。
- **単位6(実原本49件を通常経路で通す受入)未実施**。実データを扱うため、ユーザーの明示指示が必要。
- 画面(`ocr_review.js`)は、模擬APIのページで描画・XSS無害化を確認したが、**実サーバー上での画面確認とスクリーンショットは未実施**。
- 本番の実データで OCR を実行したことはない。

## 8. 壊してはいけない安全原則

1. **「読めなかった」「不明」「未承認」を、0円・問題なし・成功・承認済み・OCR不要にしない。** 空欄・0・読取不能は別の状態として持ち続ける。
2. **前月請求額・入金額・繰越額を、当月の費用へ配賦しない。** 車両番号の無い行は車両原価明細にしない。
3. **原本の独立した合計が無いものを確定扱いにしない。** `source_controls` の空配列を合格としない。
4. **OCRの `failed`・`needs_review`・未承認・改ざんを、後続計算や経験RAGへ渡さない。** 承認・公開・RAG登録の直前に、原本SHA-256と成果物manifestを毎回再検証する。
5. **人間承認前のRAG登録は0件。** 既定は登録しない。承認しただけでも登録しない(`confirm_rag` の明示が要る)。
6. **ページ数はクライアントの入力や既定値でなく、保存済み原本PDFから求める**(`resolve_ocr_page_spec`)。開けない・501ページ以上は拒否する。
   ページ別の抽出情報が空(=読めなかった)のときは、全ページを `unreadable` として OCR 対象にする(`evaluate_ocr_triggers`)。判定材料がゼロのときも `OCR_NOT_REQUIRED` にしない。
7. **OCR結果は信頼できない外部データ。** 画面で `innerHTML` に入れない(`textContent`/`createElement`のみ)。原本や個人情報を外部AI・外部OCRへ送らない。localhost以外のOCRサービスを拒否する。
8. 動的コード実行(`exec`/`eval`)でアダプターを適用しない。採用は、リポジトリ内の関数IDへの明示分岐のみ。
9. 上の原則を弱める変更は、理由の明記と、安全性が下がらないことを示すテストが要る。

## 9. 本番反映と巻き戻し

- 反映内容: 上記すべて。OCRは既定無効。本番の `web` コンテナだけを再作成(`docker compose up -d --no-build --no-deps web`)。他のサービスには触れていない。
- 反映前の退避: 旧イメージ `localsaporter-web:before-vehicle-ocr-20260925`(=反映前の `latest`)、ソース `.jenkins_backup\20260925-020324\`、データ `C:/Users/example\Short-video-local\backup_prod_20260925\localsaporter_app_data.tar.gz`。
- 巻き戻し: `docker tag localsaporter-web:before-vehicle-ocr-20260925 localsaporter-web:latest` → `docker compose up -d --no-build --no-deps web`。
- 本番へは、経験RAG作業の残骸(`experience_memory.json`、`experience_memory/`、`experience_lessons/`、`success-cases-export.json`、RAG登録用スクリプト)とビルド生成物(`local_voice_ai.egg-info/`)を**入れていない**。
- 本番の `app/web.py` は取り込み後に1行(`FRONT_AI_PROVIDER` の既定 `chatgpt`→`ollama`)変更されていた。ジェンキンス側にも同一内容が入っていたため上書きした。
- 検証: 本番イメージ内で全テスト656件合格(`tests/` などは NAS を直接マウントできないため、ローカルへコピーして読み取り専用でマウントし、`.env` は含めない)。`scripts/healthcheck.ps1` の統合フロントは PASS。
- 注意: PowerShell 5.1 は BOM なし UTF-8 の `.ps1` を文字化けさせる(BOM付きで保存する)。Git Bash から `docker run -v Z:...` は NAS を直接マウントできない。

## 10. 運用上の既知事項(今回の変更とは無関係)

- 本番アプリのモデル選択は `/data/memory/local_model.json`(データボリューム)に保存され、`.env` の `OLLAMA_MODEL` より優先される。変更は `PUT /api/local-models/selected`(実行中の処理が無いときだけ可)。現在は `mistral-small3.2:24b-instruct-2506-q4_K_M`。`gpt-oss:20b` と `qwen3.5:9b` も取得済み。
- 効いている Ollama は Docker の `llmhib-ollama`(11434)1つ。`mistral-small3.2`(15GB)はGPU16GBに載り切らず、CPU/GPU併用で動く。
- Open WebUI・Computer・Google Publisher Browser は、Docker Desktop の終了時から停止している。
- OCR の検証・開発に使った作業コピーは、ジェンキンス(`http://localhost:8012/`)のプロジェクト `LOCALSAPORTER` にある。作業指示書は各単位の `docs/改修依頼_*.md`。
