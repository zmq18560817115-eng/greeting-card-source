"""Run on the backend host to inspect field availability without modifying staff."""
import argparse
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description="只读检查飞书字段对应，输出人数和缺失原因，不输出员工资料或密钥。")
    parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.field_report import check_fields
    result = check_fields()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
