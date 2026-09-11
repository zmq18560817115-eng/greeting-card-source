"""FastAPI 人事后台：名单、身份核验、模板排版与审核推送。"""
import csv
import io
import logging
import sqlite3
import uuid
from datetime import date
from pathlib import Path

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import compose, employees, feishu, feishu_config, pipeline, presentation, push, scheduler, sync, templates
from .dates import completed_years_since, next_cycle, parse_date, this_cycle
from .db import init_db, now, query, query_one, tx
from .settings import ADMIN_TOKEN, BASE_DIR, DRY_RUN, OUTPUT_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
                    handlers=[logging.StreamHandler(), logging.FileHandler(BASE_DIR / "logs" / "app.log", encoding="utf-8")])
log = logging.getLogger("api")
app = FastAPI(title="员工贺卡推送系统", docs_url="/docs")
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/files", StaticFiles(directory=OUTPUT_DIR), name="files")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def auth(x_admin_token: str = Header(default=""), token: str = Query(default="")):
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN and token != ADMIN_TOKEN:
        raise HTTPException(401, "口令不对")
    return True


@app.on_event("startup")
def _startup():
    init_db()
    from .recovery import recover_interrupted_jobs
    recover_interrupted_jobs()
    scheduler.start()
    log.info("服务已启动 DRY_RUN=%s", DRY_RUN)


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


def _event_view(ev):
    emp = query_one("SELECT * FROM employees WHERE id=?", (ev["employee_id"],)) or {}
    cards = query("SELECT * FROM cards WHERE event_id=? ORDER BY idx", (ev["id"],))
    for card in cards:
        card["url"] = f"/files/cards/{Path(card['file_path']).name}" if card.get("file_path") else None
    return {**ev, **presentation.delivery_fields(ev),
            "anniversary_years": completed_years_since(emp.get("join_date"), ev.get("event_date")) if parse_date(ev.get("event_date")) else None,
            "exception_hint": presentation.event_notice(ev, emp),
            "employee": presentation.employee_fields({k: emp.get(k) for k in
            ("id", "name", "employee_no", "department", "join_date", "birth_date", "active", "email",
             "identity_status", "identity_error", "identity_verified_at")} |
            {"open_id": emp.get("feishu_open_id")}), "cards": cards}


@app.get("/api/events")
def list_events(scope: str = "all", _=Depends(auth)):
    sql, params = "SELECT * FROM events", []
    if scope in ("next", "this"):
        start, end = next_cycle() if scope == "next" else this_cycle()
        sql += " WHERE event_date BETWEEN ? AND ?"
        params = [start.isoformat(), end.isoformat()]
    elif scope == "pending":
        sql += " WHERE status IN ('ready','generating','blocked','gen_failed','needs_regeneration')"
    elif scope != "all":
        raise ValueError("未知周期")
    return [_event_view(ev) for ev in query(sql + " ORDER BY event_date, trigger_at", params)]


@app.get("/api/events/{event_id}")
def get_event(event_id: int, _=Depends(auth)):
    ev = query_one("SELECT * FROM events WHERE id=?", (event_id,))
    if not ev:
        raise HTTPException(404, "事件不存在")
    return _event_view(ev)


@app.post("/api/events/{event_id}/select")
def select_card(event_id: int, card_id: int = Body(..., embed=True), _=Depends(auth)):
    with tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if not row:
            raise HTTPException(404, "事件不存在")
        ev = dict(row)
        if ev["status"] not in ("ready", "confirmed", "failed", "blocked", "simulated"):
            raise ValueError("此状态不能更换海报")
        ev["selected_card_id"] = card_id
        push._review_data(conn, ev)
        conn.execute("""UPDATE events SET selected_card_id=?,status='ready',confirmed_at=NULL,
                     confirmed_by=NULL,employee_snapshot=NULL,delivery_uuid=NULL,last_error=NULL,updated_at=? WHERE id=?""",
                     (card_id, now(), event_id))
    return {"ok": True}


@app.post("/api/events/{event_id}/confirm")
def confirm(event_id: int, operator: str = Body("hr", embed=True), _=Depends(auth)):
    return push.confirm_event(event_id, operator)


@app.post("/api/events/{event_id}/skip")
def skip(event_id: int, _=Depends(auth)):
    with tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT status FROM events WHERE id=?", (event_id,)).fetchone()
        if not row:
            raise HTTPException(404, "事件不存在")
        if row["status"] in ("pushing", "pushed", "delivery_unknown"):
            raise ValueError("发送中、已发送或回执不明的事件不能跳过")
        conn.execute("""UPDATE events SET status='skipped',generation_token=NULL,
                     confirmed_at=NULL,confirmed_by=NULL,updated_at=? WHERE id=?""", (now(), event_id))
    return {"ok": True}


@app.post("/api/events/{event_id}/regenerate")
def regenerate(event_id: int, count: int | None = Body(None, embed=True), _=Depends(auth)):
    pipeline.submit_generate(event_id, count=count)
    return {"ok": True, "submitted": True}


@app.post("/api/events/{event_id}/push")
def manual_push(event_id: int, force: bool = Body(True, embed=True),
                operator: str = Body("hr", embed=True), _=Depends(auth)):
    return push.push_event(event_id, operator=operator, force=force)


@app.get("/api/events/{event_id}/logs")
def event_logs(event_id: int, _=Depends(auth)):
    return query("SELECT * FROM push_logs WHERE event_id=? ORDER BY id DESC", (event_id,))


@app.get("/api/employees")
def list_employees(keyword: str = "", only_active: bool = True, _=Depends(auth)):
    sql, params = "SELECT * FROM employees WHERE 1=1", []
    if only_active:
        sql += " AND active=1"
    if keyword:
        sql += " AND (name LIKE ? OR employee_no LIKE ? OR email LIKE ? OR department LIKE ? OR feishu_open_id LIKE ?)"
        params += [f"%{keyword}%"] * 5
    return [presentation.employee_fields(emp) for emp in query(sql + " ORDER BY name,id", params)]


@app.post("/api/employees")
def upsert_employee(payload: dict = Body(...), _=Depends(auth)):
    return employees.save_employee(payload)


HEADER_MAP = {
    "姓名": "name", "工号": "employee_no", "邮箱": "email", "部门": "department",
    "入职日期": "join_date", "入职时间": "join_date", "生日": "birth_date", "出生日期": "birth_date",
    "飞书id": "feishu_open_id", "飞书open_id": "feishu_open_id", "open_id": "feishu_open_id",
    "飞书user_id": "feishu_user_id", "备注": "note",
}
for _field in ("name", "employee_no", "email", "department", "join_date", "birth_date",
               "feishu_open_id", "feishu_user_id", "note"):
    HEADER_MAP[_field] = _field


@app.post("/api/employees/import")
async def import_employees(file: UploadFile = File(...), _=Depends(auth)):
    raw = await file.read(5 * 1024 * 1024 + 1)
    if not raw or len(raw) > 5 * 1024 * 1024:
        raise ValueError("导入文件不能为空且不能超过5MB")
    filename = (file.filename or "").lower()
    if filename.endswith(".xlsx"):
        import openpyxl
        import zipfile
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                if sum(item.file_size for item in archive.infolist()) > 30 * 1024 * 1024:
                    raise ValueError("Excel解压后内容过大")
            wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
            try:
                values = []
                for row in wb.active.iter_rows(values_only=True):
                    values.append(list(row))
                    if len(values) > 10001:
                        raise ValueError("单次最多导入10000行")
            finally:
                wb.close()
        except (zipfile.BadZipFile, KeyError) as exc:
            raise ValueError("不是有效的XLSX文件") from exc
    elif filename.endswith(".csv"):
        try:
            content = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            content = raw.decode("gb18030")
        values = list(csv.reader(io.StringIO(content)))
    else:
        raise ValueError("仅支持CSV或XLSX文件")
    if len(values) < 2 or len(values) > 10001:
        raise ValueError("文件需包含表头和1到10000行员工资料")
    headers = [HEADER_MAP.get(str(h or "").strip().lower()) for h in values[0]]
    mapped = [h for h in headers if h]
    if "name" not in mapped:
        raise ValueError("缺少姓名/name表头")
    if len(mapped) != len(set(mapped)):
        raise ValueError("表头含重复字段，请检查中英文同义列")
    rows = []
    for number, values_row in enumerate(values[1:], 2):
        if not any(v is not None and str(v).strip() for v in values_row):
            continue
        row = {key: values_row[i] for i, key in enumerate(headers) if key and i < len(values_row)}
        rows.append((number, row))
    if not rows:
        raise ValueError("没有可导入的员工资料")
    result = employees.import_rows(rows)
    for error in result.get("errors", []):
        error["msg"] = error.get("msg") or error.get("error", "导入失败")
    return result


@app.post("/api/employees/batch-disable")
def batch_disable(payload: dict = Body(...), _=Depends(auth)):
    return employees.deactivate(payload.get("ids", []))


@app.delete("/api/employees/{employee_id}")
def delete_employee(employee_id: int, _=Depends(auth)):
    result = employees.deactivate([employee_id])
    return {**result, "mode": "disabled", "msg": "已停用并取消待发送任务，历史记录保留"}


@app.get("/api/employees/template.csv")
def csv_template(_=Depends(auth)):
    return {"csv": "姓名,工号,部门,邮箱,飞书open_id,入职日期,生日\n"}


@app.post("/api/employees/verify")
def verify_batch(payload: dict = Body(...), _=Depends(auth)):
    ids = payload.get("ids", [])
    if not isinstance(ids, list) or not ids or len(ids) > 200:
        raise ValueError("每次请选择1到200位员工")
    ids = [employees._id(eid) for eid in ids]
    results = []
    for eid in dict.fromkeys(ids):
        try:
            result = employees.verify_employee(eid)
            results.append({**result, "msg": result.get("error") or "姓名、部门、飞书ID核验通过"})
        except ValueError as exc:
            results.append({"id": eid, "ok": False, "msg": str(exc)})
    return {"results": results}


@app.post("/api/employees/match-feishu")
def match_feishu(payload: dict = Body(...), _=Depends(auth)):
    return employees.match_employee(payload.get("employee_id"))


@app.post("/api/employees/bind-feishu")
def bind_feishu(payload: dict = Body(...), _=Depends(auth)):
    if not payload.get("open_id"):
        raise ValueError("open_id必填")
    result = employees.verify_employee(payload.get("employee_id"), open_id=payload["open_id"])
    return {**result, "msg": result.get("error") or "三项核验通过，已绑定"}


@app.get("/api/employees/missing")
def missing(_=Depends(auth)):
    return sync.missing_report()


@app.post("/api/sync")
def do_sync(fill_birthday: bool = Body(True, embed=True), _=Depends(auth)):
    return sync.sync_from_feishu(fill_birthday=fill_birthday)


def _cycle(scope):
    if scope not in ("this", "next"):
        raise ValueError("周期应为this或next")
    return this_cycle() if scope == "this" else next_cycle()


@app.post("/api/run-weekly")
def do_weekly(scope: str = Body("next", embed=True), _=Depends(auth)):
    tid, task = pipeline.run_weekly_async(cycle=_cycle(scope))
    return {"task_id": tid, "created": task["created"], "total": task["total"], "cycle_start": task["cycle"][0], "cycle_end": task["cycle"][1]}


@app.get("/api/gen-task/{task_id}")
def gen_task(task_id: str, _=Depends(auth)):
    task = pipeline.async_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


@app.post("/api/scan")
def do_scan(scope: str = Body("next", embed=True), _=Depends(auth)):
    created, start, end = pipeline.scan_cycle(cycle=_cycle(scope))
    return {"created": created, "cycle_start": start.isoformat(), "cycle_end": end.isoformat()}


@app.get("/api/feishu/config")
def get_feishu_config(_=Depends(auth)):
    return feishu_config.public_config()


@app.post("/api/feishu/config")
def save_feishu_config(payload: dict = Body(...), _=Depends(auth)):
    return feishu_config.save_config(payload.get("app_id"), payload.get("app_secret", ""))


@app.post("/api/feishu/check")
def check_feishu_connection(_=Depends(auth)):
    return feishu_config.check_connection()


@app.get("/api/health")
def health(_=Depends(auth)):
    start, end = next_cycle()
    feishu_ok, feishu_err = True, None
    try:
        feishu.ping()
    except Exception as exc:
        feishu_ok, feishu_err = False, str(exc)
    return {"dry_run": DRY_RUN, "next_cycle": [start.isoformat(), end.isoformat()],
            "employees_active": query_one("SELECT COUNT(*) c FROM employees WHERE active=1")["c"],
            "event_stats": {r["status"]: r["c"] for r in query("SELECT status,COUNT(*) c FROM events GROUP BY status")},
            "jobs": scheduler.jobs(), "feishu": {"ok": feishu_ok, "error": feishu_err}}


@app.exception_handler(ValueError)
async def validation_error(request, exc):
    return JSONResponse(status_code=getattr(exc, "status_code", 400), content={"detail": str(exc)})


@app.exception_handler(sqlite3.IntegrityError)
async def conflict_error(request, exc):
    return JSONResponse(status_code=409, content={"detail": "员工标识冲突或数据关联已变更，请刷新后检查"})


@app.exception_handler(feishu.FeishuError)
async def feishu_error(request, exc):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def unexpected_error(request, exc):
    log.exception("接口异常 %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "操作失败，请查看服务日志或检查飞书权限配置"})


@app.get("/api/templates")
def get_templates(_=Depends(auth)):
    return compose.load_config()


@app.get("/api/fonts")
def get_fonts(_=Depends(auth)):
    return {"fonts": templates.list_fonts()}


@app.post("/api/templates")
def save_templates(payload: dict = Body(...), _=Depends(auth)):
    cfg = templates.merge_config(compose.load_config(), payload)
    _render_preview(cfg, {})
    templates.save_config(cfg)
    return {"ok": True}


@app.post("/api/templates/{key}/background")
async def upload_background(key: str, file: UploadFile = File(...), _=Depends(auth)):
    if key not in compose.load_config()["templates"]:
        raise ValueError("未知模板")
    raw = await file.read(10 * 1024 * 1024 + 1)
    return templates.save_base_image(raw, file.filename or "", max_bytes=10 * 1024 * 1024)


def _render_preview(cfg, payload):
    day = parse_date(payload.get("event_date") or now()[:10])
    if not day:
        raise ValueError("预览日期无效")
    if payload.get("employee_id"):
        emp = query_one("SELECT * FROM employees WHERE id=?", (payload["employee_id"],))
        if not emp:
            raise ValueError("预览员工不存在")
    else:
        # With no recorded employee selected, show template fields literally.
        emp = {field: "{" + field + "}" for field in
               ("name", "department", "join_date", "birth_date", "id")}
        emp["id"] = "{employee_id}"
    images = {}
    for key in cfg["templates"]:
        event_years = "{years}"
        if payload.get("employee_id"):
            source_date = parse_date(emp.get("join_date" if key == "anniversary" else "birth_date"))
            event_years = max(0, day.year - source_date.year) if source_date and (key == "anniversary" or source_date.year > 1901) else ""
        ctx = pipeline.build_context({"event_date": day.isoformat(), "years": event_years}, emp)
        path = OUTPUT_DIR / f"preview_{key}_{uuid.uuid4().hex}.png"
        compose.render(key, ctx, out_path=path, cfg=cfg)
        images[key] = "/files/" + path.name
    return {"ok": True, "images": images}


@app.post("/api/templates/preview")
def preview_template(payload: dict = Body(...), _=Depends(auth)):
    cfg = templates.merge_config(compose.load_config(), {k: payload[k] for k in ("templates", "vars", "canvas") if k in payload})
    return _render_preview(cfg, payload)
