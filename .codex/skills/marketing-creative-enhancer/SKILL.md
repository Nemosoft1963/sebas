---
name: marketing-creative-enhancer
description: Improve human-facing Local Supporter campaign assets with a structured design brief, Canva or another design service, quality review, and approval before Google Sites publication. Use for landing pages, sales visuals, diagrams, and social creatives; do not use for private operational documents.
---

# Marketing Creative Enhancer

Turn approved campaign facts into clear, reviewable visual assets without changing the offer or adding unsupported claims.

The production runtime is Local Supporter, not Codex. Use external AI provider APIs for bounded component work and Canva Connect API for template composition. Codex may maintain the implementation but must not be required by the running campaign workflow.

## Workflow

1. Read the campaign title, audience, offer, CTA, publication state, and existing creative state.
2. Generate the creative package through the campaign `creative/package` API. Treat its brief, brand profile, asset manifest, and quality report as the canonical input.
3. Send only intended public copy and brand data to Canva or another selected design service. Exclude credentials, lead records, private project context, and customer data.
4. For multi-provider production, call creative/orchestrate. Local Supporter sends only the public data envelope to explicitly allowed provider APIs, assigns bounded copy, visual-direction, and review roles, and records every result.
5. When direct Canva Connect credentials and an autofill template are configured, Local Supporter fills the template and records its design URL. Otherwise it automatically uses the local responsive composition.
6. For local-only generation, call creative/auto-generate; it creates a responsive local visual, records an automatic review, and returns a preview URL. Treat its score as automated evidence, not human approval.
7. Register any manually produced external design URL, optional public HTTPS image URL, review, and score through creative/review. Preserve the local result if an external service is unavailable.
8. Require a human to inspect the preview and approve it through creative/approve. Scores below 70 require revision.
9. Generate the landing package only after creative approval. Verify desktop and mobile layout, CTA consistency, image loading, contrast, and Google Form navigation.

Read [references/workflow.md](references/workflow.md) when operating the Local Supporter API or choosing a fallback.

## Invariants

- Keep campaign facts unchanged unless the user explicitly edits the campaign.
- Do not invent testimonials, customers, awards, performance numbers, guarantees, prices, or outcomes.
- A Canva design URL is production evidence, not a public image URL. Register an image URL only when it directly serves an approved image over HTTPS.
- External design creation does not authorize Google Sites publication or social posting.
- Preserve the local brief and quality report when an external service fails; retry the service once, then continue with the local provider or request human action.
