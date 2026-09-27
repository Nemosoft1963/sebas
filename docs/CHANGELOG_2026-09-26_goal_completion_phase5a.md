# Goal Completion Phase 5-A 変更履歴

## 変更範囲

- 現在のコード、Git、API、feature flag、別SQLite、試験結果、文書一覧を読み取り専用で収集する生成器を追加。
- `CURRENT_SYSTEM_STATE.json` と、そのJSONから作る `CURRENT_SYSTEM_STATE.md` を追加。
- 対象ファイルのSHA-256と集約ハッシュを持つ `RELEASE_MANIFEST.json` の生成・読み取り専用検査を追加。

## 安全境界

- flagは既定OFFのまま変更しない。
- pytestを生成器から実行しない。未指定の試験結果は `unknown` とする。
- 本番検証・デプロイは明示引数がない限り `false` とする。
- DBは存在するときだけSQLite URIの `mode=ro` で読む。承認台帳、Gate、factsを変更しない。
- 既存の `MANIFEST.json`、`VERSIONS.md`、`BUILD_REPORT.md`、`AGENTS.md` は変更しない。

## 対象外・未検証

- 指標ダッシュボード、旧completed表示の縮小、UIの5画面化、Git導入は対象外。
- 実ブラウザ、実Ollama、本番案件、本番反映およびflag ONは未検証。
