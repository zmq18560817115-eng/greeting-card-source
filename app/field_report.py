"""Read-only field mapping diagnostics. Return counts, never employee values."""
from collections.abc import Mapping

from . import db, employees, feishu, sync


FIELDS = (
    ("name", "姓名", "飞书姓名 name", "与当前应用的 open_id 一一对应；同名冲突需先处理。"),
    ("department", "部门", "department_ids → 部门名称", "检查用户组织架构信息、部门读取权限及通讯录范围，发布后重新同步。"),
    ("employee_no", "工号", "employee_no", "检查用户受雇信息权限及飞书中是否填写工号；工号不能用 open_id 代替。"),
    ("join_date", "入职日期", "join_time → 日期", "检查用户受雇信息权限及入职时间是否填写，也可通过名单模板补全。"),
    ("birth_date", "生日（月日）", "生日 / 出生日期自定义字段 → 月日", "需有可读取的生日自定义字段及员工字段值；否则通过名单模板补全。生日只按月日推送，不计算年龄。"),
    ("active", "在职状态", "status.is_resigned", "仅明确的飞书离职状态会停用员工；本地已停用人员不会自动恢复。"),
    ("feishu_open_id", "飞书 open_id", "open_id", "使用当前飞书应用返回的 open_id；发送前检查对应姓名和在职状态。"),
)


def local_report():
    rows = db.query("SELECT name,department,employee_no,join_date,birth_date,active,feishu_open_id FROM employees WHERE active=1")
    return {"app_id": feishu.FEISHU_APP_ID, "local_total": len(rows), "remote_total": None,
            "checked_at": None, "fields": [
                {"key": key, "label": label, "source": source, "hint": hint,
                 "local_filled": sum(not employees._blank(row.get(key)) for row in rows), "remote": None}
                for key, label, source, hint in FIELDS]}


def _presence(user, key):
    if key not in user:
        return "not_returned"
    return "empty" if employees._blank(user[key]) or user[key] == [] else "available"


def _remote_state(user, key, attr_ids, department_cache):
    raw_key = {"department": "department_ids", "join_date": "join_time", "birth_date": "custom_attrs",
               "feishu_open_id": "open_id", "active": "status"}.get(key, key)
    if key == "birth_date" and not attr_ids:
        return "unavailable"
    state = _presence(user, raw_key)
    if state != "available":
        return state
    value = user[raw_key]
    if key == "department":
        if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
            return "invalid"
        ids = tuple(sorted(set(value)))
        if ids not in department_cache:
            try:
                employees.resolve_departments(user)
                department_cache[ids] = "available"
            except employees.IdentityError:
                department_cache[ids] = "unreadable"
        return department_cache[ids]
    if key == "active":
        if not isinstance(value, Mapping):
            return "invalid"
        if "is_resigned" not in value:
            return "not_returned"
        return "available" if isinstance(value["is_resigned"], bool) else "invalid"
    if key == "join_date":
        return "available" if sync._ts_to_date(value) else "invalid"
    if key == "birth_date":
        if not isinstance(value, list):
            return "invalid"
        matches = [item for item in value if isinstance(item, Mapping) and item.get("id") in attr_ids]
        if not matches:
            return "not_returned"
        if sync._extract_birthday(user, attr_ids):
            return "available"
        return "invalid" if any(isinstance(item.get("value"), Mapping) and item["value"].get("text") for item in matches) else "empty"
    if not isinstance(value, str) or not value.strip():
        return "invalid"
    if key == "feishu_open_id" and (not value.startswith("ou_") or value != value.strip()):
        return "invalid"
    return "available"


@feishu.in_application
def check_fields():
    result = local_report()
    result.update(ok=False, checked_at=db.now(), warnings=[])
    try:
        # Fail closed on incomplete directory access; never call it an empty directory.
        users = list(feishu.list_users())
    except Exception as exc:
        result["error"] = feishu.connection_error(exc)
        return result
    if any(not isinstance(user, Mapping) for user in users):
        result["error"] = "飞书返回了无效的人员记录，无法统计字段。"
        return result
    attr_ids = sync._birthday_attr_ids(result["warnings"])
    department_cache = {}
    result["remote_total"] = len(users)
    for field in result["fields"]:
        counts = dict.fromkeys(("available", "not_returned", "empty", "invalid", "unreadable", "unavailable"), 0)
        for user in users:
            counts[_remote_state(user, field["key"], attr_ids, department_cache)] += 1
        field["remote"] = counts
    result["ok"] = True
    return result
