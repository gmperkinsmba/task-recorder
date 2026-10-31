# Security

Task Recorder records actions in its included demo workspace. Its local server checks Host and Origin, requires a request token, and limits JSON input. State validation restricts action types and counts; writes use owner-only permissions and atomic replacement. A heuristic rejects some GitHub and AWS credential formats, but cannot catch every secret. Do not record credentials or private data. The app has no shell-command execution path. Use made-up actions in reports.

Saved state is plaintext protected by account permissions. Use your Mac’s disk encryption to protect saved files. The native app has an ad-hoc code signature. This lets the installer check whether its files changed. It does not prove who made the app. The build is not notarized.

Report security issues through [GitHub private vulnerability reporting](https://github.com/gmperkinsmba/task-recorder/security/advisories/new).
