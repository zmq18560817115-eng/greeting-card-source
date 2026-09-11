"""Local Feishu configuration. API responses never contain the application secret."""
import os
import re
import tempfile
import threading

from . import feishu, settings

ENV_PATH = settings.BASE_DIR / ".env"
_save_lock = threading.Lock()


def public_config():
    return {"app_id": feishu.FEISHU_APP_ID, "secret_configured": bool(feishu.FEISHU_APP_SECRET),
            "configured": bool(feishu.FEISHU_APP_ID and feishu.FEISHU_APP_SECRET)}


def save_config(app_id, app_secret=""):
    if not isinstance(app_id, str) or not re.fullmatch(r"cli_[A-Za-z0-9]+", app_id.strip()):
        raise ValueError("请填写 cli_ 开头的飞书 App ID")
    if not isinstance(app_secret, str) or any(char in app_secret for char in "\r\n\x00"):
        raise ValueError("App Secret 格式无效，请重新复制应用密钥")
    app_id, app_secret = app_id.strip(), app_secret.strip()
    with feishu._application_lock, _save_lock, feishu._lock:
        if feishu._active_application_operations:
            raise ValueError("正在读取通讯录或执行推送，请完成后再更改飞书配置")
        if not app_secret:
            if app_id != feishu.FEISHU_APP_ID or not feishu.FEISHU_APP_SECRET:
                raise ValueError("首次连接或更换 App ID 时必须填写对应的 App Secret")
            app_secret = feishu.FEISHU_APP_SECRET
        values = {"FEISHU_APP_ID": app_id, "FEISHU_APP_SECRET": app_secret}
        lines = ENV_PATH.read_text(encoding="utf-8-sig").splitlines() if ENV_PATH.exists() else []
        output, written = [], set()
        for line in lines:
            key = line.split("=", 1)[0].strip()
            if "=" in line and key in values:
                if key not in written:
                    output.append(f"{key}={values[key]}")
                    written.add(key)
            else:
                output.append(line)
        output.extend(f"{key}={value}" for key, value in values.items() if key not in written)
        # Keep temporary credentials inside the same already-ignored .env.* family.
        pending = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                    dir=ENV_PATH.parent, prefix=".env.", suffix=".tmp", delete=False) as handle:
                pending = handle.name
                handle.write("\n".join(output) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(pending, ENV_PATH)
        finally:
            if pending and os.path.exists(pending):
                os.unlink(pending)
        feishu.FEISHU_APP_ID = settings.FEISHU_APP_ID = app_id
        feishu.FEISHU_APP_SECRET = settings.FEISHU_APP_SECRET = app_secret
        os.environ.update(values)
        feishu._token.update(value=None, expire_at=0)
    return {"ok": True, **public_config(), "msg": "飞书配置已保存并生效，请检查通讯录连接"}


@feishu.in_application
def check_connection():
    try:
        # A valid token alone does not prove permission to read the directory.
        feishu.tenant_access_token()
        groups, has_scope = set(), False
        for data in feishu._pages("/open-apis/contact/v3/scopes", {
                "user_id_type": "open_id", "department_id_type": "open_department_id", "page_size": 100}):
            if not isinstance(data, dict):
                raise feishu.FeishuError(-1, "通讯录权限范围返回格式无效")
            has_scope = has_scope or any(data.get(key) for key in ("user_ids", "department_ids", "group_ids"))
            groups.update(data.get("group_ids") or [])
        if not has_scope:
            return {"ok": False, "msg": "应用已连接，但通讯录授权范围为空；请在飞书开放平台配置目标员工并发布应用"}
        # Groups must not be silently ignored when deciding whether a name is unique.
        feishu._group_scope_ids(groups)
        return {"ok": True, "msg": "通讯录授权范围检查通过；同步后按姓名 + open_id 一一对应，发送前实时检查姓名、open_id 和在职状态"}
    except Exception as exc:
        return {"ok": False, "msg": feishu.connection_error(exc)}
