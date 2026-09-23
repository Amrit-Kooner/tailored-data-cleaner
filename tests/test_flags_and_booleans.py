"""
Tests for two project rules:

  1. 0, FALSE and a blank are three DIFFERENT values -- in Excel files as well as
     CSV files. (pandas used to turn an Excel FALSE into 0.0 when the column also
     held blanks or numbers, so FALSE was flagged BLUE as a "zero price" and could
     be reported as a duplicate of a column of zeros.)
  2. Every flag column is standardised to Y / N: TRUE/FALSE, yes/no, y/n, t/f,
     1/0 ... in any casing, whatever the column is called. Fields that are not
     flags are left alone.

Run from the project root:
    python -m unittest discover -s tests -v
"""

import csv
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openpyxl                                   # noqa: E402
import pandas as pd                               # noqa: E402

from uni_cleaner.pipeline import clean_file       # noqa: E402


def _tmp(name):
    return os.path.join(tempfile.mkdtemp(), name)


def clean_xlsx(header, rows):
    """Cleans an Excel file built from python values (True/False/None/numbers are REAL cell types)."""
    path = _tmp("t.xlsx")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(header)
    for r in rows:
        ws.append(list(r))
    wb.save(path)
    return clean_file(path)


def clean_csv(header, rows):
    path = _tmp("t.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return clean_file(path)


def cells(mask, col):
    """Row numbers (0-based) flagged in `mask` for column `col`."""
    return [int(i) for i in mask.index[mask[col]]]


def dupe_pairs(res):
    out = set()
    for a, others in res.duplicate_name_map.items():
        for b in others:
            out.add(frozenset((a, b)))
    return out


def vals(res, col):
    return [None if pd.isna(v) else v for v in res.df[col].tolist()]


class FalseIsNotZeroInExcel(unittest.TestCase):
    def test_false_in_a_price_column_stays_false_and_is_never_blue(self):
        res = clean_xlsx(["id", "unit_price"], [(1, 12.5), (2, 0), (3, None), (4, False), (5, 3)])
        col = res.df["unit_price"].tolist()
        self.assertIs(col[3], False)                       # kept as the boolean FALSE, not 0.0
        self.assertEqual(col[1], 0)                        # the genuine zero is still 0
        self.assertTrue(pd.isna(col[2]))                   # the blank is still blank
        self.assertEqual(cells(res.review_mask, "unit_price"), [1])   # BLUE: only the real zero price
        self.assertEqual(cells(res.fail_mask, "unit_price"), [3])     # RED: the FALSE (not a price, kept)

    def test_true_false_blank_column_with_a_price_like_name_becomes_y_n_and_is_not_blue(self):
        for name in ("surcharge", "amount_paid", "fee_waived", "cost_included"):
            with self.subTest(column=name):
                res = clean_xlsx(["id", name], [(1, True), (2, False), (3, None), (4, False)])
                self.assertEqual(vals(res, name), ["Y", "N", None, "N"])
                self.assertEqual(cells(res.review_mask, name), [])
                self.assertEqual(cells(res.fail_mask, name), [])

    def test_a_plain_flag_column_with_a_blank_becomes_y_n_not_1_0(self):
        res = clean_xlsx(["id", "is_paid", "approved"],
                         [(1, True, True), (2, False, False), (3, None, None), (4, True, True)])
        self.assertEqual(vals(res, "is_paid"), ["Y", "N", None, "Y"])
        self.assertEqual(vals(res, "approved"), ["Y", "N", None, "Y"])

    def test_csv_and_excel_treat_a_false_in_a_price_column_the_same_way(self):
        res = clean_csv(["id", "unit_price"], [(1, "12.5"), (2, "0"), (3, ""), (4, "FALSE"), (5, "3")])
        self.assertEqual(cells(res.review_mask, "unit_price"), [1])
        self.assertEqual(cells(res.fail_mask, "unit_price"), [3])
        self.assertEqual(res.df["unit_price"].tolist()[3], "FALSE")   # not turned into 0


class ZeroBlankAndFalseAreNeverDuplicates(unittest.TestCase):
    def test_all_zero_column_is_not_a_duplicate_of_an_all_blank_column(self):
        for build in (clean_xlsx, clean_csv):
            with self.subTest(source=build.__name__):
                blank = None if build is clean_xlsx else ""
                res = build(["row", "zeros", "blanks"], [(n, 0, blank) for n in range(1, 5)])
                self.assertEqual(dupe_pairs(res), set())

    def test_zero_blank_mixes_that_differ_are_not_duplicates(self):
        blank = None
        rows = [(n, 0 if n % 2 else blank, blank if n % 2 else 0) for n in range(1, 6)]
        res = clean_xlsx(["row", "zero_then_blank", "blank_then_zero"], rows)
        self.assertEqual(dupe_pairs(res), set())

    def test_a_false_column_is_not_a_duplicate_of_a_zero_column_even_with_blanks(self):
        rows = [(1, False, 0), (2, False, 0), (3, None, None), (4, False, 0)]
        res = clean_xlsx(["row", "was_false", "was_zero"], rows)
        self.assertEqual(dupe_pairs(res), set())

    def test_genuinely_identical_columns_are_still_flagged(self):
        res = clean_xlsx(["row", "zeros", "zeros_b"], [(n, 0, 0) for n in range(1, 5)])
        self.assertEqual(dupe_pairs(res), {frozenset(("zeros", "zeros_b"))})


class FlagsAreStandardisedToYN(unittest.TestCase):
    def test_every_spelling_of_yes_and_no_in_any_case(self):
        res = clean_csv(["id", "a", "b", "c", "d"], [
            (1, "TRUE", "Yes", "y", "T"),
            (2, "false", "NO", "N", "f"),
            (3, "True", "yes", "Y", "t"),
            (4, "FALSE", "No", "n", "F"),
        ])
        for col in "abcd":
            self.assertEqual(vals(res, col), ["Y", "N", "Y", "N"], col)

    def test_yes_no_true_false_1_0_can_all_be_mixed_in_one_column(self):
        res = clean_csv(["id", "answer"], [(1, "yes"), (2, "FALSE"), (3, "N"), (4, "0"), (5, "1"), (6, "")])
        self.assertEqual(vals(res, "answer"), ["Y", "N", "N", "N", "Y", None])   # blank stays blank

    def test_one_and_zero_with_a_blank_cell_are_converted_when_the_name_says_flag(self):
        res = clean_xlsx(["id", "is_active", "paid_flag"], [(1, 1, 1), (2, 0, 0), (3, 1, None), (4, None, 1)])
        self.assertEqual(vals(res, "is_active"), ["Y", "N", "Y", None])
        self.assertEqual(vals(res, "paid_flag"), ["Y", "N", None, "Y"])

    def test_plain_numbers_that_happen_to_be_one_and_zero_are_not_flags(self):
        res = clean_csv(["id", "orders"], [(1, "1"), (2, "0"), (3, "1")])
        self.assertNotIn("Y", vals(res, "orders"))

    def test_flag_columns_with_an_id_word_in_the_name_are_still_flags(self):
        cols = ["app_approved", "bpa_active", "id_verified", "has_code"]
        rows = [(1, "yes", "TRUE", "y", "Yes"), (2, "no", "FALSE", "n", "No"), (3, "YES", "true", "Y", "yes")]
        res = clean_csv(["row"] + cols, rows)
        for col in cols:
            self.assertEqual(vals(res, col), ["Y", "N", "Y"], col)

    def test_a_column_that_is_all_one_side_is_still_converted(self):
        res = clean_csv(["id", "all_yes", "all_false", "all_true"],
                        [(1, "Yes", "FALSE", "TRUE"), (2, "yes", "False", "true"), (3, "YES", "false", "True")])
        self.assertEqual(vals(res, "all_yes"), ["Y", "Y", "Y"])
        self.assertEqual(vals(res, "all_false"), ["N", "N", "N"])
        self.assertEqual(vals(res, "all_true"), ["Y", "Y", "Y"])

    def test_a_stray_value_is_kept_and_flagged_red_while_the_rest_are_converted(self):
        res = clean_csv(["id", "reviewed"], [(1, "yes"), (2, "no"), (3, "maybe"), (4, "Yes"), (5, "NO")])
        self.assertEqual(vals(res, "reviewed"), ["Y", "N", "maybe", "Y", "N"])
        self.assertEqual(cells(res.fail_mask, "reviewed"), [2])

    def test_a_free_text_column_with_the_odd_yes_is_not_a_flag(self):
        res = clean_csv(["id", "comment"], [(1, "yes"), (2, "no"), (3, "call back next week"),
                                            (4, "waiting on the supplier"), (5, "see attached")])
        self.assertEqual(vals(res, "comment")[2], "call back next week")
        self.assertNotIn("Y", vals(res, "comment"))

    def test_a_status_column_of_active_inactive_is_not_turned_into_y_n(self):
        res = clean_csv(["id", "status"], [(1, "Active"), (2, "Inactive"), (3, "active"), (4, "INACTIVE")])
        self.assertEqual(vals(res, "status"), ["Active", "Inactive", "Active", "Inactive"])

    def test_an_ordinary_text_column_is_left_alone(self):
        res = clean_csv(["id", "colour"], [(1, "red"), (2, "blue"), (3, "red")])
        self.assertEqual(vals(res, "colour"), ["red", "blue", "red"])


if __name__ == "__main__":
    unittest.main()
