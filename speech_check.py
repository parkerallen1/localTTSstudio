"""
Speech check — does a take say what its paragraph says?

The runaway check in main.py catches takes that are far too long. This catches
the rest of what goes wrong in generation: garbled or repeated words, skipped
phrases, an HTML entity read aloud. The take is transcribed (Whisper, through
mlx_worker.py) and the transcript is compared word by word with the text.

Pure text functions, no dependencies, so the app (main.py), the worker's venv
and audit_speech.py can all use it. Whisper never matches a reading exactly —
"17" vs "seventeen", "2 Corinthians" vs "Second Corinthians", dropped
punctuation — so both sides are normalized first, and the thresholds were set
against the backfill's own narration (see audit_speech.py).
"""
import difflib
import html
import re
import unicodedata

_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen "
         "fourteen fifteen sixteen seventeen eighteen nineteen").split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
_ORDINAL = {"1": "first", "2": "second", "3": "third"}


def _spell(n):
    """An integer as words, the way it'd be read: 1999 -> nineteen ninety nine."""
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else " " + _ONES[n % 10])
    if n < 1000:
        return _ONES[n // 100] + " hundred" + ("" if n % 100 == 0 else " " + _spell(n % 100))
    if 1100 <= n < 10000 and n % 100 != 0 and not 2000 <= n < 2010:
        return _spell(n // 100) + " " + _spell(n % 100)          # years
    if n < 1_000_000:
        return _spell(n // 1000) + " thousand" + ("" if n % 1000 == 0 else " " + _spell(n % 1000))
    return " ".join(_ONES[int(d)] for d in str(n))


def words(text):
    """Normalized words: lowercase, no punctuation or apostrophes, numbers
    spelled out, a leading book number read as an ordinal ("2 Corinthians")."""
    t = unicodedata.normalize("NFKC", html.unescape(text or "")).lower()
    t = t.replace("’", "'").replace("&", " and ")
    t = re.sub(r"\b([123]) (?=[a-z])", lambda m: _ORDINAL[m.group(1)] + " ", t)
    t = re.sub(r"(\d),(\d{3})", r"\1\2", t)
    t = re.sub(r"\d+", lambda m: " " + _spell(int(m.group())) + " ", t)
    t = t.replace("'", "")
    return re.findall(r"[a-z]+", t)


def compare(expected_text, heard_text):
    """How far the transcript is from the text. `missing` and `extra` are
    unmatched words as a fraction of the text's length; `error` is their sum
    (a word error rate, roughly). `longest_extra` is the longest run of words
    heard that aren't in the text — babble shows up there first."""
    exp, got = words(expected_text), words(heard_text)
    n = max(1, len(exp))
    sm = difflib.SequenceMatcher(None, exp, got, autojunk=False)
    matched = sum(b.size for b in sm.get_matching_blocks())
    longest_extra = max((j2 - j1 for op, _i1, _i2, j1, j2 in sm.get_opcodes()
                         if op in ("insert", "replace")), default=0)
    missing, extra = (len(exp) - matched) / n, (len(got) - matched) / n
    return {"words": len(exp), "heard_words": len(got), "missing": round(missing, 3),
            "extra": round(extra, 3), "error": round(missing + extra, 3),
            "longest_extra": longest_extra}


# Thresholds — calibrated in audit_speech.py against the backfill's narration.
MAX_ERROR = 0.35
MAX_EXTRA_RUN = 8
MIN_WORDS = 4      # shorter than this, Whisper's guesses swamp the signal


def is_bad(result):
    """Whether a compare() result means the take doesn't say its text."""
    if result["words"] < MIN_WORDS:
        return False
    return result["error"] > MAX_ERROR or result["longest_extra"] > MAX_EXTRA_RUN
