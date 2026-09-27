# Google authentication

Use a dedicated Google Workspace user such as `marketing-automation@company.example` as the owner of the Form and Site.

Required runtime settings:

- `GOOGLE_MARKETING_ACCOUNT`
- `GOOGLE_OAUTH_CLIENT_ID`
- `GOOGLE_OAUTH_CLIENT_SECRET`
- `GOOGLE_OAUTH_REFRESH_TOKEN`

Authorize only the scopes needed to create the campaign's Google Forms and related Drive files. Store real secrets only in the local runtime secret configuration; never place them in the Docker image, repository, campaign assets, event details, screenshots, or skill files.

OAuth consent must open in a normal user-controlled browser. If the refresh token is invalid, revoked, expired, or lacks permission, stop with `reauth_required`. A human signs in, completes two-factor authentication and consent, then updates the runtime secret and restarts the web service.

The Google Sites browser profile is stored in the dedicated `localsaporter_google_publisher_browser_data` Docker volume. Do not copy it into the workspace or share it with the general browser container.
