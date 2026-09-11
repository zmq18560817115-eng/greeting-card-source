"""A high-entropy, expiring link grants access to one delivered poster only."""
import hashlib
import hmac
import re
import secrets
import time
from pathlib import Path

from . import db, delivery_config, feishu

VALID_SECONDS = 30 * 86400


def issue(event, card, employee, delivery_uuid):
    base = delivery_config.validate_base_url(delivery_config.current()["base_url"])
    with db.tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT OR IGNORE INTO kv(key,value) VALUES('poster_link_secret',?)", (secrets.token_hex(32),))
        secret = conn.execute("SELECT value FROM kv WHERE key='poster_link_secret'").fetchone()["value"]
        content = f"{event['id']}:{card['id']}:{delivery_uuid}:{feishu.FEISHU_APP_ID}:{employee['feishu_open_id']}"
        token = hmac.new(secret.encode(), content.encode(), hashlib.sha256).hexdigest()
        digest = hashlib.sha256(token.encode()).hexdigest()
        # The same delivery always reuses its link and expiry, including uncertain receipts.
        conn.execute("""INSERT OR IGNORE INTO poster_links
            (token_hash,event_id,card_id,delivery_uuid,app_id,open_id,expires) VALUES(?,?,?,?,?,?,?)""",
            (digest, event['id'], card['id'], delivery_uuid, feishu.FEISHU_APP_ID,
             employee['feishu_open_id'], int(time.time()) + VALID_SECONDS))
    return f"{base}/greeting/{token}"


def resolve(token):
    if not re.fullmatch(r"[a-f0-9]{64}", token):
        return None
    row = db.query_one("""SELECT l.*,c.file_path,c.status AS card_status,
            e.status,e.selected_card_id,e.delivery_uuid AS current_uuid,e.confirmed_app_id,
            emp.active,emp.feishu_open_id FROM poster_links l
            JOIN events e ON e.id=l.event_id JOIN cards c ON c.id=l.card_id AND c.event_id=e.id
            JOIN employees emp ON emp.id=e.employee_id WHERE l.token_hash=?""",
            (hashlib.sha256(token.encode()).hexdigest(),))
    if (not row or row['expires'] <= time.time() or row['status'] not in ('pushing', 'pushed', 'delivery_unknown')
            or row['card_status'] != 'ok' or row['selected_card_id'] != row['card_id']
            or row['current_uuid'] != row['delivery_uuid'] or row['active'] != 1
            or row['feishu_open_id'] != row['open_id']
            or row['app_id'] != row['confirmed_app_id'] or row['app_id'] != feishu.FEISHU_APP_ID):
        return None
    path = Path(row['file_path'] or '')
    return path if path.is_file() else None
