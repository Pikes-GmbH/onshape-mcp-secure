"""Onshape API budget (persisted) and per-client tool-call rate limiting."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

from loguru import logger


class QuotaExceeded(RuntimeError):
    pass


def _now_keys() -> tuple[str, str, str, str]:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d"), now.strftime("%G-W%V"), now.strftime("%Y-%m"), now.strftime("%Y")


ATTEMPTS_PER_DAY = 500  # hard cap incl. failed calls (Onshape doesn't bill them, but may throttle)


class OnshapeBudget:
    """Counts Onshape API requests per UTC day, month and year.

    Onshape bills only successful (2xx/3xx) calls and rejects calls with HTTP 402 once
    the annual allowance is used up. A unit is reserved atomically before each request
    and refunded if Onshape answers with an error, so the counters mirror Onshape's
    billing while parallel calls can never overshoot. State lives on the Fly volume.
    """

    def __init__(self, path: str, daily_limit: int, monthly_limit: int, yearly_limit: int = 0,
                 weekly_limit: int = 0):
        self.path = path
        self.daily_limit = daily_limit
        self.weekly_limit = weekly_limit
        self.monthly_limit = monthly_limit
        self.yearly_limit = yearly_limit
        self._lock = asyncio.Lock()
        self._state = {
            "day": "", "day_count": 0, "week": "", "week_count": 0, "month": "", "month_count": 0,
            "year": "", "year_count": 0,
            "attempts": 0,
        }
        self._persist = True
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                for key in self._state:
                    if key in data and isinstance(data[key], type(self._state[key])):
                        self._state[key] = data[key]
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            logger.warning(f"Could not read usage file, starting from zero: {exc}")
        directory = os.path.dirname(self.path) or "."
        if not os.access(directory, os.W_OK):
            logger.warning(
                f"Usage directory {directory!r} is not writable - API budget will reset on restart"
            )
            self._persist = False

    def _roll(self) -> None:
        day, week, month, year = _now_keys()
        if self._state["week"] != week:
            # first run after upgrade: at least today's usage belongs to this week
            first = not self._state["week"] and self._state["day"] == day
            self._state["week"], self._state["week_count"] = week, self._state["day_count"] if first else 0
        if self._state["day"] != day:
            self._state["day"], self._state["day_count"], self._state["attempts"] = day, 0, 0
        if self._state["month"] != month:
            self._state["month"], self._state["month_count"] = month, 0
        if self._state["year"] != year:
            if not self._state["year"] and self._state["month_count"]:
                # upgrade from a version without yearly counter: start from this month's usage
                self._state["year_count"] = self._state["month_count"]
            else:
                self._state["year_count"] = 0
            self._state["year"] = year

    def _save(self) -> None:
        if not self._persist:
            return
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._state, fh)
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.warning(f"Could not write usage file: {exc}")

    def _limit_error(self) -> QuotaExceeded | None:
        if self.daily_limit and self._state["day_count"] >= self.daily_limit:
            return QuotaExceeded(
                f"Daily Onshape API budget of {self.daily_limit} requests is used up (resets 00:00 UTC). "
                "Tell the user; do not retry today."
            )
        if self.weekly_limit and self._state["week_count"] >= self.weekly_limit:
            return QuotaExceeded(f"Weekly Onshape API budget of {self.weekly_limit} requests is used up.")
        if self.monthly_limit and self._state["month_count"] >= self.monthly_limit:
            return QuotaExceeded(f"Monthly Onshape API budget of {self.monthly_limit} requests is used up.")
        if self.yearly_limit and self._state["year_count"] >= self.yearly_limit:
            return QuotaExceeded(f"Yearly Onshape API budget of {self.yearly_limit} requests is used up.")
        if self._state["attempts"] >= max(ATTEMPTS_PER_DAY, 2 * self.daily_limit):
            return QuotaExceeded("Too many Onshape requests today (including failed ones).")
        return None

    async def check(self) -> None:
        """Fail fast before a tool starts (does not consume budget)."""
        async with self._lock:
            self._roll()
            error = self._limit_error()
        if error:
            raise error

    async def reserve(self) -> None:
        """Atomically take one unit for the request about to be sent."""
        async with self._lock:
            self._roll()
            error = self._limit_error()
            if error:
                raise error
            for key in ("day_count", "week_count", "month_count", "year_count", "attempts"):
                self._state[key] += 1
            self._save()

    async def refund(self) -> None:
        """Onshape answered with an error: it doesn't bill that call (attempt stays counted)."""
        async with self._lock:
            self._roll()
            for key in ("day_count", "week_count", "month_count", "year_count"):
                self._state[key] = max(0, self._state[key] - 1)
            self._save()

    def snapshot(self) -> dict:
        self._roll()
        return {
            "today_utc": self._state["day_count"],
            "daily_limit": self.daily_limit or "unlimited",
            "this_week_utc": self._state["week_count"],
            "weekly_limit": self.weekly_limit or "unlimited",
            "this_month_utc": self._state["month_count"],
            "monthly_limit": self.monthly_limit or "unlimited",
            "this_year_utc": self._state["year_count"],
            "yearly_limit": self.yearly_limit or "unlimited",
            "persisted": self._persist,
        }


class RateLimiter:
    """Sliding one-minute window of tool calls per client label."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._calls: dict[str, deque[float]] = defaultdict(deque)

    def hit(self, client: str) -> None:
        now = time.monotonic()
        window = self._calls[client]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= self.per_minute:
            raise QuotaExceeded(
                f"Rate limit: max {self.per_minute} tool calls per minute. Wait a moment and retry."
            )
        window.append(now)
