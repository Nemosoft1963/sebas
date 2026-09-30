# 変更履歴 2026-09-30: 完走不能問題の修正

## 概要

「本システムを販売実行」案件が、外部AIレビューの再注入、評価タスクの増殖、車両専用再構成、汎用案件の一律未達判定、UI状態の不一致によって完走できない問題を修正した。

添付 `LOCALSAPORTER_deploy_20260930_completion_recovery.zip` を隔離検証し、そのまま採用せず本番固有ファイルと組み合わせて全回帰試験を行った。ZIP内の誤ったテスト前提を是正し、本番の読み取り専用プレビューで残存した評価タスク流用も追加修正した。

## 反映内容

- 旧レビューを現行計画の指摘へ自動再注入しない版境界を追加。
- レビュー全文と計画生成入力を分離し、指摘を構造化差分として処理。
- 汎用案件向け `rebuild_generic` と候補検証を追加。
- 汎用達成条件を成果物、最終検証、外部実行証拠、人間最終確認で判定。
- readiness、next-action、UIの手動実行可否と自動実行可否を統一。
- 外部AI接続失敗を計画指摘から分離。
- `GET|POST /api/projects/{project_id}/plan/recovery/preview` を追加。
- `POST /api/projects/{project_id}/plan/recovery/apply` を追加。明示的な承認者名と現行版一致を要求。
- 回復候補で旧「計画草案の評価と改善提案」のタイトル・評価用見出しを再利用せず、達成条件を実行タスクへ変換。

## 添付物から除外・是正した内容

- TRIZ原本は74件であるため、原本を含めずテストだけ145件へ変更した差分を不採用とした。
- private/publicで異なるワークスペース既定パスを個人名へ固定するテストを、マウント範囲の安全条件を検査する形へ変更した。
- `docker-compose.yml` 全体のSHA-1固定値テストを廃止し、OCRサービスが基底composeへ混入せず、OCRが既定有効にならないことを構造検査するよう変更した。
- 添付手順の「全ファイルを含む」は事実と異なり、ZIPには基底composeとTRIZ入力原本が無かった。既存ファイルへの差分適用として扱った。

## 検証結果

- 更新候補単体: 901 passed / 8 failed。8件はZIPに未収録の既存ファイル不足。
- 公開リポジトリ重ね合わせ: 905 passed / 4 failed。private/public固有値と誤ったTRIZ 145件前提を検出。
- 本番固有ファイル重ね合わせ: 906 passed / 3 failed。上記誤前提を確定。
- 是正後: 909 passed。
- 評価タスク流用の追加修正後: 集中試験 54 passed、全体 910 passed。
- `scripts/healthcheck.ps1`: PASS。
- `GET /api/health`: `status=ok`。
- 本番回復プレビュー: 現行Ver.22、候補Ver.23、候補13タスク、coverage PASS、評価タスク名0件、`saved=false`。

## 本番反映

- 本番ソースへ303ファイルを照合反映し、SHA-256不一致0件を確認。
- `localsaporter-web:latest` を再ビルド。
- `web` だけを `docker compose up -d --no-deps web` で再作成。
- Open WebUI、Computer、Google Publisher Browser、OCRコンテナは停止・再作成していない。
- ロールバック用イメージ: `localsaporter-web:before-completion-recovery-20260930`。
- ソース退避: `.codex_update_backups/20260930-103333-completion-recovery/`。
- データ退避: 同一Dockerボリューム内 `/data/update_backups/20260930-completion-recovery/`。

## 現在の案件状態

- 「本システムを販売実行」はVer.22のまま。自動更新していない。
- Ver.23候補のプレビュー生成のみ実施し、保存・計画承認・外部AI送信・実行はしていない。
- Ver.23候補は旧評価タスクを除去しているが、人間が差分と既存成果物の再利用可否を確認してから適用する。
- Google Sites公開、SNS投稿、見込み客連絡などは従来どおり個別承認が必要。

## 維持した安全原則

- OCRの未承認・失敗・改ざん結果を後続計算やRAGへ渡さない。
- `confirm_rag=true` なしで経験RAGへ登録しない。
- 不明、未承認、読取不能を成功や0として扱わない。
- 車両損益Completion Gateを変更しない。
- 外部AI接続失敗を目標達成や計画不備へ変換しない。

## ロールバック

1. `docker tag localsaporter-web:before-completion-recovery-20260930 localsaporter-web:latest`
2. ソースを `.codex_update_backups/20260930-103333-completion-recovery/` から復元。
3. `docker compose up -d --no-build --no-deps web`
4. 必要な場合のみ、同一Dockerボリューム内の更新前DBを復元する。
## 追加検出したセキュリティ回帰と是正

添付ZIPのDockerfileは公開Gitの確定済みセキュリティ強化より古く、pip/setuptools削除と脆弱パッケージ更新を巻き戻す内容だったため不採用とした。公開Gitの強化版を復元したうえで、RAG有効ビルド時に依存導入前にpipを削除して失敗する順序不具合を修正した。

最終本番イメージでは `pip` と `setuptools` は不在、`msgpack 1.2.3` を確認した。強化版でのhealthcheckはPASS。公開用の選択差分では全体 **911 passed**。
