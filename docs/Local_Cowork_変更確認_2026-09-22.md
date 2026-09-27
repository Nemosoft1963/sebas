# 現状・変更確認 2026-09-22

比較基準: 2026-09-21 18:28保存のdevelopment_resume_20260921/manifest.json。読み取り調査と隔離コピーでの追加テストのみ実施。起動・停止・配備・本番変更は行っていない。

## 確認された変更

既存ソースはapp/triz_common.pyとapp/web.pyの2ファイル変更。意味的な変更は各1行: Noneを空文字として検証する修正、FRONT_AI_PROVIDER未設定時の既定をchatgptからollamaへ変更。明示されたプロバイダ設定は優先される。

追加: app/triz_import.py、scripts/import_triz_problems.py、テスト3ファイル、inputsの74件JSONとメタ、docsの変更記録・改修依頼・外部分析報告。TRIZインポートはハッシュ照合、変換、dry-run、出典URLによる重複回避、競合時再試行を備える。今回外部調査は行っていない。

## 配備と稼働

Local Coworkの4サービスは停止中。webコンテナは2026-09-21 22:47 JSTに作成され、22:48起動、22:53停止した記録がある。イメージIDはsha256:5da40aa7cd33bc06872c27bb4702f8c56c5df4c0bff93672b5d1c17e53570ccdへ更新。
停止コンテナからweb.py、triz_common.py、triz_import.py、vehicle_auto.py、vehicle_workflow.pyを読み出し、現行ソースと全5件ハッシュ一致を確認。

## 本番データの確認

インポート用案件f8daae1bf0204e93acdde0b22be359c6へtriz-problems-export.jsonが登録済み。学習DBに74件のgeneral_inventionsがあり、すべてstatus=defined。候補0、検証ケース0、実験0。
したがって「74課題の取り込み」は完了しているが「74件の検証済み手順を利用できる」状態ではない。車両損益案件へ自動接続されたことも示さない。
CHANGELOGの本番未投入という記載は現在のDBと不一致。実データ確認結果を優先する。

## 検証

隔離した現行ソースで追加3テストファイルを実行: 31 passed、DeprecationWarning 1。新しい変更記録では全体434 passed・1 skippedと報告されているが、本調査では全体再実行はしていない。停止維持のため稼働前提のhealthcheckも実行していない。

## 残る課題

vehicle_auto.py、vehicle_workflow.py、vehicle_service.py、automatic_triz.py、executable_recipes.py、UI JavaScriptは保存時と同じ。したがって再監査で指摘した読取不能金額の0円化、原本合計照合欠落、粗い重複除外、月別配賦の不足、TRIZの採用・業務再実行接続、レシピの実行再利用、UIの進行復旧は未修正。
TRIZのNone修正はvehicle_auto.money(0)の不具合修正とは別。

BUILD_REPORT.mdは従前の403件の記録のまま。新しい変更記録434件との同期が必要。9月21日の再開ZIPには、その後の追加実装とDB更新が含まれないため、今後の再開は現行ソースと本報告を優先し、旧ZIPを一括復元しない。

## 証跡

C:/Users/example/LocalCowork/workspace/projects/status_review_20260922/ に隔離ソース、tests_new.log、配備5ファイル、読み取り用DBコピー、learning_summary.jsonを保存。DBは業務データのため外部送信しない。

次の開発優先順位は、従来の再監査に従い正確性の欠陥修正→UIの停止理由と次操作→計画再検証から実行への接続。今回の追加機能だけで既存の停止問題が解消したとは判断しない。
