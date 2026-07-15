# Local TTS Studio v3.8.5

## Much faster downloads

- **Download Audio is now a single server-side export.** Merging, the Clear
  Speech treatment, and M4A conversion all happen in one pass on the server.
  Previously the full uncompressed WAV was uploaded and re-downloaded up to
  three times, which added minutes of dead time when generating against a
  remote server — that round-tripping is gone. The browser uploads the small
  FLAC segments once and downloads only the finished file.
- Export progress is logged step by step in the Logs panel
  (`Export: merging…`, `Export complete — Ns total`).
