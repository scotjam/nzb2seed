"""Minimal bencode decoder/encoder.

The decoder also reports the raw byte span of the top-level ``info`` value so
the infohash can be computed from the exact bytes the torrent was built with
(re-encoding is not guaranteed to round-trip byte-for-byte on sloppy torrents).
"""


class BencodeError(ValueError):
    pass


def _decode(data: bytes, i: int):
    c = data[i:i + 1]
    if c == b"i":
        end = data.index(b"e", i)
        return int(data[i + 1:end]), end + 1
    if c == b"l":
        i += 1
        out = []
        while data[i:i + 1] != b"e":
            v, i = _decode(data, i)
            out.append(v)
        return out, i + 1
    if c == b"d":
        i += 1
        out = {}
        while data[i:i + 1] != b"e":
            k, i = _decode(data, i)
            if not isinstance(k, bytes):
                raise BencodeError("dict key is not a byte string")
            v, i = _decode(data, i)
            out[k] = v
        return out, i + 1
    if c.isdigit():
        colon = data.index(b":", i)
        n = int(data[i:colon])
        start = colon + 1
        if start + n > len(data):
            raise BencodeError("string runs past end of data")
        return data[start:start + n], start + n
    raise BencodeError(f"unexpected byte {c!r} at offset {i}")


def decode(data: bytes):
    try:
        value, end = _decode(data, 0)
    except (IndexError, ValueError) as e:
        if isinstance(e, BencodeError):
            raise
        raise BencodeError(str(e)) from e
    if end != len(data):
        raise BencodeError(f"trailing data after offset {end}")
    return value


def info_span(data: bytes) -> tuple[int, int]:
    """Byte span [start, end) of the top-level dict's ``info`` value."""
    if data[:1] != b"d":
        raise BencodeError("torrent is not a dictionary")
    i = 1
    while data[i:i + 1] != b"e":
        k, i = _decode(data, i)
        start = i
        _, i = _decode(data, i)
        if k == b"info":
            return start, i
    raise BencodeError("torrent has no info dictionary")


def encode(value) -> bytes:
    if isinstance(value, bool):
        raise TypeError("bool is not bencodable")
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, (bytes, bytearray)):
        return b"%d:%s" % (len(value), bytes(value))
    if isinstance(value, list):
        return b"l" + b"".join(encode(v) for v in value) + b"e"
    if isinstance(value, dict):
        items = sorted((k.encode() if isinstance(k, str) else k, v) for k, v in value.items())
        return b"d" + b"".join(encode(k) + encode(v) for k, v in items) + b"e"
    raise TypeError(f"cannot bencode {type(value).__name__}")
