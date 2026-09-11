"""供 main 调用的模板配置服务，不依赖 Web 框架或外部服务。

load_config / validate_config 返回独立的、补齐默认值的 dict。
merge_config(cfg, edits) 只合并内存：dict 递归合并，layers 按索引更新，
其中 None/{} 跳过该层；不提供隐式删除/追加图层（完整结构可交 save_config）。
save_config(cfg, path=None) 校验后原子替换文件；并发保存为最后写入者生效。
list_fonts() -> [{value, label}]。
save_base_image(data, filename='background', directory=None)
    -> {base_image, width, height}，仅保存图片，不改模板配置。

文字字段沿用 text/font/size/color/xy/anchor/halign/spacing/line_gap/max_width，
新增 max_height/min_size/wrap。line_gap 是相邻行基线距离，随字号等比缩放。
有底图时以底图实际尺寸为画布；无底图时使用 canvas。
check_assets=False 仅用于离线检查/修复配置；保存和渲染始终检查真实资源。
"""
from copy import deepcopy
from io import BytesIO
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
from string import Formatter
import tempfile
import uuid
import warnings

from PIL import Image, ImageOps

from . import fonts
from .settings import BASE_DIR, TEMPLATE_CONFIG, TEMPLATE_DIR

PLACEHOLDERS = frozenset({
    "name", "years", "company", "year", "month", "day", "date", "event_date",
    "join_date", "birth_date", "department", "employee_id",
})
MAX_PIXELS = 40_000_000
MAX_TEXT = 10_000


class TemplateError(ValueError):
    """可直接由 HTTP 层转换为 400 的配置/资源错误。"""


def _fail(where, message):
    raise TemplateError(f"{where}: {message}")


def _number(value, where, minimum=0, maximum=100_000, integer=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not minimum <= value <= maximum or not math.isfinite(value)
            or (integer and not isinstance(value, int))):
        _fail(where, f"必须是 {minimum}..{maximum} 范围内的{'整数' if integer else '有限数值'}")
    return value


def parse_color(value):
    if isinstance(value, str) and re.fullmatch(r"#?(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{8})", value):
        raw = value.lstrip("#")
        rgba = tuple(int(raw[i:i + 2], 16) for i in range(0, len(raw), 2))
        return rgba + (255,) if len(rgba) == 3 else rgba
    if (isinstance(value, (tuple, list)) and len(value) in (3, 4)
            and all(type(c) is int and 0 <= c <= 255 for c in value)):
        return tuple(value) + (255,) if len(value) == 3 else tuple(value)
    raise TemplateError("颜色必须为 #RRGGBB/#RRGGBBAA 或 0..255 的 RGB/RGBA 数组")


def validate_placeholders(text, allowed=PLACEHOLDERS, where="text"):
    """只接受 {name} 这样的简单名称及 {{ }}，拒绝属性访问/索引/格式表达式。"""
    if not isinstance(text, str) or len(text) > MAX_TEXT:
        _fail(where, f"必须是长度不超过 {MAX_TEXT} 的字符串")
    fields = set()
    try:
        for _, field, spec, conversion in Formatter().parse(text):
            if field is None:
                continue
            if field not in allowed or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field):
                _fail(where, f"不支持的占位符 {{{field}}}")
            if spec or conversion:
                _fail(where, "占位符不支持格式修饰或转换")
            fields.add(field)
    except ValueError as exc:
        if isinstance(exc, TemplateError):
            raise
        _fail(where, f"占位符语法错误: {exc}")
    return fields


def format_text(text, ctx, where="text"):
    fields = validate_placeholders(text, PLACEHOLDERS | set(ctx), where)
    missing = fields - ctx.keys()
    if missing:
        _fail(where, f"缺少占位符值: {', '.join(sorted(missing))}")
    result = text.format_map(ctx).replace("\r\n", "\n").replace("\r", "\n")
    if len(result) > MAX_TEXT:
        _fail(where, f"替换后的文字超过 {MAX_TEXT} 字符")
    if any(ord(ch) < 32 and ch != "\n" for ch in result):
        _fail(where, "文字不能包含制表符或控制字符，请使用空格和换行")
    return result


def asset_path(value):
    if not isinstance(value, (str, os.PathLike)) or not str(value) or "\x00" in str(value):
        raise TemplateError("图片路径必须为非空本地路径")
    raw = str(value)
    if "://" in raw or (os.name != "nt" and PureWindowsPath(raw).is_absolute()):
        raise TemplateError(f"不是当前系统可访问的本地图片路径: {raw}")
    p = Path(raw.replace("\\", "/"))
    if ".." in p.parts or (PureWindowsPath(raw).drive and not PureWindowsPath(raw).is_absolute()):
        raise TemplateError("图片路径不能包含 .. 或使用盘符相对路径")
    return p if p.is_absolute() else Path(BASE_DIR) / p


def read_image(source):
    """解码并关闭文件句柄，统一 EXIF 方向；尺寸限制在像素解码前检查。"""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as im:
                if im.width * im.height > MAX_PIXELS:
                    raise TemplateError(f"图片不能超过 {MAX_PIXELS} 像素")
                if im.format not in {"PNG", "JPEG", "WEBP"} or getattr(im, "is_animated", False):
                    raise TemplateError("底图/图层图片必须为静态 PNG、JPEG 或 WebP")
                im.load()
                return ImageOps.exif_transpose(im).convert("RGBA")
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        if isinstance(exc, TemplateError):
            raise
        raise TemplateError(f"无法读取图片: {exc}") from exc


def _xy(value, where, width, height):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        _fail(where, "必须为 [x, y]")
    _number(value[0], where + ".x", maximum=width)
    _number(value[1], where + ".y", maximum=height)


def _rect(value, where, width, height):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        _fail(where, "必须为 [x, y, width, height]")
    x, y, w, h = value
    for number, label, lower in zip(value, ("x", "y", "width", "height"), (0, 0, 1, 1)):
        _number(number, where + "." + label, minimum=lower, integer=True)
    if x + w > width or y + h > height:
        _fail(where, f"矩形超出 {width}×{height} 画布")


def validate_config(cfg, *, check_assets=True):
    """校验整个配置并返回深拷贝；所有错误包含模板/图层字段位置。"""
    if not isinstance(cfg, dict):
        _fail("config", "必须为对象")
    cfg = deepcopy(cfg)
    canvas = cfg.setdefault("canvas", {"width": 1080, "height": 1440})
    if not isinstance(canvas, dict):
        _fail("canvas", "必须为对象")
    for key in ("width", "height"):
        _number(canvas.get(key), "canvas." + key, 1, 16384, integer=True)
    if canvas["width"] * canvas["height"] > MAX_PIXELS:
        _fail("canvas", f"不能超过 {MAX_PIXELS} 像素")
    variables = cfg.setdefault("vars", {})
    if not isinstance(variables, dict):
        _fail("vars", "必须为对象")
    for key, value in variables.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            _fail("vars", "变量名必须为字母/数字/下划线，不能以数字开头")
        if not isinstance(value, (str, int, float, bool, type(None))):
            _fail("vars." + key, "变量值必须为标量")
        if isinstance(value, float) and not math.isfinite(value):
            _fail("vars." + key, "变量值必须为有限数值")
    allowed = PLACEHOLDERS | variables.keys()
    tpls = cfg.get("templates")
    if not isinstance(tpls, dict) or not tpls:
        _fail("templates", "必须为非空对象")
    checked_fonts = set()
    for key, tpl in tpls.items():
        if not isinstance(key, str) or not re.fullmatch(r"[\w-]{1,80}", key) or not isinstance(tpl, dict):
            _fail("templates", "模板名只允许 1..80 个字母、数字、下划线或连字符，模板必须为对象")
        where = f"templates.{key}"
        width, height = canvas["width"], canvas["height"]
        if tpl.get("base_image") is not None:
            path = asset_path(tpl["base_image"])
            if check_assets:
                try:
                    with read_image(path) as im:
                        width, height = im.size
                except TemplateError as exc:
                    _fail(where + ".base_image", str(exc))
        _number(tpl.get("flow_gap", 60), where + ".flow_gap", maximum=height)
        if tpl.get("ai") is None:
            tpl["ai"] = {}
        ai = tpl["ai"]
        if not isinstance(ai, dict):
            _fail(where + ".ai", "必须为对象")
        ai.setdefault("enabled", False)
        if not isinstance(ai["enabled"], bool):
            _fail(where + ".ai.enabled", "必须为布尔值")
        if "rect" in ai:
            _rect(ai["rect"], where + ".ai.rect", width, height)
        _number(ai.get("radius", 0), where + ".ai.radius", maximum=min(width, height), integer=True)
        if "prompt" in ai:
            validate_placeholders(ai["prompt"], allowed, where + ".ai.prompt")
        if "prompts" in ai:
            if not isinstance(ai["prompts"], list):
                _fail(where + ".ai.prompts", "必须为数组")
            for prompt in ai["prompts"]:
                validate_placeholders(prompt, allowed, where + ".ai.prompts")
        layers = tpl.setdefault("layers", [])
        if not isinstance(layers, list) or len(layers) > 200:
            _fail(where + ".layers", "必须为不超过 200 层的数组")
        for index, layer in enumerate(layers):
            loc = f"{where}.layers[{index}]"
            if not isinstance(layer, dict):
                _fail(loc, "图层必须为对象")
            kind = layer.get("type", "text")
            if kind not in ("text", "paragraph", "image", "rect"):
                _fail(loc + ".type", "不支持的图层类型")
            for flag in ("bold", "flow", "wrap"):
                if flag in layer and not isinstance(layer[flag], bool):
                    _fail(loc + "." + flag, "必须为布尔值")
            if layer.get("flow") and kind not in ("text", "paragraph"):
                _fail(loc + ".flow", "只支持文字图层")
            if "color" in layer:
                try:
                    parse_color(layer["color"])
                except TemplateError as exc:
                    _fail(loc + ".color", str(exc))
            if kind in ("text", "paragraph"):
                validate_placeholders(layer.get("text", ""), allowed, loc + ".text")
                _xy(layer.get("xy", [0, 0]), loc + ".xy", width, height)
                size = layer.get("size", 40 if kind == "paragraph" else 48)
                _number(size, loc + ".size", 1, 512, integer=True)
                _number(layer.get("min_size", min(12, size)), loc + ".min_size", 1, size, integer=True)
                _number(layer.get("spacing", 0), loc + ".spacing", maximum=512)
                if "line_gap" in layer:
                    _number(layer["line_gap"], loc + ".line_gap", 1, height)
                for field, limit in (("max_width", width), ("max_height", height)):
                    if field in layer:
                        _number(layer[field], loc + "." + field, 1, limit)
                anchor = layer.get("anchor", "lt")
                if not isinstance(anchor, str) or not re.fullmatch(r"[lmr][atmsbd]", anchor):
                    _fail(loc + ".anchor", "必须为 l/m/r + a/t/m/s/b/d")
                if layer.get("halign", "l") not in ("l", "m", "r"):
                    _fail(loc + ".halign", "必须为 l/m/r")
                horizontal = layer.get("halign", anchor[0]) if kind == "paragraph" else anchor[0]
                x = layer.get("xy", [0, 0])[0]
                if "max_width" in layer:
                    left = x - layer["max_width"] * {"l": 0, "m": .5, "r": 1}[horizontal]
                    if left < 0 or left + layer["max_width"] > width:
                        _fail(loc + ".max_width", "配置的文字区域超出画布")
                try:
                    fonts.validate_spec(layer.get("font"))
                    font_key = (layer.get("font"), layer.get("bold", False))
                    if check_assets and font_key not in checked_fonts:
                        fonts.resolve(*font_key)
                        checked_fonts.add(font_key)
                except fonts.FontError as exc:
                    _fail(loc + ".font", str(exc))
                for effect in ("stroke", "shadow"):
                    data = layer.get(effect)
                    if data is None:
                        continue
                    if not isinstance(data, dict):
                        _fail(loc + "." + effect, "必须为对象或 null")
                    try:
                        parse_color(data.get("color", "#000000"))
                    except TemplateError as exc:
                        _fail(loc + "." + effect + ".color", str(exc))
                    if effect == "stroke":
                        _number(data.get("width", 3), loc + ".stroke.width", maximum=64, integer=True)
                    else:
                        for axis in ("x", "y"):
                            _number(data.get(axis, 0), loc + ".shadow." + axis, -512, 512)
                        _number(data.get("blur", 6), loc + ".shadow.blur", maximum=128)
            elif kind == "rect":
                _rect(layer.get("rect"), loc + ".rect", width, height)
                _number(layer.get("radius", 0), loc + ".radius", maximum=min(width, height), integer=True)
            else:
                fields = validate_placeholders(layer.get("path"), allowed, loc + ".path")
                path = asset_path(layer["path"])
                if "rect" in layer:
                    _rect(layer["rect"], loc + ".rect", width, height)
                else:
                    _xy(layer.get("xy", [0, 0]), loc + ".xy", width, height)
                if check_assets and not fields:
                    try:
                        with read_image(path) as im:
                            if "rect" not in layer:
                                x, y = layer.get("xy", [0, 0])
                                if x + im.width > width or y + im.height > height:
                                    _fail(loc, "图片图层超出画布")
                    except TemplateError as exc:
                        _fail(loc + ".path", str(exc))
    try:
        json.dumps(cfg, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        _fail("config", f"不能序列化为 JSON: {exc}")
    return cfg


def load_config(path=None, *, check_assets=True):
    try:
        cfg = json.loads(Path(TEMPLATE_CONFIG if path is None else path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise TemplateError(f"无法读取模板配置: {exc}") from exc
    return validate_config(cfg, check_assets=check_assets)


def merge_config(cfg, edits, *, check_assets=True):
    """接受 {templates, vars, canvas} 补丁；预览请求的 ctx 字段由 main 单独提取。

    layers 按原索引合并，短数组保留后续层，None/{} 表示不改这一层。
    不认识的顶层字段拒绝，防止把 name/employee_id 等请求数据写进配置。
    """
    if not isinstance(cfg, dict) or not isinstance(edits, dict):
        raise TemplateError("配置和编辑内容必须为对象")
    if set(edits) - {"templates", "vars", "canvas"}:
        raise TemplateError("编辑仅接受 templates、vars、canvas；请先分离预览上下文")

    def merge(base, patch, loc="config"):
        if not isinstance(base, dict) or not isinstance(patch, dict):
            return deepcopy(patch)
        result = deepcopy(base)
        for key, value in patch.items():
            if value is None and ".layers[" in loc and key in {
                "size", "min_size", "max_width", "max_height", "line_gap", "spacing"
            }:
                result.pop(key, None)
                continue
            if key == "layers" and isinstance(result.get(key), list) and isinstance(value, list):
                if len(value) > len(result[key]):
                    _fail(loc + ".layers", "按索引编辑不能追加图层，请使用完整配置保存")
                for i, edit in enumerate(value):
                    if edit is None:
                        continue
                    if not isinstance(edit, dict):
                        _fail(loc + f".layers[{i}]", "编辑必须为对象或 null")
                    result[key][i] = merge(result[key][i], edit, loc + f".layers[{i}]")
            else:
                result[key] = merge(result.get(key), value, loc + "." + key)
        return result

    return validate_config(merge(cfg, edits), check_assets=check_assets)


def _atomic_write(path, write):
    """同目录临时文件 + flush/fsync + os.replace；Windows 替换前关闭句柄。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix="." + path.name + ".",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            write(stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def save_config(cfg, path=None):
    """严格校验后原子保存；返回实际写入的独立配置 dict。"""
    valid = validate_config(cfg)
    raw = (json.dumps(valid, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    _atomic_write(TEMPLATE_CONFIG if path is None else path, lambda stream: stream.write(raw))
    return valid


def list_fonts():
    return fonts.list_fonts()


def background_size(cfg, key):
    """Keep replacement backgrounds on the template's existing coordinate system."""
    tpl = cfg["templates"][key]
    if tpl.get("base_image"):
        with read_image(asset_path(tpl["base_image"])) as image:
            return image.size
    return cfg["canvas"]["width"], cfg["canvas"]["height"]


def save_base_image(data, filename="background", *, directory=None, max_bytes=20 * 1024 * 1024,
                    size=None, fit="cover"):
    """接收 bytes 或二进制 file（例如 UploadFile.file）；返回未应用的底图元数据。

    按实际文件内容验证，重编码为 PNG，忽略客户端文件路径并生成唯一文件名。
    directory 由服务器指定，不能直接使用客户端传入的目标目录。
    """
    if hasattr(data, "read"):
        data = data.read(max_bytes + 1)
    if not isinstance(data, (bytes, bytearray)) or not data or len(data) > max_bytes:
        raise TemplateError(f"上传必须为非空图片，且不超过 {max_bytes} 字节")
    if fit not in ("cover", "contain"):
        raise TemplateError("底图适配方式须为铺满或完整保留")
    if size is not None:
        if not isinstance(size, (list, tuple)) or len(size) != 2:
            raise TemplateError("底图画布尺寸无效")
        for axis in size:
            _number(axis, "底图画布尺寸", 1, 16384, integer=True)
        if size[0] * size[1] > MAX_PIXELS:
            raise TemplateError("底图画布像素过大")
    with read_image(BytesIO(data)) as original:
        im = original
        if size is not None:
            im = (ImageOps.fit(original, size, Image.Resampling.LANCZOS) if fit == "cover"
                  else ImageOps.pad(original, size, Image.Resampling.LANCZOS, color="white"))
        try:
            return _store_base_image(im, filename, directory)
        finally:
            if im is not original:
                im.close()


def _store_base_image(im, filename, directory):
    stem = PureWindowsPath(str(filename or "background")).stem
    stem = "".join(ch for ch in stem if ch.isalnum() or ch in "-_")[:60] or "background"
    target = Path(TEMPLATE_DIR if directory is None else directory).resolve() / f"{stem}_{uuid.uuid4().hex}.png"
    _atomic_write(target, lambda stream: im.save(stream, "PNG"))
    try:
        value = target.relative_to(Path(BASE_DIR).resolve()).as_posix()
    except ValueError:
        value = str(target)
    return {"base_image": value, "width": im.width, "height": im.height}


# main 端兼容名称，返回结构及异常与上述接口完全相同。
font_options = list_fonts
store_background = save_base_image
