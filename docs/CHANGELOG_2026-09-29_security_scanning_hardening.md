# セキュリティ検査・高重大度対応 変更履歴

- 確定日: 2026-09-29
- 対象: GitHub公開版 `Nemosoft1963/sebas`
- 対象ブランチ: `main`
- 最終コミット: `14693363972445f696c9afa5e8fd53b20e453689`
- 本番反映: 未実施。この記録は公開GitHub版の変更を対象とする。

## 目的

公開版にCodeQLとTrivyを導入し、CriticalおよびHighの検出を解消する。OCRの安全原則、原本整合性検証、人間承認、経験RAG登録条件は変更しない。

## 確定した変更

### 検査基盤

- CodeQLによるPython静的解析を追加した。
- TrivyによるDockerイメージ検査、SARIF登録、修正可能なCritical・Highの失敗ゲートを追加した。
- `pytest`、`CodeQL`、`Container image scan` をmainの必須チェックに設定した。

### アプリケーション

- Google Sites公開確認のHTTP接続先を `sites.google.com` に固定した。
- リダイレクトを自動追従せず、各転送先を再検証して最大4回に制限した。
- DuckDuckGo判定をドメイン境界付きの完全一致・サブドメイン一致へ変更した。
- インポート文末処理の正規表現を線形時間の `rstrip` へ変更した。
- テスト内のURL部分一致を、構造または生成済み値との一致へ変更した。

### コンテナ

- `pip>=26.1.2`、`setuptools>=78.1.1`、`msgpack>=1.2.1` をビルド時に要求した。
- 旧版メタデータを除去し、ビルド中に実バージョンを検証するようにした。
- 全依存とWhisperモデル準備後、実行時に不要な `pip` と `setuptools` を削除した。
- これによりpip同梱の脆弱なvendor版msgpackも実行イメージから除去した。

## OCRパス警告の評価

CodeQLのHigh 3件は誤検知としてGitHub上でdismissした。判断根拠は次のとおり。

- 絶対パス、区切りを含む名前、`..`、多重URLエンコードを拒否する。
- 成果物名を固定許可リストで制限する。
- `resolve()` 後のパスが成果物ルート配下であることを確認する。
- 読み出し前にmanifest登録を要求する。
- 読み出したバイト列のSHA-256をmanifestと照合する。
- traversal、別プロジェクト、manifest未登録、改ざんの拒否テストがある。

安全処理は削除・緩和していない。

## コミット

| コミット | 内容 |
|---|---|
| `a01b3be` | CodeQLとコンテナ検査を追加 |
| `14d93f8` | SSRF、URL境界、ReDoS、初期依存警告を修正 |
| `d4165cb` | msgpackとsetuptoolsを安全版へ更新 |
| `54854e3` | 旧版メタデータ除去とビルド時バージョン検証を追加 |
| `1469336` | 実行イメージからpipとsetuptoolsを削除 |

## 検証結果

| 検証 | 結果 | 証拠 |
|---|---|---|
| 対象ローカルテスト | 44 passed, 1 skipped | URL、公開ページ、Cowork、経験記憶関連 |
| GitHub pytest | 成功 | Actions run `36522900170` |
| CodeQL | 成功 | Actions run `36522900150` |
| Trivy Critical/Highゲート | 成功 | Actions run `36522900150` |
| CodeQL Critical | 0件 | 2026-09-29確認 |
| CodeQL High | 0件 | 2026-09-29確認 |
| Trivy Critical/High | 0件 | 2026-09-29確認 |

ローカルOCR一式テストは検証用venvに `pypdf` がなく収集できなかった。OCRコードは変更せず、GitHub pytestの成功と既存専用テストを根拠にした。

## 継続事項

- CodeQL Medium 11件は未解決。重大度順に内容を確認する。
- DependabotのChromaDB関連4件（Critical 2、High 2）は修正版未公開。標準コンテナは `INSTALL_EXPERIENCE_RAG=0` でChromaDBを導入しない。
- 代表的な実PDFによるOCR受入と、実案件データによる車両別月次損益の一連受入は未完了のまま維持する。
- 本番環境へ反映する場合は、この公開版コミットを起点に別途反映・healthcheck・受入試験を実施する。
