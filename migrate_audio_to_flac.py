#!/usr/bin/env python3
"""One-time migration: re-encode stored project audio from WAV to FLAC.

FLAC is lossless (bit-identical audio) and roughly half the size of WAV.
Each WAV is converted, the FLAC is verified to read back with the same number
of samples and sample rate, and only then is the original WAV deleted.

Safe to re-run: already-converted projects have no WAV files left to migrate.
Scans both the dev data dir (./data) and the installed app dir
(~/.qwen_tts_studio).
"""
import os
import sys
import soundfile as sf

CANDIDATE_DATA_DIRS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"),
    os.path.expanduser("~/.qwen_tts_studio"),
]


def convert_one(wav_path):
    """Convert a single WAV to FLAC alongside it. Returns (ok, message)."""
    flac_path = os.path.splitext(wav_path)[0] + ".flac"
    try:
        data, sr = sf.read(wav_path)
    except Exception as e:
        return False, f"read failed: {e}"

    try:
        sf.write(flac_path, data, sr, format="FLAC")
    except Exception as e:
        if os.path.exists(flac_path):
            os.remove(flac_path)
        return False, f"write failed: {e}"

    # Verify the FLAC round-trips before deleting the source WAV.
    try:
        check = sf.SoundFile(flac_path)
        ok = (check.samplerate == sr and check.frames == len(data))
        check.close()
    except Exception as e:
        ok = False
        e_msg = str(e)
    else:
        e_msg = "sample count / rate mismatch"

    if not ok:
        if os.path.exists(flac_path):
            os.remove(flac_path)
        return False, f"verification failed: {e_msg}"

    wav_size = os.path.getsize(wav_path)
    flac_size = os.path.getsize(flac_path)
    os.remove(wav_path)
    return True, f"{wav_size/1024:.0f}KB -> {flac_size/1024:.0f}KB"


def main():
    total_converted = 0
    total_failed = 0
    total_wav_bytes = 0
    total_flac_bytes = 0

    for data_dir in CANDIDATE_DATA_DIRS:
        projects_dir = os.path.join(data_dir, "projects")
        if not os.path.isdir(projects_dir):
            continue
        print(f"\nScanning {projects_dir} ...")
        for root, _dirs, files in os.walk(projects_dir):
            for fname in files:
                if not fname.lower().endswith(".wav"):
                    continue
                wav_path = os.path.join(root, fname)
                wav_bytes = os.path.getsize(wav_path)
                ok, msg = convert_one(wav_path)
                rel = os.path.relpath(wav_path, projects_dir)
                if ok:
                    total_converted += 1
                    total_wav_bytes += wav_bytes
                    flac_path = os.path.splitext(wav_path)[0] + ".flac"
                    total_flac_bytes += os.path.getsize(flac_path)
                    print(f"  ✓ {rel}  ({msg})")
                else:
                    total_failed += 1
                    print(f"  ✗ {rel}  ({msg})")

    print(f"\nDone. Converted {total_converted} file(s), {total_failed} failed.")
    if total_converted:
        saved = total_wav_bytes - total_flac_bytes
        print(
            f"Storage: {total_wav_bytes/1048576:.1f} MB WAV -> "
            f"{total_flac_bytes/1048576:.1f} MB FLAC "
            f"(saved {saved/1048576:.1f} MB, {100*saved/total_wav_bytes:.0f}%)"
        )
    return 1 if total_failed else 0


if __name__ == "__main__":
    sys.exit(main())
