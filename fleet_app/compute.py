"""Calendar arithmetic for fleet protection periods.

All date ranges are half-open: ``start`` is included, ``end`` is excluded —
the contractual rule "起日计入，止日不计".  Keeping a single convention
throughout avoids the classic off-by-one on the last day.

This module performs no I/O.  The same functions are used both when a
contract is ingested (to build the merged "parts" of a vehicle) and when a
month is extracted (to clip a part to a calendar month).  No routine walks
a multi-year period day by day: month lists and day counts are computed
from date differences.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

DEFAULT_WAITING_DAYS = 30


@dataclass(frozen=True)
class Span:
    """A merged protection part for one vehicle.

    ``waiting_days`` is stored per part because it is a policy snapshot:
    parts are only rebuilt when the set of active contracts changes, so a
    later policy change must not silently rewrite an already published
    month.
    """

    start: date  # inclusive
    end: date  # exclusive
    waiting_days: int

    @property
    def wait_end(self) -> date:
        """First day after the waiting period (also capped at the part end)."""
        return min(self.start + timedelta(days=self.waiting_days), self.end)

    @property
    def effective_start(self) -> date:
        """First day whose days and claims are payable."""
        return self.wait_end


def build_spans(rows, waiting_days: int = DEFAULT_WAITING_DAYS) -> list[Span]:
    """Merge contracts of one vehicle into contiguous parts.

    Two periods that touch (``[a,b)`` and ``[b,c)``) or overlap become one
    part.  An overlap therefore never counts days twice, and the waiting
    period applies once to the merged part rather than once per contract —
    this is what makes a back-dated supplement re-assess the second
    period's waiting-period claims.
    """
    if waiting_days < 0:
        raise ValueError("waiting_days must be >= 0")

    cleaned: list[tuple[date, date]] = []
    for start, end in rows:
        if end <= start:
            continue  # empty or reversed; contributes nothing
        cleaned.append((start, end))
    cleaned.sort()

    merged: list[list[date]] = []
    for start, end in cleaned:
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])

    return [Span(s, e, waiting_days) for s, e in merged]


def month_bounds(month: str) -> tuple[date, date]:
    """Return ``[first day, first day of next month)`` for a YYYY-MM string."""
    year_text, month_text = month.split("-")
    year, mon = int(year_text), int(month_text)
    if not 1 <= mon <= 12:
        raise ValueError(f"非法月份: {month}")
    start = date(year, mon, 1)
    end = date(year + 1, 1, 1) if mon == 12 else date(year, mon + 1, 1)
    return start, end


def months_between(first: date, last: date) -> list[str]:
    """Every calendar month touched by the inclusive date range."""
    if last < first:
        return []
    out = []
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        out.append(f"{year:04d}-{month:02d}")
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
    return out


def span_months(span: Span) -> list[str]:
    """Months touched by a span, without day-by-day expansion."""
    return months_between(span.start, span.end - timedelta(days=1))


def month_covered_by(spans: list[Span]) -> list[str]:
    """Months covered by any part, sorted and de-duplicated."""
    covered: set[str] = set()
    for span in spans:
        covered.update(span_months(span))
    return sorted(covered)


def _clip(start: date, end: date, span: Span) -> int:
    lo = max(start, span.start)
    hi = min(end, span.end)
    return max(0, (hi - lo).days)


def waiting_days_in_month(span: Span, month: str) -> int:
    """Days of the month that fall inside this part's waiting period."""
    month_start, month_end = month_bounds(month)
    lo = max(month_start, span.start)
    hi = min(month_end, span.wait_end, span.end)
    return max(0, (hi - lo).days)


def payable_days_in_month(span: Span, month: str) -> int:
    """Days of the month that are inside the part but after its waiting period."""
    month_start, month_end = month_bounds(month)
    lo = max(month_start, span.start, span.effective_start)
    hi = min(month_end, span.end)
    return max(0, (hi - lo).days)


def payable_days(spans: list[Span], month: str) -> int:
    return sum(payable_days_in_month(span, month) for span in spans)


def waiting_days(spans: list[Span], month: str) -> int:
    """Waiting days deducted in this month (sum over parts; parts are disjoint)."""
    return sum(waiting_days_in_month(span, month) for span in spans)


def is_effective(spans: list[Span], day: date) -> bool:
    """True when ``day`` is payable under any part (waiting period excluded)."""
    return any(span.effective_start <= day < span.end for span in spans)


def month_has_coverage(spans: list[Span], month: str) -> bool:
    month_start, month_end = month_bounds(month)
    return any(span.start < month_end and month_start < span.end for span in spans)


def total_payable_days(spans: list[Span]) -> int:
    """Total payable days across the whole coverage (for the vehicle summary)."""
    return sum(max(0, (span.end - span.effective_start).days) for span in spans)
