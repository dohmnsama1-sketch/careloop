"""Time source. Every timestamp in CareLoop goes through here so tests and demos can freeze time.

CARELOOP_NOW=2026-10-23T08:05 freezes the clock (interpreted in the household time zone).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


class Clock:
    def __init__(self, tz: str = "UTC", frozen: str | None = None):
        self.tz = ZoneInfo(tz)
        self._frozen: datetime | None = None
        frozen = frozen if frozen is not None else os.environ.get("CARELOOP_NOW")
        if frozen:
            self.freeze(frozen)

    def freeze(self, when: str | datetime) -> None:
        dt = datetime.fromisoformat(when) if isinstance(when, str) else when
        self._frozen = dt if dt.tzinfo else dt.replace(tzinfo=self.tz)

    def advance(self, **kw) -> None:
        if self._frozen is None:
            raise RuntimeError("advance() needs a frozen clock")
        self._frozen += timedelta(**kw)

    def now(self) -> datetime:
        return (self._frozen or datetime.now(self.tz)).astimezone(self.tz).replace(microsecond=0)

    def parse(self, value: str) -> datetime:
        """Parse an ISO time ("2026-10-23T08:00", "08:00" = today at 08:00) in the household zone."""
        value = value.strip()
        if len(value) <= 5 and ":" in value:
            h, m = (int(x) for x in value.split(":"))
            return self.now().replace(hour=h, minute=m, second=0)
        dt = datetime.fromisoformat(value)
        return (dt if dt.tzinfo else dt.replace(tzinfo=self.tz)).astimezone(self.tz)
