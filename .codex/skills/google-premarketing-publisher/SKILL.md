---
name: google-premarketing-publisher
description: Publish an approved premarketing campaign through Google Forms and Google Sites, then synchronize consented responses into Local Supporter. Use for Google-hosted lead capture publication or recovery; do not use for unapproved publishing, sign-in automation, or CAPTCHA bypass.
---

# Google Premarketing Publisher

Turn an existing Local Supporter campaign into an externally reachable Google lead funnel and keep its responses synchronized.

## Workflow

1. Read the campaign, landing copy, audience, offer, CTA, current publication state, and Google readiness status.
2. If credentials or the dedicated browser are unavailable, report the exact missing configuration. Never request or expose passwords, cookies, refresh tokens, or verification codes in chat or project files.
3. Register the publication request. Obtain human approval immediately before creating or publishing Google resources.
4. After approval, call the campaign's `publish-form` API. Reuse an existing `google_form_id`; never create a replacement merely because Google Sites automation failed.
5. Before generating human-facing landing assets, apply the `marketing-creative-enhancer` workflow and confirm the creative state is `approved`.
6. Read [references/sites.md](references/sites.md) when a Google Sites page must be created or updated. Use the dedicated Google publisher browser, not the general-purpose browser profile.
7. Register the verified edit and public URLs, then run response synchronization and report imported, duplicate, and rejected counts.
8. Leave qualified-lead contact actions in approval state. Publishing approval does not authorize sending email or contacting a lead.

## Invariants

- Do not automate Google sign-in, two-factor authentication, CAPTCHA, account recovery, or OAuth consent. Pause as `reauth_required` for a human.
- Publish only the approved campaign content. Do not introduce claims, testimonials, prices, companies, or results not present in the approved assets.
- Keep Google response IDs as idempotency keys and never fabricate submissions.
- Stop after three transient API attempts. Authentication and permission failures require human action; they are not transient retries.
- Verify that the public page uses HTTPS, displays the intended title and CTA, opens the campaign's Google Form, and accepts responses before recording the public URL.

Read [references/auth.md](references/auth.md) when configuring or repairing credentials. Read [references/retry-policy.md](references/retry-policy.md) when a publication or synchronization run fails.
