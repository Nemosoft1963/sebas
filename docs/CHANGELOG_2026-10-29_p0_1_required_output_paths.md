# 2026-10-29 P0-1 必須成果物パス 本番反映

## 反映内容
- 提供ZIP `LOCALSAPORTER_deploy_20261029_p0_1_required_output_paths.zip`（SHA-256: `957C09A0686F1FDA9E2B692449C5751FD9A83331D81DD0D2024B292B4CFB7DBC`）と手順書を確認し、ZIP内の2ファイルをハッシュ照合後に反映。
- `app/structured_planning.py` を更新し、達成条件に明示された `result/` 内の `.md` / `.csv` 成果物パスを抽出する。計画の成果物契約と一致しない場合は検証で拒否し、CSVには表契約を要求する。曖昧または危険なパスは確定扱いにしない。
- `tests/test_p0_1_required_output_paths.py` を追加。既存の外部AI送信、OCR、RAG、人間承認フローは変更していない。
- ホスト/コンテナの本体SHA-256: `81F04F691BCB62829CDEDDD4F95DD567A4F768AB05913356DDFA58AC120B268D`。試験ファイルSHA-256: `6B48122F9A06244EF3DAFFFCB07CF2044332F979E4C65F5A239E19C2BB3982B4`。
- Webイメージは `sha256:d2c54318118482c374501e7d883eb0f7116f19164c2672d662c4ec0a689837f6`、コンテナは healthy。

## 検証
- 新規受入試験: **9 passed**。
- 新規試験と周辺3ファイルをプラグイン有効で再実行: **64 passed**。
- 初回の周辺試験は `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` で実行したため非同期試験7件がプラグイン未読込で失敗した。設定を外した再実行では64件全て合格。コードの失敗として数えない。
- `scripts/healthcheck.ps1`: 全項目PASS。Ollama選択モデル `qwen3.5:9b`、Webは healthy。
- 全体試験、実ローカルLLMを使う新規PJの通し実行、既存PJの再生成は今回未実施。提供側の全体結果を本番結果とは扱わない。
- P0-2の原本との対応付けはこのパッケージの対象外。OCR Phase 5、実案件受入、業務確認事項36項目も未完了のまま。

## 保全・復旧
- 旧ソース、停止時の `/data/memory`、展開済み提供ファイルは `backups/20261029_p0_1_required_output_paths_final49/` に保全。旧イメージは `localsaporter-web:before-p0-1-required-output-paths-20261029`（`sha256:0d612c0a21b68b4ea63a6d823017caeae03d3cadff2c4b701ccd7004bd07f8ae`）。
- 復旧が必要ならWebを停止し、退避した `app/structured_planning.py` を戻し、追加試験ファイルを今回の反映対象から外し、旧イメージでWebを再作成する。停止時データは必要性を確認するまで巻き戻さない。
