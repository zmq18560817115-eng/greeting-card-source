"""Derived review fields and readable notices; original records are not rewritten."""
import re

from .dates import completed_years_since, parse_date


def friendly_error(value):
    raw = str(value or "").strip()
    if not raw:
        return ""
    lower = raw.lower()
    if any(word in lower for word in ("尚未连接飞书", "尚未在.env配置", "未配置飞书")):
        return "还没有连接飞书，请在「系统状态 → 飞书连接」填写应用凭据。"
    if any(word in lower for word in ("app secret invalid", "invalid app secret", "app_secret invalid", "invalid app_id", "invalid app id")):
        return "飞书应用凭据不正确，请核对同一个应用的 App ID 和 App Secret 后重新保存。"
    if any(word in lower for word in ("insufficient scope", "scopes is required", "scopes are required", "access denied", "no permission", "forbidden")):
        return "访问权限不足，请管理员根据技术详情开通对应权限；飞书应用修改权限后还需发布生效。"
    if any(word in lower for word in ("read timed out", "connecttimeout", "readtimeout", "timed out", "timeout", "连接飞书超时")):
        return "连接超时，暂时无法取得结果。请检查网络；涉及推送时先核实员工是否已收到，避免重复发送。"
    if any(word in lower for word in ("connectionerror", "connection refused", "max retries exceeded", "failed to establish", "name resolution", "proxyerror", "sslerror")):
        return "无法连接服务，请检查网络、代理和证书设置后重新检查连接。"
    if any(word in lower for word in ("too many requests", "rate limit", "frequency limit")):
        return "操作过于频繁，请稍后再试。"
    if "缺少飞书 open_id" in raw:
        return "尚未对应飞书 ID，请连接飞书并同步名单，系统将按唯一姓名自动对应。"
    if "姓名与飞书通讯录不一致" in raw:
        return "姓名与飞书记录不一致，请核对员工姓名和绑定的飞书 ID。"
    if "部门与飞书完整部门名" in raw:
        return "部门与飞书记录不一致，请使用飞书中的完整部门名称。"
    if "is_resigned" in raw:
        return "飞书没有返回有效的在职状态，请检查通讯录字段权限后重新核验。"
    if "员工日期已变更" in raw:
        return "员工的生日或入职日期已经修改，请跳过旧记录并重新扫描正确日期。"
    if "缺少完整部门" in raw:
        return "还没有填写部门，请补全与飞书一致的完整部门名称。"
    # Preserve existing Chinese instructions. English-only failures get a readable summary.
    description = re.sub(r"^\[飞书\s+[^\]]+\]\s*", "", raw)
    if re.search(r"[\u4e00-\u9fff]", description):
        return description
    return "操作未完成，请展开技术详情查看原因，或联系管理员处理。"


def birthday_display(value):
    day = parse_date(value)
    if not day:
        return None
    return day.strftime("%m-%d") if day.year <= 1901 else day.isoformat()


def employee_fields(emp):
    return {**emp, "anniversary_years": completed_years_since(emp.get("join_date")),
            "birth_date_display": birthday_display(emp.get("birth_date")),
            "identity_hint": friendly_error(emp.get("identity_error"))}


def delivery_fields(event):
    status = event.get("status")
    if status == "delivery_unknown":
        state, label = "unknown", "回执不明"
    elif status == "simulated":
        state, label = "simulated", "演练未发送"
    elif event.get("pushed_at") or status == "pushed":
        state, label = "sent", "已推送"
    elif status == "pushing":
        state, label = "sending", "推送中"
    else:
        state, label = "not_sent", "未推送"
    return {"delivery_state": state, "delivery_label": label,
            "is_pushed": None if state in ("unknown", "sending") else state == "sent",
            "planned_push_at": event.get("trigger_at"),
            "actual_push_at": event.get("pushed_at") if state == "sent" else None}


def event_notice(event, emp):
    status = event.get("status")
    if status == "delivery_unknown":
        return "飞书未返回明确结果，员工可能已收到贺卡。请先核实消息，暂勿重新发送。"
    if status == "expired":
        return "已超过这次贺卡的发送日期，系统已停止推送。"
    if status == "needs_regeneration":
        return "员工资料已修改，请重新生成海报并再次审核。"
    if event.get("last_error"):
        return friendly_error(event["last_error"])
    if emp.get("identity_error"):
        return friendly_error(emp["identity_error"])
    if status == "gen_failed":
        return "海报生成失败，请检查模板和底图；具体原因可在核查详情中查看。"
    if status == "failed":
        return "本次推送失败，请先查看推送记录中的原因，处理后再重试。"
    if status == "blocked":
        return "员工尚未完成飞书对应，请同步名单并处理姓名或 ID 冲突。"
    return ""
