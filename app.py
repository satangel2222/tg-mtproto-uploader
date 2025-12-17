# app.py
import os
import tempfile
import asyncio
from typing import Optional

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel
import httpx
from pyrogram import Client
from pyrogram.enums import ParseMode

# ------------- 环境变量（必须在 Render / 环境里设置） -------------
# TG_API_ID, TG_API_HASH, TG_STRING_SESSION
try:
    TG_API_ID = int(os.environ["TG_API_ID"])
    TG_API_HASH = os.environ["TG_API_HASH"]
    TG_STRING_SESSION = os.environ["TG_STRING_SESSION"]
except KeyError as e:
    raise RuntimeError(f"Missing env var: {e.args[0]}")

# ------------- Pyrogram Client -------------
app = FastAPI(title="TG MTProto Uploader (robust)")

client = Client(
    "mtuploader",
    api_id=TG_API_ID,
    api_hash=TG_API_HASH,
    session_string=TG_STRING_SESSION,
    in_memory=True,
)

# ---- 参数 ----
DOWNLOAD_MAX_RETRIES = 5
DOWNLOAD_BACKOFF_BASE = 1.0  # 秒，指数退避基数
DOWNLOAD_CHUNK = 1024 * 1024  # 1 MB
HEAD_TIMEOUT = 10.0  # 秒
DOWNLOAD_TIMEOUT = None
HTTPX_LIMITS = httpx.Limits(max_keepalive_connections=5, max_connections=10)
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
)

class UploadRequest(BaseModel):
    chat_id: str
    file_url: str
    caption: Optional[str] = None
    parse_mode: Optional[str] = None
    kind: str = "video"  # "video" or "photo"


@app.on_event("startup")
async def on_startup():
    await client.start()


@app.on_event("shutdown")
async def on_shutdown():
    await client.stop()


# ---------------- 健康检查 ----------------

@app.get("/")
async def health():
    return {"ok": True, "message": "mtproto uploader is up"}


# ✅【关键补丁】：
# 让 UptimeRobot（HEAD）= 200
# 否则 Render Free 不会被唤醒
@app.head("/")
async def health_head():
    return Response(status_code=200)


# ---------------- 工具函数 ----------------

def to_parse_mode_enum(mode_str: Optional[str]):
    if not mode_str:
        return None
    s = str(mode_str).strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()
    s_up = s.upper()
    if s_up == "HTML":
        return ParseMode.HTML
    if s_up.startswith("MARKDOWN"):
        return ParseMode.MARKDOWN
    return None


async def head_check(url: str) -> dict:
    headers = {"User-Agent": DEFAULT_UA, "Accept": "*/*"}
    async with httpx.AsyncClient(
        timeout=HEAD_TIMEOUT,
        follow_redirects=True,
        limits=HTTPX_LIMITS,
    ) as h:
        try:
            r = await h.head(url)
            if r.status_code >= 400:
                r2 = await h.get(url, headers=headers, timeout=HEAD_TIMEOUT)
                return dict(r2.headers)
            return dict(r.headers)
        except Exception as e:
            raise RuntimeError(f"HEAD/quick-check failed: {e}")


async def download_to_temp_with_retries(url: str, suffix: str) -> str:
    try:
        headers = await head_check(url)
    except Exception:
        headers = {}

    ct = headers.get("content-type", "")
    if ct and not any(x in ct for x in ("video", "image", "application/octet-stream", "binary")):
        pass  # 仅 warning，不直接拒绝

    last_exc = None
    for attempt in range(1, DOWNLOAD_MAX_RETRIES + 1):
        fd, path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        try:
            async with httpx.AsyncClient(
                headers={"User-Agent": DEFAULT_UA},
                timeout=DOWNLOAD_TIMEOUT,
                follow_redirects=True,
                limits=HTTPX_LIMITS,
            ) as h:
                async with h.stream("GET", url) as r:
                    r.raise_for_status()
                    resp_ct = r.headers.get("content-type", "")
                    if resp_ct and not any(
                        x in resp_ct
                        for x in ("video", "image", "application/octet-stream", "binary")
                    ):
                        snippet = await r.aread()
                        snippet_head = snippet[:512].decode(errors="ignore")
                        raise RuntimeError(
                            f"Bad Request: wrong type of the web page content "
                            f"(content-type={resp_ct}) snippet={snippet_head}"
                        )

                    with open(path, "wb") as f:
                        async for chunk in r.aiter_bytes(chunk_size=DOWNLOAD_CHUNK):
                            if chunk:
                                f.write(chunk)

            return path

        except Exception as e:
            last_exc = e
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass

            if attempt == DOWNLOAD_MAX_RETRIES:
                raise RuntimeError(f"download_failed_after_retries: {last_exc}")

            backoff = DOWNLOAD_BACKOFF_BASE * (2 ** (attempt - 1))
            await asyncio.sleep(backoff)

    raise RuntimeError("unreachable_download_error")


# ---------------- 主接口 ----------------

@app.post("/upload")
async def upload(req: UploadRequest):
    try:
        kind = (req.kind or "video").lower()
        suffix = ".mp4" if kind == "video" else ".jpg"

        pm = to_parse_mode_enum(req.parse_mode)
        print(
            f"[UPLOAD] kind={kind} chat={req.chat_id} "
            f"pm={pm} raw_pm={req.parse_mode} url={req.file_url}"
        )

        path = await download_to_temp_with_retries(req.file_url, suffix=suffix)

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
        raise HTTPException(status_code=500, detail=f"uploader_error: {e}")
