"""周期与触发日期计算。"""
import calendar
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
    if not s:
        return None
    s = str(s).strip().replace("/", "-").replace(".", "-")
    if len(s.split("-")) == 2:
        s = "1896-" + s  # 闰年占位，支持2月29日且不推算未知出生年份的年龄。
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y%m%d", "%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            if fmt == "%m-%d":
                return date(1900, dt.month, dt.day)
            return dt.date()
        except ValueError:
            continue
    return None


def _same_md(src: date, target: date):
    """月日是否相同；2/29 生日在平年落到 2/28。"""
    if src.month == target.month and src.day == target.day:
        return True
    if src.month == 2 and src.day == 29:
        if target.month == 2 and target.day == 28 and not calendar.isleap(target.year):
            return True
    return False


def match_employee(emp, cycle_start, cycle_end):
    """返回该员工在周期内命中的事件列表 [{event_type, event_date, years, trigger_at}]。"""
    out = []
    birth = parse_date(emp.get("birth_date"))
    join = parse_date(emp.get("join_date"))
    for day in daterange(cycle_start, cycle_end):
        if birth and _same_md(birth, day):
            years = day.year - birth.year if birth.year > 1901 else None
            out.append({
                "event_type": "birthday",
                "event_date": day.isoformat(),
                "years": years,
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
