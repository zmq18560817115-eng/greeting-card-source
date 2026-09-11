"""Automatically associate exact, unique employee names with Feishu recipients."""
from collections import defaultdict
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
import uuid

from . import db, employees, feishu

_binding_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="employee-binding")
_binding_tasks = {}
_binding_tasks_lock = Lock()


def submit_auto_bind(ids):
    """Return after the local save; directory latency must not hold the editor open."""
    ids = list(dict.fromkeys(employees._id(eid) for eid in ids))
    base = {"matched": 0, "pending": len(ids), "errors": []}
    if not ids:
        return {**base, "status": "done", "msg": "没有需要更新的飞书对应关系。"}
    with _binding_tasks_lock:
        # Bound both queued work and retained results. Saved employee data is never rolled back.
        if sum(task["status"] == "running" for task in _binding_tasks.values()) >= 20:
            return {**base, "status": "failed", "msg": "资料已保存，飞书对应任务较多，请稍后从飞书同步。"}
        for key in list(_binding_tasks):
            if len(_binding_tasks) < 100:
                break
            if _binding_tasks[key]["status"] != "running":
                del _binding_tasks[key]
        task_id = uuid.uuid4().hex
        task = {**base, "task_id": task_id, "status": "running", "msg": "资料已保存，正在后台更新飞书对应关系。"}
        _binding_tasks[task_id] = task
        submitted = dict(task)

    def work():
        try:
            result = {**auto_bind(ids), "status": "done"}
        except Exception as exc:
            result = {**base, "status": "failed", "msg": "资料已保存，飞书对应未完成：" + feishu.connection_error(exc)}
        with _binding_tasks_lock:
            task.update(result)

    try:
        _binding_pool.submit(work)
    except Exception:
        with _binding_tasks_lock:
            task.update(status="failed", msg="资料已保存，飞书对应任务启动失败，请稍后从飞书同步。")
            return dict(task)
    return submitted


def binding_task(task_id):
    with _binding_tasks_lock:
        result = _binding_tasks.get(task_id)
        return dict(result) if result else None


@feishu.in_application
def auto_bind(ids=None, *, users=None):
    result = {"matched": 0, "pending": 0, "errors": [], "msg": ""}
    rows = db.query("SELECT * FROM employees WHERE active=1 ORDER BY id")
    if ids is not None:
        selected = set(ids)
        rows = [row for row in rows if row["id"] in selected]
    if not rows:
        return result
    if users is None:
        if not feishu.FEISHU_APP_ID or not feishu.FEISHU_APP_SECRET:
            result.update(pending=len(rows), msg="资料已保存；连接飞书并同步后，系统会按唯一姓名自动对应收件人。")
            return result
        try:
            users = list(feishu.list_users())
        except Exception as exc:
            result.update(pending=len(rows), msg="资料已保存，暂未完成飞书对应：" + feishu.connection_error(exc))
            return result
    users = list(users)
    by_name, by_id = defaultdict(dict), defaultdict(list)
    for user in users:
        if (isinstance(user, Mapping) and isinstance(user.get("name"), str) and user["name"].strip()
                and isinstance(user.get("open_id"), str) and user["open_id"].strip()):
            by_name[user["name"]][user["open_id"]] = user
            by_id[user["open_id"]].append(user)
    for row in rows:
        candidates = list(by_name.get(row["name"], {}).values())
        reason = ""
        if not candidates:
            reason = "飞书授权通讯录中未找到该姓名，请检查姓名或应用可见范围。"
        elif len(candidates) != 1:
            reason = "飞书中存在多个同名人员，无法唯一对应，请先处理重复姓名。"
        elif any(u != candidates[0] for u in by_id[candidates[0]["open_id"]]):
            reason = "同一飞书 ID 返回了不同人员资料，暂不建立对应关系。"
        elif row.get("feishu_open_id") and row["feishu_open_id"] != candidates[0]["open_id"]:
            reason = "姓名对应的飞书 ID 与现有绑定冲突，请检查是否更换了应用或人员。"
        try:
            if reason:
                with db.tx() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    current = employees._get(conn, row["id"])
                    employees._assert_editable(conn, row["id"])
                    if current != row:
                        raise employees.EmployeeError("资料已变化，请重新同步飞书")
                    employees._reset_identity(conn, row["id"], reason, "failed")
                    # An established but now conflicting name mapping cannot keep an approval.
                    if row.get("feishu_open_id"):
                        employees._invalidate_events(conn, row["id"], "飞书对应异常：" + reason)
                result["pending"] += 1
                result["errors"].append({"id": row["id"], "name": row["name"], "msg": reason})
                continue
            candidate = candidates[0]
            outcome = employees.verify_employee(row["id"], open_id=candidate["open_id"], remote_user=candidate, directory_users=users)
            if outcome["ok"]:
                result["matched"] += 1
            else:
                result["pending"] += 1
                result["errors"].append({"id": row["id"], "name": row["name"], "msg": outcome["error"]})
        except employees.EmployeeError as exc:
            result["pending"] += 1
            result["errors"].append({"id": row["id"], "name": row["name"], "msg": str(exc)})
    result["msg"] = f"飞书自动对应：已对应 {result['matched']} 人，待处理 {result['pending']} 人。"
    return result
