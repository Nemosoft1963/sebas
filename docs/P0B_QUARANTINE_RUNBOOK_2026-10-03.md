# P0-B 隔離スクリプト運用手順(2026-10-03)

対象: 本番の経験DBにある外部(48G収集エージェント)取込由来の verified 75件。
方針: 削除しない。単純な再索引はしない。`needs_review` に隔離し、原本再取得の一致でのみ `candidate` へ進める。

## 前提(事実)

- 既定DB指定: コンテナ内の `DATA_DIR`(既定 `/data`)/`memory`/`conversations.db`。スクリプトは隣接する経験DBを解決する。
- 経験DB: `/data/memory/experience_memory/experience.sqlite3` の `experiences` テーブル。
- 索引: `/data/memory/experience_memory/chroma`(再生成可能なキャッシュ)。
- 設定: `/data/memory/experience_memory.json`。
- スクリプトに復元用の逆操作は無い。復旧手順は索引の再生成のみである。旧75件を無審査で verifiedには戻さない。
- `--apply` で未隔離が0件のときはバックアップも索引再生成もせず、変更0件で終了する。

## 実行手順

1. バックアップ確認: スクリプトは `--apply` 時にDB・索引ディレクトリ・設定を時刻付きで自動複写する。出力のバックアップ先を控える。
2. dry-run: 何も変更せず対象ID・件数・理由(来歴キー)を表示する。
3. `--apply`: 対象を `needs_review` に変更し、移行イベント(いつ・なぜ・旧状態)を追加する。既存レビュー履歴は改変しない。
4. 件数確認: 出力の対象件数・変更件数・スキップ件数と `GET /api/projects/{pid}/experience/needs-review` の件数を突合する。
5. 索引確認: 案件別に索引を再生成した件数を確認する。失敗時は非ゼロ終了し、正本DBは保たれる。

## 本番での実行コマンド例(コンテナ内)

```sh
python scripts/quarantine_external_imports.py --project <project_id>
python scripts/quarantine_external_imports.py --project <project_id> --apply
```

既定パスを使う場合 `--db`/`--index-dir`/`--config` の指定は不要である。
対象を明示する場合 `--db /data/memory/conversations.db --index-dir /data/memory/experience_memory/chroma --config /data/memory/experience_memory.json` を付ける。

## 失敗時の復旧

- バックアップ失敗時: 何も変更せず終了する。出力のエラーを確認する。
- 索引の再生成の失敗時: 正本DBの状態は保たれる。DBをバックアップから戻さず、索引の再生成を再実行する。旧75件を無審査で verifiedには戻さない。


## 現行本番での補足 (2026-10-03)

正本DBと索引はSQLite。索引ファイルは `vector_index.sqlite3`。現行P5のLANプロキシを先に停止し、SQLite backup APIで両DBをバックアップする。ZIP全展開はしない。旧verifiedの隔離後も自動再承認はせず、原本再取得と人間確認を要する。
