# セバス 作業記憶・再開位置

更新日: 2026-10-02 JST

## まず読む資料

1. `AGENTS.md` と `docs/CHANGELOG_2026-09-25_vehicle_pnl_and_ocr.md`（安全原則）。
2. `docs/CHANGELOG_2026-10-01_mmi_deployment.md`（直近の改修、検証、復旧）。
3. `docs/MMI再構成案_2026-10-01.md`（画面設計と受入条件）。
4. `BUILD_REPORT.md`（継続的な検証記録）。

## 稼働版と同期

- 公開リポジトリ: `https://github.com/Nemosoft1963/sebas`、本番のMMI実装コミット: `68c0a1122662767d0a0ac875e989cbe1ad9cdb77`。
- 2026-10-02確認時点で `origin/main` は同コミット。本番Webコンテナは `localsaporter-web` のMMI版で healthy。`scripts/healthcheck.ps1` は全項目 PASS。
- GitHub Actionsの同コミットに対する `test` と `security` は両方 success。今回の記録文書のコミットは別途確認する。
- 画面: `http://127.0.0.1:8099`。ポート8099、3000、8000、8010はlocalhost公開のみ。

## 直近で完了した作業

- MMIを5タブ（全体像／目標と計画／実行／成果物／履歴）へ再構成。プロジェクト選択を常設バーへ移動。
- 内部JSONを、計画契約・外部AI指摘・実行結果・改訂履歴のカードと表に翻訳。原データは技術情報へ残す。
- 現行APIの人間確認必須、停止時の処理、外部AIの判定・指摘・修正案を正しく表示。
- 計画検証欄の初期展開、エラー後のボタン回復を復元。
- ZIPの古いバックエンドは採用せず、LLM切替と計画検証の既存修正を保持。
- 新MMI受入31件、関連UI回帰41件が合格。GitHubのtest/securityも合格。ローカル全体テストは検証環境の `openpyxl`/`pypdf` 欠落で合格とは扱わない。

## 案件の現在位置

- 販売プロジェクトID: `2a39815e16e4422581aa29b777a92910`。
- ローカルAPIで確認した状態: 計画Ver.33、20工程、`planning`／`plan_issues_open`。外部AI全体レビューは `not_passed`、必要成功数5、現時点成功数0。完走・計画承認・実行開始は未達。
- 画面が示す次操作: 「指摘から修正案を作成する」(`propose_feedback`)。修正案の内容を確認して反映し、その後に再検証する。外部AI接続障害と計画内容の指摘を区別する。
- 前回記録の未対応論点: OAuth再認証の停止ゲート、既存サイトの証拠確認、見込み客評価前のリード数条件、キャンペーン各操作の承認と証拠。前回レビューでGemini HTTP 503、Meta HTTP 401 invalid_api_key。接続状態は再検証時に再確認する。

## 未完了・注意

- 実ブラウザでの視覚確認は、画面操作ランタイムがWindows sandboxエラーで起動せず未実施。HTTP配信、JS構文、DOM構造テストで確認した範囲と区別する。
- OCRの実PDF受入（Phase 5）、車両別月次損益の実案件一連受入、業務確認事項36項目の残件は完了と記録しない。
- 「読めない／未確認／未承認」を0円・成功・承認済みにしない。OCR失敗や未承認結果を計算・RAGへ渡さない。外部送信やRAG登録の承認ゲートを弱めない。
- 添付ZIPを一括上書きしない。特に `app/web.py`、`app/external_ai.py`、`app/core.py`、`app/structured_planning.py` の古い版で新しい修正を戻さない。

## 次回の作業順

1. `scripts/healthcheck.ps1`、`docker compose ps web`、`origin/main` を確認。
2. ブラウザ操作環境が使える場合、プロジェクト切替、5タブ、全体像、外部AI指摘表、成果物リンクを目視し、JavaScriptエラーを確認。
3. 販売案件の現行API状態を再取得。指摘から修正案を作成し、目標・成果物契約を満たすか確認してから計画へ反映、外部AI再検証へ進む。公開・営業実行は別の人間承認点で停止する。
4. 主要変更後は関連テストと `scripts/healthcheck.ps1` を実行し、`BUILD_REPORT.md` と日付付きCHANGELOGを更新。

## 復旧資材

- MMI適用前のイメージ: `localsaporter-web:before-mmi-20261001`。
- MMI適用前ファイル: `temp/mmi_backup_20261001`。
- 巻き戻しの詳細は `docs/CHANGELOG_2026-10-01_mmi_deployment.md`。Docker volume削除や全体pruneは行わない。
