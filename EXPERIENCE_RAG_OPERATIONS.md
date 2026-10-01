# 経験RAG・外部AI回答キャッシュ 運用手順

## 状態
コード実装済み。初期状態はoff。既存コンテナの起動・再作成は行わない。
設計基準はDESIGN.md、実機記録はsmoke_result.json、全回帰結果はregression.logを参照。

## データとモード
アプリのmemory DBと同じディレクトリにexperience_memory.jsonを置く。
DBと索引はその隣のexperience_memory/に保存される。アプリ既存のDBパスを変更しない。
設定ファイルがない、またはprojectsに対象IDがなければoff。
- off: 既存動作。新しい検索・蓄積・予算制御は動作しない。
- shadow: 経験検索を監査記録するがプロンプトへ挿入しない。回答キャッシュは使わない。外部AIの保存・予算制御・送信許可の確認は有効。
- enforce: 検証済み経験を参照し、有効な完全一致回答を再利用する。

experience_memory.example.jsonを基に対象project IDだけをshadow/enforceに指定する。
埋め込みは既存nomic-embed-text:latestで実機確認したが、日本語検索の十分な評価は未実施。
embedding_revisionは実際のモデルIDと一致する値を記録する。変更時は別索引になるので再索引する。
Windowsのembedding_urlはhttp://127.0.0.1:11434、Dockerはhttp://host.docker.internal:11434。
max_cosine_distanceの既定は0.35。実機1問は0.5で確認した。業務評価で調整する。
埋め込みはkeep_alive=0、HTTPタイムアウト60秒。検索失敗時は記録を残して既存ローカル処理へ戻る。
LangSmithトレースは禁止し、ChromaテレメトリとOTELエクスポートは無効。

## インストールとビルド
作業コピーの専用.venvにインストール・検証済み。既存環境のPythonは変更していない。
Python版はpip install -e ".[dev,rag]"。本番Dockerはdocker/Dockerfileのビルド引数INSTALL_EXPERIENCE_RAG=1で必要パッケージを追加できる。
既定ビルド引数は0。DockerのRAG入りイメージのビルド・本番起動は今回未実施。
ベクトル索引は組み込みSQLiteを使用する。埋め込み生成はローカルOllamaだけを使用し、外部のベクトルDB依存はない。
requirements-rag-tested.txtは専用Windows環境の記録で、Linux用の完全なlockfileではない。

## 経験の整理
成功・失敗はまずcandidateとして保存。ステータスcompletedだけではverifiedにしない。
自動収集の対象はupgrade実行経路の終了・失敗と、機能対象スコープ内の外部HTTP回答。
旧経路のすべての会話や過去ログの一括取り込み・教訓の自動要約は今回実装していない。
短く整理した経験は以下のJSONからaddで登録する。

```json
{"kind":"failure","content":"入力資料が変わったら過去成果物を未検証で再利用しない。入力ハッシュと成果物ハッシュを照合する。","applicability":{},"evidence":{"source":"検証記録のIDまたはパス"}}
```

applicability={}は当該project内で一般化した教訓。原本依存ならinput_versionを指定する。
自動収集した経験はinput_versionに限定される。一般化する場合は元の証拠を参照した別経験を追加する。
外部AIのraw回答は類似検索から除外する。検証した教訓を別のsuccess/failureレコードとして登録し、evidenceに外部回答IDを記載する。
原本・引用・計算・再開検査を経験で代用しない。

## ローカルCLI
作業コピーのルートで専用Pythonを使う。ROOTには上記experience_memoryディレクトリ、PROJECTには正確なIDを指定。

```powershell
.\.venv\Scripts\python.exe -m app.experience_cli --root ROOT --project PROJECT list
.\.venv\Scripts\python.exe -m app.experience_cli --root ROOT --project PROJECT add lesson.json
.\.venv\Scripts\python.exe -m app.experience_cli --root ROOT --project PROJECT review RECORD_ID --status verified --reviewer operator --proof "根拠を確認した検証記録" --days 30
.\.venv\Scripts\python.exe -m app.experience_cli --root ROOT --project PROJECT index --config experience_memory.json
.\.venv\Scripts\python.exe -m app.experience_cli --root ROOT --project PROJECT stats
.\.venv\Scripts\python.exe -m app.experience_cli --root ROOT --project PROJECT review RECORD_ID --status revoked --reviewer operator --proof "根拠の失効"
```

review後にindexを実行する。取消・期限切れは索引に残っていても参照直前のSQLite照合で除外し、再索引時に削除する。
CLIはローカルの信頼された操作者用。レビューのproofは入力必須だが、その内容自体を自動で真偽判定するものではない。

## 外部AI費用管理
対象: project計画・工程・復旧のスコープ、手動外部AI調査、通常会話の機能有効project。
通常会話ではローカルキャッシュのみ許可し、未命中なら既存ローカルフォールバックへ進む。新規外部送信は明示的な外部AI調査またはmissionの許可が必要。
外部へ経験検索内容を追加送信しない。機能offの経路は既存動作を維持する。
手動外部AI調査は既存共有コンテキストを送る仕様を維持するため、秘密・個人情報を含む案件での使用は不可。自動匿名化や秘密検出を保証する機能は今回含まない。

予約単位はmicro USD（1000000で1 USD）。daily_callsはUTC日単位の実HTTP試行回数。
daily_micro_usdは予約額の合計上限、per_call_micro_usdは管理者が設定する1試行あたりの保守的な予約額。
実際の請求額を保証する上限ではない。モデル料金に合わせた予約額設定とプロバイダ側の予算設定が必要。
失敗・取消・タイムアウトも返金扱いにしない。usageはプロバイダの返却値を保存する。
max_output_tokens（既定4096）とmax_request_chars（既定64000）を超える要求は送信前に拒否する。
同一要求がpendingなら別プロセスでも送信を拒否。プロセス異常終了で残ったpendingは自動失効させず、再送を避ける。手動復旧は遠隔実行結果を確認した上で行う。
保存回答はcandidate。検証・期限設定後だけ完全一致キャッシュに使える。類似質問は一致とみなさない。
外部送信の許可がなくても、有効なローカルキャッシュは参照できる。

## 限界と次の検証
費用の実削減率・日本語検索精度・本番同時実行負荷・Linux Dockerでの新依存動作は未測定。
1問の実機成功を一般的な品質向上と解釈しない。
同一モデル・未登録問題でRAGなし/ありを比較してから対象を拡大する。
機能停止は対象projectをoffにする。保存記録を削除する必要はない。
