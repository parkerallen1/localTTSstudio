"""Checks for speech_check.py — pure text, no model needed.

    ./venv/bin/python test_speech_check.py
"""
import sys

import speech_check as sc

PASS, FAIL = [], []


def check(label, got, want):
    (PASS if got == want else FAIL).append(label)
    print(f"[{'ok  ' if got == want else 'FAIL'}] {label}" + ("" if got == want else f"\n        got {got!r}, want {want!r}"))


def bad(text, heard):
    return sc.is_bad(sc.compare(text, heard))


# A leading outline marker, however Whisper hears it (the Blueprint heading
# that failed all three takes on 2026-10-20 was the first one).
for heard in ("of valuing God's love.", "A, valuing God's love.", "valuing God's love.", "uh valuing God's love"):
    check(f"marker heard as {heard!r} passes", bad("A. Valuing God’s love", heard), False)
check("'2)' heard as 'to'", sc.compare("2) In what area of my life do I resist truth", "to, in what area of my life do I resist truth")["error"], 0.0)
check("'C.' heard as 'See'", sc.compare("C. Deep humility is seeing our need", "See, deep humility is seeing our need")["error"], 0.0)
check("'1)' heard as 'one'", sc.compare("1) Be patient and remember", "one, be patient and remember")["error"], 0.0)

# The heading itself still has to be said.
check("heading garbled still fails", bad("A. Valuing God’s love", "of evaluating gods glove"), True)
check("heading missing a word still fails", bad("B. Understanding the love of God", "B. Understanding the glove"), True)
check("a real first word isn't eaten", sc.compare("A. Valuing God’s love", "a really valuing God's love")["extra"] > 0, True)

# No marker: unchanged.
check("no marker, 'A' as a word", sc.compare("A man went out", "A man went out")["error"], 0.0)
check("no marker, mistakes still count", bad("The quick brown fox jumps", "the slow red dog sits"), True)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
