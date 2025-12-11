# app.py
import os
import tempfile
import asyncio
from typing import Optional

from fastapi import FastAPI, HTTPException
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
HEAD_TIMEOUT = 10.0  # 秒，HEAD 请求超时（用于快速验证）
DOWNLOAD_TIMEOUT = None  # None -> no total timeout (stream controlled)
HTTPX_LIMITS = httpx.Limits(max_keepalive_connections=5, max_connections=10)
DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

class UploadRequest(BaseModel):
    chat_id: str
    file_url: str
    caption: Optional[str] = None
    parse_mode: Optional[str] = None  # "HTML" / "Markdown" / None
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
    """
    对远端 URL 做 HEAD 请求检查 content-type / content-length 的快速验证。
    返回 headers dict；如果 HEAD 不允许（405/501 等），会尝试用 GET (小量读取) 兜底。
    """
    headers = {"User-Agent": DEFAULT_UA, "Accept": "*/*"}
    async with httpx.AsyncClient(timeout=HEAD_TIMEOUT, follow_redirects=True, limits=HTTPX_LIMITS) as h:
        try:
            r = await h.head(url)
            # some servers disallow HEAD -> fallback to a small GET
            if r.status_code >= 400:
                r2 = await h.get(url, headers=headers, timeout=HEAD_TIMEOUT)
                return dict(r2.headers)
            return dict(r.headers)
        except Exception as e:
            # 抛给上层做重试逻辑
            raise RuntimeError(f"HEAD/quick-check failed: {e}")


async def download_to_temp_with_retries(url: str, suffix: str) -> str:
    """
    带重试的流式下载到临时文件，返回本地路径。
    - 做 HEAD 检查确认 content-type 看起来像视频/图片（若不是，会报错）
    - 使用指数退避重试（HTTP 错误/连接重置/短暂超时）
    """
    # 先 HEAD/quick-check
    try:
        headers = await head_check(url)
    except Exception as e:
        # 仍可继续尝试下载（可能 HEAD 被 405，但 GET 可用），所以不立即终止
        headers = {}
    ct = headers.get("content-type", "")
    # 如果 content-type 明显不是 http video/image，warn 但不强制拒绝，因为有些 CDN 可能不返回标准类型
    if ct and not any(x in ct for x in ("video", "image", "application/octet-stream", "binary")):
        # 记录警告（不过仍尝试下载）
        # raise RuntimeError(f"Unexpected content-type: {ct}")
        pass

    last_exc = None
    for attempt in range(1, DOWNLOAD_MAX_RETRIES + 1):
        fd, path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)  # 我们会用 open 写入
        try:
            async with httpx.AsyncClient(headers={"User-Agent": DEFAULT_UA}, timeout=DOWNLOAD_TIMEOUT, follow_redirects=True, limits=HTTPX_LIMITS) as h:
                async with h.stream("GET", url) as r:
                    r.raise_for_status()
                    # 很重要：检查服务器返回 content-type 再决定是否继续
                    resp_ct = r.headers.get("content-type", "")
                    if resp_ct and not any(x in resp_ct for x in ("video", "image", "application/octet-stream", "binary")):
                        # 如果返回 HTML（比如 404 页面），直接报错
                        # 读取一小段响应体用于 debug
                        snippet = await r.aread()
                        snippet_head = (snippet[:512]).decode(errors="ignore")
                        raise RuntimeError(f"Bad Request: wrong type of the web page content (content-type={resp_ct}) snippet={snippet_head}")

                    with open(path, "wb") as f:
                        async for chunk in r.aiter_bytes(chunk_size=DOWNLOAD_CHUNK):
                            if not chunk:
                                continue
                            f.write(chunk)
            # 成功
            return path
        except Exception as e:
            last_exc = e
            # 清理可能残留的临时文件
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
            backoff = DOWNLOAD_BACKOFF_BASE * (2 ** (attempt - 1))
            # 若最后一次仍失败，抛出
            if attempt == DOWNLOAD_MAX_RETRIES:
                raise RuntimeError(f"download_failed_after_retries: {last_exc}")
            # 对某些明显不可重试的异常直接 raise
            # 否则 sleep 并重试
            await asyncio.sleep(backoff)

    # 不该到这里
    raise RuntimeError("unreachable_download_error")


@app.post("/upload")
async def upload(req: UploadRequest):
    """
    Node/Tampermonkey 调用：
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

        pm = to_parse_mode_enum(req.parse_mode)
        print(
            f"[UPLOAD] kind={kind} chat={req.chat_id} "
            f"pm={pm} raw_pm={req.parse_mode} url={req.file_url}"
        )

        # 下载（带重试）
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
        # 把清晰的错误信息返回给前端，便于调试
        raise HTTPException(status_code=500, detail=f"uploader_error: {e}")
