"""webhook.py — 跨机任务触发的轻量 HTTP 入口（stdlib，零依赖）

形态（用户拍板）：跨机 0.0.0.0 + 强制 token。
  - 监听 settings：webhook_host（默认 0.0.0.0）/ webhook_port（默认 8620）
  - **webhook_token 未配置 → 拒绝启动**（log warning 后返回 None，fail-open 不拖垮插件）
  - 所有 /dag/* 请求必须带 `X-Dag-Token` 头（hmac.compare_digest 恒时比较）；
    GET /health 免鉴权（只暴露存活，不含任何 run 信息）
  - body 上限 1MB

端点：
  POST /dag/run            {dag, params?, idempotency_key?} → 与 dag_run 工具同响应
  GET  /dag/status?run_id= → 与 dag_status 工具同响应（unknown run → 404）
  GET  /health             → {"ok": true}
"""

from __future__ import annotations

import hmac
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger("hermes-dag.webhook")

MAX_BODY = 1 << 20  # 1MB


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, engine, token: str):
        self.engine = engine
        self.token = token
        super().__init__(address, _Handler)


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    # ------------------------------------------------------------------ util
    def log_message(self, fmt, *args):  # 静默默认 stderr 噪音
        log.debug("webhook: %s", fmt % args)

    def _json(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        provided = self.headers.get("X-Dag-Token") or ""
        return hmac.compare_digest(provided, self.server.token)

    def _read_body(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    # ------------------------------------------------------------------ verbs
    def do_POST(self):  # noqa: N802
        if urlsplit(self.path).path != "/dag/run":
            self._json(404, {"ok": False, "error": "not found"})
            return
        if not self._authorized():
            self._json(401, {"ok": False, "error": "missing or invalid X-Dag-Token"})
            return
        data = self._read_body()
        if data is None:
            self._json(400, {"ok": False, "error": "body must be a JSON object ≤1MB"})
            return
        dag = data.get("dag")
        if not dag or not isinstance(dag, str):
            self._json(400, {"ok": False, "error": "field 'dag' is required"})
            return
        params = data.get("params")
        idem = data.get("idempotency_key")
        if params is None:
            params = {}
        if not isinstance(params, dict) or (idem is not None and not isinstance(idem, str)):
            self._json(400, {"ok": False, "error": "'params' must be object, "
                             "'idempotency_key' must be string"})
            return
        try:
            result = self.server.engine.dag_run(dag, params=params,
                                                idempotency_key=idem)
        except Exception as exc:
            log.exception("webhook dag_run failed")
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        code = 200 if result.get("ok") else 400
        self._json(code, result)

    def do_GET(self):  # noqa: N802
        parts = urlsplit(self.path)
        if parts.path == "/health":
            self._json(200, {"ok": True, "plugin": "hermes-dag"})
            return
        if parts.path == "/dag/status":
            if not self._authorized():
                self._json(401, {"ok": False, "error": "missing or invalid X-Dag-Token"})
                return
            run_id = (parse_qs(parts.query).get("run_id") or [None])[0]
            try:
                result = self.server.engine.dag_status(run_id)
            except Exception as exc:
                log.exception("webhook dag_status failed")
                self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
                return
            code = 200 if result.get("ok") else 404
            self._json(code, result)
            return
        self._json(404, {"ok": False, "error": "not found"})


class WebhookServer:
    """生命周期封装：start()/stop()；port 属性暴露实际端口（port=0 时由系统分配）。"""

    def __init__(self, engine, host: str = "0.0.0.0", port: int = 8620, token: str = ""):
        self._server = _Server((host, port), engine, token)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="hermes-dag-webhook", daemon=True)
        self._thread.start()

    def stop(self):
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:
            log.exception("webhook stop error")


def start_webhook(engine, host: str, port: int, token: str | None,
                  retries: int = 3, retry_delay: float = 2.0) -> WebhookServer | None:
    """工厂：token 强制——未配置拒绝启动（返回 None），由调用方决定是否告警。
    绑定失败短重试（重启交接期端口可能短暂被上一个进程持有）。"""
    if not token:
        log.warning("hermes-dag: webhook_enabled 但未配置 webhook_token，拒绝启动 "
                    "（跨机监听 0.0.0.0 无鉴权不可接受）")
        return None
    last_exc = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            srv = WebhookServer(engine, host=host, port=int(port), token=token)
            srv.start()
            log.info("hermes-dag: webhook listening on %s:%s (token required)", host, srv.port)
            return srv
        except OSError as exc:
            last_exc = exc
            if attempt < retries:
                log.warning("hermes-dag: webhook bind %s:%s failed (attempt %d/%d): %s; retrying",
                            host, port, attempt, retries, exc)
                time.sleep(retry_delay)
        except Exception:
            log.exception("hermes-dag: webhook start failed (fail-open)")
            return None
    log.error("hermes-dag: webhook bind %s:%s failed after %d attempts (fail-open): %s",
              host, port, retries, last_exc)
    return None
