# Local Supporter creative workflow

## States

`not_started` → `brief_ready` → `reviewed` → `approved`

`failed` can return to `brief_ready` by regenerating the package. Google Sites package generation requires `approved`.

## API operations

- `POST /api/projects/{project_id}/premarketing/campaigns/{campaign_id}/creative/package`
  - Body: provider (`canva`, `local`, or `other`) and optional brand name, tone, and four six-digit hex colors.
  - Writes `creative_brief.md`, `brand_profile.json`, `asset_manifest.json`, and `quality_report.md` under the campaign workspace.
- `POST /api/projects/{project_id}/premarketing/campaigns/{campaign_id}/creative/review`
  - Body: `design_url`, optional `image_url`, `review_text`, and integer `quality_score` from 0 to 100.
  - Canva requires a `canva.com` HTTPS design URL. Local work may leave the design URL empty.
- `POST /api/projects/{project_id}/premarketing/campaigns/{campaign_id}/creative/approve`
  - Accepts reviewed work scoring at least 70.

- `POST /api/projects/{project_id}/premarketing/campaigns/{campaign_id}/creative/auto-generate`
  - Creates the local responsive hero treatment and automatic quality review from approved public campaign facts.
  - Returns `/premarketing/{token}/creative/preview`; human approval remains mandatory.

## Direct multi-provider production

- `POST /api/projects/{project_id}/premarketing/campaigns/{campaign_id}/creative/orchestrate`
  - Runs in Local Supporter without Codex.
  - Sends only title, audience, offer, CTA, and public brand settings.
  - Uses explicitly allowed and configured AI providers for bounded component roles; each role can fail over once to another provider.
  - Uses Canva Connect Autofill only when `CANVA_ACCESS_TOKEN` and `CANVA_BRAND_TEMPLATE_ID` are configured.
  - Preserves provider errors and falls back to the local responsive design.
  - Never treats AI or Canva completion as human publication approval.

Use Canva templates with text fields named TITLE, AUDIENCE, OFFER, and CTA. External provider output is stored as reviewable component evidence and never executed as code.

## Review dimensions

- Message hierarchy: the audience, problem, offer, and CTA are understood in that order.
- Brand: colors, type, image style, and tone are consistent.
- Conversion: the CTA wording matches the campaign and is visually prominent.
- Accessibility: text remains HTML, color contrast is adequate, and mobile content is not clipped.
- Factual integrity: no claim appears unless present in the approved basic data.

If Canva is unavailable, use the generated local LP visual treatment, review it against the same dimensions, register it with provider `local`, and preserve the unavailable-service reason in the review.
