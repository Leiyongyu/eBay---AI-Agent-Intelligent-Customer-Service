"""cloudflared 跟着程序启停：没配置不启动；令牌经环境变量传给子进程；能识别已连接；停止后进程退出。"""
import sys
import time

from app import config

FAKE = ("import os, sys, time\n"
        "print('token set' if os.environ.get('TUNNEL_TOKEN') == 'tok-123' else 'token missing', flush=True)\n"
        "print('INF Registered tunnel connection connIndex=0 location=lax01', flush=True)\n"
        "time.sleep(60)\n")


def test_not_started_without_config(app_env, monkeypatch):
    from app.tunnel import TunnelProcess, not_ready_reasons

    monkeypatch.delenv("CLOUDFLARED_PATH", raising=False)
    monkeypatch.delenv("CLOUDFLARE_TUNNEL_TOKEN", raising=False)
    config.get_settings.cache_clear()
    assert not TunnelProcess().start()
    monkeypatch.setenv("CLOUDFLARED_PATH", "D:/nowhere/cloudflared.exe")
    config.get_settings.cache_clear()
    assert {r.split("：")[0] for r in not_ready_reasons()} == {"找不到 cloudflared", "CLOUDFLARE_TUNNEL_TOKEN 未配置"}


def test_start_passes_token_by_env_and_stops(app_env, monkeypatch):
    from app.tunnel import TunnelProcess

    monkeypatch.setenv("CLOUDFLARE_TUNNEL_TOKEN", "tok-123")
    config.get_settings.cache_clear()
    t = TunnelProcess()
    t.command_override = [sys.executable, "-c", FAKE]
    assert t.start() and t.running
    deadline = time.time() + 10
    while t.connections == 0 and time.time() < deadline:
        time.sleep(0.1)
    st = t.status()
    assert st["connections"] == 1 and "token set" in st["last_lines"]  # 令牌经环境变量 TUNNEL_TOKEN 传入
    proc = t.proc
    t.stop()
    assert proc.poll() is not None and not t.running and t.status()["pid"] is None
