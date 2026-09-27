# Google Sites publication

Google Forms uses the API. The current Google Sites editor is a browser workflow.

1. Open the dedicated publisher browser on local port `8010` through its supported computer-use interface.
2. If Google requests sign-in, two-factor authentication, CAPTCHA, recovery, or additional consent, pause for a human. Do not type or retrieve credentials.
3. Confirm the campaign creative state is `approved`, then open a new Site or the organization-approved template. The dedicated browser extension displays `Local Supporter：下書きを配置` only on Google Sites.
4. Use that button only when exactly one approved, unpublished campaign is returned by the internal draft endpoint. It places the approved headline and an HTML embed containing the audience, offer, CTA, privacy notice, disclaimer, and existing `google_form_url`.
5. If the assistant reports missing selectors, multiple candidates, or an authentication page, stop and preserve the draft. Do not improvise a different campaign, create a second form, or loop clicks.
6. Preview desktop and mobile layouts. Confirm the title, CTA, privacy notice, and form are visible.
7. The assistant must never click Publish. Publish manually only after the campaign has recorded `publication_approved_at` and `site_publication_approved_at` values.
8. Open the public HTTPS URL in a signed-out view. Verify it is publicly reachable and the embedded form accepts responses.
9. Register both the edit URL and public URL through the campaign `google/site` API.

Treat selector changes or missing template elements as `failed`, preserve the form and draft Site, and capture a metadata-only error. Do not loop clicks indefinitely.
