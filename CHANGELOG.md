# 変更履歴

このファイルには利用者に影響する主な変更を記録します。詳細な実装履歴は `docs/CHANGELOG_*.md` を参照してください。

## [Unreleased]

### 2026-10-06

- 未達条件を証拠から診断して残件カードに表示する解決コーディネータを追加。計画矛盾は計画修復へ、実行失敗は回復記録へ引き渡す。
- 外部収集事例の原本本文をサーバー側でSHA-256照合して案件に登録。候補の承認には案件原本の再照合と入力版の指定を要求する。
- 計画修復で未解決指摘を現行の達成条件・工程・成果物契約へ対応付け、業務事実の回答待ち、内容レビュー不可、署名変更を承認不可として扱う。
- RAG適格性の不採用理由とTRIZ候補の順位を記録。候補・隔離試験の成功を業務目標達成とは扱わない。
- 限定自動再開に期限付きclaimと成果物の再照合を追加。既定無効、承認済みローカル文書工程のみという実行範囲を維持する。
- 疑似案件の横断テストを追加。本番の実案件完走、実PDF受入、外部AIレビュー、UIの使いやすさは未検証。

### これまでの未リリース変更

- 目標判定と工程判定の外部実行証拠を整合。フォーム公開とリード取得を分離し、無関係な操作や無効な公開URLによる達成判定を防止。

- 公開用ダッシュボード画像をREADMEへ追加
- システム構成図をREADMEへ追加
- 目標・計画、OCRレビュー、成果物一覧、経験RAG登録の公開用画面を追加
- ReleaseごとのCycloneDX SBOM生成・添付を自動化
- CodeQLによるPython静的解析を追加
- TrivyによるDockerイメージ検査とGitHub SecurityへのSARIF登録を追加
- Google Sites公開確認の接続先固定とリダイレクト再検証によりSSRFを防止
- 公開Web検索のドメイン境界判定と、経験記憶の文末処理を安全化
- 実行コンテナのCritical・High脆弱性を解消し、不要なpip/setuptoolsを削除
- 2026-09-29時点でCodeQL Critical/High 0件、Trivy Critical/High 0件を確認

## [0.1.0-preview] - 2026-09-29

### 追加

- 目標、達成条件、計画、承認、実行、検証、成果物保存の統合フロー
- OCRフォールバック、人間確認、原本整合性検証
- 車両別・月別損益処理
- TRIZによる解決手順生成
- 手順学習と成功事例の経験RAG登録
- 成功事例JSONの確認・登録UI
- GitHub Actions、Dependabot、Issue・PRテンプレート

### 安全性

- OCRの失敗、要確認、未承認、完全性検証失敗を後続処理から遮断
- 経験RAG登録に明示的な人間確認を要求
- 公開ポートを既定で127.0.0.1へ限定

### 既知の制約

- 代表的な実PDFによるOCR受入は継続中
- 実案件データによる車両別月次損益の一連受入は継続中
- ChromaDBの修正版未公開セキュリティアラートを追跡中。ChromaDBサーバーAPIは公開しない

## 2026-09-30

- Added safe completion recovery for generic projects, including version-bound review feedback, generic rebuilding, evidence checks, unified UI action state, provider failure isolation, and read-only recovery previews.
- Prevented legacy review tasks from being reused as execution tasks during recovery.
- Fixed the hardened Docker build order for RAG-enabled builds while keeping runtime package installers removed.
