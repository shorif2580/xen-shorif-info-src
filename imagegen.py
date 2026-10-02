"""
🖼️ In-house image generator (Flash API ছাড়া) — Banner & Outfit
আইটেম আইকন CDN থেকে নিয়ে Pillow দিয়ে ছবি বানায়।
"""
import io
import os
import math
import unicodedata
from concurrent.futures import ThreadPoolExecutor

import requests
from PIL import Image, ImageDraw, ImageFont, ImageFilter

ICON_SOURCES = [
    "https://cdn.jsdelivr.net/gh/ShahGCreator/icon@main/PNG/{id}.png",
    "https://cdn.jsdelivr.net/gh/0xMe/ff-resources@main/pngs/300x300/{id}.png",
    "https://0xme.github.io/ff-resources/pngs/300x300/{id}.png",
]
_ICON_CACHE = {}
_POOL = ThreadPoolExecutor(max_workers=10)

FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
# প্রতিটি অক্ষরের জন্য প্রথম যে ফন্টে গ্লিফ আছে সেটা ব্যবহার হয় (ফলব্যাক চেইন)
FONT_FILES = ["DejaVuSans-Bold.ttf", "FreeSerifBold.ttf", "FreeSerif.ttf", "unifont.otf", "unifont_upper.otf"]
# অদৃশ্য/ফাঁকা অক্ষর (গেমের নামে ফাঁক বোঝাতে ব্যবহার হয়) — বক্স না দেখিয়ে স্পেস ধরা হবে
BLANKS = {0x3164, 0x2800, 0x115F, 0x1160, 0xFFA0, 0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00A0, 0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200A, 0x202F, 0x205F, 0x3000}

_FONT_CMAPS = {}
_FONT_OBJS = {}

_FONT_ERR = []

def _load_cmaps():
    if _FONT_CMAPS: return
    try:
        from fontTools.ttLib import TTFont
    except Exception as e:
        _FONT_ERR.append(f"fonttools import failed: {e}"); return
    for fn in FONT_FILES:
        p = os.path.join(FONT_DIR, fn)
        if os.path.exists(p):
            try: _FONT_CMAPS[fn] = set(TTFont(p, lazy=True).getBestCmap().keys())
            except Exception as e: _FONT_ERR.append(f"{fn}: {e}")

def fonts_status():
    """ডিবাগ: ফন্ট ফোল্ডার/ফাইল/fonttools ঠিক আছে কি না"""
    _load_cmaps()
    return {"fonts_dir": FONT_DIR, "dir_exists": os.path.isdir(FONT_DIR),
            "loaded": sorted(_FONT_CMAPS), "missing_files": [f for f in FONT_FILES if not os.path.exists(os.path.join(FONT_DIR, f))],
            "errors": _FONT_ERR, "fallback_mode": not _FONT_CMAPS}

def _font_obj(fn, size):
    key = (fn, size)
    if key not in _FONT_OBJS:
        if len(_FONT_OBJS) > 200: _FONT_OBJS.clear()
        _FONT_OBJS[key] = ImageFont.truetype(os.path.join(FONT_DIR, fn), size)
    return _FONT_OBJS[key]

def pick_font(ch):
    _load_cmaps()
    cp = ord(ch)
    for fn in FONT_FILES:
        if cp in _FONT_CMAPS.get(fn, ()): return fn
    return None

def font(size):
    try: return _font_obj("DejaVuSans-Bold.ttf", size)
    except Exception:
        try: return ImageFont.load_default(size)
        except TypeError: return ImageFont.load_default()

def describe(text):
    """ডিবাগ: প্রতিটি অক্ষরের কোডপয়েন্ট + কোন ফন্টে আছে"""
    out = []
    for ch in str(text or ""):
        out.append({"char": ch, "code": f"U+{ord(ch):04X}", "blank": ord(ch) in BLANKS, "font": None if ord(ch) in BLANKS else pick_font(ch)})
    return out

def _runs(text, size):
    runs, x = [], 0.0
    for ch in str(text or ""):
        cp = ord(ch)
        if cp in BLANKS:
            runs.append((ch, None, x)); x += size * 0.36; continue
        fn = pick_font(ch)
        if not fn:
            if _FONT_CMAPS:
                continue  # ফন্ট আছে কিন্তু এই অক্ষরের গ্লিফ নেই — বক্স না এঁকে বাদ
            if cp > 127:
                continue  # ফন্ট ফাইলই নেই: শুধু ASCII আঁকা হবে (নাম পুরো গায়েব হবে না)
            try: f = ImageFont.load_default(size)
            except TypeError: f = ImageFont.load_default()
        else:
            f = _font_obj(fn, size)
        runs.append((ch, f, x)); x += f.getlength(ch)
    return runs, x

def draw_text(d, xy, text, size, fill, stroke=0, anchor="l", max_width=None):
    """baseline-এ টেক্সট আঁকে; anchor: l/m/r; max_width দিলে ফন্ট ছোট করে ফিট করে"""
    runs, width = _runs(text, size)
    while max_width and width > max_width and size > 22:
        size -= 2; runs, width = _runs(text, size)
    x0, y = xy
    if anchor == "m": x0 -= width / 2
    elif anchor == "r": x0 -= width
    for ch, f, dx in runs:
        if f is None: continue
        d.text((x0 + dx, y), ch, font=f, fill=fill, anchor="ls", stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
    return width

def fetch_icon(item_id):
    """আইটেম আইকন (RGBA) বা None"""
    if not item_id: return None
    key = str(item_id)
    if key in _ICON_CACHE: return _ICON_CACHE[key]
    img = None
    for tpl in ICON_SOURCES:
        try:
            r = requests.get(tpl.format(id=key), timeout=6)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                img = Image.open(io.BytesIO(r.content)).convert("RGBA"); break
        except Exception:
            continue
    if len(_ICON_CACHE) > 300: _ICON_CACHE.clear()
    _ICON_CACHE[key] = img
    return img

def fetch_many(ids):
    futs = {i: _POOL.submit(fetch_icon, i) for i in ids if i}
    return {i: f.result() for i, f in futs.items()}

def cover(img, w, h):
    s = max(w / img.width, h / img.height)
    im = img.resize((max(1, int(img.width * s)), max(1, int(img.height * s))), Image.LANCZOS)
    l, t = (im.width - w) // 2, (im.height - h) // 2
    return im.crop((l, t, l + w, t + h))

def to_png(img):
    b = io.BytesIO(); img.convert("RGB").save(b, "PNG", optimize=True); return b.getvalue()

def to_webp_sticker(img, width=512):
    """টেলিগ্রাম স্টিকারের জন্য: প্রস্থ ৫১২ px, WebP (ছবি কেটে যায় না)"""
    im = img.convert("RGBA")
    if im.width != width:
        im = im.resize((width, max(1, round(im.height * width / im.width))), Image.LANCZOS)
    b = io.BytesIO(); im.save(b, "WEBP", lossless=True, quality=100, method=6); return b.getvalue()

def encode(img, fmt):
    return to_webp_sticker(img) if fmt == "webp" else to_png(img)

# ---------------------------------------------------------------- BANNER
def banner_image(data, fmt="png"):
    b = data.get("basicInfo") or {}
    c = data.get("clanBasicInfo") or {}
    W, H = 1280, 275
    ic = fetch_many([b.get("bannerId"), b.get("headPic"), b.get("pinId")])
    bg_icon = ic.get(b.get("bannerId")) if b.get("bannerId") else None

    if bg_icon:
        canvas = cover(bg_icon, W, H).convert("RGBA")
    else:  # fallback gradient
        canvas = Image.new("RGBA", (W, H))
        px = canvas.load()
        for x in range(W):
            t = x / W
            for y in range(H):
                px[x, y] = (int(40 + 150 * t), int(10 + 15 * t), int(30 + 40 * t), 255)
    # বাঁ দিকে হালকা কালো ওভারলে — লেখা পড়ার সুবিধার জন্য
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0)); od = ImageDraw.Draw(ov)
    for x in range(W):
        od.line([(x, 0), (x, H)], fill=(0, 0, 0, int(120 * max(0, 1 - x / (W * 0.8)))))
    canvas = Image.alpha_composite(canvas, ov)
    d = ImageDraw.Draw(canvas)

    # অবতার
    av = ic.get(b.get("headPic")) if b.get("headPic") else None
    if av:
        canvas.paste(cover(av, H, H), (0, 0))
    else:
        d.rectangle([0, 0, H, H], fill=(30, 30, 40, 255))
    d.rectangle([0, 0, H - 1, H - 1], outline=(255, 255, 255, 230), width=5)
    pin = ic.get(b.get("pinId")) if b.get("pinId") else None
    if pin:
        p = pin.resize((84, 84), Image.LANCZOS); canvas.alpha_composite(p, (8, H - 92))

    x0 = H + 40
    draw_text(d, (x0, 118), b.get("nickname") or "Player", 78, (255, 255, 255, 255), 5, "l", W - x0 - 40)
    guild = c.get("clanName")
    if guild: draw_text(d, (x0, 205), guild, 54, (255, 70, 70, 255), 4, "l", W - x0 - 40)
    # লেভেল
    lv = f"Lv.{b.get('level', 0)}"
    f = font(44); tw = d.textlength(lv, font=f)
    pad = 16
    d.rounded_rectangle([W - tw - pad * 2 - 14, H - 66, W - 14, H - 14], radius=12, fill=(0, 0, 0, 170))
    d.text((W - tw - pad - 14, H - 40), lv, font=f, fill=(255, 255, 255, 255), anchor="lm")
    return encode(canvas, fmt)

# ---------------------------------------------------------------- OUTFIT
def hexagon(cx, cy, r):
    return [(cx + r * math.cos(math.radians(60 * i - 90)), cy + r * math.sin(math.radians(60 * i - 90))) for i in range(6)]

def outfit_image(data):
    b = data.get("basicInfo") or {}
    p = data.get("profileInfo") or {}
    W, H = 1200, 1000
    slot_ids = (list(p.get("clothes") or []) + list(b.get("weaponSkinShows") or []))[:8]
    ids = slot_ids + [p.get("avatarId"), b.get("headPic")]
    ic = fetch_many(ids)

    # ব্যাকগ্রাউন্ড: ডার্ক পার্পল গ্রেডিয়েন্ট + গ্লো
    bg = Image.new("RGBA", (W, H))
    bd = ImageDraw.Draw(bg)
    for y in range(H):
        t = y / H
        bd.line([(0, y), (W, y)], fill=(int(18 + 42 * t), int(8 + 14 * t), int(40 + 55 * t), 255))
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0)); gd = ImageDraw.Draw(glow)
    gd.ellipse([W / 2 - 330, 140, W / 2 + 330, 800], fill=(150, 80, 255, 90))
    glow = glow.filter(ImageFilter.GaussianBlur(90))
    canvas = Image.alpha_composite(bg, glow)
    d = ImageDraw.Draw(canvas)

    # প্ল্যাটফর্ম
    cx, py = W // 2, 835
    for i, (rx, ry, col) in enumerate([(300, 70, (60, 40, 110, 255)), (250, 56, (30, 20, 70, 255)), (205, 44, (80, 55, 140, 255))]):
        d.ellipse([cx - rx, py - ry + i * 6, cx + rx, py + ry + i * 6], fill=col, outline=(150, 110, 230, 255), width=3)

    # সেন্টার: ক্যারেক্টার আইকন
    ch = ic.get(p.get("avatarId")) or ic.get(b.get("headPic"))
    if ch:
        s = 520
        ch = ch.resize((s, s), Image.LANCZOS)
        mask = Image.new("L", (s, s), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, s - 1, s - 1], radius=40, fill=255)
        sh = Image.new("RGBA", (s + 60, s + 60), (0, 0, 0, 0))
        ImageDraw.Draw(sh).rounded_rectangle([30, 30, s + 30, s + 30], radius=40, fill=(0, 0, 0, 140))
        sh = sh.filter(ImageFilter.GaussianBlur(18))
        canvas.alpha_composite(sh, (cx - s // 2 - 30, 300 - 30))
        canvas.paste(ch, (cx - s // 2, 300), mask)
        d.rounded_rectangle([cx - s // 2, 300, cx + s // 2, 300 + s], radius=40, outline=(190, 150, 255, 255), width=4)

    # ৮টি হেক্সাগন স্লট (বামে ৪, ডানে ৪)
    R = 92
    pos_l = [(210, 190), (130, 400), (130, 610), (210, 820)]
    pos_r = [(W - x, y) for x, y in pos_l]
    for i, (sx, sy) in enumerate(pos_l + pos_r):
        poly = hexagon(sx, sy, R)
        d.polygon(poly, fill=(25, 18, 55, 255))
        if i < len(slot_ids):
            icon = ic.get(slot_ids[i])
            if icon:
                s = int(2 * (R - 8)) + 4
                im = cover(icon, s, s)
                m = Image.new("L", (W, H), 0)
                ImageDraw.Draw(m).polygon(hexagon(sx, sy, R - 8), fill=255)
                layer = Image.new("RGBA", (W, H), (0, 0, 0, 0)); layer.paste(im, (sx - s // 2, sy - s // 2))
                canvas.paste(layer, (0, 0), m)
        d.polygon(poly, outline=(170, 130, 255, 255), width=5)

    draw_text(d, (W // 2, 78), b.get("nickname") or "Player", 56, (255, 255, 255, 255), 4, "m", W - 120)
    draw_text(d, (W // 2, 130), f"Lv.{b.get('level', 0)}  •  UID {b.get('accountId', '')}", 30, (210, 190, 255, 255), 3, "m")
    return to_png(canvas)