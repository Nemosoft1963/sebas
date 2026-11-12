# NEXTFIX 計画構造修正と架空デモ再実行

## 2026-10-17 NEXTFIX 計画構造修正・架空デモ再実行

- 提供ZIP SHA-256: E588981C74AFF19FBD03FA5935716CAFE757C76F073EF56FFD3DB5DCC56DE79F。6ファイルの差分を確認して本番Webへ反映。重複関数除去、複数成果物スキーマとプロンプト、出力点数の目標照合、意味検査の無効化防止、外部評価へ送る公開構造情報を補正した。原本バイト列の外部送信は行わない。
- 反映前の対象コードは backups/nextfix_20261017_before_apply、2回目の追加修正前コードは backups/nextfix_20261017_round2。Webイメージは localsaporter-web:pre-nextfix-20261017。/dataは C:\Users\kanto\LocalCowork\workspace\temp\nextfix_20261017_data_backup に保全。既存ボリュームの削除・初期化なし。
- 関連試験70 passed。healthcheck全PASS。Webコンテナ running/healthy。本番コンテナ内の変更4ファイルのSHA-256を確認。
- 広範囲試験は2035 passed/27 failed/4 skipped/5 warnings（temp/nextfix_broad_pytest_20261017.log）。27件は主にローカルPythonのopenpyxl不足、WindowsのNode・エンコーディング・workspace環境差、従来のrestore系2件。9試験はopenpyxl未導入で収集できなかった。依存パッケージの追加インストールはしていない。関連試験に失敗はないが、全体試験合格とは記載しない。
- 架空デモ3件を更新後のWeb APIで計画再生成しGemini・Claudeで再評価。イベントは2成果物・両AI conditional、カフェは2成果物・両AI conditional、備品申請は1成果物・Gemini pass/Claude conditional。全体は3件とも not_passed。FAQの自動指摘反映ループは1回評価後、指摘の対応先不明で stopped。実行開始は3件ともゲートで拒否。成果物本文の生成・人間承認・経験RAG登録は未実施。
- API状態は output/note_sebas_demo_20261017/round2_readiness.json、レビュー原文・計画JSONは同フォルダ。実画面3枚を同フォルダと publish に保存。公開用記事も現時点の結果へ更新。
- 残課題: 成果物ごとの個別要件と検査の対応を計画契約へ明示し、公開構造で件数と検査種別の誤解が起きないようにする。外部AI指摘の曖昧な工程対応付けは人間確認を維持する。評価未合格を実行可能に変える緩和はしていない。
