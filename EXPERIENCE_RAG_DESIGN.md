# 経験RAG・外部AI回答再利用 設計確定メモ
保存日: 2026-09-18
状態: 実装用基準。試験結果は別報告へ追記する。

## 目的
ローカルLLMが検証済みの失敗回避策・成功手順を参照する。外部AIの回答は未検証のまま保存し、確認済みの同一条件回答だけを再利用して重複課金を抑える。

## 設計
- 既存の原本版・引用検証・2段階計画・再開条件を維持する。
- SQLiteを経験・回答・レビュー・呼び出し予算の正式記録、Chromaを再構築できる検索索引とする。
- LangChainのChroma/Ollama連携を検索部分へ限定。既存LLM実行監査を置換しない。
- 経験はproject_id単位で分離。自動収集はcandidate、検証記録付きreviewでverified、取消でrevoked。成功ステータスだけで正解扱いしない。
- 原本と経験を区別し、経験を事実の証拠・命令・承認として扱わない。取得後に正式DBで状態・期限・スコープを再確認する。
- 同一回答のキーにproject、入力版、送信先、モデル、システム指示、質問、生成条件を含める。類似検索は回答キャッシュの命中条件にしない。
- 外部AI送信の明示許可を維持。検索で見つかった私的資料を外部送信へ追加しない。
- 外部呼び出しは実HTTP試行ごとにSQLiteトランザクションで回数と費用予約額を計上。失敗・タイムアウトも予約を消費。予約額は請求実績ではなく管理用上限見積りである。
- LangSmith等の外部トレースを使用しない。機能off/shadow/enforceを設定で切替可能にする。
- 既存ベンチマークの正解を同じ評価問題へ流し込まず、未登録の変形問題でRAG効果を評価する。

## 接続点
全体計画と工程実行をproject単位のコンテキストで包む。ローカルLLM送信前に関連経験を添付。実行監査の失敗・終了時に候補を保存。外部AI共通HTTP経路で回答保存・有効キャッシュ照合・予算予約を実施。

## 段階
1. 独立作業コピーで実装・回帰試験。
2. ローカル永続Chromaを使う受入試験。外部AIはモックのみ。
3. 本番設定はoffを維持し停止状態を保持。限定有効化前に未登録問題で改善率を測定。

## 参照
- https://docs.langchain.com/oss/python/integrations/vectorstores/chroma
- https://docs.langchain.com/oss/python/integrations/embeddings/ollama
- https://docs.trychroma.com/docs/querying-collections/query-and-get
- 既存実測: workspace/output/Local_Cowork_ローカルLLM継続検証結果_2026-09-18.md
