#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TypeByVoice —— 用手机的语音输入法，直接给电脑打字。

思路：手机打开本机网页（添加到主屏幕 = 一个图标），用手机上任意输入法的语音键说话，
文字落到网页输入框里 → 通过局域网 WebSocket 实时送到电脑 → 注入到当前焦点窗口。

换句话说：手机负责"听"，电脑负责"打字"，中间这条局域网链路常驻、断了自动重连。

特性：
  * 零第三方运行时依赖（qrcode/Pillow 仅用于显示二维码，缺了也能跑）
  * 断线自动重连 + 本地暂存队列，文字不丢
  * 服务器常驻、开机自启、崩溃自动拉起（配合 watchdog.vbs）
  * 全局热键：暂停注入 / 撤销上一句 / 唤出面板
"""

import argparse
import asyncio
import base64
import hashlib
import io
import json
import os
import queue
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from collections import deque
from urllib.parse import urlsplit, parse_qs, unquote

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(APP_DIR, "web")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
STATE_PATH = os.path.join(APP_DIR, "state.json")
STOP_FLAG = os.path.join(APP_DIR, "stopped.flag")
ALIVE_PATH = os.path.join(APP_DIR, "bridge.alive")
LAUNCHER = os.path.join(APP_DIR, "launch.exe")
VERSION = "1.1.0"
# 手机页上要显示"这台是哪个电脑"，否则同一个家里有两台电脑时容易说错机器
try:
    HOST_NAME = os.environ.get("COMPUTERNAME") or __import__("socket").gethostname() or "PC"
except Exception:  # noqa: BLE001
    HOST_NAME = "PC"

DEFAULT_CONFIG = {
    "port": 8765,
    "token": "",
    "auto_inject": True,
    "paste_mode": False,
    "hotkeys": True,
    "auto_send_ms": 1200,
}

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def log(*args):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}]", *args, flush=True)


def lan_ip() -> str:
    """取本机在默认路由上的局域网 IP（排除 127/虚拟网卡）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        s.close()


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(load_json(CONFIG_PATH, {}))
    if not cfg.get("token"):
        cfg["token"] = secrets.token_urlsafe(8)
        save_json(CONFIG_PATH, cfg)
    return cfg


def save_config(cfg):
    save_json(CONFIG_PATH, cfg)


# --------------------------------------------------------------------------
# 文本注入（Win32 SendInput）
# --------------------------------------------------------------------------
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT

    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    user32.OpenClipboard.argtypes = (wintypes.HWND,)
    user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.GetClipboardData.argtypes = (wintypes.UINT,)
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)

    # 前台窗口相关（64 位下必须显式声明，否则句柄会被截断）
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.SetFocus.argtypes = (wintypes.HWND,)
    user32.SetFocus.restype = wintypes.HWND
    user32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
    user32.AttachThreadInput.restype = wintypes.BOOL
    try:  # 未公开但长期可用：绕过前台锁把窗口翻到最前
        user32.SwitchToThisWindow.argtypes = (wintypes.HWND, wintypes.BOOL)
        user32.SwitchToThisWindow.restype = None
    except AttributeError:
        pass
    user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    kernel32.GetCurrentProcessId.restype = wintypes.DWORD


class Injector:
    """把文本打进当前焦点窗口。"""

    def __init__(self):
        self.available = sys.platform == "win32"
        self._last_foreign = None   # 最近一个"不属于本程序"的前台窗口
        self._watch_stop = False

    # ---- 记住用户真正在用的窗口 -----------------------------------------
    def is_foreground_ours(self) -> bool:
        """当前前台窗口是不是本程序（面板）自己的。"""
        if not self.available:
            return False
        try:
            h = user32.GetForegroundWindow()
            if not h:
                return False
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
            return pid.value == kernel32.GetCurrentProcessId()
        except Exception:  # noqa: BLE001
            return False

    def watch_foreground(self):
        """后台线程：持续记住用户最近操作的那个窗口。"""
        while not self._watch_stop:
            try:
                h = user32.GetForegroundWindow()
                if h and not self.is_foreground_ours():
                    self._last_foreign = h
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.8)

    def activate_last_foreign(self, settle=0.25) -> bool:
        """把用户先前的窗口提回前台，避免文字打進面板自己。"""
        if not self.available or not self._last_foreign:
            return False
        try:
            cur = user32.GetForegroundWindow()
            cur_thread = user32.GetWindowThreadProcessId(cur, None) if cur else 0
            me = kernel32.GetCurrentThreadId()
            attached = False
            if cur_thread:
                attached = bool(user32.AttachThreadInput(me, cur_thread, True))
            ok = user32.SetForegroundWindow(self._last_foreign)
            user32.SetFocus(self._last_foreign)
            if attached:
                user32.AttachThreadInput(me, cur_thread, False)
            time.sleep(settle)
            return bool(ok)
        except Exception:  # noqa: BLE001
            return False

    def force_foreground(self, hwnd) -> bool:
        """把指定窗口（本程序面板）提到前台并给焦点。"""
        if not self.available or not hwnd:
            return False
        try:
            cur = user32.GetForegroundWindow()
            cur_thread = user32.GetWindowThreadProcessId(cur, None) if cur else 0
            me = kernel32.GetCurrentThreadId()
            attached = False
            if cur_thread and cur_thread != me:
                attached = bool(user32.AttachThreadInput(me, cur_thread, True))
            user32.SetForegroundWindow(hwnd)
            user32.SetFocus(hwnd)
            if attached:
                user32.AttachThreadInput(me, cur_thread, False)
            return True
        except Exception:  # noqa: BLE001
            return False

    # ---- 键盘注入 -------------------------------------------------------
    def _events_for_text(self, text):
        events = []
        data = text.encode("utf-16-le")
        for i in range(0, len(data), 2):
            unit = int.from_bytes(data[i:i + 2], "little")
            if unit == 0x0A:  # \n -> Enter
                events.append((0x0D, 0, 0))
                events.append((0x0D, 0, KEYEVENTF_KEYUP))
            elif unit == 0x0D:
                continue
            elif unit == 0x09:  # \t -> Tab
                events.append((0x09, 0, 0))
                events.append((0x09, 0, KEYEVENTF_KEYUP))
            else:
                events.append((0, unit, KEYEVENTF_UNICODE))
                events.append((0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
        return events

    def _send(self, events):
        if not events:
            return 0
        arr = (INPUT * len(events))()
        for idx, (vk, scan, flags) in enumerate(events):
            arr[idx].type = 1  # INPUT_KEYBOARD
            arr[idx].ki = KEYBDINPUT(vk, scan, flags, 0, 0)
        sent = user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))
        return sent

    def type_text(self, text, paste=False):
        """返回 (ok, method)。"""
        if not text:
            return True, "empty"
        if not self.available:
            return False, "unsupported"
        if paste:
            return self._paste_text(text)
        events = self._events_for_text(text)
        total = 0
        step = 240  # 一批 120 个字符
        for i in range(0, len(events), step):
            total += self._send(events[i:i + step])
            time.sleep(0.002)
        expected = len(events)
        if total >= expected * 0.9:
            return True, "keyboard"
        # 键盘注入被吞（例如以管理员权限运行的窗口），退回剪贴板粘贴
        ok, how = self._paste_text(text)
        return ok, how if ok else "keyboard-failed"

    # ---- 剪贴板 ---------------------------------------------------------
    def _set_clipboard(self, text):
        for _ in range(6):
            if user32.OpenClipboard(None):
                break
            time.sleep(0.03)
        else:
            return False
        try:
            user32.EmptyClipboard()
            buf = ctypes.create_unicode_buffer(text)
            size = ctypes.sizeof(buf)
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
            if not handle:
                return False
            ptr = kernel32.GlobalLock(handle)
            ctypes.memmove(ptr, buf, size)
            kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                return False
            return True
        finally:
            user32.CloseClipboard()

    def _get_clipboard(self):
        if not user32.OpenClipboard(None):
            return None
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return None
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return None
            try:
                return ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

    def _send_combo(self, vk_mod, vk_key):
        ev = [
            (vk_mod, 0, 0),
            (vk_key, 0, 0),
            (vk_key, 0, KEYEVENTF_KEYUP),
            (vk_mod, 0, KEYEVENTF_KEYUP),
        ]
        self._send(ev)

    def _paste_text(self, text):
        prev = self._get_clipboard()
        if not self._set_clipboard(text):
            return False, "clipboard-busy"
        time.sleep(0.06)
        self._send_combo(0x11, 0x56)  # Ctrl+V
        time.sleep(0.22)
        if prev is not None:
            try:
                self._set_clipboard(prev)
            except Exception:
                pass
        return True, "clipboard"

    # ---- 撤销 -----------------------------------------------------------
    def backspaces(self, count):
        if count <= 0:
            return
        count = min(count, 2000)
        ev = []
        for _ in range(count):
            ev.append((0x08, 0, 0))
            ev.append((0x08, 0, KEYEVENTF_KEYUP))
        for i in range(0, len(ev), 240):
            self._send(ev[i:i + 240])
            time.sleep(0.003)


# --------------------------------------------------------------------------
# WebSocket 服务
# --------------------------------------------------------------------------
def ws_frame(text: str) -> bytes:
    data = text.encode("utf-8")
    n = len(data)
    if n < 126:
        return struct.pack("BB", 0x81, n) + data
    if n < 65536:
        return struct.pack(">BBH", 0x81, 126, n) + data
    return struct.pack(">BBQ", 0x81, 127, n) + data


async def ws_read_frame(reader):
    head = await reader.readexactly(2)
    b1, b2 = head[0], head[1]
    fin = bool(b1 & 0x80)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack(">H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", await reader.readexactly(8))[0]
    if length > 4 * 1024 * 1024:
        raise ValueError("frame too large")
    mask = await reader.readexactly(4) if masked else b"\x00\x00\x00\x00"
    payload = await reader.readexactly(length) if length else b""
    if masked:
        payload = bytes(c ^ mask[i & 3] for i, c in enumerate(payload))
    return fin, opcode, payload


class Client:
    def __init__(self, cid, writer, name="phone"):
        self.cid = cid
        self.writer = writer
        self.name = name
        self.q = asyncio.Queue(maxsize=200)
        self.alive = True

    async def writer_loop(self):
        try:
            while self.alive:
                msg = await self.q.get()
                if msg is None:
                    break
                self.writer.write(ws_frame(msg))
                await self.writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        except Exception as exc:  # noqa: BLE001
            log("writer error:", exc)

    def send(self, loop, obj):
        if not self.alive:
            return
        try:
            data = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
            loop.call_soon_threadsafe(self.q.put_nowait, data)
        except (RuntimeError, asyncio.QueueFull):
            pass


class Bridge:
    def __init__(self, cfg, headless=False):
        self.cfg = cfg
        self.headless = headless
        self.loop = None
        self.clients = {}
        self.client_seq = 0
        self.paused = False
        self.recent_ids = deque(maxlen=2000)
        self.pending = []          # 尚未注入的段落 [{id,text,ts}]
        self.inject_q = queue.Queue()
        self.injector = Injector()
        self.log_lines = deque(maxlen=200)
        self.stats = {"received": 0, "injected": 0, "failed": 0, "dropped": 0}
        self.last_injected = ""
        self.last_injected_id = None
        self.started = time.time()
        self.url = ""
        self.panel = None
        self.show_request = False   # 另一个实例点快捷方式时置位，由面板 refresh 消费
        self._load_state()

    # ---- 状态持久化 -----------------------------------------------------
    def _load_state(self):
        st = load_json(STATE_PATH, {})
        self.pending = [p for p in st.get("pending", []) if isinstance(p, dict) and p.get("text")]
        for pid in st.get("recent_ids", [])[-500:]:
            self.recent_ids.append(pid)
        if self.pending:
            log(f"恢复 {len(self.pending)} 条未注入文本（先挂起，可在面板里注入）")

    def _save_state(self):
        try:
            save_json(STATE_PATH, {
                "pending": self.pending[-200:],
                "recent_ids": list(self.recent_ids)[-800:],
            })
        except Exception as exc:  # noqa: BLE001
            log("state save failed:", exc)

    # ---- 日志 -----------------------------------------------------------
    def add_log(self, text):
        self.log_lines.append(f"{time.strftime('%H:%M:%S')}  {text}")

    # ---- 注入工作线程 ---------------------------------------------------
    def start_injector(self):
        t = threading.Thread(target=self._inject_loop, name="injector", daemon=True)
        t.start()
        w = threading.Thread(target=self.injector.watch_foreground, name="focus-watch", daemon=True)
        w.start()

    def _inject_loop(self):
        while True:
            item = self.inject_q.get()
            if item is None:
                break
            if len(item) >= 3:
                pid, text, activate = item[0], item[1], item[2]
            else:
                pid, text, activate = item[0], item[1], False
            if activate:
                self.injector.activate_last_foreign(0.25)
            ok, how = self.injector.type_text(text, paste=bool(self.cfg.get("paste_mode")))
            if ok:
                self.stats["injected"] += 1
                self.last_injected = text
                self.last_injected_id = pid
                self.add_log(f"已注入 {len(text)} 字（{how}）：{text[:40]}")
            else:
                self.stats["failed"] += 1
                # 注入失败（目标窗口提权、锁屏等）：放回待注入队列，别把用户的话弄丢
                if self.loop:
                    self.loop.call_soon_threadsafe(self._requeue_failed, pid, text)
                self.add_log(f"注入失败（{how}），已转暂存：{text[:40]}")
            if self.loop:
                self.loop.call_soon_threadsafe(self._after_inject, pid, ok, how)

    def _after_inject(self, pid, ok, how):
        self._broadcast({"t": "done", "id": pid, "ok": ok, "how": how,
                         "pending": len(self.pending)})
        if self.panel:
            self.panel.on_change()

    def _requeue_failed(self, pid, text):
        """注入失败的文字退回待注入队列（按 id 去重），之后可手动「注入暂存」。"""
        if any(it.get("id") == pid for it in self.pending):
            return
        self.pending.append({"id": pid, "text": text, "ts": time.time()})
        self._save_state()

    # ---- 消息处理 -------------------------------------------------------
    def _broadcast(self, obj):
        for c in list(self.clients.values()):
            c.send(self.loop, obj)

    def _status_obj(self):
        return {
            "t": "status",
            "paused": self.paused,
            "clients": len(self.clients),
            "pending": len(self.pending),
            "stats": self.stats,
            "paste": bool(self.cfg.get("paste_mode")),
            "auto_inject": bool(self.cfg.get("auto_inject")),
        }

    def handle_message(self, client, raw):
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if not isinstance(msg, dict):
            return
        t = msg.get("t")
        if t == "hello":
            client.name = str(msg.get("device") or "phone")[:40]
            client.send(self.loop, self._status_obj())
            self._broadcast({"t": "status", **{k: v for k, v in self._status_obj().items() if k != "t"}})
            self.add_log(f"手机已连接：{client.name}")
            if self.panel:
                self.panel.on_change()
            return
        if t == "ping":
            client.send(self.loop, {"t": "pong", "ts": time.time()})
            return
        if t == "seg":
            text = msg.get("text")
            pid = msg.get("id")
            if not isinstance(text, str) or not text.strip() or pid is None:
                return
            if pid in self.recent_ids:
                client.send(self.loop, {"t": "ack", "id": pid, "dup": True})
                return
            self.recent_ids.append(pid)
            self.stats["received"] += 1
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            if self.paused or not self.cfg.get("auto_inject"):
                self.pending.append({"id": pid, "text": text, "ts": time.time()})
                self._save_state()
                self.add_log(f"暂存 {len(text)} 字（暂停中）：{text[:40]}")
                client.send(self.loop, {"t": "ack", "id": pid, "queued": True,
                                        "pending": len(self.pending)})
            else:
                self.inject_q.put((pid, text, False))
                client.send(self.loop, {"t": "ack", "id": pid, "queued": False})
                self.add_log(f"收到 {len(text)} 字：{text[:40]}")
            if self.panel:
                self.panel.on_change()
            return
        if t == "cmd":
            action = msg.get("action")
            if action == "pause":
                self.set_paused(True)
            elif action == "resume":
                self.set_paused(False)
            elif action == "undo":
                self.undo()
            client.send(self.loop, self._status_obj())

    def set_paused(self, value):
        self.paused = bool(value)
        self.add_log("已暂停注入" if self.paused else "已恢复注入")
        self._broadcast(self._status_obj())
        if self.panel:
            self.panel.on_change()

    def flush_pending(self):
        if not self.pending:
            return 0
        items = self.pending
        self.pending = []
        self._save_state()
        for it in items:
            self.inject_q.put((it["id"], it["text"], True))
        self.add_log(f"注入暂存 {len(items)} 条")
        self._broadcast(self._status_obj())
        return len(items)

    def undo(self):
        if not self.last_injected:
            self.add_log("没有可撤销的内容")
            return
        if self.injector.is_foreground_ours():
            self.injector.activate_last_foreign(0.2)
        n = len(self.last_injected)
        self.injector.backspaces(n)
        self.add_log(f"已撤销上一句（{n} 字）")
        self.last_injected = ""
        self._broadcast({"t": "undo", "n": n})

    # ---- HTTP / WS 入口 -------------------------------------------------
    async def handle_conn(self, reader, writer):
        peer = writer.get_extra_info("peername")
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=15)
            if not raw:
                return
            try:
                method, target, _ = raw.decode("latin1").split(" ", 2)
            except ValueError:
                return
            headers = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=15)
                if not line or line in (b"\r\n", b"\n"):
                    break
                k, _, v = line.decode("latin1").partition(":")
                headers[k.strip().lower()] = v.strip()

            parts = urlsplit(target)
            path = unquote(parts.path)
            qs = parse_qs(parts.query)

            if path == "/ws":
                token = (qs.get("k") or [""])[0]
                if token != self.cfg["token"]:
                    await self._http(writer, 403, "text/plain; charset=utf-8", b"bad token")
                    return
                if headers.get("upgrade", "").lower() != "websocket":
                    await self._http(writer, 400, "text/plain; charset=utf-8", b"not websocket")
                    return
                await self._serve_ws(reader, writer, headers)
                return

            if path == "/health":
                body = json.dumps({"ok": True, "app": "typebyvoice", "version": VERSION,
                                   "host": HOST_NAME, "port": self.cfg["port"],
                                   "clients": len(self.clients)}, ensure_ascii=False).encode()
                await self._http(writer, 200, "application/json; charset=utf-8", body)
                return

            if path == "/status":
                body = json.dumps({
                    "ok": True, "app": "typebyvoice", "version": VERSION,
                    "port": self.cfg["port"],
                    "clients": len(self.clients),
                    "client_names": [c.name for c in self.clients.values()],
                    "paused": self.paused,
                    "pending": len(self.pending),
                    "received": self.stats["received"],
                    "injected": self.stats["injected"],
                    "watchdog": watchdog_alive(),
                    "firewall": getattr(self, "fw_state", "unknown"),
                    "log": list(self.log_lines)[-8:],
                }, ensure_ascii=False).encode()
                await self._http(writer, 200, "application/json; charset=utf-8", body, cache=False)
                return

            if path == "/show":
                self.show_request = True
                body = json.dumps({"ok": self.panel is not None, "app": "typebyvoice",
                                   "showing": self.panel is not None}).encode()
                await self._http(writer, 200, "application/json; charset=utf-8", body, cache=False)
                return

            if path == "/qr.png":
                token = (qs.get("k") or [""])[0]
                if token != self.cfg["token"]:
                    await self._http(writer, 403, "text/plain; charset=utf-8", b"bad token")
                    return
                png = self._qr_png(self.url)
                if png is None:
                    await self._http(writer, 503, "text/plain; charset=utf-8", b"qrcode not installed")
                else:
                    await self._http(writer, 200, "image/png", png, cache=False)
                return

            if method not in ("GET", "HEAD"):
                await self._http(writer, 405, "text/plain; charset=utf-8", b"method not allowed")
                return

            if path in ("/", ""):
                path = "/index.html"
            name = os.path.basename(path)
            allowed = {
                "index.html": "text/html; charset=utf-8",
                "manifest.webmanifest": "application/manifest+json; charset=utf-8",
                "icon-192.png": "image/png",
                "icon-512.png": "image/png",
                "apple-touch-icon.png": "image/png",
            }
            if name not in allowed:
                await self._http(writer, 404, "text/plain; charset=utf-8", b"not found")
                return
            fp = os.path.join(WEB_DIR, name)
            if not os.path.isfile(fp):
                await self._http(writer, 404, "text/plain; charset=utf-8", b"not found")
                return
            with open(fp, "rb") as f:
                body = f.read()
            await self._http(writer, 200, allowed[name], body, cache=name not in ("index.html",))
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError):
            pass
        except Exception as exc:  # noqa: BLE001
            log("conn error:", repr(exc))
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _http(self, writer, code, ctype, body, cache=True):
        reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
                  405: "Method Not Allowed", 503: "Service Unavailable"}.get(code, "OK")
        head = (
            f"HTTP/1.1 {code} {reason}\r\n"
            f"Content-Type: {ctype}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Cache-Control: {'max-age=3600' if cache else 'no-store'}\r\n"
            "X-Content-Type-Options: nosniff\r\n"
            "Connection: close\r\n\r\n"
        )
        writer.write(head.encode("latin1") + body)
        await writer.drain()

    def _qr_png(self, text):
        try:
            import qrcode
        except Exception:
            return None
        try:
            img = qrcode.make(text, box_size=8, border=2)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        except Exception as exc:  # noqa: BLE001
            log("qr error:", exc)
            return None

    async def _serve_ws(self, reader, writer, headers):
        key = headers.get("sec-websocket-key")
        if not key:
            return
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        writer.write((
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode())
        await writer.drain()

        self.client_seq += 1
        client = Client(self.client_seq, writer)
        self.clients[client.cid] = client
        wtask = asyncio.create_task(client.writer_loop())
        client.send(self.loop, {"t": "hello", "server": "typebyvoice",
                                "version": VERSION, "paused": self.paused,
                                "pending": len(self.pending)})
        self._broadcast(self._status_obj())
        if self.panel:
            self.panel.on_change()

        buf = b""
        msg_op = None
        try:
            while True:
                fin, opcode, payload = await ws_read_frame(reader)
                if opcode == 0x8:  # close
                    break
                if opcode == 0x9:  # ping
                    pong = struct.pack("BB", 0x8A, len(payload)) + payload
                    writer.write(pong)
                    await writer.drain()
                    continue
                if opcode == 0xA:
                    continue
                if opcode in (0x1, 0x2):
                    buf = payload
                    msg_op = opcode
                elif opcode == 0x0:
                    buf += payload
                if fin:
                    if msg_op == 0x1:
                        self.handle_message(client, buf.decode("utf-8", "replace"))
                    buf = b""
                    msg_op = None
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        except Exception as exc:  # noqa: BLE001
            log("ws error:", repr(exc))
        finally:
            client.alive = False
            self.clients.pop(client.cid, None)
            try:
                client.q.put_nowait(None)
            except Exception:
                pass
            wtask.cancel()
            self.add_log(f"手机断开：{client.name}")
            self._broadcast(self._status_obj())
            if self.panel:
                self.panel.on_change()

    # ---- 生命周期 -------------------------------------------------------
    async def serve(self):
        self.loop = asyncio.get_running_loop()
        self.url = f"http://{lan_ip()}:{self.cfg['port']}/?k={self.cfg['token']}"
        server = await asyncio.start_server(self.handle_conn, "0.0.0.0", self.cfg["port"])
        log(f"服务已启动：{self.url}")
        async with server:
            await server.serve_forever()

    def run_loop_thread(self):
        self.start_error = None

        def runner():
            try:
                asyncio.run(self.serve())
            except BaseException as exc:  # noqa: BLE001
                self.start_error = exc
                log("服务线程结束：", repr(exc))

        t = threading.Thread(target=runner, name="server", daemon=True)
        t.start()
        return t


# --------------------------------------------------------------------------
# 面板
# --------------------------------------------------------------------------
class Panel:
    def __init__(self, bridge: Bridge):
        import tkinter as tk
        from tkinter import ttk
        self.tk = tk
        self.ttk = ttk
        self.bridge = bridge
        self.root = tk.Tk()
        self.root.title("TypeByVoice · 常驻桥")
        self.root.geometry("560x780")
        self.root.minsize(520, 660)
        self.qr_photo = None
        self._last_sig = None
        self._fw_result = None
        self._fw_applied = None
        self._set_icon()
        self._build()

    def _set_icon(self):
        try:
            ico = os.path.join(WEB_DIR, "icon-192.png")
            if os.path.isfile(ico):
                self._icon_img = self.tk.PhotoImage(file=ico)
                self.root.iconphoto(True, self._icon_img)
        except Exception:  # noqa: BLE001
            pass

    def _build(self):
        tk, ttk = self.tk, self.ttk
        pad = {"padx": 14}
        top = ttk.Frame(self.root)
        top.pack(fill="x", pady=(12, 4), **pad)
        self.status_var = tk.StringVar(value="启动中…")
        self.status_lbl = tk.Label(top, textvariable=self.status_var, anchor="w",
                                   font=("Microsoft YaHei UI", 12, "bold"), fg="#0a7d33")
        self.status_lbl.pack(side="left")
        self.count_var = tk.StringVar(value="")
        tk.Label(top, textvariable=self.count_var, anchor="e",
                 font=("Microsoft YaHei UI", 9), fg="#666").pack(side="right")

        ttk.Label(self.root, text="手机打开下面这个地址（建议添加到主屏幕，以后点图标即用）：",
                  wraplength=520, justify="left").pack(anchor="w", pady=(6, 2), **pad)
        urlrow = ttk.Frame(self.root)
        urlrow.pack(fill="x", **pad)
        self.url_var = tk.StringVar(value="")
        ent = ttk.Entry(urlrow, textvariable=self.url_var, state="readonly", font=("Consolas", 10))
        ent.pack(side="left", fill="x", expand=True)
        ttk.Button(urlrow, text="复制", width=6, command=self.copy_url).pack(side="left", padx=(6, 0))
        ttk.Button(urlrow, text="打开", width=6, command=self.open_url).pack(side="left", padx=(6, 0))

        qrwrap = ttk.Frame(self.root)
        qrwrap.pack(pady=10)
        self.qr_lbl = tk.Label(qrwrap, text="（安装 qrcode 后显示二维码）", width=28, height=10,
                               relief="groove", bg="#fafafa")
        self.qr_lbl.pack()

        hint = ("用法：手机点开图标 → 输入框自动聚焦 → 用手机输入法的语音键说话 →\n"
                "停顿约 1 秒，文字自动出现在电脑当前光标处。")
        ttk.Label(self.root, text=hint, justify="left", foreground="#444",
                  font=("Microsoft YaHei UI", 9)).pack(anchor="w", **pad)

        btns = ttk.Frame(self.root)
        btns.pack(fill="x", pady=8, **pad)
        self.pause_btn = ttk.Button(btns, text="暂停注入", command=self.toggle_pause)
        self.pause_btn.pack(side="left")
        ttk.Button(btns, text="撤销上一句", command=self.bridge.undo).pack(side="left", padx=8)
        self.flush_btn = ttk.Button(btns, text="注入暂存", command=self.flush)
        self.flush_btn.pack(side="left")
        ttk.Button(btns, text="测试注入(3秒)", command=self.test_inject).pack(side="left", padx=8)

        opt = ttk.Frame(self.root)
        opt.pack(fill="x", **pad)
        self.paste_var = tk.BooleanVar(value=bool(self.bridge.cfg.get("paste_mode")))
        ttk.Checkbutton(opt, text="兼容模式（剪贴板粘贴，某些程序键盘注入无效时用）",
                        variable=self.paste_var, command=self.toggle_paste).pack(anchor="w")
        self.autostart_var = tk.BooleanVar(value=self._autostart_enabled())
        ttk.Checkbutton(opt, text="开机自动启动", variable=self.autostart_var,
                        command=self.toggle_autostart).pack(anchor="w")

        fwrow = ttk.Frame(opt)
        fwrow.pack(anchor="w", pady=(4, 0))
        ttk.Button(fwrow, text="防火墙放行", command=self.open_firewall).pack(side="left")
        self.fw_lbl = tk.Label(fwrow, text="", font=("Microsoft YaHei UI", 8), fg="#777")
        self.fw_lbl.pack(side="left", padx=8)

        ttk.Label(self.root, text="最近记录：").pack(anchor="w", pady=(8, 0), **pad)
        self.logbox = tk.Text(self.root, height=7, wrap="word", state="disabled",
                              font=("Microsoft YaHei UI", 9), bg="#fbfbfb")
        self.logbox.pack(fill="both", expand=True, **pad)

        foot = ttk.Frame(self.root)
        foot.pack(fill="x", pady=8, **pad)
        self.hotkey_lbl = tk.Label(foot, text="热键：Ctrl+Alt+P 暂停 / Ctrl+Alt+Z 撤销 / Ctrl+Alt+B 唤出",
                                   font=("Microsoft YaHei UI", 8), fg="#777")
        self.hotkey_lbl.pack(side="left")
        self.watch_var = self.tk.StringVar(value="")
        self.tk.Label(foot, textvariable=self.watch_var, font=("Microsoft YaHei UI", 8),
                      fg="#777").pack(side="left", padx=10)
        ttk.Button(foot, text="退出", command=self.quit).pack(side="right")

        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.root.after(300, self.refresh)
        self.root.after(800, self._fw_tick)

    # ---- 动作 -----------------------------------------------------------
    def copy_url(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.bridge.url)
        self.status_var.set("地址已复制")
        self.root.after(1500, self.refresh)

    def open_url(self):
        webbrowser.open(self.bridge.url)

    def toggle_pause(self):
        self.bridge.set_paused(not self.bridge.paused)

    def flush(self):
        n = self.bridge.flush_pending()
        self.status_var.set(f"已注入暂存 {n} 条" if n else "没有暂存")

    def toggle_paste(self):
        self.bridge.cfg["paste_mode"] = bool(self.paste_var.get())
        save_config(self.bridge.cfg)

    def test_inject(self):
        text = "这是一段来自跨屏桥的测试文字，如果你看到它，说明注入链路是通的。"
        self.status_var.set("3 秒后注入测试文字，请切到目标窗口…")

        def fire():
            inj = self.bridge.injector
            if inj.is_foreground_ours():
                inj.activate_last_foreign(0.3)
            ok, how = inj.type_text(text, paste=bool(self.bridge.cfg.get("paste_mode")))
            self.bridge.add_log(f"测试注入：{'成功' if ok else '失败'}（{how}）")
            self.refresh()

        self.root.after(3000, fire)

    def hide(self):
        self.root.iconify()

    def _ensure_onscreen(self):
        """窗口最小化到屏幕外（坐标 -32000 或越界）时挪回可见区域。"""
        try:
            self.root.update_idletasks()
            x, y = self.root.winfo_x(), self.root.winfo_y()
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            w = max(self.root.winfo_width(), 560)
            h = max(self.root.winfo_height(), 660)
            off = (x < -1000 or y < -1000
                   or x + 120 > sw or y + 60 > sh or x + w < 120 or y + 40 < 0)
            if off:
                nx = max(40, min(sw - w - 40, (sw - w) // 2))
                ny = max(30, min(sh - h - 60, (sh - h) // 3))
                self.root.geometry(f"{w}x{h}+{nx}+{ny}")
        except Exception:  # noqa: BLE001
            pass

    def show(self):
        self._ensure_onscreen()
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        try:
            self.root.attributes("-topmost", True)
        except Exception:  # noqa: BLE001
            pass
        try:
            hwnd = int(self.root.wm_frame(), 16)
            self.bridge.injector.force_foreground(hwnd)
            _switch_to_window(hwnd)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.root.focus_force()
        except Exception:  # noqa: BLE001
            pass
        # 保持置顶一小段时间，确保它真的盖到别的窗口上面；随后恢复普通层级
        self.root.after(1500, lambda: self.root.attributes("-topmost", False))

    def quit(self):
        try:
            with open(STOP_FLAG, "w", encoding="utf-8") as f:
                f.write("stop")
        except Exception:
            pass
        self.root.destroy()

    # ---- 自动启动 -------------------------------------------------------
    def _startup_dir(self):
        return os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup")

    def _shortcut_path(self):
        return os.path.join(self._startup_dir(), "TypeByVoice.lnk")

    def _autostart_enabled(self):
        return os.path.exists(self._shortcut_path())

    def toggle_autostart(self):
        path = self._shortcut_path()
        if self.autostart_var.get():
            try:
                self._create_shortcut(path)
                self.status_var.set("已设置开机自启")
            except Exception as exc:  # noqa: BLE001
                self.status_var.set(f"设置失败：{exc}")
        else:
            try:
                if os.path.exists(path):
                    os.remove(path)
                self.status_var.set("已取消开机自启")
            except Exception as exc:  # noqa: BLE001
                self.status_var.set(f"取消失败：{exc}")

    def _create_shortcut(self, path):
        target = os.path.join(APP_DIR, "watchdog.vbs")
        if not os.path.exists(target):
            raise FileNotFoundError("watchdog.vbs 不存在")
        ps = (
            "$ws = New-Object -ComObject WScript.Shell; "
            f"$sc = $ws.CreateShortcut('{path}'); "
            f"$sc.TargetPath = 'wscript.exe'; "
            f"$sc.Arguments = '\"{target}\"'; "
            f"$sc.WorkingDirectory = '{APP_DIR}'; "
            "$sc.WindowStyle = 7; $sc.Save()"
        )
        import subprocess
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                       check=True, capture_output=True)

    # ---- 防火墙 ---------------------------------------------------------
    FIREWALL_RULE = "TypeByVoiceBridge"

    def _firewall_state(self):
        """一次查清三件事：端口规则是否生效、python 程序规则是否生效、当前网络类别。

        两种放行方式都要认：按端口加的规则（本程序按钮生成）和按程序加的规则
        （Windows 首次弹窗时你点的"允许"）。而且必须比对**当前网络类别**——
        只在"公用"上放行的规则，系统把 Wi-Fi 改判成"专用"后就完全不起作用
        （本机今天就踩了这个坑：Public 变 Private，手机直接连不上）。
        """
        exe = sys.executable.replace("'", "''")
        cond = ("$_.Direction -eq 'Inbound' -and $_.Action -eq 'Allow' -and $_.Enabled -eq 'True'"
                " -and ($_.Profile -eq 'Any' -or ($cat -and ([string]$_.Profile) -like \"*$cat*\"))")
        ps = (
            f"$p='{exe}'; "
            "$idx=(Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |"
            " Sort-Object RouteMetric | Select-Object -First 1).InterfaceIndex; "
            "$cat=''; if($idx){$cat=[string](Get-NetConnectionProfile -InterfaceIndex $idx"
            " -ErrorAction SilentlyContinue).NetworkCategory}; "
            "$pr=Get-NetFirewallRule -DisplayName 'TypeByVoiceBridge*' -ErrorAction SilentlyContinue |"
            f" Where-Object {{{cond}}}; "
            "$portOk=[bool]$pr; "
            "$f=Get-NetFirewallApplicationFilter -Program $p -ErrorAction SilentlyContinue; "
            "$progOk=$false; "
            f"if($f){{$progOk=[bool]($f | Get-NetFirewallRule -ErrorAction SilentlyContinue | Where-Object {{{cond}}})}}; "
            "\"$portOk;$progOk;$cat\""
        )
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, timeout=30,
            ).stdout.decode("utf-8", "replace").strip()
        except Exception as exc:  # noqa: BLE001
            return False, False, f"error:{exc!r}"
        parts = (out.split(";") + ["", "", ""])[:3]
        return parts[0] == "True", parts[1] == "True", parts[2]

    def _named_rule_exists(self, name) -> bool:
        ps = f"[bool](Get-NetFirewallRule -DisplayName '{name}' -ErrorAction SilentlyContinue)"
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, timeout=20,
            ).stdout.decode("utf-8", "replace").strip()
            return out == "True"
        except Exception:  # noqa: BLE001
            return False

    def _fw_tick(self):
        """周期性复查防火墙：换 Wi-Fi、系统把网络从“公用”改成“专用”都会让规则失效。"""
        try:
            self._check_firewall()
        finally:
            self.root.after(30000, self._fw_tick)

    def _check_firewall(self):
        """后台线程跑 PowerShell（1~3 秒），结果由 refresh() 应用到界面，避免卡住窗口。"""
        def work():
            try:
                port = int(self.bridge.cfg["port"])
                port_ok, prog_ok, cat = self._firewall_state()
                self._fw_result = (port, port_ok, prog_ok, cat)
            except Exception as exc:  # noqa: BLE001
                self._fw_result = None
                self.bridge.fw_state = f"error:{exc!r}"
        threading.Thread(target=work, daemon=True).start()

    def _apply_firewall(self, res):
        port, port_ok, prog_ok, cat = res
        label = cat or "当前网络"
        if port_ok:
            self.bridge.fw_state = f"port-rule[{label}]"
            self.fw_lbl.config(text=f"已放行 {port}（{label}）", fg="#0a7d33")
        elif prog_ok:
            self.bridge.fw_state = f"program-rule[{label}]"
            self.fw_lbl.config(text=f"已放行 {port}（python 规则 · {label}）", fg="#0a7d33")
        else:
            self.bridge.fw_state = f"none[{label}]"
            self.fw_lbl.config(text=f"未放行 {port}（{label} 下没规则，手机连不上就点这里）", fg="#b45309")

    def open_firewall(self):
        port = int(self.bridge.cfg["port"])
        name = self.FIREWALL_RULE if not self._named_rule_exists(self.FIREWALL_RULE) \
            else self.FIREWALL_RULE + "Any"
        ps = (
            "Start-Process -FilePath netsh -Verb RunAs -ArgumentList "
            f"'advfirewall firewall add rule name={name} dir=in action=allow "
            f"protocol=TCP localport={port} profile=any'"
        )
        try:
            subprocess.Popen(["powershell", "-NoProfile", "-Command", ps],
                             creationflags=0x08000000)  # CREATE_NO_WINDOW
            self.fw_lbl.config(text="已弹出授权窗口，点“是”即可", fg="#777")
        except Exception as exc:  # noqa: BLE001
            self.fw_lbl.config(text=f"失败：{exc}", fg="#b91c1c")
        self.root.after(5000, self._check_firewall)

    # ---- 刷新 -----------------------------------------------------------
    def on_change(self):
        self._last_sig = None

    def refresh(self):
        b = self.bridge
        if getattr(b, "show_request", False):
            b.show_request = False
            try:
                self.show()
            except Exception as exc:  # noqa: BLE001
                b.add_log(f"唤出面板失败：{exc}")
        sig = (len(b.clients), b.paused, len(b.pending), b.stats["received"],
               b.stats["injected"], b.url, len(b.log_lines))
        if sig != self._last_sig:
            self._last_sig = sig
            if b.clients:
                names = "、".join(c.name for c in b.clients.values())
                self.status_var.set(f"已连接：{names}")
                self.status_lbl.config(fg="#0a7d33")
            else:
                self.status_var.set("等待手机连接…")
                self.status_lbl.config(fg="#b35c00")
            self.count_var.set(f"收 {b.stats['received']} / 注入 {b.stats['injected']}"
                               + (f" / 暂存 {len(b.pending)}" if b.pending else ""))
            self.url_var.set(b.url)
            self.pause_btn.config(text="恢复注入" if b.paused else "暂停注入")
            self.flush_btn.config(text=f"注入暂存({len(b.pending)})" if b.pending else "注入暂存")
            if b.url:
                self._refresh_qr(b.url)
            self._refresh_log()
        res = getattr(self, "_fw_result", None)
        if res is not None and res != getattr(self, "_fw_applied", None):
            self._fw_applied = res
            self._apply_firewall(res)
        self.watch_var.set("守护：运行中" if watchdog_alive() else "守护：未启动")
        self.root.after(400, self.refresh)

    def _refresh_qr(self, url):
        if self.qr_photo and getattr(self, "_qr_url", None) == url:
            return
        try:
            import qrcode
            from PIL import ImageTk
        except Exception:
            return
        try:
            img = qrcode.make(url, box_size=6, border=2).convert("RGB")
            img = img.resize((220, 220))
            self.qr_photo = ImageTk.PhotoImage(img)
            self.qr_lbl.config(image=self.qr_photo, text="", width=220, height=220)
            self._qr_url = url
        except Exception:
            pass

    def _refresh_log(self):
        lines = list(self.bridge.log_lines)[-12:]
        self.logbox.config(state="normal")
        self.logbox.delete("1.0", "end")
        self.logbox.insert("1.0", "\n".join(lines))
        self.logbox.see("end")
        self.logbox.config(state="disabled")

    def run(self):
        self.root.mainloop()


# --------------------------------------------------------------------------
# 全局热键
# --------------------------------------------------------------------------
def start_hotkeys(bridge: Bridge):
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x0001, 0x0002, 0x4000
    WM_HOTKEY = 0x0312

    def worker():
        hk = {
            1: (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord("P")),
            2: (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord("Z")),
            3: (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord("B")),
        }
        for hid, (mods, vk) in hk.items():
            if not user32.RegisterHotKey(None, hid, mods, vk):
                log(f"热键注册失败：{hid}")
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_HOTKEY:
                hid = int(msg.wParam)
                if hid == 1:
                    bridge.set_paused(not bridge.paused)
                elif hid == 2:
                    bridge.undo()
                elif hid == 3 and bridge.panel:
                    bridge.panel.root.after(0, bridge.panel.show)

    threading.Thread(target=worker, name="hotkeys", daemon=True).start()


# --------------------------------------------------------------------------
def probe_existing(port, timeout=1.5) -> bool:
    """端口上是否已经跑着一个健康的本程序实例。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return data.get("app") == "typebyvoice"
    except Exception:  # noqa: BLE001
        return False


def request_show(port, timeout=1.5) -> bool:
    """请已经在运行的实例把面板唤出（给快捷方式用）。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/show", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return bool(data.get("ok"))
    except Exception:  # noqa: BLE001
        return False


def _switch_to_window(hwnd) -> bool:
    """把窗口翻到最前；Windows 的前台锁会让 SetForegroundWindow 静默失败。"""
    if sys.platform != "win32":
        return False
    try:
        user32.SwitchToThisWindow(hwnd, True)
        return True
    except Exception:  # noqa: BLE001
        return False


WATCHDOG_TPL = """' TypeByVoice - watchdog (auto-generated by bridge.py)
' Restarts bridge.py when its heartbeat file (bridge.alive) goes stale.
' Exits for good when stopped.flag exists (退出 button / stop.bat).
' NOTE: launching python.exe directly from WSH pops a Windows Terminal window
'       (it steals focus), so we always go through launch.exe (no console).
Option Explicit
Dim sh, fso, base, py, q, cmd, age, f, cool
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)
py   = "{py}"
q    = Chr(34)
cmd  = q & base & "\\launch.exe" & q & " " & q & base & q & " " & q & py & q & " " & q & base & "\\bridge.py" & q & " " & q & "--silent" & q
cool = 0
Do
  If fso.FileExists(base & "\\stopped.flag") Then Exit Do
  ' 心跳始终每 3 秒刷新一次：新实例靠它判断"已有守护在跑"，不会重复拉起守护
  On Error Resume Next
  fso.CreateTextFile(base & "\\watchdog.alive", True).Close
  Err.Clear
  On Error GoTo 0
  age = 9999
  If fso.FileExists(base & "\\bridge.alive") Then
    Set f = fso.GetFile(base & "\\bridge.alive")
    age = DateDiff("s", f.DateLastModified, Now)
  End If
  If age > 15 And cool <= 0 Then
    sh.Run cmd, 0, False
    cool = 6            ' 6 * 3s = 18s 冷却，避免连环拉起
  End If
  If cool > 0 Then cool = cool - 1
  WScript.Sleep 3000
Loop
"""


def ensure_watchdog(port) -> str:
    """生成/更新 watchdog.vbs，返回路径。

    用绝对 python.exe 路径（不用 pythonw）：本机防火墙对 pythonw.exe 有过入站 Block 规则，
    而对 python.exe 是 Allow，用 python.exe 才能保证手机连得上；
    再由 launch.exe 以 CREATE_NO_WINDOW 启动，避免每次拉起都闪一个黑窗。
    """
    py = sys.executable
    cand = os.path.join(os.path.dirname(sys.executable), "python.exe")
    if os.path.isfile(cand):
        py = cand
    if not os.path.isfile(LAUNCHER):
        log("警告：缺少 launch.exe，守护拉起时会闪控制台窗口（可用 tools/launch.cs 重新编译）")
    content = WATCHDOG_TPL.format(py=py.replace('"', '""'))
    path = os.path.join(APP_DIR, "watchdog.vbs")
    try:
        # WSH 按系统 ANSI 代码页读 .vbs，用 mbcs 写才能兼容中文路径
        with open(path, "w", encoding="mbcs", errors="replace", newline="\r\n") as f:
            f.write(content)
    except OSError as exc:
        log("watchdog.vbs 写入失败：", exc)
    return path


def _watchdog_mtime() -> float:
    try:
        return os.path.getmtime(os.path.join(APP_DIR, "watchdog.alive"))
    except OSError:
        return 0.0


def _touch_alive() -> None:
    try:
        with open(ALIVE_PATH, "w", encoding="ascii") as f:
            f.write(str(int(time.time())))
    except OSError:
        pass


def start_heartbeat() -> None:
    """每 2 秒刷新 bridge.alive；守护脚本据此判断服务是否还活着。

    比 netstat 文本解析可靠：不受系统语言/编码影响，也不产生任何子进程窗口。
    """
    _touch_alive()

    def beat():
        while True:
            _touch_alive()
            time.sleep(2.0)

    threading.Thread(target=beat, name="heartbeat", daemon=True).start()


def watchdog_alive(window: float = 10.0) -> bool:
    """守护脚本每轮刷新 watchdog.alive；10 秒内有心跳即视为在跑。"""
    m = _watchdog_mtime()
    return m > 0 and (time.time() - m) < window


def _watchdog_worker(path: str) -> None:
    m0 = _watchdog_mtime()
    if watchdog_alive():
        time.sleep(5.5)          # 真正在跑的守护会在这期间刷新心跳
        if _watchdog_mtime() > m0:
            log("守护已在运行")
            return
    wscript = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "wscript.exe")
    if not os.path.isfile(wscript):
        wscript = "wscript.exe"
    try:
        subprocess.Popen(
            [wscript, path],
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0),
            close_fds=True,
        )
        log("守护已启动：崩溃后约 4 秒自动拉起")
    except OSError as exc:
        log("守护启动失败：", exc)


def start_watchdog(path: str) -> None:
    """后台确认并拉起守护脚本（不阻塞界面启动）。"""
    threading.Thread(target=_watchdog_worker, args=(path,), daemon=True).start()


def main():
    ap = argparse.ArgumentParser(description="TypeByVoice 常驻桥")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-gui", action="store_true")
    ap.add_argument("--minimized", action="store_true")
    ap.add_argument("--silent", action="store_true", help="启动时不显示面板（守护拉起时用）")
    ap.add_argument("--paused", action="store_true", help="启动时暂停注入（测试用）")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    cfg = load_config()
    if args.port:
        cfg["port"] = args.port
    bridge = Bridge(cfg)
    bridge.paused = args.paused
    bridge.start_injector()
    start_heartbeat()
    if os.path.exists(STOP_FLAG):
        try:
            os.remove(STOP_FLAG)
        except OSError:
            pass
    bridge.run_loop_thread()
    time.sleep(0.8)

    err = getattr(bridge, "start_error", None)
    if err is not None:
        if probe_existing(cfg["port"]):
            if request_show(cfg["port"]):
                log(f"已有实例在运行，已唤出它的面板（端口 {cfg['port']}）。")
            else:
                log(f"本程序已在运行（端口 {cfg['port']}），本次启动退出。")
            return
        msg = f"服务启动失败（端口 {cfg['port']}）：{err}"
        log(msg)
        if not args.no_gui:
            try:
                import tkinter as _tk
                from tkinter import messagebox as _mb
                _r = _tk.Tk()
                _r.withdraw()
                _mb.showerror("TypeByVoice", msg + "\n\n可以改 config.json 里的 port 换一个端口。")
                _r.destroy()
            except Exception:  # noqa: BLE001
                pass
        return

    if not os.environ.get("XNB_NO_WATCHDOG"):
        wd = ensure_watchdog(cfg["port"])
        start_watchdog(wd)

    if cfg.get("hotkeys") and not args.no_gui:
        start_hotkeys(bridge)

    if args.no_gui:
        log("headless 模式，Ctrl+C 退出")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return

    panel = Panel(bridge)
    bridge.panel = panel
    if args.silent:
        panel.root.withdraw()   # 连任务栏都不出现，Ctrl+Alt+B 唤出
    elif args.minimized:
        panel.root.iconify()
    panel.run()


if __name__ == "__main__":
    main()
