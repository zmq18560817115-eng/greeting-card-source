"""飞书开放平台客户端：只用主动调用（tenant_access_token），不做事件回调。

用到的能力 / 权限（后台「权限管理」需开通）：
  contact:contact.base:readonly / contact:user.base:readonly   读通讯录基础信息
  contact:user.employee_id:readonly                            读 user_id
  contact:user.employee_job:readonly (或 contact:user.base)    读 join_time 入职时间
  contact:custom_attr.read (可选)                              读企业自定义字段（如生日）
  im:message:send_as_bot / im:message                          机器人发单聊消息
  im:resource  (上传图片)
"""
import json
import logging
import threading
import time
from urllib.parse import quote

import requests

from .settings import FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_BASE
from .presentation import friendly_error

log = logging.getLogger("feishu")

_token = {"value": None, "expire_at": 0}
_lock = threading.Lock()


class FeishuError(RuntimeError):
    def __init__(self, code, msg, detail=None, definitive=False):
        super().__init__(f"[飞书 {code}] {msg}")
        self.code = code
        self.msg = msg
        self.detail = detail
        self.definitive = definitive


def _check(resp):
    try:
        data = resp.json()
    except Exception:
        raise FeishuError(-1, f"HTTP {resp.status_code} 非 JSON 响应: {resp.text[:200]}")
    if not isinstance(data, dict):
        raise FeishuError(-1, "飞书响应不是对象")
    if not 200 <= resp.status_code < 300:
        raise FeishuError(data.get("code", resp.status_code), data.get("msg", f"HTTP {resp.status_code}"),
                          data, definitive=400 <= resp.status_code < 500 and resp.status_code != 408)
    if data.get("code", 0) != 0:
        raise FeishuError(data.get("code"), data.get("msg", ""), data, definitive=True)
    return data


def tenant_access_token(force=False):
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        raise FeishuError(-1, "尚未连接飞书，请到「系统状态 → 飞书连接」填写 App ID 和 App Secret")
    with _lock:
        if not force and _token["value"] and time.time() < _token["expire_at"]:
            return _token["value"]
        r = requests.post(
            f"{FEISHU_BASE}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
            timeout=15,
        )
        data = _check(r)
        _token["value"] = data["tenant_access_token"]
        # 提前 5 分钟过期
        _token["expire_at"] = time.time() + max(60, int(data.get("expire", 7200)) - 300)
        return _token["value"]


def _headers(json_ct=True):
    h = {"Authorization": f"Bearer {tenant_access_token()}"}
    if json_ct:
        h["Content-Type"] = "application/json; charset=utf-8"
    return h


def connection_error(exc):
    """Explain remote failures without exposing tokens, request bodies or credentials."""
    if isinstance(exc, FeishuError):
        message = str(exc)
        for secret in (FEISHU_APP_SECRET, _token.get("value")):
            if secret:
                message = message.replace(secret, "[已隐藏]")
        message = message[:800]
        hint = friendly_error(message)
        return message if hint == message else f"{hint}\n技术信息：{message}"
    if isinstance(exc, requests.exceptions.Timeout):
        return "连接飞书超时，请检查网络后重试"
    if isinstance(exc, requests.exceptions.RequestException):
        return "无法连接飞书，请检查网络和代理设置后重试"
    return "飞书响应处理失败，请检查应用权限和服务日志"


def _get(path, params=None, retry=1):
    r = requests.get(f"{FEISHU_BASE}{path}", headers=_headers(), params=params, timeout=20)
    if r.status_code == 401 and retry > 0:
        tenant_access_token(force=True)
        return _get(path, params, retry - 1)
    return _check(r)


def _post(path, payload, params=None, retry=1):
    r = requests.post(
        f"{FEISHU_BASE}{path}", headers=_headers(), params=params,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), timeout=30,
    )
    if r.status_code == 401 and retry > 0:
        tenant_access_token(force=True)
        return _post(path, payload, params, retry - 1)
    return _check(r)


# --------------------------------------------------------------------------
# 通讯录
# --------------------------------------------------------------------------
def get_user(open_id):
    return _get(f"/open-apis/contact/v3/users/{quote(str(open_id), safe='')}",
                {"user_id_type": "open_id", "department_id_type": "open_department_id"})["data"]["user"]


def get_department(department_id):
    return _get(f"/open-apis/contact/v3/departments/{quote(str(department_id), safe='')}",
                {"department_id_type": "open_department_id"})["data"]["department"]

def list_departments():
    """返回全公司部门（含根部门 0）的 open_department_id 列表。"""
    ids, page_token = ["0"], None
    while True:
        params = {
            "fetch_child": "true",
            "page_size": 50,
            "department_id_type": "open_department_id",
        }
        if page_token:
            params["page_token"] = page_token
        data = _get("/open-apis/contact/v3/departments/0/children", params)["data"]
        for d in data.get("items", []):
            did = d.get("open_department_id") or d.get("department_id")
            if did:
                ids.append(did)
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
    return ids


def _pages(path, params):
    params = dict(params)
    seen_tokens = set()
    while True:
        data = _get(path, params)["data"]
        yield data
        if not data.get("has_more"):
            return
        token = data.get("page_token")
        if not token or token in seen_tokens:
            raise FeishuError(-1, "通讯录分页信息不完整")
        seen_tokens.add(token)
        params["page_token"] = token


def list_users():
    """只读取应用授权范围，含直接授权用户和授权部门的成员。"""
    return list_scope_users()


def list_scope_users():
    users, user_ids, department_ids = {}, set(), set()
    for data in _pages("/open-apis/contact/v3/scopes",
                       {"user_id_type": "open_id", "department_id_type": "open_department_id", "page_size": 100}):
        user_ids.update(data.get("user_ids") or [])
        department_ids.update(data.get("department_ids") or [])
    expanded = set(department_ids)
    for did in sorted(department_ids):
        for data in _pages(f"/open-apis/contact/v3/departments/{quote(did, safe='')}/children",
                           {"department_id_type": "open_department_id", "fetch_child": "true", "page_size": 50}):
            expanded.update(item["open_department_id"] for item in data.get("items", []) if item.get("open_department_id"))
    for did in sorted(expanded):
        for data in _pages("/open-apis/contact/v3/users/find_by_department",
                           {"department_id": did, "department_id_type": "open_department_id",
                            "user_id_type": "open_id", "page_size": 50}):
            for user in data.get("items", []):
                if user.get("open_id"):
                    users[user["open_id"]] = user
    for oid in sorted(user_ids):
        if oid not in users:
            users[oid] = get_user(oid)
    return list(users.values())


def list_departments_map():
    """open_department_id -> 部门名 映射（含根部门）。"""
    mapping = {"0": "根部门"}
    page_token = None
    while True:
        params = {"page_size": 50, "department_id_type": "open_department_id",
                  "fetch_child": "true"}
        if page_token:
            params["page_token"] = page_token
        data = _get("/open-apis/contact/v3/departments/0/children", params)["data"]
        for d in data.get("items", []):
            did = d.get("open_department_id")
            name = d.get("name") or ""
            if isinstance(name, list):
                name = "".join(v.get("value", "") for v in name if isinstance(v, dict))
            if did and name:
                mapping[did] = name
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
    return mapping


def user_id_by_email_or_mobile(value):
    """按邮箱或手机号换取本应用的 open_id 列表。"""
    payload = {}
    if "@" in str(value):
        payload["emails"] = [value]
    else:
        payload["mobiles"] = [value]
    data = _post("/open-apis/contact/v3/users/batch_get_id", payload,
                 params={"user_id_type": "open_id"})
    out = []
    for v in data.get("data", {}).get("user_list", []):
        if v.get("user_id"):
            out.append({"user_id": v["user_id"]})
    return out


def list_custom_attrs():
    """企业自定义用户字段定义，用来定位「生日」字段的 id。"""
    out, page_token = [], None
    while True:
        params = {"page_size": 50}
        if page_token:
            params["page_token"] = page_token
        data = _get("/open-apis/contact/v3/custom_attrs", params)["data"]
        out.extend(data.get("items", []))
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
    return out


# --------------------------------------------------------------------------
# 消息
# --------------------------------------------------------------------------
def upload_image(file_path):
    """上传图片，返回 image_key。"""
    with open(file_path, "rb") as f:
        r = requests.post(
            f"{FEISHU_BASE}/open-apis/im/v1/images",
            headers={"Authorization": f"Bearer {tenant_access_token()}"},
            files={"image": f},
            data={"image_type": "message"},
            timeout=60,
        )
    return _check(r)["data"]["image_key"]


def send_image(open_id, image_key):
    data = _post(
        "/open-apis/im/v1/messages",
        {"receive_id": open_id, "msg_type": "image",
         "content": json.dumps({"image_key": image_key}, ensure_ascii=False)},
        params={"receive_id_type": "open_id"},
    )
    return data["data"]["message_id"]


def send_text(open_id, text):
    data = _post(
        "/open-apis/im/v1/messages",
        {"receive_id": open_id, "msg_type": "text",
         "content": json.dumps({"text": text}, ensure_ascii=False)},
        params={"receive_id_type": "open_id"},
    )
    return data["data"]["message_id"]


def ping():
    """连通性自检。"""
    tenant_access_token(force=True)
    return True


def send_card(open_id, title, md_text, image_key, note="HR 关怀", uuid=None):
    """以消息卡片形式推送：文案 + 收起的图片（点击才打开大图）。"""
    card = {
        "config": {"wide_screen_mode": True},
        "header": {"template": "turquoise",
                   "title": {"tag": "plain_text", "content": title}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": md_text}},
            {"tag": "img", "img_key": image_key,
             "mode": "fit_horizontal", "preview": True,
             "alt": {"tag": "plain_text", "content": title}},
            {"tag": "hr"},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": note}]},
        ],
    }
    payload = {"receive_id": open_id, "msg_type": "interactive",
               "content": json.dumps(card, ensure_ascii=False)}
    if uuid:
        payload["uuid"] = uuid
    data = _post(
        "/open-apis/im/v1/messages",
        payload,
        params={"receive_id_type": "open_id"},
    )
    return data["data"]["message_id"]
