"""Recover abandoned jobs after db.init_db(), before starting the scheduler.

The claimant must persist worker_pid in the same transaction as its claim, and
persist delivery_started_at before the message request. A PID alone cannot
detect a dead thread inside a live process or PID reuse: both stay untouched.
No network calls, process termination, schema changes, or automatic sends occur.
"""
import ctypes
import errno
import logging
import os
from ctypes import wintypes

from . import db

log = logging.getLogger("recovery")


def _windows_pid_alive(pid):
    """Query a process handle; never use os.kill on Windows."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    get_exit_code = kernel32.GetExitCodeProcess
    get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    get_exit_code.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        # ERROR_INVALID_PARAMETER for a positive PID means it no longer exists.
        # Access denied and any unrecognized failure are not proof of death.
        return ctypes.get_last_error() != 87
    try:
        exit_code = wintypes.DWORD()
        if not get_exit_code(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == 259  # STILL_ACTIVE
    finally:
        close_handle(handle)


def _posix_pid_alive(pid):
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno != errno.ESRCH
    return True


def _pid_alive(pid):
    """Only a missing PID or definite process absence authorizes recovery."""
    if pid is None:
        return False
    # Do not signal a process group or truncate an invalid Windows DWORD PID.
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return True
    if os.name == "nt" and pid > 0xFFFFFFFF:
        return True
    try:
        return _windows_pid_alive(pid) if os.name == "nt" else _posix_pid_alive(pid)
    except OSError:
        return True  # Failure to inspect is not evidence that a worker died.


def recover_interrupted_jobs():
    """Atomically recover dead/missing owners; return counts by target status.

    Counts include gen_failed, failed, delivery_unknown and skipped_live.
    Existing logs, cards, approval, delivery UUID, attempt count and delivery
    timestamp are preserved. Each recovered push appends a recovery log in the
    same transaction as its event update. Repeated/concurrent calls are safe.
    Required migrations: worker_pid INTEGER and delivery_started_at TEXT.
    """
    counts = {"gen_failed": 0, "failed": 0, "delivery_unknown": 0, "skipped_live": 0}
    with db.tx() as conn:
        # Serialize with job claims, completion, and other startup recoveries.
        # All PID checks are local, bounded OS queries while this lock is held.
        conn.execute("BEGIN IMMEDIATE")
        jobs = conn.execute("""SELECT * FROM events WHERE status='pushing'
                            OR (status='generating' AND generation_token IS NOT NULL
                                AND generation_token<>'') ORDER BY id""").fetchall()
        for row in jobs:
            event = dict(row)
            pid = event["worker_pid"]
            if _pid_alive(pid):
                counts["skipped_live"] += 1
                continue

            if event["status"] == "generating":
                status = "gen_failed"
                reason = ("启动恢复：生成进程已中断；"
                          f"worker_pid={pid!r}, generation_token={event['generation_token']!r}")
            elif pid is None or event["delivery_started_at"]:
                status = "delivery_unknown"
                reason = ("启动恢复：旧任务缺少执行者或消息发送已开始，回执未知，请人工核对；"
                          f"worker_pid={pid!r}, delivery_started_at={event['delivery_started_at']!r}, "
                          f"delivery_uuid={event['delivery_uuid']!r}")
            else:
                status = "failed"
                reason = ("启动恢复：发送进程已中断，尚未开始消息请求；"
                          f"worker_pid={pid!r}, delivery_uuid={event['delivery_uuid']!r}")
            error = "\n".join(value for value in (event["last_error"], reason) if value)
            timestamp = db.now()
            changed = conn.execute("""UPDATE events SET status=?,worker_pid=NULL,
                        generation_token=CASE WHEN status='generating' THEN NULL ELSE generation_token END,
                        last_error=?,updated_at=?
                        WHERE id=? AND status=? AND worker_pid IS ?
                        AND generation_token IS ? AND delivery_started_at IS ?""",
                                   (status, error, timestamp, event["id"], event["status"], pid,
                                    event["generation_token"], event["delivery_started_at"]))
            if not changed.rowcount:
                continue
            if event["status"] == "pushing":
                conn.execute("""INSERT INTO push_logs(event_id,card_id,attempt,status,error,
                              operator,recipient_snapshot,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                             (event["id"], event["selected_card_id"], event["push_attempts"],
                              status, reason, "recovery", event["employee_snapshot"], timestamp))
            counts[status] += 1
    if any(counts[key] for key in ("gen_failed", "failed", "delivery_unknown")):
        log.info("启动恢复完成：%s", counts)
    return counts
