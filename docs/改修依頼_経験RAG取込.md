# 改修依頼: 成功事例収集エージェントのデータを LOCALSAPORTER の経験RAGに取り込む(調査→承認→実装)

## 背景
LOCALSAPORTER には経験RAGの仕組みが実装済みだが、初期状態はoff。
- `app/experience_cli.py` / `app/experience_memory.py` / `app/experience_store.py` に実装がある
- `EXPERIENCE_RAG_OPERATIONS.md` に運用手順が書かれている(必ず読むこと)
- 依存関係(chromadb, langchain-chroma, langchain-ollama)は、この作業コピーにはまだインストールされていない(`.venv`が存在しない)
- `experience_memory.json`(設定ファイル)は空("projects": {})
- 別プロジェクト「成功事例収集エージェント」で、承認済み30件の成功事例をJSON出力できる
  (`success-cases-export.json`、各項目は title/situation/action/outcome/success_factors/conditions/evidence 等の多項目形式)
- 経験RAGが受け取る形式はもっと短い教訓形式:
  ```json
  {"kind":"success","content":"(短い一般化された教訓文)","applicability":{},"evidence":{"source":"..."}}
  ```
  形式が違うため、変換が必要。

## やること

### フェーズ1: 調査(実装前に必ず報告)
1. `EXPERIENCE_RAG_OPERATIONS.md`、`EXPERIENCE_RAG_DESIGN.md`、`app/experience_cli.py`、`app/experience_memory.py`、
   `app/experience_store.py`、`experience_memory.example.json`、`tests/test_experience_memory.py` を読む。
2. LOCALSAPORTER が使う「project ID」がどのような値を取るか(既存コードやテストの中で、実際にどんな文字列が
   project IDとして使われているか)を確認し、今回の経験をどの project ID に紐づけるべきか(既存のどれかに乗せるのか、
   新規IDを作るのか)を報告する。
3. RAG依存パッケージのインストール方法(`pip install -e ".[dev,rag]"`)を実際に試し、専用の `.venv` を
   このプロジェクトディレクトリ内に作って導入する。バージョンは `requirements-rag-tested.txt` を参考にする。
   導入ログ・エラーがあれば報告する。
4. 埋め込みモデル `nomic-embed-text:latest` が Ollama(`http://host.docker.internal:11434` または
   `http://127.0.0.1:11434`)で使えるか確認する。無ければ `ollama pull nomic-embed-text` が必要な旨を報告する
   (pull自体の実行は承認を得てから)。
5. 上記の確認結果を日本語で報告し、承認を得てから次のフェーズに進む。

### フェーズ2: データ変換(承認後)
- 提供する `success-cases-export.json`(承認済み30件)の各項目を、経験RAGの登録形式
  `{"kind":"success","content":"...","applicability":{},"evidence":{"source":"..."}}`
  に変換するスクリプトを書く。
  - `content` は、situation(状況)・action(施策)・outcome(成果)を1〜3文程度に要約した、
    一般化された教訓文にする(固有名詞は残してよいが、簡潔に)。
  - `evidence.source` には、元の `source_url` を入れる。
  - `applicability` は空オブジェクト `{}` のままでよい(当該project内で一般化した教訓として扱う)。
- 変換したJSONファイルを、経験ごとに1ファイルとして保存する(`app.experience_cli.py add` が1ファイル=1レコード想定のため)。

### フェーズ3: 登録(承認後)
- `experience_memory.json` に対象project IDを **shadow モード**で追加する(enforce にはしない。
  理由: 今回は初回投入であり、いきなり回答に挿入せず、まず監査記録のみで様子を見るため)。
- `.venv` の専用Pythonで、30件それぞれについて:
  1. `experience_cli.py add <file>` で登録
  2. `experience_cli.py review <id> --status verified --reviewer operator --proof "成功事例収集エージェントでの人手レビュー済み"` で検証済みにする
  3. 全件登録後に `experience_cli.py index --config experience_memory.json` で索引を作る
- 最後に `experience_cli.py stats` で件数を報告する。

## 制約
- **本番LOCALSAPORTER本体の再ビルド・起動・`INSTALL_EXPERIENCE_RAG=1`でのDockerビルドは、今回のタスクでは行わない。**
  ここまでの作業は、あくまで作業コピー内の`.venv`とローカルファイルの範囲に留める。
  本番反映は別途、人間の判断で行う。
- 各フェーズの区切りで、確認結果・実行計画を報告し、承認を得てから次に進む。
- 実行していないことを「できた」と書かない。
- 既存のLOCALSAPORTERのコード(experience_memory.py等の実装)は変更しない。変換スクリプトと設定ファイル追記のみ。
