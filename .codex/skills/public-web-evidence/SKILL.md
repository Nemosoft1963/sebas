---
name: public-web-evidence
description: Collect current public web information as auditable evidence for a Local Cowork project. Use for public rules, grant guidelines, laws, schedules, specifications, or other facts that may have changed; do not use to transmit private project sources or perform authenticated actions.
---

# Public Web Evidence

Collect public information so that a local LLM can analyze it without sending project originals to an external model.

## Workflow

1. Identify the claims that require current external evidence. Keep private facts, uploaded files, trial balances, personal data, credentials, internal paths, and extracted source text out of search queries.
2. Search the public web. For rules, grants, laws, deadlines, and application requirements, prefer the responsible authority's primary source. Use secondary sources only to discover or cross-check primary sources.
3. Open the primary pages or PDFs and verify the title, issuing body, applicable edition or call number, publication/update date, scope, deadline, and canonical HTTPS URL. Do not treat a search-result snippet or an AI answer as evidence.
4. Record each usable source with URL, title, publisher, publication/update date when available, retrieval time, and the exact claim it supports. Mark unavailable fields as unknown instead of guessing.
5. Store the public-source capture in the current project's context as a Web source. Keep it separate from uploaded originals and preserve the retrieval metadata and content hash.
6. Let the local LLM synthesize the collected text. Every material number, eligibility rule, subsidy rate, upper limit, deadline, and version claim must cite a captured source or be marked `要確認` / `仮説`.
7. Use external AI only when the user has allowed it and local collection or analysis is insufficient, or when an independent review was requested. Send only the public research question and public-source excerpts; never send private project context. Treat external-AI output as advice, not evidence, until its cited page is fetched and verified.
8. Report missing official sources, conflicts between editions, retrieval failures, and any decision requiring human confirmation. Never submit an application, sign in, accept terms, or publish content under this skill.

## Grant-guideline checks

For a grant call such as a Japanese subsidy program, do not declare the work current or application-ready until the official call guideline and its edition are captured. Separate program overview from the currently open call, and verify eligibility, eligible costs, subsidy rate and limits, schedule, required attachments, application channel, and amendment notices individually.

Stop if only summaries, cached copies, or external-AI assertions are available. Produce a missing-evidence report instead of filling gaps with plausible values.
