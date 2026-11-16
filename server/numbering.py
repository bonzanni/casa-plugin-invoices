# server/numbering.py
"""Invoice number pattern: render, parse, and the next-number rule (spec 5.3).

Pure: no file access. The caller passes every number already issued (from
``invoices/`` and ``finalizing/``) and the company's ``numbering`` floor.

A pattern holds ``{YYYY}`` and ``{seq:N}`` (N = 1..9, zero padding) exactly
once each; every other character is literal text. For a fixed year the number
is therefore ``prefix + digits + suffix`` with fixed prefix and suffix, so
parsing is a plain prefix/suffix check and can never mistake one number for
another.
"""
import re

from model import ValidationError

_TOKEN = re.compile(r"\{[^{}]*\}")
_SEQ = re.compile(r"seq:([1-9])")
_DIGITS = frozenset("0123456789")
_LITERAL_OK = re.compile(r"[A-Za-z0-9._ -]")


def _split(pattern):
    """Return (before_seq, after_seq, width) with ``{YYYY}`` still in place."""
    if not isinstance(pattern, str):
        raise ValidationError("numbering.pattern: must be text")
    years, seqs = 0, []
    for m in _TOKEN.finditer(pattern):
        body = m.group()[1:-1]
        if body == "YYYY":
            years += 1
            continue
        s = _SEQ.fullmatch(body)
        if s is None:
            raise ValidationError(
                f"numbering.pattern: unknown token {m.group()!r}; "
                "use {YYYY} and {seq:N} with N from 1 to 9")
        seqs.append((m, int(s.group(1))))
    literal = _TOKEN.sub("", pattern)
    if "{" in literal or "}" in literal:
        raise ValidationError("numbering.pattern: unmatched { or }")
    if years != 1 or len(seqs) != 1:
        raise ValidationError(
            "numbering.pattern: must contain {YYYY} and {seq:N} exactly once each")
    m, width = seqs[0]
    return pattern[:m.start()], pattern[m.end():], width


def _year_text(year):
    if isinstance(year, bool) or not isinstance(year, int) or not 1000 <= year <= 9999:
        raise ValidationError("year: must be a four-digit year")
    return str(year)


def validate_pattern(pattern: str) -> None:
    """Raise ValidationError unless ``pattern`` is a usable number pattern.

    The number names the invoice's files, so its literal text is limited to
    letters, digits, ``.``, ``_``, ``-`` and space, and may not start with ``.``.
    """
    _split(pattern)
    bad = sorted(set(_LITERAL_OK.sub("", _TOKEN.sub("", pattern))))
    if bad:
        raise ValidationError(
            f"numbering.pattern: {''.join(bad)!r} cannot be part of an invoice number "
            "(use letters, digits, '.', '_', '-' or spaces; e.g. '-' instead of '/')")
    if pattern.startswith("."):
        raise ValidationError("numbering.pattern: must not start with '.'")


def render(pattern: str, year: int, seq: int) -> str:
    before, after, width = _split(pattern)
    y = _year_text(year)
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise ValidationError("seq: must be a whole number from 1")
    return (before.replace("{YYYY}", y) + str(seq).zfill(width)
            + after.replace("{YYYY}", y))


def parse_seq(pattern: str, number: str, year: int) -> int | None:
    """The seq of ``number`` if it matches ``pattern`` for ``year``, else None.

    Any run of at least N ASCII digits counts (``INV-2026-00050`` is 50): a
    number that was issued is never ignored, so the next one goes past it.
    """
    before, after, width = _split(pattern)
    y = _year_text(year)
    if not isinstance(number, str):
        raise TypeError(f"invoice number must be str, not {type(number).__name__}")
    prefix, suffix = before.replace("{YYYY}", y), after.replace("{YYYY}", y)
    if len(number) < len(prefix) + width + len(suffix):
        return None
    if not (number.startswith(prefix) and number.endswith(suffix)):
        return None
    digits = number[len(prefix):len(number) - len(suffix)]
    if not set(digits) <= _DIGITS:
        return None
    return int(digits)


def next_number(pattern: str, year: int, existing: list[str], floor: dict) -> tuple[str, int]:
    """Max seq issued for ``year`` + 1, raised to ``floor["next_seq"]`` when the
    floor belongs to that year. Returns ``(number, seq)``."""
    validate_pattern(pattern)
    _year_text(year)
    seqs = [s for s in (parse_seq(pattern, n, year) for n in existing) if s is not None]
    seq = max(seqs, default=0) + 1
    if floor["seq_year"] == year:
        seq = max(seq, floor["next_seq"])
    return render(pattern, year, seq), seq
