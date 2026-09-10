#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""飞书通讯录取数自检 —— 只用 Python 标准库，不需要 pip install。

用法：
    python3 probe_feishu.py                      # 从同目录 .env 或环境变量读取
    python3 probe_feishu.py <app_id> <app_secret>

它会依次检查：
    1) 能不能拿到 tenant_access_token
    2) 应用的「可用范围」是多少人（scopes）
    3) 能不能遍历部门
    4) 能不能拉到人 + 姓名 / user_id / open_id
    5) join_time（入职时间）到底有没有值
    6) 企业自定义字段里有没有「生日」，有多少人填了
    7) 飞书人事(CoreHR) 通不通，能不能读到出生日期
最后输出 feishu_probe_result.csv（全量名单）和一份结论。
"""
import csv
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

BASE = os.environ.get("FEISHU_BASE", "https://open.feishu.cn").rstrip("/")
CTX = ssl.create_default_context()
OUT_CSV = "feishu_probe_result.csv"

# 权限码 -> 人话
HINTS = {
    99991672: "应用没有这个接口的权限，去 开发者后台 → 权限管理 添加对应权限后【重新发布版本】",
    99991663: "app_id / app_secret 不对，或应用还没发布",
    99991661: "app_id / app_secret 不对",
    20005: "对象不存在（部门 ID 或用户不在可用范围内）",
    91402: "找不到该对象，通常是不在应用可用范围内",
    99991400: "参数不对",
}

ok_marks = {True: "✅", False: "❌"}
_token = {"v": None}


class ApiError(Exception):
    def __init__(self, code, msg, path):
        super().__init__(f"code={code} msg={msg}")
        self.code, self.msg, self.path = code, msg, path


def call(path, method="GET", params=None, body=None, auth=True):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if auth:
        headers["Authorization"] = "Bearer " + _token["v"]
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        raw = urllib.request.urlopen(req, timeout=25, context=CTX).read()
    except urllib.error.HTTPError as e:
        raw = e.read()
    except Exception as e:
        raise ApiError(-1, f"网络错误: {e}", path)
    try:
        j = json.loads(raw.decode("utf-8"))
    except Exception:
        raise ApiError(-1, f"非 JSON 响应: {raw[:200]!r}", path)
    if j.get("code", 0) != 0:
        raise ApiError(j.get("code"), j.get("msg", ""), path)
    return j.get("data", {})


def paged(path, params, item_key="items", page_size=50):
    out, token = [], None
    while True:
        p = dict(params)
        p["page_size"] = page_size
        if token:
            p["page_token"] = token
        d = call(path, params=p)
        out.extend(d.get(item_key) or [])
        if not d.get("has_more"):
            return out
        token = d.get("page_token")
        if not token:
            return out


def ts_to_date(ts):
    if not ts:
        return None
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    if ts > 10_000_000_000:
        ts //= 1000
    try:
        return datetime.fromtimestamp(ts).date().isoformat()
    except Exception:
        return None


def mask(name):
    if not name:
        return ""
    return name[0] + "*" * (len(name) - 1) if len(name) > 1 else name


def load_creds():
    if len(sys.argv) >= 3:
        return sys.argv[1], sys.argv[2]
    aid, sec = os.environ.get("FEISHU_APP_ID"), os.environ.get("FEISHU_APP_SECRET")
    if aid and sec:
        return aid, sec
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (".env", os.path.join(here, ".env"),
                 os.path.join(os.path.dirname(here), ".env")):
        if os.path.exists(cand):
            kv = {}
            for line in open(cand, encoding="utf-8"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    kv[k.strip()] = v.strip()
            if kv.get("FEISHU_APP_ID"):
                return kv["FEISHU_APP_ID"], kv.get("FEISHU_APP_SECRET", "")
    sys.exit("找不到凭据。用法: python3 probe_feishu.py <app_id> <app_secret>")


def section(n, title):
    print(f"\n{'─' * 62}\n【{n}】{title}\n{'─' * 62}")


def fail(e):
    print(f"❌ 失败: code={e.code} {e.msg}")
    if e.code in HINTS:
        print(f"   → {HINTS[e.code]}")
    return None


def main():
    app_id, app_secret = load_creds()
    print(f"环境: {BASE}   应用: {app_id}")
    result = {}

    # 1 token ---------------------------------------------------------------
    section(1, "获取 tenant_access_token")
    req = urllib.request.Request(
        BASE + "/open-apis/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        j = json.loads(urllib.request.urlopen(req, timeout=25, context=CTX).read())
    except urllib.error.HTTPError as e:
        j = json.loads(e.read())
    except Exception as e:
        sys.exit(f"❌ 连不上飞书：{e}\n   → 检查网络/代理，或 FEISHU_BASE 是否该改成 open.larksuite.com")
    if j.get("code", 0) != 0 or not j.get("tenant_access_token"):
        fail(ApiError(j.get("code"), j.get("msg", ""), "token"))
        sys.exit("\n拿不到 token，后面都没法测了。先确认 app_id/app_secret 正确且应用已发布。")
    _token["v"] = j["tenant_access_token"]
    print(f"✅ 成功，有效期 {j.get('expire')} 秒")

    # 2 可用范围 -------------------------------------------------------------
    section(2, "应用可用范围（能看到多少人）")
    scope_users = []
    try:
        d = call("/open-apis/contact/v3/scopes",
                 params={"user_id_type": "open_id", "page_size": 100})
        scope_users = d.get("user_ids") or []
        depts = d.get("department_ids") or []
        print(f"✅ 可见部门 {len(depts)} 个，直接授权用户 {len(scope_users)} 人"
              f"{'（has_more，实际更多）' if d.get('has_more') else ''}")
        if not depts and not scope_users:
            print("   ⚠️ 可用范围是空的！去 开发者后台 → 应用发布 → 可用范围，改成【全体员工】")
    except ApiError as e:
        fail(e)

    # 3 部门 -----------------------------------------------------------------
    section(3, "遍历部门")
    dept_ids = ["0"]
    try:
        items = paged("/open-apis/contact/v3/departments/children",
                      {"parent_department_id": "0", "fetch_child": "true",
                       "department_id_type": "open_department_id"})
        for it in items:
            did = it.get("open_department_id") or it.get("department_id")
            if did:
                dept_ids.append(did)
        print(f"✅ 拿到 {len(items)} 个子部门（加根部门共 {len(dept_ids)} 个）")
        for it in items[:5]:
            print(f"   · {it.get('name')}  ({it.get('member_count', '?')} 人)")
    except ApiError as e:
        fail(e)
        print("   → 只用根部门继续试")

    # 4 拉人 -----------------------------------------------------------------
    section(4, "拉取员工（姓名 / user_id / open_id）")
    users, seen = [], set()
    id_type_ok = True
    for did in dept_ids:
        for id_type in (["user_id", "open_id"] if id_type_ok else ["open_id"]):
            try:
                items = paged("/open-apis/contact/v3/users/find_by_department",
                              {"department_id": did,
                               "department_id_type": "open_department_id",
                               "user_id_type": id_type})
                for u in items:
                    oid = u.get("open_id")
                    if oid and oid in seen:
                        continue
                    if oid:
                        seen.add(oid)
                    users.append(u)
                break
            except ApiError as e:
                if id_type == "user_id" and e.code == 99991672:
                    id_type_ok = False
                    print("   ⚠️ 读不到 user_id（缺 contact:user.employee_id:readonly），改用 open_id")
                    continue
                if did == "0":
                    fail(e)
                break
    print(f"{ok_marks[bool(users)]} 共拿到 {len(users)} 位员工")
    if not users:
        sys.exit("\n一个人都没拉到，先解决上面的权限/可用范围问题。")

    with_openid = sum(1 for u in users if u.get("open_id"))
    with_userid = sum(1 for u in users if u.get("user_id"))
    with_empno = sum(1 for u in users if u.get("employee_no"))
    print(f"   open_id:     {with_openid}/{len(users)}  {'(推送就靠它)' if with_openid else '❌ 没有就没法推送'}")
    print(f"   user_id:     {with_userid}/{len(users)}")
    print(f"   employee_no: {with_empno}/{len(users)}")

    # 5 入职时间 -------------------------------------------------------------
    section(5, "入职时间 join_time")
    join_dates = {u.get("open_id"): ts_to_date(u.get("join_time")) for u in users}
    has_join = sum(1 for v in join_dates.values() if v)
    print(f"{ok_marks[has_join > 0]} {has_join}/{len(users)} 人有入职日期")
    if has_join == 0:
        print("   → 两种可能：")
        print("     a) 缺权限 contact:user.employee_job:readonly（加完要重新发布版本）")
        print("     b) HR 压根没在飞书通讯录里填「入职时间」——那就得走本地表格维护")
    else:
        for u in users[:5]:
            d = join_dates.get(u.get("open_id"))
            if d:
                print(f"   · {mask(u.get('name'))}  入职 {d}")

    # 6 生日（自定义字段）----------------------------------------------------
    section(6, "生日（企业自定义字段）")
    attr_defs, birth_ids = [], set()
    try:
        attr_defs = paged("/open-apis/contact/v3/custom_attrs", {})
        print(f"✅ 企业共配置了 {len(attr_defs)} 个自定义字段：")
        for a in attr_defs:
            label = a.get("i18n_name")
            label = (label[0].get("value") if isinstance(label, list) and label else None) or a.get("name") or ""
            flag = ""
            if any(h in str(label) for h in ("生日", "出生")) or "birth" in str(label).lower():
                birth_ids.add(a.get("id"))
                flag = "   ← 疑似生日字段"
            print(f"   · id={a.get('id')}  名称={label}  类型={a.get('type')}{flag}")
        if not attr_defs:
            print("   ⚠️ 一个自定义字段都没有 → 飞书里没地方存生日")
    except ApiError as e:
        fail(e)
        print("   → 缺 contact:custom_attr.read；或者企业本来就没配自定义字段")

    birth_dates = {}
    sample_attr_dump = None
    for u in users:
        for item in (u.get("custom_attrs") or []):
            if sample_attr_dump is None:
                sample_attr_dump = u.get("custom_attrs")
            if birth_ids and item.get("id") not in birth_ids:
                continue
            val = (item.get("value") or {}).get("text") or ""
            if val:
                birth_dates[u.get("open_id")] = val
                break
    has_birth = len(birth_dates)
    print(f"\n{ok_marks[has_birth > 0]} {has_birth}/{len(users)} 人的自定义字段里读到了生日值")
    if has_birth:
        for oid, v in list(birth_dates.items())[:5]:
            nm = next((mask(u.get("name")) for u in users if u.get("open_id") == oid), "")
            print(f"   · {nm}  {v}")
    elif sample_attr_dump:
        print("   （能读到自定义字段但没匹配上生日，下面是一条原始数据，看看格式）")
        print("   " + json.dumps(sample_attr_dump, ensure_ascii=False)[:500])

    # 7 CoreHR ---------------------------------------------------------------
    section(7, "飞书人事 CoreHR（另一条读生日的路，可选）")
    try:
        d = call("/open-apis/corehr/v2/employees/batch_get", "POST",
                 params={"user_id_type": "open_id"},
                 body={"user_ids": [u["open_id"] for u in users[:3] if u.get("open_id")],
                       "fields": ["person_info"]})
        items = d.get("items") or []
        print(f"✅ CoreHR 可访问，试读 {len(items)} 条")
        got = 0
        for it in items:
            dob = ((it.get("person_info") or {}).get("date_of_birth"))
            if dob:
                got += 1
                print(f"   · date_of_birth = {dob}")
        if not got:
            print("   ⚠️ 接口通了但没读到 date_of_birth（可能没买人事模块 / 字段为空）")
    except ApiError as e:
        print(f"⚪ 不可用: code={e.code} {e.msg}")
        print("   （没开飞书人事就是这个结果，属正常；只要上面第 6 步有生日就够了）")

    # 8 汇总 -----------------------------------------------------------------
    section(8, "结论与名单导出")
    rows = []
    for u in users:
        oid = u.get("open_id")
        rows.append({
            "name": u.get("name"),
            "employee_no": u.get("employee_no") or "",
            "email": u.get("email") or u.get("enterprise_email") or "",
            "feishu_user_id": u.get("user_id") or "",
            "feishu_open_id": oid or "",
            "department": ",".join(u.get("department_ids") or []),
            "join_date": join_dates.get(oid) or "",
            "birth_date": birth_dates.get(oid) or "",
        })
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    both = sum(1 for r in rows if r["join_date"] and r["birth_date"] and r["feishu_open_id"])
    print(f"总人数            {len(rows)}")
    print(f"有 open_id        {with_openid}")
    print(f"有入职日期        {has_join}")
    print(f"有生日            {has_birth}")
    print(f"三者齐全（可推送）{both}")
    print(f"\n📄 名单已导出: {os.path.abspath(OUT_CSV)}")
    print("   → 缺的字段在 CSV 里补齐，就能直接从审核台「导入 CSV」进系统")
    miss_join = [r["name"] for r in rows if not r["join_date"]]
    miss_birth = [r["name"] for r in rows if not r["birth_date"]]
    miss_oid = [r["name"] for r in rows if not r["feishu_open_id"]]
    for label, lst in (("缺入职日期", miss_join), ("缺生日", miss_birth), ("缺 open_id", miss_oid)):
        if lst:
            shown = "、".join(mask(n) for n in lst[:12])
            more = f" 等 {len(lst)} 人" if len(lst) > 12 else ""
            print(f"\n⚠️ {label}({len(lst)}): {shown}{more}   —— 真实姓名见 CSV")

    print()
    if has_join == len(rows) and has_birth == len(rows):
        print("🎉 飞书数据完整，直接用「从飞书同步名单」即可，不用维护本地表。")
    elif has_join and has_birth:
        print("👉 两个字段都能读到，但有人没填。两条路：")
        print("   a) 让 HR 在飞书通讯录里把缺的补上，再同步一次（推荐，长期省事）")
        print("   b) 直接在导出的 CSV 里补，从审核台「导入 CSV」进系统")
    elif has_join:
        print("👉 入职时间能取到，生日取不到 —— 生日走本地维护（CSV 导入 / 审核台页面上填）。")
    elif has_birth:
        print("👉 生日能取到，入职时间取不到 —— 多半是缺 contact:user.employee_job:readonly，"
              "加完权限要【重新发布版本】才生效。")
    else:
        print("👉 两个都取不到 —— 先按第 5、6 步的提示排查权限；"
              "排查完还是没有，就说明飞书里压根没填，两个字段都走本地 CSV 维护。")


if __name__ == "__main__":
    main()
