"""Employee identity and safe imports.

Imports use stable identifiers for updates. Feishu recipients are matched by
exact name and open_id; department is editable business data, not a binding rule.
"""
import json
import re
import sqlite3
from collections.abc import Mapping
from datetime import date, datetime

from . import db, feishu


class EmployeeError(ValueError):
    def __init__(self, message, code="invalid_employee", status_code=400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class IdentityError(EmployeeError):
    def __init__(self, message, code="identity_mismatch", evidence=None):
        super().__init__(message, code)
        self.evidence = evidence or {}


FIELDS = ("name", "employee_no", "email", "department", "feishu_user_id",
          "feishu_open_id", "join_date", "birth_date", "gender", "active", "note")
IDENTITY_FIELDS = ("name", "feishu_open_id", "feishu_user_id")
HEADER_MAP = {
    **{key: key for key in ("id", *FIELDS)},
    "员工id": "id", "姓名": "name", "工号": "employee_no", "邮箱": "email",
    "部门": "department", "入职日期": "join_date", "入职时间": "join_date",
    "生日": "birth_date", "生日（月日）": "birth_date", "出生日期": "birth_date", "出生年月": "birth_date", "性别": "gender",
    "飞书open_id": "feishu_open_id", "open_id": "feishu_open_id",
    "用户id": "feishu_open_id", "用户 id": "feishu_open_id",
    "用户id（open_id）": "feishu_open_id", "用户 id（open_id）": "feishu_open_id",
    "用户id(open_id)": "feishu_open_id", "用户 id(open_id)": "feishu_open_id",
    "飞书user_id": "feishu_user_id", "备注": "note", "在职": "active", "在职状态": "active",
}


def _blank(value):
    return value is None or (isinstance(value, str) and not value.strip())


def department_names(value):
    """Whitespace at name boundaries is ignored; spelling and case are exact."""
    return tuple(sorted({p.strip() for p in re.split(r"[、,，]", value or "") if p.strip()}))


def _id(value):
    if isinstance(value, bool) or not re.fullmatch(r"[1-9]\d*", str(value).strip()):
        raise EmployeeError("员工 id 必须是正整数", "invalid_id")
    return int(value)


def _date(value, field):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raw = str(value).strip().replace("/", "-").replace(".", "-")
    chinese_date = re.fullmatch(
        r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日(?:\s+(\d{1,2}:\d{2}:\d{2}))?", raw)
    if chinese_date:
        year, month, day, clock = chinese_date.groups()
        raw = f"{year}-{month}-{day}" + (f" {clock}" if clock else "")
    elif field == "birth_date":
        chinese_birthday = re.fullmatch(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", raw)
        if chinese_birthday:
            raw = "-".join(chinese_birthday.groups())
    formats = ["%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y%m%d"]
    for fmt in formats:
        if fmt == "%Y%m%d" and not re.fullmatch(r"\d{8}", raw):
            continue
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    if field == "birth_date" and re.fullmatch(r"\d{1,2}-\d{1,2}", raw):
        # Keep legacy storage compatibility; scheduling and display use month/day only.
        try:
            year = "1896" if tuple(map(int, raw.split("-"))) == (2, 29) else "1900"
            return datetime.strptime(year + "-" + raw, "%Y-%m-%d").date().isoformat()
        except ValueError:
            pass
    label = {"join_date": "入职日期", "birth_date": "生日"}.get(field, field)
    hint = "请填写有效日期，例如 2021-02-01 或 2021年2月1日"
    if field == "join_date":
        hint += "，入职日期必须包含年份"
    else:
        hint = "请填写有效月日，例如 02-01 或 2月1日；生日推送不使用年份"
    raise EmployeeError(f"{label}无效：{value}；{hint}", "invalid_date")


def _normalize(payload):
    if not isinstance(payload, Mapping):
        raise EmployeeError("员工资料必须是对象", "invalid_row")
    out = {}
    for field in ("id", *FIELDS):
        if field not in payload or _blank(payload[field]):
            continue  # Empty CSV/XLSX cells and partial forms preserve existing data.
        value = payload[field]
        if field == "id":
            out[field] = _id(value)
        elif field in ("join_date", "birth_date"):
            out[field] = _date(value, field)
        elif field == "feishu_open_id":
            if not isinstance(value, str) or not re.fullmatch(r"ou_[A-Za-z0-9_]+", value.strip()):
                raise EmployeeError("用户 ID 格式无效，请填写当前飞书应用中以 ou_ 开头的 open_id，不能填写工号或 user_id", "invalid_open_id")
            out[field] = value.strip()
        elif field == "active":
            values = {"1": 1, "true": 1, "在职": 1, "0": 0, "false": 0, "离职": 0, "已离职": 0, "停用": 0, "已停用": 0}
            if str(value).strip().lower() not in values:
                raise EmployeeError("在职状态请填写在职或离职，也支持 0/1、true/false", "invalid_active")
            out[field] = values[str(value).strip().lower()]
        else:
            if not isinstance(value, (str, int)) or isinstance(value, bool):
                raise EmployeeError(f"{field} 必须是文本", "invalid_field")
            out[field] = str(value).strip()
    return out


def _get(conn, employee_id):
    row = conn.execute("SELECT * FROM employees WHERE id=?", (employee_id,)).fetchone()
    if not row:
        raise EmployeeError("员工不存在", "not_found", 404)
    return dict(row)


def _assert_editable(conn, employee_id):
    if conn.execute("SELECT 1 FROM events WHERE employee_id=? AND status='pushing' LIMIT 1",
                    (employee_id,)).fetchone():
        raise EmployeeError("该员工有正在推送的事件，请稍后重试", "employee_pushing", 409)


def _invalidate_events(conn, employee_id, reason, deactivate=False):
    """Revoke pending deliveries without deleting events, images or push logs."""
    pending = ("employee_id=? AND status NOT IN ('pushed','delivery_unknown') "
               "AND (status='simulated' OR pushed_at IS NULL OR pushed_at='')")
    cancelled = conn.execute(f"SELECT COUNT(*) FROM events WHERE {pending} AND status<>'skipped'",
                             (employee_id,)).fetchone()[0] if deactivate else 0
    conn.execute(f"""UPDATE cards SET status='invalid', error=? WHERE event_id IN
                   (SELECT id FROM events WHERE {pending})""", (reason, employee_id))
    conn.execute(f"""UPDATE events SET
                   status=CASE WHEN ? OR status='skipped' THEN 'skipped' ELSE 'needs_regeneration' END,
                   selected_card_id=NULL, confirmed_by=NULL, confirmed_at=NULL, generation_token=NULL,
                   last_error=?, updated_at=? WHERE {pending}""",
                 (int(deactivate), reason, db.now(), employee_id))
    return cancelled


def _reset_identity(conn, employee_id, reason, status="pending", evidence=None):
    conn.execute("""UPDATE employees SET identity_status=?, identity_error=?,
                   identity_verified_at=NULL, identity_snapshot=?, updated_at=? WHERE id=?""",
                 (status, reason, json.dumps(evidence, ensure_ascii=False) if evidence else None,
                  db.now(), employee_id))


def _check_identifiers(conn, data, employee_id=None):
    for key in ("feishu_open_id", "employee_no", "feishu_user_id"):
        if not data.get(key):
            continue
        hits = conn.execute(f"SELECT id FROM employees WHERE {key}=?", (data[key],)).fetchall()
        if any(r["id"] != employee_id for r in hits):
            raise EmployeeError(f"{key} 已属于其他员工，请人工核对", "identifier_conflict", 409)


def _ambiguous_name(conn, data):
    for row in conn.execute("SELECT * FROM employees WHERE name=?", (data["name"],)):
        local = set(department_names(row["department"]))
        incoming = set(department_names(data.get("department")))
        if not local or not incoming or local.intersection(incoming):
            # Two fully identified people may genuinely share name and department.
            if not (row["feishu_open_id"] and data.get("feishu_open_id")
                    and row["feishu_open_id"] != data["feishu_open_id"]):
                raise EmployeeError("已有同名同部门或部门不明的员工，请指定员工 id；不会自动合并",
                                    "ambiguous_employee", 409)


def _save(conn, data, employee_id=None, source="local"):
    data = {k: v for k, v in data.items() if k in FIELDS}
    current = _get(conn, employee_id) if employee_id is not None else None
    if current:
        _assert_editable(conn, employee_id)
    _check_identifiers(conn, data, employee_id)
    if current is None:
        if not data.get("name"):
            raise EmployeeError("新增员工必须填写姓名", "missing_name")
        _ambiguous_name(conn, data)
        values = {**data, "source": source, "created_at": db.now(), "updated_at": db.now()}
        keys = list(values)
        cur = conn.execute(f"INSERT INTO employees({','.join(keys)}) VALUES({','.join('?' for _ in keys)})",
                           list(values.values()))
        employee_id = cur.lastrowid
    else:
        changed = {k: v for k, v in data.items() if v != current.get(k)}
        if changed:
            conn.execute(f"UPDATE employees SET {','.join(k+'=?' for k in changed)},updated_at=? WHERE id=?",
                         [*changed.values(), db.now(), employee_id])
            if set(changed).intersection((*IDENTITY_FIELDS, "active")):
                _reset_identity(conn, employee_id, "员工姓名、用户 ID 或在职状态已修改，等待更新飞书对应关系")
            _invalidate_events(conn, employee_id, "员工资料已修改，请重新生成并确认贺卡",
                               deactivate=data.get("active", current["active"]) == 0)
    return {"ok": True, "id": employee_id, "created": current is None,
            "employee": _get(conn, employee_id)}


def save_employee(payload):
    """Explicit edit by id, otherwise insert; explicit blank name/dept is invalid.

    Other empty values preserve current data. The import path intentionally
    permits empty name/department cells to retain existing identity fields.
    """
    if isinstance(payload, Mapping):
        for field in ("name", "department"):
            if field in payload and _blank(payload[field]):
                raise EmployeeError(f"{field} 不能为空；不修改时请省略该字段", "missing_" + field)
    data = _normalize(payload)
    try:
        with db.tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return _save(conn, data, data.get("id"))
    except sqlite3.IntegrityError as exc:
        raise EmployeeError("员工标识冲突，资料未保存", "identifier_conflict", 409) from exc


EDIT_FIELDS = ("name", "department", "employee_no", "join_date", "birth_date", "active", "feishu_open_id")


def batch_update(updates):
    """Apply explicit field changes atomically, rejecting stale editor snapshots."""
    if not isinstance(updates, list) or not 1 <= len(updates) <= 1000:
        raise EmployeeError("每次请更正 1 到 1000 位员工", "invalid_batch")
    ids, prepared = set(), []
    for item in updates:
        if not isinstance(item, Mapping):
            raise EmployeeError("批量更正资料格式无效", "invalid_batch")
        eid = _id(item.get("id"))
        if eid in ids:
            raise EmployeeError("批量更正包含重复员工，请重新选择", "duplicate_employee")
        ids.add(eid)
        changes, original = item.get("changes"), item.get("original")
        if not isinstance(changes, Mapping) or not changes or set(changes) - set(EDIT_FIELDS):
            raise EmployeeError("请选择要更正的基础资料字段", "invalid_fields")
        if not isinstance(original, Mapping) or set(original) != set(EDIT_FIELDS):
            raise EmployeeError("缺少原始资料，请关闭更正窗口并刷新后重试", "missing_snapshot")
        label = str(original.get("name") or "员工")
        try:
            for key in ("name", "department", "active", "feishu_open_id"):
                if key in changes and _blank(changes[key]):
                    raise EmployeeError({"name": "姓名", "department": "部门", "active": "在职状态", "feishu_open_id": "用户 ID"}[key] + "不能为空")
            data = _normalize(changes)
            # Clearing optional fields is explicit here, unlike blank import cells.
            for key in ("employee_no", "join_date", "birth_date"):
                if key in changes and _blank(changes[key]):
                    data[key] = None
        except EmployeeError as exc:
            raise EmployeeError(f"{label}：{exc}；本次更正未保存", exc.code, exc.status_code) from exc
        prepared.append((eid, original, data))
    changed_ids, changed_names = [], []
    with db.tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for eid, original, data in prepared:
            label = str(original.get("name") or "员工")
            try:
                current = _get(conn, eid)
                if any(current.get(key) != original[key] for key in EDIT_FIELDS):
                    raise EmployeeError("资料已被修改，请关闭更正窗口并刷新后重试", "employee_changed", 409)
                changed = {key: value for key, value in data.items() if current.get(key) != value}
                if not changed:
                    continue
                _save(conn, changed, eid)
                changed_ids.append(eid)
                if "name" in changed:
                    changed_names.append((eid, changed["name"]))
            except (EmployeeError, sqlite3.IntegrityError) as exc:
                reason = str(exc) if isinstance(exc, EmployeeError) else "工号或人员标识重复"
                for key, text in (("employee_no", "工号"), ("feishu_open_id", "飞书 ID")):
                    reason = reason.replace(key, text)
                raise EmployeeError(f"{label}：{reason}；本次更正均未保存",
                                    getattr(exc, "code", "identifier_conflict"), getattr(exc, "status_code", 409)) from exc
        for eid, name in changed_names:
            if conn.execute("SELECT 1 FROM employees WHERE name=? AND id<>?", (name, eid)).fetchone():
                raise EmployeeError(f"{name}：已有同名员工，无法唯一对应飞书 ID；本次更正均未保存", "ambiguous_employee", 409)
    return {"ok": True, "updated": len(changed_ids), "unchanged": len(prepared) - len(changed_ids), "ids": changed_ids}


def _import_target(conn, data):
    if data.get("id"):
        _get(conn, data["id"])
        return data["id"]
    ids = set()
    for field in ("feishu_open_id", "employee_no"):
        if data.get(field):
            ids.update(r["id"] for r in conn.execute(f"SELECT id FROM employees WHERE {field}=?", (data[field],)))
    if len(ids) > 1:
        raise EmployeeError("飞书 open_id 与工号指向不同员工", "identifier_conflict", 409)
    if not ids:
        return None
    employee_id = ids.pop()
    current = _get(conn, employee_id)
    for field in IDENTITY_FIELDS:
        if not data.get(field) or not current.get(field):
            continue
        old, new = current[field], data[field]
        if field == "department":
            old, new = department_names(old), department_names(new)
        if old != new:
            raise EmployeeError(f"{field} 与已有员工不一致，请指定员工 id 人工修改", "identity_conflict", 409)
    return employee_id


def _map_row(raw):
    if not isinstance(raw, Mapping):
        raise EmployeeError("导入行必须是对象", "invalid_row")
    mapped = {}
    for key, value in raw.items():
        canonical = HEADER_MAP.get(str(key).strip().lstrip("\ufeff").lower())
        if canonical and not _blank(value):
            if canonical in mapped and mapped[canonical] != value:
                raise EmployeeError(f"重复列 {canonical} 的值不一致", "duplicate_column")
            mapped[canonical] = value
    if not mapped and any(not _blank(v) for k, v in raw.items() if k != "_row"):
        raise EmployeeError("该行没有可识别的员工字段", "invalid_columns")
    return mapped


def import_rows(rows):
    """Import dicts or (physical_row_number, dict) pairs; dicts start at row 2.

    A dict may alternatively contain _row. Each rejected row rolls back to a
    savepoint; successful rows commit together. Infrastructure failures roll
    back the whole import instead of returning misleading success counters.
    CSV decoding and XLSX reading belong to the API; date/datetime cells work.
    """
    result = {"ok": True, "total": 0, "added": 0, "updated": 0, "skipped": 0,
              "errors": [], "results": []}
    with db.tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for position, raw in enumerate(rows, 2):
            row_number = position
            if isinstance(raw, (tuple, list)) and len(raw) == 2:
                row_number, raw = raw
            elif isinstance(raw, Mapping):
                row_number = raw.get("_row", position)
            result["total"] += 1
            conn.execute("SAVEPOINT employee_import_row")
            try:
                mapped = _map_row(raw)
                if not mapped:
                    result["skipped"] += 1
                    result["results"].append({"row": row_number, "action": "skipped"})
                else:
                    data = _normalize(mapped)
                    saved = _save(conn, data, _import_target(conn, data))
                    action = "added" if saved["created"] else "updated"
                    result[action] += 1
                    result["results"].append({"row": row_number, "id": saved["id"], "action": action})
            except (EmployeeError, sqlite3.IntegrityError) as exc:
                conn.execute("ROLLBACK TO employee_import_row")
                result["errors"].append({"row": row_number, "code": getattr(exc, "code", "identifier_conflict"),
                                         "error": str(exc), "msg": str(exc)})
            finally:
                conn.execute("RELEASE employee_import_row")
    result["ok"] = not result["errors"]
    return result


def deactivate(ids):
    """Atomically soft-disable an entire selection, preserving delivery history."""
    if isinstance(ids, (str, bytes)) or not ids:
        raise EmployeeError("请提供员工 id 列表", "invalid_ids")
    employee_ids = list(dict.fromkeys(_id(value) for value in ids))
    cancelled = 0
    with db.tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for employee_id in employee_ids:
            _get(conn, employee_id)
            _assert_editable(conn, employee_id)
        for employee_id in employee_ids:
            conn.execute("UPDATE employees SET active=0, updated_at=? WHERE id=?", (db.now(), employee_id))
            _reset_identity(conn, employee_id, "员工已停用")
            cancelled += _invalidate_events(conn, employee_id, "员工已停用，取消未发送事件", deactivate=True)
    return {"ok": True, "ids": employee_ids, "disabled": len(employee_ids), "cancelled": cancelled, "mode": "disabled"}


def resolve_departments(user):
    """Resolve *all* open_department_ids; never substitute IDs for names."""
    ids = user.get("department_ids")
    if not isinstance(ids, (tuple, list)) or not ids or any(not isinstance(i, str) or not i.strip() for i in ids):
        raise IdentityError("飞书未返回完整的 open_department_id 列表", "missing_departments")
    departments = []
    for department_id in dict.fromkeys(ids):
        try:
            department = feishu.get_department(department_id)
        except feishu.FeishuError as exc:
            raise IdentityError(f"无法读取飞书部门：{feishu.connection_error(exc)}", "department_unavailable") from exc
        except Exception as exc:
            raise IdentityError(f"无法读取飞书部门 {department_id}", "department_unavailable") from exc
        if (not isinstance(department, Mapping)
                or department.get("open_department_id") != department_id
                or not isinstance(department.get("name"), str) or not department["name"].strip()):
            raise IdentityError(f"飞书部门 {department_id} 返回的 ID 或名称不完整", "invalid_department")
        departments.append({"open_department_id": department_id, "name": department["name"].strip()})
    return departments


def _validate_identity(emp, remote_user=None, directory_users=None):
    """Read-only live verification; return evidence or raise IdentityError.

    Does not consult or mutate cached verification state. Callers must compare
    their local snapshot inside their delivery transaction before sending.
    """
    emp = dict(emp)
    evidence = {"checked_at": db.now(), "local": {k: emp.get(k) for k in ("id", "name", "department", "feishu_open_id")},
                "rule": "unique_exact_name_and_open_id", "app_id": feishu.FEISHU_APP_ID}

    def fail(message, code):
        raise IdentityError(message, code, evidence)

    if emp.get("active") != 1:
        fail("员工已停用", "inactive_employee")
    open_id = emp.get("feishu_open_id")
    if not isinstance(open_id, str) or not open_id.strip():
        fail("缺少飞书 open_id", "missing_open_id")
    if not open_id.startswith("ou_") or open_id.strip() != open_id:
        fail("飞书收件人必须使用当前应用的 open_id（ou_ 开头），不能填写 user_id 或部门 ID", "invalid_open_id")
    if not isinstance(emp.get("name"), str) or not emp["name"].strip():
        fail("缺少员工姓名", "missing_name")
    duplicates = db.query("SELECT id FROM employees WHERE name=? AND id<>?", (emp["name"], emp.get("id", -1)))
    if duplicates:
        fail("存在同名员工，姓名无法唯一对应飞书 ID，请先处理重复姓名", "ambiguous_employee")
    owner = db.query_one("SELECT id FROM employees WHERE feishu_open_id=? AND id<>?", (open_id, emp.get("id", -1)))
    if owner:
        fail("该 open_id 已对应其他员工，停止推送", "identifier_conflict")
    try:
        user = remote_user if remote_user is not None else feishu.get_user(open_id)
    except feishu.FeishuError as exc:
        raise IdentityError(feishu.connection_error(exc), "user_unavailable", evidence) from exc
    except Exception as exc:
        raise IdentityError(feishu.connection_error(exc), "user_unavailable", evidence) from exc
    if not isinstance(user, Mapping):
        fail("飞书用户响应格式无效", "invalid_user")
    evidence["remote"] = {"open_id": user.get("open_id"), "name": user.get("name")}
    if user.get("open_id") != open_id:
        fail("飞书返回的 open_id 与员工资料不一致", "open_id_mismatch")
    if user.get("name") != emp["name"]:
        fail("姓名与飞书通讯录不一致", "name_mismatch")
    status = user.get("status")
    if not isinstance(status, Mapping) or not isinstance(status.get("is_resigned"), bool):
        fail("飞书未明确返回布尔型 is_resigned 在职状态，请检查通讯录权限", "invalid_user_status")
    for key in ("is_resigned", "is_frozen", "is_exited"):
        if key in status and not isinstance(status[key], bool):
            fail("飞书在职状态无效", "invalid_user_status")
        if status.get(key) is True:
            fail("飞书用户已离职、冻结或退出", "inactive_feishu_user")
    try:
        directory = list(directory_users) if directory_users is not None else feishu.list_scope_users()
    except Exception as exc:
        raise IdentityError(feishu.connection_error(exc), "directory_unavailable", evidence) from exc
    matching_ids = set()
    for item in directory:
        if not isinstance(item, Mapping) or not isinstance(item.get("name"), str) or not item["name"].strip() or not item.get("open_id"):
            fail("飞书通讯录缺少姓名或 open_id，无法确认一一对应", "invalid_directory")
        if item["open_id"] == open_id and item["name"] != emp["name"]:
            fail("同一 open_id 返回不同姓名，停止推送", "conflicting_directory_rows")
        if item["name"] == emp["name"]:
            matching_ids.add(item["open_id"])
    if len(matching_ids) > 1:
        fail("飞书通讯录中存在同名人员，无法唯一对应 open_id，停止推送", "ambiguous_feishu_name")
    if matching_ids != {open_id}:
        fail("授权通讯录未找到一致的姓名和 open_id，停止推送", "recipient_not_in_directory")
    evidence["directory_unique"] = True
    return evidence


@feishu.in_application
def validate_identity(emp):
    """Return {ok: True, evidence}; failure raises IdentityError. Never writes."""
    return {"ok": True, "evidence": _validate_identity(emp)}


@feishu.in_application
def verify_employee(employee_id, open_id=None, *, remote_user=None, directory_users=None):
    """Explicitly verify/bind an id; failed checks revoke old approval and cards.

    Network calls happen outside the write lock. The saved employee is compared
    again under BEGIN IMMEDIATE so a stale check cannot authorize changed data.
    Failed candidate bindings do not overwrite the existing open_id.
    """
    employee_id = _id(employee_id)
    with db.tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        original = _get(conn, employee_id)
        _assert_editable(conn, employee_id)
    candidate = dict(original)
    if open_id is not None:
        if not isinstance(open_id, str) or not open_id.strip():
            raise EmployeeError("open_id 不能为空", "missing_open_id")
        candidate["feishu_open_id"] = open_id.strip()
    failure = None
    try:
        evidence = _validate_identity(candidate, remote_user=remote_user, directory_users=directory_users)
    except IdentityError as exc:
        failure, evidence = exc, exc.evidence
    with db.tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = _get(conn, employee_id)
        _assert_editable(conn, employee_id)
        if current != original:
            raise EmployeeError("核验期间员工资料已变化，请重新核验", "employee_changed", 409)
        if failure is None:
            try:
                _check_identifiers(conn, {"feishu_open_id": candidate.get("feishu_open_id")}, employee_id)
                if conn.execute("SELECT 1 FROM employees WHERE name=? AND id<>? LIMIT 1", (current["name"], employee_id)).fetchone():
                    raise EmployeeError("存在同名员工，无法唯一对应飞书 ID", "ambiguous_employee")
            except EmployeeError as exc:
                failure = IdentityError(str(exc), exc.code, evidence)
        if failure is not None:
            status = "unavailable" if failure.code in ("user_unavailable", "department_unavailable", "directory_unavailable") else "failed"
            _reset_identity(conn, employee_id, str(failure), status, evidence)
            _invalidate_events(conn, employee_id, "飞书对应异常：" + str(failure), deactivate=not current["active"])
            return {"ok": False, "id": employee_id, "open_id": original.get("feishu_open_id"),
                    "code": failure.code, "error": str(failure), "evidence": evidence}
        if candidate["feishu_open_id"] != original.get("feishu_open_id"):
            _invalidate_events(conn, employee_id, "飞书绑定已变更，请重新生成并确认贺卡")
        conn.execute("""UPDATE employees SET feishu_open_id=?, identity_status='verified',
                       identity_error=NULL, identity_verified_at=?, identity_snapshot=?, updated_at=? WHERE id=?""",
                     (candidate["feishu_open_id"], evidence["checked_at"], json.dumps(evidence, ensure_ascii=False),
                      db.now(), employee_id))
    return {"ok": True, "id": employee_id, "open_id": candidate["feishu_open_id"], "evidence": evidence}


@feishu.in_application
def match_employee(employee_id):
    """Return explicit candidates, including rejection reasons; never bind."""
    employee_id = _id(employee_id)
    emp = db.query_one("SELECT * FROM employees WHERE id=?", (employee_id,))
    if not emp:
        raise EmployeeError("员工不存在", "not_found", 404)
    try:
        users = feishu.list_scope_users()
    except Exception as exc:
        reason = feishu.connection_error(exc)
        return {"ok": False, "candidates": [], "errors": [reason], "msg": reason, "ambiguous": False}
    candidates, seen = [], set()
    for user in users:
        if not isinstance(user, Mapping) or user.get("name") != emp["name"]:
            continue
        open_id = user.get("open_id")
        if not isinstance(open_id, str) or not open_id or open_id in seen:
            continue
        seen.add(open_id)
        item = {"open_id": open_id, "name": user["name"], "employee_no": user.get("employee_no"),
                "eligible": False, "department": ""}
        try:
            evidence = _validate_identity({**emp, "feishu_open_id": open_id}, directory_users=users)
            owner = db.query_one("SELECT id FROM employees WHERE feishu_open_id=? AND id<>?", (open_id, employee_id))
            item.update(evidence=evidence)
            if owner:
                item.update(code="identifier_conflict", error="该 open_id 已属于其他员工")
            else:
                item["eligible"] = True
        except IdentityError as exc:
            item.update(code=exc.code, error=str(exc), evidence=exc.evidence)
            item["department"] = "、".join(d["name"] for d in exc.evidence.get("remote", {}).get("departments", []))
        candidates.append(item)
    eligible_count = sum(c["eligible"] for c in candidates)
    ambiguous = len(candidates) > 1
    if ambiguous:
        for candidate in candidates:
            candidate.update(eligible=False, code="ambiguous_employee", error="飞书中存在多个同名人员，无法唯一对应")
    return {"ok": bool(eligible_count) and not ambiguous, "candidates": candidates, "ambiguous": ambiguous,
            "requires_selection": False, "msg": "请同步飞书名单，系统按唯一姓名自动对应；同名人员不会自动绑定"}
