# 変更記録 2026-09-21(2): `None` の扱いと `FRONT_AI_PROVIDER` の既定値

対象: LOCALSAPORTER
作業ツール: ジェンキンス(実装は Claude)
種別: 既存ファイルの修正(各1行)+ テスト追加
指示書: `docs/改修依頼_2_None処理と既定値.md`
根拠: `docs/analysis/` の3つの調査報告と比較(`comparison-claude-vs-gemini.md`)

## 1. 変更内容

| パス | 変更 |
|---|---|
| `app/triz_common.py` 4行目 | `validate_problem` が、値が `None` のとき文字列 `"None"` ではなく空文字にするよう変更 |
| `app/web.py` 99行目 | `FRONT_AI_PROVIDER` のコード既定を `"chatgpt"` から `"ollama"` に変更 |
| `tests/test_triz_common_none.py`(新規) | 16件 |
| `tests/test_front_ai_provider_default.py`(新規) | 4件(修正前後で挙動が変わるものを含む) |

既存のテストは変更していない。上記以外のファイルも変更していない。

### 修正1: `validate_problem` の `None` 処理
- 変更前: `str(values.get(k,'')).strip()`。値が `None` だと `"None"` という文字列になり、空を許す `physical` にも保存され、LLMプロンプトに入っていた。
- 変更後: 値が `None` のときは空文字。`None` 以外の扱い(文字列化、前後の空白除去、必須項目の空チェック、2500文字上限)は変えていない。
- 影響: 必須項目が `None` のときは、変更前は `"None"`(非空)として通ってしまっていたが、変更後は空として `ValueError` になる。
  意図した是正だが、必須項目に `None` を渡す呼び出し元があれば、以前は通っていたものが弾かれる。

### 修正2: `FRONT_AI_PROVIDER` の既定
- 変更前: 環境変数が未設定のとき `"chatgpt"`。`docker-compose.yml` と `.env.example` の既定は `ollama` で、コードだけが食い違っていた。
  compose を使わない起動や、変数の欠落で、通常チャットが外部AIへ送信され得た。
- 変更後: 既定 `"ollama"`。`ollama` は `PROVIDERS`(claude / chatgpt / gemini / grok / meta)に無く、`front_provider()` は `None` を返し、
  外部AI呼び出しの経路(`call_provider_with_metadata`)に入らない。
- 影響: コード直接起動(compose なし)で、変数を設定していなかった環境は、外部AIではなくローカルモデルで応答する挙動に変わる。
  外部AIをフロントに使いたい場合は、`FRONT_AI_PROVIDER` を明示的に設定すること。
- `"chatgpt"` を既定として前提にしている他の箇所は無い(全走査で確認)。ヒットはすべて、明示指定されたプロバイダIDの分岐。

## 2. 検証結果

- 修正前のベースライン: `python -m pytest tests -q` = 414 passed, 1 skipped。
- 新規テスト(修正1)を、修正前に実行: **9 failed, 7 passed**。修正が無いと落ちることを確認した。修正後は16 passed。
- 修正2: ファイルを変更せず、ソースを `chatgpt` に戻した文字列をメモリ上で実行し、修正前は既定が `chatgpt` になることを確認した。修正後は4 passed。
- 最終: **434 passed, 1 skipped**(414 + 新規20、失敗0)。別途こちらで再実行した結果も一致した。
- 差分: 既存ファイルは各1行のみ。元ファイルの改行(`app/web.py` は全行 CRLF)を保ったまま置換した。

## 3. 作業上の記録

- エージェントは、最初 `app/triz_common.py` と `app/web.py` を全体書き換えで書こうとした。元ファイルの改行が CRLF(一部 LF 混在)で、
  書き換えると改行が変わり不要な差分になるため、書き込みは却下し、バイト単位の置換に切り替えた。
- 一時ファイルの作成(`tmp_inspect.py`)も却下した。以降は `python -c` のみで調査した。
- テストの脆い検査(`PROVIDERS` のキーの完全一致)は、無関係な変更で落ちるため、承認後に削除した。

## 4. 未対応

- 調査報告で指摘された、その他の項目(`app/web.py` の分割、テスト件数の記録の不一致、認証の欠如、イメージタグの固定など)は今回は変更していない。
- `MANIFEST.json`(13件)、`SYSTEM_DESCRIPTION_FOR_AI.md`(50件)、`BUILD_REPORT.md`(403件)のテスト件数の記録は、実測の 434件と食い違ったままである。
