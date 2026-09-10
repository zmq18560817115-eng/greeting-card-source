"""塞几条演示数据，方便本地先把流程跑通。"""
import sys
from datetime import date, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.db import execute, init_db, now  # noqa: E402
from app.dates import next_cycle  # noqa: E402

init_db()
s, _ = next_cycle()
people = [
    ("王小明", "2026-03-25", (s + timedelta(days=1)).replace(year=1995).isoformat(), "ou_demo_0001"),
    ("李静",   (s + timedelta(days=2)).replace(year=2021).isoformat(), "1993-01-08", "ou_demo_0002"),
    ("欧阳娜娜十个字", "2019-01-02", (s + timedelta(days=4)).replace(year=1990).isoformat(), "ou_demo_0003"),
]
for name, join, birth, oid in people:
    execute("""INSERT OR IGNORE INTO employees(name,join_date,birth_date,feishu_open_id,
               department,active,source,created_at,updated_at)
               VALUES(?,?,?,?, '演示部门',1,'local',?,?)""", (name, join, birth, oid, now(), now()))
print("演示数据已写入；下个周期 =", next_cycle())
