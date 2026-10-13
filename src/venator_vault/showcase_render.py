"""Dramatic printed-card composition for upper rarities and special editions.

All artwork and fonts arrive through CardRenderer's verified asset boundary.
This module draws a public definition only; it never reads an owned instance.
"""
from __future__ import annotations

import io
import math

from PIL import Image, ImageChops, ImageDraw, ImageFilter

SIZE = (1000, 1400)
GOLD = ("#5c2d07", "#fbc54c", "#fffddd", "#da901d", "#fff1a1")
PRISM = ("#42ccff", "#fffddd", "#f889de", "#643a99", "#b8faff", "#fff5af")
VIOLET = ("#533891", "#aa8ee7", "#effaff", "#8770cd", "#d7b5ff")


def fit_ground_art(art, title_bottom):
    """Preserve a portrait's top-edge face below the actual printed name area."""
    safe_top = round(title_bottom + 18)
    if safe_top < 0 or safe_top >= SIZE[1]:
        raise ValueError("Ground card title leaves no illustration area")
    height = SIZE[1]-safe_top
    width = round(SIZE[0]*height/SIZE[1])
    subject = art.resize((width, height), Image.Resampling.LANCZOS)
    backdrop = Image.blend(art.filter(ImageFilter.GaussianBlur(25)),
                           Image.new("RGB", SIZE, (4, 12, 28)), .68)
    # Feather only the extreme perimeter into the source-color extension. The
    # eyes stay clear and the illustration avoids a hard rectangular inset.
    horizontal = Image.new("L", (width, 1))
    horizontal.putdata([round(255*min(1, x/26, (width-1-x)/26)) for x in range(width)])
    vertical = Image.new("L", (1, height))
    vertical.putdata([round(255*min(1, y/14, (height-1-y)/14)) for y in range(height)])
    mask = ImageChops.multiply(horizontal.resize(subject.size), vertical.resize(subject.size))
    backdrop.paste(subject, ((SIZE[0]-width)//2, safe_top), mask)
    return backdrop


def fit_fleet_art(art, title_bottom):
    """Keep top-edge bridges below both the printed name and HP/pip area."""
    # A short name still shares its row with the HP cartridge and rarity pips.
    # Long two-line names reserve their actual measured height instead.
    return fit_ground_art(art, max(title_bottom, 283))


def affiliation_ribbon(renderer, card, ink, width, angle):
    """One readable faction plaque, with era on its own line and a material seal."""
    band = Image.new("RGBA", (width, 72))
    draw = ImageDraw.Draw(band)
    edges = ((15, 1), (width-16, 1), (width-2, 15), (width-2, 56),
             (width-16, 70), (15, 70), (1, 56), (1, 15))
    draw.polygon(edges, fill=(5, 16, 32, 245))
    draw.line((*edges, edges[0]), fill=(*ink, 245), width=2, joint="curve")
    draw.line((84, 10, width-25, 10), fill=(*ink, 75), width=1)
    draw.ellipse((17, 8, 73, 64), fill=(*ink, 220), outline=(248, 248, 226, 255), width=2)
    draw.ellipse((23, 14, 67, 58), fill=(7, 19, 37, 255), outline=(*ink, 170), width=1)
    # A compass seal identifies affiliation without inventing an official logo.
    draw.polygon(((45, 19), (50, 31), (61, 36), (50, 41), (45, 53),
                  (40, 41), (29, 36), (40, 31)), fill=(*ink, 255))
    draw.ellipse((42, 33, 48, 39), fill=(249, 250, 232, 255))
    name = str(card["faction"]).upper()
    era = str(card["era"]).upper()
    draw.text((88, 15), name, font=renderer._fit(name, width-119, 29, 20, "heading"),
              fill=(248, 248, 233, 255), anchor="lt")
    draw.text((89, 45), era, font=renderer._fit(era, width-120, 20, 15),
              fill=(190, 216, 225, 255), anchor="lt")
    return band.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)


def _rgb(color):
    return tuple(bytes.fromhex(color.lstrip("#")))


def _metal(size, palette, phase=0.0):
    """Compact diagonal metallic bands with deliberately sharp reflections."""
    colors = [_rgb(c) for c in palette]
    small = Image.new("RGB", (96, 128))
    pixels = []
    for y in range(128):
        for x in range(96):
            value = ((y / 127 * 1.65 + x / 95 * .42 + phase) % 1) * (len(colors) - 1)
            index = min(int(value), len(colors) - 2)
            fraction = value - index
            pixels.append(tuple(round(a + (b - a) * fraction)
                                for a, b in zip(colors[index], colors[index + 1])))
    small.putdata(pixels)
    return small.resize(size, Image.Resampling.BILINEAR).convert("RGBA")


def _foil_polygon(canvas, points, palette, width=3, *, faceted=False):
    mask = Image.new("L", SIZE)
    md = ImageDraw.Draw(mask)
    md.polygon(points, fill=255)
    canvas.paste(_metal(SIZE, palette), (0, 0), mask)
    if faceted:
        facets = Image.new("RGBA", SIZE)
        fd = ImageDraw.Draw(facets)
        if len(points) == 3:
            center = tuple(sum(point[i] for point in points)/3 for i in (0, 1))
            fd.polygon((points[0], points[1], center), fill=(255, 254, 225, 115))
            fd.polygon((points[1], points[2], center), fill=(8, 12, 38, 125))
            fd.line((points[0], center, points[2]), fill=(255, 249, 212, 225), width=3)
        else:
            fd.polygon((points[0], points[1], points[3]), fill=(255, 253, 218, 58))
            fd.polygon((points[0], points[3], points[-1]), fill=(6, 17, 42, 83))
            fd.line((points[0], points[3]), fill=(255, 251, 220, 210), width=3)
        canvas.alpha_composite(facets)
    draw = ImageDraw.Draw(canvas)
    draw.line((*points, points[0]), fill=(255, 244, 195), width=width, joint="curve")


def _metal_text(renderer, text, width, size, palette, *, minimum=24, shear=.13, stroke=4):
    """Foil type, dark extrusion, bright bevel, then one unified italic shear."""
    font = renderer._fit(text, width, size, minimum, "heading")
    left, top, right, bottom = font.getbbox(text, anchor="lt")
    pad = 18
    dimensions = (math.ceil(right - min(0, left)) + pad * 2, bottom - top + pad * 2)
    image = Image.new("RGBA", dimensions)
    draw = ImageDraw.Draw(image)
    xy = (pad - min(0, left), pad - top)
    draw.text((xy[0] + 6, xy[1] + 8), text, font=font, anchor="lt", fill=(5, 10, 25),
              stroke_width=stroke + 3, stroke_fill=(5, 10, 25))
    draw.text(xy, text, font=font, anchor="lt", fill=(255, 249, 214),
              stroke_width=stroke + 2, stroke_fill=(255, 249, 214))
    draw.text(xy, text, font=font, anchor="lt", fill=(9, 19, 40),
              stroke_width=stroke, stroke_fill=(9, 19, 40))
    mask = Image.new("L", dimensions)
    ImageDraw.Draw(mask).text(xy, text, font=font, anchor="lt", fill=255)
    image.paste(_metal(dimensions, palette, phase=.13), (0, 0), mask)
    extension = math.ceil(image.height * shear)
    image = image.transform((image.width + extension, image.height), Image.Transform.AFFINE,
                            (1, shear, -extension, 0, 1, 0), Image.Resampling.BICUBIC)
    return image


def _glint(draw, x, y, radius, color):
    points = ((x, y-radius), (x+radius*.12, y-radius*.12), (x+radius, y),
              (x+radius*.12, y+radius*.12), (x, y+radius),
              (x-radius*.12, y+radius*.12), (x-radius, y), (x-radius*.12, y-radius*.12))
    draw.polygon(points, fill=color)
    draw.ellipse((x-3, y-3, x+3, y+3), fill=(255, 255, 247, 255))


def _curve(points, count=70):
    start, first, second, end = points
    return [tuple(round((1-t)**3 * start[i] + 3*(1-t)**2*t*first[i] +
                        3*(1-t)*t*t*second[i] + t**3*end[i]) for i in (0, 1))
            for t in (n / count for n in range(count + 1))]


def _action_frame(canvas, level, special, palette):
    """Sculpted corners and large foil motion stay outside the central subject."""
    rim = 21 if level == 3 else 29 if level == 4 else 35
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((10, 10, 989, 1389), radius=36, outline=(7, 15, 32), width=9)
    draw.rounded_rectangle((rim, rim, 999-rim, 1399-rim), radius=30,
                           outline=(255, 244, 195), width=3)
    # An irregular faceted silhouette reads as sculpted metal, not an app border.
    corners = [
        ((17, 18), (238, 18), (195, 40), (80, 61), (50, 190), (18, 257)),
        ((983, 18), (762, 18), (805, 40), (920, 61), (950, 190), (982, 257)),
        ((17, 1382), (238, 1382), (186, 1356), (77, 1334), (47, 1236), (18, 1160)),
        ((983, 1382), (762, 1382), (814, 1356), (923, 1334), (953, 1236), (982, 1160)),
    ]
    for points in corners:
        _foil_polygon(canvas, points, palette, 2, faceted=True)
    effects = Image.new("RGBA", SIZE)
    ed = ImageDraw.Draw(effects)
    curves = [
        _curve(((-30, 256), (182, 426), (-62, 697), (146, 915))),
        _curve(((1018, 338), (800, 501), (1034, 741), (824, 1002))),
    ]
    if level >= 5 or special:
        curves.append(_curve(((-10, 1000), (234, 784), (770, 1025), (1015, 829))))
    for index, points in enumerate(curves):
        color = (80, 218, 255) if index % 2 == 0 else (243, 163, 255)
        ed.line(points, fill=(*color, 65 if level == 3 else 95), width=30 if level == 3 else 48)
    canvas.alpha_composite(effects.filter(ImageFilter.GaussianBlur(11)))
    ed = ImageDraw.Draw(effects)
    for index, points in enumerate(curves):
        ed.line(points, fill=(19, 31, 53, 220), width=14)
        ed.line(points, fill=(243, 193, 75, 235) if level == 4 else (151, 222, 255, 235), width=9)
        ed.line(points, fill=(255, 253, 230, 240), width=3)
    canvas.alpha_composite(effects)
    if level >= 5 or special:
        # Crystal wedges occur at the edges; no category-specific subject mask.
        for points in (
            ((29, 380), (136, 449), (53, 625)),
            ((971, 572), (886, 638), (962, 814)),
            ((28, 740), (93, 825), (37, 906)),
        ):
            _foil_polygon(canvas, points, PRISM, 2, faceted=True)


def _edition_seal(renderer, canvas, card, palette):
    """The printed supply cap is the definition's real cap, never a copy serial."""
    release = card.get("release") or {}
    cap = release.get("supply_cap")
    has_cap = isinstance(cap, int) and not isinstance(cap, bool) and cap > 0
    x, y, radius = 848, 382, 86
    points = []
    for n in range(24):
        angle = n / 24 * math.tau - math.pi / 2
        length = radius if n % 2 == 0 else radius - 8
        points.append((x + math.cos(angle)*length, y + math.sin(angle)*length))
    _foil_polygon(canvas, points, palette, 2)
    draw = ImageDraw.Draw(canvas)
    draw.ellipse((x-70, y-70, x+70, y+70), fill=(8, 17, 34), outline=(255, 239, 166), width=2)
    top = "EDITION CAP" if has_cap else "EVENT"
    font = renderer._fit(top, 125, 21, 17, "heading")
    draw.text((x, y-49), top, font=font, fill=(255, 232, 157), anchor="mt")
    value = str(cap) if has_cap else "EXCLUSIVE"
    font = renderer._fit(value, 120, 69 if has_cap else 24, 18, "heading")
    draw.text((x, y-21 if has_cap else y-2), value, font=font, fill=(255, 247, 205), anchor="mt")
    if has_cap:
        draw.text((x, y+48), "TOTAL", font=renderer._font(18, "heading"),
                  fill=(175, 227, 233), anchor="mt")


def compose_showcase(renderer, card, art, profile, special):
    """Return a 5:7 transparent-corner collectible face from verified inputs."""
    level = profile["level"]
    palette = tuple(profile["colors"]) if level < 3 else VIOLET if level == 3 else GOLD if level == 4 else PRISM
    ink = _rgb(profile["ink"]) if level < 3 else (235, 217, 255) if level == 3 else (255, 230, 155) if level == 4 else (214, 250, 255)
    title_font, title_lines = renderer._wrap(card["name"].upper(), 661, 96, 2, 35, "heading")
    title_bottom = 105 + len(title_lines) * (title_font.size + 2)
    if card.get("art_layout") == "published-window":
        pass
    elif card.get("arena") == "ground":
        # Portrait and creature art can put eyes at the source's very top edge.
        # Reserve the actual name height, then contain the entire illustration
        # below it. A dark extension of its colors joins the peripheral foil.
        art = fit_ground_art(art, title_bottom)
    elif card.get("arena") == "fleet":
        art = fit_fleet_art(art, title_bottom)
    canvas = _metal(SIZE, palette)
    mask = Image.new("L", SIZE)
    ImageDraw.Draw(mask).rounded_rectangle((22, 22, 977, 1377), radius=31, fill=255)
    canvas.paste(art, (0, 0), mask)
    # Only printed zones are shaded. Keep the central illustration fully vivid.
    shade = Image.new("RGBA", SIZE)
    sd = ImageDraw.Draw(shade)
    for y in range(1400):
        top = max(0, 1-y/325) * 156
        bottom = max(0, min(1, (y-955)/205)) * 239
        sd.line((25, y, 974, y), fill=(3, 10, 29, int(max(top, bottom))))
    canvas.alpha_composite(shade)

    # Native transparent foil contributes irregular physical reflections; its
    # alpha is kept intact so the source illustration remains the central image.
    foil = renderer._foil_overlay()
    opacity = .34 if level <= 3 else .79 if level == 4 else 1.0
    if special:
        opacity = min(1.0, opacity + .15)
    if opacity < 1:
        foil.putalpha(foil.getchannel("A").point(lambda value: round(value * opacity)))
    canvas.alpha_composite(foil)
    _action_frame(canvas, level, special, palette)

    draw = ImageDraw.Draw(canvas)
    glints = [(51, 306, 32), (948, 795, 31), (92, 1007, 23)]
    if level >= 4:
        glints += [(922, 279, 44), (52, 706, 29), (931, 1324, 35)]
    if level >= 5 or special:
        glints += [(174, 520, 24), (829, 933, 28), (882, 580, 27)]
    for x, y, radius in glints:
        _glint(draw, x, y, radius, (255, 247, 209, 245))

    def label(text, xy, width, size, *, fill=(239, 244, 236), role="body", anchor="lt", minimum=16):
        font = renderer._fit(str(text), width, size, minimum, role)
        ImageDraw.Draw(canvas).text(xy, str(text), font=font, fill=fill, anchor=anchor,
                                   stroke_width=1, stroke_fill=(3, 13, 30))

    # Small collection identifiers leave the card's subject and attack dominant.
    identifiers = Image.new("RGBA", SIZE)
    ImageDraw.Draw(identifiers).polygon(((62, 41), (936, 41), (936, 80), (62, 80)),
                                        fill=(4, 15, 32, 155))
    canvas.alpha_composite(identifiers)
    label("KYBERDEX", (68, 51), 250, 21, fill=(240, 239, 209), role="heading")
    label(f"{card['arena'].upper()} / {card['role'].upper()}", (931, 52), 485, 20,
          fill=(239, 244, 236), anchor="rt")
    # A narrow angled metal rail plus extruded foil name replaces the huge flat
    # heading block. Subject art stays visible through the plate's dark glass.
    rail = ((43, 88), (775, 88), (744, 103), (73, 119), (56, title_bottom+15), (42, title_bottom+29))
    _foil_polygon(canvas, rail, palette, 2)
    for index, text in enumerate(title_lines):
        face = _metal_text(renderer, text, 661, title_font.size, palette, minimum=35, shear=.10, stroke=3)
        canvas.alpha_composite(face, (51, 103+index*(title_font.size+2)))

    # HP belongs to the top silhouette, in its own faceted foil cartridge.
    _foil_polygon(canvas, ((794, 95), (951, 95), (954, 208), (918, 250), (791, 228)), palette, 2)
    draw = ImageDraw.Draw(canvas)
    draw.polygon(((806, 107), (939, 107), (940, 205), (910, 233), (803, 215)), fill=(6, 18, 34))
    label("HP", (932, 113), 115, 22, fill=ink, role="heading", anchor="rt")
    label(str(card["health"]), (928, 151), 120, 66, fill=(255, 248, 210), role="heading", anchor="rt")
    for n in range(max(1, level + 1)):
        x = 944 - n*25
        draw.polygon(((x, 269), (x+6, 275), (x, 281), (x-6, 275)), fill=ink)

    if special:
        _edition_seal(renderer, canvas, card, palette)

    # The action name is deliberately oversized, slanted and beveled like ink
    # printed through the illustration; the label is not a floating UI chip.
    ability = str(card.get("ability_name") or "ROLE ABILITY").upper()
    action = _metal_text(renderer, ability, 820, 124 if level >= 5 else 114 if level == 4 else 100,
                         palette, minimum=36, shear=.18, stroke=5)
    action = action.rotate(6 if level >= 4 else 4, resample=Image.Resampling.BICUBIC, expand=True)
    canvas.alpha_composite(action, ((1000-action.width)//2, 930-action.height//2))

    # Faction text and foil band are transformed together, exactly parallel.
    band = affiliation_ribbon(renderer, card, ink, 856, 3)
    canvas.alpha_composite(band, ((1000-band.width)//2, 1037-band.height//2))

    # An opaque dark printed rules field ensures consistent legibility for any
    # character, fleet, droid, creature or humor illustration underneath it.
    panel = Image.new("RGBA", SIZE)
    pd = ImageDraw.Draw(panel)
    pd.polygon(((61, 1080), (731, 1072), (732, 1214), (65, 1228)), fill=(5, 15, 34, 232))
    pd.line((64, 1080, 731, 1072), fill=(*ink, 145), width=1)
    canvas.alpha_composite(panel)
    body = str(card.get("ability_description") or "Role ability. See /vault card for details.")
    font, lines = renderer._wrap(body, 639, 28, 4, 21)
    draw = ImageDraw.Draw(canvas)
    for index, text in enumerate(lines):
        draw.text((77, 1091+index*(font.size+7)), text, font=font, fill=(243, 246, 235), anchor="lt")
    _foil_polygon(canvas, ((750, 1070), (939, 1070), (939, 1190), (913, 1220), (751, 1220)), palette, 2)
    draw = ImageDraw.Draw(canvas)
    draw.polygon(((762, 1082), (927, 1082), (927, 1185), (908, 1207), (763, 1207)), fill=(6, 19, 37))
    label("ATK", (779, 1087), 130, 22, role="heading", fill=ink)
    label(str(card["attack"]), (779, 1124), 140, 70, role="heading", fill=(255, 249, 216))
    metadata = Image.new("RGBA", SIZE)
    ImageDraw.Draw(metadata).rectangle((67, 1226, 933, 1259), fill=(5, 16, 32, 204))
    canvas.alpha_composite(metadata)
    label(f"SPEED  {card['speed']}", (76, 1233), 245, 22, role="heading", fill=ink)
    label("HYPERSPACE FOIL", (926, 1234), 365, 20, role="heading", fill=(200, 224, 230), anchor="rt")

    if special:
        # A substantial plaque spans the whole lower card, independently of the
        # rarity treatment. The cap's large medallion remains visible above.
        event = card.get("event_id") or card.get("category") == "legends"
        stamp = "EVENT EXCLUSIVE" if event else "LIMITED EDITION"
        plaque = _metal((856, 60), PRISM)
        plaque_mask = Image.new("L", plaque.size)
        plaque_edges = ((10, 0), (846, 0), (855, 10), (855, 49), (845, 59), (10, 59), (0, 49), (0, 10))
        ImageDraw.Draw(plaque_mask).polygon(plaque_edges, fill=255)
        plaque.putalpha(plaque_mask)
        pfont = renderer._fit(stamp, 780, 37, 20, "heading")
        pd = ImageDraw.Draw(plaque)
        pd.line((*plaque_edges, plaque_edges[0]), fill=(255, 247, 210), width=2, joint="curve")
        pd.text((428, 30), stamp, font=pfont, fill=(8, 22, 36), anchor="mm")
        plaque = plaque.rotate(math.degrees(math.atan2(17, 857)),
                               resample=Image.Resampling.BICUBIC, expand=True)
        canvas.alpha_composite(plaque, ((1000-plaque.width)//2, 1291-plaque.height//2))
        rarity_y, rarity_size, set_y = 1340, 29, 1346
    else:
        rarity_y, rarity_size, set_y = 1303, 54, 1331
    rarity = _metal_text(renderer, card["rarity"].upper(), 430, rarity_size, palette,
                        minimum=22, shear=.12, stroke=2)
    canvas.alpha_composite(rarity, (59, rarity_y-13))
    label(str(card.get("set") or "CORE ARCHIVE").upper(), (925, set_y), 420, 19,
          fill=(218, 231, 232), anchor="rt")

    # Keep Discord attachments and web cards on the same genuine card silhouette.
    outer = Image.new("L", SIZE)
    ImageDraw.Draw(outer).rounded_rectangle((0, 0, 999, 1399), radius=44, fill=255)
    canvas.putalpha(outer)
    output = io.BytesIO()
    canvas.save(output, format="PNG", compress_level=4)
    return output.getvalue()
