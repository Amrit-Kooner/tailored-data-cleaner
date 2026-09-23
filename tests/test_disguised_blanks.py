"""
Tests for disguised blanks: placeholder text like N/A, TBD, unknown and '-'.

They are NOT removed or turned into blanks any more. The cleaner keeps them
and flags each cell holding one PURPLE (masks.build_disguised_mask).
A genuinely empty cell is still ORANGE, and a placeholder is never counted as a failed
conversion (RED).

Run from the project root:
    python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openpyxl                                   # noqa: E402
import pandas as pd                               # noqa: E402

import config                                     # noqa: E402
from uni_cleaner.output.excel import save_as_formatted_excel   # noqa: E402
from uni_cleaner.pipeline import clean_file       # noqa: E402

PURPLE, RED, ORANGE, BLUE = "D5B3FF", "FFB3B3", "FFD9A6", "A6C8FF"


def run(csv_text):
    path = os.path.join(tempfile.mkdtemp(), "t.csv")
    with open(path, "w", encoding="utf-8") as f:
        f.write(csv_text)
    return clean_file(path)


def workbook(res):
    path = os.path.join(tempfile.mkdtemp(), "out.xlsx")
    save_as_formatted_excel(res, path, "t.csv")
    return openpyxl.load_workbook(path)


def cleaned_sheet(res):
    """The Cleaned sheet -- always plain, no fills, full row set in original order."""
    return workbook(res)["Cleaned"]


def full_fills(res, col_name):
    """
    Colour-coded results now live on separate per-colour sheets, each holding
    only the flagged rows plus a leading "Cleaned Row #" column. This
    reconstructs a fills-per-row list the same shape as the old single-sheet
    version (None for a row not flagged on any of the four sheets), by
    walking every flagged sheet and placing each fill at its Cleaned-sheet
    row position.
    """
    wb = workbook(res)
    n_rows = len(res.df)
    out = [None] * n_rows
    for sheet_name in (
        "Failed Values", "Missing Values", "ID Duplicates",
        "ID Anomalies", "Price Anomalies", "Disguised Blanks",
    ):
        if sheet_name not in wb.sheetnames:
            continue
        ws = wb[sheet_name]
        headers = [c.value for c in ws[1]]
        if col_name not in headers:
            continue
        col_idx = headers.index(col_name) + 1
        for r in range(2, ws.max_row + 1):
            cleaned_row = ws.cell(r, 1).value
            if not isinstance(cleaned_row, int):
                continue
            c = ws.cell(r, col_idx)
            fill = c.fill.start_color.rgb[-6:] if c.fill.fill_type else None
            if fill:
                out[cleaned_row - 2] = fill
    return out


def values(ws, col_name):
    idx = [c.value for c in ws[1]].index(col_name) + 1
    return [ws.cell(row=r, column=idx).value for r in range(2, ws.max_row + 1)]


class PlaceholdersAreKeptAndPurple(unittest.TestCase):
    CSV = "id,note\n1,ok\n2,N/A\n3,TBD\n4,unknown\n5,-\n6,\n7,fine\n"

    def test_every_placeholder_is_kept_not_blanked(self):
        res = run(self.CSV)
        self.assertEqual(res.df["note"].tolist()[:5], ["ok", "N/A", "TBD", "unknown", "-"])
        self.assertEqual(res.df["note"].iloc[6], "fine")
        self.assertTrue(pd.isna(res.df["note"].iloc[5]))                 # the truly empty cell stays empty

    def test_placeholder_cells_are_purple_and_the_real_blank_is_orange(self):
        res = run(self.CSV)
        self.assertEqual(full_fills(res, "note"), [None, PURPLE, PURPLE, PURPLE, PURPLE, ORANGE, None])
        self.assertEqual(values(cleaned_sheet(res), "note")[1:5], ["N/A", "TBD", "unknown", "-"])

    def test_only_the_placeholder_cells_are_in_the_purple_mask(self):
        res = run(self.CSV)
        self.assertEqual(res.disguised_mask["note"].tolist(), [False, True, True, True, True, False, False])
        self.assertEqual(int(res.disguised_mask["id"].sum()), 0)
        self.assertEqual(res.summary.count("PURPLE") > 0, True)

    def test_matching_ignores_case_and_surrounding_spaces(self):
        res = run("id,note\n1, n/a \n2,Tbd\n3,UNKNOWN\n4,real\n")
        self.assertEqual(res.disguised_mask["note"].tolist(), [True, True, True, False])


class PandasNoLongerBlanksThemAtLoad(unittest.TestCase):
    """pandas itself reads N/A, NA, n/a, NULL, None as empty before the cleaner sees them."""

    def test_the_common_pandas_placeholders_survive_a_csv(self):
        res = run("id,note\n1,N/A\n2,NA\n3,n/a\n4,NULL\n5,None\n6,real\n")
        self.assertEqual(res.df["note"].tolist(), ["N/A", "NA", "n/a", "NULL", "None", "real"])
        self.assertEqual(int(res.disguised_mask["note"].sum()), 5)
        self.assertEqual(int(res.missing_mask["note"].sum()), 0)

    def test_the_common_pandas_placeholders_survive_an_excel_file(self):
        path = os.path.join(tempfile.mkdtemp(), "t.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["id", "note"])
        for i, v in enumerate(["N/A", "NA", "n/a", "NULL", "None", "real"], start=1):
            ws.append([i, v])
        wb.save(path)
        res = clean_file(path)
        self.assertEqual(res.df["note"].tolist(), ["N/A", "NA", "n/a", "NULL", "None", "real"])
        self.assertEqual(int(res.disguised_mask["note"].sum()), 5)

    def test_other_pandas_blank_words_are_still_read_as_blank(self):
        res = run("id,note\n1,#N/A\n2,nan\n3,real\n")
        self.assertTrue(pd.isna(res.df["note"].iloc[0]) and pd.isna(res.df["note"].iloc[1]))
        self.assertEqual(int(res.disguised_mask["note"].sum()), 0)

    def test_the_list_in_config_decides_what_counts(self):
        with mock.patch.object(config, "DISGUISED_BLANKS", ["xx"]):
            res = run("id,note\n1,xx\n2,TBD\n3,N/A\n4,real\n")
        self.assertEqual(res.disguised_mask["note"].tolist(), [True, False, False, False])
        self.assertEqual(res.df["note"].iloc[1], "TBD")                   # not on the list: just ordinary text
        self.assertTrue(pd.isna(res.df["note"].iloc[2]))                  # N/A is off the list: pandas blanks it as before


class PlaceholdersAreNeverFailedConversions(unittest.TestCase):
    def test_numeric_column_keeps_its_placeholders_and_converts_the_rest(self):
        res = run("id,quantity\n1,5\n2,N/A\n3,7\n4,TBD\n")
        self.assertEqual(res.df["quantity"].iloc[0], 5)
        self.assertEqual(res.df["quantity"].iloc[2], 7)
        self.assertEqual(res.df["quantity"].iloc[1], "N/A")
        self.assertEqual(res.df["quantity"].iloc[3], "TBD")
        self.assertEqual(int(res.fail_mask["quantity"].sum()), 0)         # not RED
        self.assertEqual(full_fills(res, "quantity"), [None, PURPLE, None, PURPLE])

    def test_a_price_column_mostly_placeholders_is_still_converted(self):
        res = run("id,unit_price\n1,5.00\n2,TBD\n3,TBD\n4,TBD\n")
        self.assertIn("unit_price", res.money_cols)                        # placeholders don't count against it
        self.assertEqual(res.df["unit_price"].tolist(), [5.0, "TBD", "TBD", "TBD"])

    def test_a_date_column_keeps_its_placeholders_and_they_are_purple_not_red(self):
        res = run("id,start_date\n1,2026-04-01\n2,TBD\n3,05/03/2026\n4,2026-02-03\n")
        self.assertEqual(res.df["start_date"].tolist(), ["01/04/2026", "TBD", "05/03/2026", "03/02/2026"])
        self.assertEqual(int(res.fail_mask["start_date"].sum()), 0)
        self.assertEqual(full_fills(res, "start_date"), [None, PURPLE, None, None])

    def test_a_real_conversion_failure_is_still_red(self):
        res = run("id,quantity\n1,5\n2,N/A\n3,abc\n4,7\n")
        self.assertEqual(res.fail_mask["quantity"].tolist(), [False, False, True, False])
        self.assertEqual(full_fills(res, "quantity"), [None, PURPLE, RED, None])

    def test_a_column_of_nothing_but_placeholders_is_left_alone(self):
        res = run("id,note\n1,N/A\n2,N/A\n")
        self.assertEqual(res.df["note"].tolist(), ["N/A", "N/A"])
        self.assertEqual(int(res.disguised_mask["note"].sum()), 2)


class PlaceholdersNeverCauseRowLoss(unittest.TestCase):
    def test_rows_with_a_placeholder_app_number_are_not_merged(self):
        res = run("app,description\nN/A,aa\nN/A,bbbb\nTBD,cc\nTBD,dddd\nA1,eee\n")
        self.assertEqual(len(res.df), 5)
        self.assertEqual(res.df["app"].tolist(), ["N/A", "N/A", "TBD", "TBD", "A1"])

    def test_a_placeholder_description_does_not_win_the_longest_description(self):
        res = run("app,description\nA1,not available at all\nA1,real\n")
        self.assertEqual(res.df["description"].tolist(), ["not available at all"])   # ordinary text still compared
        res = run("app,description\nA1,unknown\nA1,ok\n")
        self.assertEqual(res.df["description"].tolist(), ["ok"])                       # 'unknown' counts as no description

    def test_placeholder_id_cells_show_lavender_only_not_purple_or_green(self):
        """
        A placeholder APP value (TBD) is flagged as invalid on its own
        LAVENDER sheet. It is excluded from the global PURPLE disguised-blank
        scan (that's the APP sheet's job now) and is never treated as a
        duplicate (PINK/green), since two placeholder cells don't count as
        "the same real value".
        """
        res = run("app,description\nTBD,aa\nTBD,bbbb\nA1,eee\nA2,ff\n")
        self.assertEqual(int(res.app_duplicate_mask["app"].sum()), 0)
        self.assertEqual(int(res.app_invalid_mask["app"].sum()), 2)
        self.assertEqual(int(res.disguised_mask["app"].sum()), 0)


class ReportAndLog(unittest.TestCase):
    CSV = "id,note\n1,N/A\n2,TBD\n3,real\n"

    def test_the_report_says_kept_and_purple_not_converted(self):
        res = run(self.CSV)
        self.assertIn("PURPLE = a disguised blank", res.summary)
        self.assertIn("Disguised blanks (kept, flagged PURPLE):  2", res.summary)
        self.assertIn("2 cell(s) highlighted PURPLE", res.summary)
        self.assertNotIn("converted to blank", res.summary)
        self.assertNotIn("Total empty cells", res.summary)

    def test_the_audit_log_names_the_columns(self):
        res = run(self.CSV)
        entry = [e["detail"] for e in res.log["steps"] if e["step"] == "flag_disguised_blanks"]
        self.assertEqual(len(entry), 1)
        self.assertIn("2 disguised blank cell(s)", entry[0])
        self.assertIn("KEPT (not removed or blanked)", entry[0])
        self.assertIn("'note': 2", entry[0])
        self.assertFalse(any(e["step"] == "convert_disguised_blanks" for e in res.log["steps"]))

    def test_the_stats_carry_the_count(self):
        res = run(self.CSV)
        self.assertEqual(res.disguised_mask.sum().sum(), 2)

    def test_no_placeholders_means_no_purple_and_a_clear_log_line(self):
        res = run("id,note\n1,a\n2,b\n")
        self.assertEqual(int(res.disguised_mask.sum().sum()), 0)
        entry = [e["detail"] for e in res.log["steps"] if e["step"] == "flag_disguised_blanks"]
        self.assertIn("0 disguised blank cell(s)", entry[0])
        self.assertIn("Disguised blanks (kept, flagged PURPLE):  0", res.summary)


if __name__ == "__main__":
    unittest.main()
