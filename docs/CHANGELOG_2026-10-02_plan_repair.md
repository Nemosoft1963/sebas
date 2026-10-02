# Plan review repair and readiness (2026-10-02)

- Separate unresolved plan conflicts, missing business facts, and actual development blockers in workflow readiness. Plan approval now rejects unresolved structural feedback.
- Add a local edit and validation path for a saved plan feedback draft. Each edited issue retains its ID, is checked against the current goal contract and plan signature, and does not consume another local model generation attempt.
- Limit generic rebuild changes to their selected current criteria. Bind new proposals to the current goal contract hash and revalidate before applying.
- Fail closed on approval checks when the goal completion hook is enabled. Render reviewer supplied stop reasons as text.
- Validation: focused readiness, feedback, UI, and approval tests passed; production healthcheck passed after Web startup. No project plan, external review, business result, or final goal acceptance is implied by this code change.
