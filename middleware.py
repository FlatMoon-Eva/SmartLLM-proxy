import asyncio
import time
from litellm.proxy.proxy_server import app
from fastapi import Request

last_request_time = 0
INTERVAL = 12.0  # 秒 (60/5=12)

@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    global last_request_time
    if request.url.path == "/chat/completions":
        now = time.time()
        wait = INTERVAL - (now - last_request_time)
        if wait > 0:
            await asyncio.sleep(wait)
        last_request_time = time.time()
    return await call_next(request)
