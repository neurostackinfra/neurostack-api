"""The ``[[MIDIA:n]]`` marker the agent ends a reply with to send a file.

envio-midia-agente, Scope IN 3 and DESIGN Decision 8.  The LLM chooses at
most one of the numbered candidates the server offered by ending its reply
with ``[[MIDIA:n]]``.  :func:`parse_media_marker` runs on the raw LLM text
before anything else reads it (the price and scheduling gates, the TTS, the
persisted ``suggested_response``, the training records), so no marker
variant ever reaches the patient.

Matching runs on the NFC form of the text, so a decomposed ``MÍDIA`` (``I`` +
combining acute) is the same word.  The spelling is ``MIDIA``, ``MÍDIA`` or
``MEDIA`` (a model that "translates" the token), any case, and it must not run
on into a letter, even after blanks, so ``[[Mediação]]``, ``[[Mídias]]``,
``[[Mídia social]]`` or ``[[Media Kit]]`` are ordinary text, while
``[[MIDIA 1]]`` or ``[[MIDIA-1]]`` are markers.
Then:

* every closed variant is removed wherever it is: ``[[`` + optional blanks +
  the word + anything up to the next ``]]``, each bracket optionally escaped
  with a backslash (``\\[\\[MIDIA:1\\]\\]``, the markdown-escaped form), and
  one stray bracket on each side (``[[[MIDIA:1]]]``) goes with it;
* a half-bracket marker with an index (``[MIDIA:1]``, ``[[MIDIA:1]``,
  ``[MIDIA:1]]``, escaped or not) is removed too, and never resolves: it
  counts as a marker with no valid index (``invalid_marker``);
* an unclosed tail at the very end of the text is removed only when it is a
  prefix of a marker (``[[``, ``[[M``, ``[[MIDIA:``, ``[[MIDIA:1``: a reply cut
  by ``max_tokens`` in the middle of the marker).  Any other ``[[`` stays;
* the index is resolved only from a closed ``[[MIDIA:<digits>]]`` (any of the
  three spellings, blanks around the parts, escaped brackets allowed) within
  ``1..candidate_count``, the numbers the server generated; the first valid
  one wins, and a truncated or half-bracket marker never resolves;
* text that is only markers parses to ``None``: no reply, and the caller
  drops the file even when the index is valid (the turn takes the
  ``no_llm_response`` path).

Text without any marker comes back unchanged, byte for byte (the same
object).  Text that had a marker comes back in NFC.  Every pattern is linear
in the length of the text (no blank or character is rescanned): the parse runs
synchronously on every reply.
Pure.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# The marker word: MIDIA, MÍDIA or MEDIA, not running on into a letter.
_WORD = r"M[IÍE]DIA(?![^\W\d_])"
# After the word (and optional blanks) a letter means a phrase
# (``[[Mídia social]]``, ``[[Media Kit]]``): ordinary text.  Anything else
# (``:``, a close, ``[[MIDIA 1]]``, ``[[MIDIA-1]]``, ``[[MIDIA_1]]``) is a
# marker, as the DESIGN's ``\[\[\s*MIDIA[^\]]*\]\]`` says.
_WORD_END = r"(?!\s*+[^\W\d_])"
# A bracket, optionally markdown-escaped with a backslash.
_OPEN = r"\\?\["
_CLOSE = r"\\?\]"
# The blanks just before a marker (so "PDF [[MIDIA:1]] ok" becomes "PDF ok").
# The lookbehind makes a run of blanks a single start position and the
# possessive ``*+`` never gives one back: without both, every position inside
# a long run of blanks rescans the run (quadratic on every reply).
_BLANKS = r"(?<![ \t])[ \t]*+"
# A closed marker.  ``[^\[\]]*`` stops at the first bracket: the marker ends at
# the first ``]]`` after the word, and a start without a close stops scanning
# at the next ``[``.  One stray bracket on each side (``[[[MIDIA:1]]]``) goes
# with it.
_CLOSED_MARKER = re.compile(
    rf"{_BLANKS}(?:{_OPEN})?{_OPEN}{_OPEN}\s*+{_WORD}{_WORD_END}[^\[\]]*"
    rf"{_CLOSE}{_CLOSE}(?:{_CLOSE})?",
    re.IGNORECASE,
)
# The one shape that names an index (matched against a closed marker).  At
# most 6 digits: ``int()`` refuses more than 4300, and no offer reaches 7.
_INDEXED_MARKER = re.compile(
    rf"(?:{_OPEN})?{_OPEN}{_OPEN}\s*{_WORD}\s*:\s*([0-9]{{1,6}})\s*{_CLOSE}{_CLOSE}"
    rf"(?:{_CLOSE})?",
    re.IGNORECASE,
)
# A half-bracket marker with an index (run after the closed ones are gone):
# stripped, never resolved.
_HALF_BRACKET_MARKER = re.compile(
    rf"{_BLANKS}(?:{_OPEN}){{1,2}}\s*+{_WORD}\s*+:\s*+[0-9]++\s*+(?:{_CLOSE}){{1,2}}",
    re.IGNORECASE,
)
# An unclosed prefix of a marker at the very end of the text: ``[[`` and then
# nothing, or a prefix of the word, or the word (not a phrase, as in
# ``_WORD_END``) and an unclosed rest.  Possessive quantifiers, and a rest
# that stops at the next bracket: no blank or character is ever rescanned.
_UNCLOSED_TAIL = re.compile(
    rf"{_BLANKS}{_OPEN}{_OPEN}\s*+"
    rf"(?:M(?:[IÍE](?:D(?:I(?:A{_WORD_END}"
    r"[^\[\]]*+(?:\\?\])?)?)?)?)?)?"
    r"\s*+\Z",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MediaMarkerParse:
    """The reply without markers and the candidate it chose, if any."""

    # The cleaned reply; ``None`` when nothing is left once the markers go.
    text: str | None
    # The 1-based candidate index of the first valid marker, else ``None``.
    index: int | None
    # A marker (closed, half-bracket or truncated) was found and removed.
    marker_found: bool


def parse_media_marker(text: str, candidate_count: int) -> MediaMarkerParse:
    """Remove every media marker from ``text`` and resolve the chosen index.

    ``candidate_count`` is how many candidates the server numbered for this
    turn (``0`` when it offered none: every marker is then invalid, and is
    still removed).
    """

    if "[" not in text:  # no marker shape can start without a bracket
        return MediaMarkerParse(text=text, index=None, marker_found=False)
    normalized = unicodedata.normalize("NFC", text)
    closed = list(_CLOSED_MARKER.finditer(normalized))
    cleaned = _CLOSED_MARKER.sub("", normalized) if closed else normalized
    cleaned, half_brackets = _HALF_BRACKET_MARKER.subn("", cleaned)
    tail = _UNCLOSED_TAIL.search(cleaned)
    if tail is not None:
        cleaned = cleaned[: tail.start()]
    if not closed and not half_brackets and tail is None:
        return MediaMarkerParse(text=text, index=None, marker_found=False)

    index: int | None = None
    for match in closed:
        indexed = _INDEXED_MARKER.fullmatch(match.group(0).lstrip(" \t"))
        if indexed is None:
            continue
        candidate = int(indexed.group(1))
        if 1 <= candidate <= candidate_count:
            index = candidate
            break

    stripped = cleaned.strip()
    return MediaMarkerParse(
        text=stripped or None,
        index=index,
        marker_found=True,
    )


__all__ = ["MediaMarkerParse", "parse_media_marker"]
