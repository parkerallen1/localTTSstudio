# Local TTS Studio v3.5.2

A cleanup-and-hardening release: a full pass over the app to remove rough edges and add safeguards. No workflow changes — everything works the way it did, just more reliably.

## Fixes & safeguards

**Backend**
- The local server now rejects requests with unexpected Host headers, closing a DNS-rebinding hole where a malicious web page could reach the app.
- Generation requests are validated up front: empty text is rejected before it touches the model, oversized paragraphs (>5000 chars) get a clear error instead of a runaway generation, and out-of-range expressiveness values are clamped.
- In-app updates only accept GitHub release URLs, and the update check/download can no longer hang forever on a bad connection (timeouts added).
- Starting a second model download while one is running is now blocked instead of scrambling the progress banner.
- Temp files are always cleaned up — failed voice-clone uploads, failed audio treatments, and failed format conversions no longer leave stray files behind.

**Frontend**
- Deleting a paragraph now saves the project and removes that paragraph's audio takes from disk (previously they were orphaned).
- Empty paragraphs are skipped during Generate All instead of being sent to the model.
- The activity log is capped at 500 entries so day-long sessions don't slow the page down.

## Install

Download the `.zip` below, unzip, and drag **Local TTS Studio.app** to Applications. First launch: right-click → Open (the app is unsigned).
