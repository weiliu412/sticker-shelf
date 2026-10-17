#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""表情包小抽屉 · 单文件小服务（只用 Python 标准库）。

    python3 server.py            # 然后浏览器打开 http://127.0.0.1:8767

它做三件事：
1. 端出 web/ 里的管理页：上传、改名、开关「小机能不能用」、删除；
2. 端出图片：GET /stickers/img/<编号>，你的聊天前端把 [sticker:编号] 换成这张图；
3. 给你的 AI 三条接口（见 README）：
   GET  /stickers/context     接在系统提示末尾：规矩 + 小机能选的表情包清单
   POST /stickers/expand      聊天记录交给小机之前：你发的表情包 → 【表情包：名字】
   POST /stickers/normalize   小机的回复存下来之前：写成文字的表情包 → 真表情包
"""
import hashlib
import hmac
import io
import json
import os
import sys
import threading
import time
import zipfile
from http.cookies import SimpleCookie
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import labeler  # noqa: E402
import stickers  # noqa: E402

HOST = os.environ.get("HOST") or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
PORT = int(os.environ.get("PORT") or 8767)
PORT_TRIES = 12
MAX_JSON = 64 * 1024
ADMIN_PASSWORD = os.environ.get("STICKER_ADMIN_PASSWORD", "")
API_TOKEN = os.environ.get("STICKER_API_TOKEN", "")
SESSION_SECONDS = 7 * 24 * 60 * 60
LOGIN_WINDOW = 5 * 60
LOGIN_LIMIT = 8
LOGIN_ATTEMPTS: dict[str, list[float]] = {}
LOGIN_LOCK = threading.Lock()
SESSION_KEY_LOCK = threading.Lock()
SESSION_KEY_CACHE: tuple[str, bytes] = ("", b"")


def _session_key() -> bytes:
    """从管理员密码派生稳定的签名密钥，不把密码写进 Cookie。"""
    global SESSION_KEY_CACHE
    with SESSION_KEY_LOCK:
        if SESSION_KEY_CACHE[0] != ADMIN_PASSWORD:
            SESSION_KEY_CACHE = (
                ADMIN_PASSWORD,
                hashlib.pbkdf2_hmac(
                    "sha256", ADMIN_PASSWORD.encode("utf-8"), b"sticker-shelf-session-v1", 200_000
                ),
            )
        return SESSION_KEY_CACHE[1]


def make_session(now: int | None = None) -> str:
    expires = int(now if now is not None else time.time()) + SESSION_SECONDS
    payload = str(expires)
    signature = hmac.new(_session_key(), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def valid_session(token: str, now: int | None = None) -> bool:
    if not ADMIN_PASSWORD or "." not in token:
        return False
    payload, signature = token.rsplit(".", 1)
    try:
        expires = int(payload)
    except ValueError:
        return False
    expected = hmac.new(_session_key(), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return expires >= int(now if now is not None else time.time()) and hmac.compare_digest(signature, expected)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT / "web"), **kw)

    def log_message(self, fmt, *args):  # 只打接口，静态文件不刷屏
        request = str(args[0]) if args else ""
        if "/stickers/" in request and "/stickers/img/" not in request:
            super().log_message(fmt, *args)

    # ── 小工具 ──
    def _send(self, body: bytes, content_type: str, code: int = 200, cors: bool = False, cache: str = "no-store"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if cors:  # 读接口允许你自己别的端口上的前端来调
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200, cors: bool = False):
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", code, cors)

    def _error(self, message: str, code: int = 400):
        self._json({"error": message}, code)

    def _redirect(self, location: str):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _cookie(self, name: str) -> str:
        cookies = SimpleCookie(self.headers.get("Cookie") or "")
        return cookies[name].value if name in cookies else ""

    def _has_session(self) -> bool:
        return not ADMIN_PASSWORD or valid_session(self._cookie("sticker_session"))

    def _bearer_ok(self) -> bool:
        if not API_TOKEN:
            return False
        scheme, _, token = (self.headers.get("Authorization") or "").partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(token, API_TOKEN)

    def _read_authorized(self) -> bool:
        return self._has_session() or self._bearer_ok()

    def _require_json_auth(self, *, allow_token: bool = False) -> bool:
        if self._has_session() or (allow_token and self._bearer_ok()):
            return True
        self._error("请先登录。", 401)
        return False

    def _login_limited(self) -> bool:
        address = self.client_address[0]
        cutoff = time.monotonic() - LOGIN_WINDOW
        with LOGIN_LOCK:
            attempts = [stamp for stamp in LOGIN_ATTEMPTS.get(address, []) if stamp >= cutoff]
            LOGIN_ATTEMPTS[address] = attempts
            return len(attempts) >= LOGIN_LIMIT

    def _login_failed(self) -> None:
        with LOGIN_LOCK:
            LOGIN_ATTEMPTS.setdefault(self.client_address[0], []).append(time.monotonic())

    def _login_succeeded(self) -> None:
        with LOGIN_LOCK:
            LOGIN_ATTEMPTS.pop(self.client_address[0], None)

    def _set_session(self):
        body = json.dumps({"ok": True}).encode("utf-8")
        secure = (self.headers.get("X-Forwarded-Proto") or "").lower() == "https"
        cookie = f"sticker_session={make_session()}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS}"
        if secure:
            cookie += "; Secure"
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _clear_session(self):
        body = b'{"ok":true}'
        secure = (self.headers.get("X-Forwarded-Proto") or "").lower() == "https"
        cookie = "sticker_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
        if secure:
            cookie += "; Secure"
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _export_backup(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            if stickers.INDEX_PATH.is_file():
                archive.write(stickers.INDEX_PATH, "stickers.json")
            if stickers.IMAGE_DIR.is_dir():
                for path in stickers.IMAGE_DIR.iterdir():
                    if path.is_file() and not path.name.startswith("."):
                        archive.write(path, f"images/{path.name}")
        body = output.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", 'attachment; filename="sticker-shelf-backup.zip"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _same_origin(self) -> bool:
        """改数据的接口只给本页面用：别的网站不能偷偷替你上传或删除。"""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        return urlsplit(origin).netloc == host

    def _body(self, limit: int) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise stickers.StickerError("内容太大了。", 413)
        return self.rfile.read(length) if length else b""

    def _json_body(self) -> dict:
        try:
            data = json.loads(self._body(MAX_JSON) or b"{}")
        except ValueError as error:
            raise stickers.StickerError("请求不是有效的 JSON。") from error
        if not isinstance(data, dict):
            raise stickers.StickerError("请求不是有效的 JSON。")
        return data

    # ── 路由 ──
    def do_OPTIONS(self):
        self._send(b"", "text/plain", 204, cors=True)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/healthz":
            return self._json({"ok": True})
        if path == "/login":
            if self._has_session():
                return self._redirect("/")
            return self._send((ROOT / "web" / "login.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/stickers/api/export":
            if not self._require_json_auth():
                return None
            return self._export_backup()
        if path == "/stickers/api/list":
            if not self._require_json_auth():
                return None
            return self._json({
                "items": stickers.list_stickers(),
                "auto_label": labeler.available(),
                "auth_enabled": bool(ADMIN_PASSWORD),
            })
        if path == "/stickers/catalog":
            if not self._read_authorized():
                return self._error("请提供登录会话或 API Token。", 401)
            return self._json({
                "items": [item for item in stickers.list_stickers() if item["ai_enabled"]],
            })
        if path == "/stickers/context":
            if not self._read_authorized():
                return self._error("请提供登录状态或 API Token。", 401)
            return self._send(stickers.prompt_context().encode("utf-8"), "text/plain; charset=utf-8", cors=True)
        if path.startswith("/stickers/img/"):
            if not self._read_authorized():
                return self._error("请提供登录状态或 API Token。", 401)
            found = stickers.image_path(path.rsplit("/", 1)[-1])
            if found is None:
                return self._error("表情包不存在。", 404)
            file_path, media_type = found
            return self._send(file_path.read_bytes(), media_type, cors=True, cache="private, max-age=604800")
        if ADMIN_PASSWORD and not self._has_session():
            return self._redirect("/login")
        return super().do_GET()

    def do_POST(self):
        path = urlsplit(self.path).path
        try:
            if path == "/auth/login":
                if not ADMIN_PASSWORD:
                    return self._json({"ok": True})
                if self._login_limited():
                    return self._error("尝试次数太多，请五分钟后再试。", 429)
                password = str(self._json_body().get("password") or "")
                if not hmac.compare_digest(password, ADMIN_PASSWORD):
                    self._login_failed()
                    return self._error("密码不对。", 401)
                self._login_succeeded()
                return self._set_session()
            if path == "/auth/logout":
                if not self._same_origin():
                    return self._error("只能在表情包小抽屉自己的页面里退出。", 403)
                return self._clear_session()
            if path == "/stickers/expand":
                if not self._require_json_auth(allow_token=True):
                    return None
                data = self._json_body()
                role = "assistant" if data.get("role") == "assistant" else "user"
                return self._json({"text": stickers.expand_for_ai(str(data.get("text") or ""), role)}, cors=True)
            if path == "/stickers/normalize":
                if not self._require_json_auth(allow_token=True):
                    return None
                return self._json({"text": stickers.normalise_reply(str(self._json_body().get("text") or ""))}, cors=True)
            if not self._require_json_auth():
                return None
            if not self._same_origin():
                return self._error("只能在表情包小抽屉自己的页面里修改。", 403)
            if path == "/stickers/api/upload":
                raw = self._body(stickers.MAX_BYTES + 1)
                label = unquote(self.headers.get("X-Label") or "")
                description = unquote(self.headers.get("X-Description") or "")
                auto = False
                if not stickers.clean_label(label):
                    kind = stickers.sniff_image(raw)
                    label = labeler.suggest_label(raw, kind[1]) if kind and len(raw) <= stickers.MAX_BYTES else ""
                    auto = bool(label)
                return self._json({**stickers.add_sticker(raw, label, description), "auto_labelled": auto}, 201)
            if path == "/stickers/api/update":
                data = self._json_body()
                enabled = data.get("ai_enabled")
                return self._json(stickers.update_sticker(
                    str(data.get("id") or ""),
                    label=data["label"] if isinstance(data.get("label"), str) else None,
                    description=data["description"] if isinstance(data.get("description"), str) else None,
                    ai_enabled=enabled if isinstance(enabled, bool) else None,
                ))
            if path == "/stickers/api/delete":
                stickers.delete_sticker(str(self._json_body().get("id") or ""))
                return self._json({"ok": True})
        except stickers.StickerError as error:
            return self._error(str(error), error.status)
        return self._error("没有这个接口。", 404)


def serve():
    if HOST != "127.0.0.1" and not ADMIN_PASSWORD:
        print("拒绝启动：云端运行必须设置 STICKER_ADMIN_PASSWORD。")
        sys.exit(2)
    if HOST != "127.0.0.1" and len(ADMIN_PASSWORD) < 12:
        print("拒绝启动：云端管理密码至少需要 12 个字符。")
        sys.exit(2)
    # 端口被占就往后挪，免得撞上别的程序。
    port_range = range(PORT, PORT + (1 if os.environ.get("PORT") else PORT_TRIES))
    for port in port_range:
        try:
            server = ThreadingHTTPServer((HOST, port), Handler)
        except OSError:
            continue
        print(f"表情包小抽屉开在 http://{HOST}:{port}  （关掉这个窗口就停了）")
        if not labeler.available():
            print("没有配置 Claude，自动起名关闭：上传时自己填名字就好（见 README）。")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return
    print(f"{PORT}–{PORT + PORT_TRIES - 1} 端口都被占用了，关掉一些程序再试。")
    sys.exit(1)


if __name__ == "__main__":
    serve()
