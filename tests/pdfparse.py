# tests/pdfparse.py
"""Test helper: just enough PDF parsing to check what server/pdf.py writes.

Splits the file into numbered objects, inflates Flate streams, and reads the
text-showing operators (`Tm` + `Tj`) of each page's content stream. Not a
general PDF reader: it understands the subset pdf.py produces.
"""
from __future__ import annotations

import re
import zlib

_OBJ = re.compile(rb"(\d+) 0 obj\s*(.*?)\s*endobj", re.S)
_STREAM = re.compile(rb"(.*?)stream\r?\n(.*?)\r?\nendstream", re.S)
_TOKEN = re.compile(rb"\((?:\\.|[^\\)])*\)|[^\s()]+", re.S)
_ESC = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f",
        b"(": b"(", b")": b")", b"\\": b"\\"}


def objects(data: bytes) -> dict[int, tuple[bytes, bytes | None]]:
    """{object number: (dictionary bytes, inflated stream or None)}."""
    out = {}
    for m in _OBJ.finditer(data):
        body = m.group(2)
        s = _STREAM.match(body)
        if s is None:
            out[int(m.group(1))] = (body, None)
            continue
        head, raw = s.group(1), s.group(2)
        if b"/FlateDecode" in head:
            raw = zlib.decompress(raw)
        out[int(m.group(1))] = (head, raw)
    return out


def unescape(literal: bytes) -> str:
    """A PDF literal string body (without the parentheses) as text (cp1252)."""
    out, i = bytearray(), 0
    while i < len(literal):
        c = literal[i:i + 1]
        if c != b"\\":
            out += c
            i += 1
            continue
        nxt = literal[i + 1:i + 2]
        octal = re.match(rb"[0-7]{1,3}", literal[i + 1:i + 4])
        if octal:
            out.append(int(octal.group(0), 8) & 0xFF)
            i += 1 + len(octal.group(0))
        else:
            out += _ESC.get(nxt, nxt)
            i += 2
    return out.decode("cp1252")


def pages(data: bytes) -> list[int]:
    """Object numbers of the page objects, in /Kids order."""
    objs = objects(data)
    for head, _ in objs.values():
        if re.search(rb"/Type\s*/Pages\b", head):
            kids = re.search(rb"/Kids\s*\[(.*?)\]", head, re.S).group(1)
            return [int(n) for n in re.findall(rb"(\d+) 0 R", kids)]
    raise AssertionError("no /Pages object")


def page_count(data: bytes) -> int:
    return len(pages(data))


def texts(data: bytes) -> list[dict]:
    """Every `Tj`, in drawing order: {page, x, y, font, size, text}.

    x, y are the text matrix origin (the baseline start, PDF space: y up).
    """
    objs = objects(data)
    out = []
    for pno, num in enumerate(pages(data)):
        head = objs[num][0]
        content = int(re.search(rb"/Contents\s+(\d+) 0 R", head).group(1))
        stream = objs[content][1] or b""
        stack, font, size, x, y = [], None, None, 0.0, 0.0
        for tok in _TOKEN.findall(stream):
            if tok == b"Tf":
                font, size = stack[-2].lstrip(b"/").decode(), float(stack[-1])
            elif tok == b"Tm":
                x, y = float(stack[-2]), float(stack[-1])
            elif tok == b"Tj":
                out.append({"page": pno, "x": x, "y": y, "font": font, "size": size,
                            "text": unescape(stack[-1][1:-1])})
            if re.fullmatch(rb"[A-Za-z*'\"]+", tok):
                stack = []
            else:
                stack.append(tok)
    return out
