"""``parse_media_marker`` (envio-midia-agente Group 4).

No variant of the ``[[MIDIA:n]]`` marker survives the parse, an index only
resolves within the server's numbering (first valid wins), a truncated
marker never resolves, text that was only markers is no reply, and text
without any marker comes back byte for byte.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable

import pytest

from src.services.media_marker import MediaMarkerParse, parse_media_marker


@pytest.mark.parametrize(
    ("raw", "count", "text", "index"),
    [
        # The wish's variants.
        ("Segue o preparo. [[midia:1]]", 3, "Segue o preparo.", 1),
        ("Segue o preparo. [[ MIDIA : 9 ]]", 3, "Segue o preparo.", None),
        ("Segue o preparo. [[ MIDIA : 2 ]]", 3, "Segue o preparo.", 2),
        ("Segue o preparo. [[MIDIA:x]]", 3, "Segue o preparo.", None),
        ("Segue. [[MIDIA:1]] e [[MIDIA:2]]", 3, "Segue. e", 1),
        ("Segue o preparo. [[MIDIA:1", 3, "Segue o preparo.", None),
        # The first VALID index wins, not the first marker.
        ("Segue. [[MIDIA:9]] [[MIDIA:2]]", 3, "Segue.", 2),
        ("Segue. [[MIDIA:0]] [[MIDIA:3]]", 3, "Segue.", 3),
        # Accented and mixed-case spellings, blanks and newlines inside.
        ("Segue o arquivo [[MÍDIA:2]]", 2, "Segue o arquivo", 2),
        ("Segue o arquivo [[mídia:1]]", 2, "Segue o arquivo", 1),
        ("Segue o arquivo\n[[\n  MIDIA:1\n]]", 1, "Segue o arquivo", 1),
        # Mid-text marker: the blank before it goes with it.
        ("Segue [[MIDIA:1]] o preparo.", 1, "Segue o preparo.", 1),
        # Truncated tails of every length.
        ("Segue o preparo [[", 1, "Segue o preparo", None),
        ("Segue o preparo [[M", 1, "Segue o preparo", None),
        ("Segue o preparo [[ MIDIA : ", 1, "Segue o preparo", None),
        # A closed marker plus a truncated one after it.
        ("Segue. [[MIDIA:1]] Mais [[MIDIA:", 1, "Segue. Mais", 1),
        # Out of the numbering, or nothing offered (count 0): stripped, no file.
        ("Segue. [[MIDIA:6]]", 5, "Segue.", None),
        ("Segue. [[MIDIA:1]]", 0, "Segue.", None),
        ("Segue. [[MIDIA:-1]]", 3, "Segue.", None),
        ("Segue. [[MIDIA:1.0]]", 3, "Segue.", None),
    ],
)
def test_every_variant_is_removed_and_only_a_numbered_index_resolves(
    raw: str, count: int, text: str, index: int | None
) -> None:
    parsed = parse_media_marker(raw, count)

    assert parsed == MediaMarkerParse(text=text, index=index, marker_found=True)
    assert "[[" not in (parsed.text or "")
    assert "midia" not in (parsed.text or "").lower()


@pytest.mark.parametrize(
    "raw",
    [
        "[[MIDIA:1]]",
        "  [[MIDIA:1]]\n",
        "[[MIDIA:1]][[MIDIA:2]]",
        "[[MIDIA:1",
        "\n\n[[",
    ],
)
def test_text_that_was_only_markers_is_no_reply(raw: str) -> None:
    parsed = parse_media_marker(raw, 2)

    assert parsed.text is None
    assert parsed.marker_found is True


def test_an_only_marker_reply_still_reports_its_valid_index() -> None:
    # The caller drops the file (``dropped``): the parse only reports.
    assert parse_media_marker("[[MIDIA:2]]", 2) == MediaMarkerParse(
        text=None, index=2, marker_found=True
    )


@pytest.mark.parametrize(
    "raw",
    [
        "Olá! Como posso ajudar?",
        "  espaços nas pontas e  duplos  ficam  \n",
        "Colchetes simples [assim] ficam.",
        "Um par fechado que não é marcador [[nota]] fica.",
        "MIDIA:1 sem colchetes fica",
        "",
    ],
)
def test_text_without_markers_comes_back_byte_for_byte(raw: str) -> None:
    parsed = parse_media_marker(raw, 3)

    assert parsed == MediaMarkerParse(text=raw, index=None, marker_found=False)
    assert parsed.text is raw


def test_a_closed_non_media_pair_is_kept_while_a_trailing_tail_goes() -> None:
    parsed = parse_media_marker("Veja [[nota]] e depois [[MIDIA:1", 1)

    assert parsed == MediaMarkerParse(
        text="Veja [[nota]] e depois", index=None, marker_found=True
    )


# --- LOW-2: an unclosed ``[[`` is cut only when it is a marker prefix. ---


@pytest.mark.parametrize(
    "raw",
    [
        "Use [[ para abrir e depois continue normalmente",
        "Abra com [[ e feche com ]] depois",
        "Veja [[nota sem fechar",
        "Texto com [[Mediação no fim",
        "Um [[ no meio e [[ outro",
        "Segue o preparo, sem nenhum marcador.",
    ],
)
def test_ordinary_open_brackets_come_back_byte_for_byte(raw: str) -> None:
    parsed = parse_media_marker(raw, 3)

    assert parsed == MediaMarkerParse(text=raw, index=None, marker_found=False)
    assert parsed.text is raw


@pytest.mark.parametrize(
    "tail",
    [
        "[[",
        "[[M",
        "[[MID",
        "[[MÍDIA",
        "[[MIDIA:",
        "[[MIDIA:1",
        "[[MIDIA:1]",
        "[[ midia : 1 ",
        "[[MED",
        "[[MEDIA:2",
        "\\[\\[MIDIA:1\\",
    ],
)
def test_a_marker_prefix_cut_at_the_end_is_removed(tail: str) -> None:
    parsed = parse_media_marker(f"Segue o preparo. {tail}", 3)

    assert parsed == MediaMarkerParse(
        text="Segue o preparo.", index=None, marker_found=True
    )


# --- LOW-1: off-spec marker shapes never reach the patient. ---


@pytest.mark.parametrize(
    ("raw", "text", "index"),
    [
        # Resolve like the canonical marker: the double-bracket structure is
        # intact, only the spelling or the encoding differs.
        ("Here is the file. [[MEDIA:1]]", "Here is the file.", 1),
        ("Here is the file. [[ media : 2 ]]", "Here is the file.", 2),
        ("Segue o arquivo [[MÍDIA:2]]", "Segue o arquivo", 2),
        ("Segue o arquivo \\[\\[MIDIA:1\\]\\]", "Segue o arquivo", 1),
        ("Segue o arquivo \\[\\[MEDIA:2\\]\\]", "Segue o arquivo", 2),
        # Off the numbering: stripped, no index.
        ("Here is the file. [[MEDIA:9]]", "Here is the file.", None),
        ("Segue \\[\\[MIDIA:9\\]\\]", "Segue", None),
        # Half-bracket shapes: stripped, never resolved.
        ("Segue o arquivo [MIDIA:1]", "Segue o arquivo", None),
        ("Segue o arquivo [MEDIA:1] ok", "Segue o arquivo ok", None),
        ("[[MIDIA:1] Segue o preparo", "Segue o preparo", None),
        ("Segue [MIDIA:1]] ok", "Segue ok", None),
        ("Segue \\[MIDIA:1\\]", "Segue", None),
    ],
)
def test_off_spec_markers_are_stripped(raw: str, text: str, index: int | None) -> None:
    parsed = parse_media_marker(raw, 3)

    assert parsed == MediaMarkerParse(text=text, index=index, marker_found=True)
    assert "midia" not in (parsed.text or "").lower()
    assert "media" not in (parsed.text or "").lower()


@pytest.mark.parametrize(
    "raw",
    [
        "Segue [[Mediação]] ok",
        "Veja [[Mídias sociais]] ok",
        "Nossa [[Mídia social]] tem novidades",
        "Check our [[Media Kit]] for details",
        "Check our [[Media Kit]]",
        "Fim com [[Media Kit",
        "Clique em [Mídia] ok",
        "Clique em [Media: fotos] ok",
        "Os arquivos de mídia: 1 PDF.",
        "Ver [1] e [2]",
    ],
)
def test_ordinary_bracket_text_with_the_word_is_kept(raw: str) -> None:
    parsed = parse_media_marker(raw, 3)

    assert parsed == MediaMarkerParse(text=raw, index=None, marker_found=False)


@pytest.mark.parametrize(
    ("raw", "text"),
    [
        ("Segue [[[MIDIA:1]]] ok", "Segue ok"),
        ("Segue [[MIDIA:1]]] ok", "Segue ok"),
        ("Segue [[[MIDIA:1]] ok", "Segue ok"),
    ],
)
def test_a_stray_bracket_next_to_a_marker_goes_with_it(raw: str, text: str) -> None:
    assert parse_media_marker(raw, 3) == MediaMarkerParse(
        text=text, index=1, marker_found=True
    )


# --- The parse runs synchronously on every reply: it must stay linear. ---

# A wall-clock budget flakes under load, so the guard is a scaling ratio:
# the same input shape at n and 4n characters.  A linear parse takes about 4x
# as long, a quadratic one about 16x; load slows both sizes alike.
_SCALE = 4
_MAX_RATIO = 8.0
# Each sample repeats the parse until the small size takes at least this long,
# so sub-millisecond noise cannot make a ratio.
_SAMPLE_SECONDS = 0.005
# Below this the large sample is noise: only the absolute bound applies.
_RATIO_FLOOR_SECONDS = 0.005
# A loose absolute bound per parse of the large size (the linear parse takes
# milliseconds at these sizes, the quadratic ones seconds).
_MAX_PARSE_SECONDS = 1.0

# id -> (input of about n characters, n).  Each n is where the quadratic rule
# this shape caught is already dominated by its n**2 term.
_ADVERSARIAL_SHAPES: dict[str, tuple[Callable[[int], str], int]] = {
    "blanks": (lambda n: " " * n + "x [", 3_000),
    "tabs": (lambda n: "\t" * n + "x [", 3_000),
    "bracket-blanks": (lambda n: "[" + " " * n + "x", 3_000),
    "open-blanks": (lambda n: "[[" + " " * n + "x", 3_000),
    "word-blanks": (lambda n: "[[MIDIA" + " " * n + "x] y", 3_000),
    "open-markers": (lambda n: "[[MIDIA:" * (n // 8), 6_000),
    "open-markers-then-close": (lambda n: "[[MIDIA:" * (n // 8) + "] x", 24_000),
    "half-markers": (lambda n: "[MIDIA:" * (n // 7), 6_000),
    "open-prefixes": (lambda n: "[[M" * (n // 3), 6_000),
}


def _sample(raw: str, repeat: int) -> float:
    started = time.perf_counter()
    for _ in range(repeat):
        parse_media_marker(raw, 3)
    return time.perf_counter() - started


@pytest.mark.parametrize("shape", list(_ADVERSARIAL_SHAPES))
def test_the_parse_scales_linearly_on_adversarial_input(shape: str) -> None:
    make, n = _ADVERSARIAL_SHAPES[shape]
    small, large = make(n), make(_SCALE * n)

    single = min(_sample(small, 1) for _ in range(3))
    repeat = max(1, min(1_000, math.ceil(_SAMPLE_SECONDS / max(single, 1e-9))))
    small_time = (
        single if repeat == 1 else min(_sample(small, repeat) for _ in range(3))
    )
    # Best of 3; once one sample is within the ratio, more can only be lower.
    large_time = math.inf
    for _ in range(3):
        large_time = min(large_time, _sample(large, repeat))
        if large_time < _MAX_RATIO * small_time:
            break

    assert large_time / repeat <= _MAX_PARSE_SECONDS, f"{large_time / repeat:.3f}s"
    if large_time >= _RATIO_FLOOR_SECONDS:
        ratio = large_time / small_time
        assert ratio < _MAX_RATIO, (
            f"t({_SCALE}n)/t(n) = {ratio:.1f} "
            f"({large_time:.4f}s / {small_time:.4f}s, x{repeat})"
        )


@pytest.mark.parametrize(
    "raw",
    [
        "Segue o preparo. [[MIDIA 1]]",
        "Segue o preparo. [[MIDIA-1]]",
        "Segue o preparo. [[MIDIA #1]]",
        "Segue o preparo. [[MIDIA_1]]",
        "Segue o preparo. [[MIDIA=1]]",
        "Segue o preparo. [[MIDIA 1",
    ],
)
def test_near_markers_are_stripped_and_never_resolve(raw: str) -> None:
    """The DESIGN strips every ``[[MIDIA...]]``: only a letter after the word
    (a phrase like ``[[Media Kit]]``) is ordinary text."""

    assert parse_media_marker(raw, 3) == MediaMarkerParse(
        text="Segue o preparo.", index=None, marker_found=True
    )


@pytest.mark.parametrize("digits", [7, 4301, 5000])
def test_a_giant_index_is_stripped_without_crashing_and_never_resolves(
    digits: int,
) -> None:
    # ``int()`` refuses more than 4300 digits; the index is bounded first.
    # The marker was there, its index is not one the server numbered:
    # ``invalid_marker`` at the call site.
    raw = "Segue o preparo. [[MIDIA:" + "1" * digits + "]]"

    assert parse_media_marker(raw, 3) == MediaMarkerParse(
        text="Segue o preparo.", index=None, marker_found=True
    )
