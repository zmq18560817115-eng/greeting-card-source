import os, requests
from dotenv import load_dotenv
load_dotenv("/Users/apple/.openclaw/greeting-card/.env")
aid=os.getenv("FEISHU_APP_ID"); sec=os.getenv("FEISHU_APP_SECRET")
r=requests.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",json={"app_id":aid,"app_secret":sec},timeout=10).json()
tok=r["tenant_access_token"]
H={"Authorization":f"Bearer {tok}"}

# 1. 上传图片
img_path="/Users/apple/.openclaw/greeting-card/assets/templates/birthday_base.png"
with open(img_path,"rb") as f:
    up=requests.post("https://open.feishu.cn/open-apis/im/v1/images",headers=H,
        data={"image_type":"message"},files={"image":("birthday_base.png",f,"image/png")},timeout=30).json()
print("upload:",up.get("code"),up.get("msg"))
if up.get("code")!=0: raise SystemExit
img_key=up["data"]["image_key"]

# 2. 发图片消息给授权用户第1个
j=requests.get("https://open.feishu.cn/open-apis/contact/v3/scopes",headers=H,params={"user_id_type":"open_id"},timeout=10).json()
ou=(j["data"].get("user_ids") or [])[0]
s=requests.post("https://open.feishu.cn/open-apis/im/v1/messages",headers=H,params={"receive_id_type":"open_id"},
    json={"receive_id":ou,"msg_type":"image","content":'{"image_key":"%s"}'%img_key},timeout=10).json()
print("send image:",s.get("code"),s.get("msg"))
