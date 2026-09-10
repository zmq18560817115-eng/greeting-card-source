"""手动从飞书同步通讯录。用法： python scripts/sync_feishu.py"""
import logging
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
from app import sync  # noqa: E402
from app.db import init_db  # noqa: E402

init_db()
print(sync.sync_from_feishu())
for row in sync.missing_report():
    print("  资料不全:", row["name"], row["join_date"], row["birth_date"], row["feishu_open_id"])
