# 2026-11-12 P1-1 成果物manifest

## 本番反映

- 配布元: `LOCALSAPORTER_deploy_20261112_p11_artifact_manifest.zip`。ZIP SHA-256: `AC85842B807116C9E0071159DCEC958D9CB905C39DB8D164FC3D58FF2C0B1022`。同梱 `hashes.txt` の5件と展開内容を照合。
- 現行P0-4との差分を監査し、Webだけを停止。旧コード3件と `/data` を `backups/20261112_p11_artifact_manifest/` に保全した後、5ファイルを配置してWebを再ビルド・起動。
- 環境フラグ・外部通信設定は変更していない。

## 変更内容

- 実行工程が成果フォルダへ保存したファイルに、本文を含まないSHA-256・出所・計画署名・工程IDのmanifestを記録する。手動操作とアップロードは `manual_preview` とする。
- ワークスペース一覧APIに `origin`、`validation_state`、`draft` を追加。manifest側の保存領域は一覧に表示しない。
- 成果物ZIPに `MANIFEST.json` を追加。ドラフトまたはmanifest無しのファイルがあれば `README_試験用ドラフト.txt` を追加する。
- 自動合格判定への接続はこの配布物に含まれない。実行工程の記録は `unverified` であり、意味検査により `valid` に昇格する経路は未実装。

## 配布物の監査で補った修正

- manifest読取に失敗した一覧が出所情報を欠いたまま返る経路を修正。該当時は `origin=unknown`、`validation_state=unverified`、`draft=true` とする。
- ZIP作成時に現物とmanifestのSHA-256が不一致、または検査不能なファイルは `draft=true` とし、READMEの対象に含める。SHA不一致は `invalid` と記録する。
- 読取失敗とSHA不一致を確認する回帰試験を2件追加。このため、`app/web.py` と `tests/test_p11_artifact_manifest.py` の最終ハッシュは配布元 `hashes.txt` と異なる。

| ファイル | 本番配置SHA-256 |
|---|---|
| `app/artifact_manifest.py` | `5F30D1652888E3BD13599EC011B87494AD101E0E00EA16A495C4616B8F0748A5` |
| `app/project_manager.py` | `093B7BE9B0E1996AFA91C4B7062D3F30D9A75A4840A9AA11E609D16F473D6379` |
| `app/web.py` | `D9C0986047EE02D14DF5C3B6FA91DCE73A417FB08C2ED82B7EFF5FDC29DB40C6` |
| `app/workspace_files.py` | `5C5D403CEE5B9FDF1DB13C93143701E692C418C25ADDA37481786CEF5D6DE639` |
| `tests/test_p11_artifact_manifest.py` | `B9A3B4522AF6DF00F653698324EE05DDC3EEFF034A179CFB5010B682FF1CAF5F` |

## 検証

- 隔離コンテナのP1-1・ワークスペース・fail-open関係試験: **36 passed**。
- 全体試験: **2943 passed / 10 failed / 83 skipped**。失敗10件はP0-4反映後と試験名が完全一致。全体合格ではない。記録: `backups/20261112_p11_artifact_manifest/full_suite_20261112.txt`。
- 本番コンテナ内の4コードファイルのSHA-256一致を確認。`scripts/healthcheck.ps1` 全項目PASS。WebとLANプロキシはhealthy。
- 既存PJの成果物ZIPを読み取りで検証。元の成果物84件がZIPにも84件残り、`MANIFEST.json` に84件の要約と `README_試験用ドラフト.txt` が含まれることを確認した。実ファイル名や内容は出力していない。

## 限界

- 実行工程の主経路以外の修復・再生成・レポート保存にはmanifest更新を接続していない。本文が変わったファイルはZIP検査でSHA不一致を `invalid` と表示する。
- 画面JSは未変更。新しいAPI項目を使う画面表示は未実装。実運用での手動操作・新規実行成果物の受入は未実施。
- OCR実PDF受入、車両損益の実案件受入、業務確認事項36項目も未完了。

## 差し戻し

1. `docker compose stop web` でWebだけ停止し、現行コードを別途保全する。
2. `backups/20261112_p11_artifact_manifest/old/` の旧コード3件を元の位置へ戻し、新規の `app/artifact_manifest.py` と `tests/test_p11_artifact_manifest.py` を除く。
3. `docker compose up -d --build --no-deps web` で起動し、`scripts/healthcheck.ps1` を実行する。運用データを無条件に上書きせず、必要な場合だけ `data_before_p11` から個別に復元する。

