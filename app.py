import os
import tempfile

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import httpx
from pyrogram import Client

# ------------- 环境变量 -------------
# Render Environment 里必须有：
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
    # 目标频道 / 群：可以是 @xxx 或 数字 ID
    chat_id: str

    # Node 可能传 file_url，也可能传 url，这里两个都兼容
    file_url: str | None = None
    url: str | None = None

    caption: str | None = None       # 可选：文字
    parse_mode: str | None = None    # 可选："HTML" / "Markdown"
    kind: str = "video"              # "video" or "photo"


@app.on_event("startup")
async def on_startup():
    await client.start()
    me = await client.get_me()
    print(f"[MTProto] logged in as {me.id} ({me.first_name})")


@app.on_event("shutdown")
async def on_shutdown():
    await client.stop()
    print("[MTProto] client stopped")


@app.get("/")
async def health():
    return {"ok": True, "message": "mtproto uploader is up"}


async def download_to_temp(url: str, suffix: str) -> str:
    """
    把远程 URL 流式下载到临时文件，返回本地路径
    不一次性读入内存，避免 900MB 直接撑爆内存。
    """
    fd, path = tempfile.mkstemp(suffix=suffix)
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=None) as h:
            async with h.stream("GET", url) as r:
                r.raise_for_status()
                with os.fdopen(fd, "wb") as f:
                    async for chunk in r.aiter_bytes():
                        if not chunk:
                            continue
                        f.write(chunk)
        return path
    except Exception:
        # 如果下载失败要把临时文件删掉
        try:
            os.remove(path)
        except OSError:
            pass
        raise


@app.post("/upload")
async def upload(req: UploadRequest):
    """
    由 Node 调用：
    POST /upload
    {
      "chat_id": "@xxxx" 或 数字ID,
      "file_url" 或 "url": "https://...",
      "caption": "...",
      "parse_mode": "HTML",
      "kind": "video" | "photo"
    }
    """
    # 1) 取真正的 URL
    src_url = req.file_url or req.url
    if not src_url:
        raise HTTPException(status_code=400, detail="missing file_url/url")

    try:
        kind = (req.kind or "video").lower()
        suffix = ".mp4" if kind == "video" else ".jpg"

        print(f"[UPLOAD] kind={kind} chat={req.chat_id} url={src_url}")

        # 2) 先下载到本地临时文件（流式）
        path = await download_to_temp(src_url, suffix=suffix)

        # 3) 再通过 MTProto 发送
        try:
            if kind == "video":
                msg = await client.send_video(
                    chat_id=req.chat_id,
                    video=path,
                    caption=req.caption,
                    parse_mode=req.parse_mode,
                    supports_streaming=True,
                )
            else:
                msg = await client.send_photo(
                    chat_id=req.chat_id,
                    photo=path,
                    caption=req.caption,
                    parse_mode=req.parse_mode,
                )
        finally:
            # 4) 不管成功失败，都尝试删除临时文件
            try:
                os.remove(path)
            except OSError:
                pass

        return {"ok": True, "message_id": msg.id}
    except Exception as e:
        # 让 Node 能看到具体错误
        err = f"uploader_error: {e.__class__.__name__}: {e}"
        print("[ERROR]", err)
        raise HTTPException(status_code=500, detail=err)
