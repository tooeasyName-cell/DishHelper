"""Authenticated loopback HTTP adapter and one optional mobile worker."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from urllib.parse import urlsplit

from .mobile import MobileService


class MobileRuntime:
    def __init__(self, repo, service=None):
        self.service = service or MobileService(repo)
        self.server = None
        self.stop_event = threading.Event()
        self.error = ""
        self.threads = []

    def start(self, port=None):
        if self.server:
            return self
        service = self.service
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, code, value):
                body = json.dumps(value, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def route(self, method):
                auth = self.headers.get("Authorization", "")
                if not auth.startswith("Bearer ") or not service.authorized(auth[7:]):
                    return self.respond(401, {"error": "未授权或接入未启用。"})
                try:
                    body = {}
                    if method == "POST":
                        size = int(self.headers.get("Content-Length", "0"))
                        if not 0 < size <= 100000:
                            return self.respond(413, {"error": "请求大小无效。"})
                        body = json.loads(self.rfile.read(size))
                        if not isinstance(body, dict):
                            raise ValueError("请求须为JSON对象。")
                    path = urlsplit(self.path).path
                    if path == "/bridge/v1/messages" and method == "POST":
                        if set(body) != {"message_id", "origin", "text"}:
                            raise ValueError("消息字段无效。")
                        job = service.enqueue(**body)
                        return self.respond(202, {"id": job["id"], "status": job["status"]})
                    parts = path.strip("/").split("/")
                    if parts[:3] == ["bridge", "v1", "jobs"] and len(parts) in (4, 5):
                        job = service.get_job(parts[3])
                        if not job:
                            return self.respond(404, {"error": "任务不存在。"})
                        if len(parts) == 5 and parts[4] == "delivery" and method == "POST":
                            if set(body) != {"status"}:
                                raise ValueError("投递字段无效。")
                            service.delivery(job["id"], body["status"])
                            return self.respond(200, {"ok": True})
                        if len(parts) == 4 and method == "GET":
                            return self.respond(200, {key: job[key] for key in ("id", "status", "reply", "error", "delivery")})
                    return self.respond(404, {"error": "接口不存在。"})
                except (ValueError, TypeError, UnicodeError, KeyError):
                    return self.respond(400, {"error": "请求内容无效或来源不匹配。"})
                except Exception:
                    return self.respond(503, {"error": "本机服务暂不可用，请在电脑检查。"})

            def do_POST(self):
                self.route("POST")

            def do_GET(self):
                self.route("GET")
        # Bind first: a second UI process must not interrupt a running owner's jobs.
        self.server = ThreadingHTTPServer(("127.0.0.1", self.service.get_settings()["port"] if port is None else port), Handler)
        self.server.daemon_threads = True
        self.service.recover_interrupted()
        self.threads = [threading.Thread(target=self.server.serve_forever, daemon=True),
                        threading.Thread(target=self._work, daemon=True)]
        for thread in self.threads:
            thread.start()
        return self

    def _work(self):
        while not self.stop_event.is_set():
            try:
                result = self.service.process_next()
            except Exception:
                self.error = "手机工作线程异常停止；请重启应用并检查中断记录。"
                break
            if result is None:
                self.stop_event.wait(0.3)

    def stop(self):
        self.stop_event.set()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        for thread in self.threads:
            thread.join(timeout=1)

    def status(self):
        return {"running": bool(self.server and not self.error), "error": self.error,
                "port": self.server.server_port if self.server else None}


_runtimes = {}
_lock = threading.Lock()


def get_runtime_status(repo):
    """Inspect only; never bind a port, recover jobs, or start a worker."""
    with _lock:
        runtime = _runtimes.get(str(repo.path.resolve()))
        return runtime.status() if runtime else {"running": False, "error": "", "port": None}


def ensure_runtime(repo):
    key = str(repo.path.resolve())
    service = MobileService(repo)
    cfg = service.get_settings()
    with _lock:
        runtime = _runtimes.get(key)
        if not cfg["enabled"]:
            if runtime:
                runtime.stop()
                _runtimes.pop(key, None)
            return {"running": False, "error": "", "port": None}
        if runtime and runtime.server and runtime.server.server_port == cfg["port"]:
            return runtime.status()
        if runtime:
            runtime.stop()
        try:
            runtime = MobileRuntime(repo, service).start()
            _runtimes[key] = runtime
            return runtime.status()
        except OSError:
            return {"running": False, "error": "手机桥接端口不可用，可能已有另一实例运行或端口被占用。", "port": cfg["port"]}
