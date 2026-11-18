#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自检：HTTP 静态页 / WebSocket 收发 / 待注入队列 / 中文键盘注入。

用法：python selftest.py
会短暂创建一个 Tk 窗口并抢占焦点（约 1 秒），用于验证注入。
"""
import base64
import json
import os
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:  # 控制台是 GBK 时避免打印 ✔ 之类字符直接崩掉
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass
import bridge as B  # noqa: E402

PORT = 8799
TOKEN = "selftest-token"
FAILS = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


# --------------------------------------------------------------------------
# 极简 WS 客户端
# --------------------------------------------------------------------------
def ws_connect(port, token):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET /ws?k={token} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    s.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(1)
        if not chunk:
            raise RuntimeError("handshake closed")
        buf += chunk
    if b"101" not in buf.split(b"\r\n")[0]:
        raise RuntimeError("handshake failed: " + buf.split(b"\r\n")[0].decode("latin1"))
    return s


def ws_send(s, obj):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    mask = os.urandom(4)
    n = len(data)
    head = bytearray([0x81])
    if n < 126:
        head.append(0x80 | n)
    elif n < 65536:
        head.append(0x80 | 126)
        head += struct.pack(">H", n)
    else:
        head.append(0x80 | 127)
        head += struct.pack(">Q", n)
    head += mask
    s.sendall(bytes(head) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


def recv_exact(s, n):
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


def ws_recv(s):
    b1, b2 = recv_exact(s, 2)
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack(">H", recv_exact(s, 2))[0]
    elif n == 127:
        n = struct.unpack(">Q", recv_exact(s, 8))[0]
    payload = recv_exact(s, n) if n else b""
    return json.loads(payload.decode("utf-8"))


def http_get(port, path, timeout=3):
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    s.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode())
    data = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    s.close()
    head, _, body = data.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    return status, head, body


# --------------------------------------------------------------------------
def main():
    print("== 1. 启动服务（headless, 不自动注入） ==")
    state_bak = None
    if os.path.exists(B.STATE_PATH):
        state_bak = open(B.STATE_PATH, "rb").read()

    cfg = {"port": PORT, "token": TOKEN, "auto_inject": False,
           "paste_mode": False, "hotkeys": False, "auto_send_ms": 1200}
    br = B.Bridge(cfg, headless=True)
    br.pending = []
    br.start_injector()
    br.run_loop_thread()
    time.sleep(0.8)
    check("服务线程无错误", getattr(br, "start_error", None) is None, getattr(br, "start_error", None))

    print("== 2. HTTP 静态资源 ==")
    st, _, body = http_get(PORT, "/health")
    check("/health 200", st == 200, st)
    check("/health 内容", b"typebyvoice" in body, body[:80])
    st, _, body = http_get(PORT, "/")
    check("/ 200 且是页面", st == 200 and "TypeByVoice".encode() in body and b"<textarea" in body, st)
    st, _, body = http_get(PORT, "/manifest.webmanifest")
    check("manifest 200", st == 200 and b"standalone" in body, st)
    st, _, body = http_get(PORT, "/icon-192.png")
    check("图标 200 PNG", st == 200 and body[:8] == b"\x89PNG\r\n\x1a\n", st)
    st, _, body = http_get(PORT, f"/qr.png?k={TOKEN}")
    check("二维码 200 PNG", st == 200 and body[:8] == b"\x89PNG\r\n\x1a\n", st)
    st, _, body = http_get(PORT, "/qr.png?k=wrong")
    check("二维码错 token 403", st == 403, st)
    st, _, _ = http_get(PORT, "/../bridge.py")
    check("目录穿越被挡", st in (403, 404), st)

    print("== 3. WebSocket 收发 ==")
    s = ws_connect(PORT, TOKEN)
    hello = ws_recv(s)
    check("收到 hello", hello.get("t") == "hello", hello)
    ws_send(s, {"t": "hello", "device": "selftest"})
    ws_send(s, {"t": "seg", "id": "st-1", "text": "你好，测试一句。"})
    ack = None
    t0 = time.time()
    while time.time() - t0 < 3:
        m = ws_recv(s)
        if m.get("t") == "ack" and m.get("id") == "st-1":
            ack = m
            break
    check("收到 ack", ack is not None, ack)
    check("文本进入待注入队列", any(p.get("text") == "你好，测试一句。" for p in br.pending),
          br.pending)
    ws_send(s, {"t": "seg", "id": "st-1", "text": "你好，测试一句。"})
    time.sleep(0.4)
    check("重复 id 被去重", sum(1 for p in br.pending if p.get("id") == "st-1") == 1, br.pending)
    ws_send(s, {"t": "ping"})
    got_pong = False
    t0 = time.time()
    while time.time() - t0 < 2:
        m = ws_recv(s)
        if m.get("t") == "pong":
            got_pong = True
            break
    check("ping/pong", got_pong)
    s.close()

    print("== 4. 中文注入（会短暂抢焦点） ==")
    try:
        import ctypes
        import tkinter as tk
        from ctypes import wintypes

        u32 = ctypes.WinDLL("user32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        u32.GetForegroundWindow.restype = wintypes.HWND
        u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u32.GetWindowThreadProcessId.restype = wintypes.DWORD
        u32.SetForegroundWindow.argtypes = [wintypes.HWND]
        u32.SetFocus.argtypes = [wintypes.HWND]
        u32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        k32.GetCurrentThreadId.restype = wintypes.DWORD

        def force_foreground(hwnd):
            """绕过 Windows 前台锁，把窗口真正提到前台。"""
            cur = u32.GetForegroundWindow()
            cur_thread = u32.GetWindowThreadProcessId(cur, None)
            me = k32.GetCurrentThreadId()
            u32.AttachThreadInput(me, cur_thread, True)
            u32.SetForegroundWindow(hwnd)
            u32.SetFocus(hwnd)
            u32.AttachThreadInput(me, cur_thread, False)

        root = tk.Tk()
        root.title("selftest")
        root.geometry("420x200+200+200")
        txt = tk.Text(root, font=("Microsoft YaHei UI", 16))
        txt.pack(fill="both", expand=True)

        def pump(seconds):
            """边等边泵消息：SendInput 的事件要等目标线程取消息才落到窗口上。"""
            end = time.time() + seconds
            while time.time() < end:
                root.update()
                time.sleep(0.02)

        def refocus():
            # SetForegroundWindow 会被系统降级，Tk 的 focus_force 单独用无效，必须配 AttachThreadInput
            force_foreground(int(root.wm_frame(), 16))
            root.update()
            txt.focus_force()
            root.update()
            pump(0.3)

        root.update_idletasks()
        root.wait_visibility()
        refocus()

        ok, how = br.injector.type_text("中文注入 test-123")
        pump(0.8)
        got = txt.get("1.0", "end-1c")
        check("注入返回成功", ok, how)
        check("注入了正确文字", got == "中文注入 test-123", repr(got))

        txt.delete("1.0", "end")
        pump(0.2)
        refocus()
        br.injector.type_text("第一行\n第二行")
        pump(0.8)
        got2 = txt.get("1.0", "end-1c")
        check("换行变成回车", got2 == "第一行\n第二行", repr(got2))
        root.destroy()
    except Exception as exc:  # noqa: BLE001
        check("注入测试未抛异常", False, repr(exc))

    if state_bak is not None:
        open(B.STATE_PATH, "wb").write(state_bak)
    elif os.path.exists(B.STATE_PATH):
        os.remove(B.STATE_PATH)

    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：" + ", ".join(FAILS))
        return 1
    print("全部通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
