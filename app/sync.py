"""Conservative directory sync: fill blanks by open_id, never infer departures.

An incomplete list or reduced authorization scope is not evidence of departure.
Only an explicit boolean is_resigned=True disables an existing open_id. Existing
local department names, dates and inactive state are preserved. Unique exact
names are associated automatically; department differences do not block binding.
"""
import logging
from collections.abc import Mapping
from datetime import datetime
from zoneinfo import ZoneInfo

from . import bindings, db, employees, feishu
from .settings import TZ

log = logging.getLogger("sync")
BIRTHDAY_ATTR_HINTS = ("生日", "birth", "出生")


def _ts_to_date(ts):
    if not ts or isinstance(ts, bool):
        return None
    try:
        ts = int(ts)
        if ts <= 0:
            return None
        if ts > 10_000_000_000:
            ts //= 1000
        return datetime.fromtimestamp(ts, ZoneInfo(TZ)).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _birthday_attr_ids():
    try:
        attrs = feishu.list_custom_attrs()
    except Exception as exc:
        log.info("读不到企业自定义字段，跳过生日回填：%s", exc)
        return set()
    ids = set()
    for attr in attrs:
        label = attr.get("name") or ""
        names = attr.get("i18n_name")
        if isinstance(names, list):
            label = " ".join(str(n.get("value") or "") for n in names if isinstance(n, Mapping)) or label
        if attr.get("id") and any(hint in str(label).lower() for hint in BIRTHDAY_ATTR_HINTS):
            ids.add(attr["id"])
    return ids


def _extract_birthday(user, attr_ids):
    # Without a known birthday field, other date-valued custom fields are unsafe.
    if not attr_ids:
        return None
    for item in user.get("custom_attrs") or []:
        if not isinstance(item, Mapping) or item.get("id") not in attr_ids:
            continue
        value = item.get("value") or {}
        if not isinstance(value, Mapping) or not value.get("text"):
            continue
        try:
            return employees._date(value["text"], "birth_date")
        except employees.EmployeeError:
            pass
    return None


def _sync_user(user, attr_ids):
    if not isinstance(user, Mapping) or not isinstance(user.get("open_id"), str) or not user["open_id"].strip():
        raise employees.EmployeeError("飞书用户缺少 open_id", "missing_open_id")
    open_id = user["open_id"]
    status = user.get("status") or {}
    if not isinstance(status, Mapping) or ("is_resigned" in status and not isinstance(status["is_resigned"], bool)):
        raise employees.EmployeeError("飞书离职状态不是明确布尔值", "invalid_user_status")
    resigned = status.get("is_resigned") is True
    departments = []
    if not resigned:
        try:
            departments = employees.resolve_departments(user)
        except employees.IdentityError:
            pass  # Department access is optional for name/ID association.
    with db.tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT * FROM employees WHERE feishu_open_id=?", (open_id,)).fetchone()
        if not existing and not resigned and user.get("name"):
            names = conn.execute("SELECT * FROM employees WHERE name=?", (user["name"],)).fetchall()
            if len(names) > 1:
                raise employees.EmployeeError("本地存在同名员工，无法唯一对应飞书 ID", "ambiguous_employee")
            if names:
                if names[0]["feishu_open_id"]:
                    raise employees.EmployeeError("姓名已有另一个飞书 ID，请先处理对应冲突", "identifier_conflict")
                existing = names[0]
        current = dict(existing) if existing else None
        if current:
            employees._assert_editable(conn, current["id"])
        if resigned:
            if not current:
                return "skipped", None
            conn.execute("UPDATE employees SET active=0, updated_at=? WHERE id=?", (db.now(), current["id"]))
            employees._reset_identity(conn, current["id"], "飞书明确标记员工已离职")
            employees._invalidate_events(conn, current["id"], "飞书明确标记员工已离职", deactivate=True)
            return ("disabled" if current["active"] else "skipped"), None
        if current and not current["active"]:
            return "skipped", None  # Restoring inactive employees requires an explicit local edit.

        remote_name = user.get("name")
        error = None
        if not isinstance(remote_name, str) or not remote_name.strip():
            error = employees.IdentityError("飞书缺少姓名", "missing_name")
        elif current and current.get("name") and current["name"] != remote_name:
            error = employees.IdentityError("飞书姓名与本地姓名冲突，已保留本地资料", "name_mismatch")
        if error:
            if current:
                employees._reset_identity(conn, current["id"], str(error), "failed")
                employees._invalidate_events(conn, current["id"], "同步发现身份无法核实：" + str(error))
                # Commit revocation, reporting this row as rejected without replacing local data.
                return "rejected", error
            raise error

        proposed = {
            "name": remote_name,
            "department": "、".join(sorted({d["name"] for d in departments})),
            "feishu_open_id": open_id,
            "employee_no": user.get("employee_no"), "email": user.get("email"),
            "feishu_user_id": user.get("user_id"), "gender": str(user.get("gender") or ""),
            "join_date": _ts_to_date(user.get("join_time")),
            "birth_date": _extract_birthday(user, attr_ids),
        }
        if current:
            proposed = {key: value for key, value in proposed.items() if employees._blank(current.get(key))}
            if not any(not employees._blank(v) for v in proposed.values()):
                return "skipped", None
        data = employees._normalize(proposed)
        employees._save(conn, data, current["id"] if current else None, source="feishu")
        return ("updated" if current else "added"), None


def sync_from_feishu(fill_birthday=True):
    # Materialize before any writes: iteration failure cannot commit a partial run.
    users = list(feishu.list_users())
    attr_ids = _birthday_attr_ids() if fill_birthday else set()
    result = {"ok": True, "total_from_feishu": len(users), "added": 0, "updated": 0,
              "disabled": 0, "skipped": 0, "errors": []}
    by_id = {}
    by_name = {}
    for user in users:
        if isinstance(user, Mapping) and isinstance(user.get("open_id"), str):
            by_id.setdefault(user["open_id"], []).append(user)
            if isinstance(user.get("name"), str) and user["name"]:
                by_name.setdefault(user["name"], set()).add(user["open_id"])
    processed = set()
    for position, user in enumerate(users, 1):
        open_id = user.get("open_id") if isinstance(user, Mapping) else None
        try:
            if isinstance(open_id, str):
                if open_id in processed:
                    result["skipped"] += 1
                    continue
                processed.add(open_id)
                if any(other != user for other in by_id.get(open_id, [])):
                    raise employees.EmployeeError("同一 open_id 返回冲突的通讯录记录，拒绝同步", "conflicting_directory_rows")
                if isinstance(user.get("name"), str) and len(by_name.get(user["name"], ())) > 1:
                    raise employees.EmployeeError("飞书中存在多个同名人员，无法唯一对应", "ambiguous_employee")
            action, error = _sync_user(user, attr_ids)
            if error:
                raise error
            result[action] += 1
        except employees.EmployeeError as exc:
            result["errors"].append({"row": position, "open_id": open_id, "code": exc.code,
                                     "error": str(exc), "msg": str(exc)})
    result["ok"] = not result["errors"]
    result["binding"] = bindings.auto_bind(users=users)
    log.info("同步完成：新增=%s 补全=%s 明确离职=%s 错误=%s",
             result["added"], result["updated"], result["disabled"], len(result["errors"]))
    return result


def missing_report():
    """Active employees missing dates, identity fields or successful verification."""
    return db.query(
        """SELECT id,name,department,join_date,birth_date,feishu_open_id,
                  identity_status,identity_error FROM employees
           WHERE active=1 AND (COALESCE(birth_date,'')='' OR COALESCE(join_date,'')=''
                 OR COALESCE(feishu_open_id,'')='' OR COALESCE(department,'')=''
                 OR COALESCE(identity_status,'pending')<>'verified') ORDER BY name"""
    )
