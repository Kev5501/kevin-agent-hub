"""Google Sheets connector (service-account auth, via gspread).

Layout assumptions: row `header_row` (default 1) holds column names; every row below is a
record; the record locator is the 1-based sheet row number.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from core.connectors.base import ConnectorError, TableConnector, register
from core.models import Record, Table, Update, is_blank

_MAX_RANGES_PER_CALL = 200


def col_letter(n: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA ..."""
    if n < 1:
        raise ValueError("column index must be >= 1")
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def build_batch(col_index: dict[str, int], updates: Sequence[Update]) -> list[dict[str, Any]]:
    """Convert per-row updates into the fewest A1 ranges: consecutive rows in the same column
    are merged into one range (a workout's rows are normally contiguous)."""
    cells: dict[str, dict[int, Any]] = {}
    for upd in updates:
        for col, value in upd.values.items():
            if col not in col_index:
                raise ConnectorError(f"Column '{col}' not found in the sheet header")
            cells.setdefault(col, {})[int(upd.locator)] = value

    data: list[dict[str, Any]] = []
    for col, by_row in cells.items():
        letter = col_letter(col_index[col])
        rows = sorted(by_row)
        start = prev = rows[0]
        for row in [*rows[1:], None]:
            if row is not None and row == prev + 1:
                prev = row
                continue
            if start == prev:
                data.append({"range": f"{letter}{start}", "values": [[by_row[start]]]})
            else:
                data.append(
                    {
                        "range": f"{letter}{start}:{letter}{prev}",
                        "values": [[by_row[r]] for r in range(start, prev + 1)],
                    }
                )
            if row is not None:
                start = prev = row
    return data


@register("gsheets")
class GoogleSheetsConnector(TableConnector):
    def __init__(
        self,
        spreadsheet_id: str,
        credentials_json: str,
        header_row: int = 1,
        spreadsheet: Any = None,
    ) -> None:
        super().__init__()
        self._spreadsheet_id = spreadsheet_id
        self._credentials_json = credentials_json
        self.header_row = header_row
        self._spreadsheet = spreadsheet  # injectable for tests
        self._worksheets: dict[str, Any] = {}
        self._col_index: dict[str, dict[str, int]] = {}

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> GoogleSheetsConnector:
        for key in ("spreadsheet_id", "credentials"):
            if not params.get(key):
                raise ConnectorError(f"gsheets connection needs '{key}_env' in connections.yaml")
        return cls(
            spreadsheet_id=params["spreadsheet_id"],
            credentials_json=params["credentials"],
            header_row=int(params.get("header_row", 1)),
        )

    # -- plumbing ---------------------------------------------------------------------------
    def _open(self) -> Any:
        if self._spreadsheet is None:
            import gspread

            try:
                info = json.loads(self._credentials_json)
            except ValueError as exc:
                raise ConnectorError(
                    "Google credentials secret is not valid JSON (paste the whole key file)"
                ) from exc
            client = gspread.service_account_from_dict(info)
            self._spreadsheet = client.open_by_key(self._spreadsheet_id)
        return self._spreadsheet

    def _ws(self, table: str) -> Any:
        if table not in self._worksheets:
            self._worksheets[table] = self._open().worksheet(table)
        return self._worksheets[table]

    # -- connector API ----------------------------------------------------------------------
    def _read(self, table: str) -> Table:
        values = self._ws(table).get_all_values()
        if len(values) < self.header_row:
            raise ConnectorError(f"Worksheet '{table}' has no header row")
        header = [str(h).strip() for h in values[self.header_row - 1]]
        col_index: dict[str, int] = {}
        for idx, name in enumerate(header, start=1):
            if name and name not in col_index:
                col_index[name] = idx
        self._col_index[table] = col_index

        records: list[Record] = []
        for row_no, row in enumerate(values[self.header_row :], start=self.header_row + 1):
            cells = list(row) + [""] * (len(header) - len(row))
            if all(is_blank(c) for c in cells):
                continue
            records.append(Record(row_no, {n: cells[i - 1] for n, i in col_index.items()}))
        return Table(columns=list(col_index), records=records)

    def ensure_fields(self, table: str, names: Sequence[str]) -> list[str]:
        ws = self._ws(table)
        header = [str(h).strip() for h in ws.row_values(self.header_row)]
        missing = [n for n in names if n not in header]
        if not missing:
            return []
        first_new_col = len(header) + 1
        last_col = first_new_col + len(missing) - 1
        if last_col > ws.col_count:
            ws.add_cols(last_col - ws.col_count)
        ws.batch_update(
            [
                {"range": f"{col_letter(first_new_col + i)}{self.header_row}", "values": [[n]]}
                for i, n in enumerate(missing)
            ],
            value_input_option="USER_ENTERED",
        )
        self._col_index.pop(table, None)
        return missing

    def _write(self, table: str, updates: Sequence[Update]) -> int:
        if table not in self._col_index:
            self._read(table)  # refresh header mapping
        data = build_batch(self._col_index[table], updates)
        ws = self._ws(table)
        for i in range(0, len(data), _MAX_RANGES_PER_CALL):
            ws.batch_update(data[i : i + _MAX_RANGES_PER_CALL], value_input_option="USER_ENTERED")
        return sum(len(u.values) for u in updates)
