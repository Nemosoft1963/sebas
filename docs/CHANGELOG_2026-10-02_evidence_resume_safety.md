# External evidence and retry safety (2026-10-02)

- Task execution and goal acceptance now read the same validated operation stream. Distinct evidence IDs count toward required operation totals; cancelled actions and reversed approval/execution timestamps do not.
- Campaign progress counts only supported site publication, posted URLs, consented leads, and scored leads after valid sync. A URL or lead row alone does not imply completion.
- Repeating the same social post URL or published site registration preserves its original evidence timestamp. Conflicting URLs require a new review path instead of overwriting provenance.
- Google Form creation uses an atomic database claim. Once Google returns a Form ID it is checkpointed locally. Failed or interrupted work resumes against that same ID, verifies required questions and publishing state, and adds only missing items. An attempt with no returned ID is not blindly reissued.
- Validation: 92 focused tests passed and production healthcheck passed. This does not establish a complete versioned evidence ledger, external reconciliation workflow, or goal completion.
