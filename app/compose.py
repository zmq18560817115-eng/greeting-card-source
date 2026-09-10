"""固定设计底图 + Pillow 可配置文字；内存预览与正式输出共用排版逻辑。

render(key, ctx, ai_image_path=None, out_path=None, cfg=None) -> Path
render_image(key, ctx, ai_image_path=None, cfg=None) -> PIL.Image (RGB)
render_png(key, ctx, ai_image_path=None, cfg=None) -> bytes

cfg 非 None 时只读取传入配置，不读取或写入全局模板文件。无法完整容纳的
文字抛 LayoutError，文件输出在整张图渲染成功后才原子替换。max_width/
max_height 限制整块文字（含描边、阴影），未指定时仍检查画布边界。
"""
from dataclasses import dataclass
from io import BytesIO
import math
from pathlib import Path
import unicodedata

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from . import fonts, templates
from .settings import TEMPLATE_CONFIG

_CN_DIGITS = "零一二三四五六七八九"


class LayoutError(ValueError):
    """指定的文字无法在最小字号下完整放入可用区域。"""


def _cn_num(n):
    n = int(n)
    if n < 0:
        return str(n)
    if n < 10:
        return _CN_DIGITS[n]
    if n < 20:
        return "十" + (_CN_DIGITS[n % 10] if n % 10 else "")
    if n < 100:
        return _CN_DIGITS[n // 10] + "十" + (_CN_DIGITS[n % 10] if n % 10 else "")
    return str(n)


def load_config(path=None, *, check_assets=True):
    """兼容原有 pipeline 入口。"""
    return templates.load_config(TEMPLATE_CONFIG if path is None else path, check_assets=check_assets)


def _abs(p):
    return templates.asset_path(p)


def _hex(c):
    return templates.parse_color(c)


def _load_font(spec, size, bold=False):
    path = fonts.resolve(spec, bold=bold)
    return ImageFont.truetype(path, size, index=fonts.font_index(path, bold))


def _clusters(text):
    """常见组合音标/变体选择符/ZWJ 不单独换行；中文和长单词均可逐字折行。"""
    result = []
    for ch in text:
        if result and (unicodedata.combining(ch) or ch in "\ufe0e\ufe0f\u200d"
                       or result[-1].endswith("\u200d")):
            result[-1] += ch
        else:
            result.append(ch)
    return result


@dataclass
class _Line:
    text: str
    runs: list
    left: float
    top: float
    right: float
    bottom: float

    @property
    def width(self):
        return self.right - self.left


@dataclass
class _TextLayout:
    font: object
    size: int
    lines: list
    positions: list
    bbox: tuple


class _Measure:
    def __init__(self, draw, font, spacing=0, stroke=0):
        self.draw, self.font = draw, font
        self.spacing, self.stroke = spacing, stroke
        self.cache = {}

    def line(self, text):
        if text in self.cache:
            return self.cache[text]
        # 无字距时整句绘制保留 kerning/shaping；有字距时测量和绘制同一组 runs。
        pieces = _clusters(text) if self.spacing else [text]
        runs, boxes, cursor = [], [], 0.0
        for i, piece in enumerate(pieces):
            runs.append((piece, cursor))
            boxes.append(self.draw.textbbox((cursor, 0), piece, font=self.font,
                                            anchor="ls", stroke_width=self.stroke))
            cursor += self.draw.textlength(piece, font=self.font)
            if i < len(pieces) - 1:
                cursor += self.spacing
        if not text or text.isspace():
            ascent, descent = self.font.getmetrics()
            boxes = [(0, -ascent, cursor, descent)]
        left = min(0, min(box[0] for box in boxes))
        right = max(cursor, max(box[2] for box in boxes))
        result = _Line(text, runs, left, min(box[1] for box in boxes),
                       right, max(box[3] for box in boxes))
        self.cache[text] = result
        return result

    def wrap(self, text, width):
        lines = []
        for paragraph in text.split("\n"):
            current = ""
            for ch in _clusters(paragraph):
                trial = current + ch
                if current and self.line(trial).width > width:
                    previous = _clusters(current)
                    if ch in "，。！？、；：）》】」』”’…％%!?;:,." and len(previous) > 1:
                        # Keep closing punctuation with the preceding character.
                        carry = previous[-1] + ch
                        if self.line(carry).width <= width:
                            lines.append(self.line("".join(previous[:-1])))
                            current = carry
                            continue
                    lines.append(self.line(current))
                    current = ch
                else:
                    current = trial
            lines.append(self.line(current))
        return lines


def _text_width(draw, text, font, spacing=0):
    return _Measure(draw, font, spacing).line(text).width


def _wrap_chars(draw, text, font, max_width, spacing=0):
    return [line.text for line in _Measure(draw, font, spacing).wrap(text, max_width)]


def _shadow_padding(layer):
    sh = layer.get("shadow")
    if not sh:
        return (0, 0, 0, 0)
    # Pillow 的 GaussianBlur 使用有限支撑核；3 sigma 保守预留阴影空间。
    margin = math.ceil(3 * sh.get("blur", 6))
    dx, dy = sh.get("x", 0), sh.get("y", 4)
    return (max(0, margin - dx), max(0, margin - dy),
            max(0, margin + dx), max(0, margin + dy))


def _layout_text(draw, layer, text, canvas_size, *, flow_top=None):
    width, height = canvas_size
    is_paragraph = layer.get("type", "text") == "paragraph"
    preferred = layer.get("size", 40 if is_paragraph else 48)
    minimum = layer.get("min_size", min(12, preferred))
    x, y = layer.get("xy", [0, 0])
    anchor = layer.get("anchor", "lt")
    horizontal = layer.get("halign", anchor[0]) if is_paragraph else anchor[0]
    vertical = anchor[1]
    pad_l, pad_t, pad_r, pad_b = _shadow_padding(layer)
    available = {"l": width - x, "m": 2 * min(x, width - x), "r": x}[horizontal]
    max_width = min(layer.get("max_width", available), available)
    max_height = layer.get("max_height", height)
    # 字体路径只解析一次；每次缩小重新测量字形、换行和可用高度。
    path = fonts.resolve(layer.get("font"), layer.get("bold", False))
    stroke = (layer.get("stroke") or {}).get("width", 3) if layer.get("stroke") else 0
    last_reason = "可用区域为空"
    for size in range(preferred, minimum - 1, -1):
        font = ImageFont.truetype(path, size, index=fonts.font_index(path, layer.get("bold", False)))
        measure = _Measure(draw, font, layer.get("spacing", 0), stroke)
        # 同时预留画布左右边缘及 max_width 内的阴影空间。
        alignment_width = {"l": width - x - pad_r, "r": x - pad_l,
                           "m": 2 * min(x - pad_l, width - x - pad_r)}[horizontal]
        line_width = min(max_width - pad_l - pad_r, alignment_width)
        if line_width <= 0:
            break
        if layer.get("wrap", is_paragraph):
            lines = measure.wrap(text, line_width)
        else:
            lines = [measure.line(line) for line in text.split("\n")]
        if any(line.width > line_width for line in lines):
            last_reason = "单行或单个字符超过可用宽度"
            continue
        ink_top = min(line.top for line in lines)
        ink_bottom = max(line.bottom for line in lines)
        step = max(layer.get("line_gap", preferred * 1.3) * size / preferred,
                   ink_bottom - ink_top)
        top = min(line.top + i * step for i, line in enumerate(lines))
        bottom = max(line.bottom + i * step for i, line in enumerate(lines))
        ascent, descent = font.getmetrics()
        baseline = {
            "t": y - top,
            "a": y + ascent,
            "m": y - (top + bottom) / 2,
            "b": y - bottom,
            "s": y - (len(lines) - 1) * step,
            "d": y - descent - (len(lines) - 1) * step,
        }[vertical]
        if flow_top is not None:
            baseline = flow_top + pad_t - top
        positions, boxes = [], []
        for i, line in enumerate(lines):
            left = x - line.width * {"l": 0, "m": .5, "r": 1}[horizontal]
            bx, by = left - line.left, baseline + i * step
            positions.append((bx, by))
            boxes.append((left, by + line.top, left + line.width, by + line.bottom))
        bbox = (min(b[0] for b in boxes) - pad_l, min(b[1] for b in boxes) - pad_t,
                max(b[2] for b in boxes) + pad_r, max(b[3] for b in boxes) + pad_b)
        if bbox[3] - bbox[1] > max_height:
            last_reason = f"段落高度 {bbox[3] - bbox[1]:.1f} 超过 max_height={max_height}"
            continue
        if bbox[0] < 0 or bbox[1] < 0 or bbox[2] > width or bbox[3] > height:
            last_reason = f"文字及描边/阴影边界 {tuple(round(v, 1) for v in bbox)} 超出 {width}×{height} 画布"
            continue
        return _TextLayout(font, size, lines, positions, bbox)
    raise LayoutError(f"文字无法完整容纳（min_size={minimum}）：{last_reason}；请增大区域、调整位置或减小最小字号")


def _paint_layout(canvas, layer, layout):
    stroke = layer.get("stroke") or {}
    stroke_width = stroke.get("width", 3) if stroke else 0

    def paint(target, color, dx=0, dy=0, stroke_color=None):
        draw = ImageDraw.Draw(target)
        for line, (x, y) in zip(layout.lines, layout.positions):
            for text, offset in line.runs:
                draw.text((x + offset + dx, y + dy), text, font=layout.font,
                          fill=color, anchor="ls", stroke_width=stroke_width,
                          stroke_fill=stroke_color if stroke_color is not None else color)

    shadow = layer.get("shadow")
    if shadow:
        with Image.new("RGBA", canvas.size) as overlay:
            paint(overlay, _hex(shadow.get("color", "#00000066")),
                  shadow.get("x", 0), shadow.get("y", 4))
            blurred = overlay.filter(ImageFilter.GaussianBlur(shadow.get("blur", 6)))
            canvas.alpha_composite(blurred)
            blurred.close()
    with Image.new("RGBA", canvas.size) as overlay:
        paint(overlay, _hex(layer.get("color", "#000000")),
              stroke_color=_hex(stroke.get("color", "#FFFFFF")))
        canvas.alpha_composite(overlay)


def _draw_text(img, layer, ctx, *, flow_top=None):
    text = templates.format_text(layer.get("text", ""), ctx)
    if not text.strip():
        return None
    layout = _layout_text(ImageDraw.Draw(img), layer, text, img.size, flow_top=flow_top)
    _paint_layout(img, layer, layout)
    return layout


def _draw_paragraph(img, layer, ctx):
    layout = _draw_text(img, {**layer, "type": "paragraph"}, ctx)
    return layout.bbox[3] - layer.get("xy", [0, 0])[1] if layout else 0


def _cover_resize(im, box_w, box_h):
    scale = max(box_w / im.width, box_h / im.height)
    new = im.resize((max(box_w, math.ceil(im.width * scale)),
                     max(box_h, math.ceil(im.height * scale))), Image.Resampling.LANCZOS)
    left, top = (new.width - box_w) // 2, (new.height - box_h) // 2
    result = new.crop((left, top, left + box_w, top + box_h))
    new.close()
    return result


def _rounded_mask(size, radius):
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius=radius, fill=255)
    return mask


def render_image(template_key, ctx, ai_image_path=None, cfg=None):
    """纯内存渲染；不读取全局配置（已传 cfg 时），不写文件，不修改 cfg/ctx。"""
    cfg = load_config() if cfg is None else templates.validate_config(cfg)
    if template_key not in cfg["templates"]:
        raise templates.TemplateError(f"模板不存在: {template_key}")
    if not isinstance(ctx, dict):
        raise templates.TemplateError("ctx 必须为对象")
    ctx = {**cfg.get("vars", {}), **ctx}
    years = str(ctx.get("years", ""))
    if years.isascii() and years.isdigit():
        ctx["years"] = _cn_num(years)
    tpl = cfg["templates"][template_key]
    if tpl.get("base_image"):
        canvas = templates.read_image(_abs(tpl["base_image"]))
    else:
        c = cfg["canvas"]
        canvas = Image.new("RGBA", (c["width"], c["height"]), "white")
    try:
        ai_cfg = tpl.get("ai") or {}
        if ai_image_path is not None and ai_cfg.get("enabled", False):
            x, y, w, h = ai_cfg.get("rect", [0, 0, canvas.width, canvas.height])
            with templates.read_image(ai_image_path) as art:
                resized = _cover_resize(art, w, h)
            radius = ai_cfg.get("radius", 0)
            if radius:
                with _rounded_mask((w, h), radius) as mask:
                    # 保留源 alpha，再应用圆角蒙版。
                    alpha = ImageChops.multiply(resized.getchannel("A"), mask)
                    resized.putalpha(alpha)
                    alpha.close()
            canvas.alpha_composite(resized, (x, y))
            resized.close()

        flow_y = None
        for i, original in enumerate(tpl.get("layers", [])):
            layer = dict(original)
            loc = f"templates.{template_key}.layers[{i}]"
            flow_top = (flow_y + tpl.get("flow_gap", 60)
                        if layer.get("flow") and flow_y is not None else None)
            kind = layer.get("type", "text")
            try:
                if kind in ("text", "paragraph"):
                    layout = _draw_text(canvas, layer, ctx, flow_top=flow_top)
                    if layer.get("flow") and layout:
                        # 使用实际缩小后的文字及效果边界，空段落不移动光标。
                        flow_y = layout.bbox[3]
                elif kind == "image":
                    path = _abs(templates.format_text(layer["path"], ctx, loc + ".path"))
                    with templates.read_image(path) as im:
                        if "rect" in layer:
                            x, y, w, h = layer["rect"]
                            resized = im.resize((w, h), Image.Resampling.LANCZOS)
                        else:
                            x, y = layer.get("xy", [0, 0])
                            resized = im.copy()
                    try:
                        if x + resized.width > canvas.width or y + resized.height > canvas.height:
                            raise LayoutError("图片图层超出画布")
                        canvas.alpha_composite(resized, (int(x), int(y)))
                    finally:
                        resized.close()
                elif kind == "rect":
                    x, y, w, h = layer["rect"]
                    with Image.new("RGBA", canvas.size) as overlay:
                        ImageDraw.Draw(overlay).rounded_rectangle(
                            [x, y, x + w - 1, y + h - 1], radius=layer.get("radius", 0),
                            fill=_hex(layer.get("color", "#FFFFFF")))
                        canvas.alpha_composite(overlay)
            except (LayoutError, templates.TemplateError, fonts.FontError) as exc:
                raise type(exc)(f"{loc}: {exc}") from exc
        return canvas.convert("RGB")
    finally:
        canvas.close()


def render_png(template_key, ctx, ai_image_path=None, cfg=None):
    """返回 PNG bytes，适合无需临时文件的 HTTP 预览。"""
    with render_image(template_key, ctx, ai_image_path, cfg) as image:
        stream = BytesIO()
        image.save(stream, "PNG")
        return stream.getvalue()


def render(template_key, ctx, ai_image_path=None, out_path=None, cfg=None):
    """保持原有文件输出签名；成功返回 Path，失败不覆盖已有输出。"""
    if out_path is None:
        raise templates.TemplateError("render 需要 out_path；内存预览可调用 render_png/render_image")
    with render_image(template_key, ctx, ai_image_path, cfg) as image:
        return templates._atomic_write(Path(out_path), lambda stream: image.save(stream, "PNG"))
