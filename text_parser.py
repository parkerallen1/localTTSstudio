"""
Server-side text parsing — Python port of the frontend parsing pipeline.

static/script.js turns pasted Markdown into paragraph cards in the browser
(strip Markdown markers, drop QQT metadata lines, detect H2 chapter headings,
clean text for TTS, merge short lines). This module is the same pipeline in
Python so the backend can ingest raw text directly — used by
POST /api/projects/import (which the Google Docs watcher calls).

Keep the two implementations in sync: if you change the parsing rules here or
in script.js, mirror the change in the other file.
"""
import regex  # third-party "regex" module — needed for \p{Extended_Pictographic}

# H2 headings whose text matches one of these are kept as paragraph breaks
# but are NOT marked as chapters (boilerplate section headers).
CHAPTER_EXCLUDE = {
    'settle in',
    'thought starter',
    'reflection questions',
    'humor break',
    'bring the inspiration with you',
}


def heading_key(text: str) -> str:
    """Normalize a heading to a comparison key: drop emojis/markdown/punctuation,
    collapse whitespace, lowercase. e.g. "**🧘 Settle in**" -> "settle in"."""
    text = regex.sub(r'\p{Extended_Pictographic}', '', text)
    text = regex.sub(r'[\uFE0F\u200D\u20E3]', '', text)
    text = regex.sub(r'[^\p{L}\p{N}\s]', '', text)
    text = regex.sub(r'\s+', ' ', text)
    return text.strip().lower()


def strip_markdown(line: str) -> str:
    """Strip Markdown markers from a single line so the TTS model reads the
    words, not the syntax. Heading detection (## ) happens before this."""
    # Unescape backslash-escaped punctuation (e.g. "Jerusalem\!" -> "Jerusalem!")
    line = regex.sub(r'\\([\\!.,*_~`>#()\[\]-])', r'\1', line)
    # Heading markers (#, ##, ### ...) and blockquote markers (>).
    line = regex.sub(r'^#{1,6}(\s+|$)', '', line)
    line = regex.sub(r'^>\s?', '', line)
    # List bullets (-, *, +) and ordered-list markers (1. ) at the start
    line = regex.sub(r'^[-*+]\s+', '', line)
    line = regex.sub(r'^\d+\.\s+', '', line)
    # Links: [text](url) -> text
    line = regex.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', line)
    # Bold / italic emphasis
    line = regex.sub(r'\*\*([^*]+)\*\*', r'\1', line)
    line = regex.sub(r'__([^_]+)__', r'\1', line)
    line = regex.sub(r'\*([^*]+)\*', r'\1', line)
    line = regex.sub(r'_([^_]+)_', r'\1', line)
    # Any stray leftover emphasis markers
    line = line.replace('**', '')
    return line.strip()


def clean_text_general(text: str) -> str:
    """General cleanup applied to all text."""
    # strip emojis and pictographs — they confuse the TTS model
    text = regex.sub(r'\p{Extended_Pictographic}', '', text)
    # strip leftover emoji modifiers: variation selector, ZWJ, keycap, skin tones
    text = regex.sub(r'[\uFE0F\u200D\u20E3]|\p{Emoji_Modifier}', '', text)
    text = regex.sub(r'[ \t]+', ' ', text)
    # Ending every line with a period
    text = regex.sub(r'(^[^\n.]+)(?=$|\n)', r'\1.', text, flags=regex.MULTILINE)
    # remove any lines with just a period and whitespace
    text = regex.sub(r'^\.\s*$', '', text, flags=regex.MULTILINE)
    # removes blank lines and whitespace
    text = regex.sub(r'\n+', '\n', text).strip()
    # sometimes brackets mess up tts
    text = regex.sub(r'[\[\]]', ',', text)
    return text


# Numbered Bible books: "1 Corinthians" -> "First Corinthians", etc.
_BIBLE_BOOKS = [
    ('1', 'First', ['Corinthians', 'Chronicles', 'Kings', 'Samuel',
                    'Thessalonians', 'Timothy', 'Peter', 'John']),
    ('2', 'Second', ['Corinthians', 'Chronicles', 'Kings', 'Samuel',
                     'Thessalonians', 'Timothy', 'Peter', 'John']),
    ('3', 'Third', ['John']),
]

# Bible translation abbreviations -> spoken names. Order matters where one
# abbreviation is a prefix of another (AMPC before AMP, NABRE before NAB).
_BIBLE_TRANSLATIONS = [
    ('AMPC', 'Amplified Bible Classic.'),
    ('AMP', 'Amplified Bible.'),
    ('ASV', 'American Standard Version.'),
    ('CEB', 'Common English Bible.'),
    ('CEV', 'Contemporary English Version.'),
    ('CSB', 'Christian Standard Bible.'),
    ('ESV', 'English Standard Version.'),
    ('GNT', 'Good News Translation.'),
    ('HCSB', 'Holman Christian Standard Bible.'),
    ('KJV', 'King James Version.'),
    ('TLB', 'The Living Bible.'),
    ('MSG', 'The Message.'),
    ('NABRE', 'New American Bible Revised Edition.'),
    ('NAB', 'New American Bible.'),
    ('NASB', 'New American Standard Bible.'),
    ('NCV', 'New Century Version.'),
    ('NIRV', "New International Reader's Version."),
    ('NIV', 'New International Version.'),
    ('NJB', 'New Jerusalem Bible.'),
    ('NKJV', 'New King James Version.'),
    ('NLT', 'New Living Translation.'),
    ('NRSV', 'New Revised Standard Version.'),
    ('RSV', 'Revised Standard Version.'),
    ('TPT', 'The Passion Translation.'),
    ('WEB', 'World English Bible.'),
    ('YLT', "Young's Literal Translation."),
    ('ERV', 'Easy to Read Version.'),
    ('NIrV', "New International Reader's Version."),
]


def clean_text_bible(text: str) -> str:
    """Bible-specific transforms — only applied when bible mode is on."""
    for num, word, books in _BIBLE_BOOKS:
        for book in books:
            text = regex.sub(rf'\b{num} ({book})\b', rf'{word} \1', text)
    for abbr, spoken in _BIBLE_TRANSLATIONS:
        text = regex.sub(rf'\b{abbr}\b', spoken, text)
    # Verses and ranges formatting
    text = regex.sub(r'(\d+):(\d+)', r'\1. verse \2,', text)
    text = regex.sub(r'[,.]-(\d+)', r' through \1.', text)
    text = regex.sub(r'\[(\d+)\]', '', text)
    # Replace colons not part of time notation
    text = regex.sub(r'(?<!\d):(?!\d)', ', ', text)
    return text


def combine_short_paragraphs(items, min_len=225, max_len=325):
    """Merge short consecutive paragraphs. Operates on {text, chapter, heading}
    dicts. Any heading (H2) always starts a fresh paragraph; following body
    text may still merge into it, and the resulting paragraph keeps the
    heading's chapter flag."""
    result = []
    buffer = None
    for item in items:
        if buffer is None:
            buffer = dict(item)
            continue
        if item['heading']:
            result.append(buffer)
            buffer = dict(item)
            continue
        combined = buffer['text'] + ' ' + item['text']
        if len(buffer['text']) < min_len and len(combined) <= max_len:
            buffer['text'] = combined  # buffer's chapter flag preserved
        else:
            result.append(buffer)
            buffer = dict(item)
    if buffer is not None:
        result.append(buffer)
    return result


def parse_paragraphs(raw_text: str, bible_mode: bool = False):
    """Turn raw Markdown text into TTS-ready paragraphs.

    Returns a list of {"text": str, "chapter": bool} dicts — the same output
    the frontend's Parse button produces from the same input.
    """
    items = []
    for raw_line in raw_text.split('\n'):
        trimmed = raw_line.strip()
        if not trimmed:
            continue
        is_heading = bool(regex.match(r'^##(?!#)\s+', trimmed))  # H2 only
        stripped = strip_markdown(trimmed)
        # Drop the QQT metadata block — the labels are constant, content varies.
        if regex.match(r'^(Time|Focus|Scriptures)\b[^:\n]{0,24}:', stripped, flags=regex.IGNORECASE):
            continue
        # Boilerplate headings start their own paragraph but are NOT chapters.
        is_chapter = is_heading and heading_key(stripped) not in CHAPTER_EXCLUDE
        # Bible formatting must run BEFORE clean_text_general: general turns every
        # [ or ] into a comma, which would clobber bible mode's verse-number
        # removal (e.g. "[28]" -> "" only matches while the brackets survive).
        cleaned = clean_text_bible(stripped) if bible_mode else stripped
        cleaned = clean_text_general(cleaned)
        cleaned = cleaned.strip()
        if not cleaned:
            continue
        items.append({'text': cleaned, 'chapter': is_chapter, 'heading': is_heading})

    paragraphs = combine_short_paragraphs(items)
    # The first paragraph is the title — always a chapter start.
    if paragraphs:
        paragraphs[0]['chapter'] = True
    return [{'text': p['text'], 'chapter': p['chapter']} for p in paragraphs]


def derive_title(raw_text: str) -> str:
    """Use the first non-empty line (the document's title) as the project name."""
    for line in raw_text.split('\n'):
        line = line.strip()
        if line:
            return regex.sub(r'[.\s]+$', '', strip_markdown(line)).strip()
    return ''
