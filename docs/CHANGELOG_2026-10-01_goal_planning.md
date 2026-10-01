# Goal-driven planning and local model recovery - 2026-10-01

## Changes

- Preserve all numbered achievement criteria when rebuilding a generic plan.
- Order campaign execution after market, service, commercial-term, sales-asset, and approval preparation.
- Represent campaign execution as publication, posting-kit generation, copy approval, manual posting, URL registration, response synchronization, and lead evaluation.
- Require human semantic confirmation for commercial terms, sales assets, outreach content, external actions, and final verification.
- Limit external-action dependencies to the artifacts and approvals they actually consume.
- Include source-reference counts, completion evidence, failure policy, responsible roles, and observable exit checks in privacy-safe plan-review structures.
- Recover structural plan feedback into a bounded generic-plan rebuild while preserving business-fact blockers.
- Bound local-model warmup and retain the previously working model if a requested switch fails.
- Improve the project UI guidance for planning progress and blocked next actions.

## Validation

- Related regression suite: 66 passed.
- Application health check: PASS.
- No credentials, local data, project source documents, or execution databases are included in this release.
## 2026-10-01 CI completion fixes

- Plan task ordering now preserves declared dependencies while retaining research-task priority.
- Structured JSON requests explicitly disable model thinking to keep schema output stable.
- The runtime image upgrades Debian packages before installing application packages, incorporating current security fixes.
- Validation: 70 focused planning and local-model tests passed; runtime health check passed.

## 2026-10-01 Experience RAG security update

- Replaced ChromaDB with a project-scoped SQLite vector index.
- The reviewed experience store remains authoritative; the vector index is rebuildable and cannot grant approval.
- Removed ChromaDB and LangChain vector dependencies because no patched ChromaDB release was available for the reported critical advisories.
- Local Ollama remains the only embedding endpoint and is restricted to loopback or the Docker host bridge.
- Validation: 33 experience-memory, import UI, and human-review tests passed.
