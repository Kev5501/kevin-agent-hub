from __future__ import annotations

import pytest

from core.connectors.base import ConnectorError
from core.connectors.gsheets import GoogleSheetsConnector, build_batch, col_letter
from core.models import Update


def test_col_letter():
    assert [col_letter(i) for i in (1, 26, 27, 52, 53, 702, 703)] == [
        "A",
        "Z",
        "AA",
        "AZ",
        "BA",
        "ZZ",
        "AAA",
    ]


def test_build_batch_merges_consecutive_rows_per_column():
    ups = [Update(r, {"M": 1, "N": "NA"}) for r in (2, 3, 4)] + [Update(9, {"M": 0})]
    data = build_batch({"M": 13, "N": 14}, ups)
    assert {"range": "M2:M4", "values": [[1], [1], [1]]} in data
    assert {"range": "N2:N4", "values": [["NA"], ["NA"], ["NA"]]} in data
    assert {"range": "M9", "values": [[0]]} in data
    assert len(data) == 3


def test_build_batch_unknown_column():
    with pytest.raises(ConnectorError):
        build_batch({"A": 1}, [Update(2, {"B": 1})])


class FakeWorksheet:
    def __init__(self, grid):
        self.grid = grid
        self.col_count = max(len(r) for r in grid)
        self.batches = []

    def get_all_values(self):
        return [list(r) for r in self.grid]

    def row_values(self, n):
        row = self.grid[n - 1]
        while row and row[-1] == "":
            row = row[:-1]
        return list(row)

    def add_cols(self, n):
        self.col_count += n

    def batch_update(self, data, value_input_option=None):
        self.batches.append(data)


class FakeSpreadsheet:
    def __init__(self, ws):
        self.ws = ws

    def worksheet(self, title):
        return self.ws


def make_connector(grid):
    ws = FakeWorksheet(grid)
    conn = GoogleSheetsConnector("id", "{}", spreadsheet=FakeSpreadsheet(ws))
    return conn, ws


def test_read_pads_short_rows_skips_blank_rows_and_numbers_rows():
    conn, _ = make_connector(
        [["Date", "Comment", "flag"], ["d1", "hi"], ["", "", ""], ["d2", "yo", "1"]]
    )
    table = conn.read_table("T")
    assert table.columns == ["Date", "Comment", "flag"]
    assert [r.locator for r in table.records] == [2, 4]
    assert table.records[0].fields["flag"] == ""


def test_write_uses_sheet_row_numbers_and_header_positions():
    conn, ws = make_connector([["Date", "Comment", "flag"], ["d1", "hi", ""], ["d1", "hi", ""]])
    conn.read_table("T")
    n = conn.write_updates("T", [Update(2, {"flag": 1}), Update(3, {"flag": 1})])
    assert n == 2
    assert ws.batches == [[{"range": "C2:C3", "values": [[1], [1]]}]]


def test_write_never_overwrites_cells_that_were_filled_at_read_time():
    conn, ws = make_connector([["Date", "flag"], ["d1", "0"], ["d1", ""]])
    conn.read_table("T")
    n = conn.write_updates("T", [Update(2, {"flag": 1}), Update(3, {"flag": 1})])
    assert n == 1
    assert ws.batches == [[{"range": "B3", "values": [[1]]}]]


def test_ensure_fields_appends_headers_and_grows_grid():
    conn, ws = make_connector([["Date", "Comment"], ["d1", "hi"]])
    created = conn.ensure_fields("T", ["Comment", "new_a", "new_b"])
    assert created == ["new_a", "new_b"]
    assert ws.batches == [
        [
            {"range": "C1", "values": [["new_a"]]},
            {"range": "D1", "values": [["new_b"]]},
        ]
    ]
    assert ws.col_count == 4
    assert conn.ensure_fields("T", []) == []
