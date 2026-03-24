from datetime import datetime, timezone
import base64
import json
import os
from pathlib import Path
import time
from urllib import error as urllib_error
from urllib import request as urllib_request
import uuid

from flask import Flask, Response, jsonify, redirect, request, stream_with_context


app = Flask(__name__)

API_KEY = os.getenv("OPENAI_API_KEY")
LEGACY_API_KEY = os.getenv("API_KEY", "sixseven")
VALID_API_KEYS = {k for k in [API_KEY, LEGACY_API_KEY] if k}
PIXELDRAIN_API_KEY = os.getenv("PIXELDRAIN_API_KEY", "01fc925f-6884-4b46-b16d-0c53b6b0c12c")
DUMPS_DIR = Path("dumps")
UPLOAD_LOG_FILE = DUMPS_DIR / "upload_links.txt"
AVAILABLE_MODELS = [
	{
		"id": "sixseven",
		"object": "model",
		"created": 1700000003,
		"owned_by": "custom-owner",
	},
	
]


def build_dump_text(path: str, body_json: dict, raw_body: str, completion_id: str) -> str:
	lines = [
		f"timestamp_utc: {datetime.now(timezone.utc).isoformat()}",
		f"completion_id: {completion_id}",
		f"path: {path}",
		"",
		"=== SYSTEM MESSAGES ===",
	]

	messages = body_json.get("messages", []) if isinstance(body_json, dict) else []
	system_messages = [m for m in messages if isinstance(m, dict) and m.get("role") == "system"]
	if system_messages:
		for i, msg in enumerate(system_messages, start=1):
			lines.append(f"[{i}] {msg.get('content', '')}")
	else:
		lines.append("(none)")

	lines.extend([
		"",
		"=== FULL JSON BODY ===",
		json.dumps(body_json, ensure_ascii=False, indent=2) if isinstance(body_json, dict) else "{}",
		"",
		"=== RAW BODY ===",
		raw_body or "",
	])
	return "\n".join(lines)


def upload_file_to_pixeldrain(file_path: Path, max_retries: int = 3):
	if not PIXELDRAIN_API_KEY:
		return None, "PIXELDRAIN_API_KEY is not configured"

	boundary = f"----dump-{uuid.uuid4().hex}"
	file_name = file_path.name
	file_bytes = file_path.read_bytes()

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
	DUMPS_DIR.mkdir(parents=True, exist_ok=True)

	file_name = f"dump_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{completion_id}.txt"
	file_path = DUMPS_DIR / file_name
	file_path.write_text(build_dump_text(path, body_json, raw_body, completion_id), encoding="utf-8")

	upload_url, upload_error = upload_file_to_pixeldrain(file_path)
	if upload_error:
		log_line = f"{datetime.now(timezone.utc).isoformat()} | {file_name} | upload_failed | {upload_error}"
		print(f"[dump] Upload failed for {file_name}: {upload_error}")
	else:
		log_line = f"{datetime.now(timezone.utc).isoformat()} | {file_name} | {upload_url}"
		print(f"[dump] Uploaded {file_name} -> {upload_url}")

	with UPLOAD_LOG_FILE.open("a", encoding="utf-8") as f:
		f.write(log_line + "\n")

	return upload_url, upload_error


def openai_error(message: str, status: int = 400, err_type: str = "invalid_request_error"):
	return jsonify(
		{
			"error": {
				"message": message,
				"type": err_type,
				"param": None,
				"code": None,
			}
		}
	), status


def require_api_key():
	auth_header = request.headers.get("Authorization", "")
	if not auth_header.startswith("Bearer "):
		return openai_error(
			"You must provide an API key in the Authorization header using Bearer format.",
			status=401,
			err_type="invalid_api_key",
		)

	provided_key = auth_header.split(" ", 1)[1].strip()
	if not provided_key:
		return openai_error(
			"Invalid API key provided.",
			status=401,
			err_type="invalid_api_key",
		)

	# Accept configured keys (OPENAI_API_KEY/API_KEY) or OpenAI-style sk-* keys.
	if provided_key not in VALID_API_KEYS and not provided_key.startswith("sk-"):
		return openai_error(
			"Incorrect API key provided.",
			status=401,
			err_type="invalid_api_key",
		)

	return None


@app.before_request
def authenticate_openai_style():
	if request.method == "OPTIONS":
		return ("", 204)

	protected_prefixes = ("/v1/", "/chat/", "/hampter/")
	if request.path.startswith(protected_prefixes) or request.path in ("/models", "/model"):
		auth_error = require_api_key()
		if auth_error:
			return auth_error


@app.after_request
def add_cors_headers(response):
	response.headers["Access-Control-Allow-Origin"] = "*"
	response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
	response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
	return response


@app.route("/", methods=["GET"])
def index():
	return  redirect("https://www.youtube.com/watch?v=2qBlE2-WL60"), 302


@app.errorhandler(404)
def handle_not_found(_err):
    return redirect("https://www.youtube.com/watch?v=2qBlE2-WL60", code=302)


@app.route("/v1/models", methods=["GET"])
@app.route("/models", methods=["GET"])
@app.route("/model", methods=["GET"])
def list_models():
	return jsonify({"object": "list", "data": AVAILABLE_MODELS}), 200


@app.route("/v1/models/<model_id>", methods=["GET"])
def get_model(model_id):
	for model in AVAILABLE_MODELS:
		if model["id"] == model_id:
			return jsonify(model), 200

	return openai_error(f"The model '{model_id}' does not exist.", status=404, err_type="invalid_request_error")


@app.route("/v1/chat/completions", methods=["POST"])
@app.route("/chat/completions", methods=["POST"])
@app.route("/v1/chatcompletion", methods=["POST"])
def chat_completion():
	body_json = request.get_json(silent=True)
	raw_body = request.get_data(cache=False, as_text=True)
	completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
	if not isinstance(body_json, dict):
		return openai_error(
			"Invalid JSON body. Expected a JSON object.",
			status=400,
			err_type="invalid_request_error",
		)

	if "model" not in body_json:
		return openai_error(
			"Missing required parameter: 'model'.",
			status=400,
			err_type="invalid_request_error",
		)

	if "messages" not in body_json or not isinstance(body_json.get("messages"), list):
		return openai_error(
			"Missing or invalid required parameter: 'messages'.",
			status=400,
			err_type="invalid_request_error",
		)

	print("=" * 80)
	print(f"Incoming request to {request.path} (system messages only)")
	messages = body_json.get("messages", [])
	system_count = 0
	for msg in messages:
		if isinstance(msg, dict) and msg.get("role") == "system":
			system_count += 1
			system_only = {
				"role": "system",
				"content": msg.get("content", ""),
			}
			print(json.dumps(system_only, ensure_ascii=False, separators=(",", ":")))

	if system_count == 0:
		print('{"role":"system","content":""}')
	print("=" * 80)

	upload_url, upload_error = write_dump_and_upload(request.path, body_json, raw_body, completion_id)

	model_name = body_json.get("model", "dump-model")
	content_text = "Dump completed บอทโคตรกาก."
	if upload_url:
		content_text += f"\nLink: {upload_url}"
	else:
		content_text += f"\nLink: upload failed ({upload_error})"
	created_ts = int(time.time())
	stream = bool(body_json.get("stream", False))

	# Return OpenAI-compatible response so SillyTavern accepts it.
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
		def event_stream():
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

		return Response(stream_with_context(event_stream()), mimetype="text/event-stream")

	return jsonify(response_payload), 200


@app.route("/hampter/chats/<chat_id>/messages", methods=["POST"])
def save_messages(chat_id):
	body_json = request.get_json(silent=True)
	print(f"[save] chatId={chat_id} body={body_json}")
	response_data = {
		**(body_json if isinstance(body_json, dict) else {}),
		"message_id": f"msg_{uuid.uuid4().hex[:12]}",
		"timestamp": datetime.now(timezone.utc).isoformat(),
		"success": True,
	}
	return jsonify([response_data]), 200


@app.route("/health", methods=["GET"])
def health_check():
	return jsonify({"status": "ok", "service": "OpenAI-format dump server"}), 200





if __name__ == "__main__":
	app.run(host="0.0.0.0", port=5000, debug=True)
