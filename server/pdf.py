"""A minimal PDF 1.4 writer: A4 pages, the two standard Helvetica fonts,
text, lines, filled/stroked rectangles. Standard library only.

Coordinates are in points with y measured from the TOP of the page (as pdfkit
does); they are converted to PDF space (y up) when written. For text, y is the
top of the line box and the baseline sits one ascent (0.718 em) below it.

Text is encoded as cp1252 (WinAnsiEncoding); a character outside it raises
ValueError (callers validate text earlier, at set/edit time).
"""
from __future__ import annotations

import math
import zlib

PAGE_W = 595.28           # A4
PAGE_H = 841.89
ASCENT = 0.718            # Helvetica ascender, 1/1000 em (AFM "Ascender 718")
FONTS = {"F1": "Helvetica", "F2": "Helvetica-Bold"}

# Glyph advance widths in 1/1000 em for cp1252 codes 32..255, in code order.
# Source: Adobe Core14 AFM metrics, Helvetica.afm / Helvetica-Bold.afm
# (Version 002.000, Adobe 1997), each code mapped to its WinAnsiEncoding glyph
# name (PDF 32000-1:2008, Annex D.2); 0xA0 -> space, 0xAD -> hyphen. Codes with
# no glyph (0x7F, 0x81, 0x8D, 0x8F, 0x90, 0x9D) get 556; cp1252 cannot encode
# text to them, so they never print.
_HELVETICA = (
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584, 556,
    556, 556, 222, 556, 333, 1000, 556, 556, 333, 1000, 667, 333, 1000, 556, 611, 556,
    556, 222, 222, 333, 333, 350, 556, 1000, 333, 1000, 500, 333, 944, 556, 500, 667,
    278, 333, 556, 556, 556, 556, 260, 556, 333, 737, 370, 556, 584, 333, 737, 333,
    400, 584, 333, 333, 333, 556, 537, 278, 333, 333, 365, 556, 834, 834, 834, 611,
    667, 667, 667, 667, 667, 667, 1000, 722, 667, 667, 667, 667, 278, 278, 278, 278,
    722, 722, 778, 778, 778, 778, 778, 584, 778, 722, 722, 722, 722, 667, 667, 611,
    556, 556, 556, 556, 556, 556, 889, 500, 556, 556, 556, 556, 278, 278, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 584, 611, 556, 556, 556, 556, 500, 556, 500,
)
_HELVETICA_BOLD = (
    278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611,
    975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556,
    333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
    611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584, 556,
    556, 556, 278, 556, 500, 1000, 556, 556, 333, 1000, 667, 333, 1000, 556, 611, 556,
    556, 278, 278, 500, 500, 350, 556, 1000, 333, 1000, 556, 333, 944, 556, 500, 667,
    278, 333, 556, 556, 556, 556, 280, 556, 333, 737, 370, 556, 584, 333, 737, 333,
    400, 584, 333, 333, 333, 611, 556, 278, 333, 333, 365, 556, 834, 834, 834, 611,
    722, 722, 722, 722, 722, 722, 1000, 722, 667, 667, 667, 667, 278, 278, 278, 278,
    722, 722, 778, 778, 778, 778, 778, 584, 778, 722, 722, 722, 722, 667, 667, 611,
    556, 556, 556, 556, 556, 556, 889, 556, 556, 556, 556, 556, 278, 278, 278, 278,
    611, 611, 611, 611, 611, 611, 611, 584, 611, 611, 611, 611, 611, 556, 611, 556,
)
HELVETICA_WIDTHS: dict[int, int] = dict(enumerate(_HELVETICA, start=32))
HELVETICA_BOLD_WIDTHS: dict[int, int] = dict(enumerate(_HELVETICA_BOLD, start=32))
_WIDTHS = {"F1": HELVETICA_WIDTHS, "F2": HELVETICA_BOLD_WIDTHS}


def encode(text: str) -> bytes:
    try:
        return text.encode("cp1252")
    except UnicodeEncodeError as e:
        raise ValueError(f"text {text!r} has characters that cannot be printed "
                         f"(not in Windows-1252): {e.object[e.start:e.end]!r}") from None


def text_width(text: str, font: str, size: float) -> float:
    widths = _WIDTHS[font]
    return sum(widths.get(b, 556) for b in encode(text)) * size / 1000


def wrap(text: str, font: str, size: float, max_width: float) -> list[str]:
    """`text` broken into lines no wider than `max_width`, at spaces; a word
    longer than a line is broken between characters. Always at least one line."""
    lines: list[str] = []
    line = ""
    for word in text.split():
        candidate = f"{line} {word}" if line else word
        if text_width(candidate, font, size) <= max_width:
            line = candidate
            continue
        if line:
            lines.append(line)
        line = ""
        while text_width(word, font, size) > max_width:
            cut = 1
            while cut < len(word) and text_width(word[:cut + 1], font, size) <= max_width:
                cut += 1
            lines.append(word[:cut])
            word = word[cut:]
        line = word
    lines.append(line)
    return lines


def _n(v: float) -> str:
    """A number for a content stream: up to 3 decimals, no trailing zeros."""
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _rgb(color) -> str:
    return " ".join(_n(c) for c in color)


def _literal(text: str) -> str:
    """A PDF literal string; bytes kept as latin-1 chars so the stream encodes 1:1."""
    raw = encode(text).decode("latin-1")
    return "(" + raw.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ")"


def _utf16(text: str) -> bytes:
    return b"<FEFF" + text.encode("utf-16-be").hex().upper().encode() + b">"


class Document:
    """Pages of drawing operators; `to_bytes` writes the file."""

    def __init__(self, info: dict[str, str] | None = None):
        self.pages: list[list[str]] = []
        self.info = dict(info or {})
        self._alphas: set[int] = set()             # opacities in percent, one ExtGState each

    def new_page(self) -> None:
        self.pages.append([])

    def _ops(self) -> list[str]:
        if not self.pages:
            self.new_page()
        return self.pages[-1]

    # ---------------------------------------------------------------- text

    def text(self, x: float, y: float, s: str, font: str = "F1", size: float = 10,
             color=(0, 0, 0)) -> None:
        base = PAGE_H - y - ASCENT * size
        self._ops().append(f"BT /{font} {_n(size)} Tf {_rgb(color)} rg "
                           f"1 0 0 1 {_n(x)} {_n(base)} Tm {_literal(s)} Tj ET")

    def text_right(self, x_right: float, y: float, s: str, font: str = "F1",
                   size: float = 10, color=(0, 0, 0)) -> None:
        self.text(x_right - text_width(s, font, size), y, s, font, size, color)

    def text_center(self, x_center: float, y: float, s: str, font: str = "F1",
                    size: float = 10, color=(0, 0, 0)) -> None:
        self.text(x_center - text_width(s, font, size) / 2, y, s, font, size, color)

    def rotated_text(self, x: float, y: float, s: str, angle_deg: float, size: float,
                     color=(0, 0, 0), font: str = "F2", opacity: float = 1.0) -> None:
        """`s` with its baseline start at (x, y), turned counter-clockwise."""
        a = math.radians(angle_deg)
        c, si = math.cos(a), math.sin(a)
        alpha = ""
        if opacity < 1:
            pct = round(opacity * 100)
            self._alphas.add(pct)
            alpha = f"/GS{pct} gs "
        self._ops().append(f"q {alpha}BT /{font} {_n(size)} Tf {_rgb(color)} rg "
                           f"{_n(c)} {_n(si)} {_n(-si)} {_n(c)} {_n(x)} {_n(PAGE_H - y)} Tm "
                           f"{_literal(s)} Tj ET Q")

    # ---------------------------------------------------------------- shapes

    def line(self, x1: float, y1: float, x2: float, y2: float, width: float = 1,
             color=(0, 0, 0)) -> None:
        self._ops().append(f"{_n(width)} w {_rgb(color)} RG {_n(x1)} {_n(PAGE_H - y1)} m "
                           f"{_n(x2)} {_n(PAGE_H - y2)} l S")

    def rect(self, x: float, y: float, w: float, h: float, fill=None, stroke=None,
             radius: float = 0, line_width: float = 0.5) -> None:
        """A rectangle with its top-left corner at (x, y); rounded when `radius`."""
        if fill is None and stroke is None:
            return
        x0, y0 = x, PAGE_H - y - h                     # bottom-left in PDF space
        if radius <= 0:
            path = f"{_n(x0)} {_n(y0)} {_n(w)} {_n(h)} re"
        else:
            r = min(radius, w / 2, h / 2)
            k = r * 0.5523                             # Bezier quarter-circle constant
            x1, y1 = x0 + w, y0 + h
            path = " ".join([
                f"{_n(x0 + r)} {_n(y0)} m", f"{_n(x1 - r)} {_n(y0)} l",
                f"{_n(x1 - r + k)} {_n(y0)} {_n(x1)} {_n(y0 + r - k)} {_n(x1)} {_n(y0 + r)} c",
                f"{_n(x1)} {_n(y1 - r)} l",
                f"{_n(x1)} {_n(y1 - r + k)} {_n(x1 - r + k)} {_n(y1)} {_n(x1 - r)} {_n(y1)} c",
                f"{_n(x0 + r)} {_n(y1)} l",
                f"{_n(x0 + r - k)} {_n(y1)} {_n(x0)} {_n(y1 - r + k)} {_n(x0)} {_n(y1 - r)} c",
                f"{_n(x0)} {_n(y0 + r)} l",
                f"{_n(x0)} {_n(y0 + r - k)} {_n(x0 + r - k)} {_n(y0)} {_n(x0 + r)} {_n(y0)} c",
                "h"])
        ops = []
        if fill is not None:
            ops.append(f"{_rgb(fill)} rg")
        if stroke is not None:
            ops.append(f"{_n(line_width)} w {_rgb(stroke)} RG")
        paint = "B" if fill is not None and stroke is not None else ("f" if fill is not None else "S")
        self._ops().append(" ".join(ops + [path, paint]))

    # ---------------------------------------------------------------- output

    def to_bytes(self, compress: bool = True) -> bytes:
        if not self.pages:
            self.new_page()
        objs: list[bytes] = []                       # object n is objs[n - 1]

        def add(body: bytes) -> int:
            objs.append(body)
            return len(objs)

        catalog = add(b"")                           # filled once /Pages is known
        pages_ref = add(b"")
        font_refs = []
        for key, name in FONTS.items():
            num = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /%s "
                      b"/Encoding /WinAnsiEncoding >>" % name.encode())
            font_refs.append(f"/{key} {num} 0 R")
        fonts = " ".join(font_refs)
        resources = f"/Font << {fonts} >>"
        if self._alphas:
            states = " ".join(f"/GS{a} << /Type /ExtGState /ca {_n(a / 100)} >>"
                              for a in sorted(self._alphas))
            resources += f" /ExtGState << {states} >>"
        kids = []
        for ops in self.pages:
            stream = "\n".join(ops).encode("latin-1")
            head = b""
            if compress:
                stream = zlib.compress(stream)
                head = b" /Filter /FlateDecode"
            content = add(b"<< /Length %d%s >>\nstream\n" % (len(stream), head)
                          + stream + b"\nendstream")
            kids.append(add(
                f"<< /Type /Page /Parent {pages_ref} 0 R /MediaBox [0 0 {_n(PAGE_W)} "
                f"{_n(PAGE_H)}] /Resources << {resources} >> /Contents {content} 0 R >>"
                .encode()))
        objs[catalog - 1] = f"<< /Type /Catalog /Pages {pages_ref} 0 R >>".encode()
        objs[pages_ref - 1] = (f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] "
                               f"/Count {len(kids)} >>").encode()
        info = None
        if self.info:
            info = add(b"<< " + b" ".join(b"/" + k.encode("ascii") + b" " + _utf16(v)
                                          for k, v in self.info.items()) + b" >>")

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for num, body in enumerate(objs, start=1):
            offsets.append(len(out))
            out += b"%d 0 obj\n" % num + body + b"\nendobj\n"
        xref = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
        out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
        trailer = f"<< /Size {len(objs) + 1} /Root {catalog} 0 R"
        if info is not None:
            trailer += f" /Info {info} 0 R"
        out += f"trailer\n{trailer} >>\nstartxref\n{xref}\n%%EOF\n".encode()
        return bytes(out)
