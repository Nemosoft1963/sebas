# 2026-10-02 Campaign execution evidence

Campaign workflow requirements now use evidence for each operation: site publication, posting kit generation, copy approval, a published social post and its URL registration, form response synchronization, and evaluation of a consented lead. Drafts and opened composers do not count as published posts; an unsuccessful or stale synchronization does not count as a current response sync. A generic external action cannot impersonate one of these campaign operations.

When a workflow pauses for an external operation, its event records the missing requirement and the next action. Guidance recognizes an already published site and a form connection that needs reauthorization. A local plan-revision proposal now retains valid feedback rows when one model-generated amendment has an invalid target; that row stays unresolved and prevents automatic application.

Validation: 56 focused tests passed locally. The deployed web health check passed. GitHub Actions `test` and `security` passed for code commit `c363480`.

This changelog describes software behavior only. Operational project status and private campaign evidence are stored locally.
