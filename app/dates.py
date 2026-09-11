"""周期与触发日期计算。"""
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .settings import ANNIVERSARY_PUSH_TIME, BIRTHDAY_PUSH_TIME, TZ


def next_cycle(today=None):
    """返回下一个「周一~周日」区间。周四晚上跑时即为下周一到下周日。"""
    today = today or datetime.now(ZoneInfo(TZ)).date()
    days = (7 - today.weekday()) % 7 or 7
    start = today + timedelta(days=days)
    return start, start + timedelta(days=6)


def this_cycle(today=None):
    """本周（周一~周日）。"""
    today = today or datetime.now(ZoneInfo(TZ)).date()
    start = today - timedelta(days=today.weekday())
    return start, start + timedelta(days=6)


def daterange(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def parse_date(s):
    """Parse a complete date; never supply a missing employment year."""
    if not s:
        return None
    if isinstance(s, datetime):
        return s.date()
    if isinstance(s, date):
        return s
    s = str(s).strip().replace("/", "-").replace(".", "-")
    if not re.fullmatch(r"(?:\d{4}-\d{1,2}-\d{1,2}(?: \d{1,2}:\d{2}:\d{2})?|\d{8})", s):
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_birthday(value):
    """Read month/day from either a legacy full date or a month/day birthday."""
    full = parse_date(value)
    if full:
        return date(2000, full.month, full.day)
    raw = str(value or "").strip().replace("/", "-").replace(".", "-")
    matched = re.fullmatch(r"(\d{1,2})\s*(?:-|月)\s*(\d{1,2})\s*日?", raw)
    if matched:
        try:
            return date(2000, *map(int, matched.groups()))
        except ValueError:
            pass
    return None


def _same_md(src: date, target: date):
    """Exact month/day only, without moving leap-day events to another date."""
    return (src.month, src.day) == (target.month, target.day)


def completed_years_since(start, as_of=None):
    """Completed service years from a full joining date, never rounded early."""
    joined = parse_date(start)
    day = parse_date(as_of) if as_of else datetime.now(ZoneInfo(TZ)).date()
    if not joined or not day:
        return None
    return max(0, day.year - joined.year - int((day.month, day.day) < (joined.month, joined.day)))


def match_employee(emp, cycle_start, cycle_end):
    """返回该员工在周期内命中的事件列表 [{event_type, event_date, years, trigger_at}]。"""
    out = []
    birth = parse_birthday(emp.get("birth_date"))
    join = parse_date(emp.get("join_date"))
    for day in daterange(cycle_start, cycle_end):
        if birth and _same_md(birth, day):
            out.append({
                "event_type": "birthday",
                "event_date": day.isoformat(),
                "years": None,
                "trigger_at": _at(day, BIRTHDAY_PUSH_TIME),
            })
        if join and _same_md(join, day) and day.year > join.year:
            out.append({
                "event_type": "anniversary",
                "event_date": day.isoformat(),
                "years": day.year - join.year,
                "trigger_at": _at(day, ANNIVERSARY_PUSH_TIME),
            })
    return out


def _at(day: date, hm):
    return datetime(day.year, day.month, day.day, hm[0], hm[1]).strftime("%Y-%m-%d %H:%M:%S")
