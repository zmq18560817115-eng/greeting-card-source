"""手动跑一次周期任务（等价于周四晚上的定时任务）。
用法： python scripts/run_weekly.py [next|this]"""
import logging
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
from app import pipeline  # noqa: E402
from app.dates import next_cycle, this_cycle  # noqa: E402
from app.db import init_db  # noqa: E402

init_db()
scope = sys.argv[1] if len(sys.argv) > 1 else "next"
print(pipeline.run_weekly(cycle=this_cycle() if scope == "this" else next_cycle()))
