from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.automation.errors import AutomationValidationError


_MAXIMUM_DAY_BY_MONTH = {
    1: 31,
    2: 29,
    3: 31,
    4: 30,
    5: 31,
    6: 30,
    7: 31,
    8: 31,
    9: 30,
    10: 31,
    11: 30,
    12: 31,
}


@dataclass(frozen=True, slots=True)
class _CronField:
    values: frozenset[int]
    wildcard: bool

    @classmethod
    def parse(
        cls,
        expression: str,
        minimum: int,
        maximum: int,
        *,
        sunday_alias: bool = False,
    ) -> "_CronField":
        values: set[int] = set()
        for part in expression.split(","):
            if not part:
                raise AutomationValidationError("cron field contains an empty item")
            base, separator, step_text = part.partition("/")
            if separator:
                try:
                    step = int(step_text)
                except ValueError as error:
                    raise AutomationValidationError("cron step is invalid") from error
                if step < 1:
                    raise AutomationValidationError("cron step must be positive")
            else:
                step = 1

            if base == "*":
                start, end = minimum, maximum
            elif "-" in base:
                start_text, end_text = base.split("-", maxsplit=1)
                try:
                    start, end = int(start_text), int(end_text)
                except ValueError as error:
                    raise AutomationValidationError("cron range is invalid") from error
            else:
                try:
                    start = int(base)
                except ValueError as error:
                    raise AutomationValidationError("cron value is invalid") from error
                end = maximum if separator else start

            alias_maximum = 7 if sunday_alias else maximum
            if start < minimum or end > alias_maximum or start > end:
                raise AutomationValidationError("cron value is outside its field range")
            for item in range(start, end + 1, step):
                values.add(0 if sunday_alias and item == 7 else item)
        complete = set(range(minimum, maximum + 1))
        return cls(frozenset(values), values == complete)

    def matches(self, value: int) -> bool:
        return value in self.values


@dataclass(frozen=True, slots=True)
class CronExpression:
    """A bounded five-field cron evaluator using IANA timezones.

    Evaluation walks UTC minutes and then converts to local time. A skipped
    DST wall-clock minute never fires; a repeated wall-clock minute fires for
    both distinct UTC instants. Day-of-month/day-of-week follows traditional
    cron OR semantics when both fields are restricted.
    """

    minute: _CronField
    hour: _CronField
    day_of_month: _CronField
    month: _CronField
    day_of_week: _CronField

    @classmethod
    def parse(cls, expression: str) -> "CronExpression":
        fields = expression.split()
        if len(fields) != 5:
            raise AutomationValidationError("cron expression must contain five fields")
        parsed = cls(
            minute=_CronField.parse(fields[0], 0, 59),
            hour=_CronField.parse(fields[1], 0, 23),
            day_of_month=_CronField.parse(fields[2], 1, 31),
            month=_CronField.parse(fields[3], 1, 12),
            day_of_week=_CronField.parse(fields[4], 0, 6, sunday_alias=True),
        )
        parsed._validate_calendar_feasibility()
        return parsed

    def _validate_calendar_feasibility(self) -> None:
        """Reject calendar combinations that can never match.

        When day-of-week is restricted, traditional cron OR semantics can
        still match a weekday even if the day-of-month does not exist.  The
        impossible case is therefore a restricted day-of-month combined with
        a wildcard weekday and months that never contain any requested day.
        """

        if self.day_of_month.wildcard or not self.day_of_week.wildcard:
            return
        if not any(
            day <= _MAXIMUM_DAY_BY_MONTH[month]
            for month in self.month.values
            for day in self.day_of_month.values
        ):
            raise AutomationValidationError(
                "cron day-of-month never exists in the selected months"
            )

    def matches(self, instant: datetime, timezone_name: str) -> bool:
        zone = _zone(timezone_name)
        if instant.tzinfo is None:
            raise AutomationValidationError("cron instants must be timezone-aware")
        return self._matches_in_zone(instant, zone)

    def _matches_in_zone(self, instant: datetime, zone: ZoneInfo) -> bool:
        local = instant.astimezone(zone)
        cron_weekday = (local.weekday() + 1) % 7
        day_of_month_match = self.day_of_month.matches(local.day)
        day_of_week_match = self.day_of_week.matches(cron_weekday)
        if self.day_of_month.wildcard:
            day_match = day_of_week_match
        elif self.day_of_week.wildcard:
            day_match = day_of_month_match
        else:
            day_match = day_of_month_match or day_of_week_match
        return (
            self.minute.matches(local.minute)
            and self.hour.matches(local.hour)
            and self.month.matches(local.month)
            and day_match
        )

    def next_after(
        self,
        instant: datetime,
        timezone_name: str,
        *,
        max_search_minutes: int = 5 * 366 * 24 * 60,
    ) -> datetime:
        zone = _zone(timezone_name)
        if instant.tzinfo is None:
            raise AutomationValidationError("cron instants must be timezone-aware")
        candidate = instant.astimezone(timezone.utc).replace(
            second=0,
            microsecond=0,
        ) + timedelta(minutes=1)
        for _ in range(max_search_minutes):
            if self._matches_in_zone(candidate, zone):
                return candidate
            candidate += timedelta(minutes=1)
        raise AutomationValidationError("cron expression has no fire time in search window")

    def previous_at_or_before(
        self,
        instant: datetime,
        timezone_name: str,
        *,
        max_search_minutes: int = 5 * 366 * 24 * 60,
    ) -> datetime:
        """Return the latest matching UTC minute at or before ``instant``."""

        zone = _zone(timezone_name)
        if instant.tzinfo is None:
            raise AutomationValidationError("cron instants must be timezone-aware")
        candidate = instant.astimezone(timezone.utc).replace(second=0, microsecond=0)
        for _ in range(max_search_minutes):
            if self._matches_in_zone(candidate, zone):
                return candidate
            candidate -= timedelta(minutes=1)
        raise AutomationValidationError("cron expression has no fire time in search window")


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as error:
        raise AutomationValidationError("schedule timezone is unknown") from error


__all__ = ["CronExpression"]
