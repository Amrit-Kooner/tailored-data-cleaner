"""
Takes a finished, cleaned dataframe and writes it out as a real, properly
formatted .xlsx -- not a CSV, which is plain text and can't carry any
formatting at all (that's why a CSV always shows "General" for every column
when opened in Excel).
"""

from datetime import datetime
import pandas as pd
import openpyxl
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter


def save_as_formatted_excel(df, path, date_cols):
    """
    Numeric columns get a real number format. Date columns are converted
    back into genuine Excel date values (not just dd/mm/yy TEXT that merely
    looks like a date), so sorting, filtering, and date arithmetic actually
    work on them in Excel.
    """
    wb = openpyxl.Workbook()
    ws = wb.active

    for col_idx, col_name in enumerate(df.columns, start=1):
        cell = ws.cell(row=1, column=col_idx, value=col_name)
        cell.font = Font(bold=True)

    for row_idx, row in enumerate(df.itertuples(index=False), start=2):
        for col_idx, (col_name, value) in enumerate(zip(df.columns, row), start=1):
            is_date_col = col_name in date_cols

            if is_date_col and isinstance(value, str) and value.strip():
                # Values here are currently "dd/mm/yy" strings -- parse back
                # into a real date object so Excel treats it as an actual
                # date, not text that merely looks like one.
                try:
                    real_date = datetime.strptime(value, "%d/%m/%y")
                    cell = ws.cell(row=row_idx, column=col_idx, value=real_date)
                    cell.number_format = "DD/MM/YY"
                    continue
                except ValueError:
                    pass  # fall through and write as plain text if parsing fails

            cell = ws.cell(row=row_idx, column=col_idx, value=value)

            if pd.api.types.is_bool_dtype(df[col_name]):
                pass  # leave booleans as True/False, no special number format
            elif pd.api.types.is_numeric_dtype(df[col_name]):
                cell.number_format = "#,##0.00"

    # Roughly auto-size columns based on content width
    for col_idx, col_name in enumerate(df.columns, start=1):
        max_len = max(
            len(str(col_name)),
            df[col_name].astype(str).str.len().max() if len(df) > 0 else 0
        )
        ws.column_dimensions[get_column_letter(col_idx)].width = min(max_len + 2, 40)

    wb.save(path)
