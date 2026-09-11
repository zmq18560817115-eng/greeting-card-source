"""Authentication for generated posters; never put the admin secret in image URLs."""
import hashlib
import hmac
import ipaddress
import time

COOKIE_NAME = "gc_files"
SESSION_SECONDS = 3600


def require_private_access(host, admin_token):
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if not loopback and not admin_token:
        raise ValueError("通过内网提供后台访问前，必须设置 ADMIN_TOKEN 管理口令；本机调试请使用 HOST=127.0.0.1")


def _signature(secret, expires):
    return hmac.new(secret.encode("utf-8"), f"poster-access:{expires}".encode("ascii"), hashlib.sha256).hexdigest()


def issue_file_session(secret):
    expires = int(time.time()) + SESSION_SECONDS
    return f"{expires}.{_signature(secret, expires)}"


def valid_file_session(value, secret):
    if not secret:
        return False
    try:
        timestamp, signature = value.split(".", 1)
        expires = int(timestamp)
        if not int(time.time()) < expires <= int(time.time()) + SESSION_SECONDS:
            return False
        return hmac.compare_digest(signature, _signature(secret, expires))
    except (AttributeError, TypeError, ValueError):
        return False
