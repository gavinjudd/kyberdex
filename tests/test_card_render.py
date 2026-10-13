"""Renderer boundaries: cache invalidation, verified fonts and catalog-wide fit."""
import copy
import io
import json
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageDraw

from venator_vault.card_render import FINISHES, CardRenderer, is_special_edition

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def render_case(tmp_path):
    shutil.copytree(ROOT / "assets/fonts", tmp_path / "assets/fonts")
    shutil.copytree(ROOT / "assets/finishes", tmp_path / "assets/finishes")
    (tmp_path / "assets/art").mkdir()
    Image.new("RGB", (200, 300), "#123456").save(tmp_path / "assets/art/sample.png")
    source = json.loads((ROOT / "data/cards.json").read_text())["cards"]
    card = copy.deepcopy(next(c for c in source if c["id"] == "millennium-falcon"))
    card["art"] = "assets/art/sample.png"
    # Synthetic full-frame sources exercise legacy framing unless a test opts in.
    card.pop("art_layout", None)
    return tmp_path, card


def test_collectible_png_and_cache_exclude_player_state(render_case):
    root, card = render_case
    renderer = CardRenderer(root)
    first = renderer.render(card)
    # Personal annotations do not affect pixels, cache identity or PNG metadata.
    personal = dict(card, user_id="SECRET-USER", instance_id="SECRET-COPY", favorite=True)
    assert renderer.render(personal) is first
    with Image.open(io.BytesIO(first)) as image:
        assert image.size == (1000, 1400)
        assert image.format == "PNG" and image.mode == "RGBA"
        assert image.getpixel((0, 0))[3] == 0 and image.getpixel((500, 700))[3] == 255
        assert not image.info
    assert renderer.cache_info["entries"] == 1


def test_withdrawal_and_file_replacement_invalidate_warm_cache(render_case):
    root, card = render_case
    renderer = CardRenderer(root)
    first = renderer.render(card)
    with pytest.raises(ValueError, match="not ready"):
        renderer.render(dict(card, art_status="visual_qa_rejected"))
    Image.new("RGB", (200, 300), "#993311").save(root / card["art"])
    assert renderer.render(card) != first
    (root / card["art"]).unlink()
    with pytest.raises(ValueError, match="not ready"):
        renderer.render(card)


@pytest.mark.parametrize('rarity', ['common', 'legendary'])
def test_reviewed_published_window_is_not_shrunk_again_and_invalidates_cache(render_case, rarity):
    root, card = render_case
    image = Image.new('RGB', (1024, 1536), '#021224')
    ImageDraw.Draw(image).rectangle((200, 350, 824, 800), fill='#00ff00')
    image.save(root / card['art'])
    card.update(rarity=rarity, arena='ground')
    renderer = CardRenderer(root)
    legacy = renderer.render(card)
    published = renderer.render(dict(card, art_layout='published-window'))
    assert legacy != published
    with Image.open(io.BytesIO(published)) as rendered:
        assert rendered.getpixel((500, 350))[1] > 220
    with Image.open(io.BytesIO(legacy)) as rendered:
        assert rendered.getpixel((500, 350))[1] < 150


def test_lru_memory_is_bounded_and_private_path_is_rejected(render_case):
    root, card = render_case
    renderer = CardRenderer(root, max_entries=2, max_bytes=200_000)
    for rarity in FINISHES:
        renderer.render(dict(card, rarity=rarity))
        assert renderer.cache_info["entries"] <= 2
        assert renderer.cache_info["bytes"] <= 200_000
    (root / "private.png").write_bytes(b"PRIVATE")
    with pytest.raises(ValueError, match="not ready"):
        renderer.render(dict(card, art="private.png"))


def test_font_tampering_or_missing_face_never_uses_system_fallback(render_case):
    root, card = render_case
    manifest = json.loads((root / "assets/fonts/manifest.json").read_text())
    font = root / "assets/fonts" / manifest["fonts"][0]["file"]
    font.write_bytes(b"changed")
    with pytest.raises(ValueError, match="font hash mismatch"):
        CardRenderer(root).render(card)
    font.unlink()
    with pytest.raises(ValueError, match="font path"):
        CardRenderer(root).render(card)


def test_all_font_sizes_use_the_same_verified_byte_snapshot(render_case):
    root, card = render_case
    expected = CardRenderer(root).render(card)
    renderer = CardRenderer(root)
    renderer._load_fonts()
    manifest = json.loads((root / "assets/fonts/manifest.json").read_text())
    paths = {f["role"]: root / "assets/fonts" / f["file"] for f in manifest["fonts"]}
    paths["body"].write_bytes(paths["heading"].read_bytes())
    assert renderer.render(card) == expected
    with pytest.raises(ValueError, match="font hash mismatch"):
        CardRenderer(root).render(card)


def test_showcase_finish_is_verified_and_does_not_depend_on_subject_category(render_case):
    root, card = render_case
    renderer = CardRenderer(root)
    first = renderer.render(card)
    # The same display fields work for characters, fleets, creatures and droids.
    for subject_type in ("character", "starship", "vehicle", "droid", "creature"):
        assert renderer.render(dict(card, subject_type=subject_type)) is first
    manifest = json.loads((root / "assets/finishes/manifest.json").read_text())
    texture = root / "assets/finishes" / manifest["file"]
    texture.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="finish texture hash mismatch"):
        CardRenderer(root).render(card)
    # Already verified bytes remain the immutable asset snapshot for this worker.
    assert renderer.render(dict(card, attack=card["attack"] + 1))


def test_queued_cache_hit_rechecks_art_after_waiting(render_case):
    root, card = render_case
    renderer = CardRenderer(root)
    renderer.render(card)
    mutex = threading.Lock()
    queued = threading.Event()

    class SignalledLock:
        def __enter__(self):
            queued.set()
            mutex.acquire()

        def __exit__(self, *_args):
            mutex.release()

    renderer._lock = SignalledLock()
    with ThreadPoolExecutor(max_workers=1) as executor:
        mutex.acquire()
        future = executor.submit(renderer.render, card)
        try:
            assert queued.wait(2)
            (root / card["art"]).unlink()
        finally:
            mutex.release()
        with pytest.raises(ValueError, match="not ready"):
            future.result(timeout=2)
def test_rarity_progression_and_edition_are_independent(render_case):
    root, card = render_case
    for property_name in ("rim", "sheen", "stars", "etch"):
        levels = [p[property_name] for p in FINISHES.values()]
        assert levels == sorted(levels)
    renderer = CardRenderer(root)
    ordinary = renderer.render(card)
    limited = dict(card, release={"supply_cap": 48}, category="limited")
    event = dict(card, event_id="special-release")
    assert is_special_edition(limited) and is_special_edition(event)
    assert not is_special_edition(card)
    assert renderer.render(limited) != ordinary
    assert renderer.render(event) != ordinary
    assert limited["rarity"] == card["rarity"]


def test_all_catalog_names_rules_and_labels_fit_verified_fonts():
    renderer = CardRenderer(ROOT)
    renderer._load_fonts()
    cards = json.loads((ROOT / "data/cards.json").read_text())["cards"]
    for card in cards:
        if FINISHES[card["rarity"]]["level"] >= 3 or is_special_edition(card):
            font, lines = renderer._wrap(card["name"].upper(), 661, 96, 2, 35, "heading")
            assert len(lines) <= 2 and all(font.getlength(line) <= 661 for line in lines), card["id"]
            font, lines = renderer._wrap(card["ability_description"], 639, 28, 4, 21)
            assert 1091 + (len(lines)-1) * (font.size+7) + font.size < 1233, card["id"]
            renderer._fit(card["ability_name"].upper(), 820, 124, 36, "heading")
            renderer._fit(str(card["health"]), 120, 66, 16, "heading")
            renderer._fit(card["set"].upper(), 420, 19)
        font, lines = renderer._wrap(card["name"].upper(), 680, 112, 2, 38, "heading")
        assert len(lines) <= 2 and all(font.getlength(line) <= 680 for line in lines), card["id"]
        font, lines = renderer._wrap(card["ability_description"], 674, 30, 3, 22)
        assert 1070 + len(lines) * (font.size + 8) < 1194, card["id"]
        renderer._fit(f"{card['faction'].upper()} / {card['era'].upper()}", 814, 27, role="heading")
        renderer._fit(str(card["attack"]), 141, 72, role="heading")
        renderer._fit(str(card["health"]), 180, 78, role="heading")
        renderer._fit(card["set"].upper(), 467, 22)


@pytest.mark.parametrize("rarity", ["common", "rare", "legendary", "mythic"])
def test_fleet_top_edge_identity_clears_actual_title_and_hp(render_case, rarity):
    root, card = render_case
    # An identity marker near the source's top edge stands in for bridge towers.
    # Exercise final composition, so dropping the framing call fails this test.
    source = root / card["art"]
    art = Image.new("RGB", (1000, 1400), "black")
    ImageDraw.Draw(art).rectangle((370, 55, 630, 110), fill=(0, 255, 20))
    art.save(source)
    original = source.read_bytes()
    renderer = CardRenderer(root)
    marker_tops = []
    for name in ("Executor", "Venator-class Star Destroyer"):
        fixture = dict(card, name=name, rarity=rarity, arena="fleet", release=None,
                       event_id=None, category="canon", availability="regular")
        png = renderer.render(fixture)
        with Image.open(io.BytesIO(png)) as image:
            red, green, blue = image.convert("RGB").crop((250, 285, 750, 700)).split()
            marker = ImageChops.multiply(green.point(lambda v: 255 if v > 170 else 0),
                                         red.point(lambda v: 255 if v < 70 else 0))
            marker = ImageChops.multiply(marker, blue.point(lambda v: 255 if v < 80 else 0))
            bounds = marker.getbbox()
        assert bounds is not None, "Top-edge bridge identity was hidden by the title"
        top = bounds[1] + 285
        # The HP cartridge and rarity pips share the top area even on short names.
        assert top > 283
        if rarity in {"legendary", "mythic"}:
            font, lines = renderer._wrap(name.upper(), 661, 96, 2, 35, "heading")
            title_bottom = 105 + len(lines) * (font.size + 2)
        else:
            font, lines = renderer._wrap(name.upper(), 680, 112, 2, 38, "heading")
            title_bottom = 117 + len(lines) * (font.size + 1)
        assert top > title_bottom
        assert bounds[2] - bounds[0] > 150 and bounds[3] - bounds[1] > 30
        marker_tops.append(top)
    assert marker_tops[1] > marker_tops[0], "Two-line titles must reserve more source headroom"
    assert source.read_bytes() == original
