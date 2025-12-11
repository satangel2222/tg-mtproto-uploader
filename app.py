import os
import tempfile

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import httpx
from pyrogram import Client
from pyrogram.enums import ParseMode

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
    parse_mode: str | None = None  # "HTML" / "Markdown" / None（Node 传字符串）
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


def is_http_url(u: str) -> bool:
    if not u or not isinstance(u, str):
        return False
    s = u.strip()
    if not s:
        return False
    return s.lower().startswith("http://") or s.lower().startswith("https://")


async def download_to_temp(url: str, suffix: str) -> str:
    """
    把远程 URL **流式** 下载到临时文件，返回本地路径
    （不会把整个文件一次性塞进内存）
    """
    if not is_http_url(url):
        raise ValueError("file_url must be an http(s) URL")

    fd, path = tempfile.mkstemp(suffix=suffix)

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=None) as h:
            async with h.stream("GET", url) as r:
                r.raise_for_status()
                with os.fdopen(fd, "wb") as f:
                    async for chunk in r.aiter_bytes(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        f.write(chunk)
        return path
    except Exception:
        # 出错时把空文件删掉
        try:
            os.remove(path)
        except OSError:
            pass
        raise


def to_parse_mode_enum(mode_str: str | None):
    """
    把 Node 传进来的 parse_mode（可能是 "HTML"、"Markdown"，
    也可能是 '"HTML"' 这种多一层引号的），转成 Pyrogram 的枚举；
    有问题就返回 None（相当于不用 parse_mode）。
    """
    if not mode_str:
        return None

    s = str(mode_str).strip()

    # 去掉外面多余的引号：比如 '"HTML"' / "'HTML'"
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()

    s_up = s.upper()
    if s_up == "HTML":
        return ParseMode.HTML
    if s_up.startswith("MARKDOWN"):
        return ParseMode.MARKDOWN

    # 其他乱七八糟的就不用 parse_mode，当普通文本
    return None


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
        if not is_http_url(req.file_url):
            raise HTTPException(status_code=400, detail=f"invalid_file_url: {req.file_url}")

        kind = (req.kind or "video").lower()
        suffix = ".mp4" if kind == "video" else ".jpg"

        pm = to_parse_mode_enum(req.parse_mode)
        print(
            f"[UPLOAD] kind={kind} chat={req.chat_id} "
            f"pm={pm} raw_pm={req.parse_mode} url={req.file_url}"
        )

        path = await download_to_temp(req.file_url, suffix=suffix)

        try:
            if kind == "video":
                m = await client.send_video(
                    chat_id=req.chat_id,
                    video=path,
                    caption=req.caption,
                    parse_mode=pm,
                    supports_streaming=True,
                )
            else:
                m = await client.send_photo(
                    chat_id=req.chat_id,
                    photo=path,
                    caption=req.caption,
                    parse_mode=pm,
                )
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

        return {"ok": True, "message_id": m.id}
    except HTTPException:
        raise
    except Exception as e:
        # 让 Node 能看到具体错误
        raise HTTPException(status_code=500, detail=f"uploader_error: {e}")
