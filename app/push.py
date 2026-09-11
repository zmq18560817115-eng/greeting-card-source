"""确认后投递，发送前实时检查姓名与飞书 ID，并对并发发送和不确定回执做保护。"""
import json
import logging
import os
import uuid
from pathlib import Path

from . import compact_delivery, employees, feishu
from .db import execute, now, query, tx
from .dates import match_employee, parse_date
from .pipeline import employee_snapshot
from .settings import DELIVERY_MODE, DRY_RUN, MAX_PUSH_ATTEMPTS

log = logging.getLogger("push")
CARD_TITLE = {"birthday": "生日贺卡", "anniversary": "入职周年贺卡"}


def _review_data(conn, event):
    row = conn.execute("SELECT * FROM employees WHERE id=?", (event["employee_id"],)).fetchone()
    if not row or not row["active"]:
        raise ValueError("员工已离职或停用，不能发送")
    emp = dict(row)
    day = parse_date(event.get("event_date"))
    hit = next((item for item in match_employee(emp, day, day)
                if item["event_type"] == event["event_type"]), None) if day else None
    if not hit:
        raise ValueError("贺卡日期与员工生日月日或完整入职日期不符，请跳过旧事件并重新扫描")
    if event.get("years") != hit["years"]:
        raise ValueError("贺卡仍使用旧年龄或周年数，请重新生成并审核；生日不计算年龄")
    card_row = conn.execute("SELECT * FROM cards WHERE id=? AND event_id=?",
                            (event["selected_card_id"], event["id"])).fetchone()
    if not card_row or card_row["status"] != "ok" or not card_row["file_path"] or not Path(card_row["file_path"]).is_file():
        raise ValueError("请先选定该事件的有效海报")
    card = dict(card_row)
    if card.get("employee_snapshot") != employee_snapshot(emp):
        raise ValueError("员工资料与海报不一致，请重新生成并审核")
    return emp, card


@feishu.in_application
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
                      employee_snapshot=?,delivery_uuid=?,confirmed_app_id=?,push_attempts=0,last_error=NULL,updated_at=? WHERE id=?""",
                     (operator, now(), employee_snapshot(emp), uuid.uuid4().hex, feishu.FEISHU_APP_ID, now(), event_id))
    return {"ok": True, "trigger_at": event["trigger_at"]}


@feishu.in_application
def push_event(event_id, operator="auto", force=False, with_text=True):
    """force 只跳过当天的计划时刻，不能跨日期或跳过审核、身份校验。"""
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
        if event["event_date"] > now()[:10]:
            return {"ok": False, "msg": "未到贺卡日期，仅可在生日或入职周年当天推送"}
        if not force and event["trigger_at"] > now():
            return {"ok": False, "msg": "尚未到计划推送时间"}
        if not force and event["push_attempts"] >= MAX_PUSH_ATTEMPTS:
            return {"ok": False, "msg": "已达到自动重试次数上限"}
        try:
            if not event.get("confirmed_app_id") or event["confirmed_app_id"] != feishu.FEISHU_APP_ID:
                raise ValueError("审核记录未绑定当前飞书应用，请重新生成并确认排期；不会沿用其他应用的 open_id")
            emp, card = _review_data(conn, event)
            if event["employee_snapshot"] != employee_snapshot(emp):
                raise ValueError("确认后员工资料已变更，请重新审核")
        except ValueError as exc:
            conn.execute("UPDATE events SET status='blocked',last_error=?,updated_at=? WHERE id=?",
                         (str(exc), now(), event_id))
            return {"ok": False, "msg": str(exc), "status": "blocked"}
        attempt = event["push_attempts"] + 1
        delivery_uuid = event["delivery_uuid"] or uuid.uuid4().hex
        mode = event.get('delivery_mode') or ('notice_then_full_card' if
            event.get('notice_message_id') and event.get('notice_delivery_uuid') == delivery_uuid else DELIVERY_MODE)
        conn.execute("""UPDATE events SET status='pushing',push_attempts=?,delivery_uuid=?,worker_pid=?,delivery_mode=?,
                      delivery_started_at=NULL,updated_at=? WHERE id=?""",
                     (attempt, delivery_uuid, os.getpid(), mode, now(), event_id))
    if mode == 'compact_link':
        return compact_delivery.deliver(event, emp, card, delivery_uuid, attempt, operator, dry_run=DRY_RUN, clock=now)
    image_key = message_id = None
    send_started = False
    evidence = None
    status = "failed"
    app_id = feishu.FEISHU_APP_ID
    notice_sent = bool(event.get("notice_message_id") and event.get("notice_delivery_uuid") == delivery_uuid)
    stage = "完整贺卡" if notice_sent else "简短通知"
    try:
        if notice_sent and (event.get("notice_app_id"), event.get("notice_open_id")) != (app_id, emp["feishu_open_id"]):
            status = "blocked"
            raise ValueError("通知发送后飞书应用或收件人已变化，请联系管理员核对")
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
            if feishu.FEISHU_APP_ID != app_id:
                status = "blocked"
                raise ValueError("发送期间飞书应用已变化，请重新检查连接")
            if not notice_sent:
                # Each stage has a stable, distinct UUID. Persist the first receipt
                # before starting the second request, including crash recovery.
                execute("UPDATE events SET delivery_started_at=?,updated_at=? WHERE id=? AND status='pushing'",
                        (now(), now(), event_id))
                send_started = True
                notice_id = feishu.send_notice(emp["feishu_open_id"], CARD_TITLE[event["event_type"]],
                    uuid=uuid.uuid5(uuid.NAMESPACE_URL, delivery_uuid + ":notice").hex)
                with tx() as conn:
                    timestamp = now()
                    conn.execute("""UPDATE events SET notice_message_id=?,notice_delivery_uuid=?,
                                 notice_app_id=?,notice_open_id=?,notice_sent_at=?,
                                 delivery_started_at=NULL,updated_at=? WHERE id=?""",
                                 (notice_id, delivery_uuid, app_id, emp["feishu_open_id"], timestamp, timestamp, event_id))
                    conn.execute("""INSERT INTO push_logs(event_id,card_id,attempt,status,message_id,
                                 operator,recipient_snapshot,created_at) VALUES(?,?,?,'notice_success',?,?,?,?)""",
                                 (event_id, card["id"], attempt, notice_id, operator,
                                  json.dumps(evidence, ensure_ascii=False), timestamp))
                notice_sent = True
                send_started = False
            stage = "完整贺卡"
            # A recipient may leave or become unavailable between the two messages.
            result = employees.validate_identity(emp)
            if not result["ok"]:
                status = "blocked"
                raise ValueError(result.get("error") or "完整贺卡发送前身份核验失败")
            evidence = result.get("evidence") or result
            if feishu.FEISHU_APP_ID != app_id:
                status = "blocked"
                raise ValueError("通知发送后飞书应用已变化，请联系管理员核对")
            if event["event_date"] < now()[:10]:
                status = "expired"
                raise ValueError("事件日期已过期，停止发送完整贺卡")
            execute("UPDATE events SET delivery_started_at=?,updated_at=? WHERE id=? AND status='pushing'",
                    (now(), now(), event_id))
            send_started = True
            message_id = feishu.send_full_card(emp["feishu_open_id"], CARD_TITLE[event["event_type"]],
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
                "msg": "演练完成，未向飞书发送消息" if DRY_RUN else "简短通知和完整贺卡已依次发送"}
    except Exception as exc:
        # 发送请求一旦发出，超时也可能已送达，不自动重发以免员工收到两份。
        if send_started and not (isinstance(exc, feishu.FeishuError) and exc.definitive):
            status = "delivery_unknown"
        elif isinstance(exc, employees.IdentityError):
            status = "blocked"
        detail = str(exc)[:1000]
        if notice_sent:
            error = "简短通知已发送；完整贺卡" + ("发送结果待确认：" if status == "delivery_unknown" else "未发送成功：") + detail
        else:
            error = stage + ("发送结果待确认：" if status == "delivery_unknown" else "未发送成功：") + detail
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
               WHERE status IN ('ready','confirmed','failed','gen_failed','needs_regeneration','blocked') AND event_date < ?""", (now(), now()[:10]))
    return query("""SELECT e.* FROM events e JOIN employees emp ON emp.id=e.employee_id
                 WHERE emp.active=1 AND e.trigger_at <= ? AND e.confirmed_at IS NOT NULL
                 AND (e.status='confirmed' OR (e.status='failed' AND e.push_attempts < ?))
                 AND e.selected_card_id IS NOT NULL ORDER BY e.trigger_at LIMIT ?""",
                 (now(), MAX_PUSH_ATTEMPTS, limit))
