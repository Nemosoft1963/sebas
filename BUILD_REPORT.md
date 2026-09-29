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
