import os, requests, json
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")
mobile = os.getenv("FEISHU_TEST_MOBILE", "").strip()
email = os.getenv("FEISHU_TEST_EMAIL", "").strip()
if not mobile or not email:
    raise SystemExit("请先设置 FEISHU_TEST_MOBILE 和 FEISHU_TEST_EMAIL 为已授权测试员工的联系方式")
aid=os.getenv("FEISHU_APP_ID"); sec=os.getenv("FEISHU_APP_SECRET")
r=requests.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",json={"app_id":aid,"app_secret":sec},timeout=10).json()
tok=r["tenant_access_token"]
H={"Authorization":f"Bearer {tok}"}

# 1. 批量查多个用户详情，看名字
s=requests.get("https://open.feishu.cn/open-apis/contact/v3/scopes",headers=H,params={"user_id_type":"open_id","page_size":100},timeout=10).json()
uids=s["data"].get("user_ids") or []
print("授权 open_id 列表:", uids)

# 2. 用 batch_get_id 逐个方式：手机号 & 邮箱都试
for label, payload in [("手机号", {"mobiles":[mobile]}),
                       ("邮箱", {"emails":[email]})]:
    j=requests.post("https://open.feishu.cn/open-apis/contact/v3/users/batch_get_id",
        headers=H, params={"user_id_type":"open_id"}, json=payload, timeout=10).json()
    print(label, "->", json.dumps(j.get("data"), ensure_ascii=False)[:200])

# 3. 试试用 userIdType=user_id 查（换个 ID 体系）
j=requests.post("https://open.feishu.cn/open-apis/contact/v3/users/batch_get_id",
    headers=H, params={"user_id_type":"open_id"},
    json={"mobiles":[mobile],"emails":[email]},timeout=10).json()
print("组合查询 ->", json.dumps(j.get("data"), ensure_ascii=False)[:300])

# 4. 授权用户详情（虽然字段空，看看能不能拿到 open_id 本身对应谁）
for ou in uids:
    u=requests.get(f"https://open.feishu.cn/open-apis/contact/v3/users/{ou}",
        headers=H, params={"user_id_type":"open_id"}, timeout=10).json()
    d=(u.get("data") or {}).get("user") or {}
    print(ou[-6:], "name=", d.get("name"), "mobile=", d.get("mobile"), "email=", d.get("email"))
