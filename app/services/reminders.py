"""Date arithmetic for anniversaries and one-off important dates.

Kept free of the database and of Telegram so the awkward cases - a recurring
date that has already passed this year, a 29 February anniversary in a common
year - can be tested directly.

Two kinds of date share one table:

* **recurring** (`recurring = true`): an anniversary. The stored date is the
  original event; what matters is its month and day, and the next occurrence is
  this year's or next year's, whichever is still ahead.
* **one-off** (`recurring = false`): a concert, a booking. Once it is past it
  simply stops appearing - it is history, not a reminder.
"""

import logging
from dataclasses import dataclass
from datetime import date

logger = logging.getLogger(__name__)

# Days before an occurrence on which the bot speaks up. Milestones rather than
# "every day within a week", which would nag daily and get muted.
REMINDER_MILESTONES = (30, 14, 7, 3, 1, 0)


@dataclass(frozen=True)
class Upcoming:
    """A date resolved against today."""

    id: int
    label: str
    stored_date: str
    recurring: bool
    occurs_on: date
    days_until: int
    # Anniversary count, e.g. 3 for the third anniversary. None for one-offs
    # and for the original year itself.
    years: int | None = None

    @property
    def is_today(self) -> bool:
        return self.days_until == 0

    def describe_when(self) -> str:
        if self.days_until == 0:
            return "today"
        if self.days_until == 1:
            return "tomorrow"
        return f"in {self.days_until} days"

    def describe(self) -> str:
        """One line suitable for a chat message."""
        suffix = f" ({_ordinal(self.years)})" if self.years else ""
        return f"{self.label}{suffix} - {self.describe_when()} ({self.occurs_on.isoformat()})"


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _on_or_after(original: date, year: int) -> date:
    """The anniversary of `original` in `year`.

    29 February is observed on 28 February in common years: an anniversary
    should fall inside the same month as the original, and moving it to 1 March
    would slide it past the month boundary.
    """
    try:
        return original.replace(year=year)
    except ValueError:
        return date(year, 2, 28)


def next_occurrence(
    stored_date: str, recurring: bool, today: date | None = None
) -> date | None:
    """When this date next happens, or None if it is a past one-off."""
    today = today or date.today()
    try:
        original = date.fromisoformat(stored_date)
    except (TypeError, ValueError):
        logger.warning("unparseable stored date %r", stored_date)
        return None

    if not recurring:
        return original if original >= today else None

    candidate = _on_or_after(original, today.year)
    if candidate < today:
        candidate = _on_or_after(original, today.year + 1)
    return candidate


def resolve(row, today: date | None = None) -> Upcoming | None:
    """Turn a `dates` row into an Upcoming, or None if it is in the past."""
    today = today or date.today()
    data = dict(row)
    recurring = bool(data.get("recurring"))
    stored = data.get("date")

    occurs_on = next_occurrence(stored, recurring, today)
    if occurs_on is None:
        return None

    years = None
    if recurring:
        try:
            original_year = date.fromisoformat(stored).year
            elapsed = occurs_on.year - original_year
            years = elapsed if elapsed > 0 else None
        except (TypeError, ValueError):
            years = None

    return Upcoming(
        id=int(data["id"]),
        label=str(data["label"]),
        stored_date=stored,
        recurring=recurring,
        occurs_on=occurs_on,
        days_until=(occurs_on - today).days,
        years=years,
    )


def upcoming(rows, today: date | None = None, limit: int | None = None) -> list[Upcoming]:
    """Every future date, soonest first. Past one-offs are dropped."""
    today = today or date.today()
    resolved = [r for r in (resolve(row, today) for row in rows) if r is not None]
    resolved.sort(key=lambda u: (u.days_until, u.label))
    return resolved[:limit] if limit else resolved


def due_today(
    rows, today: date | None = None, milestones: tuple[int, ...] = REMINDER_MILESTONES
) -> list[Upcoming]:
    """The dates worth announcing on this particular day.

    Only exact milestone hits qualify, so a date is mentioned a handful of
    times as it approaches rather than every morning for a month.
    """
    return [u for u in upcoming(rows, today) if u.days_until in milestones]
