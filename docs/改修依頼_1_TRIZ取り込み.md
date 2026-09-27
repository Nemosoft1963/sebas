# 改修依頼 1: TRIZ収集データの取り込みスクリプト(既存コードは変更しない)

## 背景
`inputs/triz-problems-export.json` に、別システム(TRIZ情報収集エージェント)が集めて人間が承認した設計ネタ74件が入っている。
先に作成済みの調査報告 `docs/analysis/claude-20260921-115043.md` の第9章に、取り込み方針がある。まずこの報告と、
関連コード(`app/triz_common.py`, `app/triz_general.py`, `app/triz_general_api.py`, `app/procedure_learning_api.py`,
`app/agent_examples_api.py`, `tests/test_triz_general.py`)を読み直して、報告の内容が正しいかを自分で確認してから作業すること。
食い違いがあれば、報告ではなくコードを正とし、その点を最終報告に書く。

## 作るもの(新規ファイルのみ)
1. `app/triz_import.py` — 副作用のない純粋関数。
   - `export_item_to_values(item, human_checks=..., max_seconds=300) -> dict`:
     エクスポートの1件を、`POST .../learning/general` の `values` に変換する。
     * `physical` が None / 空 / "False" などの場合は空文字 `""`(既存の `validate_problem` が `str(None)` を "None" にするため)
     * 必須項目が空、または各2500文字超なら `ValueError`(切り詰めない)
     * `domain` が `app/triz_adapters.DOMAINS` に無ければ `ValueError`
     * `human_checks` は、自動生成であることを明記した定型文にする(「人の確認済み」と誤解させない文言)
   - `verify_export_file(export_path, meta_path) -> None`: メタの `sha256` と実ファイルのSHA-256が違えば `ValueError`
2. `scripts/import_triz_problems.py` — 上記を使うCLI(argparse)。
   - 引数: `--base-url`(既定 http://127.0.0.1:8099)、`--project`(必須)、`--file`、`--meta`、`--dry-run`、`--limit`
   - 手順: SHA-256照合 → 実行例(`POST /api/projects/{pid}/agent-examples`)を作成、または既存を再利用 →
     itemごとに `revision` を取り直して `POST .../learning/general` → 409(busy/Conflict)は待って再試行、422は理由を記録して続行
   - 二重投入の防止(冪等): 既に同じ出典URLの課題が登録済みならスキップする。判定方法は、実際のAPIの応答を読んで決め、根拠を書く
   - `--dry-run` は、HTTPリクエストを一切送らず、変換結果の件数とスキップ理由だけを表示する
   - 終了時に 成功/スキップ/失敗 の件数と理由を表示する
3. `tests/test_triz_import.py` — 次を検証する。
   - physical の None / "False" / "" → ""、通常の文字列はそのまま
   - 2500文字超・必須項目欠落・未知のdomainの拒否
   - SHA-256不一致で停止
   - `--dry-run` で通信が発生しない
   - 実データ(`inputs/triz-problems-export.json` の全件)が `export_item_to_values` を通ること
   - `tests/test_triz_general.py` の `api` fixture 形式(FastAPI + TestClient + ダミーLLM)で、数件を実際に投入でき、
     2回目の実行で二重登録されないこと

## 制約(必ず守る)
- 既存ファイル(`app/*.py`、既存のテスト、`docker-compose.yml`、`config/`)は変更しない。追加のみ。
- 実サービス(Ollama、外部AI、実際のポート8099のアプリ)へは接続しない。テストはダミーとTestClientだけで行う。
- `.env` や認証情報を読まない・出力しない。
- 変更前に実行計画(作成ファイルと確認方法)を示す。完了時は、既存テストを含めた実際の合否件数を報告する。
- 実行できていないことを「できた」と書かない。
