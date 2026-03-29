from datetime import datetime, timezone
import base64
import json
import os
from pathlib import Path
import time
from urllib import error as urllib_error
from urllib import request as urllib_request
import uuid
import asyncio
import random

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

API_KEY = os.getenv("OPENAI_API_KEY")
LEGACY_API_KEY = os.getenv("API_KEY", "yiersansi")
VALID_API_KEYS = {k for k in [API_KEY, LEGACY_API_KEY] if k}
PIXELDRAIN_API_KEY = os.getenv("PIXELDRAIN_API_KEY", "b23ee241-07e5-4293-9cdd-6c6eb61f14dc")

# Use /tmp on serverless; local runs can override with DUMPS_DIR env.
DUMPS_DIR = Path(os.getenv("DUMPS_DIR", "/tmp/dumps"))
UPLOAD_LOG_FILE = DUMPS_DIR / "upload_links.txt"

AVAILABLE_MODELS = [
    {
        "id": "日本正在把脚步转化为电能(yi)",
        "object": "model",
        "created": 1700000004,
        "owned_by": "custom-owner",
    },
    {
        "id": "你爱我的我爱你(er)",
        "object": "model",
        "created": 1700000003,
        "owned_by": "custom-owner",
    },
]

FUNNY_PHRASES = [
    "阿巴阿巴阿巴...", "玛卡巴卡！", "鸡你太美~", "泰裤辣！", "退！退！退！",
    "大威天龙！", "哎哟你干嘛~", "我只会心疼giegie~", "你是一个一个一个...",
    "奥利给！", "发生甚么事了？", "年轻人不讲武德！", "耗子尾汁！",
    "汪汪汪！", "喵喵喵？", "CPU已烧毁...", "正在强行运算..."
]

ARTICLE_TEXT = """日本利用一种称为“压电发电”的技术，将人们的脚步转化为电能。在这种系统中，地面会安装特殊的地砖，这些地砖内部含有压电材料，例如某些晶体或陶瓷。这类材料在受到机械压力时，能够产生电荷。

当人们走在这些地砖上时，身体的重量会对地砖产生压力，使内部材料发生微小的形变。虽然这种变化非常细微，肉眼几乎无法察觉，但已经足以产生少量的电能。每一步产生的电量都很小，但在像火车站这样人流密集的地方，每分钟会有成千上万的脚步，这些微小的电能就会不断累积。

这些由脚步产生的电能会被收集并储存起来，然后用于为一些低功耗设备供电，例如LED灯、电子显示屏或传感器等。这项技术已经在日本的一些繁忙场所（特别是铁路车站）中得到应用，用于展示可再生能源的利用方式并提高能源利用效率。

虽然这种方式产生的电量不足以为整栋建筑或城市供电，但它是一种创新的能源利用方法，能够将日常的人类活动转化为能源，同时也有助于提高人们对可持续发展的认识。"""


def openai_error(message: str, status: int = 400, err_type: str = "invalid_request_error"):
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "message": message,
                "type": err_type,
                "param": None,
                "code": None,
            }
        },
    )


def require_api_key(auth_header: str):
    if not auth_header.startswith("Bearer "):
        return openai_error(
            "你必须在 Authorization 请求头中使用 Bearer 格式提供 API 密钥。",
            status=401,
            err_type="invalid_api_key",
        )

    provided_key = auth_header.split(" ", 1)[1].strip()
    if not provided_key:
        return openai_error(
            "提供的 API 密钥无效。",
            status=401,
            err_type="invalid_api_key",
        )

    # Accept configured keys (OPENAI_API_KEY/API_KEY) or OpenAI-style sk-* keys.
    if provided_key not in VALID_API_KEYS and not provided_key.startswith("sk-"):
        return openai_error(
            "提供的 API 密钥不正确。",
            status=401,
            err_type="invalid_api_key",
        )

    return None


@app.middleware("http")
async def authenticate_openai_style(request: Request, call_next):
    if request.method == "OPTIONS":
        return JSONResponse(status_code=204, content={})

    path = request.url.path
    protected_prefixes = ("/v1/", "/chat/", "/hampter/")
    if path.startswith(protected_prefixes) or path in ("/models", "/model"):
        auth_error = require_api_key(request.headers.get("authorization", ""))
        if auth_error:
            return auth_error

    return await call_next(request)


def build_dump_text(path: str, body_json: dict, raw_body: str, completion_id: str) -> str:
    lines = [
        f"timestamp_utc: {datetime.now(timezone.utc).isoformat()}",
        f"completion_id: {completion_id}",
        f"path: {path}",
        "",
        "=== ALL MESSAGES ===",
    ]

    messages = body_json.get("messages", []) if isinstance(body_json, dict) else []
    
    # Save everything, no need to filter by role
    if messages:
        for i, msg in enumerate(messages, start=1):
            if isinstance(msg, dict):
                role = msg.get('role', 'unknown')
                content = msg.get('content', '')
                lines.append(f"[{i}] {role.upper()}: {content}")
    else:
        lines.append("(none)")

    lines.extend(
        [
            "",
            "=== FULL JSON BODY ===",
            json.dumps(body_json, ensure_ascii=False, indent=2) if isinstance(body_json, dict) else "{}",
            "",
            "=== RAW BODY ===",
            raw_body or "",
        ]
    )
    return "\n".join(lines)


def upload_bytes_to_pixeldrain(file_name: str, file_bytes: bytes, max_retries: int = 3):
    if not PIXELDRAIN_API_KEY:
        return None, "PIXELDRAIN_API_KEY is not configured"

    boundary = f"----dump-{uuid.uuid4().hex}"
    body = (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"file\"; filename=\"{file_name}\"\r\n"
        "Content-Type: text/plain\r\n\r\n"
    ).encode("utf-8") + file_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

    auth_token = base64.b64encode(f":{PIXELDRAIN_API_KEY}".encode("utf-8")).decode("ascii")
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
        "Authorization": f"Basic {auth_token}",
    }

    request_obj = urllib_request.Request(
        url="https://pixeldrain.com/api/file",
        data=body,
        headers=headers,
        method="POST",
    )

    response_data = None
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            with urllib_request.urlopen(request_obj, timeout=60) as response:
                response_data = json.loads(response.read().decode("utf-8"))
            break
        except urllib_error.HTTPError as exc:
            try:
                err_payload = exc.read().decode("utf-8")
            except Exception:
                err_payload = str(exc)
            last_error = f"HTTP {exc.code}: {err_payload}"
        except Exception as exc:
            last_error = str(exc)

        if attempt < max_retries:
            time.sleep(attempt)

    if response_data is None:
        return None, last_error or "upload failed"

    file_id = response_data.get("id")
    if not file_id:
        return None, f"Unexpected response: {response_data}"

    return f"https://pixeldrain.com/u/{file_id}", None


def write_dump_and_upload(path: str, body_json: dict, raw_body: str, completion_id: str):
    file_name = f"dump_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{completion_id}.txt"
    dump_text = build_dump_text(path, body_json, raw_body, completion_id)

    # Best-effort local write for debugging. Does not fail request on serverless.
    try:
        DUMPS_DIR.mkdir(parents=True, exist_ok=True)
        file_path = DUMPS_DIR / file_name
        file_path.write_text(dump_text, encoding="utf-8")
    except Exception as exc:
        print(f"[转储] 本地写入已跳过: {exc}")

    upload_url, upload_error = upload_bytes_to_pixeldrain(file_name, dump_text.encode("utf-8"))
    if upload_error:
        log_line = f"{datetime.now(timezone.utc).isoformat()} | {file_name} | upload_failed | {upload_error}"
        print(f"[转储] 上传失败 {file_name}: {upload_error}")
    else:
        log_line = f"{datetime.now(timezone.utc).isoformat()} | {file_name} | {upload_url}"
        print(f"[转储] 上传成功 {file_name} -> {upload_url}")

    # Best-effort log file.
    try:
        DUMPS_DIR.mkdir(parents=True, exist_ok=True)
        with UPLOAD_LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(log_line + "\n")
    except Exception as exc:
        print(f"[转储] 上传日志写入已跳过: {exc}")

    return upload_url, upload_error


@app.get("/")
def index():
    return RedirectResponse("https://www.youtube.com/watch?v=2qBlE2-WL60", status_code=302)


@app.exception_handler(404)
async def handle_not_found(_request: Request, _err):
    return RedirectResponse("https://www.youtube.com/watch?v=2qBlE2-WL60", status_code=302)


@app.get("/v1/models")
@app.get("/models")
@app.get("/model")
def list_models():
    return {"object": "list", "data": AVAILABLE_MODELS}


@app.get("/v1/models/{model_id}")
def get_model(model_id: str):
    for model in AVAILABLE_MODELS:
        if model["id"] == model_id:
            return model

    return openai_error(f"模型 '{model_id}' 不存在。", status=404, err_type="invalid_request_error")


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
@app.post("/v1/chatcompletion")
async def chat_completion(request: Request):
    try:
        body_json = await request.json()
    except Exception:
        body_json = None

    raw_body_bytes = await request.body()
    raw_body = raw_body_bytes.decode("utf-8", errors="replace")
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

    if not isinstance(body_json, dict):
        return openai_error(
            "JSON 请求体无效，应为 JSON 对象。",
            status=400,
            err_type="invalid_request_error",
        )

    if "model" not in body_json:
        return openai_error(
            "缺少必填参数: 'model'。",
            status=400,
            err_type="invalid_request_error",
        )

    if "messages" not in body_json or not isinstance(body_json.get("messages"), list):
        return openai_error(
            "缺少或无效的必填参数: 'messages'。",
            status=400,
            err_type="invalid_request_error",
        )

    print("=" * 80)
    print(f"收到请求 {request.url.path}（显示所有消息）")
    messages = body_json.get("messages", [])
    
    # Print all messages to the console instead of filtering
    if messages:
        for msg in messages:
            if isinstance(msg, dict):
                print(json.dumps(msg, ensure_ascii=False, separators=(",", ":")))
    else:
        print("[]")
        
    print("=" * 80)

    upload_url, upload_error = write_dump_and_upload(request.url.path, body_json, raw_body, completion_id)

    model_name = body_json.get("model", "dump-model")
    created_ts = int(time.time())
    stream = bool(body_json.get("stream", False))

    is_funny_model = model_name == "日本正在把脚步转化为电能(yi)"

    if is_funny_model:
        funny_thoughts = "".join(random.choice(FUNNY_PHRASES) + "\n" for _ in range(25))
        content_text = f"<think>\n{funny_thoughts}</think>\n\n{ARTICLE_TEXT}"
    else:
        content_text = "转储完成。"
        if upload_url:
            content_text += f"\n链接: {upload_url}"
        else:
            content_text += f"\n链接: 上传失败（{upload_error}）"

    response_payload = {
        "id": completion_id,
        "object": "chat.completion",
        "created": created_ts,
        "model": model_name,
        "system_fingerprint": "fp_local_dump_server",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content_text,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }

    if stream:

        async def event_stream():
            if is_funny_model:
                chunk = {
                    "id": completion_id, "object": "chat.completion.chunk", "created": created_ts, "model": model_name,
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": "<think>\n"}, "finish_reason": None}]
                }
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

                # 模拟 5 秒钟的废话流式生成 (25 * 0.2秒 = 5秒)
                for _ in range(25):
                    await asyncio.sleep(0.2)
                    word = random.choice(FUNNY_PHRASES) + "\n"
                    chunk["choices"][0]["delta"] = {"content": word}
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

                chunk["choices"][0]["delta"] = {"content": "</think>\n\n"}
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

                # 以每次 5 个字符的速度打字输出正文
                chunk_size = 5
                for i in range(0, len(ARTICLE_TEXT), chunk_size):
                    await asyncio.sleep(0.05)
                    chunk["choices"][0]["delta"] = {"content": ARTICLE_TEXT[i:i+chunk_size]}
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

                chunk["choices"][0]["delta"] = {}
                chunk["choices"][0]["finish_reason"] = "stop"
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"

            else:
                chunk1 = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created_ts,
                    "model": model_name,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": content_text},
                            "finish_reason": None,
                        }
                    ],
                }
                yield f"data: {json.dumps(chunk1, ensure_ascii=False)}\n\n"

                chunk2 = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created_ts,
                    "model": model_name,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop",
                        }
                    ],
                }
                yield f"data: {json.dumps(chunk2, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    return JSONResponse(status_code=200, content=response_payload)


@app.post("/hampter/chats/{chat_id}/messages")
async def save_messages(chat_id: str, request: Request):
    try:
        body_json = await request.json()
    except Exception:
        body_json = None

    print(f"[保存] 聊天ID={chat_id} 请求体={body_json}")
    response_data = {
        **(body_json if isinstance(body_json, dict) else {}),
        "message_id": f"msg_{uuid.uuid4().hex[:12]}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "success": True,
    }
    return JSONResponse(status_code=200, content=[response_data])


@app.get("/health")
def health_check():
    return {"status": "ok", "service": "OpenAI 格式转储服务"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=5000, reload=True)
