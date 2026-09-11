"""Prepare upcoming greetings without making HR open and approve every poster."""
from datetime import timedelta
from pathlib import Path

from . import db, pipeline, push
from .dates import parse_date


def prepare(limit=30):
    today = parse_date(db.now()[:10])
    end = today + timedelta(days=7)
    pipeline.scan_cycle((today, end))
    pending = db.query("""SELECT e.id FROM events e JOIN employees p ON p.id=e.employee_id
        WHERE p.active=1 AND e.event_date BETWEEN ? AND ? AND
        ((e.status='generating' AND e.generation_token IS NULL) OR e.status IN ('gen_failed','needs_regeneration'))
        ORDER BY e.event_date,e.id LIMIT ?""", (today.isoformat(), end.isoformat(), limit))
    for row in pending:
        try:
            pipeline.generate_for_event(row['id'])
        except Exception as exc:
            db.execute("UPDATE events SET last_error=?,updated_at=? WHERE id=? AND status NOT IN ('pushing','pushed','skipped','delivery_unknown')",
                       (str(exc)[:500], db.now(), row['id']))
    ready = db.query("SELECT id FROM events WHERE status='ready' AND event_date BETWEEN ? AND ? ORDER BY event_date,id LIMIT ?",
                     (today.isoformat(), end.isoformat(), limit))
    confirmed = 0
    for row in ready:
        try:
            with db.tx() as conn:
                conn.execute('BEGIN IMMEDIATE')
                current = conn.execute("SELECT * FROM events WHERE id=? AND status='ready'", (row['id'],)).fetchone()
                if not current:
                    continue
                if current['selected_card_id'] is None:
                    cards = conn.execute("SELECT * FROM cards WHERE event_id=? AND status='ok' ORDER BY idx", (row['id'],)).fetchall()
                    card = next((c for c in cards if c['file_path'] and Path(c['file_path']).is_file()), None)
                    if not card:
                        raise ValueError('海报不可用，请重新生成')
                    conn.execute("UPDATE events SET selected_card_id=? WHERE id=?", (card['id'], row['id']))
            push.confirm_event(row['id'], operator='system')
            confirmed += 1
        except Exception as exc:
            # Never overwrite a concurrent edit, skip or delivery claim.
            db.execute("UPDATE events SET status='blocked',last_error=?,updated_at=? WHERE id=? AND status='ready'",
                       ('自动排期暂停：' + str(exc)[:500], db.now(), row['id']))
    return {'generated_candidates': len(pending), 'scheduled': confirmed}
