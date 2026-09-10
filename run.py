"""启动服务：python run.py"""
import uvicorn

from app.settings import HOST, PORT

if __name__ == "__main__":
    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=False)
