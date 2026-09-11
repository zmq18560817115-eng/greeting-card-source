"""Deliver one banner notification and retain strict recipient/receipt protection."""
import json
from pathlib import Path

from . import db, employees, feishu, poster_links
from .settings import TEMPLATE_DIR


def deliver(event, employee, card, delivery_uuid, attempt, operator, *, dry_run, clock):
    sent_request = False
    image_key = message_id = None
    evidence = None
    status = "failed"
    try:
        result = employees.validate_identity(employee)
        if not result['ok']:
            status = 'blocked'
            raise ValueError(result.get('error') or '姓名与 open_id 对应失败，停止推送')
        evidence = result.get('evidence') or result
        if dry_run:
            status = 'simulated'
            message_id = image_key = 'DRY_RUN'
        else:
            poster_url = poster_links.issue(event, card, employee, delivery_uuid)
            banner = Path(TEMPLATE_DIR) / f"{event['event_type']}_banner.png"
            if not banner.is_file():
                raise ValueError('横版贺卡封面缺失，请恢复模板资源')
            image_key = feishu.upload_image(banner)
            result = employees.validate_identity(employee)
            if not result['ok']:
                status = 'blocked'
                raise ValueError(result.get('error') or '发送前收件人对应失败，停止推送')
            evidence = result.get('evidence') or result
            if feishu.FEISHU_APP_ID != event['confirmed_app_id']:
                status = 'blocked'
                raise ValueError('飞书应用已变化，停止推送')
            if clock()[:10] != event['event_date']:
                status = 'expired'
                raise ValueError('已超过贺卡日期，停止推送')
            db.execute("UPDATE events SET delivery_started_at=?,updated_at=? WHERE id=? AND status='pushing'",
                       (clock(), clock(), event['id']))
            sent_request = True
            title = '生日贺卡' if event['event_type'] == 'birthday' else '入职周年贺卡'
            message_id = feishu.send_greeting(employee['feishu_open_id'], employee['name'], title,
                                              image_key, poster_url, uuid=delivery_uuid)
            status = 'pushed'
        with db.tx() as conn:
            conn.execute("UPDATE events SET status=?,pushed_at=?,worker_pid=NULL,last_error=NULL,updated_at=? WHERE id=?",
                         (status, clock() if status == 'pushed' else None, clock(), event['id']))
            conn.execute("""INSERT INTO push_logs(event_id,card_id,attempt,status,image_key,message_id,
                         operator,recipient_snapshot,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                         (event['id'], card['id'], attempt, 'success' if status == 'pushed' else 'simulated',
                          image_key, message_id, operator, json.dumps(evidence, ensure_ascii=False), clock()))
        return {'ok': True, 'message_id': message_id, 'dry_run': dry_run,
                'msg': '演练完成，未向飞书发送消息' if dry_run else '图文通知已发送，点击整条消息即可查看完整海报'}
    except Exception as exc:
        if sent_request and not (isinstance(exc, feishu.FeishuError) and exc.definitive):
            status = 'delivery_unknown'
        elif isinstance(exc, employees.IdentityError):
            status = 'blocked'
        error = ('图文通知发送结果待确认：' if status == 'delivery_unknown' else '图文通知未发送：') + feishu.connection_error(exc)
        with db.tx() as conn:
            conn.execute("UPDATE events SET status=?,worker_pid=NULL,last_error=?,updated_at=? WHERE id=?",
                         (status, error, clock(), event['id']))
            conn.execute("""INSERT INTO push_logs(event_id,card_id,attempt,status,image_key,error,
                         operator,recipient_snapshot,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                         (event['id'], card['id'], attempt, status, image_key, error, operator,
                          json.dumps(evidence, ensure_ascii=False), clock()))
        return {'ok': False, 'msg': error, 'status': status}
