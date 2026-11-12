# 2026-11-11 P0-4 計画生成失敗の修正指示化

## 本番反映

- 配布元: `LOCALSAPORTER_deploy_20261111_p04_planning_errors.zip`。ZIP SHA-256: `9095988E51180B40E9A8260D7B3D5177C7D3374E5B34E112ECC3CE29155BD60F`。同梱 `hashes.txt` の3件と展開内容を照合。
- 現行P0-3との差分を監査し、Webのみ停止。旧 `app/project_manager.py` と `/data` を `backups/20261111_p04_planning_errors/` に保全した後、3ファイルを配置してWebを再ビルド・起動。
- 外部AI送信、画面、フラグの設定は変更していない。実PJの計画再生成は実施していない。

## 変更内容

- `app/planning_errors.py` を追加。ローカル計画生成の形式エラーを固定コード・出力番号・項目・期待値・観測値に正規化する。対応外は `unknown` とする。
- 次回のローカルLLM用プロンプトにはコードに対応する修正指示を入れる。初回プロンプトは従来と同じ。候補が無い試行は同一候補判定に使わず、同じ候補・同じエラーが続けば無益な再試行を止める。
- 確定できなかった場合は `planning_blocked` イベントに基準番号、試行ごとのコード・項目・候補ハッシュと切替候補を記録する。モデルの自動切替はしない。失敗は従来どおり例外として扱い、計画成功にしない。

## 最終配置SHA-256

| ファイル | SHA-256 |
|---|---|
| `app/planning_errors.py` | `28CA448648CFD2E2F6669A510801E5FEA49A19A4EDD10DD15010FF361E06BD27` |
| `app/project_manager.py` | `322F78C4A9903D8FEFAFD3FDCEB2F3FC33107A488E6D905D85E3EEEAEE65DBB4` |
| `tests/test_p04_planning_errors.py` | `89E4967F4E627B8BBB7CC9A5684F63B502A0CC391E39D334F2B0904A8D9B70AF` |

本番コンテナ内の2コードファイルも同じハッシュと確認した。

## 検証

- 隔離コンテナのP0-4・構造化計画・ProjectManager・目標レビュー・fail-open関係試験: **111 passed**。
- 全体試験: **2931 passed / 10 failed / 83 skipped**。失敗10件はP0-3反映後と試験名が完全一致。全体合格ではない。記録: `backups/20261111_p04_planning_errors/full_suite_20261111.txt`。
- `scripts/healthcheck.ps1` 全項目PASS。WebとLANプロキシはhealthy。

## 未実施・限界

- 実際のローカルLLMで修正指示が再生成成功率を改善するかは、架空PJでの実走が必要。今回の試験は疑似応答と回帰試験まで。
- `unknown` の場合は短縮した従来エラー文字列をローカルLLMへの次回プロンプトに含む。最終例外にも従来どおり生エラーが残る。イベントの再現データには本文・パス・プロンプトを含めない。
- P1-1成果物manifest、P1-2提供元可用性は未実装。OCR実PDF受入、車両損益の実案件受入、業務確認事項36項目も未完了。

## 差し戻し

1. `docker compose stop web` でWebだけ停止し、現行コードを別途保全する。
2. `backups/20261111_p04_planning_errors/old/app/project_manager.py` を戻し、新規の `app/planning_errors.py` と `tests/test_p04_planning_errors.py` を除く。
3. `docker compose up -d --build --no-deps web` で起動し、`scripts/healthcheck.ps1` を実行する。運用データを無条件に上書きせず、必要な場合だけ `data_before_p04` から個別に復元する。

