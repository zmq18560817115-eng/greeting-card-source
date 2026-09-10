import os, requests
from dotenv import load_dotenv
load_dotenv("/Users/apple/.openclaw/greeting-card/.env")
aid=os.getenv("FEISHU_APP_ID"); sec=os.getenv("FEISHU_APP_SECRET")
r=requests.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",json={"app_id":aid,"app_secret":sec},timeout=10).json()
tok=r["tenant_access_token"]
H={"Authorization":f"Bearer {tok}"}

def dept_users(dept_open_id="0", page_size=50):
    users=[]
    pt=None
    while True:
        p={"department_id":dept_open_id,"page_size":page_size,"user_id_type":"open_id"}
        if pt: p["page_token"]=pt
        j=requests.get("https://open.feishu.cn/open-apis/contact/v3/users/find_by_department",headers=H,params=p,timeout=10).json()
        if j.get("code")!=0:
            print("find_by_department:",j.get("code"),j.get("msg")); return users
        users+=j["data"].get("items",[])
        if not j["data"].get("has_more"): break
        pt=j["data"]["page_token"]
    return users

users=dept_users()
print(f"根部门用户数: {len(users)}")
for u in users[:10]:
    bd = u.get("birthday") or "?"
    if bd == "0": bd = "?"
    print(f'- {u.get("name")} open_id={u.get("open_id")} birthday={bd} employee_no={u.get("employee_no")}')

if users:
    ou=users[0]["open_id"]; name=users[0].get("name")
    j=requests.post("https://open.feishu.cn/open-apis/im/v1/messages",headers=H,params={"receive_id_type":"open_id"},json={"receive_id":ou,"msg_type":"text","content":'{"text":"贺卡系统权限测试 ✅（请忽略）"}'},timeout=10).json()
    print(f"send to {name}:",j.get("code"),j.get("msg"))
