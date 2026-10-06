# 変更履歴 2026-10-06: PJライフサイクル L1〜L5

配布元: `LOCALSAPORTER_diff_20261006_lifecycle_L1-L5.zip` (SHA-256: `89448cd343500288fc46b40834e42ed8b93e0408b2faf3c76564dfbdb8fb2e98`)。
差分28ファイルを本番コードへ反映。配布文書は参考として扱い、隔離テストと安全性確認を経て補正した。

## 実装

- L1: PJ関連ストアの登録簿、退避、manifest検証、復元。作業ファイルごとのサイズとSHA-256をmanifestへ記録し、検証時に照合する。
- L2: PJ初期化プレビュー、世代を保持した初期化、世代復元。
- L3: 削除プレビュー、退避を伴う論理削除、復元、完全削除プレビューと実行。旧 `DELETE /api/projects/{project_id}` は409で拒否し、安全な画面手順へ誘導する。
- L4: Gemini草案→ローカルLLM整理→許可AIによる最大2回の計画検証→ローカルLLMでの指摘取り込み。取り込み失敗時はGemini整理へ切替。人の承認前に計画実行・RAG登録しない。
- L5/L5b: 疑似PJとスタブAIを使う横断受入テスト、操作手順。

## 検証

- 隔離コピーの関連9テスト群: 283 passed / 2 failed（旧API拒否に伴う旧期待値）。期待値修正後の2件: 2 passed。合計285件の関連ケースは通過。
- 本番Webイメージの再ビルド: 成功。Web起動後Docker healthはhealthy、`/api/health` はHTTP 200。新APIとJS3点の配信を確認。
- Windows仮想環境の広域テスト: 1280 passed / 19 failed / 3 skipped。主な失敗はWindows仮想環境に`openpyxl`/`pypdf`がないこと。1件はWindowsコードページのsubprocessデコード失敗。全体の失敗0は未確認。
- `scripts/healthcheck.ps1`: WebとOllama等はPASS。全体は11項目FAIL。以前から停止中のOpen WebUI、Computer、Google Publisher、LAN proxyとそのポートに由来する。
- 実Gemini/外部AI通信、実PJの削除・復元、利用者によるUI受入は未実施。

## 状態・復旧

検証後、Webを以前の停止状態へ戻した。その他の停止中サービスも起動していない。
反映前退避: `backups/pre_lifecycle_L1L5_20261006_001/` (元コード、/dataコピー、manifest)。
元Webイメージ: `localsaporter-web:pre-lifecycle-20261006`。
巻戻し時はWeb停止を確認し、退避コードを対応パスへ戻し、退避データは必要な場合のみ個別に復元する。既存データを一括上書きしない。
