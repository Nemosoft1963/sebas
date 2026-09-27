# 変更履歴 2026-09-28: 実行例アーカイブと成功事例取り込みAPI

## 追加機能

- 実行例を物理削除せず、理由と実行者を監査ログへ残してアーカイブできるAPIを追加した。
- 人が確認済みの成功事例をHTTP経由で経験RAGへ取り込むAPIを追加した。
- 重複する内容はSHA-256で検出し、二重登録しない。
- 経験RAGが `off` のプロジェクトでは取り込みを拒否する。
- `confirm_rag=true` が明示されない限り、成功事例を経験RAGへ登録しない。

## API

- `POST /api/projects/{pid}/agent-examples/{eid}/archive`
- `POST /api/projects/{project_id}/experience/import-success-cases`

## 安全条件

アーカイブは物理削除ではありません。成功事例取り込みには、対象プロジェクトの経験RAG設定、人間による内容確認、実行者、証明、`confirm_rag=true` が必要です。未承認データ、OCR確認待ち、失敗、改ざん検知されたデータを成功事例として登録してはいけません。
