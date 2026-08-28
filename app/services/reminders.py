"""Date arithmetic for anniversaries, monthsaries and one-off dates.

Kept free of the database and of Telegram so the awkward cases - a recurrence
that has already passed this period, a 29 February anniversary in a common
year, a monthly date landing on the 31st in February - can be tested directly.

Three kinds of date share one table:

* **yearly** - an anniversary. The stored date is the original event; the next
  occurrence is this year's or next year's, whichever is still ahead.
* **monthly** - a monthsary. Same day each month.
* **once** - a concert, a booking. Once past it stops appearing; it is history,
  not a reminder.

Which milestones a date announces at is stored per date rather than fixed here,
because the people using this app should be able to say "just tell me on the
day" for one date and "warn me a month ahead" for another without editing code.
REMINDER_MILESTONES is only the default for a date that has never been given
its own.
"""

import logging
from calendar import monthrange
from dataclasses import dataclass
from datetime import date

logger = logging.getLogger(__name__)

# Days before an occurrence on which the bot may speak up. Offered in the UI,
# and used as the default when a date has no setting of its own.
AVAILABLE_MILESTONES = (30, 14, 7, 3, 1, 0)
REMINDER_MILESTONES = (30, 14, 7, 3, 1, 0)

ONCE, MONTHLY, YEARLY = "once", "monthly", "yearly"
RECURRENCES = (ONCE, MONTHLY, YEARLY)


def parse_milestones(raw: str | None) -> tuple[int, ...]:
    """Read a stored milestone list, falling back to the default.

    Stored as comma-separated days. An empty string means "never announce",
    which is different from unset, so it is preserved rather than defaulted.
    """
    if raw is None:
        return REMINDER_MILESTONES
    text = raw.strip()
    if not text:
        return ()
    days: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            logger.warning("ignoring unreadable milestone %r", part)
            continue
        if value >= 0 and value not in days:
            days.append(value)
    return tuple(sorted(days, reverse=True))


def format_milestones(days) -> str:
    return ",".join(str(d) for d in sorted({int(d) for d in days}, reverse=True))


def describe_milestones(days: tuple[int, ...]) -> str:
    if not days:
        return "no reminders"
    parts = ["on the day" if d == 0 else f"{d}d" for d in days]
    return ", ".join(parts)


@dataclass(frozen=True)
class Upcoming:
    """A date resolved against today."""

    id: int
    label: str
    stored_date: str
    recurrence: str
    occurs_on: date
    days_until: int
    milestones: tuple[int, ...] = REMINDER_MILESTONES
    # Anniversary count for yearly dates, month count for monthly ones.
    count: int | None = None

    @property
    def is_today(self) -> bool:
        return self.days_until == 0

    @property
    def recurring(self) -> bool:
        return self.recurrence in (MONTHLY, YEARLY)

    def describe_when(self) -> str:
        if self.days_until == 0:
            return "today"
        if self.days_until == 1:
            return "tomorrow"
        return f"in {self.days_until} days"

    def describe_count(self) -> str:
        if not self.count:
            return ""
        if self.recurrence == MONTHLY:
            return f" ({self.count} months)"
        return f" ({_ordinal(self.count)})"

    def describe(self) -> str:
        """One line suitable for a chat message."""
        return (
            f"{self.label}{self.describe_count()} - "
            f"{self.describe_when()} ({self.occurs_on.isoformat()})"
        )


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _clamped(year: int, month: int, day: int) -> date:
    """The given day of that month, or its last day if the month is shorter.

    A monthsary on the 31st falls on the 30th in a 30-day month and on the 28th
    in February; the alternative is skipping those months entirely, which is
    not what anyone means by "the 31st of each month".
    """
    return date(year, month, min(day, monthrange(year, month)[1]))


def _yearly_on(original: date, year: int) -> date:
    """29 February is observed on 28 February in common years, so it stays
    inside its own month."""
    try:
        return original.replace(year=year)
    except ValueError:
        return date(year, 2, 28)


def next_occurrence(
    stored_date: str, recurrence: str, today: date | None = None
) -> date | None:
    """When this date next happens, or None if it is a past one-off."""
    today = today or date.today()
    try:
        original = date.fromisoformat(stored_date)
    except (TypeError, ValueError):
        logger.warning("unparseable stored date %r", stored_date)
        return None

    if recurrence == YEARLY:
        candidate = _yearly_on(original, today.year)
        if candidate < today:
            candidate = _yearly_on(original, today.year + 1)
        return candidate

    if recurrence == MONTHLY:
        year, month = today.year, today.month
        for _ in range(3):  # this month, then the next two; always resolves
            candidate = _clamped(year, month, original.day)
            if candidate >= today:
                return candidate
            month += 1
            if month > 12:
                year, month = year + 1, 1
        return None

    return original if original >= today else None


def resolve(row, today: date | None = None) -> Upcoming | None:
    """Turn a `dates` row into an Upcoming, or None if it is in the past."""
    today = today or date.today()
    data = dict(row)

    recurrence = (data.get("recurrence") or "").strip().lower()
    if recurrence not in RECURRENCES:
        # Older rows only had a boolean.
        recurrence = YEARLY if data.get("recurring") else ONCE

    stored = data.get("date")
    occurs_on = next_occurrence(stored, recurrence, today)
    if occurs_on is None:
        return None

    count = None
    try:
        original = date.fromisoformat(stored)
        if recurrence == YEARLY:
            elapsed = occurs_on.year - original.year
            count = elapsed if elapsed > 0 else None
        elif recurrence == MONTHLY:
            months = (occurs_on.year - original.year) * 12 + (
                occurs_on.month - original.month
            )
            count = months if months > 0 else None
    except (TypeError, ValueError):
        count = None

    return Upcoming(
        id=int(data["id"]),
        label=str(data["label"]),
        stored_date=stored,
        recurrence=recurrence,
        occurs_on=occurs_on,
        days_until=(occurs_on - today).days,
        milestones=parse_milestones(data.get("reminder_days")),
        count=count,
    )


def upcoming(rows, today: date | None = None, limit: int | None = None) -> list[Upcoming]:
    """Every future date, soonest first. Past one-offs are dropped."""
    today = today or date.today()
    resolved = [r for r in (resolve(row, today) for row in rows) if r is not None]
    resolved.sort(key=lambda u: (u.days_until, u.label))
    return resolved[:limit] if limit else resolved


def banner_dates(rows, today: date | None = None) -> list[Upcoming]:
    """What the Mini App banner shows: the nearest monthly date and the nearest
    non-monthly one.

    A monthsary recurs every month, so it is almost always sooner than an
    anniversary. Showing only the single nearest date would let the monthly one
    hide the yearly one permanently, which is exactly backwards - the yearly
    one matters more.
    """
    items = upcoming(rows, today)
    nearest_monthly = next((u for u in items if u.recurrence == MONTHLY), None)
    nearest_other = next((u for u in items if u.recurrence != MONTHLY), None)
    chosen = [u for u in (nearest_other, nearest_monthly) if u is not None]
    chosen.sort(key=lambda u: u.days_until)
    return chosen


def due_today(rows, today: date | None = None) -> list[Upcoming]:
    """The dates worth announcing today, each against its own milestones."""
    return [u for u in upcoming(rows, today) if u.days_until in u.milestones]
