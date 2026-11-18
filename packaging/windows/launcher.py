"""Start the installed desktop app and keep its local service tied to a window."""

import ctypes
import http.client
import logging
import os
import socket
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8765
URL = f"http://{HOST}:{PORT}"
TITLE = "ReportContrast"
MUTEX = r"Local\ReportContrast.Desktop.7763E5EB"
INSTANCE_HEADER = "X-ReportContrast-Desktop"
LOG = logging.getLogger("reportcontrast.desktop")


def data_directory():
    override = os.environ.get("REPORTCONTRAST_DATA_DIR")
    return Path(override or (Path(os.environ["LOCALAPPDATA"]) / "ReportContrast")).resolve()


def another_instance():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateMutexW
    create.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
    create.restype = ctypes.c_void_p
    handle = create(None, False, MUTEX)
    error = ctypes.get_last_error()
    if not handle:
        raise ctypes.WinError(error)
    if error == 183:  # ERROR_ALREADY_EXISTS
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel.CloseHandle(handle)
        return True
    # Keep the original handle until process exit so Setup can detect it.
    return False


def warn(message):
    ctypes.windll.user32.MessageBoxW(None, message, TITLE, 0x30)


def open_browser():
    try:
        os.startfile(URL)
    except OSError:
        warn(f"无法打开默认浏览器。请手动访问 {URL}。")


def wait_for_existing_instance():
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        connection = http.client.HTTPConnection(HOST, PORT, timeout=0.5)
        try:
            connection.request("GET", "/api/health")
            response = connection.getresponse()
            if response.status == 200 and response.getheader(INSTANCE_HEADER) == "1":
                return True
        except (OSError, http.client.HTTPException):
            pass
        finally:
            connection.close()
        time.sleep(0.2)
    return False


class LocalService(threading.Thread):
    def __init__(self, listener, data_dir):
        super().__init__(name="reportcontrast-local-service")
        self.listener = listener
        self.data_dir = data_dir
        self.server = None
        self.stopping = threading.Event()

    @property
    def ready(self):
        return self.server is not None and self.server.started

    def stop(self):
        self.stopping.set()
        if self.server is not None:
            self.server.should_exit = True

    def run(self):
        try:
            import uvicorn
            from backend.app import ROOT, create_app

            if not (ROOT / "frontend" / "dist" / "index.html").is_file():
                raise RuntimeError("Bundled frontend index.html is missing")
            config = uvicorn.Config(
                create_app(self.data_dir), host=HOST, port=PORT, workers=1,
                loop="asyncio", http="h11", ws="none", lifespan="on",
                proxy_headers=False, log_config=None, access_log=False,
                headers=[(INSTANCE_HEADER, "1")],
            )
            self.server = uvicorn.Server(config)
            if not self.stopping.is_set():
                self.server.run(sockets=[self.listener])
        except (Exception, SystemExit):
            LOG.exception("Local service failed")
        finally:
            self.listener.close()


def control_window(service, log_path):
    import tkinter as tk
    from tkinter import ttk

    window = tk.Tk()
    window.title(TITLE)
    window.resizable(False, False)
    panel = ttk.Frame(window, padding=20)
    panel.pack(fill="both", expand=True)
    status = tk.StringVar(value="正在启动本机服务…")
    ttk.Label(panel, textvariable=status, width=44).pack(anchor="w")
    ttk.Label(panel, text=URL).pack(anchor="w", pady=(4, 14))
    buttons = ttk.Frame(panel)
    buttons.pack(anchor="w")
    browser = ttk.Button(buttons, text="打开浏览器", command=open_browser, state="disabled")
    browser.pack(side="left", padx=(0, 8))

    def stop():
        status.set("正在停止服务，等待本机任务结束…")
        browser.configure(state="disabled")
        exit_button.configure(state="disabled")
        service.stop()

    exit_button = ttk.Button(buttons, text="退出", command=stop)
    exit_button.pack(side="left")
    window.protocol("WM_DELETE_WINDOW", stop)
    opened = False

    def refresh():
        nonlocal opened
        if not service.is_alive():
            if not service.stopping.is_set():
                warn(f"本机服务未能启动或已异常停止。\n请查看日志：\n{log_path}")
            window.destroy()
            return
        if service.ready and not opened and not service.stopping.is_set():
            opened = True
            status.set("本机服务正在运行。此窗口可以最小化。")
            browser.configure(state="normal")
            open_browser()
        window.after(150, refresh)

    service.start()
    try:
        window.after(150, refresh)
        window.mainloop()
    finally:
        service.stop()
        service.join()


def main():
    log_path = None
    try:
        if another_instance():
            if wait_for_existing_instance():
                open_browser()
            else:
                warn(f"已有 ReportContrast 进程，服务尚未就绪。请检查控制窗口和端口 {PORT}。")
            return

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            try:
                # Keep the bound socket through Uvicorn startup. On Windows use
                # the default bind rules so a graceful exit can be followed by a restart.
                listener.bind((HOST, PORT))
            except OSError as exc:
                if exc.winerror not in (10013, 10048):
                    raise
                warn(f"无法使用本机端口 {PORT}。请关闭占用此端口的程序后重试。")
                return

            data_dir = data_directory()
            log_dir = data_dir / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "reportcontrast.log"
            logging.basicConfig(
                level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                handlers=[RotatingFileHandler(log_path, maxBytes=2 * 1024 * 1024, backupCount=2,
                                              encoding="utf-8")],
            )
            LOG.info("Starting %s on %s", TITLE, URL)
            control_window(LocalService(listener, data_dir), log_path)
    except Exception:
        LOG.exception("Desktop startup failed")
        detail = f"请查看日志：\n{log_path}" if log_path else "请检查本机端口和数据目录权限。"
        warn(f"无法启动 ReportContrast。\n{detail}")


if __name__ == "__main__":
    if sys.platform != "win32":
        raise SystemExit("This launcher is for Windows. On macOS, use ./run.sh.")
    main()
