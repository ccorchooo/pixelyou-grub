#!/usr/bin/env python3
"""Generate the "Pixel You" GRUB theme from a wallpaper.

Extracts a Material You palette from the wallpaper, then renders the windowed
background, menu pill, progress bar, terminal box, OS icons and GRUB fonts.

    python3 generate.py wallpaper.jpg -r 1920x1080 -o build/pixel-you --preview preview.png
"""
import argparse, json, math, os, shutil, socket, subprocess, sys, tempfile
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
SS = 4  # supersampling factor for anti-aliased shapes
MKFONT = "grub-mkfont"

# ───────────────────────────── colour science ─────────────────────────────
# Material You uses HCT (CAM16 hue/chroma + CIELAB L* tone). We approximate it
# with OKLCH hue/chroma and solve for the exact L* tone, which keeps the tone
# ladder (and therefore contrast) faithful to Material's.

def _lin(c):
    c /= 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

def _delin(x):
    x = max(0.0, min(1.0, x))
    return round(255 * (12.92 * x if x <= 0.0031308 else 1.055 * x ** (1 / 2.4) - 0.055))

def rgb_to_oklch(rgb):
    r, g, b = (_lin(v) for v in rgb)
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    L = 0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s
    A = 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s
    B = 0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s
    return L, math.hypot(A, B), math.degrees(math.atan2(B, A)) % 360

def _oklch_to_linear(L, C, H):
    A, B = C * math.cos(math.radians(H)), C * math.sin(math.radians(H))
    l = (L + 0.3963377774 * A + 0.2158037573 * B) ** 3
    m = (L - 0.1055613458 * A - 0.0638541728 * B) ** 3
    s = (L - 0.0894841775 * A - 1.2914855480 * B) ** 3
    return (4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
            -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
            -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s)

def _in_gamut(lin):
    return all(-1e-4 <= v <= 1 + 1e-4 for v in lin)

def _lstar(lin):
    y = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    return 116 * y ** (1 / 3) - 16 if y > 216 / 24389 else y * 24389 / 27

def tone(hue, chroma, t):
    """sRGB colour with the given OKLCH hue/chroma whose CIELAB L* equals t."""
    if t <= 0: return (0, 0, 0)
    if t >= 100: return (255, 255, 255)
    lo, hi, best = 0.0, 1.0, (0, 0, 0)
    for _ in range(28):
        L = (lo + hi) / 2
        c_lo, c_hi = 0.0, chroma
        lin = _oklch_to_linear(L, chroma, hue)
        if not _in_gamut(lin):  # shrink chroma until it fits sRGB
            for _ in range(18):
                c = (c_lo + c_hi) / 2
                if _in_gamut(_oklch_to_linear(L, c, hue)): c_lo = c
                else: c_hi = c
            lin = _oklch_to_linear(L, c_lo, hue)
        best = lin
        if _lstar(lin) < t: lo = L
        else: hi = L
    return tuple(_delin(v) for v in best)

def source_color(img):
    """Pick the most prominent *colourful* hue in the image (Material-style scoring)."""
    small = img.convert("RGB").resize((96, 96), Image.Resampling.BILINEAR)
    data = small.get_flattened_data() if hasattr(small, "get_flattened_data") else small.getdata()
    px = [rgb_to_oklch(p) for p in data]
    bins = [0.0] * 36
    for L, C, H in px:
        if C > 0.03 and 0.15 < L < 0.97:
            bins[int(H // 10) % 36] += C
    score = [bins[i - 1] * 0.5 + bins[i] + bins[(i + 1) % 36] * 0.5 for i in range(36)]
    best = max(range(36), key=score.__getitem__)
    if score[best] < 0.5:  # (nearly) grayscale wallpaper → Material's fallback blue
        return rgb_to_oklch((0x42, 0x85, 0xF4))[2], 0.0
    sx = sy = wsum = csum = 0.0
    for L, C, H in px:
        d = abs((H - (best * 10 + 5) + 180) % 360 - 180)
        if C > 0.03 and d <= 15:
            sx += C * math.cos(math.radians(H)); sy += C * math.sin(math.radians(H))
            wsum += 1; csum += C
    return math.degrees(math.atan2(sy, sx)) % 360, csum / wsum

# OKLCH chroma per palette (≈ Material's CAM16 chroma × 0.0023)
SCHEMES = {
    "tonal-spot": dict(p=0.083, s=0.037, t=0.055, n=0.014, nv=0.018, rot=60),
    "vibrant":    dict(p=0.200, s=0.060, t=0.080, n=0.022, nv=0.028, rot=40),
    "neutral":    dict(p=0.028, s=0.018, t=0.030, n=0.006, nv=0.010, rot=60),
}

ROLES = {  # role: (palette, dark tone, light tone)
    "primary": ("p", 80, 40), "on_primary": ("p", 20, 100),
    "primary_container": ("p", 30, 90), "on_primary_container": ("p", 90, 10),
    "secondary": ("s", 80, 40), "secondary_container": ("s", 30, 90),
    "on_secondary_container": ("s", 90, 10),
    "tertiary": ("t", 80, 40), "tertiary_container": ("t", 30, 90),
    "on_tertiary_container": ("t", 90, 10),
    "surface": ("n", 6, 98), "surface_container_low": ("n", 10, 96),
    "surface_container": ("n", 12, 94), "surface_container_high": ("n", 17, 92),
    "surface_container_highest": ("n", 22, 90), "on_surface": ("n", 90, 10),
    "on_surface_variant": ("nv", 80, 30), "outline": ("nv", 60, 50),
    "outline_variant": ("nv", 30, 80), "scrim": ("n", 0, 0),
}

def build_palette(hue, src_chroma, mode, scheme):
    sc = SCHEMES[scheme]
    # never exceed what the wallpaper actually has by too much for muted images
    boost = 1.0 if src_chroma == 0 or src_chroma > 0.05 else 0.6
    pal = {"p": (hue, sc["p"] * boost), "s": (hue, sc["s"]),
           "t": ((hue + sc["rot"]) % 360, sc["t"] * boost),
           "n": (hue, sc["n"]), "nv": (hue, sc["nv"])}
    i = 1 if mode == "dark" else 2
    return {role: tone(*pal[spec[0]], spec[i]) for role, spec in ROLES.items()}

def hexc(rgb): return "#%02x%02x%02x" % rgb[:3]
def rgba(rgb, a=255): return (*rgb[:3], a)

# ───────────────────────────── fonts ─────────────────────────────
UI_FAMILIES = ["Google Sans Flex", "Google Sans", "Google Sans Text", "Product Sans",
               "Outfit", "Figtree", "Inter", "Adwaita Sans", "Roboto", "Noto Sans",
               "DejaVu Sans"]
MONO_FAMILIES = ["Google Sans Code", "JetBrains Mono", "Adwaita Mono", "Roboto Mono",
                 "Noto Sans Mono", "DejaVu Sans Mono"]

def fc_find(families, style="Regular"):
    for fam in families:
        try:
            out = subprocess.run(["fc-match", "-f", "%{family}\n%{file}", f"{fam}:style={style}"],
                                 capture_output=True, text=True, check=True).stdout.split("\n")
        except (OSError, subprocess.CalledProcessError):
            break
        if len(out) == 2 and fam.lower() in out[0].lower() and os.path.exists(out[1]):
            return out[1]
    for p in ("/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if os.path.exists(p): return p
    sys.exit("No usable TTF font found; pass --font /path/to/font.ttf")

def find_symbol_font():
    """A Nerd Font (distro logos live at U+F300…)."""
    try:
        out = subprocess.run(["fc-list", ":charset=f303 f17a", "file"],
                             capture_output=True, text=True).stdout.split("\n")
    except OSError:
        return None
    files = sorted(l.rstrip(": ") for l in out if l.strip())
    pick = [f for f in files if "Propo-Regular" in f] or [f for f in files if "Regular" in f] or files
    return pick[0] if pick else None

def load_font(path, size, weight=400):
    f = ImageFont.truetype(path, max(1, round(size)))
    try:
        axes = f.get_variation_axes()
        f.set_variation_by_axes([weight if a.get("name") in (b"Weight", "Weight")
                                 else a["default"] for a in axes])
    except Exception:
        pass
    return f

def has_glyph(font, ch):
    missing = font.getmask("\U0010FFFD")
    m = font.getmask(ch)
    return m.size != (0, 0) and (m.size != missing.size or bytes(m) != bytes(missing))

def mkfont(ttf, size, name, out):
    ranges = "0x20-0x7E,0xA0-0x24F,0x370-0x3FF,0x400-0x4FF,0x2000-0x206F,0x2190-0x21FF,0x2500-0x259F"
    subprocess.run([MKFONT, "-s", str(size), "-n", name, "-r", ranges, "-o", out, ttf],
                   check=True, stderr=subprocess.DEVNULL)
    with open(out, "rb") as fh:  # read the real font name from the PF2 header
        data = fh.read(256)
    i = data.index(b"NAME")
    n = int.from_bytes(data[i + 4:i + 8], "big")
    return data[i + 8:i + 8 + n].rstrip(b"\0").decode()

# ───────────────────────────── drawing helpers ─────────────────────────────

def rrect_mask(w, h, r):
    """Anti-aliased rounded-rectangle mask (supersampled)."""
    m = Image.new("L", (w * SS, h * SS), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w * SS - 1, h * SS - 1), radius=r * SS, fill=255)
    return m.resize((w, h), Image.Resampling.LANCZOS)

def paste_rrect(base, box, r, fill, alpha=255):
    x, y, w, h = (round(v) for v in box)
    layer = Image.new("RGBA", (w, h), rgba(fill))
    mask = rrect_mask(w, h, r)
    if alpha < 255:
        mask = mask.point(lambda v: v * alpha // 255)
    base.alpha_composite(Image.composite(layer, Image.new("RGBA", (w, h), (0, 0, 0, 0)), mask), (x, y))

def cover(img, w, h):
    iw, ih = img.size
    s = max(w / iw, h / ih)
    img = img.resize((max(w, round(iw * s)), max(h, round(ih * s))), Image.Resampling.LANCZOS)
    l, t = (img.width - w) // 2, (img.height - h) // 2
    return img.crop((l, t, l + w, t + h))

def stroke_lines(draw, pts, width, fill):
    draw.line(pts, fill=fill, width=width, joint="curve")
    for x, y in (pts[0], pts[-1]):
        draw.ellipse((x - width / 2, y - width / 2, x + width / 2, y + width / 2), fill=fill)

def glyph_icon(size, ch, font_path, fg):
    """Draw a single symbol glyph, centred, anti-aliased."""
    im = Image.new("RGBA", (size * SS, size * SS), (0, 0, 0, 0))
    f = load_font(font_path, size * SS)
    d = ImageDraw.Draw(im)
    x0, y0, x1, y1 = d.textbbox((0, 0), ch, font=f)
    d.text(((size * SS - (x1 - x0)) / 2 - x0, (size * SS - (y1 - y0)) / 2 - y0), ch, font=f, fill=rgba(fg))
    return im.resize((size, size), Image.Resampling.LANCZOS)

# ───────────────────────────── OS icons ─────────────────────────────
# GRUB picks icons/<class>.png for the first --class of an entry that has one.
# Classes come from 10_linux (GRUB_DISTRIBUTOR), 30_os-prober (first word of
# the os-prober label, lowercased) and 30_uefi-firmware / custom entries.
NF = {
    "arch": 0xF303, "archlinux": 0xF303, "manjaro": 0xF312, "manjarolinux": 0xF312,
    "ubuntu": 0xF31B, "kubuntu": 0xF31B, "xubuntu": 0xF31B, "lubuntu": 0xF31B,
    "ubuntustudio": 0xF31B, "ubuntu_budgie": 0xF31B,
    "debian": 0xF306, "devuan": 0xF307, "fedora": 0xF30A, "linuxmint": 0xF30E, "mint": 0xF30E,
    "opensuse": 0xF314, "opensuse_tumbleweed": 0xF314, "opensuse_leap": 0xF314, "suse": 0xF314,
    "gentoo": 0xF30D, "nixos": 0xF313, "pop": 0xF32A, "pop_os": 0xF32A, "popos": 0xF32A,
    "endeavouros": 0xF322, "endeavour": 0xF322, "elementary": 0xF309, "centos": 0xF304,
    "rhel": 0xF316, "redhat": 0xF316, "redhatenterpriseserver": 0xF316,
    "almalinux": 0xF31D, "rocky": 0xF32B, "zorin": 0xF32F, "kali": 0xF327, "void": 0xF32E,
    "alpine": 0xF300, "artix": 0xF31F, "garuda": 0xF337, "solus": 0xF32D, "deepin": 0xF321,
    "mageia": 0xF310, "slackware": 0xF318, "parrot": 0xF329, "qubes": 0xF342,
    "arcolinux": 0xF346, "neon": 0xF331, "freebsd": 0xF30C, "openbsd": 0xF328,
    "raspbian": 0xF315, "guix": 0xF325,
    "gnu-linux": 0xF31A, "gnu": 0xF31A, "linux": 0xF31A, "os": 0xF31A, "hurd": 0xF31A,
    "lfs": 0xF31A, "unknown": 0xF31A,
    "windows": 0xF05B3, "windows10": 0xF05B3, "windows11": 0xF05B3, "microsoft": 0xF05B3,
    "osx": 0xF302, "macosx": 0xF302, "macos": 0xF302, "darwin": 0xF302,
    "efi": 0xF013, "uefi": 0xF013, "uefi-firmware": 0xF013, "firmware": 0xF013,
    "settings": 0xF013, "recovery": 0xF0E2, "memtest": 0xF035B, "memtest86": 0xF035B,
    "shutdown": 0xF011, "poweroff": 0xF011, "reboot": 0xF021, "restart": 0xF021,
}

def make_icon(cls, size, pal, sym_font, ui_font):
    bg, fg = pal["primary_container"], pal["on_primary_container"]
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    im.alpha_composite(Image.composite(Image.new("RGBA", (size, size), rgba(bg)),
                                       Image.new("RGBA", (size, size), (0, 0, 0, 0)),
                                       rrect_mask(size, size, size // 2)))
    inner = round(size * 0.62)
    ch = chr(NF[cls]) if sym_font and cls in NF else None
    if ch and has_glyph(load_font(sym_font, 32), ch):
        g = glyph_icon(inner, ch, sym_font, fg)
    else:
        g = glyph_icon(round(size * 0.5), cls[:1].upper(), ui_font, fg)  # no Nerd Font: letter badge
    im.alpha_composite(g, ((size - g.width) // 2, (size - g.height) // 2))
    return im

# ───────────────────────────── layout ─────────────────────────────

# Zoom magnifies the content *inside* the window; the window itself always
# keeps the same size on screen. Above MAX_ZOOM fewer than 3 menu entries fit.
MIN_ZOOM, MAX_ZOOM = 0.5, 1.33

def clamp_zoom(z):
    return max(MIN_ZOOM, min(MAX_ZOOM, z))

def layout(W, H, zoom=1.0):
    f = H / 1080                  # frame scale: window size, corners, shadow
    s = f * clamp_zoom(zoom)      # content scale: text, menu, icons, spacing
    win_w = min(round(1360 * f), W - round(120 * f))
    win_h = round(760 * f)
    wx, wy = (W - win_w) // 2, (H - win_h) // 2
    pad = round(18 * f)
    lw = round(win_w * 0.42)
    L = dict(f=f, s=s, win=(wx, wy, win_w, win_h), win_r=round(46 * f), pad=pad,
             left=(wx + pad, wy + pad, lw, win_h - 2 * pad), left_r=round(28 * f))
    rx = wx + pad + lw + round(60 * s)
    rw = wx + win_w - round(56 * s) - rx
    L["right_x"], L["right_w"] = rx, rw
    L["head_y"] = wy + round(78 * s)
    L["item_h"] = round(58 * s) // 2 * 2
    L["item_gap"] = round(8 * s)
    L["icon"] = round(34 * s)
    L["icon_space"] = round(18 * s)
    menu_top = wy + round(196 * s)
    L["menu"] = (rx - round(12 * s), menu_top, rw + round(24 * s),
                 wy + win_h - round(174 * s) - menu_top)
    L["timeout_y"] = wy + win_h - round(112 * s)
    # GRUB forces progress bars to be >= 28px tall, so the box is 32px+ and the
    # visible pill is a thin stripe centred in transparent padding.
    L["bar_stripe"] = max(4, round(8 * s)) // 2 * 2
    box = max(32, round(32 * s)) // 2 * 2
    L["bar"] = (rx, wy + win_h - round(66 * s) - box // 2, rw, box)
    L["font_item"] = round(24 * s)
    L["font_small"] = round(18 * s)
    L["font_term"] = round(18 * s)
    L["term_r"] = L["win_r"]  # console box = window outline
    return L

# ───────────────────────────── background ─────────────────────────────

def render_background(wall, W, H, L, pal, mode, fonts, title):
    s, fr = L["s"], L["f"]
    on_wall = (255, 255, 255)
    # 1. blurred, tinted wallpaper behind everything
    bg = cover(wall, W, H).filter(ImageFilter.GaussianBlur(70 * fr)).convert("RGBA")
    bg.alpha_composite(Image.new("RGBA", (W, H), rgba(pal["surface"], 70 if mode == "dark" else 50)))

    # 2. soft elevation shadow under the window
    wx, wy, ww, wh = L["win"]
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    paste_rrect(sh, (wx, wy + 24 * fr, ww, wh), L["win_r"], (0, 0, 0), 120 if mode == "dark" else 70)
    bg.alpha_composite(sh.filter(ImageFilter.GaussianBlur(40 * fr)))

    # 3. the window surface
    paste_rrect(bg, L["win"], L["win_r"], pal["surface_container"])

    # 4. left panel: sharp wallpaper, concentric corner radius
    lx, ly, lw, lh = L["left"]
    panel = cover(wall, lw, lh).convert("RGBA")
    grad = Image.linear_gradient("L").resize((lw, lh))  # top→bottom 0..255
    top_scrim = grad.point(lambda v: max(0, 110 - v * 2))           # dark fade at the top
    bot_scrim = grad.point(lambda v: max(0, (v - 140) * 150 // 115))  # dark fade at the bottom
    black = Image.new("RGBA", (lw, lh), (0, 0, 0, 255))
    panel = Image.composite(black, panel, top_scrim)
    panel = Image.composite(black, panel, bot_scrim)
    d = ImageDraw.Draw(panel)

    m = round(32 * s)
    # wordmark (hostname by default), like a lock-screen clock
    f_title = load_font(fonts["ui"], 46 * s, 700)
    d.text((m, m - round(6 * s)), title, font=f_title, fill=on_wall)

    # "Select your system →" chip, frosted (dropped if zoom leaves no room)
    f_chip = load_font(fonts["ui"], 16 * s, 500)
    chip_txt = "Select your system"
    tw = d.textlength(chip_txt, font=f_chip)
    ch_h = round(38 * s)
    ch_w = round(tw + 64 * s)
    cx, cy = lw - m - ch_w, m
    if cx > m + d.textlength(title, font=f_title) + 16 * s:
        paste_rrect(panel, (cx, cy, ch_w, ch_h), ch_h // 2, (255, 255, 255), 56)
        d = ImageDraw.Draw(panel)
        d.text((cx + 18 * s, cy + ch_h / 2), chip_txt, font=f_chip, fill=on_wall, anchor="lm")
        ax, ay, a = cx + ch_w - 26 * s, cy + ch_h / 2, 5 * s
        lwid = max(2, round(2 * s))
        stroke_lines(d, [(ax - 6 * s, ay), (ax + a, ay)], lwid, on_wall)
        stroke_lines(d, [(ax, ay - a), (ax + a, ay), (ax, ay + a)], lwid, on_wall)

    # bottom: message + keyboard hint card (frosted glass)
    card_h = round(92 * s)
    card = (m, lh - m - card_h, lw - 2 * m, card_h)
    region = panel.crop((card[0], card[1], card[0] + card[2], card[1] + card[3]))
    region = region.filter(ImageFilter.GaussianBlur(18 * s))
    region.alpha_composite(Image.new("RGBA", region.size, rgba(pal["surface_container_high"], 190)))
    panel.paste(Image.composite(region, panel.crop((card[0], card[1], card[0] + card[2], card[1] + card[3])),
                                rrect_mask(card[2], card[3], round(24 * s))), card[:2])
    d = ImageDraw.Draw(panel)

    f_msg = load_font(fonts["ui"], 22 * s, 400)
    f_msg_b = load_font(fonts["ui"], 22 * s, 600)
    msg_y = card[1] - round(86 * s)
    parts = [("Press a ", f_msg), ("shortcut key", f_msg_b), (" to", f_msg)]
    x = m
    for t, f in parts:
        d.text((x, msg_y), t, font=f, fill=on_wall); x += d.textlength(t, font=f)
    d.text((m, msg_y + round(30 * s)), "change how your system starts", font=f_msg, fill=on_wall)

    f_key = load_font(fonts["ui"], 17 * s, 600)
    f_lbl = load_font(fonts["ui"], 17 * s, 500)
    kh = round(36 * s)
    # pick the longest labels that fit, then spread the groups evenly
    for labels in (("Boot", "Edit entry", "Console"), ("Boot", "Edit", "Console"),
                   ("Boot", "Edit", "Shell"), ("", "", "")):
        widths = [kh + (12 * s + d.textlength(l, font=f_lbl) if l else 0) for l in labels]
        gap = (card[2] - 2 * 18 * s - sum(widths)) / 2
        if gap >= 14 * s or not labels[0]:
            break
    keys = list(zip(("enter", "E", "C"), labels))
    kx = card[0] + 18 * s
    for i, (k, lbl) in enumerate(keys):
        if i: kx += widths[i - 1] + gap
        ky = round(card[1] + (card_h - kh) / 2)
        paste_rrect(panel, (round(kx), ky, kh, kh), round(10 * s), pal["primary"])
        d = ImageDraw.Draw(panel)
        if k == "enter":  # ↵ drawn as strokes so it never depends on font coverage
            c, w2 = kx + kh / 2, max(2, round(2.2 * s))
            pts = [(c + 7 * s, ky + 11 * s), (c + 7 * s, ky + 21 * s), (c - 7 * s, ky + 21 * s)]
            stroke_lines(d, pts, w2, pal["on_primary"])
            stroke_lines(d, [(c - 3 * s, ky + 17 * s), (c - 7 * s, ky + 21 * s), (c - 3 * s, ky + 25 * s)],
                         w2, pal["on_primary"])
        else:
            d.text((kx + kh / 2, ky + kh / 2), k, font=f_key, fill=pal["on_primary"], anchor="mm")
        d.text((kx + kh + 12 * s, ky + kh / 2), lbl, font=f_lbl, fill=pal["on_surface"], anchor="lm")

    bg.alpha_composite(Image.composite(panel, Image.new("RGBA", (lw, lh), (0, 0, 0, 0)),
                                       rrect_mask(lw, lh, L["left_r"])), (lx, ly))

    # 5. right column header (the menu itself is drawn by GRUB)
    d = ImageDraw.Draw(bg)
    rx = L["right_x"]
    f_over = load_font(fonts["ui"], 16 * s, 600)
    f_head = load_font(fonts["ui"], 40 * s, 500)
    d.text((rx, L["head_y"]), "STARTUP", font=f_over, fill=pal["primary"])
    d.text((rx, L["head_y"] + round(26 * s)), "Choose a system", font=f_head, fill=pal["on_surface"])
    # divider above the countdown
    dy = L["timeout_y"] - round(26 * s)
    d.line((rx, dy, rx + L["right_w"], dy), fill=pal["outline_variant"], width=max(1, round(s)))
    return bg.convert("RGB")

# ───────────────────────────── 9-slice pieces ─────────────────────────────

def pill_caps(h, color, prefix, out, box_h=None):
    """select_w/c/e style pieces: half-circle ends exactly h tall, stretchable centre.
    With box_h the pill is centred vertically in a taller transparent canvas."""
    r = h // 2
    box_h = box_h or h
    full = Image.new("RGBA", (2 * r + 1, box_h), (0, 0, 0, 0))
    pill = Image.composite(Image.new("RGBA", (2 * r + 1, h), rgba(color)),
                           Image.new("RGBA", (2 * r + 1, h), (0, 0, 0, 0)),
                           rrect_mask(2 * r + 1, h, r))
    full.alpha_composite(pill, (0, (box_h - h) // 2))
    h = box_h
    full.crop((0, 0, r, h)).save(f"{out}/{prefix}_w.png")
    full.crop((r, 0, r + 1, h)).save(f"{out}/{prefix}_c.png")
    full.crop((r + 1, 0, 2 * r + 1, h)).save(f"{out}/{prefix}_e.png")

def pill_caps_vertical(w, color, prefix, out):
    r = w // 2
    full = Image.new("RGBA", (w, 2 * r + 1), (0, 0, 0, 0))
    full.alpha_composite(Image.composite(Image.new("RGBA", full.size, rgba(color)),
                                         Image.new("RGBA", full.size, (0, 0, 0, 0)),
                                         rrect_mask(w, full.height, r)))
    full.crop((0, 0, w, r)).save(f"{out}/{prefix}_n.png")
    full.crop((0, r, w, r + 1)).save(f"{out}/{prefix}_c.png")
    full.crop((0, r + 1, w, 2 * r + 1)).save(f"{out}/{prefix}_s.png")

def nine_slice(r, color, prefix, out, border=None):
    n = 2 * r + 1
    full = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    paste_rrect(full, (0, 0, n, n), r, border or color)
    if border:
        paste_rrect(full, (2, 2, n - 4, n - 4), r - 2, color)
    for name, box in {"nw": (0, 0, r, r), "n": (r, 0, r + 1, r), "ne": (r + 1, 0, n, r),
                      "w": (0, r, r, r + 1), "c": (r, r, r + 1, r + 1), "e": (r + 1, r, n, r + 1),
                      "sw": (0, r + 1, r, n), "s": (r, r + 1, r + 1, n), "se": (r + 1, r + 1, n, n)}.items():
        full.crop(box).save(f"{out}/{prefix}_{name}.png")

# ───────────────────────────── theme.txt ─────────────────────────────

def rel(v, D):
    """GRUB proportional spec ("P%+px"): exact at the generated resolution and it
    follows the stretched background if GRUB ends up in a different mode."""
    p = int(v * 100 // D)
    return f'"{p}%{v - round(p * D / 100):+d}"'

def theme_txt(W, H, L, pal, fn):
    mx, my, mw, mh = L["menu"]
    bx, by, bw, bh = L["bar"]
    s = L["s"]
    # console (also shows "Loading Linux…" while booting) sits exactly on the window
    wx, wy, ww, wh = L["win"]
    tr = L["term_r"]
    X = lambda v: rel(v, W)
    Y = lambda v: rel(v, H)
    return f"""# Pixel You — Material You GRUB theme (generated for {W}x{H})
title-text: ""
desktop-image: "background.png"
desktop-image-scale-method: "stretch"
desktop-color: "{hexc(pal['surface'])}"
message-font: "{fn['small']}"
message-color: "{hexc(pal['on_surface'])}"
terminal-font: "{fn['term']}"
terminal-box: "terminal_box_*.png"
terminal-left: {X(wx + tr)}
terminal-top: {Y(wy + tr)}
terminal-width: {X(ww - 2 * tr)}
terminal-height: {Y(wh - 2 * tr)}
terminal-border: "0"

+ boot_menu {{
  left = {X(mx)}
  top = {Y(my)}
  width = {X(mw)}
  height = {Y(mh)}
  item_font = "{fn['item']}"
  item_color = "{hexc(pal['on_surface'])}"
  selected_item_font = "{fn['item']}"
  selected_item_color = "{hexc(pal['on_primary'])}"
  item_height = {L['item_h']}
  item_padding = 0
  item_spacing = {L['item_gap']}
  icon_width = {L['icon']}
  icon_height = {L['icon']}
  item_icon_space = {L['icon_space']}
  selected_item_pixmap_style = "select_*.png"
  scrollbar = true
  scrollbar_width = {max(4, round(6 * s)) // 2 * 2}
  scrollbar_frame = "scroll_track_*.png"
  scrollbar_thumb = "scroll_thumb_*.png"
  scrollbar_slice = "center"
  scrollbar_left_pad = {round(10 * s)}
}}

+ label {{
  id = "__timeout__"
  left = {X(bx)}
  top = {Y(L['timeout_y'])}
  width = {X(bw)}
  height = {round(28 * s)}
  align = "left"
  font = "{fn['small']}"
  color = "{hexc(pal['on_surface_variant'])}"
  text = "Starting the highlighted system in %d s"
}}

+ progress_bar {{
  id = "__timeout__"
  left = {X(bx)}
  top = {Y(by)}
  width = {X(bw)}
  height = {bh}
  show_text = false
  bar_style = "progress_track_*.png"
  highlight_style = "progress_bar_*.png"
  highlight_overlay = true
}}
"""

# ───────────────────────────── preview (simulated GRUB render) ─────────────────────────────

def render_preview(theme_dir, bg, W, H, L, pal, fonts):
    im = bg.convert("RGBA")
    d = ImageDraw.Draw(im)
    s = L["s"]
    f_item = load_font(fonts["ui"], L["font_item"], 400)
    f_small = load_font(fonts["ui"], L["font_small"], 400)
    mx, my, mw, mh = L["menu"]
    items = [("arch", "Arch Linux"), (None, "Advanced options for Arch Linux"),
             ("windows", "Windows Boot Manager (on /dev/nvme0n1p1)"),
             ("ubuntu", "Ubuntu 24.04.1 LTS (on /dev/sda2)"),
             ("fedora", "Fedora Linux 42 (on /dev/sdb3)"), (None, "UEFI Firmware Settings")]
    ih, gap = L["item_h"], L["item_gap"]
    for i, (cls, text) in enumerate(items):
        y = my + i * (ih + gap)
        if y + ih > my + mh: break
        if i == 0:
            w = Image.open(f"{theme_dir}/select_w.png"); e = Image.open(f"{theme_dir}/select_e.png")
            c = Image.open(f"{theme_dir}/select_c.png").resize((mw - w.width - e.width, ih))
            im.alpha_composite(w, (mx, y)); im.alpha_composite(c, (mx + w.width, y))
            im.alpha_composite(e, (mx + mw - e.width, y))
        x = mx + ih // 2
        if cls:
            im.alpha_composite(Image.open(f"{theme_dir}/icons/{cls}.png"), (x, y + (ih - L["icon"]) // 2))
        tx = x + L["icon"] + L["icon_space"]
        d.text((tx, y + ih / 2), text, font=f_item, anchor="lm",
               fill=pal["on_primary"] if i == 0 else pal["on_surface"])
    d.text((L["bar"][0], L["timeout_y"] + round(14 * s)), "Starting the highlighted system in 4 s",
           font=f_small, anchor="lm", fill=pal["on_surface_variant"])
    bx, by, bw, bh = L["bar"]
    st = L["bar_stripe"]; sy = by + (bh - st) // 2
    paste_rrect(im, (bx, sy, bw, st), st // 2, pal["surface_container_highest"])
    paste_rrect(im, (bx, sy, round(bw * 0.6), st), st // 2, pal["primary"])
    return im.convert("RGB")

# ───────────────────────────── main ─────────────────────────────

def _edid_mode(path):
    """Preferred mode from the first detailed timing descriptor of an EDID blob."""
    try:
        e = open(path, "rb").read()
    except OSError:
        return None
    if len(e) < 128 or e[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00" or e[54] == e[55] == 0:
        return None
    w = e[56] | (e[58] >> 4) << 8
    h = e[59] | (e[61] >> 4) << 8
    return f"{w}x{h}" if w and h else None

def detect_resolution():
    """Native mode of each connected display (kernel mode list, else EDID)."""
    import glob
    found = []
    for status in sorted(glob.glob("/sys/class/drm/*/status")):
        conn = os.path.dirname(status)
        try:
            if open(status).read().strip() != "connected": continue
            mode = open(os.path.join(conn, "modes")).readline().strip()
        except OSError:
            mode = ""
        mode = mode if "x" in mode else _edid_mode(os.path.join(conn, "edid"))
        if mode:
            found.append((os.path.basename(conn).split("-", 1)[-1], mode.split("i")[0]))
    if not found:
        print("note: couldn't detect the display resolution; using 1920x1080 (pass -r WxH)",
              file=sys.stderr)
        return "1920x1080"
    if len({m for _, m in found}) > 1:
        print("note: several displays: " + ", ".join(f"{c} {m}" for c, m in found)
              + f"; using {found[0][1]} (pass -r WxH for another)", file=sys.stderr)
    return found[0][1]

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("wallpaper", nargs="?", default=os.path.join(HERE, "wallpapers", "waves.png"))
    ap.add_argument("-r", "--resolution", default=None, help="WxH (default: native resolution of the connected display)")
    ap.add_argument("-m", "--mode", choices=["dark", "light", "auto"], default="dark")
    ap.add_argument("-s", "--scheme", choices=list(SCHEMES), default="tonal-spot")
    ap.add_argument("-c", "--color", help="seed colour #rrggbb instead of extracting from the wallpaper")
    ap.add_argument("-t", "--title", default=socket.gethostname().split(".")[0],
                    help="wordmark on the wallpaper panel (default: hostname)")
    ap.add_argument("--font", help="UI .ttf/.otf (default: Google Sans → Inter → Adwaita Sans → Noto Sans …)")
    ap.add_argument("--mono-font", help="terminal .ttf")
    ap.add_argument("-o", "--out", default=os.path.join(HERE, "build", "pixel-you"))
    ap.add_argument("-z", "--zoom", type=float, default=1.0,
                    help="magnify the content inside the window: 1.0 = default, 1.2 = 20%% bigger, 0.8 = smaller "
                         "(0.5–1.33; the window keeps its size)")
    ap.add_argument("--preview", help="also write a simulated screenshot to this path")
    a = ap.parse_args()

    global MKFONT
    MKFONT = shutil.which("grub-mkfont") or shutil.which("grub2-mkfont")
    if not MKFONT:
        sys.exit("grub-mkfont not found (it ships with the grub / grub2-tools package)")
    W, H = map(int, (a.resolution or detect_resolution()).lower().split("x"))
    wall = Image.open(a.wallpaper).convert("RGB")

    if a.color:
        c = a.color.lstrip("#")
        _, chroma, hue = rgb_to_oklch(tuple(int(c[i:i + 2], 16) for i in (0, 2, 4)))
    else:
        hue, chroma = source_color(wall)
    mode = a.mode
    if mode == "auto":
        mean = sum(wall.convert("L").resize((64, 64)).getdata()) / 4096
        mode = "light" if mean > 150 else "dark"
    pal = build_palette(hue, chroma, mode, a.scheme)

    fonts = {"ui": a.font or fc_find(UI_FAMILIES), "mono": a.mono_font or fc_find(MONO_FAMILIES),
             "sym": find_symbol_font()}
    if clamp_zoom(a.zoom) != a.zoom:
        print(f"note: zoom must be {MIN_ZOOM}–{MAX_ZOOM}; using {clamp_zoom(a.zoom):g}", file=sys.stderr)
    L = layout(W, H, a.zoom)

    out = a.out
    if os.path.isdir(out): shutil.rmtree(out)
    os.makedirs(f"{out}/icons")

    bg = render_background(wall, W, H, L, pal, mode, fonts, a.title)
    bg.save(f"{out}/background.png", optimize=True)

    pill_caps(L["item_h"], pal["primary"], "select", out)
    pill_caps(L["bar_stripe"], pal["surface_container_highest"], "progress_track", out, L["bar"][3])
    pill_caps(L["bar_stripe"], pal["primary"], "progress_bar", out, L["bar"][3])
    sw = max(4, round(6 * L["s"])) // 2 * 2
    pill_caps_vertical(sw, pal["surface_container_highest"], "scroll_track", out)
    pill_caps_vertical(sw, pal["outline"], "scroll_thumb", out)
    nine_slice(L["term_r"], pal["surface_container"], "terminal_box", out,
               border=pal["outline_variant"])

    for cls in NF:
        make_icon(cls, L["icon"], pal, fonts["sym"], fonts["ui"]).save(f"{out}/icons/{cls}.png")

    fn = {"item": mkfont(fonts["ui"], L["font_item"], "PixelYou", f"{out}/pixelyou_{L['font_item']}.pf2"),
          "small": mkfont(fonts["ui"], L["font_small"], "PixelYou", f"{out}/pixelyou_{L['font_small']}.pf2"),
          "term": mkfont(fonts["mono"], L["font_term"], "PixelYouMono", f"{out}/pixelyou_mono_{L['font_term']}.pf2")}
    with open(f"{out}/theme.txt", "w") as fh:
        fh.write(theme_txt(W, H, L, pal, fn))
    with open(f"{out}/palette.json", "w") as fh:
        json.dump({"mode": mode, "scheme": a.scheme, "resolution": f"{W}x{H}",
                   **{k: hexc(v) for k, v in pal.items()}}, fh, indent=2)

    if a.preview:
        render_preview(out, bg, W, H, L, pal, fonts).save(a.preview)

    print(f"theme   → {out}  ({W}x{H}, {mode}, {a.scheme}, zoom {clamp_zoom(a.zoom):g})")
    print(f"seed    → hue {hue:.0f}°, primary {hexc(pal['primary'])}, surface {hexc(pal['surface_container'])}")
    print(f"fonts   → {os.path.basename(fonts['ui'])}, {os.path.basename(fonts['mono'])}, "
          f"icons: {os.path.basename(fonts['sym']) if fonts['sym'] else 'letter badges (no Nerd Font found)'}")
    if a.preview: print(f"preview → {a.preview}")

if __name__ == "__main__":
    main()
