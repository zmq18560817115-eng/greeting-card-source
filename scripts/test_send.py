import os, requests
from dotenv import load_dotenv
load_dotenv("/Users/apple/.openclaw/greeting-card/.env")
aid=os.getenv("FEISHU_APP_ID"); sec=os.getenv("FEISHU_APP_SECRET")
r=requests.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",json={"app_id":aid,"app_secret":sec},timeout=10).json()
tok=r["tenant_access_token"]
H={"Authorization":f"Bearer {tok}"}

j=requests.get("https://open.feishu.cn/open-apis/contact/v3/scopes",headers=H,params={"user_id_type":"open_id","page_size":100},timeout=10).json()
uids=j["data"].get("user_ids") or []
print("uids:",uids)

# 给授权范围内第一个用户发测试消息
if uids:
    j2=requests.post("https://open.feishu.cn/open-apis/im/v1/messages",headers=H,params={"receive_id_type":"open_id"},
        json={"receive_id":uids[0],"msg_type":"text","content":'{"text":"贺卡系统权限测试 ✅（请忽略）"}'},timeout=10).json()
    print("send:",j2.get("code"),j2.get("msg"))
