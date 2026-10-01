# セバス MMI再構成の本番反映（2026-10-01）

## 適用元と方針

- 受領: `LOCALSAPORTER_deploy_20261001_mmi.zip` と `本番反映手順_20261001_MMI再構成.md`。
- ZIPの全ファイル上書きは行わなかった。バックエンドが現在の本番より古く、ローカルLLM切替時の安全なウォームアップ、外部AIレビューの出力長、計画生成の修正を戻すため。
- 画面の静的ファイル9件、新規画面テスト6件、および `app/web.py` の `/static/presenters.js` 配信ルートのみを採用。適用前の静的ファイルと `web.py` は `temp/mmi_backup_20261001` に保存。

## 反映内容

- プロジェクト選択と新規作成を常設のプロジェクトバーへ移動。設定は折り畳みパネルに整理。
- タブを「全体像」「目標と計画」「実行」「成果物」「履歴」に再配置。旧タブキーには互換エイリアスを用意。
- 全体像に現在位置、停止理由、次の操作、担当、達成の証拠を表示。
- 計画契約、外部AIレビュー、実行結果、改訂履歴を読みやすいカード・表で表示し、原データは技術情報から確認可能。
- 現行APIの `human_confirmation_required`、`semantic_review_required`、`failure_policy`、外部AIの `pass/conditional/fail/unverifiable` と `unmet_goal/remedy` に表示を合わせた。
- 計画検証欄の初期展開、失敗後の操作ボタン回復を維持。

## 検証

- 新MMI受入テスト: 31 passed。
- MMIと外部AIレビューUI回帰テスト: 41 passed。
- JavaScript 7ファイルの構文検査および実データ形状を使った presenter 検査: 通過。
- 全体テストの初回収集は `openpyxl` と `pypdf` がローカル `.venv` にないため9ファイルで中断。該当ファイルを除いた実行は 817 passed、2 skipped、19 failed。このうち18件は不足ライブラリ起因、1件は検証欄の初期展開・ボタン回復の回帰で、修正後の関連41件は通過。全体テスト合格とは記録しない。
- `scripts/healthcheck.ps1`: 全項目 PASS。
- 本番 `http://127.0.0.1:8099/api/health`: `ok`、トップページと `/static/presenters.js`: HTTP 200、Webコンテナ healthy。
- ブラウザ視覚検証は、画面操作ランタイムの Windows sandbox 起動エラーで実施できず。DOM構造とHTTP配信、静的テストで確認。

## 復旧

- 旧イメージ: `localsaporter-web:before-mmi-20261001`。
- 旧静的ファイルと `app/web.py`: `temp/mmi_backup_20261001`。
- OCR有効化設定、外部AIへの送信条件、人間確認およびRAG登録のゲートは変更していない。

## GitHub連携と次回再開

- MMIの本番版は `68c0a1122662767d0a0ac875e989cbe1ad9cdb77` として `Nemosoft1963/sebas` の `main` に公開。GitHub Actions の `test` と `security` は success。
- 2026-10-02のヘルスチェックは全項目 PASS。販売案件は計画Ver.33・20工程、`plan_issues_open` で停止中。MMI反映を計画承認・目標達成とは扱わない。
- 最新の作業記憶と次回操作は `docs/SEBAS_WORK_MEMORY.md` に記録。
