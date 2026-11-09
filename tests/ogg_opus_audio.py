"""Build small Ogg Opus files (RFC 3533 pages, RFC 7845 headers) for tests.

The CRC field is never read by the code under test, so it stays zero. The
audio packets are filler bytes: only the page structure and the granule
positions carry the duration.
"""

from __future__ import annotations

OPUS_SERIAL = (0x5EED).to_bytes(4, "little")
_BOS = 0x02
_EOS = 0x04


def opus_identification_header(*, pre_skip: int = 312, channels: int = 1) -> bytes:
    """The 19-byte ``OpusHead``."""

    return (
        b"OpusHead"
        + bytes([1, channels])
        + pre_skip.to_bytes(2, "little")
        + (48_000).to_bytes(4, "little")  # input sample rate
        + (0).to_bytes(2, "little")  # output gain
        + bytes([0])  # channel mapping family
    )


def lacing(packet_size: int) -> bytes:
    """The segment table of one packet that ends on its page."""

    return bytes([255] * (packet_size // 255) + [packet_size % 255])


def ogg_page(
    body: bytes,
    *,
    granule: int,
    header_type: int = 0,
    serial: bytes = OPUS_SERIAL,
    segments: bytes | None = None,
) -> bytes:
    """One page holding ``body``, by default as a single whole packet."""

    table = lacing(len(body)) if segments is None else segments
    return (
        b"OggS"
        + bytes([0, header_type])
        + granule.to_bytes(8, "little", signed=True)
        + serial
        + bytes(4)  # page sequence number
        + bytes(4)  # CRC
        + bytes([len(table)])
        + table
        + body
    )


def ogg_opus_file(
    seconds: float,
    *,
    pre_skip: int = 312,
    audio_pages: int = 3,
    packet_size: int = 40,
) -> bytes:
    """A whole mono Ogg Opus file that plays for ``seconds``.

    ID header page, comment header page, then ``audio_pages`` audio pages
    whose granules grow evenly to ``pre_skip + seconds * 48000``; the last
    one is flagged end of stream.
    """

    total = pre_skip + round(seconds * 48_000)
    pages = [
        ogg_page(
            opus_identification_header(pre_skip=pre_skip),
            granule=0,
            header_type=_BOS,
        ),
        ogg_page(b"OpusTags" + bytes(8), granule=0),
    ]
    for index in range(1, audio_pages + 1):
        pages.append(
            ogg_page(
                bytes(packet_size),
                granule=pre_skip + (total - pre_skip) * index // audio_pages,
                header_type=_EOS if index == audio_pages else 0,
            )
        )
    return b"".join(pages)
