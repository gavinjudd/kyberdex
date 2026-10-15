"""Kyber Tiers faces: full-bleed art, a distinct frame per rarity, animated top tiers."""
import copy
import io
import json
import random
import re
import shutil
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageStat

from venator_vault import tier_render
from venator_vault.card_render import SIZE, CardRenderer

ROOT = Path(__file__).resolve().parents[1]
CARDS = {card["id"]: card for card in json.loads((ROOT / "data/cards.json").read_text())["cards"]}
SAMPLES = {"common": "b1-battle-droid", "uncommon": "lando-calrissian", "rare": "captain-rex",
           "epic": "ahsoka-tano", "legendary": "anakin-skywalker", "mythic": "darth-vader"}


@pytest.fixture(scope="module")
def renderer(tmp_path_factory):
    """Synthetic artwork (the public checkout ships no subject art) with a letterbox matte."""
    root = tmp_path_factory.mktemp("tiers")
    shutil.copytree(ROOT / "assets/fonts", root / "assets/fonts")
    shutil.copytree(ROOT / "assets/finishes", root / "assets/finishes")
    (root / "assets/art").mkdir()
    rng = random.Random(7)
    for card_id in SAMPLES.values():
        image = Image.new("RGB", (1024, 1536), (4, 12, 28))
        picture = Image.new("RGB", (1024, 600))
        draw = ImageDraw.Draw(picture)
        for _ in range(160):
            x, y = rng.randrange(1024), rng.randrange(600)
            draw.ellipse((x, y, x + rng.randrange(20, 120), y + rng.randrange(20, 120)),
                         fill=tuple(rng.randrange(40, 255) for _ in range(3)))
        image.paste(picture, (0, 300))
        image.save(root / CARDS[card_id]["art"])
    return CardRenderer(root)


def sample(card_id):
    return copy.deepcopy(CARDS[card_id])


@pytest.mark.parametrize("rarity", list(SAMPLES))
def test_each_rarity_renders_a_full_card(renderer, rarity):
    image = Image.open(io.BytesIO(renderer.render(sample(SAMPLES[rarity]))))
    assert image.size == SIZE and image.mode == "RGBA"
    # The art window has real picture detail where old layouts left a flat matte band.
    band = image.convert("L").crop((300, 380, 700, 520))
    assert ImageStat.Stat(band).stddev[0] > 12


def test_frames_differ_between_rarities(renderer):
    edges = {}
    for rarity, card_id in SAMPLES.items():
        if rarity == "mythic":
            continue
        image = Image.open(io.BytesIO(renderer.render(sample(card_id)))).convert("RGB")
        edges[rarity] = image.getpixel((12, 700))
    assert len(set(edges.values())) == len(edges)


@pytest.mark.parametrize("rarity", ["legendary", "mythic"])
def test_top_tiers_animate_and_lower_tiers_stay_still(renderer, rarity):
    card = sample(SAMPLES[rarity])
    assert renderer.media_kind(card) == "gif"
    gif = Image.open(io.BytesIO(renderer.render(card, kind="gif")))
    assert gif.format == "GIF" and gif.n_frames > 1
    assert renderer.media_kind(CARDS[SAMPLES["rare"]]) == "png"


def test_letterbox_matte_is_trimmed_before_cropping():
    picture = Image.new("RGB", (300, 120), (200, 40, 40))
    framed = Image.new("RGB", (300, 300), (4, 12, 28))
    framed.paste(picture, (0, 90))
    trimmed = tier_render.trim_matte(framed)
    assert trimmed.size == (300, 120)


def test_celestial_cards_render_as_the_top_tier(renderer):
    card = sample(SAMPLES["mythic"])
    card.update(rarity="celestial", category="staff", name="Darth Vader — The Hallway")
    image = Image.open(io.BytesIO(renderer.render(card)))
    assert image.size == SIZE
    assert renderer.media_kind(card) == "gif"


def test_every_catalog_name_and_ability_fits_the_tier_layout(renderer):
    renderer._load_fonts()
    for card in CARDS.values():
        title = re.sub(r"\s+", " ", card["name"]).upper()
        renderer._wrap(title, 640, 100, 2, 40, "heading")
        description = re.sub(r"^PvP:\s*", "", card.get("ability_description") or "")
        renderer._wrap(description, 590, 26, 3, 17)
        renderer._fit(str(card["faction"]).upper(), 520, 30, 14, "heading")
