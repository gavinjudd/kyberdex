"""Targeted production-catalog regressions for lore that affects game rules.

All ownership transitions below use disposable local SQLite databases. These
checks never contact Discord or touch a deployed player's collection.
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("vault_catalog_core",PROJECT/"src/venator_vault/core.py")
core = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(core)

APPROVED_GAME_CARDS = {
    "darth-revan", "bastila-shan", "satele-shan", "darth-malgus", "darth-nihilus", "leviathan",
    "jedi-knight-revan", "revan-shadow-of-revan", "darth-malak", "darth-traya", "darth-sion",
    "meetra-surik", "visas-marr", "hk-47", "canderous-ordo", "carth-onasi", "jolee-bindo",
    "t3-m4", "lana-beniko", "theron-shan",
}


class CatalogSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.catalog_path = PROJECT/"data/cards.json"
        self.document = json.loads(self.catalog_path.read_text())
        self.game = core.Game(Path(self.temp.name)/"catalog.sqlite",self.catalog_path)

    def tearDown(self):
        self.temp.cleanup()

    def own(self,user,*card_ids):
        with self.game._tx() as connection:
            return [self.game._mint(connection,"catalog-test",user,key,1,"test fixture")
                    for key in card_ids]

    def test_enemy_mentions_cannot_create_false_jedi_team_synergy(self):
        cards = self.game.catalog
        self.assertEqual(cards["darth-bane"]["faction"],"Sith Order")
        self.assertEqual(cards["dwarf-spider-droid"]["faction"],"Separatist Alliance")
        items = self.own("mixed","yoda","darth-bane","dwarf-spider-droid")
        deck = self.game.set_deck("catalog-test","mixed","ground",[item["id"] for item in items],now=1)
        self.assertEqual({item["faction_bonus"] for item in deck["cards"]},{0})

    def test_republic_units_gain_their_actual_team_bonus(self):
        items = self.own("republic","clone-commander-doom","clone-99","republic-attack-gunship")
        deck = self.game.set_deck("catalog-test","republic","ground",[item["id"] for item in items],now=1)
        self.assertEqual({item["card"]["faction"] for item in deck["cards"]},{"Galactic Republic"})
        self.assertEqual({item["faction_bonus"] for item in deck["cards"]},{0.1})

    def test_aftab_uses_resistance_synergy_not_his_fathers_rebel_affiliation(self):
        items = self.own("resistance","aftab-ackbar","finn","poe-dameron")
        deck = self.game.set_deck("catalog-test","resistance","ground",[item["id"] for item in items],now=1)
        self.assertEqual({item["card"]["faction"] for item in deck["cards"]},{"Resistance"})
        self.assertEqual({item["faction_bonus"] for item in deck["cards"]},{0.1})

        items = self.own("generations","aftab-ackbar","admiral-ackbar","wedge-antilles")
        deck = self.game.set_deck("catalog-test","generations","ground",[item["id"] for item in items],now=1)
        bonuses = {item["card"]["faction"]: item["faction_bonus"] for item in deck["cards"]}
        self.assertEqual(bonuses,{"Resistance":0,"Rebel Alliance":0.05})

    def test_persona_and_humor_variants_do_not_inflate_subject_progress(self):
        cards = self.game.catalog
        self.assertEqual(cards["anakin-skywalker"]["subject_id"],cards["darth-vader"]["subject_id"])
        self.own("personas","anakin-skywalker","darth-vader","sand-hater")
        profile = self.game.profile("catalog-test","personas")
        self.assertEqual((profile["regular_unique_cards"],profile["regular_collected_subject_count"]),(3,1))
        self.assertGreaterEqual(profile["regular_subject_count"],800)
        self.assertTrue(profile["regular_subject_metadata_complete"])

    def test_imperial_allegiance_controls_synergy_over_cultural_identity(self):
        # These selected phases serve the Empire. Mandalorian or Nightsister
        # heritage alone must not put them on a different gameplay team.
        for subject, allies in (
            ("gar-saxon", ("paz-vizsla", "the-armorer")),
            ("magistrate-morgan-elsbeth", ("mother-talzin", "old-daka")),
        ):
            with self.subTest(subject=subject):
                user = f"imperial-{subject}"
                items = self.own(user, subject, "grand-admiral-thrawn", "darth-vader")
                deck = self.game.set_deck("catalog-test", user, "ground", [item["id"] for item in items], now=1)
                self.assertEqual({item["faction_bonus"] for item in deck["cards"]}, {0.1})

                user = f"heritage-{subject}"
                items = self.own(user, subject, *allies)
                deck = self.game.set_deck("catalog-test", user, "ground", [item["id"] for item in items], now=1)
                bonuses = {item["card"]["id"]: item["faction_bonus"] for item in deck["cards"]}
                self.assertEqual(bonuses[subject], 0)
                self.assertEqual([bonuses[ally] for ally in allies], [0.05, 0.05])

    def test_anakin_clearly_outclasses_generic_starfighters_on_the_shared_scale(self):
        cards = self.game.catalog
        anakin = cards["anakin-skywalker"]
        self.assertEqual(anakin["power_profile"]["combat"], 5)
        for ship_id, minimum_ratio in (("x-wing", 4), ("y-wing", 3.5)):
            with self.subTest(ship=ship_id):
                for stat in ("attack", "health"):
                    self.assertGreater(anakin[stat], cards[ship_id][stat]*minimum_ratio)
        self.assertEqual(self.document["balance"]["schema_version"], 4)
        self.assertEqual(self.document["balance"]["formula"], "combat-power-v4")

    def test_limited_editions_match_their_base_then_add_ten_percent_to_owned_stats(self):
        cards = self.game.catalog
        self.assertEqual(self.document["balance"]["limited_edition_bonus_percent"], 10)
        for card in cards.values():
            if card["category"] != "limited":
                continue
            if card["power_profile"]["basis"] == "depiction_override":
                continue
            # variant_of selects the actual portrayal: armored Vader shares
            # collection identity with Anakin but must compare against Vader.
            base = cards[card["variant_of"]]
            with self.subTest(card=card["id"]):
                for key in ("rarity", "role", "ability", "ability_name", "ability_description", "speed"):
                    self.assertEqual(card[key], base[key])
                self.assertEqual(card["power_profile"]["combat"], base["power_profile"]["combat"])
                owned_base, owned_edition = self.own(f"edition-{card['id']}", base["id"], card["id"])
                for key in ("attack", "health"):
                    expected = (base[key] * 110 + 50) // 100
                    self.assertEqual(card[key], expected)
                    self.assertEqual(owned_edition[key], expected)
                    self.assertGreater(owned_edition[key], owned_base[key])
        # The explicitly earlier Luke depiction is the deliberate exception;
        # its edition rarity is not an excuse to grant mature Jedi mastery.
        sunset, mature = cards["luke-binary-sunset"], cards["luke-skywalker"]
        self.assertEqual(sunset["power_profile"]["basis"], "depiction_override")
        self.assertEqual(sunset["power_profile"]["combat"], 2)
        self.assertEqual(mature["power_profile"]["combat"], 5)
        self.assertEqual((sunset["attack"], sunset["health"], sunset["speed"]), (425, 1701, 70))
        self.assertLess(sunset["attack"], mature["attack"])
        self.assertLess(sunset["health"], mature["health"])

    def test_malevolence_fleet_weapon_advantage_reaches_owned_stats(self):
        venator, malevolence = self.own("ion-fleet", "venator-class", "malevolence")
        self.assertEqual(malevolence["card"]["power_profile"]["combat"], 5)
        self.assertEqual(malevolence["card"]["rarity"], "legendary")
        self.assertEqual((malevolence["attack"], malevolence["health"]), (1613, 6451))
        for stat in ("attack", "health"):
            self.assertGreater(malevolence[stat], 1.44 * venator[stat])
            self.assertLess(malevolence[stat], self.game.catalog["death-star"][stat])

    def test_humor_treatments_inherit_combat_without_copying_base_stats(self):
        cards = self.game.catalog
        for treatment in cards.values():
            if treatment["category"] != "meme" or treatment["power_profile"]["basis"] == "depiction_override":
                continue
            with self.subTest(treatment=treatment["id"]):
                base = cards[treatment["subject_id"]]
                self.assertEqual(treatment["power_profile"]["combat"], base["power_profile"]["combat"])

        # A cosmetic name grants no additional mastery. Its own rarity and
        # role still apply, so inheritance must not copy the base card's stats.
        for base_id, treatment_id, expected in (
            ("obi-wan-kenobi", "hello-there", (517, 2550, 80)),
            ("anakin-skywalker", "sand-hater", (630, 2520, 70)),
        ):
            with self.subTest(treatment=treatment_id):
                base, treatment = self.own(f"cosmetic-{treatment_id}", base_id, treatment_id)
                self.assertEqual(tuple(treatment[key] for key in ("attack", "health", "speed")), expected)
                self.assertLess(treatment["attack"], base["attack"])
                self.assertLess(treatment["health"], base["health"])
                self.assertEqual(treatment["card"]["role"], cards[treatment_id]["role"])

    def test_jecki_reviewed_combat_is_not_capped_by_padawan_rank(self):
        cards = self.game.catalog
        jecki, yord = cards["jecki-lon"], cards["yord-fandar"]
        self.assertEqual((jecki["rarity"], jecki["role"], jecki["power_profile"]["combat"]),
                         ("rare", "striker", 4))
        self.assertEqual((yord["rarity"], yord["role"], yord["power_profile"]["combat"]),
                         ("rare", "guardian", 3))
        self.assertEqual(jecki["power_profile"]["basis"], "curated_design")
        # This compares the reviewed Khofar profiles at one rarity, not a
        # universal duel result. Guardian durability cannot erase the higher
        # combat band, and minted instances must receive the reviewed stats.
        owned_jecki, owned_yord = self.own("acolyte-capability", "jecki-lon", "yord-fandar")
        self.assertEqual(tuple(owned_jecki[key] for key in ("attack", "health", "speed")),
                         (574, 2297, 70))
        for stat in ("attack", "health"):
            self.assertGreater(owned_jecki[stat], owned_yord[stat])
        # Osha's selected early phase does not inherit her later dark-side
        # feats merely because they appear elsewhere in the same biography.
        self.assertEqual(cards["osha-aniseya"]["power_profile"]["combat"], 2)
        self.assertGreater(jecki["attack"], cards["osha-aniseya"]["attack"])
        self.assertGreater(jecki["health"], cards["osha-aniseya"]["health"])

    def test_every_production_profile_excludes_narrative_importance(self):
        self.assertEqual(len(self.game.catalog), 851)
        for card in self.game.catalog.values():
            with self.subTest(card=card["id"]):
                self.assertNotIn("importance", card["power_profile"])
                self.assertLessEqual(set(card["power_profile"]), {"combat", "basis", "rationale", "sources"})

    def test_named_resurgent_does_not_gain_power_from_flagship_status(self):
        generic, named = self.own("resurgent-hardware", "first-order-star-destroyer", "finalizer")
        for key in ("attack", "health", "speed"):
            self.assertEqual(generic[key], named[key])
        for key in ("rarity", "role", "ability"):
            self.assertEqual(generic["card"][key], named["card"][key])
        self.assertEqual(generic["card"]["power_profile"]["combat"], 4)
        self.assertEqual(generic["card"]["rarity"], "legendary")
        self.assertEqual(generic["card"]["role"], "striker")

    def test_venator_combat_role_reaches_owned_base_and_scene_edition(self):
        # The ship's documented combat role must reach inventory/duel data,
        # with the edition bonus applied before deck faction bonuses.
        base, edition = self.own("venator-combat", "venator-class", "venator-coruscant")
        for item in (base, edition):
            self.assertEqual(item["card"]["role"], "striker")
            self.assertEqual(item["card"]["ability"], "focused_strike")
            self.assertEqual(item["card"]["power_profile"]["combat"], 4)
        self.assertEqual((base["attack"], base["health"], base["speed"]), (1114, 4454, 70))
        self.assertEqual((edition["attack"], edition["health"], edition["speed"]), (1225, 4899, 70))
        self.assertEqual(edition["card"]["rarity"], base["card"]["rarity"])
        self.assertEqual(core.collector_limit(edition["card"]), 1)
        self.assertIsNone(base["card"]["release"])
        self.assertEqual(edition["card"]["release"]["supply_cap"], 80)

    def test_t85_service_does_not_inherit_kaz_later_resistance_team(self):
        items = self.own("mixed-xwings", "t-85-x-wing", "resistance-x-wing", "black-one")
        deck = self.game.set_deck("catalog-test", "mixed-xwings", "fleet",
                                  [item["id"] for item in items], now=1)
        bonuses = {item["card"]["id"]: item["faction_bonus"] for item in deck["cards"]}
        self.assertEqual(self.game.catalog["t-85-x-wing"]["faction"], "New Republic")
        self.assertEqual(bonuses, {"t-85-x-wing": 0, "resistance-x-wing": 0.05, "black-one": 0.05})

    def test_resistance_spies_receive_their_selected_phase_team_bonus(self):
        items = self.own("resistance-spies", "vi-moradi", "kazuda-kaz-xiono", "poe-dameron")
        deck = self.game.set_deck("catalog-test", "resistance-spies", "ground",
                                  [item["id"] for item in items], now=1)
        self.assertEqual({item["card"]["faction"] for item in deck["cards"]}, {"Resistance"})
        self.assertEqual({item["faction_bonus"] for item in deck["cards"]}, {0.1})

    def test_satele_combat_power_is_not_penalized_for_less_screen_time(self):
        satele, ventress = self.game.catalog["satele-shan"], self.game.catalog["asajj-ventress"]
        self.assertEqual((satele["rarity"], satele["role"], satele["power_profile"]["combat"]),
                         ("legendary", "tactician", 5))
        self.assertEqual((ventress["rarity"], ventress["role"], ventress["power_profile"]["combat"]),
                         ("epic", "tactician", 4))
        self.assertEqual((satele["attack"], satele["health"]), (1452, 7164))
        self.assertEqual((ventress["attack"], ventress["health"]), (720, 3555))
        for stat in ("attack", "health"):
            self.assertGreater(satele[stat], ventress[stat]*2)
        self.assertEqual((satele["category"], satele["availability"]), ("canon", "regular"))
        self.assertIsNone(satele["event_id"])
        self.assertIn(satele["id"], {row["id"] for row in self.game.spawn_pool()})

    def test_approved_game_continuity_is_regular_without_relabelling_its_provenance(self):
        pool = {row["id"] for row in self.game.spawn_pool()}
        self.assertEqual(len(pool), 835)
        self.assertEqual(len({self.game.catalog[key]["subject_id"] for key in pool}), 818)
        self.assertLessEqual(APPROVED_GAME_CARDS, pool)
        for key in APPROVED_GAME_CARDS:
            card = self.game.catalog[key]
            with self.subTest(card=key):
                self.assertEqual((card["category"], card["availability"]), ("canon", "regular"))
                self.assertIn("Legends", card["continuity"])
                self.assertIn("user-approved Old Republic", card["continuity"])
                self.assertIsNone(card["event_id"])
                self.assertIsNone(card["release"])
                self.assertFalse(core._is_event_exclusive(card))
                baseline = core.balance_stats(key, card["rarity"], card["role"], card["arena"],
                                              card["power_profile"]["combat"])
                # A phase-specific regular card has no limited-edition boost.
                self.assertEqual({stat: card[stat] for stat in baseline}, baseline)

    def test_three_revan_phases_are_one_collection_subject_with_their_own_baseline_stats(self):
        phases = ("darth-revan", "jedi-knight-revan", "revan-shadow-of-revan")
        self.assertEqual({self.game.catalog[key]["subject_id"] for key in phases}, {"darth-revan"})
        owned = self.own("revan-phases", *phases)
        profile = self.game.profile("catalog-test", "revan-phases")
        self.assertEqual((profile["regular_unique_cards"], profile["regular_collected_subject_count"]), (3, 1))
        for item in owned:
            card = item["card"]
            expected = core.balance_stats(card["id"], card["rarity"], card["role"], card["arena"],
                                          card["power_profile"]["combat"])
            self.assertEqual({stat: item[stat] for stat in expected}, expected)

    def test_jedi_capability_is_not_inferred_from_someone_elses_biography_mentions(self):
        cards = self.game.catalog
        for master_id in ("cin-drallig", "even-piell"):
            master = cards[master_id]
            self.assertEqual(master["rarity"], "epic")
            self.assertEqual(master["faction"], "Jedi Order")
            for civilian_id in ("alora", "pypey", "darja", "silman"):
                civilian = cards[civilian_id]
                with self.subTest(master=master_id, civilian=civilian_id):
                    self.assertEqual(civilian["rarity"], "common")
                    self.assertGreater(master["attack"], civilian["attack"]*4)
                    self.assertGreater(master["health"], civilian["health"]*4)

    def test_dooku_rarity_promotion_keeps_prior_encounter_capacity_after_restart(self):
        before = json.loads(json.dumps(self.document))
        old_card = next(card for card in before["cards"] if card["id"] == "count-dooku")
        old_card.update(rarity="epic", weight=30)
        old_card["power_profile"]["combat"] = 4
        old_card.update(core.balance_stats(old_card["id"], "epic", old_card["role"], old_card["arena"], 4))
        old_path = Path(self.temp.name)/"prior-catalog.json"
        old_path.write_text(json.dumps(before))
        db_path = Path(self.temp.name)/"promotion.sqlite"
        prior = core.Game(db_path, old_path)
        encounter = prior.create_drop("catalog-test", "old-channel", "count-dooku", now=100)
        prior.claim(encounter["id"], "first", "count dooku", now=101)

        restarted = core.Game(db_path, self.catalog_path)
        current = restarted.catalog["count-dooku"]
        self.assertEqual((current["rarity"], current["power_profile"]["combat"]), ("legendary", 5))
        self.assertGreater(current["attack"], restarted.catalog["asajj-ventress"]["attack"]*2)
        second = restarted.claim(encounter["id"], "second", "count dooku", now=102)
        self.assertEqual(second["drop"]["collector_limit"], 2)
        self.assertEqual(second["drop"]["remaining"], 0)
        self.assertEqual(second["instance"]["attack"], current["attack"])

        fresh = restarted.create_drop("catalog-test", "new-channel", "count-dooku", now=103)
        self.assertEqual(fresh["collector_limit"], 1)
        restarted.claim(fresh["id"], "new", "count dooku", now=104)
        with self.assertRaises(core.GameError) as error:
            restarted.claim(fresh["id"], "extra", "count dooku", now=105)
        self.assertEqual(error.exception.code, "drop_closed")

    def test_ground_transports_and_spacecraft_cannot_cross_arena_boundary(self):
        cards = self.game.catalog
        for key in ["trexler-marauder","mtt","conveyex"]:
            self.assertEqual((cards[key]["subject_type"],cards[key]["arena"]),("vehicle","ground"))
        self.assertEqual((cards["a-wing-trainer-rz-1t"]["subject_type"],cards["a-wing-trainer-rz-1t"]["arena"]),("starship","fleet"))
        items = self.own("transport","mtt","a-wing-trainer-rz-1t")
        with self.assertRaises(core.GameError) as error:
            self.game.set_deck("catalog-test","transport","ground",[item["id"] for item in items],now=1)
        self.assertEqual(error.exception.code,"mixed_arena")

    def test_clone_affiliation_tracks_the_canon_depiction(self):
        cards = self.game.catalog
        for key in ["clone-commander-scorch","clone-commander-mayday"]:
            self.assertEqual(cards[key]["faction"],"Galactic Empire")
            self.assertEqual(cards[key]["era"],"Age of Empire")
            self.assertIn("The Bad Batch",cards[key]["depiction"])
        self.assertEqual(cards["clone-captain-howzer"]["faction"],"Independent")
        self.assertIn("resistance",cards["clone-captain-howzer"]["depiction"])

    def test_empire_legendary_promotions_open_owned_mythic_crafting_routes(self):
        for arena,ingredient,target in [("ground","grand-admiral-thrawn","darth-vader"),
                                        ("fleet","imperial-class","executor")]:
            with self.subTest(arena=arena):
                items = self.own(arena,*[ingredient]*7)
                with self.game._tx() as connection:
                    self.game._credit(connection,"catalog-test",arena,2000,"test fixture",arena,1)
                result = self.game.craft("catalog-test",arena,[item["id"] for item in items],target,now=1)
                self.assertEqual(result["instance"]["card_id"],target)
                self.assertEqual(result["instance"]["card"]["rarity"],"mythic")
                self.assertEqual(result["credits"],0)

    def test_canon_bane_depiction_remains_the_clone_wars_apparition(self):
        bane = self.game.catalog["darth-bane"]
        self.assertEqual(bane["category"],"canon")
        self.assertIn("apparition",bane["depiction"].casefold())
        self.assertIn("The Clone Wars",bane["continuity"])
        self.assertIn("https://www.starwars.com/databank/darth-bane",bane["lore_sources"])

    def test_unapproved_legends_still_require_explicit_event_activation_and_route(self):
        # Production currently has no exclusive Legends cards. Keep the
        # generic gate covered rather than turning the game-specific approval
        # into blanket admission for future Legends definitions.
        event_card = dict(self.game.catalog["darth-bane"], id="unapproved-legends-fixture",
                          category="legends", availability="event_only", event_id="test-archive",
                          release={"starts_at": "2027-05-04T00:00:00Z",
                                   "ends_at": "2027-05-11T00:00:00Z", "supply_cap": 2})
        path = Path(self.temp.name)/"exclusive-fixture.json"
        path.write_text(json.dumps({"schema_version": 1, "cards": [event_card]}))
        now = core.timestamp(event_card["release"]["starts_at"])+1
        for activated in (False, True):
            game = core.Game(Path(self.temp.name)/f"exclusive-{activated}.sqlite", path,
                             enabled_events=("test-archive",) if activated else ())
            self.assertEqual(game.spawn_pool(now=now), [])
            with self.assertRaises(core.GameError):
                game.create_drop("catalog-test", "ordinary", event_card["id"], now=now)
            if activated:
                drop = game.create_drop("catalog-test", "event", event_card["id"], now=now,
                                        event_id="test-archive")
                self.assertEqual(drop["card_id"], event_card["id"])
            else:
                with self.assertRaises(core.GameError):
                    game.create_drop("catalog-test", "event", event_card["id"], now=now,
                                     event_id="test-archive")


if __name__ == "__main__":
    unittest.main()
