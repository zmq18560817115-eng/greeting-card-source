import os, requests
from dotenv import load_dotenv
load_dotenv("/Users/apple/.openclaw/greeting-card/.env")
aid=os.getenv("FEISHU_APP_ID"); sec=os.getenv("FEISHU_APP_SECRET")
r=requests.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",json={"app_id":aid,"app_secret":sec},timeout=10).json()
tok=r["tenant_access_token"]
H={"Authorization":f"Bearer {tok}"}

# 授权范围内的5个用户，逐个查详情（含生日）
j=requests.get("https://open.feishu.cn/open-apis/contact/v3/scopes",headers=H,params={"user_id_type":"open_id","page_size":100},timeout=10).json()
uids=j["data"].get("user_ids") or []
print(f"授权用户 {len(uids)} 个")
for ou in uids:
    u=requests.get(f"https://open.feishu.cn/open-apis/contact/v3/users/{ou}",headers=H,params={"user_id_type":"open_id"},timeout=10).json()
    if u.get("code")!=0:
        print(ou,"err",u.get("code"),u.get("msg")); continue
    d=u["data"]["user"]
    bd=d.get("birthday") or ""
    print(f'- {d.get("name")} birthday={bd if bd and bd!="0" else "未填"} employee_no={d.get("employee_no")}')
