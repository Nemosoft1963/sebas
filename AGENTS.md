# Repository guidance

Preserve the OCR integrity and approval gates. Do not convert unreadable, unknown, review-required, or unapproved states into zero, success, approval, or OCR-not-required. Revalidate the source SHA-256 and manifest before approval, publication, or experience-RAG registration. Human approval is required before registering experience RAG. OCR is disabled by default through `LOCALSAPORTER_OCR_ENABLED`.

After major changes, run `scripts/healthcheck.ps1` and the related acceptance tests. Record validation results without committing secrets, private data, machine-specific paths, or operational databases.
