"""Exact spreadsheet computation — sum/count/average/min/max over a 2D
values array. Pure functions, no ctx: the point is that these are computed
by code, not estimated by an LLM squinting at a dumped table."""
from __future__ import annotations

_OPERATIONS = ("sum", "count", "average", "min", "max")


def _flatten_nonempty(values: list[list]) -> list:
    return [cell for row in values for cell in row if cell is not None and cell != ""]


def _flatten_numeric(values: list[list]) -> list[float]:
    nums = []
    for cell in _flatten_nonempty(values):
        try:
            nums.append(float(str(cell).replace(",", "")))
        except ValueError:
            continue
    return nums


def compute_aggregate(values: list[list], operation: str) -> tuple[float, int]:
    """Returns (result, cell_count_used). Raises ValueError/RuntimeError on
    bad operation / no usable data — never silently returns a wrong number."""
    if operation not in _OPERATIONS:
        raise ValueError(f"unknown operation {operation!r}, must be one of {_OPERATIONS}")

    if operation == "count":
        cells = _flatten_nonempty(values)
        return float(len(cells)), len(cells)

    nums = _flatten_numeric(values)
    if not nums:
        raise RuntimeError("no numeric values found in this range")

    if operation == "sum":
        return sum(nums), len(nums)
    if operation == "average":
        return sum(nums) / len(nums), len(nums)
    if operation == "min":
        return min(nums), len(nums)
    return max(nums), len(nums)  # "max"
