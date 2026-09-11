"""Intranet address and automatic scheduling; no employee values in configuration."""
import ipaddress
import json
from urllib.parse import urlsplit

from . import db, settings


def validate_base_url(value):
    if not isinstance(value, str):
        raise ValueError("请填写同事可以访问的内网后台地址")
    value = value.strip().rstrip("/")
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise ValueError("内网地址或端口无效") from None
    if (url.scheme not in ("http", "https") or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path or any(c.isspace() for c in value)):
        raise ValueError("请填写完整内网地址，例如 http://192.168.1.20:8848，不包含路径或口令")
    host = url.hostname.lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("不能使用 localhost，请填写同事可以访问的内网地址")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            raise ValueError("请填写固定内网 IP 或完整内部域名") from None
    else:
        if not address.is_private or address.is_loopback or address.is_unspecified or address.is_link_local or address.is_multicast:
            raise ValueError("请填写公司内网地址，不能使用回环地址、监听地址或公网 IP")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("端口无效")
    return value


def current():
    row = db.query_one("SELECT value FROM kv WHERE key='delivery_config'")
    saved = json.loads(row["value"]) if row else {}
    return {"base_url": saved.get("base_url", settings.POSTER_BASE_URL),
            "auto_schedule": saved.get("auto_schedule", settings.AUTO_SCHEDULE),
            "delivery_mode": "compact_link", "link_valid_days": 30}


def save(payload):
    base_url = validate_base_url(payload.get("base_url", ""))
    auto_schedule = payload.get("auto_schedule", True)
    if type(auto_schedule) is not bool:
        raise ValueError("自动排期必须为开启或关闭")
    db.execute("INSERT OR REPLACE INTO kv(key,value) VALUES('delivery_config',?)",
               (json.dumps({"base_url": base_url, "auto_schedule": auto_schedule}),))
    return {"ok": True, **current()}
