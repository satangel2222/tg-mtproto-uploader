import os
import asyncio
import tempfile
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pyrogram import Client

# 从环境变量读 Telegram 配置
API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
STRING = os.environ["TG_STRING_SESSION"]

app = FastAPI(title="TG MTProto Uploader")

# Pyrogram 客户端（使用你的账号，不是 Bot）
client = Client(
    "uploader",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=STRING,
    workdir="/tmp"
)

@app.on_event("startup")
async def startup():
    await client.start()

@app.on_event("shutdown")
async def shutdown():
    await client.stop()

class UploadRequest(BaseModel):
    chat_id: str
    url: str
    type: str = "video"          # "video" 或 "photo"
    caption: str | None = None
    parse_mode: str | None = None

async def download_to_temp(url: str, suffix: str) -> str:
    # 把远程 URL 流式下载到临时文件，支援大体积
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp_path = tmp.name
    tmp.close()

    async with httpx.AsyncClient(follow_redirects=True, timeout=None) as http:
        async with http.stream("GET", url) as resp:
            if resp.status_code >= 400:
                raise HTTPException(status_code=resp.status_code, detail=f"download failed: {resp.status_code}")
            with open(tmp_path, "wb") as f:
                async for chunk in resp.aiter_bytes():
                    if chunk:
                        f.write(chunk)
    return tmp_path

@app.post("/upload")
async def upload(req: UploadRequest):
    suffix = ".mp4" if req.type == "video" else ".jpg"

    file_path = await download_to_temp(req.url, suffix)
    try:
        if req.type == "video":
            msg = await client.send_video(
                req.chat_id,
                file_path,
                caption=req.caption or "",
                parse_mode=req.parse_mode
            )
        else:
            msg = await client.send_photo(
                req.chat_id,
                file_path,
                caption=req.caption or "",
                parse_mode=req.parse_mode
            )
    finally:
        try:
            os.remove(file_path)
        except FileNotFoundError:
            pass

    return {"ok": True, "chat_id": req.chat_id, "message_id": msg.id}

@app.get("/")
async def root():
    return {"ok": True, "msg": "tg mtproto uploader running"}
