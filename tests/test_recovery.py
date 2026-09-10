"""Recovery regression tests: temporary SQLite, mocked PIDs, no services.

Run: python -B -m unittest discover -s tests -p test_recovery.py -v
"""
import ctypes
import errno
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from app import db, recovery


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        database = patch.object(db, "DB_PATH", Path(temporary.name) / "recovery.db")
        database.start()
        self.addCleanup(database.stop)
        db.init_db()
        # Allows running this isolated test before the caller lands migrations.
        with db.tx() as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
            for column, kind in (("worker_pid", "INTEGER"), ("delivery_started_at", "TEXT")):
                if column not in columns:
                    conn.execute(f"ALTER TABLE events ADD COLUMN {column} {kind}")
        self.employee_id = db.execute("INSERT INTO employees(name) VALUES('Recovery test')")
        self.sequence = 0
        live = patch.object(recovery, "_pid_alive", side_effect=lambda pid: pid == 222)
        self.pid_alive = live.start()
        self.addCleanup(live.stop)

    def event(self, status="pushing", pid=111, started=None, token=None):
        self.sequence += 1
        event_id = db.execute("""INSERT INTO events(employee_id,event_type,event_date,trigger_at,
                              status,worker_pid,delivery_started_at,generation_token,
                              confirmed_by,confirmed_at,employee_snapshot,delivery_uuid,
                              push_attempts,last_error,updated_at)
                              VALUES(?,'birthday',?,'2026-09-10 09:00:00',?,?,?,?,
                                     'reviewer','approval-time','snapshot','delivery-uuid',2,
                                     'previous evidence','old-update')""",
                             (self.employee_id, f"2026-09-{self.sequence:02d}",
                              status, pid, started, token))
        card_id = db.execute("""INSERT INTO cards(event_id,idx,file_path,employee_snapshot)
                             VALUES(?,1,'existing-card.png','card-snapshot')""", (event_id,))
        db.execute("UPDATE events SET selected_card_id=? WHERE id=?", (card_id, event_id))
        return event_id

    def get(self, event_id):
        return db.query_one("SELECT * FROM events WHERE id=?", (event_id,))

    def logs(self, event_id):
        return db.query("SELECT * FROM push_logs WHERE event_id=? ORDER BY id", (event_id,))

    def test_dead_generation_is_released_and_keeps_cards_and_evidence(self):
        event_id = self.event("generating", token="generation-claim")
        card = db.query_one("SELECT * FROM cards WHERE event_id=?", (event_id,))
        result = recovery.recover_interrupted_jobs()
        event = self.get(event_id)
        self.assertEqual(result["gen_failed"], 1)
        self.assertEqual(event["status"], "gen_failed")
        self.assertIsNone(event["generation_token"])
        self.assertIsNone(event["worker_pid"])
        self.assertIn("previous evidence", event["last_error"])
        self.assertIn("generation-claim", event["last_error"])
        self.assertEqual(card, db.query_one("SELECT * FROM cards WHERE event_id=?", (event_id,)))
        self.assertEqual(self.logs(event_id), [])

    def test_dead_push_before_message_is_retryable_with_approval_unchanged(self):
        event_id = self.event()
        before = self.get(event_id)
        self.assertEqual(recovery.recover_interrupted_jobs()["failed"], 1)
        event = self.get(event_id)
        self.assertEqual(event["status"], "failed")
        self.assertIsNone(event["worker_pid"])
        for field in ("confirmed_at", "confirmed_by", "employee_snapshot", "selected_card_id",
                      "delivery_uuid", "delivery_started_at", "push_attempts", "pushed_at"):
            self.assertEqual(before[field], event[field], field)
        audit = self.logs(event_id)[0]
        self.assertEqual((audit["status"], audit["operator"], audit["attempt"]), ("failed", "recovery", 2))
        self.assertEqual(audit["recipient_snapshot"], "snapshot")
        self.assertIsNone(audit["message_id"])
        self.assertIsNone(db.query_one("SELECT id FROM events WHERE employee_id=? AND status='pushing'",
                                      (self.employee_id,)))

    def test_dead_push_after_message_started_is_unknown_and_keeps_timestamp(self):
        event_id = self.event(started="2026-09-10 10:00:00")
        self.assertEqual(recovery.recover_interrupted_jobs()["delivery_unknown"], 1)
        event = self.get(event_id)
        self.assertEqual(event["status"], "delivery_unknown")
        self.assertIsNone(event["worker_pid"])
        self.assertEqual(event["delivery_started_at"], "2026-09-10 10:00:00")
        self.assertEqual(self.logs(event_id)[0]["status"], "delivery_unknown")

    def test_legacy_push_is_always_unknown_even_without_send_marker(self):
        for started in (None, "", "2026-09-10 10:00:00"):
            with self.subTest(started=started):
                event_id = self.event(pid=None, started=started)
                recovery.recover_interrupted_jobs()
                self.assertEqual(self.get(event_id)["status"], "delivery_unknown")
                self.assertEqual(self.get(event_id)["delivery_started_at"], started)

    def test_legacy_generation_with_token_can_recover(self):
        event_id = self.event("generating", pid=None, token="old-token")
        recovery.recover_interrupted_jobs()
        event = self.get(event_id)
        self.assertEqual(event["status"], "gen_failed")
        self.assertIsNone(event["generation_token"])

    def test_live_workers_are_untouched_regardless_of_job_phase(self):
        ids = [self.event("generating", pid=222, token="live-claim"),
               self.event(pid=222), self.event(pid=222, started="started")]
        before = [self.get(event_id) for event_id in ids]
        result = recovery.recover_interrupted_jobs()
        self.assertEqual(result, {"gen_failed": 0, "failed": 0, "delivery_unknown": 0, "skipped_live": 3})
        self.assertEqual(before, [self.get(event_id) for event_id in ids])
        self.assertEqual(db.query("SELECT * FROM push_logs"), [])

    def test_unclaimed_generation_and_other_statuses_are_untouched(self):
        ids = [self.event("generating", token=token) for token in (None, "")]
        ids += [self.event(status, token="keep-token") for status in
                ("ready", "confirmed", "failed", "blocked", "gen_failed", "skipped", "pushed", "delivery_unknown")]
        before = [self.get(event_id) for event_id in ids]
        recovery.recover_interrupted_jobs()
        self.assertEqual(before, [self.get(event_id) for event_id in ids])
        self.pid_alive.assert_not_called()

    def test_existing_delivery_logs_are_preserved_and_recovery_is_idempotent(self):
        event_id = self.event(started="started")
        db.execute("""INSERT INTO push_logs(event_id,status,message_id,image_key,error,recipient_snapshot)
                    VALUES(?,'success','message-evidence','image-evidence','old-error','recipient-evidence')""",
                   (event_id,))
        old_log = self.logs(event_id)[0]
        recovery.recover_interrupted_jobs()
        after = self.get(event_id)
        logs = self.logs(event_id)
        self.assertEqual(logs[0], old_log)
        self.assertEqual(len(logs), 2)
        self.assertIn("previous evidence", after["last_error"])
        self.assertEqual(recovery.recover_interrupted_jobs(),
                         {"gen_failed": 0, "failed": 0, "delivery_unknown": 0, "skipped_live": 0})
        self.assertEqual(self.get(event_id), after)
        self.assertEqual(self.logs(event_id), logs)

    def test_log_failure_rolls_back_the_event_transition(self):
        event_id = self.event()
        before = self.get(event_id)
        db.execute("""CREATE TRIGGER reject_recovery BEFORE INSERT ON push_logs
                    BEGIN SELECT RAISE(ABORT, 'simulated audit write failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            recovery.recover_interrupted_jobs()
        self.assertEqual(self.get(event_id), before)
        self.assertEqual(self.logs(event_id), [])

    def test_concurrent_recoveries_append_only_one_audit_record(self):
        event_id = self.event()
        barrier = threading.Barrier(2)

        def recover():
            barrier.wait(timeout=5)
            return recovery.recover_interrupted_jobs()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: recover(), range(2)))
        self.assertEqual(sum(result["failed"] for result in results), 1)
        self.assertEqual(len(self.logs(event_id)), 1)

    def test_recovery_waits_for_claim_transaction_and_observes_live_owner(self):
        event_id = self.event()
        started = threading.Event()

        def recover():
            started.set()
            return recovery.recover_interrupted_jobs()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with db.tx() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("UPDATE events SET worker_pid=222 WHERE id=?", (event_id,))
                future = pool.submit(recover)
                self.assertTrue(started.wait(5))
            result = future.result(timeout=5)
        self.assertEqual(result["skipped_live"], 1)
        self.assertEqual(self.get(event_id)["worker_pid"], 222)
        self.assertEqual(self.get(event_id)["status"], "pushing")
        self.assertEqual(self.logs(event_id), [])


class PidProbeTests(unittest.TestCase):
    def windows_api(self, *, handle=987, exit_code=259, query_ok=True, error=0):
        kernel = Mock()
        kernel.OpenProcess.return_value = handle

        def query(process, pointer):
            ctypes.cast(pointer, ctypes.POINTER(recovery.wintypes.DWORD))[0] = exit_code
            return query_ok

        kernel.GetExitCodeProcess.side_effect = query
        loader = patch.object(recovery.ctypes, "WinDLL", return_value=kernel, create=True)
        loader.start()
        self.addCleanup(loader.stop)
        last_error = patch.object(recovery.ctypes, "get_last_error", return_value=error, create=True)
        last_error.start()
        self.addCleanup(last_error.stop)
        return kernel

    def test_windows_live_and_dead_processes_close_the_handle(self):
        for exit_code, alive in ((259, True), (0, False), (7, False)):
            with self.subTest(exit_code=exit_code):
                kernel = self.windows_api(exit_code=exit_code)
                self.assertEqual(recovery._windows_pid_alive(123), alive)
                kernel.OpenProcess.assert_called_once_with(0x1000, False, 123)
                kernel.CloseHandle.assert_called_once_with(987)
                self.assertIs(kernel.OpenProcess.restype, recovery.wintypes.HANDLE)

    def test_windows_missing_process_is_dead_but_access_denied_is_live(self):
        for error, alive in ((87, False), (5, True), (0, True), (31, True)):
            with self.subTest(error=error):
                kernel = self.windows_api(handle=0, error=error)
                self.assertEqual(recovery._windows_pid_alive(123), alive)
                kernel.GetExitCodeProcess.assert_not_called()
                kernel.CloseHandle.assert_not_called()

    def test_windows_exit_code_query_failure_is_live_and_closes_handle(self):
        kernel = self.windows_api(query_ok=False)
        self.assertTrue(recovery._windows_pid_alive(123))
        kernel.CloseHandle.assert_called_once_with(987)

    def test_windows_dispatch_never_calls_os_kill(self):
        self.windows_api()
        with patch.object(recovery, "os") as operating_system:
            operating_system.name = "nt"
            self.assertTrue(recovery._pid_alive(123))
            operating_system.kill.assert_not_called()

    def test_posix_live_missing_and_permission_denied(self):
        for error, alive in ((None, True), (ProcessLookupError(errno.ESRCH, "gone"), False),
                             (PermissionError(errno.EPERM, "denied"), True),
                             (OSError(errno.EIO, "unknown"), True)):
            with self.subTest(error=error), patch.object(recovery.os, "kill", side_effect=error) as kill:
                self.assertEqual(recovery._posix_pid_alive(123), alive)
                kill.assert_called_once_with(123, 0)

    def test_missing_pid_is_dead_and_invalid_pid_never_probes_a_process_group(self):
        with patch.object(recovery, "_windows_pid_alive") as windows, \
                patch.object(recovery, "_posix_pid_alive") as posix:
            self.assertFalse(recovery._pid_alive(None))
            for pid in (0, -1, True, "123", "", 1.5):
                self.assertTrue(recovery._pid_alive(pid))
            with patch.object(recovery, "os") as operating_system:
                operating_system.name = "nt"
                self.assertTrue(recovery._pid_alive(0x100000000))
            windows.assert_not_called()
            posix.assert_not_called()

    def test_probe_error_is_conservatively_live(self):
        with patch.object(recovery, "_windows_pid_alive", side_effect=PermissionError), \
                patch.object(recovery, "_posix_pid_alive", side_effect=PermissionError):
            self.assertTrue(recovery._pid_alive(123))


if __name__ == "__main__":
    unittest.main()
