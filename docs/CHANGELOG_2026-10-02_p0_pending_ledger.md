# 2026-10-02 P0 pending ledger

Added a shared, read-only summary of current plan issues, unresolved revision blockers, external-action approvals, and result approval. Workflow readiness and next-action now share the same action decision logic. The overview shows the counts and approval state directly.

Unreadable records remain unknown rather than being reported as zero. Expired or stale result approvals are rejected, and unimplemented TRIZ adoption stays blocked. Existing plan-conflict and human-approval gates remain in place.

The focused acceptance suite passed (40 tests). Full local collection was blocked by optional spreadsheet/PDF test dependencies missing from the Windows virtual environment.
