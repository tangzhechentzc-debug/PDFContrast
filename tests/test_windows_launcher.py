"""Windows 桌面启动器 wait_for_existing_instance 跨平台行为回归测试。"""

from contextlib import contextmanager
import http.server
import importlib.util
import json
from pathlib import Path
import socket
import sys
import threading
import time as real_time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER_PATH = ROOT / "packaging" / "windows" / "launcher.py"
PROXY_ENV_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


@pytest.fixture
def launcher(monkeypatch):
    """按用例隔离加载 Windows 启动器模块，并在结束后清理 sys.modules。"""
    for var in PROXY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    module_name = "reportcontrast_test_windows_launcher"
    spec = importlib.util.spec_from_file_location(module_name, LAUNCHER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(module_name, None)


def install_fake_clock(launcher_mod, monkeypatch):
    """仅替换 launcher 模块内的 time 对象，避免影响 pytest 与线程调度。"""
    state = {"now": 0.0, "sleeps": []}

    def monotonic():
        return state["now"]

    def sleep(seconds):
        state["sleeps"].append(seconds)
        state["now"] += seconds

    monkeypatch.setattr(
        launcher_mod,
        "time",
        SimpleNamespace(monotonic=monotonic, sleep=sleep),
    )
    return state


@contextmanager
def mock_health_server(responder):
    """启动本地临时回环 HTTP 服务并收集请求轨迹与后台异常。"""
    errors: list[BaseException] = []
    requests: list[tuple[str, str]] = []
    lock = threading.Lock()

    class QuietServer(http.server.ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

        def handle_error(self, request, client_address):
            _, exc, _ = sys.exc_info()
            # wait_for_existing_instance 仅读取状态行与响应头即关闭连接，忽略对端提前断开异常
            if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
                return
            if exc is not None:
                errors.append(exc)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            with lock:
                requests.append((self.command, self.path))
                attempt = len(requests)
            outcome = responder(self, attempt)
            if outcome is None:
                # 模拟服务端直接断开 TCP 连接，触发客户端 RemoteDisconnected
                self.close_connection = True
                self.connection.close()
                return
            status, headers, body = outcome
            self.send_response(status)
            for name, value in headers:
                self.send_header(name, value)
            if body is not None:
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body is not None:
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def log_message(self, format, *args):
            # 静默标准 HTTP 日志，避免轮询测试刷屏
            pass

    server = QuietServer(("127.0.0.1", 0), Handler)

    def serve():
        try:
            server.serve_forever(poll_interval=0.01)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield server.server_address[0], server.server_address[1], requests
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        assert not thread.is_alive(), "临时 HTTP 服务线程未能按时关闭"
        assert not errors, f"临时 HTTP 服务线程发生异常: {errors!r}"


def _health_payload() -> bytes:
    return json.dumps({"status": "ok", "version": "0.2.0"}).encode("utf-8")


def test_returns_true_when_health_returns_200_with_desktop_header(launcher, monkeypatch):
    """返回 200 且携带 X-ReportContrast-Desktop: 1 时立即认定实例已就绪。"""
    clock = install_fake_clock(launcher, monkeypatch)

    def responder(_handler, _attempt):
        return 200, [(launcher.INSTANCE_HEADER, "1")], _health_payload()

    with mock_health_server(responder) as (host, port, requests):
        monkeypatch.setattr(launcher, "HOST", host)
        monkeypatch.setattr(launcher, "PORT", port)
        assert launcher.wait_for_existing_instance() is True

    assert requests == [("GET", "/api/health")]
    assert clock["sleeps"] == []


def test_header_name_is_case_insensitive_for_http_compliance(launcher, monkeypatch):
    """HTTP 响应头名称大小写不敏感时仍能正确识别。"""
    clock = install_fake_clock(launcher, monkeypatch)

    def responder(_handler, _attempt):
        return 200, [("x-reportcontrast-desktop", "1")], _health_payload()

    with mock_health_server(responder) as (host, port, requests):
        monkeypatch.setattr(launcher, "HOST", host)
        monkeypatch.setattr(launcher, "PORT", port)
        assert launcher.wait_for_existing_instance() is True

    assert requests == [("GET", "/api/health")]
    assert clock["sleeps"] == []


def test_returns_false_when_desktop_header_is_missing(launcher, monkeypatch):
    """端口被普通服务占用（200 但缺失桌面标识头）时超时返回 False。"""
    clock = install_fake_clock(launcher, monkeypatch)
    started = real_time.perf_counter()

    def responder(_handler, _attempt):
        return 200, [("Content-Type", "application/json")], _health_payload()

    with mock_health_server(responder) as (host, port, requests):
        monkeypatch.setattr(launcher, "HOST", host)
        monkeypatch.setattr(launcher, "PORT", port)
        assert launcher.wait_for_existing_instance() is False

    assert real_time.perf_counter() - started < 1.0
    assert len(requests) == len(clock["sleeps"])
    assert sum(clock["sleeps"]) >= 15.0
    assert all(s == pytest.approx(0.2) for s in clock["sleeps"])


@pytest.mark.parametrize("bad_value", ["0", "true", "2", "10", ""])
def test_returns_false_when_desktop_header_value_is_not_one(launcher, monkeypatch, bad_value):
    """桌面标识头取值不严格等于 '1' 时不能误判就绪。"""
    clock = install_fake_clock(launcher, monkeypatch)
    started = real_time.perf_counter()

    def responder(_handler, _attempt):
        return 200, [(launcher.INSTANCE_HEADER, bad_value)], _health_payload()

    with mock_health_server(responder) as (host, port, _requests):
        monkeypatch.setattr(launcher, "HOST", host)
        monkeypatch.setattr(launcher, "PORT", port)
        assert launcher.wait_for_existing_instance() is False

    assert real_time.perf_counter() - started < 1.0
    assert sum(clock["sleeps"]) >= 15.0
    assert all(s == pytest.approx(0.2) for s in clock["sleeps"])


@pytest.mark.parametrize("status_code", [500, 503, 404, 302])
def test_returns_false_on_non_200_status_even_with_desktop_header(launcher, monkeypatch, status_code):
    """非 200 状态码即使带有桌面标识头也不能认定为就绪；302 重定向目标路径返回 200+头时也不得跟随。"""
    clock = install_fake_clock(launcher, monkeypatch)
    started = real_time.perf_counter()

    def responder(handler, _attempt):
        if status_code == 302:
            if handler.path != "/api/health":
                return 200, [(launcher.INSTANCE_HEADER, "1")], _health_payload()
            return 302, [(launcher.INSTANCE_HEADER, "1"), ("Location", "/")], b""
        return status_code, [(launcher.INSTANCE_HEADER, "1")], b""

    with mock_health_server(responder) as (host, port, requests):
        monkeypatch.setattr(launcher, "HOST", host)
        monkeypatch.setattr(launcher, "PORT", port)
        assert launcher.wait_for_existing_instance() is False

    assert requests and all(path == "/api/health" for _, path in requests)
    assert real_time.perf_counter() - started < 1.0
    assert sum(clock["sleeps"]) >= 15.0
    assert all(s == pytest.approx(0.2) for s in clock["sleeps"])


def test_returns_false_quickly_when_port_is_not_listening(launcher, monkeypatch):
    """目标端口未监听时捕获连接错误并在虚拟超时后返回 False。"""
    clock = install_fake_clock(launcher, monkeypatch)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        unused_port = probe.getsockname()[1]

    monkeypatch.setattr(launcher, "HOST", "127.0.0.1")
    monkeypatch.setattr(launcher, "PORT", unused_port)
    started = real_time.perf_counter()

    assert launcher.wait_for_existing_instance() is False
    assert real_time.perf_counter() - started < 1.0
    assert sum(clock["sleeps"]) >= 15.0
    assert all(s == pytest.approx(0.2) for s in clock["sleeps"])


def test_returns_false_when_server_drops_connection(launcher, monkeypatch):
    """服务端直接断开连接（RemoteDisconnected）时捕获 HTTPException 并超时返回 False。"""
    clock = install_fake_clock(launcher, monkeypatch)
    started = real_time.perf_counter()

    def responder(_handler, _attempt):
        return None

    with mock_health_server(responder) as (host, port, _requests):
        monkeypatch.setattr(launcher, "HOST", host)
        monkeypatch.setattr(launcher, "PORT", port)
        assert launcher.wait_for_existing_instance() is False

    assert real_time.perf_counter() - started < 1.0
    assert sum(clock["sleeps"]) >= 15.0
    assert all(s == pytest.approx(0.2) for s in clock["sleeps"])


def test_retries_until_existing_instance_becomes_ready(launcher, monkeypatch):
    """前两次未就绪（503、200 缺头）时按 0.2s 间隔重试，第 3 次成功返回 True。"""
    clock = install_fake_clock(launcher, monkeypatch)

    def responder(_handler, attempt):
        if attempt == 1:
            return 503, [(launcher.INSTANCE_HEADER, "1")], b"starting"
        if attempt == 2:
            return 200, [], _health_payload()
        return 200, [(launcher.INSTANCE_HEADER, "1")], _health_payload()

    with mock_health_server(responder) as (host, port, requests):
        monkeypatch.setattr(launcher, "HOST", host)
        monkeypatch.setattr(launcher, "PORT", port)
        assert launcher.wait_for_existing_instance() is True

    assert len(requests) == 3
    assert clock["sleeps"] == [pytest.approx(0.2), pytest.approx(0.2)]


def test_ignores_proxy_environment_variables(launcher, monkeypatch):
    """注入不可达系统代理变量时仍直连本地回环端口并成功识别。"""
    clock = install_fake_clock(launcher, monkeypatch)
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")

    def responder(_handler, _attempt):
        return 200, [(launcher.INSTANCE_HEADER, "1")], _health_payload()

    with mock_health_server(responder) as (host, port, _requests):
        monkeypatch.setattr(launcher, "HOST", host)
        monkeypatch.setattr(launcher, "PORT", port)
        assert launcher.wait_for_existing_instance() is True

    assert clock["sleeps"] == []
