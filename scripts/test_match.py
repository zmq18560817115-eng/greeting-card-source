import os, requests, json
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")
mobile = os.getenv("FEISHU_TEST_MOBILE", "").strip()
if not mobile:
    raise SystemExit("请先设置 FEISHU_TEST_MOBILE 为已授权测试员工的手机号")
aid=os.getenv("FEISHU_APP_ID"); sec=os.getenv("FEISHU_APP_SECRET")
r=requests.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",json={"app_id":aid,"app_secret":sec},timeout=10).json()
tok=r["tenant_access_token"]
H={"Authorization":f"Bearer {tok}"}

# 1. 已授权测试员工手机号换 open_id
j=requests.post("https://open.feishu.cn/open-apis/contact/v3/users/batch_get_id",
    headers=H, params={"user_id_type":"open_id"},
    json={"mobiles":[mobile]},timeout=10).json()
print("mobile lookup:", json.dumps(j.get("data"), ensure_ascii=False))

# 2. 授权范围
s=requests.get("https://open.feishu.cn/open-apis/contact/v3/scopes",headers=H,params={"user_id_type":"open_id","page_size":100},timeout=10).json()
uids=s["data"].get("user_ids") or []
print("授权用户数:", len(uids))
