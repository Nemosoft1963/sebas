# Retry and recovery policy

| Failure | Automatic action | Stop condition |
|---|---|---|
| Network error, HTTP 429, HTTP 5xx | Exponential backoff | Three attempts |
| OAuth 401/403, invalid refresh token | Mark `reauth_required` | Immediate human action |
| Google Form already recorded | Reuse it | Never create a duplicate |
| Google Sites selector or layout failure | Reload once, reopen draft once | Mark `failed` |
| CAPTCHA, 2FA, recovery prompt | None | Immediate human action |
| Duplicate Google response ID | Count as duplicate | Do not create a lead/action |
| Missing email or consent | Count as rejected | Do not contact |

After recovery, resume from the persisted publication state. A retry must not clear the form ID, public URLs, response cursor, approval timestamp, or lead idempotency keys.
