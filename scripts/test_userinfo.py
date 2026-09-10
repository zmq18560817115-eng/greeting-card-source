import os, requests, json
from dotenv import load_dotenv
load_dotenv("/Users/apple/.openclaw/greeting-card/.env")
aid=os.getenv("FEISHU_APP_ID"); sec=os.getenv("FEISHU_APP_SECRET")
r=requests.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",json={"app_id":aid,"app_secret":sec},timeout=10).json()
tok=r["tenant_access_token"]
H={"Authorization":f"Bearer {tok}"}

j=requests.get("https://open.feishu.cn/open-apis/contact/v3/scopes",headers=H,params={"user_id_type":"open_id","page_size":100},timeout=10).json()
uids=j["data"].get("user_ids") or []

for ou in uids:
    u=requests.get(f"https://open.feishu.cn/open-apis/contact/v3/users/{ou}",headers=H,params={"user_id_type":"open_id","department_id_type":"open_department_id"},timeout=10).json()
    if u.get("code")!=0:
        print(ou,"err",u.get("code"),u.get("msg")); continue
    d=u["data"]["user"]
    print(f'open_id={ou}')
    print(f'  name={d.get("name")} gender={d.get("gender")}')
    print(f'  birthday={d.get("birthday")!r} hire_date/employee_no={d.get("employee_no")!r}')
    print(f'  所有含日期的字段: '+", ".join(f'{k}={v}' for k,v in d.items() if v and ("date" in k.lower() or "time" in k.lower() or k in ("birthday","employee_no"))))
