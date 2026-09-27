# 分析比較: Claude・Gemini・Grok(LOCALSAPORTER 2026-09-21)

同じ指示(読み取り専用・同一プロンプト)で、3つのAIに調査させた結果の突き合わせ。
Gemini と Grok には独立性のため `docs/analysis/`(他のAIの報告)を読まないよう指示し、3者とも読んでいないことをツール呼び出しの記録で確認した。

| 項目 | Claude | Gemini | Grok |
|---|---|---|---|
| モデル | claude-opus-5 | gemini-3.7-flash | grok-4.6 |
| 報告 | `claude-20260921-115043.md` | `gemini-20260921-130955.md` | `grok-20260921-131513.md` |
| ツール呼び出し | 43回(読取34 / 一覧9) | 25回(読取19 / 一覧6) | 84回(読取68 / 一覧16) |
| 未読の明示 | 未読モジュールを列挙、推測に「推測:」 | なし | 未読ファイルを章立てで列挙、推測に「推測:」 |
| 調査時のプロジェクト状態 | 取り込み機能の追加前 | 追加後 | 追加後 |

## 1. 3者が一致した指摘(信頼度が高い)

1. **認証・認可がない。** localhost バインドだけが防御境界。
2. **`app/web.py` に責務が集中している。** 実際の行数は2917行(Gemini の「1,500行を超え」は過小)。
3. **実行モニターの状態(`executions`)がメモリ上にあり、再起動で消える。** 実際は117行目。
4. **TRIZ収集データの受け口は `POST .../learning/general`。** 8項目が既存の入力項目と一致する。
   ただし Gemini と Grok が見たときは取り込み機能が既にあったため、この点は、設計の独立な一致ではなく既存実装の説明である。

## 2. Claude と Grok が指摘し、実ファイルで確認できたこと(Gemini は指摘なし)

- **`validate_problem` が `None` を文字列 `"None"` にする**(`app/triz_common.py`)。
- **テスト件数の記録が食い違う**: `MANIFEST.json`=13、`SYSTEM_DESCRIPTION_FOR_AI.md`=50、`BUILD_REPORT.md`=403(Grok は 414 も併記)。
- **`lifespan` が複数回ラップされる構造**(Claude R5 / Grok の注意点)。
- **`app/main.py` + `config/settings.yaml` と Web 経路の二重実装**(Claude R9 / Grok 低12)。
- **`.env.example` の公開事業者情報**の配布時の注意(Claude R10 / Grok 低14)。

## 3. Grok だけが指摘し、実ファイルで確認できたこと(重要)

- **`FRONT_AI_PROVIDER` のコード既定が `chatgpt`**(`app/web.py` 99行目)。compose と `.env.example` は `ollama`。
  compose を使わずに起動した場合や、変数が欠けた場合、通常チャットが外部AIに送信され得る。Claude も Gemini も指摘していない。
  (Grok は行番号を「89行」と書いており、実際は99行目。)
- **イメージタグが固定されていない**: open-webui は `main`、computer と qdrant は `latest`(`docker-compose.yml`)。
- **経験RAGの依存は Docker 既定で入らない**(`INSTALL_EXPERIENCE_RAG=0`)。
- **承認後メールは、SMTP 設定があれば実際に外部へ送信される**(`_send_approved_email`)。

## 4. Claude だけが指摘したこと(実ファイルでの確認は一部のみ)

- 確認済み: `physical` の分布(`null` が多数、文字列 `"False"` が5件)。
- 未確認: `LearningStore` が1行1JSON の設計、`requirements.txt` と Dockerfile の二重管理、`app/config.py` が Web 系で未使用、コードスタイルの不統一。

## 5. Gemini だけが指摘したこと

- **SQLite のロック競合のリスク。** 根拠となる読み取りは見当たらず、推測の域を出ない。
- **外部AIのモデル名の脆弱性**(`app/external_ai.py`)。既定のモデル名がコードにあるのは事実だが、Gemini はこのファイルを読んでいない。

## 6. 確認できた誤り

| AI | 記述 | 事実 |
|---|---|---|
| Gemini | `DOMAINS` = software / web_service / accounting_tax / logistics / creative | 実際は documents / research / software / workflow / operations / browser / general の7つ |
| Gemini | `executions` は69行目 | 117行目 |
| Gemini | `SYSTEM_DESCRIPTION_FOR_AI.md` 384-391行目を認証の根拠に | ボリュームの表の箇所で、認証の記述ではない |
| Gemini | `external_ai.py`、`workspace_files.py`、`project_manager.py`、`short_term.py`、`triz_general.py` を根拠として引用 | 読んだ記録がない(未読の明示もない) |
| Grok | `FRONT_AI_PROVIDER` は `app/web.py` 89行目 | 99行目(内容は事実) |

Claude は、確認した3点(`validate_problem`、`physical` の分布、テスト件数)がすべて一致し、誤りは見つかっていない。

## 7. 評価と使い方

- **正確性の順位(今回の確認に基づく):** Grok ≧ Claude > Gemini。
  Grok は、確認した主張がほぼ事実で、行番号のずれが1件だけだった。Claude は確認した範囲で誤りなし。
  Gemini は、列挙値の誤りと、未読ファイルの引用があった。
- **網羅性:** Grok は読んだファイル数が最も多く(68)、Claude と Grok の指摘は重なりつつ、それぞれ独自の重要な指摘を持つ。
  特に、`FRONT_AI_PROVIDER` の既定は Grok だけが見つけた。
- **推奨:** 改修の優先度は、3者一致の項目(認証、`web.py`、状態のメモリ保持)を上位にし、
  次に、Claude と Grok の重複項目(`None` → `"None"`、テスト記録、`lifespan`)を置く。
  `FRONT_AI_PROVIDER` の既定値は、外部送信に関わるため、単独の指摘でも早めに確認する価値がある。
- **検査方法についての注意:** 「本文に引用されたファイルが、読んだファイル一覧に含まれるか」を機械的に検査するだけでは、
  未読を正直に明記した報告(Claude、Grok)も引っかかってしまう。「未読」と明記されているかも見る必要がある。
