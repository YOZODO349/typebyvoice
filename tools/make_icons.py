"""生成 PWA 图标（192/512/apple-touch）。"""
import os
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, "web")

BG = (14, 17, 22, 255)
ACCENT = (53, 194, 160, 255)
ACCENT2 = (35, 156, 128, 255)
WHITE = (233, 237, 244, 255)


def draw(size):
    s = 4  # 超采样
    w = size * s
    img = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = int(w * 0.22)
    d.rounded_rectangle([0, 0, w - 1, w - 1], radius=r, fill=BG)
    d.rounded_rectangle([int(w * 0.04), int(w * 0.04), int(w * 0.96), int(w * 0.96)],
                        radius=int(r * 0.9), outline=(30, 38, 50, 255), width=max(2, int(w * 0.006)))

    cx = w // 2
    # 话筒头
    mw, mh = int(w * 0.20), int(w * 0.34)
    top = int(w * 0.22)
    d.rounded_rectangle([cx - mw // 2, top, cx + mw // 2, top + mh],
                        radius=mw // 2, fill=WHITE)
    # 话筒托架
    arc_w = int(w * 0.30)
    arc_box = [cx - arc_w // 2, top + int(mh * 0.55), cx + arc_w // 2, top + mh + int(w * 0.14)]
    d.arc(arc_box, start=20, end=160, fill=ACCENT, width=max(3, int(w * 0.032)))
    # 支架
    d.rounded_rectangle([cx - int(w * 0.016), top + mh + int(w * 0.10),
                         cx + int(w * 0.016), top + mh + int(w * 0.19)],
                        radius=int(w * 0.016), fill=ACCENT)
    # 底座
    d.rounded_rectangle([cx - int(w * 0.09), top + mh + int(w * 0.17),
                         cx + int(w * 0.09), top + mh + int(w * 0.21)],
                        radius=int(w * 0.02), fill=ACCENT2)
    # 两侧声波（左右对称）
    cy = int(w * 0.40)
    for i, rr in enumerate((int(w * 0.20), int(w * 0.26))):
        bw = max(3, int(w * 0.018) - i)
        d.arc([cx - rr, cy - rr, cx + rr, cy + rr],
              start=150 - i * 8, end=210 + i * 8, fill=ACCENT, width=bw)
        d.arc([cx - rr, cy - rr, cx + rr, cy + rr],
              start=-30 + i * 8, end=30 - i * 8, fill=ACCENT, width=bw)
    return img.resize((size, size), Image.LANCZOS)


for name, size in (("icon-512.png", 512), ("icon-192.png", 192), ("apple-touch-icon.png", 180)):
    draw(size).save(os.path.join(OUT, name))
    print("wrote", name)
