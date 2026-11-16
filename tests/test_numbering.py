# tests/test_numbering.py
"""Invoice number pattern: render, parse, and the next-number rule (spec 5.3)."""
import pathlib
import sys
import unittest

SERVER = pathlib.Path(__file__).resolve().parents[1] / "server"
sys.path.insert(0, str(SERVER))
from model import ValidationError  # noqa: E402
from numbering import next_number, parse_seq, render, validate_pattern  # noqa: E402

P = "INV-{YYYY}-{seq:4}"
NO_FLOOR = {"next_seq": 1, "seq_year": None}


class TestRender(unittest.TestCase):
    def test_render_default(self):
        self.assertEqual(render(P, 2026, 12), "INV-2026-0012")

    def test_render_wider_than_padding(self):
        self.assertEqual(render(P, 2026, 12345), "INV-2026-12345")

    def test_render_other_layouts(self):
        self.assertEqual(render("{seq:3}/{YYYY}", 2026, 7), "007/2026")
        self.assertEqual(render("{YYYY}{seq:1}", 2026, 7), "20267")

    def test_render_literal_regex_metacharacters(self):
        self.assertEqual(render("F.({YYYY})*{seq:2}+", 2026, 3), "F.(2026)*03+")

    def test_render_refuses_bad_seq_and_year(self):
        for seq in (0, -1, True, 1.0, "1"):
            with self.assertRaises(ValidationError, msg=repr(seq)):
                render(P, 2026, seq)
        for year in (999, 10000, True, "2026"):
            with self.assertRaises(ValidationError, msg=repr(year)):
                render(P, year, 1)


class TestPattern(unittest.TestCase):
    def test_pattern_needs_both_tokens(self):
        for bad in ("INV-{seq:4}", "INV-{YYYY}", "INV", "",
                    "{YYYY}-{YYYY}-{seq:4}", "{YYYY}-{seq:4}-{seq:4}",
                    "{YYYY}-{seq:0}", "{YYYY}-{seq:10}", "{YYYY}-{seq}",
                    "{YYYY}-{seq:x}", "{yyyy}-{seq:4}", "{YYYY}-{seq: 4}",
                    "{YYYY}-{seq:4}-{MM}", "{YYYY}-{seq:4}}", "{{YYYY}-{seq:4}",
                    "{YYYY}-{seq:٤}"):
            with self.assertRaises(ValidationError, msg=bad):
                validate_pattern(bad)
            with self.assertRaises(ValidationError, msg=bad):
                render(bad, 2026, 1)
            with self.assertRaises(ValidationError, msg=bad):
                parse_seq(bad, "INV-2026-0001", 2026)
            with self.assertRaises(ValidationError, msg=bad):
                next_number(bad, 2026, [], NO_FLOOR)

    def test_valid_patterns(self):
        for good in (P, "{seq:1}{YYYY}", "{YYYY}{seq:9}", "A.B_C {YYYY}-{seq:2}"):
            validate_pattern(good)  # no exception

    def test_pattern_literals_must_be_file_name_safe(self):
        # The number names the invoice's files: "/" would nest a directory.
        for bad in ("{YYYY}/{seq:3}", "INV\\{YYYY}-{seq:4}", "INV:{YYYY}-{seq:4}",
                    "A.B*C[{YYYY}]({seq:2})$", "F{YYYY}#{seq:3}", "Ä{YYYY}-{seq:4}",
                    ".INV-{YYYY}-{seq:4}", ".{YYYY}-{seq:4}"):
            with self.assertRaises(ValidationError, msg=bad):
                validate_pattern(bad)
        with self.assertRaisesRegex(ValidationError, "'-' instead of '/'"):
            validate_pattern("{YYYY}/{seq:3}")

    def test_pattern_must_be_text(self):
        with self.assertRaises(ValidationError):
            validate_pattern(None)


class TestParse(unittest.TestCase):
    def test_parse_roundtrip(self):
        for seq in (1, 9, 10, 999, 9999, 10000, 123456):
            self.assertEqual(parse_seq(P, render(P, 2026, seq), 2026), seq)

    def test_parse_ignores_other_years_and_foreign_numbers(self):
        for foreign in ("INV-2025-0012", "INV-2027-0012", "INV-2026-012",
                        "INV-2026-", "INV-2026-00a2", "inv-2026-0012",
                        "INV-2026-0012 ", " INV-2026-0012", "INV-2026-0012x",
                        "XINV-2026-0012", "INV_2026-0012", "INV-2026-+012",
                        "INV-2026--012", "INV-2026-١٢٣٤", "INV-2026-¹²³⁴", ""):
            self.assertIsNone(parse_seq(P, foreign, 2026), foreign)

    def test_parse_literal_text_is_literal(self):
        pat = "F.({YYYY})*{seq:2}+"
        self.assertEqual(parse_seq(pat, "F.(2026)*03+", 2026), 3)
        for near in ("Fx(2026)*03+", "F.(2026)03+", "F.(2026)*03", "F.2026*03+",
                     "F.(2026)**03+"):
            self.assertIsNone(parse_seq(pat, near, 2026), near)

    def test_parse_extra_padding_still_counts(self):
        # A wider zero-padded number cannot clash with a rendered one, but it was
        # issued: count it so the next number goes past it, never below.
        self.assertEqual(parse_seq(P, "INV-2026-00050", 2026), 50)

    def test_parse_adjacent_digits(self):
        self.assertEqual(parse_seq("{YYYY}{seq:2}", "202612", 2026), 12)
        self.assertEqual(parse_seq("{YYYY}{seq:2}", "2026123", 2026), 123)
        self.assertEqual(parse_seq("{seq:2}{YYYY}", "1232026", 2026), 123)
        self.assertIsNone(parse_seq("{YYYY}{seq:2}", "20261", 2026))
        self.assertIsNone(parse_seq("{YYYY}{seq:2}", "202512", 2026))
        # Digit literal after the seq: the suffix must match, not just be digits.
        self.assertEqual(parse_seq("{YYYY}{seq:2}9", "2026129", 2026), 12)
        self.assertIsNone(parse_seq("{YYYY}{seq:2}9", "2026128", 2026))
        self.assertIsNone(parse_seq("9{seq:2}{YYYY}", "8122026", 2026))


class TestNext(unittest.TestCase):
    def test_first_number(self):
        self.assertEqual(next_number(P, 2026, [], NO_FLOOR), ("INV-2026-0001", 1))

    def test_next_after_existing(self):
        existing = ["INV-2026-0010", "INV-2026-0009", "INV-2026-0011"]
        self.assertEqual(next_number(P, 2026, existing, NO_FLOOR), ("INV-2026-0012", 12))

    def test_next_is_max_plus_one_not_gap_fill(self):
        existing = ["INV-2026-0001", "INV-2026-0007"]
        self.assertEqual(next_number(P, 2026, existing, NO_FLOOR), ("INV-2026-0008", 8))

    def test_floor_raises_next(self):
        floor = {"next_seq": 42, "seq_year": 2026}
        self.assertEqual(next_number(P, 2026, [], floor), ("INV-2026-0042", 42))

    def test_floor_never_lowers(self):
        existing = ["INV-2026-%04d" % i for i in range(1, 51)]
        floor = {"next_seq": 42, "seq_year": 2026}
        self.assertEqual(next_number(P, 2026, existing, floor), ("INV-2026-0051", 51))

    def test_floor_equal_to_next(self):
        floor = {"next_seq": 12, "seq_year": 2026}
        existing = ["INV-2026-0011"]
        self.assertEqual(next_number(P, 2026, existing, floor), ("INV-2026-0012", 12))

    def test_floor_for_other_year_ignored(self):
        for seq_year in (2025, 2027, None):
            floor = {"next_seq": 42, "seq_year": seq_year}
            self.assertEqual(next_number(P, 2026, [], floor), ("INV-2026-0001", 1))
            self.assertEqual(next_number(P, 2026, ["INV-2026-0003"], floor),
                             ("INV-2026-0004", 4))

    def test_year_rollover_resets(self):
        existing = ["INV-2026-%04d" % i for i in range(1, 12)]
        floor = {"next_seq": 12, "seq_year": 2026}
        self.assertEqual(next_number(P, 2027, existing, floor), ("INV-2027-0001", 1))

    def test_foreign_numbers_ignored(self):
        existing = ["LEGACY-77", "INV-2025-0900", "INV-2026-0004", "INV-2026-abcd"]
        self.assertEqual(next_number(P, 2026, existing, NO_FLOOR), ("INV-2026-0005", 5))

    def test_result_not_in_existing(self):
        # The invariant that matters: the number handed out is never one already used.
        existing = ["INV-2026-0003", "INV-2026-00004", "INV-2026-0002"]
        for floor in (NO_FLOOR, {"next_seq": 3, "seq_year": 2026},
                      {"next_seq": 4, "seq_year": 2026}):
            number, seq = next_number(P, 2026, existing, floor)
            self.assertNotIn(number, existing)
            self.assertEqual(seq, 5)

    def test_existing_must_be_strings(self):
        with self.assertRaises(TypeError):
            next_number(P, 2026, ["INV-2026-0001", None], NO_FLOOR)


if __name__ == "__main__":
    unittest.main()
