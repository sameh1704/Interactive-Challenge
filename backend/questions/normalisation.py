"""Answer normalisation for the question types that accept free-form answers.

Kept out of :mod:`questions.models` so the rules can be read, and tested, on
their own. Every function here is pure: no database, no clock, no settings.

What "the same answer" means
----------------------------
Only for a short answer question is comparison a judgement call, and this module
draws that line narrowly on purpose. A typed answer is normalised for:

* surrounding whitespace,
* repeated internal whitespace,
* letter case, where the teacher has not asked for case to matter.

That is the whole list. No stemming, no edit distance, no fuzzy matching, no
forgiving punctuation - a rule that quietly accepts "photosynthesis" for
"photosynthesising" would mark a pupil right who has not understood the word, and
this system's scores are shown to a room. Where a teacher genuinely wants a second
wording accepted, they list it: :func:`normalise_short_answer` is what decides
whether a candidate matches one of several listed answers, not what makes new
answers acceptable.
"""

from __future__ import annotations

import re
import unicodedata

# A run of any whitespace - spaces, tabs, non-breaking spaces - between words.
_WHITESPACE_RUN = re.compile(r"\s+")


def collapse_whitespace(value: str) -> str:
    """Trim the ends and reduce any internal run of whitespace to one space."""
    return _WHITESPACE_RUN.sub(" ", value).strip()


def normalise_short_answer(value: str, *, case_sensitive: bool = False) -> str:
    """Normalise a typed answer for comparison against a stored key.

    The one concession to typography: a non-breaking space is a space. Teachers
    paste questions from documents that are full of them, and a pupil typing on a
    touchscreen keyboard can produce one by accident. Treating it as a space
    removes a failure that teaches a pupil nothing about the subject.

    Accents are deliberately *not* stripped - "café" and "cafe" are different
    words, and quietly conflating them would be the fuzzy matching this project
    has ruled out.
    """
    text = unicodedata.normalize("NFC", str(value))
    # U+00A0 is a no-break space. Fold it before collapsing runs, or the regex
    # would leave it standing as its own character.
    text = text.replace(" ", " ")
    text = collapse_whitespace(text)
    if not case_sensitive:
        # casefold rather than lower: it handles ß and the Turkish dotless i
        # predictably, which matters for a school that may type either.
        text = text.casefold()
    return text


def is_blank(value) -> bool:
    return not str(value).strip()


__all__ = [
    "collapse_whitespace",
    "is_blank",
    "normalise_short_answer",
]
