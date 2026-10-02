# 2026-10-02 P2 limited safe auto-resume

Adds a read-only local patch preview and an opt-in auto-resume API. Auto-resume is off by default at both the server and project levels. The server operator must set LOCALSAPORTER_AUTO_RESUME_AVAILABLE=1 before enabling a project. No background scheduler was added.

Only an approved, current plan with no unresolved feedback or blockers can run. Production execution is restricted to local Markdown document steps with an enforced upgrade contract. External actions, publication, contact, final verification, and RAG registration remain human-gated. A SQLite claim prevents duplicate starts; the generated file must exist and be nonempty before completion is recorded. Unknown outcomes stop for review rather than being retried automatically.

The supplied snapshot was selectively merged to retain newer approval, OCR, TRIZ/RAG, and workflow fixes. The public checkout passed 1032 tests with one skip and one deprecation warning. The production feature flag remains off; no live project was enabled or resumed.
