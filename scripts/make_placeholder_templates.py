"""生成两张占位底图，方便在拿到公司正式模板前先跑通全流程。
拿到正式模板后，直接把 assets/templates/*.png 换掉即可（尺寸建议 1080x1440）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PIL import Image, ImageDraw  # noqa: E402

from app.settings import TEMPLATE_DIR  # noqa: E402

W, H = 1080, 1440


def make(path, top, bottom, accent):
    img = Image.new("RGB", (W, H), bottom)
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)],
               fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    d.rounded_rectangle([40, 40, W - 40, H - 40], radius=48, outline=accent, width=6)
    d.rounded_rectangle([60, 60, W - 60, 880], radius=32, fill=(240, 240, 240))
    img.save(path)
    print("生成占位模板:", path)


if __name__ == "__main__":
    make(TEMPLATE_DIR / "birthday_base.png", (255, 246, 235), (255, 228, 216), (224, 85, 61))
    make(TEMPLATE_DIR / "anniversary_base.png", (243, 247, 255), (222, 232, 248), (200, 149, 47))
