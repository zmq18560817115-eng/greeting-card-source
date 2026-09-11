"""项目字体及 Windows/macOS/Linux 中文字体；显式路径失败时不静默替换。"""
import hashlib
from io import BytesIO
import os
from pathlib import Path, PureWindowsPath
import tempfile

from PIL import ImageFont

from .settings import BASE_DIR, FONT_DIR

FONT_EXTENSIONS = {".ttf", ".otf", ".ttc", ".otc"}
BOLD_HINTS = ("bold", "black", "heavy", "semibold", "msyhbd", "simhei")
MAX_UPLOAD_BYTES = 32 * 1024 * 1024
FAMILY_LABELS = {
    "noto sans cjk sc": "Noto 黑体（简体中文）", "noto serif cjk sc": "Noto 宋体（简体中文）",
    "noto sans sc": "Noto 黑体（简体中文）", "noto serif sc": "Noto 宋体（简体中文）",
    "microsoft yahei": "微软雅黑", "microsoft yahei ui": "微软雅黑 UI",
    "simsun": "宋体", "simhei": "黑体", "kaiti": "楷体", "fangsong": "仿宋",
    "dengxian": "等线", "pingfang sc": "苹方（简体中文）",
    "wenquanyi zen hei": "文泉驿正黑", "hiragino sans gb": "冬青黑体",
}
SERVER_CJK_NAMES = ("noto sans cjk", "noto serif cjk", "noto sans sc", "noto serif sc",
                    "microsoft yahei", "simsun", "simhei", "kaiti", "fangsong", "dengxian",
                    "pingfang", "heiti", "hiragino sans gb", "wenquanyi", "arial unicode",
                    "微软雅黑", "宋体", "黑体", "楷体", "苹方", "思源")
CANDIDATES = [
    str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/msyh.ttc"),
    str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/msyhbd.ttc"),
    str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/simhei.ttf"),
    str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/simsun.ttc"),
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]


class FontError(ValueError):
    """字体配置无效或本机无法加载该字体。"""


def _system_dirs():
    return [
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
        Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
        / "Microsoft/Windows/Fonts",
        Path("/System/Library/Fonts"), Path("/Library/Fonts"),
        Path.home() / "Library/Fonts", Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"), Path.home() / ".local/share/fonts",
        Path.home() / ".fonts",
    ]


def _project_fonts():
    root = Path(FONT_DIR)
    return sorted(str(p) for p in root.rglob("*")
                  if p.is_file() and p.suffix.lower() in FONT_EXTENSIONS) if root.is_dir() else []


def font_index(path: str, bold: bool = False) -> int:
    """保持 PingFang 集合字重选择；微软雅黑的粗体使用独立字体文件。"""
    return 2 if bold and Path(path).name.lower() == "pingfang.ttc" else 0


def _check(path, bold=False):
    path = Path(path)
    if path.suffix.lower() not in FONT_EXTENSIONS or not path.is_file():
        raise FontError(f"字体文件不存在或扩展名不支持: {path}")
    try:
        ImageFont.truetype(str(path), 16, index=font_index(str(path), bold))
    except (OSError, ValueError) as exc:
        raise FontError(f"无法加载字体: {path}: {exc}") from exc
    return str(path.resolve())


def validate_spec(spec):
    """只检查路径语法；resolve() 另外检查文件存在及可解码。"""
    if spec is None or spec == "auto":
        return
    if not isinstance(spec, str) or not spec.strip() or "\x00" in spec:
        raise FontError("font 必须是 auto 或字体路径")
    p = PureWindowsPath(spec)
    if p.suffix.lower() not in FONT_EXTENSIONS:
        raise FontError("字体必须为 .ttf/.otf/.ttc/.otc 文件")
    if ".." in p.parts:
        raise FontError("字体相对路径不能包含 ..")
    if p.drive and not p.is_absolute():
        raise FontError("字体不能使用依赖当前目录的盘符相对路径")


def resolve(spec: str | None = None, bold: bool = False) -> str:
    """auto 优先项目内字体，再选系统中文字体；显式路径不回退。

    相对路径相对于 assets/fonts 或项目根目录，独立于进程工作目录；
    单独文件名也可匹配系统字体目录。绝对路径须在当前操作系统可访问。
    """
    validate_spec(spec)
    if spec is not None and spec != "auto":
        if os.name != "nt" and PureWindowsPath(spec).is_absolute():
            raise FontError(f"当前系统无法访问 Windows 字体路径: {spec}")
        p = Path(spec.replace("\\", "/"))
        paths = [p] if p.is_absolute() else [Path(FONT_DIR) / p, Path(BASE_DIR) / p]
        if len(p.parts) == 1:
            paths.extend(root / p for root in _system_dirs())
        for candidate in paths:
            if candidate.is_file():
                return _check(candidate, bold)
        raise FontError(f"找不到指定字体: {spec}；请上传字体到 assets/fonts 或选择 auto")

    # 内置字体使不同服务器的 auto 保持一致，上传新字体不会改变 auto。
    project = _project_fonts()
    builtin = [p for p in project if Path(p).is_relative_to(Path(FONT_DIR) / "builtin")]
    for pool in (builtin, [p for p in project if p not in builtin], CANDIDATES):
        ordered = sorted(pool, key=lambda f: any(h in Path(f).stem.lower()
                                                 for h in BOLD_HINTS) != bold)
        for candidate in ordered:
            try:
                return _check(candidate, bold)
            except FontError:
                continue
    raise FontError("找不到可用的中文字体，请将 .ttf/.otf/.ttc/.otc 放入 assets/fonts")


def _font_option(path):
    family, style = ImageFont.truetype(str(path), 16).getname()
    try:
        relative = Path(path).resolve().relative_to(Path(FONT_DIR).resolve())
        value = relative.as_posix()
        source = relative.parts[0] if relative.parts[0] in ("builtin", "uploaded") else "project"
    except ValueError:
        value, source = str(path), "server"
    label = FAMILY_LABELS.get(family.casefold(), family)
    if style not in ("Regular", "Normal", "Book"):
        label += " · " + {"Bold": "粗体", "Light": "细体", "Thin": "纤细", "ExtraLight": "特细"}.get(style, style)
    return {"value": value, "label": label, "source": source, "family": family, "style": style}


def list_fonts():
    """返回适合字体下拉框的 [{value, label}]，只枚举 Pillow 可加载文件。

    不把全部系统西文字体作为 auto 的候选，避免中文自动回退成方框。
    """
    result = [{"value": "auto", "label": "自动选择字体（优先内置）", "source": "auto"}]
    candidates = _project_fonts() + CANDIDATES
    for root in _system_dirs():
        if root.is_dir():
            candidates.extend(str(p) for p in root.rglob("*")
                              if p.is_file() and p.suffix.lower() in FONT_EXTENSIONS)
    seen = set()
    for candidate in candidates:
        try:
            path = _check(candidate)
            identity = os.path.normcase(path)
            if identity in seen:
                continue
            seen.add(identity)
            option = _font_option(path)
            if option["source"] == "server" and not any(name in option["family"].casefold() for name in SERVER_CJK_NAMES):
                continue
            result.append(option)
        except (FontError, OSError, ValueError):
            continue
    return [result[0]] + sorted(result[1:], key=lambda item: (item["label"].casefold(), item["value"]))


def save_uploaded_font(raw, filename):
    """Validate before saving, with content-addressed paths independent of the client filename."""
    suffix = PureWindowsPath(filename).suffix.lower()
    if suffix not in FONT_EXTENSIONS:
        raise FontError("字体仅支持 TTF、OTF、TTC、OTC 文件")
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        raise FontError("字体文件不能为空，且不能超过 32 MB")
    try:
        loaded = ImageFont.truetype(BytesIO(raw), 24)
        loaded.getmask("贺卡祝福 ABC 123")
    except (OSError, ValueError) as exc:
        raise FontError("无法读取字体，请选择有效的字体文件") from exc
    folder = Path(FONT_DIR) / "uploaded"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (hashlib.sha256(raw).hexdigest() + suffix)
    if not target.exists():
        pending = None
        try:
            with tempfile.NamedTemporaryFile(dir=folder, suffix=".tmp", delete=False) as handle:
                pending = handle.name
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(pending, target)
        finally:
            if pending and Path(pending).exists():
                Path(pending).unlink()
    return {"ok": True, "font": _font_option(target)}
