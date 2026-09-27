# Restore Procedure

復元は既存データの上書きを伴うため自動化していません。対象バックアップと現在の状態を確認し、現在のvolumeを追加バックアップしてから実施します。

## Workspace

`backups/YYYYMMDD_HHMMSS/workspace.zip` を使用します。既存Workspaceを別名へ退避してから展開します。

## Docker volumes

対象ファイル:

- `front-data.tar.gz`
- `open-webui-data.tar.gz`
- `cptr-data.tar.gz`

対応volume:

- `localsaporter_app_data`
- `localsaporter_open_webui_data`
- `localsaporter_cptr_data`

復元前に `docker compose stop` を実行します。既存volumeへ展開する前に必ず人間が対象を確認します。復元後は `docker compose up -d` と `scripts/healthcheck.ps1` を実行します。

`docker compose down -v` は使用しません。
