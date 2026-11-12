# 2026-10-17 F3 fail-open要確認の本番反映

## 範囲
ユーザー指定の `LOCALSAPORTER_diff_20261017_f3_cumulative.zip`（SHA-256 `6B0125FFD987A9FA7B2AD8D3B0121F01182FA6BBA6AD73B4D62CBBC1D318BA1B`）の31ファイルを確認し、本番Webへ反映。F3a（Pythonの要確認）とF3b（画面JSのエラー表示）の累積版。前回のE1/E2を保持し、添付31ファイルと反映先のSHA-256一致を確認した。

## 変更の意味
取得・読取に失敗した場合に、無言で空・0件・成功に見せていた経路の多くを停止・確認不能・画面の日本語エラー表示へ変更。`scripts/fail_open_lint.py` の検出は反映前96件から11件へ減少。残る11件は添付の `docs/FAIL_OPEN_INVENTORY_2026-10-13.md` に許容理由があるが、検査器が見落とす経路もある。ログを残して継続する経路の呼出側すべてを実案件で検証したわけではない。

## 退避と検証
- 反映前コード・試験・文書: `backups/f3_pre_20261017/source_app_tests_docs.tar.gz` SHA-256 `75B7C6178D317D3059CE254D049A8434237FD1B393A47F7413EC2D2991630279`。
- 反映前Webイメージ: `localsaporter-web:pre-f3-20261017`。Web停止中のデータ: `backups/f3_pre_20261017/data_complete_20261017.tar.gz` SHA-256 `456445DB5C37EA807F1C921779DED0E0E9BA98CC5AEF7ECD31B8D61A08D53AB8`。
- 本番と一致する隔離候補でF3対象試験 `32 passed`。変更した画面JS12ファイルのNode構文検査PASS。fail-open検出11件。
- 本番Web再ビルド・起動後、全体試験 `2185 passed, 2 failed, 4 skipped, 1 warning`（482.56秒）。失敗2件は前回と同じ `test_l2_project_generation.py::test_other_project_untouched` と `test_restore_roundtrip_all_modes`。Windowsのworkspaceファイル数・一覧照合不一致。新たな失敗なし。ログ `temp/f3_full_20261017.log`。
- `scripts/healthcheck.ps1` 全項目PASS。WebとLANプロキシhealthy。変更画面JSの代表4件 HTTP 200。実行中コンテナの `plan_feedback.py` SHA-256が本番ソースと一致。静的検査 `temp/f3_lint_20261017.json`。

## 制限と次の行動
実ブラウザでの目視・失敗注入、実PJでの計画適用・差戻し・事例参照、OCR実PDF受入は未実施。画面JSのHTTP 200・構文検査は操作上の正しさを証明しない。既知の全体試験2失敗、車両別月次損益の実案件、業務確認事項36項目、E3実案件受入を完了扱いにしない。GitHub更新は今回の依頼対象外で未実施。

巻き戻しはWeb停止後、上記コード・データ退避と旧イメージを戻し、Webのみ再起動してhealthcheckと対象試験を実行する。Dockerボリュームは削除しない。
