# 2026-10-21 P0-B前半 判断待ちへの到達を本番反映

## 目的・変更

曖昧な外部AI指摘でローカル対応付けが失敗したときも、先に全指摘の対応表正本を保存し、候補・理由を伴う判断待ちへ進む。候補ゼロの指摘を任意の工程に割り付けず、評価者への再確認・公開packetの修正・対応不可／不足機能の選択肢を返す。人の確定は版・packet・レビュー指紋に結び付け、計画合格や実行承認とは扱わない。

受領ZIP `LOCALSAPORTER_diff_20261021_p0b_front_half.zip`（SHA-256 `B8D7C4BE95796981258AC36A929C85591D502D1DF9E521586103D345EB5B1241`）の本体3ファイルと新規試験1ファイルを反映。精査で見つけた確定記録の上書き経路を追加補正した。

- `app/plan_review_loop.py`: 初回取込後の重複した正本再保存を除去し、保存済み正本の版・packet・レビュー指紋・指摘ID集合を照合するよう変更。指摘IDの並び順だけが異なる場合は同一集合として扱う。
- 同ファイルの人間確定API: 同じ版の保存済み確定行を継承し、今回確定した指摘IDだけを更新する。古い正本との不一致は保存失敗として停止し、確定済みに見せない。
- `tests/test_p0b_front_half.py`: 疑似外部評価から、存在しない工程番号の指摘が正本に保存されて判断待ちに到達する受入試験を追加。実外部通信なし。

## 退避と配備

- 反映前ソース・テスト・報告書: `backups/p0b_front_half_preapply_20261021/`
- 反映前Webイメージ: `localsaporter-web:pre-p0b-front-half-20261021`（`sha256:28b6a873b921e14f3193f5dbc87f7dcebfec7b48cc07bc61a5af12ab1b654a6c`）
- 反映前データ: `localsaporter_app_data_pre_p0b_20261021`（退避時72.8MB）
- 新Webイメージ: `localsaporter-web:p0b-front-half-20261021`（`sha256:31a5a9d27a78e283f52bfad84caf03ff2a501f3b23961ec9545ad385854b5af8`）。P0-Aイメージを土台に本体3ファイルだけを載せ、Python依存は変更していない。
- Composeは `docker-compose.yml`、`docker-compose.lan-off.yml`、`docker-compose.proxy.yml` を維持し、Webのみ再作成。OCR・Ollama・PJデータは初期化していない。

## 検証

- 分離環境の関連試験70 passed。追加補正後の重点試験51 passed。本番反映直後の重点試験59 passed。
- 本番ルート全体: **2273 passed / 12 failed / 5 skipped**（`output/p0b_front_half_full_20261021.txt`）。P0-A直後の失敗12件とテスト名が完全一致し、新規失敗0件。全体合格ではない。
- 追加した曖昧指摘の疑似実行を含む新規試験: 5 passed。正本保存、`awaiting_decision`、未確定件数、候補ゼロ選択肢を確認。
- 実ブラウザの読み取り専用試験: 1 passed。Web healthy、HTTP 200、コンテナ内3ファイルのSHA-256は反映ソースと一致。`scripts/healthcheck.ps1` PASS。

## 未検証・制限

- 既知の12失敗（目標判定7、PJ生成・バックアップ2、項目検証1、実行漏斗1、統合1）は未修正。
- 候補ゼロの3選択肢を操作する専用画面はこの差分に含まれない。APIには判断待ちの情報を返すが、画面操作の完成は次段階。
- 保存済み実案件・デモ案件の旧停止ランでの限定回復、実外部AIによる評価品質は未検証。外部AIへ新規送信していない。
- P0-C以降はこの反映に含まない。

## 復旧方針

問題時はWebのみ停止し、退避した旧イメージを `localsaporter-web:latest` に戻して同じ3ファイルComposeでWebのみ再作成する。ソースは上記バックアップの対応ファイルから復元する。データ巻き戻しが必要な場合だけ退避ボリュームを照合して個別に復元する。全体pruneや `docker compose down -v` は使わない。