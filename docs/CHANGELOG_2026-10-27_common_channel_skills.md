# 2026-10-27 本番反映: 共通外部チャネルスキル C0〜C5

## 適用と保全

受領した `LOCALSAPORTER_deploy_20261027_common_channel_skills.zip`（SHA-256 `f55fa20148b0e3f93ea59d094261523f068e911e6bb06ee174986b5894886ddf`）を審査して本番に反映した。ZIP内19ファイルは添付の `hashes.txt` と全件一致し、パス逸脱や余分なエントリはなかった。前回反映版との主要変更差分を確認した。環境、既存PJ、Ollamaモデルを初期化していない。

- 反映前の上書き対象8ファイルと `app/project_lifecycle_backup.py`、Webデータ領域（656ファイル）を `backups/20261027_common_channel_skills_final47/` に退避。主要DB 3件の `PRAGMA integrity_check` は `ok`。
- 旧Webイメージ: `localsaporter-web:before-common-channel-skills-20261027` = `sha256:91813c5d2008caeda959613f067c44b4bce555ac0406bdcd75b2857c448d9b5c`。
- Webだけを停止してソースを反映。3ファイルCompose（`docker-compose.yml`、`docker-compose.lan-off.yml`、`docker-compose.proxy.yml`）で再ビルド・再起動。`--remove-orphans` やボリューム削除はしていない。
- 新Webイメージ: `sha256:208e695cb53664d8a9a9ccc0502661111405cb59003eeaabb347bd12d64719e0`。Webは `running/healthy`。主要5ファイルのホストとコンテナ内のSHA-256は一致。

## 提供版に加えた安全補正

1. `app/channel_skill_mail.py`: IMAP UID SEARCHの引数から重複した `UID SEARCH` を除去し、`SEARCH, None, SINCE ...` に訂正。UID差分条件は `UID, n:*` として渡す。偽IMAPの受入試験も実プロトコルに合わせた。
2. 同ファイル: `completed_ok` の型を真偽値に限定。取得未完了は、0件または一部メッセージがあっても `unknown` とし、受信データもカーソルも確定保存しない。文字列 `"false"` を成功扱いしない回帰試験を追加。
3. 同ファイル: 返信候補照合のC5 Message-IDを、`action_id` 確定後に送信アクションごと取得するよう修正。未設定の `action_id` 参照を除去。
4. `app/project_lifecycle_backup.py`: Windowsで長い新テーブル名を退避した後、長い `backup_id` へのリネームでファイルが見えなくなる問題を補正。新しいバックアップIDを、PJハッシュ・時刻・ランダム値による35文字の形式に短縮。既存バックアップの形式・復元読み取りは変えない。APIの作成→一覧→検証テストで確認した。`tests/test_l1_lifecycle_registry.py` に長さの回帰検査を追加。

## 実装された能力と境界

- C0: 計画項目IDの重複を一意化し、保存前に検出する。
- C1: `web.search_public`、`mail.receive`、`social.observe`、`mail.send_approved` の共通定義、状態、成熟度、追記型の実行・証拠台帳と読取API。
- C2: 承認済み計画で公開調査を要求した項目だけ、検索語を安全検査して公開Web検索へ接続する。検索結果はURL・取得時刻・本文ハッシュを記録する。
- C3: 専用IMAP箱の設定、読み取り専用の差分取得、返信候補の紐付け。設定・資格情報・人の実行呼び出しが揃うまで実接続しない。
- C4: 登録済みSNS IDの観測枠と手動証拠登録。実SNSアダプタは未実装。
- C5: 承認版に束縛したSMTP送信。受理・未送信・結果不明を区別し、結果不明時は自動再送しない。

反映前に承認されたメールアクションは束縛がないため再承認が必要。SMTP受理は配達・既読・返信の証明ではない。画面追加はないため、新状態と履歴はAPIで確認する。

## 検証

- 関連受入試験: **78 passed**。ログ: `output/common_channel_skills_20261027_related_pytest.txt`。
- 全体試験（`PYTHONUTF8=1`、`PYTHONHASHSEED=3`）: **2533 passed / 10 failed / 5 skipped**。失敗10件は提供手順に記された既知の失敗名と一致（目標判定7、項目検証1、実行漏斗1、統合1）。ログ: `output/common_channel_skills_20261027_full_pytest_utf8.txt`。
- 初回の全体試験はWindows文字コード設定で画面/サブプロセス系9件が追加失敗し、合計19失敗。該当9件は `PYTHONUTF8=1` の個別再試験ですべて通過し、正しい設定での全体再試験でも通過した。初回ログ: `output/common_channel_skills_20261027_full_pytest.txt`。
- `scripts/healthcheck.ps1`: 全項目PASS。Webとプロキシはhealthy、選択モデルは `qwen3.5:9b`。
- 本番APIの読み取り確認: 登録済みPJで4スキルがすべて `needs_configuration`、`defined=true`、`connected=false`。未接続を利用可能扱いにしていない。
- 実メール送受信、実SNS取得、実公開Web検索・外部AI送信は行っていない。各実接続の性能・権限は未検証。資格情報は設定・閲覧していない。

## 復旧

問題時はWebを停止し、`backups/20261027_common_channel_skills_final47/` の上書き対象8ファイルと `app/project_lifecycle_backup.py` を戻す。今回新設した本体5ファイル・試験6ファイルは旧版が参照しないため、必要なら退避してから除く。旧イメージ `localsaporter-web:before-common-channel-skills-20261027` でWebを再起動する。データ復旧が必要な場合のみ、退避済み `data/` を停止状態のWebデータ領域へ戻す。ボリューム削除は行わない。

## 残課題

実SNSアダプタ、共通スキルの画面、実接続での受入、既知の全体試験10失敗は未完了。計画項目ID一意化の実Qwen架空PJでの再実走は未実施。旧承認のメールを自動送信しない。
