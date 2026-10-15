"""Hyperspace Foil collectible faces from public definitions and verified font bytes.

Encounter prompts deliberately do not use these named card faces. The renderer
has no database, ownership, networking, or credential access.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import random
import threading
from collections import OrderedDict
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .presentation import art_path

SIZE = (1000, 1400)
LEGACY_VERSION = "hyperspace-foil-2.3"
CARD_STYLE = os.environ.get("KYBERDEX_CARD_STYLE", "kyber-tiers").strip() or "kyber-tiers"
VERSION = "kyber-tiers-1" if CARD_STYLE != "hyperspace-foil" else LEGACY_VERSION
# Layout revisions invalidate the render cache without moving existing glints.
FINISH_SEED = "hyperspace-foil-2.1"
DISPLAY_FIELDS = (
    "id", "name", "rarity", "category", "availability", "event_id", "release", "arena", "role",
    "faction", "era", "set", "attack", "health", "speed", "ability_name", "ability_description", "ability",
    "art_layout",
)
FINISHES = {
    "common": {"level": 0, "rim": 14, "sheen": 0, "stars": 0, "etch": 0,
               "colors": ("#5e7180", "#b9c4c8", "#607481"), "ink": "#d4e0e5"},
    "uncommon": {"level": 1, "rim": 17, "sheen": 18, "stars": 1, "etch": 1,
                 "colors": ("#357c70", "#c0e9c9", "#548e86"), "ink": "#aee1c8"},
    "rare": {"level": 2, "rim": 23, "sheen": 55, "stars": 4, "etch": 2,
             "colors": ("#417ec4", "#d5f6ff", "#618fd2", "#a2cee5"), "ink": "#b7dafa"},
    "epic": {"level": 3, "rim": 28, "sheen": 95, "stars": 8, "etch": 3,
             "colors": ("#7965b4", "#e6d4ff", "#8bcdd6", "#a79ed5"), "ink": "#d8c9fb"},
    "legendary": {"level": 4, "rim": 36, "sheen": 150, "stars": 14, "etch": 4,
                  "colors": ("#806025", "#fff0ac", "#b6dcda", "#bb9cdd", "#ffe0a0"), "ink": "#f8e3a2"},
    "mythic": {"level": 5, "rim": 44, "sheen": 180, "stars": 22, "etch": 5,
               "colors": ("#91ddeb", "#f8e8bf", "#db9dac", "#c0b0ed", "#e8ffdf"), "ink": "#ffecc7"},
    "celestial": {"level": 5, "rim": 48, "sheen": 210, "stars": 30, "etch": 5,
                  "colors": ("#fff6d8", "#c8b8ff", "#9be8ff", "#ffd1a8", "#ffffff"), "ink": "#fff6dd"},
}


def is_special_edition(card):
    return bool(card.get("release") or card.get("event_id") or
                card.get("category") in ("limited", "legends") or card.get("availability") == "event")


def _rgb(value):
    return tuple(bytes.fromhex(value.lstrip("#")))


def _gradient(size, palette):
    """Small color field enlarged in C, avoiding a full-canvas Python pixel loop."""
    colors = [_rgb(c) for c in palette]
    tile = Image.new("RGB", (96, 128))
    pixels = []
    for y in range(128):
        for x in range(96):
            position = ((x / 95 * .62 + y / 127 * .85) % 1) * (len(colors) - 1)
            index = min(int(position), len(colors) - 2)
            fraction = position - index
            pixels.append(tuple(round(a * (1 - fraction) + b * fraction)
                                for a, b in zip(colors[index], colors[index + 1])))
    tile.putdata(pixels)
    return tile.resize(size, Image.Resampling.BILINEAR)


def _diamond(draw, x, y, radius, fill, outline=None, width=1):
    points = ((x, y - radius), (x + radius, y), (x, y + radius), (x - radius, y))
    draw.polygon(points, fill=fill)
    if outline:
        draw.line((*points, points[0]), fill=outline, width=width, joint="curve")


class CardRenderer:
    """A single render lane and byte-bounded LRU fit the Pi's memory budget."""

    def __init__(self, root: Path, max_entries=48, max_bytes=32 * 1024 * 1024):
        if max_entries < 0 or max_bytes < 0:
            raise ValueError("Cache bounds must be nonnegative")
        self.root = Path(root).resolve()
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self._cache = OrderedDict()
        self._cache_bytes = 0
        self._lock = threading.Lock()
        self._fonts = {}
        self._font_data = None
        self._finish_texture = None
        self._art_manifest = (None, {})

    @property
    def cache_info(self):
        with self._lock:
            return {"entries": len(self._cache), "bytes": self._cache_bytes,
                    "max_entries": self.max_entries, "max_bytes": self.max_bytes}

    def _load_fonts(self):
        if self._font_data is not None:
            return
        base = (self.root / "assets/fonts").resolve()
        manifest = json.loads((base / "manifest.json").read_text())
        paths = {}
        for item in manifest["fonts"]:
            role = item["role"]
            if role not in ("heading", "body"):
                continue
            path = (base / item["file"]).resolve()
            if not path.is_relative_to(base) or not path.is_file():
                raise ValueError("Card font path is unavailable")
            verified_bytes = path.read_bytes()
            if hashlib.sha256(verified_bytes).hexdigest() != item["sha256"]:
                raise ValueError("Card font hash mismatch")
            if role in paths:
                raise ValueError("Duplicate card font role")
            paths[role] = verified_bytes
        if paths.keys() != {"heading", "body"}:
            raise ValueError("Verified card heading and body fonts are required")
        self._font_data = paths

    def _font(self, size, role="body"):
        key = (size, role)
        if key not in self._fonts:
            self._fonts[key] = ImageFont.truetype(io.BytesIO(self._font_data[role]), size=size)
        return self._fonts[key]

    def _foil_overlay(self):
        """Load one verified immutable finish texture, shared by showcase faces."""
        if self._finish_texture is None:
            base = (self.root / "assets/finishes").resolve()
            record = json.loads((base / "manifest.json").read_text())
            path = (base / record["file"]).resolve()
            if not path.is_relative_to(base) or not path.is_file():
                raise ValueError("Card finish texture is unavailable")
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != record["sha256"]:
                raise ValueError("Card finish texture hash mismatch")
            with Image.open(io.BytesIO(data)) as image:
                if image.size != (1024, 1536) or image.mode != "RGBA":
                    raise ValueError("Card finish texture format mismatch")
                image.load()
                self._finish_texture = image.resize(SIZE, Image.Resampling.LANCZOS)
        return self._finish_texture.copy()

    def verify_assets(self):
        """Fail before gateway startup if the required print assets are broken."""
        with self._lock:
            self._load_fonts()
            for role in ("heading", "body"):
                self._font(24, role)
            self._foil_overlay()

    def _fit(self, text, width, size, minimum=16, role="body"):
        for current in range(size, minimum - 1, -1):
            font = self._font(current, role)
            if font.getlength(text) <= width:
                return font
        raise ValueError("Card label exceeds its printable area")

    def _wrap(self, text, width, size, max_lines, minimum=18, role="body"):
        for current in range(size, minimum - 1, -1):
            font = self._font(current, role)
            lines = []
            for paragraph in str(text).splitlines() or [""]:
                line = ""
                for word in paragraph.split():
                    if font.getlength(word) > width:
                        break
                    candidate = (line + " " + word).strip()
                    if line and font.getlength(candidate) > width:
                        lines.append(line)
                        line = word
                    else:
                        line = candidate
                else:
                    if line:
                        lines.append(line)
                    continue
                break
            else:
                if len(lines) <= max_lines:
                    return font, lines
        raise ValueError("Card text exceeds its printable area")

    def _manifest_entry(self, card_id):
        """Reviewed art metadata (placement boxes) for full-bleed crops; optional."""
        path = self.root / "assets/art/manifest.json"
        try:
            stamp = path.stat().st_mtime_ns
            if self._art_manifest[0] != stamp:
                document = json.loads(path.read_text())
                self._art_manifest = (stamp, document.get("assets", {}) if isinstance(document, dict) else {})
        except (OSError, ValueError):
            return None
        entry = self._art_manifest[1].get(card_id)
        return entry if isinstance(entry, dict) else None

    def media_kind(self, card):
        if CARD_STYLE == "hyperspace-foil":
            return "png"
        from .tier_render import ANIMATED
        return "gif" if card.get("rarity") in ANIMATED else "png"

    def render(self, card, *, kind="png"):
        # Validate readiness before cache access: withdrawn art cannot be served
        # just because a previously approved render remains in memory.
        if card.get("rarity") not in FINISHES:
            raise ValueError("Unknown card rarity")
        if kind not in ("png", "gif"):
            raise ValueError("Unknown card image kind")
        with self._lock:
            path = art_path(self.root, card)
            stamp = path.stat()
            payload = {key: card.get(key) for key in DISPLAY_FIELDS}
            key = (str(path), stamp.st_mtime_ns, stamp.st_ctime_ns, stamp.st_size,
                   json.dumps(payload, sort_keys=True, ensure_ascii=False), VERSION, kind)
            if key in self._cache:
                result = self._cache.pop(key)
                self._cache[key] = result
                return result
            self._load_fonts()
            with Image.open(path) as source:
                if source.width * source.height > 16_777_216 or source.width < 1 or source.height < 1:
                    raise ValueError("Card source image exceeds render bounds")
                source.load()
                if CARD_STYLE != "hyperspace-foil":
                    from . import tier_render
                    subject = tier_render.prepare_art(source, self._manifest_entry(card.get("id")))
                    result = (tier_render.render_gif(self, card, subject) if kind == "gif"
                              else tier_render.render_png(self, card, subject))
                else:
                    # Approved silhouettes can extend to the source edge. Preserve
                    # the entire illustration; the narrow side gutters join the rim.
                    fitted = ImageOps.contain(source.convert("RGB"), SIZE, Image.Resampling.LANCZOS)
                    art = Image.new("RGB", SIZE, (2, 18, 36))
                    art.paste(fitted, ((SIZE[0]-fitted.width)//2, (SIZE[1]-fitted.height)//2))
                    result = None
            if result is None:
                result = self._compose(card, art)
            if self.max_entries and len(result) <= self.max_bytes:
                while self._cache and (len(self._cache) >= self.max_entries or
                                       self._cache_bytes + len(result) > self.max_bytes):
                    _, removed = self._cache.popitem(last=False)
                    self._cache_bytes -= len(removed)
                self._cache[key] = result
                self._cache_bytes += len(result)
            return result

    def _compose(self, card, art):
        profile = FINISHES[card["rarity"]]
        special = is_special_edition(card)
        if profile["level"] >= 3 or special:
            from .showcase_render import compose_showcase
            return compose_showcase(self, card, art, profile, special)
        title_font, title_lines = self._wrap(card["name"].upper(), 680, 112, 2, 38, "heading")
        if card.get("art_layout") == "published-window":
            # These source assets already reserve the title and rules areas.
            # A second portrait fit would shrink and lower the reviewed image.
            pass
        elif card.get("arena") == "ground":
            from .showcase_render import fit_ground_art
            art = fit_ground_art(art, 117 + len(title_lines) * (title_font.size+1))
        elif card.get("arena") == "fleet":
            from .showcase_render import fit_fleet_art
            art = fit_fleet_art(art, 117 + len(title_lines) * (title_font.size+1))
        rim = profile["rim"] + (4 if special else 0)
        ink = _rgb(profile["ink"])
        paper = (242, 243, 213)
        canvas = _gradient(SIZE, profile["colors"]).convert("RGBA")
        rounded = Image.new("L", SIZE)
        ImageDraw.Draw(rounded).rounded_rectangle((rim, rim, 999-rim, 1399-rim), radius=32, fill=255)
        canvas.paste(art, (0, 0), rounded)
        # Darken only the printed name/rules zones; retain the art's full color.
        shade = Image.new("RGBA", SIZE)
        sd = ImageDraw.Draw(shade)
        for y in range(1400):
            top = max(0, 1 - y / 440) * 175
            bottom = max(0, min(1, (y-815)/330)) * 245
            sd.line((rim, y, 999-rim, y), fill=(2, 18, 36, int(max(top, bottom))))
        canvas.alpha_composite(shade)
        effect = Image.new("RGBA", SIZE)
        ed = ImageDraw.Draw(effect)
        strength = profile["sheen"] + (12 if special else 0)
        if strength:
            ed.polygon(((190, 0), (380, 0), (1000, 1100), (1000, 1390)), fill=(179, 233, 255, strength//2))
            ed.polygon(((50, 0), (105, 0), (880, 1400), (790, 1400)), fill=(244, 243, 191, strength//3))
        # Foil etching stays on the perimeter, clear of names and rules.
        for n in range(profile["etch"] + (1 if special else 0)):
            inset = rim + 7 + n * 7
            ed.rounded_rectangle((inset, inset, 999-inset, 1399-inset), radius=max(12, 29-n*4),
                                 outline=(*ink, max(35, 125-n*24)), width=1)
        if profile["level"] >= 4:
            for corner in (False, True):
                for n in range(4 if profile["level"] == 4 else 7):
                    y = 330 + n * 70
                    x = 46 if not corner else 954
                    ed.line((x-9, y-24, x+9, y+24), fill=(*ink, 150), width=2)
                    ed.line((x+9, y-24, x-9, y+24), fill=(*ink, 95), width=1)
        rng = random.Random(hashlib.sha256((str(card["id"])+FINISH_SEED).encode()).digest())
        for n in range(profile["stars"] + (6 if special else 0)):
            x = rng.choice((rng.randrange(52, 100), rng.randrange(900, 948)))
            y = rng.randrange(335, 915)
            radius = 10 + n % 4 * 3
            ed.line((x-radius*2, y, x+radius*2, y), fill=(*ink, 170), width=1)
            ed.line((x, y-radius*2, x, y+radius*2), fill=(245, 253, 244, 190), width=1)
            _diamond(ed, x, y, radius, (*ink, 190))
        if profile["level"] == 5:
            # Mythic adds a restrained etched orbit, rather than obscuring art.
            for offset in (0, 17):
                ed.arc((45+offset, 304+offset, 955-offset, 974-offset), 195, 344,
                       fill=(*ink, 105), width=2)
        canvas.alpha_composite(effect)
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle((2, 2, 997, 1397), radius=43, outline=(218, 240, 227, 200), width=2)
        draw.rounded_rectangle((rim, rim, 999-rim, 1399-rim), radius=32, outline=(*ink, 180), width=2)
        # A clean bevel and a printed inner rule give even ordinary cards a
        # physical edge; colored foiling builds on that material foundation.
        draw.rounded_rectangle((6, 6, 993, 1393), radius=40, outline=(250, 255, 248, 160), width=2)
        draw.rounded_rectangle((rim+3, rim+3, 996-rim, 1396-rim), radius=29,
                               outline=(1, 12, 23, 235), width=3)
        for y, direction in ((91, 1), (1283, -1)):
            draw.line((58, y, 150, y), fill=(*ink, 190), width=2)
            draw.line((850, y, 942, y), fill=(*ink, 190), width=2)
            draw.line((58, y, 58, y+direction*16), fill=(*ink, 190), width=2)
            draw.line((942, y, 942, y+direction*16), fill=(*ink, 190), width=2)

        def line(text, xy, width, size, role="body", color=paper, minimum=16, anchor="lt"):
            font = self._fit(str(text), width, size, minimum, role)
            draw.text(xy, str(text), font=font, fill=color, anchor=anchor)

        line("KYBERDEX", (60, 54), 410, 24, color=(190, 223, 217))
        line(f"{card['arena'].upper()} / {card['role'].upper()}", (940, 54), 445, 22,
             color=(190, 223, 217), anchor="rt")
        for index, text in enumerate(title_lines):
            draw.text((61, 121+index*(title_font.size+1)), text, font=title_font,
                      fill=(1, 10, 21), anchor="lt", stroke_width=2)
            draw.text((58, 117+index*(title_font.size+1)), text, font=title_font, fill=paper,
                      anchor="lt", stroke_width=1, stroke_fill=(3, 20, 37))
        line("HP", (940, 121), 170, 22, "heading", ink, anchor="rt")
        line(str(card["health"]), (940, 154), 180, 78, "heading", ink, anchor="rt")
        # Repeated pips visibly count progression as well as changing foil color.
        for n in range(profile["level"]+1):
            _diamond(draw, 931-n*26, 276, 7, ink, (249, 250, 217), 1)

        # Print label and highlight on one ribbon, then tilt that entire piece.
        # Every card therefore keeps its text parallel to both ribbon edges.
        from .showcase_render import affiliation_ribbon
        band = affiliation_ribbon(self, card, ink, 882, math.degrees(math.atan2(44, 880)))
        canvas.alpha_composite(band, ((1000-band.width)//2, 929-band.height//2))
        draw = ImageDraw.Draw(canvas)

        ability_name = card.get("ability_name") or "ROLE ABILITY"
        line(str(ability_name).upper(), (66, 1011), 645, 45, "heading")
        line("ATK", (799, 1008), 141, 21, "heading", (190, 223, 217))
        line(str(card["attack"]), (799, 1043), 141, 72, "heading", ink)
        draw.line((776, 1006, 776, 1129), fill=(152, 181, 182), width=1)
        ability = card.get("ability_description") or "Role ability. See /vault card for details."
        body_font, body_lines = self._wrap(str(ability), 674, 30, 3, 22)
        for index, text in enumerate(body_lines):
            draw.text((67, 1070+index*(body_font.size+8)), text, font=body_font,
                      fill=(226, 236, 226), anchor="lt")
        draw.line((67, 1194, 933, 1194), fill=(110, 139, 146), width=1)
        line(f"SPEED  {card['speed']}", (67, 1215), 280, 24, "heading", ink)
        line("FULL ART" if profile["level"] < 2 else "HYPERSPACE FOIL", (933, 1215), 430, 21,
             color=(185, 212, 210), anchor="rt")
        if special:
            badge = _gradient((863, 42), ("#ac8755", "#fff0b8", "#a4d5d2", "#dbc3f0")).convert("RGBA")
            canvas.alpha_composite(badge, (68, 1257))
            draw = ImageDraw.Draw(canvas)
            release = card.get("release") or {}
            cap = release.get("supply_cap")
            stamp = "EVENT EXCLUSIVE" if card.get("event_id") or card.get("category") == "legends" else "LIMITED EDITION"
            line(stamp, (83, 1267), 495, 24, "heading", (20, 35, 40))
            if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0:
                line(f"EDITION CAP {cap}", (913, 1268), 350, 21, "heading", (20, 35, 40), anchor="rt")
        _diamond(draw, 79, 1332, 12, ink, (235, 247, 219), 1)
        line(card["rarity"].upper(), (104, 1318), 425, 35, "heading", ink)
        line(str(card.get("set") or "CORE ARCHIVE").upper(), (932, 1322), 467, 22,
             color=(211, 224, 210), anchor="rt")
        outer = Image.new("L", SIZE)
        ImageDraw.Draw(outer).rounded_rectangle((0, 0, 999, 1399), radius=44, fill=255)
        canvas.putalpha(outer)
        output = io.BytesIO()
        canvas.save(output, format="PNG", compress_level=4)
        return output.getvalue()
