"""Cloudflare 隧道（cloudflared）跟着本程序启停。

- 启动：CLOUDFLARED_PATH 与 CLOUDFLARE_TUNNEL_TOKEN 都填了才启动；令牌经环境变量 TUNNEL_TOKEN 传给子进程，
  不出现在命令行里；不弹窗口；日志写 data/logs/cloudflared.log（D 盘项目目录）。
- 停止：程序正常退出时主动结束；Windows 上另把子进程放进“作业对象”（关闭即结束全部进程），
  即使 PyCharm 强制停止或 Python 崩溃，cloudflared 也会被系统一并结束，不留孤儿进程。
- 隧道的路由（hook.diwhx.xyz → localhost:8000）在 Cloudflare 后台配置，这里不管。
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path

from .config import DATA_DIR, get_settings

log = logging.getLogger(__name__)
LOG_FILE = DATA_DIR / "logs" / "cloudflared.log"


def not_ready_reasons() -> list[str]:
    s = get_settings()
    if not (s.cloudflared_path or s.cloudflare_tunnel_token):
        return ["未配置（CLOUDFLARED_PATH、CLOUDFLARE_TUNNEL_TOKEN 都为空）"]
    out = []
    if not s.cloudflared_path:
        out.append("CLOUDFLARED_PATH 未配置")
    elif not Path(s.cloudflared_path).is_file():
        out.append(f"找不到 cloudflared：{s.cloudflared_path}")
    if not s.cloudflare_tunnel_token:
        out.append("CLOUDFLARE_TUNNEL_TOKEN 未配置")
    return out


def _kill_on_close_job(pid: int):
    """Windows 作业对象：句柄关闭（本进程退出，含被强制结束）时结束其中所有进程。返回句柄，需一直持有。"""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                      "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC_LIMIT(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC_LIMIT), ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.OpenProcess.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    info = EXTENDED_LIMIT()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))  # JobObjectExtendedLimitInformation
    proc = k32.OpenProcess(0x0001 | 0x0100, False, pid)  # PROCESS_TERMINATE | PROCESS_SET_QUOTA
    ok = proc and k32.AssignProcessToJobObject(job, proc)
    if proc:
        k32.CloseHandle(proc)
    if not ok:
        log.warning("cloudflared 未能加入作业对象（错误 %s）：程序被强制结束时它可能不会跟着退出", ctypes.get_last_error())
    return job


class TunnelProcess:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self._job = None
        self._tail: deque[str] = deque(maxlen=30)
        self._log_fh = None
        self.connections = 0  # 本次启动以来 cloudflared 报告建立的连接数（正常为 4）
        self.command_override: list[str] | None = None  # 测试用：替换 cloudflared 命令

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> bool:
        if self.running:
            return True
        reasons = not_ready_reasons()
        if reasons and not self.command_override:
            return False
        s = get_settings()
        self.connections = 0
        cmd = self.command_override or [s.cloudflared_path, "tunnel", "--no-autoupdate", "run"]
        env = {**os.environ, "TUNNEL_TOKEN": s.cloudflare_tunnel_token or ""}
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._log_fh = open(LOG_FILE, "a", encoding="utf-8", errors="replace")
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self.proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, creationflags=flags, text=True,
                                     encoding="utf-8", errors="replace")
        self._job = _kill_on_close_job(self.proc.pid)
        threading.Thread(target=self._pump, name="cloudflared-log", daemon=True).start()
        log.info("Cloudflare 隧道已启动（cloudflared PID %s，日志 %s）", self.proc.pid, LOG_FILE)
        return True

    def _pump(self) -> None:
        proc, fh = self.proc, self._log_fh
        for line in proc.stdout:  # 令牌不在输出里；按行转存日志并保留最近 30 行给状态页
            line = line.rstrip()
            self._tail.append(line)
            fh.write(line + "\n")
            fh.flush()
            if "Registered tunnel connection" in line:
                self.connections += 1
                log.info("Cloudflare 隧道已连接：%s", line.split("connIndex=")[-1][:80])

    def stop(self, timeout: float = 10) -> None:
        if self.proc is None:
            return
        if self.running:
            self.proc.terminate()
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        log.info("Cloudflare 隧道已停止")
        self.proc = None
        if self._log_fh:
            self._log_fh.close()
            self._log_fh = None

    def status(self) -> dict:
        return {"configured": not not_ready_reasons(), "reasons": not_ready_reasons(), "running": self.running,
                "pid": self.proc.pid if self.running else None, "connections": self.connections,
                "last_lines": list(self._tail)[-5:]}


tunnel = TunnelProcess()
