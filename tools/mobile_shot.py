"""用手机尺寸（390x844, DPR 2）截手机页，供肉眼验收排版。

用法：python tools/mobile_shot.py [url] [out.png]
依赖：playwright（已装）+ 本机 Chrome（channel="chrome"）
"""
import json
import os
import sys

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
# 令牌从 config.json 读，别写死（写死就会 403，截出来的页面永远是"未连接"）
TOKEN = "selftest-token"
try:
    with open(os.path.join(os.path.dirname(HERE), "config.json"), encoding="utf-8") as f:
        TOKEN = json.load(f).get("token") or TOKEN
except Exception:  # noqa: BLE001
    pass

URL = sys.argv[1] if len(sys.argv) > 1 else f"http://127.0.0.1:8765/?k={TOKEN}"
OUT = sys.argv[2] if len(sys.argv) > 2 else "tools/phone.png"

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=True)
    page = browser.new_page(
        viewport={"width": 390, "height": 844},
        device_scale_factor=2,
        is_mobile=True,
        has_touch=True,
        locale="zh-CN",
    )
    errors = []
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on(
        "console",
        lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None,
    )
    page.goto(URL, wait_until="load")
    page.wait_for_timeout(900)  # 等 WebSocket 连上、状态点亮
    info = page.evaluate(
        "() => ({w: document.documentElement.clientWidth,"
        " sw: document.documentElement.scrollWidth,"
        " sh: document.documentElement.scrollHeight,"
        " status: (document.querySelector('#status')||{}).textContent})"
    )
    print(info)
    print("errors:", errors or "none")
    page.screenshot(path=OUT)
    browser.close()
print("saved", OUT)
