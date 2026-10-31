# Context

## Domain
- A macro is an ordered list of steps recorded in the synthetic CRM workspace.
- A step is one safe input, click, scroll, or wait action with a stable target ID.
- A target is the selected synthetic workspace, never another browser page or desktop app.
- A sensitive input is never stored in a step.
- Replay checks target identity and page focus before every action.

## Data
- Browser local storage is the record of saved macro steps and settings.
- The synthetic CRM state exists only to demonstrate recording and replay.
- No real user, employer, client, or customer records are included.
