# 2026-10-22 P0-A(4) 指摘分類の本番反映

## 内容

- 受領ZIP `LOCALSAPORTER_diff_20261022_p0a4_issue_classification.zip`（SHA-256 `1DED353C4A669181BB142E0B9FC13983835C8EB67327E3616F057FEA8C21D925`）の本体2・試験1ファイルを確認して反映。
- 指摘を `plan_defect`、`packet_defect`、`missing_business_fact`、`missing_capability`、`reviewer_uncertain`、`unclassified` に規則で分類し、提案として対応表・正本スナップショット・修正案に保存。指摘本文や人が確定した対応先は変更しない。
- 分類値に応じて公開packet修正、事実確認、開発指示、評価者への再確認・人の確認へ振り分ける。これらを計画本文の修正として自動適用しない。
- 受領版では一般カテゴリ (`artifact` など) が保存済み分類を隠す経路、機能不足を計画修正のまま残す経路、ローカルLLMが返した分類で正本を上書きする経路があったため補正。重複した分類補完ブロックも整理し、分類ルートの判定失敗を黙って通さないようにした。
- 追加受入テストで、一般カテゴリと保存済み分類の共存、ローカルLLMからの偽分類、5種類の安全な振り分けを検証。

## 保全と巻き戻し

- 反映前コード・試験・報告: `backups/p0a4_issue_classification_preapply_20261022/`
- 反映前Webイメージ: `localsaporter-web:pre-p0a4-issue-classification-20261022`
- 反映前データボリューム: `localsaporter_app_data_pre_p0a4_20261022`（72.8 MB）
- 巻き戻しは旧コードと旧Webイメージを戻してWebを再作成する。データボリュームの復元は反映後更新を消すため差分確認を要する。

## 検証

- 隔離コピーの関連試験41件成功。本番の関連試験41件成功。
- 本番のヘルスチェック全PASS、Web healthy、コンテナ内外の本体2ファイルSHA-256一致。
- 全体試験: **2291 passed / 12 failed / 5 skipped**（565.47秒）。失敗12件の名前は判断待ち画面反映直後と完全一致し、新規失敗0。ログ: `output/p0a4_issue_classification_full_20261022.txt`。

## 限界

- 分類は語の一致による提案であり、誤分類はありうる。実外部AIの指摘での当たり外れは未検証。
- 分類を画面へ表示する変更は含まない。評価者への再確認、公開packet修正、Jenkinsへの送信は自動実行しない。
- OCR実PDF受入、実案件受入、業務確認事項36項目は未完了のまま。