"""按统一底图生成海报，保留员工快照并防止并发任务覆盖审核。"""
import json
import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import compose, doubao
from .dates import match_employee, next_cycle, parse_date
from .db import execute, now, query, tx
from .presentation import birthday_display
from .settings import BG_DIR, CARD_DIR, CARDS_PER_EVENT

log = logging.getLogger("pipeline")
TEMPLATE_OF = {"birthday": "birthday", "anniversary": "anniversary"}
_async_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="gen")
_async_tasks = {}


def employee_snapshot(emp):
    fields = ("id", "name", "department", "feishu_open_id", "join_date", "birth_date", "active")
    return json.dumps({k: emp.get(k) for k in fields}, sort_keys=True, ensure_ascii=False)


def active_employees():
    return query("SELECT * FROM employees WHERE active=1")


def scan_cycle(cycle=None, today=None, *, employee_id=None):
    if employee_id is not None and (isinstance(employee_id, bool) or not isinstance(employee_id, int) or employee_id <= 0):
        raise ValueError("请选择有效员工")
    start, end = cycle or next_cycle(today)
    created = []
    with tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        sql, params = "SELECT * FROM employees WHERE active=1", []
        if employee_id is not None:
            sql += " AND id=?"
            params.append(employee_id)
        rows = conn.execute(sql, params).fetchall()
        if employee_id is not None and not rows:
            raise ValueError("员工不存在或已离职，无法生成贺卡")
        for row in rows:
            emp = dict(row)
            for hit in match_employee(emp, start, end):
                cur = conn.execute(
                    """INSERT OR IGNORE INTO events(employee_id,event_type,event_date,trigger_at,
                       years,cycle_start,cycle_end,status,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,'generating',?,?)""",
                    (emp["id"], hit["event_type"], hit["event_date"], hit["trigger_at"],
                     hit["years"], start.isoformat(), end.isoformat(), now(), now()))
                if cur.rowcount:
                    created.append(cur.lastrowid)
    return created, start, end


def build_context(event, emp):
    day = parse_date(event["event_date"])
    if not day:
        raise ValueError("事件日期无效")
    return {"name": emp["name"], "department": emp.get("department") or "",
            "employee_id": emp.get("id") or "", "event_date": day.isoformat(),
            "years": event.get("years") if event.get("event_type") == "anniversary" and event.get("years") is not None else "", "date": day.isoformat(),
            "year": day.year, "month": day.month, "day": day.day,
            "join_date": emp.get("join_date") or "", "birth_date": birthday_display(emp.get("birth_date")) or ""}


def _claim_generation(event_id, count, cfg):
    with tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if not row:
            raise ValueError("事件不存在")
        event = dict(row)
        if event["status"] in ("pushing", "pushed", "skipped", "delivery_unknown"):
            raise ValueError("此事件不能重新生成")
        if event["status"] == "generating" and event["generation_token"]:
            raise ValueError("海报正在生成，请等待完成")
        emp = dict(conn.execute("SELECT * FROM employees WHERE id=?", (event["employee_id"],)).fetchone())
        if not emp["active"]:
            raise ValueError("员工已离职或停用")
        day = parse_date(event["event_date"])
        hit = next((h for h in match_employee(emp, day, day) if h["event_type"] == event["event_type"]), None)
        if not hit:
            raise ValueError("员工日期已变更，请跳过旧事件并重新扫描对应周期")
        event["years"] = hit["years"]
        tpl = cfg["templates"][TEMPLATE_OF[event["event_type"]]]
        count = (count if count is not None else CARDS_PER_EVENT) if (tpl.get("ai") or {}).get("enabled", False) else 1
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 10:
            raise ValueError("候选图数量应为1到10")
        token = uuid.uuid4().hex
        snapshot = employee_snapshot(emp)
        conn.execute("DELETE FROM cards WHERE event_id=?", (event_id,))
        conn.execute("""UPDATE events SET status='generating', generation_token=?,
                     selected_card_id=NULL,confirmed_at=NULL,confirmed_by=NULL,employee_snapshot=NULL,reviewed_at=NULL,reviewed_by=NULL,
                     delivery_uuid=NULL,delivery_mode=NULL,delivery_started_at=NULL,last_error=NULL,years=?,worker_pid=?,updated_at=? WHERE id=?""",
                     (token, event["years"], os.getpid(), now(), event_id))
        for idx in range(1, count + 1):
            conn.execute("""INSERT INTO cards(event_id,idx,mode,template_key,status,employee_snapshot,created_at)
                         VALUES(?,?,'compose',?,'generating',?,?)""",
                         (event_id, idx, TEMPLATE_OF[event["event_type"]], snapshot, now()))
    return event, emp, count, token


def _generate(event, emp, count, token, cfg):
    event_id = event["id"]
    tpl_key = TEMPLATE_OF[event["event_type"]]
    ai_cfg = cfg["templates"][tpl_key].get("ai") or {}
    ctx = build_context(event, emp)
    snapshot = employee_snapshot(emp)
    try:
        results = [{} for _ in range(count)]
        if ai_cfg.get("enabled", False):
            variables = {**cfg.get("vars", {}), **ctx}
            prompt = ai_cfg.get("prompt", "").format(**variables)
            prompts = [p.format(**variables) for p in ai_cfg.get("prompts", []) if p.strip()]
            base_image = cfg["templates"][tpl_key].get("base_image")
            reference = str(compose._abs(base_image)) if ai_cfg.get("use_base_as_reference") and base_image else None
            options = {"size": ai_cfg.get("size", "2K"), "reference_image": reference}
            if prompts:
                results = [doubao.generate_one(prompts[i % len(prompts)], **options) for i in range(count)]
            else:
                results = doubao.generate_batch(prompt, count=count, **options)
        success = 0
        for idx in range(1, count + 1):
            out = CARD_DIR / f"ev{event_id}_{token}_{idx}.png"
            item = results[idx - 1]
            bg_path, error = None, None
            try:
                if item.get("error"):
                    raise ValueError(item["error"])
                if item.get("url"):
                    bg_path = BG_DIR / f"ev{event_id}_{token}_{idx}.png"
                    doubao.download(item["url"], bg_path)
                compose.render(tpl_key, ctx, ai_image_path=bg_path, out_path=out, cfg=cfg)
            except Exception as exc:
                error = str(exc)[:1000]
            with tx() as conn:
                conn.execute("BEGIN IMMEDIATE")
                current = dict(conn.execute("SELECT * FROM employees WHERE id=?", (emp["id"],)).fetchone())
                ev = conn.execute("SELECT status,generation_token FROM events WHERE id=?", (event_id,)).fetchone()
                if ev["status"] != "generating" or ev["generation_token"] != token or employee_snapshot(current) != snapshot:
                    return 0
                conn.execute("""UPDATE cards SET file_path=?,bg_url=?,bg_path=?,status=?,error=?
                             WHERE event_id=? AND idx=?""",
                             (str(out) if not error else None, item.get("url"), str(bg_path) if bg_path else None,
                              "failed" if error else "ok", error, event_id, idx))
            success += int(error is None)
        execute("""UPDATE events SET status=?,generation_token=NULL,worker_pid=NULL,updated_at=?
                   WHERE id=? AND status='generating' AND generation_token=?""",
                ("ready" if success else "gen_failed", now(), event_id, token))
        return success
    except Exception as exc:
        execute("""UPDATE events SET status='gen_failed',generation_token=NULL,worker_pid=NULL,last_error=?,updated_at=?
                   WHERE id=? AND status='generating' AND generation_token=?""", (str(exc)[:1000], now(), event_id, token))
        log.exception("生成失败 event=%s", event_id)
        return 0


def generate_for_event(event_id, count=None, cfg=None):
    cfg = cfg or compose.load_config()
    return _generate(*_claim_generation(event_id, count, cfg), cfg)


def submit_generate(event_id, count=None):
    cfg = compose.load_config()
    claim = _claim_generation(event_id, count, cfg)
    try:
        _async_pool.submit(_generate, *claim, cfg)
    except Exception:
        execute("""UPDATE events SET status='gen_failed',generation_token=NULL,worker_pid=NULL,
                   last_error='后台生成任务提交失败',updated_at=? WHERE id=? AND generation_token=?""",
                (now(), event_id, claim[3]))
        raise


def _pending_generation(start, end, *, employee_id=None):
    sql = """SELECT e.id FROM events e JOIN employees emp ON emp.id=e.employee_id
                WHERE emp.active=1 AND e.event_date BETWEEN ? AND ?
                AND ((e.status='generating' AND e.generation_token IS NULL) OR e.status='gen_failed')"""
    params = [start.isoformat(), end.isoformat()]
    if employee_id is not None:
        sql += " AND e.employee_id=?"
        params.append(employee_id)
    return [row["id"] for row in query(sql + " ORDER BY e.id", params)]


def run_weekly_async(cycle=None, today=None, *, employee_id=None):
    created, start, end = scan_cycle(cycle, today, employee_id=employee_id)
    pending = _pending_generation(start, end, employee_id=employee_id)
    tid = uuid.uuid4().hex
    task = {"status": "running", "created": len(created), "total": len(pending), "events": pending,
            "cycle": [start.isoformat(), end.isoformat()], "done": 0, "generated": 0, "errors": []}
    _async_tasks[tid] = task
    def work():
        for eid in pending:
            try:
                generated = bool(generate_for_event(eid))
                task["generated"] += int(generated)
                if not generated:
                    task["errors"].append({"event_id": eid, "msg": "海报未成功生成，请查看该事件的异常原因"})
            except Exception as exc:
                task["errors"].append({"event_id": eid, "msg": str(exc)})
            task["done"] += 1
        task["status"] = "done"
    try:
        _async_pool.submit(work)
    except Exception:
        task["status"] = "failed"
        task["error"] = "后台生成任务提交失败，请稍后重新扫描"
        raise ValueError(task["error"])
    return tid, task


def async_task(task_id):
    return _async_tasks.get(task_id)


def run_weekly(cycle=None, today=None):
    created, start, end = scan_cycle(cycle, today)
    success = 0
    pending = _pending_generation(start, end)
    for eid in pending:
        try:
            success += int(bool(generate_for_event(eid)))
        except Exception:
            log.exception("生成失败 event=%s", eid)
    return {"cycle_start": start.isoformat(), "cycle_end": end.isoformat(),
            "created": len(created), "total": len(pending), "generated": success}
