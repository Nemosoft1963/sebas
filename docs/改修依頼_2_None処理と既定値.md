# 改修依頼 2: 2件の小さな修正(既存コードの変更を含む)

## 背景
`docs/analysis/` の3つの調査報告(Claude / Gemini / Grok)と比較 `docs/analysis/comparison-claude-vs-gemini.md` で、
次の2点が、実ファイルで確認済みの指摘として残っている。まず自分でも該当コードを読み、事実であることを確認してから修正すること。
なお、`docs/analysis/` の報告は参考であり、コードを正とする。行番号は報告と違う場合がある。

## 修正 1: `app/triz_common.py` の `validate_problem` が `None` を文字列 `"None"` にする
- 現状: `problem={k:str(values.get(k,'')).strip() for k in FIELDS}`。値が `None` だと `"None"` になり、
  `physical` は空を許す項目なのに "None" という文字列が保存され、LLMプロンプトにも入る。
- 修正: 値が `None` のときは空文字として扱う。`None` 以外の値の扱い(文字列化、前後の空白除去、必須項目の空チェック、2500文字上限)は変えない。
- テスト: `physical=None` で `problem['physical']==''` になること、`goal=None` などの必須項目が `None` のときは従来どおり `ValueError`
  になること、通常の文字列は変わらないことを、`tests/` に新規テスト(例 `tests/test_triz_common_none.py`)として追加する。
  修正を外すと落ちることを、実際に確認すること(修正前のコードで新規テストを実行して、失敗を確かめる)。

## 修正 2: `app/web.py` の `FRONT_AI_PROVIDER` のコード既定が `chatgpt`
- 現状: `FRONT_AI_PROVIDER = os.getenv("FRONT_AI_PROVIDER", "chatgpt")`(99行目付近)。
  `docker-compose.yml` と `.env.example` の既定は `ollama`。compose を使わない起動や変数の欠落で、通常チャットが外部AIへ送信され得る。
- 修正: コード既定を、compose・`.env.example` と同じ `"ollama"` にする。それ以外(値の正規化、`PROVIDERS.get(...)` の扱い)は変えない。
- 確認: `"ollama"` のとき `PROVIDERS.get(FRONT_AI_PROVIDER)` が何を返し、外部AIが呼ばれない経路になることを、コードを読んで確認し、
  最終報告に根拠(関数名)を書く。他に `"chatgpt"` を既定として前提にしている箇所が無いかも、grep で確認して報告する。
- テスト: 環境変数 `FRONT_AI_PROVIDER` が未設定のときの既定値が `"ollama"` であることを確かめるテストを追加できるか検討する。
  `app.web` のimportが重く難しい場合は、無理に作らず、その理由を報告に書く(見た目だけのテストは禁止)。

## 制約(必ず守る)
- 上記2点以外は変更しない。リファクタリング、整形、無関係なファイルの修正は禁止。
- 既存のテストは変更しない(追加のみ)。全体のテスト(`python -m pytest tests -q`)を、修正前後で実行して、実際の合否件数を報告する。
  修正前のベースラインは、現在 414 passed, 1 skipped(新規テスト分を除く既存)であるはず。食い違えば報告する。
- 実サービス(Ollama、外部AI、ポート8099のアプリ)へは接続しない。`.env` や認証情報を読まない・出力しない。
- 変更前に実行計画を示す。実行していないことを「できた」と書かない。
