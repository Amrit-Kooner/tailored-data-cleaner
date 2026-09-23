"""
Date regression tests. The bug: year-first dates such as 2026-04-01 were
parsed with dayfirst=True, which swapped day and month whenever the day was
12 or less (1 April came out as 4 January).

Run from the project root:
    python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openpyxl
from datetime import datetime

from uni_cleaner.pipeline import clean_file


def dates(csv_body):
    path = os.path.join(tempfile.mkdtemp(), "d.csv")
    with open(path, "w", encoding="utf-8") as f:
        f.write("id,order_date\n" + csv_body)
    res = clean_file(path)
    return res.df["order_date"].tolist(), int(res.fail_mask["order_date"].sum())


class IsoDatesAreNotSwapped(unittest.TestCase):
    def test_iso_dates_keep_their_day_and_month(self):
        got, red = dates("1,2026-04-01\n2,2022-06-01\n3,2026-12-25\n4,2026-02-03\n")
        self.assertEqual(got, ["01/04/2026", "01/06/2022", "25/12/2026", "03/02/2026"])
        self.assertEqual(red, 0)

    def test_iso_with_time_component(self):
        got, _ = dates("1,2026-04-01 13:45:00\n2,2026-02-03T09:00:00\n")
        self.assertEqual(got, ["01/04/2026", "03/02/2026"])

    def test_year_first_with_slashes(self):
        got, _ = dates("1,2026/04/01\n2,2026/02/03\n")
        self.assertEqual(got, ["01/04/2026", "03/02/2026"])


class BlankDatesDoNotBreakAnything(unittest.TestCase):
    def test_blank_cells_in_a_date_column(self):
        got, red = dates("1,2026-04-01\n2,\n3,05/03/2026\n4,N/A\n5,2026-02-03\n")
        self.assertEqual(got[0], "01/04/2026")
        self.assertEqual(got[2], "05/03/2026")
        self.assertEqual(got[4], "03/02/2026")
        self.assertTrue(got[1] != got[1] or got[1] is None)      # blank stays blank (NaN)
        self.assertEqual(red, 0)

    def test_blank_cells_in_an_excel_date_column_of_text(self):
        path = os.path.join(tempfile.mkdtemp(), "b.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["id", "signup_date"])
        for i, v in enumerate(["2022-06-01", "2023-01-15", None, "Jan 15 2023", "01/15/2023", None], 1):
            ws.append([i, v])
        wb.save(path)
        res = clean_file(path)                                  # must not raise
        self.assertEqual(res.df["signup_date"].iloc[0], "01/06/2022")


class UkDatesStillDayFirst(unittest.TestCase):
    def test_uk_slash_dates(self):
        got, _ = dates("1,05/03/2026\n2,01/02/2026\n3,25/12/2026\n")
        self.assertEqual(got, ["05/03/2026", "01/02/2026", "25/12/2026"])

    def test_mixed_formats_in_one_column(self):
        got, red = dates('1,2026-04-01\n2,05/03/2026\n3,5 March 2026\n4,"March 5, 2026"\n5,2026/04/01\n')
        self.assertEqual(got, ["01/04/2026", "05/03/2026", "05/03/2026", "05/03/2026", "01/04/2026"])
        self.assertEqual(red, 0)

    def test_invalid_date_is_kept_and_flagged_red(self):
        got, red = dates("1,31/02/2026\n2,2026-04-01\n3,05/03/2026\n")
        self.assertEqual(got, ["31/02/2026", "01/04/2026", "05/03/2026"])
        self.assertEqual(red, 1)

    def test_real_excel_date_cells_are_unchanged(self):
        path = os.path.join(tempfile.mkdtemp(), "x.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["id", "order_date"])
        for i, d in enumerate([datetime(2026, 4, 1), datetime(2026, 3, 5), datetime(2026, 12, 25)], 1):
            ws.append([i, d])
        wb.save(path)
        res = clean_file(path)
        self.assertEqual(res.df["order_date"].tolist(), ["01/04/2026", "05/03/2026", "25/12/2026"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
