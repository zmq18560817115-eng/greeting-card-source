"""定时调度：周四晚生成 + 每分钟扫描到点的推送任务。"""
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import pipeline, push
from .settings import TZ, WEEKLY_CRON_DAY, WEEKLY_CRON_HOUR, WEEKLY_CRON_MINUTE

log = logging.getLogger("scheduler")
_sched = None


def weekly_job():
    log.info("=== 周期生成任务开始 ===")
    try:
        result = pipeline.run_weekly()
        log.info("周期生成完成：%s", result)
    except Exception:  # noqa: BLE001
        log.exception("周期生成任务异常")


def push_job():
    for ev in push.due_events():
        log.info("到点推送 event=%s", ev["id"])
        try:
            push.push_event(ev["id"], operator="auto")
        except Exception:  # noqa: BLE001
            log.exception("推送任务异常 event=%s", ev["id"])


def start():
    global _sched
    if _sched:
        return _sched
    _sched = BackgroundScheduler(timezone=TZ)
    _sched.add_job(
        weekly_job, CronTrigger(day_of_week=WEEKLY_CRON_DAY, hour=WEEKLY_CRON_HOUR,
                                minute=WEEKLY_CRON_MINUTE, timezone=TZ),
        id="weekly_generate", replace_existing=True, misfire_grace_time=3600,
    )
    _sched.add_job(
        push_job, CronTrigger(minute="*", timezone=TZ),
        id="push_scan", replace_existing=True, max_instances=1, misfire_grace_time=300,
    )
    _sched.start()
    log.info("调度器已启动：生成 每周%s %02d:%02d，推送扫描 每分钟",
             WEEKLY_CRON_DAY, WEEKLY_CRON_HOUR, WEEKLY_CRON_MINUTE)
    return _sched


def jobs():
    if not _sched:
        return []
    return [{"id": j.id, "next_run": str(j.next_run_time)} for j in _sched.get_jobs()]
