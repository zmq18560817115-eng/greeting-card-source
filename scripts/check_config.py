"""自检默认只检查本地配置；加 --online 检查飞书，不产生消息或收费生图。"""
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import compose, feishu, fonts

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--online", action="store_true", help="额外检查飞书应用凭据")
    args = parser.parse_args()
    try:
        cfg = compose.load_config()
        print("中文字体:", fonts.resolve("auto"))
        for key, tpl in cfg["templates"].items():
            print("模板:", key, tpl.get("base_image") or "纯色画布",
                  "AI开启" if tpl.get("ai", {}).get("enabled") else "固定底图")
        if args.online:
            feishu.ping()
            print("飞书应用凭据可用；员工核验与机器人收件权限需在后台分别检查。")
        print("配置检查通过")
        return 0
    except Exception as exc:
        print("配置检查失败:", exc)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
