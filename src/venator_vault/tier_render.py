"""Kyber Tiers card faces: full-bleed art with a distinct material per rarity.

Common "Field Issue" (matte steel), Uncommon "Squadron" (brushed teal),
Rare "Holo" (blue chrome + art sheen), Epic "Kyber Prism" (prismatic frame,
arched window), Legendary "Gilded Archive" (gold, etched edge, glint) and
Mythic "Kyber Crystal" (borderless, iridescent foil, shards). Legendary and
Mythic can also be rendered as a short looping GIF glint.

Pure Pillow; no network, database or ownership data. Faction sigils are
original geometric marks, not official insignia.
"""
from __future__ import annotations

import hashlib
import io
import math
import random
import re

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, ImageStat

SIZE = (1000, 1400)
STYLE = "kyber-tiers-1"
TIERS = {
    "common": {"frame": 24, "ink": (214, 222, 228), "pips": 1, "label": "COMMON"},
    "uncommon": {"frame": 28, "ink": (160, 226, 210), "pips": 2, "label": "UNCOMMON"},
    "rare": {"frame": 30, "ink": (178, 222, 255), "pips": 3, "label": "RARE"},
    "epic": {"frame": 32, "ink": (222, 204, 255), "pips": 4, "label": "EPIC"},
    "legendary": {"frame": 38, "ink": (255, 228, 150), "pips": 5, "label": "LEGENDARY"},
    "mythic": {"frame": 0, "ink": (255, 214, 220), "pips": 6, "label": "MYTHIC"},
    "celestial": {"frame": 34, "ink": (255, 240, 200), "pips": 7, "label": "CELESTIAL"},
}
ANIMATED = frozenset(("legendary", "mythic", "celestial"))
FACTIONS = {
    "Galactic Republic": ((196, 64, 52), "hexagon"), "Jedi Order": ((214, 170, 92), "orbit"),
    "Galactic Empire": ((150, 160, 172), "hexring"), "Rebel Alliance": ((226, 92, 64), "star"),
    "Separatist Alliance": ((74, 120, 206), "diamond"), "First Order": ((210, 56, 56), "triangle"),
    "Resistance": ((236, 150, 60), "star"), "Sith Order": ((200, 30, 40), "triangle"),
    "Sith Empire": ((176, 40, 60), "triangle"), "Sith Eternal": ((170, 40, 90), "triangle"),
    "Mandalorians": ((96, 150, 190), "chevron"), "Naboo": ((120, 170, 90), "orbit"),
    "New Republic": ((230, 120, 70), "star"), "Hutt Cartel": ((176, 140, 70), "diamond"),
    "Trade Federation": ((120, 140, 170), "diamond"), "Wildlife": ((110, 170, 110), "leaf"),
    "Nightsisters": ((140, 60, 110), "orbit"), "Crimson Dawn": ((190, 40, 60), "chevron"),
}
DEFAULT_FACTION = ((150, 168, 184), "circle")


def _mix(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def _ramp(size, stops, angle=0.0):
    """Multi-stop linear gradient, computed on a small tile and enlarged."""
    w, h = 160, 224
    tile = Image.new("RGB", (w, h))
    dx, dy = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    span = abs(dx) * w + abs(dy) * h
    pixels = []
    for y in range(h):
        for x in range(w):
            t = ((x - w / 2) * dx + (y - h / 2) * dy) / span + .5
            t = min(1, max(0, t)) * (len(stops) - 1)
            i = min(int(t), len(stops) - 2)
            pixels.append(_mix(stops[i], stops[i + 1], t - i))
    tile.putdata(pixels)
    return tile.resize(size, Image.Resampling.BILINEAR)


def _brushed(size, base, seed):
    rng = random.Random(seed)
    tile = Image.new("RGB", (8, size[1] // 2))
    d = ImageDraw.Draw(tile)
    for y in range(tile.height):
        shade = rng.uniform(-14, 14)
        d.line((0, y, 8, y), fill=tuple(max(0, min(255, round(c + shade))) for c in base))
    return tile.resize(size, Image.Resampling.BILINEAR).filter(ImageFilter.GaussianBlur(.6))


# ---------- art extraction and focal crop ----------

def _flat(stat_image):
    """A letterbox/matte line: nearly uniform and dark."""
    stat = ImageStat.Stat(stat_image)
    return stat.stddev[0] < 3.5 and stat.mean[0] < 40


def trim_matte(image):
    """Remove uniform letterbox bars left by older contain-fit compositions."""
    gray = image.convert("L")
    w, h = gray.size
    top, bottom, left, right = 0, h - 1, 0, w - 1
    while top < h // 2 and _flat(gray.crop((0, top, w, top + 1))):
        top += 1
    while bottom > top + h // 4 and _flat(gray.crop((0, bottom, w, bottom + 1))):
        bottom -= 1
    while left < w // 2 and _flat(gray.crop((left, top, left + 1, bottom))):
        left += 1
    while right > left + w // 4 and _flat(gray.crop((right, top, right + 1, bottom))):
        right -= 1
    if (right - left + 1) < w * .3 or (bottom - top + 1) < h * .3:
        return image
    return image.crop((left, top, right + 1, bottom + 1))


def subject_region(image, entry):
    """The published pixels inside a reviewed composition, without its matte."""
    norm = (entry or {}).get("normalization") or {}
    if norm.get("fit_mode") == "cover":
        return image  # already a full-frame picture with no matte
    box = None
    if norm.get("placement_xy") and norm.get("placement_size"):
        (x, y), (w, h) = norm["placement_xy"], norm["placement_size"]
        box = (x, y, x + w, y + h)
    elif isinstance(norm.get("placement"), dict) and norm["placement"].get("size"):
        (x, y), (w, h) = norm["placement"].get("xy", (0, 0)), norm["placement"]["size"]
        box = (x, y, x + w, y + h)
    if box and box[2] - box[0] > 40 and box[3] - box[1] > 40:
        inset = 6 if norm.get("feather_px") else 1
        box = (max(0, box[0] + inset), max(0, box[1] + inset),
               min(image.width, box[2] - inset), min(image.height, box[3] - inset))
        image = image.crop(box)
    return trim_matte(image)


def _focus(values, window, prior):
    """Best window start: edge energy with a soft positional prior."""
    n = len(values)
    if window >= n:
        return 0
    total, best, best_at = sum(values[:window]), -1.0, 0
    peak = max(1e-6, max(values) * window)
    for start in range(n - window + 1):
        if start:
            total += values[start + window - 1] - values[start - 1]
        center = (start + window / 2) / n
        score = total / peak - 1.4 * (center - prior) ** 2
        if score > best:
            best, best_at = score, start
    return best_at


def cover(image, size):
    """Fill size completely, cropping toward the busiest (usually the subject) area."""
    tw, th = size
    scale = max(tw / image.width, th / image.height)
    resized = image.resize((max(tw, round(image.width * scale)), max(th, round(image.height * scale))),
                           Image.Resampling.LANCZOS)
    if scale > 1.25:
        resized = resized.filter(ImageFilter.UnsharpMask(radius=2, percent=60, threshold=2))
    small = resized.convert("L").resize((max(1, resized.width // 8), max(1, resized.height // 8)))
    edges = small.filter(ImageFilter.FIND_EDGES)
    w, h = edges.size
    data = list(edges.getdata())
    cols = [sum(data[y * w + x] for y in range(h)) for x in range(w)]
    rows = [sum(data[y * w:(y + 1) * w]) for y in range(h)]
    x = _focus(cols, max(1, tw // 8), .5) * 8
    y = _focus(rows, max(1, th // 8), .38) * 8
    x = min(max(0, x), resized.width - tw)
    y = min(max(0, y), resized.height - th)
    return resized.crop((x, y, x + tw, y + th))


ART_BOX = (0, 0, 1000, 1010)   # picture area above the rules plate


def brighten(image, target=96):
    """Lift dark published stills so the subject reads on a small Discord card."""
    gray = image.convert("L").resize((64, 64))
    mean = ImageStat.Stat(gray).mean[0]
    if mean >= 72:
        return image
    image = ImageOps.autocontrast(image, cutoff=1, preserve_tone=True)
    mean = max(1.0, ImageStat.Stat(image.convert("L").resize((64, 64))).mean[0])
    if mean < target:
        gamma = math.log(target / 255) / math.log(mean / 255)
        lut = [round(255 * (i / 255) ** gamma) for i in range(256)]
        image = image.point(lut * 3)
    return image
KEEP_X = 0.9                  # wide shots: subjects sit anywhere across the frame
KEEP_Y = 0.72                 # tall shots: heads sit near the top


def place_art(image, size=SIZE, box=ART_BOX):
    """Blurred full-card backdrop plus the picture, cropped at most 10% wide / 28% tall.

    Wide or tall sources keep the reviewed subject visible instead of being
    zoomed into a slice; edges feather into the backdrop.
    """
    image = brighten(image.convert("RGB"))
    backdrop = ImageOps.fit(image, size, Image.Resampling.BILINEAR).filter(ImageFilter.GaussianBlur(28))
    backdrop = Image.blend(backdrop, Image.new("RGB", size, (6, 8, 12)), .45)
    bw, bh = box[2] - box[0], box[3] - box[1]
    iw, ih = image.size
    cover_scale = max(bw / iw, bh / ih)
    scale = min(cover_scale, bw / (KEEP_X * iw), bh / (KEEP_Y * ih))
    scale = max(scale, min(bw / iw, bh / ih))
    resized = image.resize((max(1, round(iw * scale)), max(1, round(ih * scale))), Image.Resampling.LANCZOS)
    if scale > 1.25:
        resized = resized.filter(ImageFilter.UnsharpMask(radius=2, percent=60, threshold=2))
    # Crop whatever exceeds the box: horizontally around the centre (lightly
    # steered by detail), vertically biased upward where heads usually are.
    cw, ch = min(bw, resized.width), min(bh, resized.height)
    x = (resized.width - cw) // 2
    y = 0  # tall images keep their top edge, where heads are
    x = min(max(0, x), resized.width - cw)
    tile = resized.crop((x, y, x + cw, y + ch))
    mask = Image.new("L", tile.size, 255)
    feather = 70
    md = ImageDraw.Draw(mask)
    for i in range(feather):
        a = round(255 * (i / feather) ** 1.6)
        if tile.height < bh:
            md.line((0, i, tile.width, i), fill=a)
            md.line((0, tile.height - 1 - i, tile.width, tile.height - 1 - i), fill=a)
        if tile.width < bw:
            md.line((i, 0, i, tile.height), fill=min(a, 255))
            md.line((tile.width - 1 - i, 0, tile.width - 1 - i, tile.height), fill=a)
    px = box[0] + (bw - cw) // 2
    py = box[1] + max(0, round((bh - ch) * .35))
    backdrop.paste(tile, (px, py), mask)
    return backdrop


# ---------- drawing helpers ----------

def _sigil(draw, cx, cy, r, shape, color):
    light = (246, 246, 240, 255)
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(*color, 255), outline=light, width=3)
    s = r * .55
    if shape in ("hexagon", "hexring"):
        pts = [(cx + s * math.cos(math.radians(60 * i + 30)), cy + s * math.sin(math.radians(60 * i + 30))) for i in range(6)]
        draw.polygon(pts, fill=light if shape == "hexagon" else None, outline=light, width=4)
        if shape == "hexring":
            draw.ellipse((cx - s * .35, cy - s * .35, cx + s * .35, cy + s * .35), fill=light)
    elif shape == "star":
        pts = []
        for i in range(10):
            rad = s if i % 2 == 0 else s * .45
            a = math.radians(-90 + 36 * i)
            pts.append((cx + rad * math.cos(a), cy + rad * math.sin(a)))
        draw.polygon(pts, fill=light)
    elif shape == "diamond":
        draw.polygon(((cx, cy - s), (cx + s * .8, cy), (cx, cy + s), (cx - s * .8, cy)), outline=light, width=4)
        draw.ellipse((cx - s * .25, cy - s * .25, cx + s * .25, cy + s * .25), fill=light)
    elif shape == "triangle":
        draw.polygon(((cx, cy - s), (cx + s * .9, cy + s * .65), (cx - s * .9, cy + s * .65)), outline=light, width=4)
    elif shape == "orbit":
        draw.ellipse((cx - s, cy - s * .45, cx + s, cy + s * .45), outline=light, width=3)
        draw.ellipse((cx - s * .3, cy - s * .3, cx + s * .3, cy + s * .3), fill=light)
    elif shape == "chevron":
        draw.line(((cx - s, cy - s * .3), (cx, cy + s * .5), (cx + s, cy - s * .3)), fill=light, width=6, joint="curve")
    elif shape == "leaf":
        draw.ellipse((cx - s * .5, cy - s, cx + s * .5, cy + s), outline=light, width=4)
        draw.line((cx, cy - s, cx, cy + s), fill=light, width=3)
    else:
        draw.ellipse((cx - s * .4, cy - s * .4, cx + s * .4, cy + s * .4), fill=light)


def _diamond(draw, x, y, r, fill, outline=None):
    pts = ((x, y - r), (x + r, y), (x, y + r), (x - r, y))
    draw.polygon(pts, fill=fill, outline=outline)


def _shade(size, top_alpha=170, bottom_start=760, bottom_alpha=235):
    shade = Image.new("L", (1, size[1]))
    values = []
    for y in range(size[1]):
        top = max(0.0, 1 - y / 330) ** 1.4 * top_alpha
        bottom = max(0.0, min(1.0, (y - bottom_start) / (size[1] - bottom_start - 120))) ** 1.2 * bottom_alpha
        values.append(round(max(top, bottom)))
    shade.putdata(values)
    alpha = shade.resize(size)
    layer = Image.new("RGBA", size, (4, 6, 10, 0))
    layer.putalpha(alpha)
    return layer


def _band(size, angle, position, width, color, alpha):
    """A soft diagonal light band; position in 0..1 across the card."""
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    w, h = size
    span = w + h
    center = -h * .5 + position * span * 1.2
    slope = math.tan(math.radians(angle))
    for i, scale in enumerate((1.0, .55, .25)):
        half = width * scale / 2
        pts = ((center - half, 0), (center + half, 0), (center + half - h * slope, h), (center - half - h * slope, h))
        d.polygon(pts, fill=(*color, round(alpha * (.35 + .3 * i))))
    return layer.filter(ImageFilter.GaussianBlur(width * .18))


def _iridescent(size, phase):
    stops = [(255, 120, 150), (255, 214, 140), (140, 255, 200), (130, 180, 255), (220, 140, 255), (255, 120, 150)]
    k = int(phase * (len(stops) - 1)) % (len(stops) - 1)
    rotated = stops[k:-1] + stops[:k] + [stops[k]]
    return _ramp(size, rotated, angle=35)


def _ring_mask(size, width, radius=44):
    mask = Image.new("L", size, 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    d.rounded_rectangle((width, width, size[0] - 1 - width, size[1] - 1 - width), radius=max(4, radius - width), fill=0)
    return mask


def _holo_border(size, phase, width, alpha=255):
    """Rainbow foil rim whose colors travel around the card as phase advances."""
    stops = [(255, 90, 140), (255, 200, 90), (120, 255, 170), (90, 190, 255), (200, 120, 255), (255, 90, 140)]
    shift = int(phase * 5) % 5
    stops = stops[shift:-1] + stops[:shift] + [stops[shift]]
    rim = _ramp(size, stops, angle=60 + phase * 120).convert("RGBA")
    mask = _ring_mask(size, width)
    rim.putalpha(mask.point(lambda v: v * alpha // 255))
    return rim


def _sparkles(size, seed, phase, count, color, scale=1.0, region=None):
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    rng = random.Random(seed)
    x0, y0, x1, y1 = region or (0, 0, size[0], size[1])
    for _ in range(count):
        x, y = rng.uniform(x0, x1), rng.uniform(y0, y1)
        offset, r = rng.random(), rng.uniform(6, 16) * scale
        a = max(0.0, math.sin(2 * math.pi * (phase + offset))) ** 2
        if a < .05:
            continue
        fill = (*color, round(235 * a))
        d.polygon(((x, y - r * 2.2), (x + r * .28, y - r * .28), (x + r * 2.2, y), (x + r * .28, y + r * .28),
                   (x, y + r * 2.2), (x - r * .28, y + r * .28), (x - r * 2.2, y), (x - r * .28, y - r * .28)), fill=fill)
        d.ellipse((x - r * .5, y - r * .5, x + r * .5, y + r * .5), fill=(255, 255, 255, round(255 * a)))
    return layer


def _rays(size, center, phase, count, color, alpha):
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, cy = center
    length = max(size) * 1.2
    for i in range(count):
        a = 2 * math.pi * (i / count + phase / count)
        spread = math.pi / count * .45
        d.polygon(((cx, cy), (cx + length * math.cos(a - spread), cy + length * math.sin(a - spread)),
                   (cx + length * math.cos(a + spread), cy + length * math.sin(a + spread))), fill=(*color, alpha))
    return layer.filter(ImageFilter.GaussianBlur(max(2, size[0] // 90)))


def _starfield(size, seed, count=260):
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    rng = random.Random(seed)
    for _ in range(count):
        x, y, r = rng.uniform(0, size[0]), rng.uniform(0, size[1]), rng.choice((1, 1, 1.5, 2, 3))
        d.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255, rng.randrange(90, 255)))
    return layer


def animated_layers(card, size, phase):
    """Everything that moves: drawn per frame on top of the static face."""
    rarity = card["rarity"]
    w, h = size
    k = w / SIZE[0]
    seed = card["id"] + STYLE
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    if rarity == "legendary":
        layer.alpha_composite(_band(size, 22, phase, 220 * k, (255, 244, 205), 95))
        layer.alpha_composite(_sparkles(size, seed, phase, 8, (255, 230, 160), k, (0, 0, w, h * .7)))
    elif rarity == "mythic":
        tint = _iridescent(size, phase).convert("RGBA")
        tint.putalpha(20)
        layer.alpha_composite(tint)
        layer.alpha_composite(_holo_border(size, phase, round(14 * k)))
        layer.alpha_composite(_band(size, 30, phase, 200 * k, (255, 255, 255), 85))
        layer.alpha_composite(_sparkles(size, seed, phase, 22, (255, 200, 215), k, (0, 0, w, h * .72)))
    elif rarity == "celestial":
        layer.alpha_composite(_rays(size, (w * .5, h * .3), phase, 14, (255, 236, 190), 12))
        layer.alpha_composite(_holo_border(size, phase, round(10 * k), 230))
        layer.alpha_composite(_band(size, 26, phase, 240 * k, (255, 250, 230), 100))
        layer.alpha_composite(_sparkles(size, seed, phase, 30, (255, 244, 210), 1.15 * k, (0, 0, w, h * .7)))
    return layer


# ---------- composition ----------

def _frame(card, tier, phase):
    rarity = card["rarity"]
    seed = hashlib.sha256((card["id"] + STYLE).encode()).digest()
    if rarity == "common":
        return _ramp(SIZE, [(116, 128, 138), (82, 92, 102), (70, 79, 88)], angle=70)
    if rarity == "uncommon":
        return _brushed(SIZE, (58, 108, 101), seed)
    if rarity == "rare":
        return _ramp(SIZE, [(44, 92, 168), (186, 228, 255), (60, 116, 196), (168, 214, 255), (42, 88, 160)], angle=45)
    if rarity == "epic":
        shift = int(phase * 5)
        stops = [(139, 108, 255), (111, 224, 255), (255, 139, 216), (180, 155, 255), (111, 255, 208)]
        stops = stops[shift % 5:] + stops[:shift % 5]
        return _ramp(SIZE, stops + [stops[0]], angle=55)
    if rarity == "legendary":
        return _ramp(SIZE, [(110, 77, 23), (246, 221, 139), (156, 116, 38), (255, 241, 184), (138, 100, 32), (232, 196, 106)], angle=40)
    if rarity == "celestial":
        nebula = _ramp(SIZE, [(16, 8, 40), (70, 30, 120), (20, 50, 110), (110, 40, 120), (14, 10, 40)], angle=65).convert("RGBA")
        nebula.alpha_composite(_starfield(SIZE, card["id"]))
        return nebula.convert("RGB")
    return Image.new("RGB", SIZE, (0, 0, 0))


def _window_mask(card, inset):
    w, h = SIZE
    mask = Image.new("L", SIZE, 0)
    d = ImageDraw.Draw(mask)
    if card["rarity"] == "mythic":
        d.rounded_rectangle((0, 0, w - 1, h - 1), radius=44, fill=255)
    elif card["rarity"] == "epic":
        d.rounded_rectangle((inset, inset + 120, w - 1 - inset, h - 1 - inset), radius=22, fill=255)
        d.ellipse((inset, inset, w - 1 - inset, inset + 300), fill=255)
        d.rectangle((inset, inset + 150, w - 1 - inset, inset + 200), fill=255)
    else:
        d.rounded_rectangle((inset, inset, w - 1 - inset, h - 1 - inset), radius=24, fill=255)
    return mask


def compose_tier(renderer, card, art, *, phase=0.25, glint=True):
    """Return an RGBA card face. `art` is the subject region (any size)."""
    rarity = card["rarity"]
    tier = TIERS[rarity]
    ink = tier["ink"]
    paper = (246, 244, 236)
    inset = tier["frame"]
    w, h = SIZE
    special = bool(card.get("release") or card.get("event_id") or card.get("category") in ("limited", "legends"))
    faction_color, sigil = FACTIONS.get(card.get("faction"), DEFAULT_FACTION)

    canvas = _frame(card, tier, phase).convert("RGBA")
    pad = max(inset, 24) + 30
    top = pad + (120 if rarity == "epic" else 0)
    title = re.sub(r"\s+", " ", str(card["name"])).upper()
    title_font, lines = renderer._wrap(title, 680 - (40 if rarity == "epic" else 0), 100, 2, 40, "heading")
    title_bottom = top + len(lines) * (title_font.size + 2) + 40   # includes the arena/role line
    # The picture starts just under the title so names never cover faces.
    filled = place_art(art, box=(0, max(0, title_bottom - 24), w, 1000)).convert("RGBA")
    # Art-only finishes sit under the text shading so labels stay crisp.
    if rarity == "rare":
        filled = Image.alpha_composite(filled, _band(SIZE, 28, phase, 260, (190, 235, 255), 120))
    elif rarity == "epic":
        stripes = Image.new("RGBA", SIZE, (0, 0, 0, 0))
        sd = ImageDraw.Draw(stripes)
        for i, color in enumerate(((255, 140, 220), (110, 224, 255), (180, 155, 255)) * 14):
            x = -400 + i * 46 + int(phase * 46)
            sd.polygon(((x, 0), (x + 22, 0), (x + 22 + 700, h), (x + 700, h)), fill=(*color, 8))
        filled = Image.alpha_composite(filled, stripes)
    elif rarity == "mythic":
        foil = _iridescent(SIZE, phase).convert("RGB")
        filled = Image.blend(filled.convert("RGB"), ImageChops.screen(filled.convert("RGB"), foil), .12).convert("RGBA")
    elif rarity == "celestial":
        gold = Image.new("RGB", SIZE, (255, 226, 160))
        filled = Image.blend(filled.convert("RGB"), ImageChops.screen(filled.convert("RGB"), gold), .08).convert("RGBA")
        filled = Image.alpha_composite(filled, _rays(SIZE, (500, 360), .2, 14, (255, 240, 200), 6))
    filled = Image.alpha_composite(filled, _shade(SIZE))
    canvas.paste(filled, (0, 0), _window_mask(card, inset))
    draw = ImageDraw.Draw(canvas)

    # Frame details per tier.
    if rarity == "common":
        draw.rectangle((inset, inset + 24, inset + 9, h - inset - 24), fill=(*faction_color, 255))
        draw.rounded_rectangle((inset, inset, w - 1 - inset, h - 1 - inset), radius=24, outline=(255, 255, 255, 40), width=2)
    elif rarity == "uncommon":
        draw.rounded_rectangle((inset, inset, w - 1 - inset, h - 1 - inset), radius=24, outline=(143, 208, 195, 255), width=4)
        ring = Image.new("RGBA", SIZE, (0, 0, 0, 0))
        ImageDraw.Draw(ring).ellipse((620, 640, 1080, 1100), outline=(143, 208, 195, 46), width=30)
        canvas.alpha_composite(ring)
    elif rarity == "rare":
        draw.rounded_rectangle((inset, inset, w - 1 - inset, h - 1 - inset), radius=24, outline=(214, 240, 255, 230), width=4)
    elif rarity == "epic":
        outline = _window_mask(card, inset).filter(ImageFilter.FIND_EDGES).point(lambda v: 255 if v > 40 else 0)
        glow = Image.new("RGBA", SIZE, (244, 232, 255, 0))
        glow.putalpha(outline.filter(ImageFilter.MaxFilter(5)))
        canvas.alpha_composite(glow)
    elif rarity == "legendary":
        draw.rounded_rectangle((inset, inset, w - 1 - inset, h - 1 - inset), radius=24, outline=(255, 233, 163, 255), width=4)
        draw.rounded_rectangle((inset - 7, inset - 7, w + 6 - inset, h + 6 - inset), radius=30, outline=(90, 60, 10, 160), width=2)
        etch = "KYBERDEX ARCHIVE  ✦  " * 8
        font = renderer._font(17, "heading")
        draw.text((w / 2, 9), etch, font=font, fill=(70, 45, 5, 175), anchor="mt")
        draw.text((w / 2, h - 10), etch, font=font, fill=(70, 45, 5, 175), anchor="mb")
    elif rarity == "celestial":
        for i, color in enumerate(((255, 246, 214), (214, 170, 90), (255, 236, 180))):
            draw.rounded_rectangle((inset - 2 - i * 3, inset - 2 - i * 3, w + 1 - inset + i * 3, h + 1 - inset + i * 3),
                                   radius=26 + i * 3, outline=(*color, 255), width=2)
        for cx, cy in ((inset, inset), (w - inset, inset), (inset, h - inset), (w - inset, h - inset)):
            _diamond(draw, cx, cy, 16, (255, 246, 214, 255), (150, 110, 40, 255))
        etch = "CELESTIAL VAULT  ✦  STAFF HONOR  ✦  " * 6
        font = renderer._font(16, "heading")
        draw.text((w / 2, 8), etch, font=font, fill=(255, 236, 190, 200), anchor="mt")
        draw.text((w / 2, h - 9), etch, font=font, fill=(255, 236, 190, 200), anchor="mb")
    elif rarity == "mythic":
        aura = Image.new("RGBA", SIZE, (255, 70, 120, 0))
        aura.putalpha(_ring_mask(SIZE, 46).filter(ImageFilter.GaussianBlur(26)).point(lambda v: v * 3 // 4))
        canvas.alpha_composite(aura)
        draw = ImageDraw.Draw(canvas)
        crack = Image.new("RGBA", SIZE, (0, 0, 0, 0))
        cd = ImageDraw.Draw(crack)
        rng = random.Random(card["id"] + "crack")
        for x, y, inward in ((0, 430, 1), (w, 390, -1), (0, 760, 1), (w, 800, -1)):
            for _ in range(4):
                nx, ny = x + inward * rng.uniform(18, 38), y + rng.uniform(-55, 55)
                cd.line((x, y, nx, ny), fill=(255, 150, 190, 210), width=3)
                x, y = nx, ny
        canvas.alpha_composite(crack.filter(ImageFilter.GaussianBlur(4)))
        canvas.alpha_composite(crack)
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle((1, 1, w - 2, h - 2), radius=44, outline=(255, 236, 240, 200), width=3)
        rng = random.Random(card["id"])
        for _ in range(5):
            x, y, s = rng.randrange(60, 940), rng.randrange(380, 900), rng.uniform(.6, 1.2)
            shard = Image.new("RGBA", SIZE, (0, 0, 0, 0))
            ImageDraw.Draw(shard).polygon(((x, y - 34 * s), (x + 12 * s, y - 6 * s), (x, y + 30 * s), (x - 12 * s, y - 6 * s)),
                                          fill=(255, 190, 205, 230))
            canvas.alpha_composite(shard.filter(ImageFilter.GaussianBlur(5)))
            canvas.alpha_composite(shard)
        draw = ImageDraw.Draw(canvas)

    def text(value, xy, width, size, role="body", fill=paper, minimum=14, anchor="lt", stroke=0):
        font = renderer._fit(str(value), width, size, minimum, role)
        draw.text(xy, str(value), font=font, fill=fill, anchor=anchor,
                  stroke_width=stroke, stroke_fill=(0, 0, 0, 200) if stroke else None)
        return font

    # Title block.
    y = top - 6
    glow_color = {"legendary": (255, 210, 120), "mythic": (255, 110, 150), "celestial": (255, 236, 170)}.get(rarity)
    if glow_color:
        glow = Image.new("RGBA", SIZE, (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        gy = y
        for line in lines:
            gd.text((pad, gy), line, font=title_font, fill=(*glow_color, 255), anchor="lt", stroke_width=8, stroke_fill=(*glow_color, 255))
            gy += title_font.size + 2
        glow = glow.filter(ImageFilter.GaussianBlur(14))
        canvas.alpha_composite(glow)
        if rarity != "legendary":
            canvas.alpha_composite(glow)
        draw = ImageDraw.Draw(canvas)
    title_fill = (255, 246, 214) if rarity == "celestial" else paper
    for line in lines:
        draw.text((pad, y), line, font=title_font, fill=title_fill, anchor="lt", stroke_width=3, stroke_fill=(0, 0, 0, 170))
        y += title_font.size + 2
    text(f"{card['arena'].upper()} · {card['role'].upper()}", (pad + 2, y + 6), 600, 24, fill=(*ink, 255), stroke=2)
    text("HP", (w - pad, top - 2), 120, 22, "heading", (*ink, 255), anchor="rt", stroke=2)
    text(str(card["health"]), (w - pad, top + 22), 220, 82, "heading", paper, anchor="rt", stroke=3)

    # Edition plaque.
    plate_top = 1000
    if special:
        release = card.get("release") or {}
        cap = release.get("supply_cap")
        stamp = "EVENT EXCLUSIVE" if card.get("event_id") or card.get("category") == "legends" else "LIMITED EDITION"
        if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0:
            stamp += f"  ·  {cap} COPIES"
        plaque = _ramp((w - 2 * pad, 46), [(172, 135, 85), (255, 240, 184), (164, 213, 210), (219, 195, 240)], angle=0).convert("RGBA")
        mask = Image.new("L", plaque.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, plaque.width - 1, plaque.height - 1), radius=12, fill=255)
        canvas.paste(plaque, (pad, plate_top - 62), mask)
        draw = ImageDraw.Draw(canvas)
        text(stamp, (w / 2, plate_top - 39), plaque.width - 40, 26, "heading", (24, 32, 38), anchor="mm")

    # Rules plate.
    left, right, bottom = pad - 14, w - pad + 14, h - max(inset, 24) - 18
    plate = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    pd = ImageDraw.Draw(plate)
    fill = {"legendary": (30, 20, 5, 220), "mythic": (14, 6, 10, 170), "celestial": (18, 10, 36, 200)}.get(rarity, (8, 12, 18, 205))
    pd.rounded_rectangle((left, plate_top, right, bottom), radius=22, fill=fill, outline=(*ink, 120), width=2)
    canvas.alpha_composite(plate)
    draw = ImageDraw.Draw(canvas)
    _sigil(draw, left + 46, plate_top + 46, 28, sigil, faction_color)
    text(str(card["faction"]).upper(), (left + 88, plate_top + 22), 520, 30, "heading", paper)
    text(str(card.get("era") or "").upper(), (left + 89, plate_top + 56), 520, 20, "body", (*ink, 230))
    ability = str(card.get("ability_name") or "Role ability").upper()
    text(ability, (left + 26, plate_top + 98), 560, 40, "heading", (*ink, 255))
    description = re.sub(r"^PvP:\s*", "", str(card.get("ability_description") or ""))
    body_font, body_lines = renderer._wrap(description, 590, 26, 3, 17)
    for i, line in enumerate(body_lines):
        draw.text((left + 27, plate_top + 146 + i * (body_font.size + 6)), line, font=body_font, fill=(226, 232, 236), anchor="lt")
    draw.line((right - 190, plate_top + 98, right - 190, plate_top + 240), fill=(*ink, 90), width=2)
    text("ATK", (right - 26, plate_top + 98), 140, 22, "heading", (*ink, 255), anchor="rt")
    text(str(card["attack"]), (right - 26, plate_top + 124), 160, 80, "heading", paper, anchor="rt")
    row = bottom - 54
    draw.line((left + 24, row - 14, right - 24, row - 14), fill=(255, 255, 255, 40), width=2)
    x = left + 38
    for _ in range(tier["pips"]):
        _diamond(draw, x, row + 18, 10, (*ink, 255))
        x += 26
    text(tier["label"], (x + 6, row + 2), 260, 34, "heading", (*ink, 255))
    text(f"SPD {card['speed']}  ·  {str(card.get('set') or 'KYBERDEX').upper()}", (right - 26, row + 6), 420, 22,
         "body", (200, 210, 216), anchor="rt")

    # Moving light (frozen at one phase for still images).
    if glint and rarity in ANIMATED:
        canvas.alpha_composite(animated_layers(card, SIZE, phase))
    outer = Image.new("L", SIZE, 0)
    ImageDraw.Draw(outer).rounded_rectangle((0, 0, w - 1, h - 1), radius=44, fill=255)
    canvas.putalpha(ImageChops.multiply(canvas.getchannel("A"), outer))
    return canvas


def render_png(renderer, card, art):
    face = compose_tier(renderer, card, art)
    out = io.BytesIO()
    face.save(out, format="PNG", compress_level=4)
    return out.getvalue()


def render_gif(renderer, card, art, *, frames=8, width=480):
    """A looping animation: one static composition plus per-frame light and foil."""
    size = (width, round(width * SIZE[1] / SIZE[0]))
    base = compose_tier(renderer, card, art, glint=False).resize(size, Image.Resampling.LANCZOS)
    alpha = base.getchannel("A")
    backdrop = Image.new("RGBA", size, (10, 12, 16, 255))
    images = []
    for i in range(frames):
        face = base.copy()
        moving = animated_layers(card, size, i / frames)
        moving.putalpha(ImageChops.multiply(moving.getchannel("A"), alpha))
        face.alpha_composite(moving)
        images.append(Image.alpha_composite(backdrop, face).convert("RGB"))
    # One shared palette sampled from several frames keeps the moving rainbow stable.
    sample = Image.new("RGB", (size[0], size[1] * 3))
    for row, index in enumerate((0, frames // 3, 2 * frames // 3)):
        sample.paste(images[index], (0, row * size[1]))
    palette = sample.quantize(colors=255, method=Image.Quantize.MEDIANCUT)
    quantized = [image.quantize(palette=palette, dither=Image.Dither.FLOYDSTEINBERG) for image in images]
    out = io.BytesIO()
    quantized[0].save(out, format="GIF", save_all=True, append_images=quantized[1:],
                      duration=90, loop=0, optimize=False, disposal=1)
    return out.getvalue()


def prepare_art(source, entry):
    return subject_region(ImageOps.exif_transpose(source).convert("RGB"), entry)
