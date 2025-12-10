import os
import tempfile
import asyncio

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import httpx
from pyrogram import Client

# ------------- 环境变量 -------------
# 一定要在 Render Environment 里设置：
# TG_API_ID, TG_API_HASH, TG_STRING_SESSION
try:
    TG_API_ID = int(os.environ["TG_API_ID"])
    TG_API_HASH = os.environ["TG_API_HASH"]
    TG_STRING_SESSION = os.environ["TG_STRING_SESSION"]
except KeyError as e:
    raise RuntimeError(f"Missing env var: {e.args[0]}")

# ------------- Pyrogram Client -------------
app = FastAPI(title="TG MTProto Uploader")

client = Client(
    "mtuploader",
    api_id=TG_API_ID,
    api_hash=TG_API_HASH,
    session_string=TG_STRING_SESSION,
    in_memory=True,
)


class UploadRequest(BaseModel):
    chat_id: str
    file_url: str
    caption: str | None = None
    parse_mode: str | None = None  # "HTML" / "Markdown" / None
    kind: str = "video"            # "video" or "photo"


@app.on_event("startup")
async def on_startup():
    await client.start()


@app.on_event("shutdown")
async def on_shutdown():
    await client.stop()


@app.get("/")
async def health():
    return {"ok": True, "message": "mtproto uploader is up"}


async def download_to_temp(url: str, suffix: str) -> str:
    """
    把远程 URL 下载到临时文件，返回本地路径
    """
    async with httpx.AsyncClient(follow_redirects=True, timeout=300) as h:
        r = await h.get(url)
        r.raise_for_status()
        data = r.content

    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


@app.post("/upload")
async def upload(req: UploadRequest):
    """
    由 Node 调用：
    POST /upload
    {
      "chat_id": "@xxxx" 或 数字ID,
      "file_url": "https://...",
      "caption": "...",
      "parse_mode": "HTML",
      "kind": "video" | "photo"
    }
    """
    try:
        kind = (req.kind or "video").lower()
        suffix = ".mp4" if kind == "video" else ".jpg"

        path = await download_to_temp(req.file_url, suffix=suffix)

        try:
            if kind == "video":
                m = await client.send_video(
                    chat_id=req.chat_id,
                    video=path,
                    caption=req.caption,
                    parse_mode=req.parse_mode,
                    supports_streaming=True,
                )
            else:
                m = await client.send_photo(
                    chat_id=req.chat_id,
                    photo=path,
                    caption=req.caption,
                    parse_mode=req.parse_mode,
                )
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

        return {"ok": True, "message_id": m.id}
    except Exception as e:
        # 让 Node 能看到具体错误
        raise HTTPException(status_code=500, detail=f"uploader_error: {e}")
