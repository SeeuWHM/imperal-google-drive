"""compute_aggregate is pure (no ctx) — the one piece of doc-reader worth a
real pytest suite, since it's exact arithmetic the whole point is to never
get wrong (see providers/spreadsheet_math.py docstring)."""
from __future__ import annotations

import pytest

from providers.spreadsheet_math import compute_aggregate


def test_sum():
    result, count = compute_aggregate([["1", "2"], ["3"]], "sum")
    assert result == 6
    assert count == 3


def test_average():
    result, count = compute_aggregate([["10"], ["20"], ["30"]], "average")
    assert result == 20
    assert count == 3


def test_min_max():
    values = [["5", "-3"], ["100"]]
    assert compute_aggregate(values, "min")[0] == -3
    assert compute_aggregate(values, "max")[0] == 100


def test_count_counts_nonempty_including_non_numeric():
    values = [["apple", "banana", ""], ["3", None]]
    result, count = compute_aggregate(values, "count")
    assert result == 3  # "apple", "banana", "3" — empty string and None excluded
    assert count == 3


def test_ignores_non_numeric_cells_for_sum():
    result, count = compute_aggregate([["1", "n/a", "2"]], "sum")
    assert result == 3
    assert count == 2


def test_handles_comma_thousands_separator():
    result, _ = compute_aggregate([["1,000", "2,500"]], "sum")
    assert result == 3500


def test_empty_range_raises_for_numeric_ops():
    with pytest.raises(RuntimeError):
        compute_aggregate([["", None], ["n/a"]], "sum")


def test_unknown_operation_raises():
    with pytest.raises(ValueError):
        compute_aggregate([["1"]], "median")
