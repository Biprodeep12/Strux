"""Dates the user said, worked out by Strux, never by the model.

The 7B model does weekday arithmetic from its training years ("friday" -> Fri 6 Oct 2023), so:
  1. `annotate()` writes the exact date after every date phrase in the user's message before the model sees it:
     "on friday at 11" -> "on friday [Fri 23 Oct 2026] at 11"
  2. `fix()` corrects a tool argument whose day is not the one the user said (when they said exactly one day),
     keeping the model's time of day.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from ..planner import parse_when

_WD = ["mon", "tues", "wednes", "thurs", "fri", "satur", "sun"]
_MON = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
DAY_PHRASE = re.compile(
    r"\b(day after tomorrow|tomorrow|tmrw|tmr|tonight|today|"
    r"(?:this|next|coming)\s+(?:mon|tues|wednes|thurs|fri|satur|sun)day|(?:mon|tues|wednes|thurs|fri|satur|sun)day|"
    r"next week|in \d+ (?:days?|weeks?)|"
    rf"\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MON}|{_MON}\s+\d{{1,2}}(?:st|nd|rd|th)?|the \d{{1,2}}(?:st|nd|rd|th))\b", re.I)
IN_TIME = re.compile(r"\bin (\d+|an?|one|two|three|half an?) (min(?:ute)?s?|hours?)\b", re.I)
_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3}
# one date said, but several days meant: "from 1 Nov onwards", "over the next 3 days", "day-wise", "every day"
MULTI_DAY = re.compile(r"\b(onwards?|from|through|till|until|(each|every|per) day|daily|day[- ]?wise|over the (next|coming)|"
                       r"next \d+ days|for \d+ days|spread|timetable|schedule for the week|week[- ]?wise)\b", re.I)


def resolve(phrase: str, now: float) -> date | None:
    """The calendar day a phrase means (upcoming occurrence for weekdays; today counts for "this <weekday>")."""
    p = re.sub(r"\s+", " ", phrase.lower().strip())
    today = datetime.fromtimestamp(now).date()
    if p == "day after tomorrow":
        return today + timedelta(days=2)
    if p in ("tomorrow", "tmrw", "tmr"):
        return today + timedelta(days=1)
    if p in ("today", "tonight"):
        return today
    if p == "next week":
        return today + timedelta(days=7 - today.weekday())                 # next Monday
    m = re.match(r"in (\d+) (day|week)", p)
    if m:
        return today + timedelta(days=int(m.group(1)) * (7 if m.group(2) == "week" else 1))
    m = re.match(r"(?:(this|next|coming) )?(mon|tues|wednes|thurs|fri|satur|sun)day", p)
    if m:
        ahead = (_WD.index(m.group(2)) - today.weekday()) % 7
        if ahead == 0 and m.group(1) != "this":
            ahead = 7                                                       # "friday" said on a friday = next week
        return today + timedelta(days=ahead)
    m = re.match(r"the (\d{1,2})", p)
    if m:
        d = int(m.group(1))
        try:
            c = today.replace(day=d)
        except ValueError:
            return None
        if c < today:                                                       # "the 5th" after the 5th: next month
            c = (c.replace(day=1) + timedelta(days=32)).replace(day=d)
        return c
    ts = parse_when(re.sub(r"(st|nd|rd|th)\b|\bof\b", "", p), now)        # "30 October", "oct 30"
    return datetime.fromtimestamp(ts).date() if ts else None


def days_said(text: str, now: float) -> list[date]:
    out: list[date] = []
    for m in DAY_PHRASE.finditer(text or ""):
        d = resolve(m.group(0), now)
        if d and d not in out:
            out.append(d)
    return out


def moment_said(text: str, now: float) -> float | None:
    """'in 2 hours' / 'in 30 minutes' -> the exact moment (only if said once)."""
    found = IN_TIME.findall(text or "")
    if len(found) != 1:
        return None
    n, unit = found[0]
    n = 0.5 if n.lower().startswith("half") else _NUM.get(n.lower(), None) or int(n)
    return now + n * (3600 if unit.lower().startswith("hour") else 60)


def annotate(text: str, now: float) -> str:
    """Put the exact date after each date phrase, and the clock time after 'in 2 hours'."""
    def day(m: re.Match) -> str:
        d = resolve(m.group(0), now)
        return f"{m.group(0)} [{d:%a %d %b %Y}]" if d else m.group(0)

    def moment(m: re.Match) -> str:
        t = moment_said(m.group(0), now)
        return f"{m.group(0)} [{datetime.fromtimestamp(t):%a %d %b %Y %H:%M}]" if t else m.group(0)

    return IN_TIME.sub(moment, DAY_PHRASE.sub(day, text or ""))


def fix(value, said: str, now: float, parse) -> tuple[object, str | None]:
    """(value, note): if the user named exactly one day (or one 'in N hours') and `value` lands elsewhere, move it
    there, keeping its time of day. `parse` turns a tool value into a timestamp (agent.parse_due / parse_when)."""
    if value in (None, ""):
        return value, None
    exact = moment_said(said, now)
    ts = parse(value, now)
    if exact is not None:
        if ts is None or abs(ts - exact) > 120:
            return datetime.fromtimestamp(exact).strftime("%Y-%m-%d %H:%M"), f"{value!r} -> in-time"
        return value, None
    days = days_said(said, now)
    if len(days) != 1 or ts is None or MULTI_DAY.search(said):
        return value, None
    got = datetime.fromtimestamp(ts)
    if got.date() == days[0]:
        return value, None
    fixed = datetime.combine(days[0], got.time())
    return fixed.strftime("%Y-%m-%d %H:%M"), f"{value!r} -> {fixed:%a %d %b %H:%M}"


_NUMW = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
         "eleven": 11, "twelve": 12}
CLOCK = re.compile(r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)|\bat\s+(\d{1,2})(?::(\d{2}))?\b(?!\s*(?:min|hour|day|%))|"
                   r"\bat\s+(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b|\b(noon|midday|midnight)\b", re.I)
PART = re.compile(r"\b(this |tomorrow |in the )?(morning|afternoon|evening|tonight|night)\b", re.I)
_PART_HOUR = {"morning": 9, "afternoon": 14, "evening": 18, "tonight": 21, "night": 21}
ABS_IN_TITLE = re.compile(r"\b(?:on\s+)?(?:(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*,?\s+)?\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*(?:\s+\d{4})?\b", re.I)


def clock_said(text: str) -> tuple[int, int] | None:
    m = CLOCK.search(text or "")
    if not m:
        p = PART.search(text or "")
        return (_PART_HOUR[p.group(2).lower()], 0) if p else None
    if m.group(1):
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower().replace(".", "")
        h = h % 12 + (12 if ap == "pm" else 0)
    elif m.group(4):
        h, mi = int(m.group(4)), int(m.group(5) or 0)
        p = PART.search(text or "")
        if p and p.group(2).lower() in ("evening", "tonight", "night", "afternoon") and h < 12:
            h += 12
        elif h < 7:                                   # "at 3" with no am/pm: afternoon
            h += 12
    elif m.group(6):
        h, mi = _NUMW[m.group(6).lower()], 0
        p = PART.search(text or "")
        if (p and p.group(2).lower() in ("evening", "tonight", "night", "afternoon") or h < 7) and h < 12:
            h += 12
    else:
        h, mi = (12, 0) if m.group(7).lower() in ("noon", "midday") else (23, 59)
    return (h, mi) if 0 <= h <= 23 and 0 <= mi <= 59 else None


def when_said(text: str, now: float, default_hour: int = 21) -> float | None:
    """The one moment the user's words name: 'tomorrow morning' -> tomorrow 09:00, 'on the 25th' -> 25th 21:00,
    'at 7pm' -> today 19:00 (tomorrow if past), 'in 2 hours'. None if they named no time, or several days."""
    exact = moment_said(text, now)
    if exact is not None:
        return exact
    days = days_said(text, now)
    if len(days) > 1 or (days and MULTI_DAY.search(text or "")):
        return None
    clock = clock_said(text)
    if not days and not clock:
        return None
    h, mi = clock or (default_hour, 0)
    if days:
        return datetime.combine(days[0], datetime.min.time()).replace(hour=h, minute=mi).timestamp()
    t = datetime.fromtimestamp(now).replace(hour=h, minute=mi, second=0, microsecond=0)
    if t.timestamp() < now:
        t += timedelta(days=1)
    return t.timestamp()


def strip_when(title: str) -> str:
    """'submit the form tomorrow morning' -> 'submit the form'; 'Pay electricity bill on Sun 25 Oct 2026' -> 'Pay electricity bill'."""
    t = ABS_IN_TITLE.sub("", title or "")
    t = DAY_PHRASE.sub("", t)
    t = IN_TIME.sub("", t)
    t = CLOCK.sub("", t)
    t = PART.sub("", t)
    t = re.sub(r"\s+", " ", t).strip(" ,.;:-")
    t = re.sub(r"\s+\b(on|at|by|for|before|until|this|next|the)$", "", t, flags=re.I).strip(" ,.;:-")
    t = re.sub(r"^(on|at|by|for)\s+", "", t, flags=re.I)
    return t if len(t) >= 2 else (title or "")
