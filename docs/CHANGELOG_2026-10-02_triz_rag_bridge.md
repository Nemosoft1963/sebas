# 2026-10-02 TRIZと承認済み経験RAGの接続

## 変更
- 自動TRIZの一般工程で、現在のプロジェクト・入力版に適合する人間検証済み経験をローカルRAGで検索し、発明プロンプトへ参考情報として渡す。未承認、撤回、期限切れ、他案件、旧入力、外部AIの生回答は既存の検索境界で除外する。
- RAGの結果は参照ID・教訓・検索状態としてTRIZの失敗記録へ保存する。候補生成や成果物試験を業務回復、目標達成、RAG登録と見なさない。
- 検索を20秒で打ち切り、失敗時は課題保存と既存の開発要否判定を継続する。車両処理は30秒上限のため同期検索を入れず、既存の承認済み実行レシピ照合を利用する。
- 実行概要にTRIZの状態とRAG参照件数を日本語表示。教訓本文は画面に直接出さない。

## 検証
- `test_triz_rag_bridge.py` と関連テスト: 26 passed、1 skipped。画面構文検査PASS。`scripts/healthcheck.ps1` 全項目PASS。
- Windows仮想環境に `openpyxl` がないため `test_triz_vehicle.py` は収集不可。これは合格とは記録しない。
- 本番の埋め込みモデル `nomic-embed-text:latest` は存在確認済み。実案件での検索品質や業務回復は未検証。

## 有効化と限界
- 経験RAGはプロジェクトごとにopt-in。対象の設定を `experience_memory.json` に書くまでoff。承認済み経験が0件なら検索対象は0件で、TRIZは従来どおり原本から処理する。
- 原本の照合、業務検査、人間確認、限定再実行は従来のゲートを維持する。TRIZの過去事例を自動でverifiedへ昇格しない。

## 巻き戻し
- 対象プロジェクトを `experience_memory.json` でoffに戻す。コードは前のWebイメージへ戻す。経験DB・索引は削除しない。

## 本番反映結果
- Webイメージ localsaporter-web:before-triz-rag-20261002 を退避し、Webサービスだけ再ビルド・反映。再起動直後の一時的な接続失敗後、ヘルスチェック全項目PASS、コンテナhealthy。
- 販売プロジェクトを既存の '/data/memory/experience_memory.json' のenforce対象へ追加。既存の車両設定と外部AI予算は維持。ローカル埋め込み768次元の応答を確認。
- 本番の検証済み経験は0件、検索はno_match。初回TRIZ前の画面/APIはRAG有効・参照0件・業務回復false。実案件のTRIZ発明・限定再実行・最終達成は未検証。


## 追加の起動経路修正
- 販売案件の既存 capability_upgrade モードはoffであり、従来のままでは一般TRIZ候補生成が抑止されていた。通常実行経路や計画状態は変更せず、/data/memory/automatic_triz.json の案件別trueでローカルTRIZ候補生成のみ許可する。既定はfalse。
- 原本不足、既知欠陥、未確認の業務事実は従来どおり発明対象外または追加開発扱い。RAG候補の存在を自動採用や再実行許可には使用しない。

- 最終反映後、販売案件のAPIは 	riz_generation_enabled=true、ag_status=ready、ag_reference_count=0、ecovery_success=false。ヘルスチェック全項目PASS、コンテナhealthy。関連34テストPASS・1 skipped、画面構文PASS。
