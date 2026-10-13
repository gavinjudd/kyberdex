import json
from pathlib import Path

import pytest

from venator_vault.presentation import card_panel, drop_panel
from venator_vault.references import lore_references


@pytest.mark.parametrize("source", [
    "javascript:alert(1)", "data:text/html,hello", "http://www.starwars.com/databank/yoda",
    "https://user:password@www.starwars.com/x", "https://www.starwars.com/x?token=secret",
    "https://www.starwars.com/x#secret", "https://127.0.0.1/x", "https://[::1]/x",
    "https://127.1/x", "https://0177.0.0.1/x", "https://0x7f.0.0.0x1/x", "https://2130706433/x",
    "https://vault.local/x", "https://vault.internal/x", "https://localhost/x",
    "https://www.starwars.com:invalid/x", "https://www.starwars.com:8080/x",
    "https://www.starwars.com/x) [spoof](https://example.com)",
    "https://www.starwars.com/\nx", "https://www.starwars.com/\\x",
    "https://www.starwars.com/\x00x", "https://www.starwars.com/\x7fx", None, {"url": "secret"},
])
def test_unsafe_reference_is_not_published(source):
    assert lore_references({"lore_sources": [source]}) == []


def test_reference_list_is_bounded_deduplicated_and_tolerates_missing_data():
    links = [f"https://www.starwars.com/databank/card-{index}" for index in range(5)]
    assert [item["url"] for item in lore_references({"lore_sources": [links[0], *links]})] == links[:3]
    for value in (None, "https://www.starwars.com/databank/yoda", {}):
        assert lore_references({"lore_sources": value}) == []


def test_discord_card_references_preserve_continuity_and_do_not_reveal_active_encounter():
    root = Path(__file__).resolve().parents[1]
    card = json.loads((root / "data/cards.json").read_text())["cards"][0]
    card = {**card, "continuity": "Star Wars Legends", "lore_sources": [
        "https://www.starwars.com/databank/yoda", "https://www.swtor.com/info",
    ]}
    details = card_panel(card).to_dict()
    field = next(field for field in details["fields"] if field["name"] == "Continuity & references")
    assert field["value"].startswith("Star Wars Legends\n")
    assert "[Reference 1 · starwars.com](https://www.starwars.com/databank/yoda)" in field["value"]
    assert "swtor.com" in field["value"]
    encounter = drop_panel({"status": "active", "expires_at": 100, "card": card,
                            "claims": [], "remaining": 2, "collector_limit": 2}, now=0).to_dict()
    assert "starwars.com" not in str(encounter)
    assert "Continuity & references" not in str(encounter)


def test_discord_reference_field_respects_embed_limit_without_truncating_links():
    root = Path(__file__).resolve().parents[1]
    card = json.loads((root / "data/cards.json").read_text())["cards"][0]
    links = ["https://www.starwars.com/databank/yoda",
             *[f"https://www.starwars.com/{index}/" + "a" * 340 for index in range(2)]]
    card = {**card, "description": "D" * 4096, "aliases": ["A" * 1000],
            "continuity": "C" * 256, "lore_sources": links}
    embed = card_panel(card)
    assert len(embed) <= 6000
    field = next(field for field in embed.to_dict()["fields"]
                 if field["name"] == "Continuity & references")
    assert len(field["value"]) <= 1024
    assert links[0] in field["value"]
    assert links[-1] not in field["value"]
    for link in links:
        assert link not in field["value"] or f"]({link})" in field["value"]


def test_approved_game_details_and_encounter_keep_legends_provenance_without_event_label():
    root = Path(__file__).resolve().parents[1]
    cards = json.loads((root / "data/cards.json").read_text())["cards"]
    card = next(card for card in cards if card["id"] == "jedi-knight-revan")
    detail_fields = {field.name: field.value for field in card_panel(card).fields}
    assert "Approved game continuity · Legends" in detail_fields["Edition"]
    assert "Canon" not in detail_fields["Edition"]
    assert "Star Wars Legends" in detail_fields["Continuity & references"]
    assert "Event-exclusive" not in detail_fields
    drop = drop_panel({"status": "active", "expires_at": 100, "card": card,
                       "claims": [], "remaining": 1, "collector_limit": 1}, now=0)
    encounter = next(field.value for field in drop.fields if field.name == "Encounter")
    assert "Approved game continuity · Legends" in encounter
    assert card["name"] not in str(drop.to_dict())
