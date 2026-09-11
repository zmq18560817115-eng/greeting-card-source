"""确认后投递，发送前实时检查姓名与飞书 ID，并对并发发送和不确定回执做保护。"""
import json
import logging
import os
import uuid
from pathlib import Path

from . import employees, feishu
from .db import execute, now, query, tx
from .pipeline import employee_snapshot
from .settings import DRY_RUN, MAX_PUSH_ATTEMPTS

log = logging.getLogger("push")
CARD_TITLE = {"birthday": "生日贺卡", "anniversary": "入职周年贺卡"}


def _review_data(conn, event):
    row = conn.execute("SELECT * FROM employees WHERE id=?", (event["employee_id"],)).fetchone()
    if not row or not row["active"]:
        raise ValueError("员工已离职或停用，不能发送")
    emp = dict(row)
    card_row = conn.execute("SELECT * FROM cards WHERE id=? AND event_id=?",
                            (event["selected_card_id"], event["id"])).fetchone()
    if not card_row or card_row["status"] != "ok" or not card_row["file_path"] or not Path(card_row["file_path"]).is_file():
        raise ValueError("请先选定该事件的有效海报")
    card = dict(card_row)
    if card.get("employee_snapshot") != employee_snapshot(emp):
        raise ValueError("员工资料与海报不一致，请重新生成并审核")
    return emp, card


def confirm_event(event_id, operator="hr"):
    # 外部核验期间不持有数据库锁，写入前再核对快照和选图。
    with tx() as conn:
        row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if not row:
            raise ValueError("事件不存在")
        event = dict(row)
        if event["status"] != "ready":
            raise ValueError("仅待审核的海报可以确认")
        if event["event_date"] < now()[:10]:
            raise ValueError("事件日期已过期，不能排入推送")
        emp, card = _review_data(conn, event)
    result = employees.validate_identity(emp)
    if not result["ok"]:
        raise ValueError(result.get("error") or "姓名与飞书ID对应未通过")
    with tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = dict(conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone())
        current_emp, current_card = _review_data(conn, current)
        if current["status"] != "ready" or current_card["id"] != card["id"] or employee_snapshot(current_emp) != employee_snapshot(emp):
            raise ValueError("审核期间资料或海报已变更，请刷新后重试")
        conn.execute("""UPDATE events SET status='confirmed',confirmed_by=?,confirmed_at=?,
                      employee_snapshot=?,delivery_uuid=?,push_attempts=0,last_error=NULL,updated_at=? WHERE id=?""",
                     (operator, now(), employee_snapshot(emp), uuid.uuid4().hex, now(), event_id))
    return {"ok": True, "trigger_at": event["trigger_at"]}


def push_event(event_id, operator="auto", force=False, with_text=True):
    """force 只跳过计划时间，不能跳过审核、身份校验或已发送保护。"""
    with tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if not row:
            raise ValueError("事件不存在")
        event = dict(row)
        if event["status"] == "pushed":
            return {"ok": True, "msg": "已推送过，跳过", "already_sent": True}
        if event["status"] not in ("confirmed", "failed") or not event["confirmed_at"]:
            return {"ok": False, "msg": "请先完成审核；发送中、已取消或回执不明的任务不可重复发送"}
        if event["event_date"] < now()[:10]:
            conn.execute("UPDATE events SET status='expired',last_error='事件日期已过期',updated_at=? WHERE id=?", (now(), event_id))
            return {"ok": False, "msg": "事件日期已过期，已停止推送"}
        if not force and event["trigger_at"] > now():
            return {"ok": False, "msg": "尚未到计划推送时间"}
        if not force and event["push_attempts"] >= MAX_PUSH_ATTEMPTS:
            return {"ok": False, "msg": "已达到自动重试次数上限"}
        try:
            emp, card = _review_data(conn, event)
            if event["employee_snapshot"] != employee_snapshot(emp):
                raise ValueError("确认后员工资料已变更，请重新审核")
        except ValueError as exc:
            conn.execute("UPDATE events SET status='blocked',last_error=?,updated_at=? WHERE id=?",
                         (str(exc), now(), event_id))
            return {"ok": False, "msg": str(exc)}
        attempt = event["push_attempts"] + 1
        delivery_uuid = event["delivery_uuid"] or uuid.uuid4().hex
        conn.execute("""UPDATE events SET status='pushing',push_attempts=?,delivery_uuid=?,worker_pid=?,
                      delivery_started_at=NULL,updated_at=? WHERE id=?""",
                     (attempt, delivery_uuid, os.getpid(), now(), event_id))
    image_key = message_id = None
    send_started = False
    evidence = None
    status = "failed"
    try:
        result = employees.validate_identity(emp)
        if not result["ok"]:
            status = "blocked"
            raise ValueError(result.get("error") or "姓名与飞书ID对应失败")
        evidence = result.get("evidence") or result
        if DRY_RUN:
            status = "simulated"
            image_key = message_id = "DRY_RUN"
        else:
            image_key = feishu.upload_image(card["file_path"])
            # 上传可能耗时，发送前再次核验在职状态及姓名与飞书 ID。
            result = employees.validate_identity(emp)
            if not result["ok"]:
                status = "blocked"
                raise ValueError(result.get("error") or "发送前身份核验失败")
            evidence = result.get("evidence") or result
            if event["event_date"] < now()[:10]:
                status = "expired"
                raise ValueError("上传期间事件日期已过期，停止发送")
            execute("UPDATE events SET delivery_started_at=?,updated_at=? WHERE id=? AND status='pushing'",
                    (now(), now(), event_id))
            send_started = True
            message_id = feishu.send_card(
                emp["feishu_open_id"], CARD_TITLE[event["event_type"]],
                "为你准备了一份专属贺卡，点击下方海报查看大图。",
                image_key, uuid=delivery_uuid)
            status = "pushed"
        with tx() as conn:
            conn.execute("""UPDATE events SET status=?,pushed_at=?,worker_pid=NULL,last_error=NULL,updated_at=? WHERE id=?""",
                         (status, now() if status == "pushed" else None, now(), event_id))
            conn.execute("""INSERT INTO push_logs(event_id,card_id,attempt,status,image_key,message_id,
                         operator,recipient_snapshot,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                         (event_id, card["id"], attempt, "success" if status == "pushed" else "simulated",
                          image_key, message_id, operator, json.dumps(evidence, ensure_ascii=False), now()))
        return {"ok": True, "message_id": message_id, "dry_run": DRY_RUN,
                "msg": "演练完成，未向飞书发送消息" if DRY_RUN else "推送成功"}
    except Exception as exc:
        # 发送请求一旦发出，超时也可能已送达，不自动重发以免员工收到两份。
        if send_started and not (isinstance(exc, feishu.FeishuError) and exc.definitive):
            status = "delivery_unknown"
        elif isinstance(exc, employees.IdentityError):
            status = "blocked"
        error = str(exc)[:1000]
        with tx() as conn:
            conn.execute("UPDATE events SET status=?,worker_pid=NULL,last_error=?,updated_at=? WHERE id=?",
                         (status, error, now(), event_id))
            conn.execute("""INSERT INTO push_logs(event_id,card_id,attempt,status,image_key,error,
                          operator,recipient_snapshot,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                         (event_id, card["id"], attempt, status, image_key, error, operator,
                          json.dumps(evidence or {"employee": json.loads(employee_snapshot(emp))}, ensure_ascii=False), now()))
        log.warning("推送终止 event=%s status=%s: %s", event_id, status, error)
        return {"ok": False, "msg": error, "status": status}


def due_events(limit=50):
    execute("""UPDATE events SET status='expired',last_error='事件日期已过期',updated_at=?
               WHERE status IN ('confirmed','failed') AND event_date < ?""", (now(), now()[:10]))
    return query("""SELECT e.* FROM events e JOIN employees emp ON emp.id=e.employee_id
                 WHERE emp.active=1 AND e.trigger_at <= ? AND e.confirmed_at IS NOT NULL
                 AND (e.status='confirmed' OR (e.status='failed' AND e.push_attempts < ?))
                 AND e.selected_card_id IS NOT NULL ORDER BY e.trigger_at LIMIT ?""",
                 (now(), MAX_PUSH_ATTEMPTS, limit))
