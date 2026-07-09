# Local TTS Studio v3.7.0

The app is now a real Mac app window — no more Chrome tab.

## New

**Native app window**
- Local TTS Studio now opens in its own window with a dock icon, like any Mac
  app. No browser tab, no menu-bar icon. Closing the window quits the app.
- Exported audio (WAV/M4A) saves through a native save dialog.
- Launching the app while it's already running brings the existing window to
  the front.

**Local / Cloud toggle**
- If you've configured a remote generation server (Settings → Remote), a
  💻 Local / ☁️ Cloud toggle appears in the header. Cloud shows green when
  active so you always know where audio is being generated. Switching to Local
  keeps your saved server settings.

## Fixes

- Updates can no longer leave the UI running stale cached code (the cause of
  buttons doing nothing after updating to v3.6.0 until a hard refresh).

## Install

Download the `.zip` below, unzip, and drag **Local TTS Studio.app** to
Applications. First launch: right-click → Open (the app is unsigned).
