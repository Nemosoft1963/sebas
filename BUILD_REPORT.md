# BUILD REPORT

## 2026-09-29 公開版セキュリティ検査・高重大度対応

- 対象: `Nemosoft1963/sebas` main
- 最終実装コミット: `14693363972445f696c9afa5e8fd53b20e453689`
- ローカル対象テスト: 44 passed, 1 skipped
- GitHub pytest: success (`36522900170`)
- CodeQL: success (`36522900150`)、Critical 0、High 0
- Trivy container: success (`36522900150`)、Critical 0、High 0
- OCR High 3件: 境界・許可名・manifest・SHA-256検証を確認し、根拠付きfalse positiveとして整理
- 未解決: CodeQL Medium 11件、修正版未公開のChromaDB Dependabot 4件
- 本番反映: 未実施
- 詳細: `docs/CHANGELOG_2026-09-29_security_scanning_hardening.md`

## 2026-09-29 全体状況記録

- 本番 `scripts/healthcheck.ps1`: 全項目PASS
- 稼働: web、Open WebUI、Computer、Google Publisher Browser、PaddleOCR document parser、PaddleOCR VLM、Ollama
- 設定モデル: `mistral-small3.2:24b-instruct-2506-q4_K_M`
- 公開版CI: pytest / CodeQL / Trivy success
- 本番ルートはGit管理外のため、公開版とのファイル一致は未証明
- 全体記録: `docs/SEBAS_CURRENT_STATUS_2026-09-29.md`
