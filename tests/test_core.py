import concurrent.futures
import contextlib
import copy
import datetime as dt
import importlib.util
import json
import math
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("vault_core",Path(__file__).resolve().parents[1]/"src/venator_vault/core.py")
core = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(core)

DEFAULT_POWER_PROFILE = {
    "combat": 2, "basis": "default_design", "rationale": "Obvious synthetic fixture."
}


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.now = time.time()
        self.catalog_path = self.directory/"cards.json"
        self.db_path = self.directory/"game.sqlite"
        cards = []
        entries = [("alpha","common","striker","ground","Republic","canon"),
                   ("bravo","common","guardian","ground","Republic","canon"),
                   ("charlie","common","support","ground","Republic","canon"),
                   ("delta","uncommon","tactician","ground","Republic","canon"),
                   ("echo","rare","striker","ground","Republic","canon"),
                   ("sith","common","striker","ground","Sith","canon"),
                   ("ship","common","striker","fleet","Republic","canon"),
                   ("limited","epic","support","ground","Republic","limited"),
                   ("future","legendary","guardian","ground","Republic","limited"),
                   ("meme","common","support","ground","Republic","meme"),
                   ("legends","uncommon","guardian","ground","Republic","legends")]
        iso = lambda seconds:dt.datetime.fromtimestamp(seconds,dt.UTC).isoformat()
        for key,rarity,role,arena,faction,category in entries:
            release = {"starts_at":iso(self.now-60),"ends_at":iso(self.now+3600),"supply_cap":4} if key=="limited" else ({"starts_at":iso(self.now+100),"ends_at":iso(self.now+200),"supply_cap":2} if key=="future" else ({"starts_at":iso(self.now-60),"ends_at":iso(self.now+3600),"supply_cap":8} if key=="legends" else None))
            cards.append({"id": key,"name": "Álpha Trooper" if key=="alpha" else key.title(),"aliases": ["R2-D2","Artoo"] if key=="alpha" else [key],"rarity": rarity,"role": role,"arena": arena,"faction": faction,"category": category,
                              "attack": 100,"health": 500,"speed": 50,"weight": 2 if key=="alpha" else 1,"release": release})
            if key=="legends":
                cards[-1]["event_id"] = "archive"
                cards[-1]["collector_limit"] = 2
        self.catalog_path.write_text(json.dumps({"schema_version":1,"cards":cards,"rarities":{}}))
        self.game = core.Game(self.db_path,self.catalog_path)
        self.channel_counter = 0

    def tearDown(self):
        self.temp.cleanup()

    def capture(self,user,card="alpha",guild="guild",now=None,event_id=None):
        self.channel_counter += 1
        now = self.now if now is None else now
        drop = self.game.create_drop(guild,str(self.channel_counter),card,now=now,event_id=event_id)
        return self.game.claim(drop["id"],user,self.game.catalog[card]["name"],now=now)["instance"]

    def assert_error(self,code,call,*args,**kwargs):
        with self.assertRaises(core.GameError) as error:
            call(*args,**kwargs)
        self.assertEqual(error.exception.code,code)

    def seed_inventory(self,user,card="alpha",guild="guild",now=None):
        """Prepare inventory/credits for non-capture behavior without a catch streak."""
        now = self.now if now is None else now
        with self.game._tx() as connection:
            item = self.game._mint(connection,guild,user,card,now,"test-fixture")
            self.game._credit(connection,guild,user,25,"test-fixture",item["id"],now,xp=10)
            return item

    def test_rarity_and_limited_collector_caps_across_concurrent_connections(self):
        total = 0
        for card_id, guess, capacity in (("alpha", "r2 d2", 2), ("limited", "Limited", 2)):
            with self.subTest(card=card_id):
                drop = self.game.create_drop("guild", "race-"+card_id, card_id, now=self.now)
                barrier = threading.Barrier(12)
                def collect(user, barrier=barrier, drop_id=drop["id"], guess=guess):
                    other = core.Game(self.db_path, self.catalog_path)
                    barrier.wait(timeout=10)
                    try:
                        return other.claim(drop_id, str(user), guess, now=self.now)
                    except core.GameError as error:
                        return error.code
                with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
                    results = list(pool.map(collect, range(12)))
                self.assertEqual(sum(isinstance(result, dict) for result in results), capacity)
                self.assertEqual(sum(result=="drop_closed" for result in results), 12-capacity)
                restarted = core.Game(self.db_path, self.catalog_path)
                persisted = restarted.get_drop(drop["id"])
                self.assertEqual(len({claim["user_id"] for claim in persisted["claims"]}), capacity)
                self.assertEqual(persisted["remaining"], 0)
                self.assertEqual(persisted["status"], "claimed")
                total += capacity
                self.assertEqual(restarted.health()["total_claims"], total)

    def test_replay_race_only_mints_and_rewards_once(self):
        drop = self.game.create_drop("guild","replay","alpha",now=self.now)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _:core.Game(self.db_path,self.catalog_path).claim(drop["id"],"one","Artoo",now=self.now),range(8)))
        self.assertEqual(len({result["instance"]["id"] for result in results}),1)
        self.assertEqual(sum(result["credits_earned"] for result in results),25)
        self.assertEqual(self.game.profile("guild","one")["total_cards"],1)
        self.assertEqual(self.game.profile("guild","one")["credits"],25)
        self.assertEqual(self.game.get_drop(drop["id"])["remaining"],1)

    def test_database_trigger_rejects_third_distinct_common_collector(self):
        drop = self.game.create_drop("guild","trigger","alpha",now=self.now)
        for user in ["a","b"]:
            self.game.claim(drop["id"],user,"Artoo",now=self.now)
        instance = self.capture("c")
        with contextlib.closing(sqlite3.connect(self.db_path)) as connection,self.assertRaisesRegex(sqlite3.IntegrityError,"collector limit"):
            connection.execute("INSERT INTO claims VALUES(?,?,?,?)",(drop["id"],"c",instance["id"],self.now))

    def test_alias_normalization_and_persisted_wrong_guess_cooldown(self):
        drop = self.game.create_drop("guild","names","alpha",now=self.now)
        self.assert_error("wrong_name",self.game.claim,drop["id"],"a","Vader",now=self.now)
        restarted = core.Game(self.db_path,self.catalog_path)
        self.assert_error("guess_cooldown",restarted.claim,drop["id"],"a","Artoo",now=self.now+2)
        caught = restarted.claim(drop["id"],"a"," ＡＬＰＨＡ   trooper!!! ",now=self.now+3)
        self.assertEqual(caught["instance"]["card_id"],"alpha")
        self.assertEqual(core.normalize_alias("R2-D₂"),"r2d2")

    def test_one_active_drop_per_channel_and_expiry_recovery(self):
        first = self.game.create_drop("guild","same","alpha",now=self.now,duration=2)
        self.assert_error("active_drop",self.game.create_drop,"guild","same","bravo",now=self.now+1)
        self.assert_error("drop_closed",self.game.claim,first["id"],"a","Artoo",now=self.now+2)
        restarted = core.Game(self.db_path,self.catalog_path)
        second = restarted.create_drop("guild","same","bravo",now=self.now+3)
        self.assertNotEqual(first["id"],second["id"])
        history = restarted.recent_drops("guild",now=self.now+3)
        self.assertEqual({row["status"] for row in history},{"active","expired"})

    def test_supply_reservations_global_and_never_exceed_cap(self):
        one = self.game.create_drop("one","a","limited",now=self.now)
        two = self.game.create_drop("two","b","limited",now=self.now)
        self.assert_error("unavailable_card",self.game.create_drop,"three","c","limited",now=self.now)
        self.assertNotIn("limited",{item["id"] for item in self.game.spawn_pool(now=self.now)})
        for drop in [one,two]:
            for user in ["a","b"]:
                self.game.claim(drop["id"],user,"Limited",now=self.now)
        with contextlib.closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT minted,reserved FROM supply WHERE card_id='limited'").fetchone(),(4,0))

    def test_expiry_releases_unclaimed_reserved_supply(self):
        drop = self.game.create_drop("guild","limited","limited",now=self.now,duration=2)
        self.game.claim(drop["id"],"a","Limited",now=self.now)
        self.game.active_drops(now=self.now+2)
        with contextlib.closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT minted,reserved FROM supply WHERE card_id='limited'").fetchone(),(1,0))
        self.game.create_drop("other","limited","limited",now=self.now+3)

    def test_release_dates_and_weight_probabilities_are_explicit(self):
        self.assert_error("unavailable_card",self.game.create_drop,"guild","future","future",now=self.now+99)
        pool = self.game.spawn_pool(now=self.now)
        probabilities = {item["id"]:item["probability"] for item in pool}
        self.assertAlmostEqual(sum(probabilities.values()),1)
        self.assertAlmostEqual(probabilities["alpha"],2*probabilities["bravo"])
        start,end = (core.timestamp(self.game.catalog["future"]["release"][key]) for key in ("starts_at","ends_at"))
        drop = self.game.create_drop("guild","future","future",now=start,duration=180)
        self.assertEqual(drop["expires_at"],end)
        self.assert_error("drop_closed",self.game.claim,drop["id"],"a","Future",now=end)

    def test_inventory_privacy_search_favorite_wishlist_and_profile(self):
        mine = self.capture("a")
        self.capture("b","bravo")
        self.capture("a","ship",guild="other")
        self.assertEqual(len(self.game.inventory("guild","a",search="alpha")),1)
        self.assertEqual(self.game.inventory("guild","a",search="nothing"),[])
        self.assert_error("not_owned",self.game.info,"guild","b",mine["id"])
        self.assert_error("not_owned",self.game.set_favorite,"other","a",mine["id"],True)
        self.assertTrue(self.game.set_favorite("guild","a",mine["id"],True)["favorite"])
        self.game.set_wishlist("guild","a","delta",True)
        self.game.set_wishlist("guild","a","delta",True)
        self.assertEqual([card["id"] for card in self.game.wishlist("guild","a")],["delta"])
        profile = self.game.profile("guild","a")
        self.assertEqual((profile["total_cards"],profile["unique_cards"],profile["catalog_count"]),(1,1,8))
        self.assertEqual(profile["completion_percent"],12.5)
        self.assertEqual((profile["all_catalog_count"],profile["event_catalog_count"]),(11,3))

    def test_daily_race_once_per_utc_day_and_streak(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            def award(_):
                try:
                    return core.Game(self.db_path,self.catalog_path).daily("guild","a",now=self.now)
                except core.GameError as error:
                    return error.code
            results = list(pool.map(award,range(6)))
        self.assertEqual(sum(isinstance(result,dict) for result in results),1)
        self.assertEqual(self.game.profile("guild","a")["credits"],120)
        next_day = self.game.daily("guild","a",now=self.now+86400)
        self.assertEqual((next_day["streak"],next_day["credits_earned"]),(2,130))

    def test_salvage_preserves_last_copy_and_favorites(self):
        first,second = self.capture("a"),self.capture("a")
        self.game.set_favorite("guild","a",first["id"],True)
        self.assert_error("protected_card",self.game.salvage,"guild","a",[first["id"]],now=self.now)
        result = self.game.salvage("guild","a",[second["id"]],now=self.now)
        self.assertEqual(result["credits_earned"],20)
        self.game.set_favorite("guild","a",first["id"],False)
        self.assert_error("last_copy",self.game.salvage,"guild","a",[first["id"]],now=self.now)
        self.assert_error("not_owned",self.game.salvage,"guild","a",[second["id"]],now=self.now)
        self.assertEqual(self.game.profile("guild","a")["credits"],70)

    def test_crafting_atomically_consumes_three_and_credits_once(self):
        items = [self.seed_inventory("a",card) for card in ["alpha","bravo","charlie"]]
        ids = [item["id"] for item in items]
        self.assert_error("insufficient_credits",self.game.craft,"guild","a",ids,"delta",now=self.now)
        self.assertEqual(self.game.profile("guild","a")["total_cards"],3)
        self.game.daily("guild","a",now=self.now)
        result = self.game.craft("guild","a",ids,"delta",now=self.now)
        self.assertEqual((result["instance"]["card_id"],result["credits"]),("delta",45))
        self.assertEqual(self.game.profile("guild","a")["total_cards"],1)
        self.assert_error("not_owned",self.game.craft,"guild","a",ids,"delta",now=self.now)

    def test_invalid_crafting_rolls_back_and_protects_deck(self):
        items = [self.seed_inventory("a",card) for card in ["alpha","bravo","sith"]]
        self.game.daily("guild","a",now=self.now)
        self.assert_error("invalid_recipe",self.game.craft,"guild","a",[item["id"] for item in items],"delta",now=self.now)
        self.game.set_deck("guild","a","ground",[items[0]["id"]],now=self.now)
        self.assert_error("protected_card",self.game.craft,"guild","a",[item["id"] for item in items],"delta",now=self.now)
        self.assertEqual(self.game.profile("guild","a")["credits"],195)

    def prepare_trade(self):
        left,right = self.capture("a"),self.capture("b","bravo")
        trade = self.game.trade_create("guild","a","b",now=self.now)
        self.game.trade_offer(trade["id"],"a",[left["id"]],now=self.now)
        trade = self.game.trade_offer(trade["id"],"b",[right["id"]],now=self.now)
        return left,right,trade

    def test_one_sided_trade_completes_as_gift(self):
        left = self.seed_inventory("a")
        trade = self.game.trade_create("guild","a","b",now=self.now)
        trade = self.game.trade_offer(trade["id"],"a",[left["id"]],now=self.now)
        self.game.trade_accept(trade["id"],"a",trade["version"],now=self.now)
        done = self.game.trade_accept(trade["id"],"b",trade["version"],now=self.now)
        self.assertEqual(done["status"],"completed")
        self.assertEqual(self.game.info("guild","b",left["id"])["user_id"],"b")

    def test_empty_trade_cannot_be_confirmed(self):
        trade = self.game.trade_create("guild","a","b",now=self.now)
        self.assert_error("empty_trade",self.game.trade_accept,trade["id"],"a",trade["version"],now=self.now)

    def test_parallel_trades_and_same_pair_reopens(self):
        first = self.game.trade_create("guild","a","b",now=self.now)
        again = self.game.trade_create("guild","b","a",now=self.now)
        self.assertEqual(first["id"],again["id"])
        self.assertTrue(again["reopened"])
        self.game.trade_create("guild","a","c",now=self.now)
        self.game.trade_create("guild","a","d",now=self.now)
        self.assert_error("active_trade",self.game.trade_create,"guild","a","e",now=self.now)
        self.game.trade_create("guild","b","e",now=self.now)

    def test_trade_allows_deck_and_favorite_cards_and_clears_them(self):
        item = self.seed_inventory("a")
        self.game.set_deck("guild","a","ground",[item["id"]],now=self.now)
        self.game.set_favorite("guild","a",item["id"],True)
        trade = self.game.trade_create("guild","a","b",now=self.now)
        trade = self.game.trade_offer(trade["id"],"a",[item["id"]],now=self.now)
        self.game.trade_accept(trade["id"],"a",trade["version"],now=self.now)
        self.game.trade_accept(trade["id"],"b",trade["version"],now=self.now)
        moved = self.game.info("guild","b",item["id"])
        self.assertFalse(moved["favorite"])
        self.assertEqual(self.game.deck("guild","a","ground")["cards"],[])

    def test_give_moves_cards_once_and_records_transfer(self):
        item = self.seed_inventory("a")
        self.game.set_deck("guild","a","ground",[item["id"]],now=self.now)
        result = self.game.give("guild","a","b",[item["id"]],now=self.now,request_id="r1",note="gg")
        self.assertEqual(result["to_id"],"b")
        self.assertEqual(self.game.info("guild","b",item["id"])["user_id"],"b")
        self.assertEqual(self.game.deck("guild","a","ground")["cards"],[])
        replay = self.game.give("guild","a","b",[item["id"]],now=self.now,request_id="r1")
        self.assertTrue(replay["replayed"])
        self.assert_error("not_owned",self.game.give,"guild","a","b",[item["id"]],now=self.now)
        self.assert_error("self_gift",self.game.give,"guild","b","b",[item["id"]],now=self.now)
        self.assertEqual(self.game.transfers("guild")[0]["kind"],"gift")

    def test_give_rejects_escrowed_cards(self):
        item = self.seed_inventory("a")
        trade = self.game.trade_create("guild","a","c",now=self.now)
        self.game.trade_offer(trade["id"],"a",[item["id"]],now=self.now)
        self.assert_error("escrow_locked",self.game.give,"guild","a","b",[item["id"]],now=self.now)

    def test_admin_grant_mints_and_respects_edition_caps(self):
        result = self.game.admin_grant("guild","admin","b","echo",3,now=self.now,request_id="g1")
        self.assertEqual(len(result["instance_ids"]),3)
        self.assertEqual(len(self.game.inventory("guild","b")),3)
        self.assertTrue(self.game.admin_grant("guild","admin","b","echo",3,now=self.now,request_id="g1")["replayed"])
        self.assertEqual(len(self.game.inventory("guild","b")),3)
        self.assert_error("unavailable_card",self.game.admin_grant,"guild","admin","b","limited",5,now=self.now)
        self.game.admin_grant("guild","admin","b","limited",4,now=self.now)
        self.assert_error("invalid_count",self.game.admin_grant,"guild","admin","b","echo",11,now=self.now)

    def test_admin_clear_inventory_cancels_trades_and_can_be_undone(self):
        keep = self.seed_inventory("a")
        other = self.seed_inventory("a","bravo")
        self.game.set_deck("guild","a","ground",[keep["id"]],now=self.now)
        trade = self.game.trade_create("guild","a","b",now=self.now)
        self.game.trade_offer(trade["id"],"a",[other["id"]],now=self.now)
        result = self.game.admin_clear_inventory("guild","admin","a",now=self.now,request_id="c1")
        self.assertEqual(sorted(result["instance_ids"]),sorted([keep["id"],other["id"]]))
        self.assertEqual(self.game.inventory("guild","a"),[])
        self.assertEqual(self.game.get_trade(trade["id"])["status"],"cancelled")
        self.assertEqual(self.game.deck("guild","a","ground")["cards"],[])
        self.assertTrue(self.game.admin_clear_inventory("guild","admin","a",now=self.now,request_id="c1")["replayed"])
        restored = self.game.admin_restore_inventory("guild","admin",result["id"],now=self.now+5)
        self.assertEqual(restored["restored"],2)
        self.assertEqual(len(self.game.inventory("guild","a")),2)
        self.assert_error("already_restored",self.game.admin_restore_inventory,"guild","admin",result["id"])
        self.assert_error("unknown_clear",self.game.admin_restore_inventory,"other-guild","admin",result["id"])

    def test_trade_requires_two_explicit_accepts_and_atomic_ownership(self):
        left,right,trade = self.prepare_trade()
        first = self.game.trade_accept(trade["id"],"a",trade["version"],now=self.now)
        self.assertEqual(first["status"],"open")
        self.assertEqual(self.game.info("guild","a",left["id"])["user_id"],"a")
        complete = self.game.trade_accept(trade["id"],"b",trade["version"],now=self.now)
        self.assertEqual(complete["status"],"completed")
        self.assertEqual(self.game.info("guild","a",right["id"])["user_id"],"a")
        self.assertEqual(self.game.info("guild","b",left["id"])["user_id"],"b")
        self.assertIsNone(self.game.info("guild","b",left["id"])["locked_by"])
        self.assert_error("trade_closed",self.game.trade_accept,trade["id"],"b",trade["version"],now=self.now)
        # Completed-history rows cannot block re-trading those same assets.
        repeat = self.game.trade_create("guild","b","a",now=self.now)
        self.game.trade_offer(repeat["id"],"b",[left["id"]],now=self.now)

    def test_stale_trade_accept_reset_and_unknown_actor_are_rejected(self):
        left,right,trade = self.prepare_trade()
        self.game.trade_accept(trade["id"],"a",trade["version"],now=self.now)
        changed = self.game.trade_offer(trade["id"],"b",[right["id"]],now=self.now)
        self.assertFalse(any(changed["accepted"].values()))
        self.assert_error("stale_trade",self.game.trade_accept,trade["id"],"a",trade["version"],now=self.now)
        self.assert_error("not_participant",self.game.trade_accept,trade["id"],"attacker",changed["version"],now=self.now)
        self.assert_error("protected_card",self.game.salvage,"guild","a",[left["id"]],now=self.now)
        self.assert_error("escrow_locked",self.game.set_favorite,"guild","a",left["id"],True)

    def test_failed_offer_replacement_restores_original_escrow(self):
        left,right,trade = self.prepare_trade()
        self.assert_error("not_owned",self.game.trade_offer,trade["id"],"a",[right["id"]],now=self.now)
        restored = self.game.get_trade(trade["id"],now=self.now)
        self.assertEqual(restored["offers"]["a"][0]["id"],left["id"])
        self.assertEqual(restored["version"],trade["version"])
        self.assertEqual(self.game.info("guild","a",left["id"])["locked_by"],trade["id"])

    def test_trade_expiry_and_restart_return_assets_without_transfer(self):
        left,right,trade = self.prepare_trade()
        restarted = core.Game(self.db_path,self.catalog_path)
        self.assertEqual(restarted.get_trade(trade["id"],now=self.now+3601)["status"],"expired")
        self.assertIsNone(restarted.info("guild","a",left["id"])["locked_by"])
        self.assertIsNone(restarted.info("guild","b",right["id"])["locked_by"])

    def test_trade_catalog_changes_invalidate_both_approvals_before_transfer(self):
        original = json.loads(self.catalog_path.read_text())
        for field,value in [("name","Changed Alpha"),("rarity","rare"),("attack",101),
                            ("arena","fleet"),("faction","Sith"),("role","guardian"),("ability","focused_strike")]:
            with self.subTest(field=field):
                left,right,trade = self.prepare_trade()
                self.game.trade_accept(trade["id"],"a",trade["version"],now=self.now)
                changed = json.loads(json.dumps(original))
                changed["cards"][0][field] = value
                self.catalog_path.write_text(json.dumps(changed))
                restarted = core.Game(self.db_path,self.catalog_path)
                self.assert_error("stale_trade",restarted.trade_accept,trade["id"],"b",trade["version"],now=self.now)
                refreshed = restarted.get_trade(trade["id"],now=self.now)
                self.assertEqual(refreshed["version"],trade["version"]+1)
                self.assertFalse(any(refreshed["accepted"].values()))
                self.assertEqual(restarted.info("guild","a",left["id"])["locked_by"],trade["id"])
                self.assertEqual(restarted.info("guild","b",right["id"])["locked_by"],trade["id"])
                first = restarted.trade_accept(trade["id"],"b",refreshed["version"],now=self.now)
                self.assertEqual(first["status"],"open")
                complete = restarted.trade_accept(trade["id"],"a",refreshed["version"],now=self.now)
                self.assertEqual(complete["status"],"completed")
                self.catalog_path.write_text(json.dumps(original))
                self.game = core.Game(self.db_path,self.catalog_path)

    def test_trade_reference_value_change_resets_review_on_read(self):
        _left,_right,trade = self.prepare_trade()
        self.game.trade_accept(trade["id"],"a",trade["version"],now=self.now)
        document = json.loads(self.catalog_path.read_text())
        document["rarities"]={"common":{"value":99}}
        self.catalog_path.write_text(json.dumps(document))
        restarted = core.Game(self.db_path,self.catalog_path)
        changed = restarted.get_trade(trade["id"],now=self.now)
        self.assertEqual(changed["values"],{"a":99,"b":99})
        self.assertEqual(changed["version"],trade["version"]+1)
        self.assertFalse(any(changed["accepted"].values()))
        self.assert_error("stale_trade",restarted.trade_accept,trade["id"],"b",trade["version"],now=self.now)

    def test_trade_metadata_only_edit_and_unchanged_restart_preserve_review(self):
        _left,_right,trade = self.prepare_trade()
        self.game.trade_accept(trade["id"],"a",trade["version"],now=self.now)
        unchanged = core.Game(self.db_path,self.catalog_path).get_trade(trade["id"],now=self.now)
        self.assertTrue(unchanged["accepted"]["a"])
        self.assertEqual(unchanged["version"],trade["version"])
        document = json.loads(self.catalog_path.read_text())
        document["cards"][0].update(description="Corrected biography",lore_sources=["https://www.starwars.com/databank"],
                                   art_review={"checked_at":"synthetic metadata"})
        self.catalog_path.write_text(json.dumps(document))
        restarted = core.Game(self.db_path,self.catalog_path)
        still = restarted.get_trade(trade["id"],now=self.now)
        self.assertEqual(still["version"],trade["version"])
        self.assertTrue(still["accepted"]["a"])
        self.assertEqual(restarted.trade_accept(trade["id"],"b",trade["version"],now=self.now)["status"],"completed")

    def test_legacy_trade_migration_requires_fresh_two_sided_review(self):
        left,right,trade = self.prepare_trade()
        self.game.trade_accept(trade["id"],"a",trade["version"],now=self.now)
        with self.game._tx() as connection:
            connection.execute("ALTER TABLE trades DROP COLUMN review_fingerprint")
        restarted = core.Game(self.db_path,self.catalog_path)
        self.assert_error("stale_trade",restarted.trade_accept,trade["id"],"b",trade["version"],now=self.now)
        changed = restarted.get_trade(trade["id"],now=self.now)
        self.assertFalse(any(changed["accepted"].values()))
        self.assertEqual(changed["version"],trade["version"]+1)
        self.assertEqual(restarted.info("guild","a",left["id"])["user_id"],"a")
        self.assertEqual(restarted.info("guild","b",right["id"])["user_id"],"b")

    def test_trade_acceptance_race_and_guild_isolation(self):
        _left,_right,trade = self.prepare_trade()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda user:core.Game(self.db_path,self.catalog_path).trade_accept(trade["id"],user,trade["version"],now=self.now),["a","b"]))
        self.assertEqual(sum(result["status"]=="completed" for result in outcomes),1)
        foreign = self.capture("a",guild="other")
        new = self.game.trade_create("guild","a","b",now=self.now)
        self.assert_error("not_owned",self.game.trade_offer,new["id"],"a",[foreign["id"]],now=self.now)

    def test_deck_faction_preview_and_separate_fleet_arena(self):
        items = [self.seed_inventory("a",card) for card in ["alpha","bravo","charlie"]]
        preview = self.game.set_deck("guild","a","ground",[item["id"] for item in items],now=self.now)
        self.assertEqual((preview["attack"],preview["health"]),(330,1650))
        self.assertEqual({item["faction_bonus"] for item in preview["cards"]},{0.1})
        fleet = self.seed_inventory("a","ship")
        self.assert_error("mixed_arena",self.game.set_deck,"guild","a","ground",[fleet["id"]],now=self.now)
        duplicate = self.seed_inventory("a")
        self.assert_error("duplicate_card",self.game.set_deck,"guild","a","ground",[items[0]["id"],duplicate["id"]],now=self.now)

    def test_neutral_classifications_never_grant_faction_combat_or_raid_bonus(self):
        for faction in ["Independent","Wildlife"]:
            with self.subTest(faction=faction):
                for key in ["alpha","bravo","charlie"]:
                    self.game.catalog[key]["faction"] = faction
                items = [self.seed_inventory(faction,key) for key in ["alpha","bravo","charlie"]]
                for size in [2,3]:
                    preview = self.game.set_deck("guild",faction,"ground",[item["id"] for item in items[:size]],now=self.now)
                    self.assertEqual((preview["attack"],preview["health"]),(100*size,500*size))
                    self.assertEqual({item["faction_bonus"] for item in preview["cards"]},{0})
                # Striker role still contributes 10%; the unrelated neutral
                # classification must not add an extra team multiplier.
                self.assertEqual(self.game.raid_damage(preview),310)

    def test_pvp_requires_opponent_accept_and_persisted_replays_no_economy_gain(self):
        left,right = self.capture("a"),self.capture("b","bravo")
        self.game.set_deck("guild","a","ground",[left["id"]],now=self.now)
        self.game.set_deck("guild","b","ground",[right["id"]],now=self.now)
        duel = self.game.duel_create("guild","a","b","ground",now=self.now)
        self.assert_error("not_opponent",self.game.duel_accept,duel["id"],"a",now=self.now)
        result = self.game.duel_accept(duel["id"],"b",now=self.now)
        self.assertEqual(result["status"],"completed")
        self.assertEqual(self.game.profile("guild","a")["credits"],25)
        self.assertEqual(core.Game(self.db_path,self.catalog_path).duel_accept(duel["id"],"b",now=self.now),result)
        self.assertTrue(any("bulwark −30%" in row["effects"] for row in result["result"]["log"]))
        self.assertTrue(any("focused strike +25%" in row["effects"] for row in result["result"]["log"]))

    def prepare_raid(self):
        for user,card in [("a","alpha"),("b","bravo")]:
            item = self.capture(user,card)
            self.game.set_deck("guild",user,"ground",[item["id"]],now=self.now)
        raid = self.game.raid_create("guild","raid",now=self.now,signup=10,duration=200)
        for user in ["a","b"]:
            self.game.raid_join(raid["id"],user,now=self.now)
        return raid

    def test_mirror_pvp_simultaneous_initiative_has_no_challenger_advantage(self):
        for user in ["a","b"]:
            item = self.capture(user)
            self.game.set_deck("guild",user,"ground",[item["id"]],now=self.now)
        duel = self.game.duel_create("guild","a","b","ground",now=self.now)
        result = self.game.duel_accept(duel["id"],"b",now=self.now)["result"]
        self.assertIsNone(result["winner_id"])
        self.assertEqual(result["remaining_hp"],[0,0])

    def test_combat_replays_deterministically_and_side_order_does_not_change_winner(self):
        decks = []
        for user,card in [("a","alpha"),("b","bravo")]:
            item = self.capture(user,card)
            decks.append(self.game.set_deck("guild",user,"ground",[item["id"]],now=self.now))
        result = self.game.combat(*decks)
        self.assertEqual(result,self.game.combat(*decks))
        reversed_result = self.game.combat(*reversed(decks))
        self.assertEqual(result["winner_side"],1-reversed_result["winner_side"])
        self.assertEqual(result["remaining_hp"],list(reversed(reversed_result["remaining_hp"])))

    def prepare_duel(self):
        left,right,alternative = self.capture("a"),self.capture("b","bravo"),self.capture("a","sith")
        self.game.set_deck("guild","a","ground",[left["id"]],now=self.now)
        self.game.set_deck("guild","b","ground",[right["id"]],now=self.now)
        return left,alternative,self.game.duel_create("guild","a","b","ground",now=self.now)

    def test_duel_persisted_preview_rejects_unseen_deck_changes(self):
        left,alternative,duel = self.prepare_duel()
        restarted = core.Game(self.db_path,self.catalog_path)
        self.assertEqual(restarted.get_duel(duel["id"])["preview"][0]["cards"][0]["id"],left["id"])
        self.game.set_deck("guild","a","ground",[alternative["id"]],now=self.now)
        self.assert_error("stale_duel",restarted.duel_accept,duel["id"],"b",now=self.now)
        self.assertIsNone(restarted.get_duel(duel["id"])["result"])

    def test_duel_preview_detects_catalog_stats_changed_after_restart(self):
        _left,_alternative,duel = self.prepare_duel()
        document = json.loads(self.catalog_path.read_text())
        document["cards"][0]["attack"] += 1
        self.catalog_path.write_text(json.dumps(document))
        restarted = core.Game(self.db_path,self.catalog_path)
        self.assert_error("stale_duel",restarted.duel_accept,duel["id"],"b",now=self.now)

    def test_catalog_arena_change_requires_rebuilding_saved_deck(self):
        left,_alternative,duel = self.prepare_duel()
        document = json.loads(self.catalog_path.read_text())
        document["cards"][0].update(arena="fleet",attack=800,health=4000)
        self.catalog_path.write_text(json.dumps(document))
        restarted = core.Game(self.db_path,self.catalog_path)
        self.assert_error("stale_deck",restarted.deck,"guild","a","ground")
        self.assert_error("stale_deck",restarted.duel_create,"guild","a","b","ground",now=self.now)
        self.assert_error("stale_duel",restarted.duel_accept,duel["id"],"b",now=self.now)
        self.assertIsNone(restarted.get_duel(duel["id"])["result"])
        self.assertEqual(restarted.info("guild","a",left["id"])["card"]["arena"],"fleet")
        self.assertEqual(restarted.set_deck("guild","a","ground",[],now=self.now)["cards"],[])
        fleet = restarted.set_deck("guild","a","fleet",[left["id"]],now=self.now)
        self.assertEqual(fleet["cards"][0]["id"],left["id"])

    def test_duel_accept_deck_change_race_never_uses_unreviewed_team(self):
        left,alternative,duel = self.prepare_duel()
        barrier = threading.Barrier(2)
        def accept():
            engine = core.Game(self.db_path,self.catalog_path)
            barrier.wait(timeout=5)
            try:
                return engine.duel_accept(duel["id"],"b",now=self.now)
            except core.GameError as error:
                return error.code
        def replace():
            engine = core.Game(self.db_path,self.catalog_path)
            barrier.wait(timeout=5)
            return engine.set_deck("guild","a","ground",[alternative["id"]],now=self.now)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            accepted,changed = pool.submit(accept),pool.submit(replace)
            result = accepted.result()
            changed.result()
        if isinstance(result,dict):
            self.assertEqual(result["result"]["decks"][0]["cards"][0]["id"],left["id"])
        else:
            self.assertEqual(result,"stale_duel")

    def test_raid_roster_scaling_cooldown_and_rewards_exactly_once(self):
        raid = self.prepare_raid()
        active = self.game.get_raid(raid["id"],now=self.now+10)
        self.assertEqual(active["max_hp"],536)
        self.assert_error("roster_locked",self.game.raid_join,raid["id"],"late",now=self.now+10)
        self.game.raid_attack(raid["id"],"a",now=self.now+10)
        self.assert_error("attack_cooldown",self.game.raid_attack,raid["id"],"a",now=self.now+11)
        final = None
        for user,seconds in [("b",10),("a",20),("b",20),("a",30),("b",30)]:
            final = self.game.raid_attack(raid["id"],user,now=self.now+seconds)
        self.assertEqual(final["status"],"defeated")
        self.assertEqual(len(final["rewards"]),2)
        self.assertEqual(self.game.profile("guild","a")["total_cards"],1)
        balance = self.game.profile("guild","a")["credits"]
        self.assert_error("raid_not_active",self.game.raid_attack,raid["id"],"a",now=self.now+40)
        self.assertEqual(core.Game(self.db_path,self.catalog_path).profile("guild","a")["credits"],balance)

    def test_raid_concurrent_same_user_attacks_only_once_and_snapshot_is_fixed(self):
        raid = self.prepare_raid()
        self.game.set_deck("guild","a","ground",[],now=self.now)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            def attack(_):
                try:
                    return core.Game(self.db_path,self.catalog_path).raid_attack(raid["id"],"a",now=self.now+10)
                except core.GameError as error:
                    return error.code
            results = list(pool.map(attack,range(4)))
        self.assertEqual(sum(isinstance(result,dict) for result in results),1)
        self.assertEqual(sum(result=="attack_cooldown" for result in results),3)
        member = next(row for row in self.game.get_raid(raid["id"],now=self.now+11)["participants"] if row["user_id"]=="a")
        self.assertEqual((member["per_attack"],member["attacks"]),(110,1))

    def test_raid_joined_stats_and_relative_difficulty_survive_catalog_rebalance(self):
        raid = self.prepare_raid()
        joined = self.game.get_raid(raid["id"], now=self.now)
        document = json.loads(self.catalog_path.read_text())
        for card in document["cards"]:
            card["attack"] *= 4
            card["health"] *= 4
        self.catalog_path.write_text(json.dumps(document))
        restarted = core.Game(self.db_path, self.catalog_path)
        self.assertEqual(restarted.deck("guild", "a", "ground")["attack"], 400)
        active = restarted.get_raid(raid["id"], now=self.now+10)
        self.assertEqual(active["participants"], joined["participants"])
        self.assertEqual(active["max_hp"], 536)
        attack = restarted.raid_attack(raid["id"], "a", now=self.now+10)
        self.assertEqual(attack["damage_dealt"], 110)
        # A newly joined roster uses current stats and the same relative target.
        updated = restarted.raid_create("guild", "new-balance", now=self.now+11, signup=10, duration=100)
        for user in ("a", "b"):
            restarted.raid_join(updated["id"], user, now=self.now+11)
        current = restarted.get_raid(updated["id"], now=self.now+21)
        self.assertEqual(sum(member["per_attack"] for member in current["participants"]), 840)
        self.assertEqual(current["max_hp"], math.ceil(840*3*.85))

    def test_raids_use_roster_damage_not_an_arena_scale(self):
        for arena, card_id in (("ground", "alpha"), ("fleet", "ship")):
            item = self.capture(arena, card_id)
            self.game.set_deck("guild", arena, arena, [item["id"]], now=self.now)
            raid = self.game.raid_create("guild", arena, arena, now=self.now, signup=10, duration=100)
            self.game.raid_join(raid["id"], arena, now=self.now)
            active = self.game.get_raid(raid["id"], now=self.now+10)
            self.assertEqual(active["participants"][0]["per_attack"], 110)
            self.assertEqual(active["max_hp"], 281)
            for seconds in (10, 20, 30):
                final = self.game.raid_attack(raid["id"], arena, now=self.now+seconds)
            self.assertEqual(final["status"], "defeated")
            self.assertEqual(final["participants"][0]["attacks"], 3)

    def test_raid_expiry_restart_and_message_reconciliation(self):
        raid = self.prepare_raid()
        self.game.attach_raid_message(raid["id"],"message")
        restarted = core.Game(self.db_path,self.catalog_path)
        recent = restarted.recent_raids("guild",now=self.now+201)
        self.assertEqual((recent[0]["status"],recent[0]["message_id"]),("escaped","message"))
        self.assertEqual(restarted.active_raids(now=self.now+201),[])

    def test_active_raid_loadout_cannot_be_reused_via_trade_after_clearing_deck(self):
        self.prepare_raid()
        original = self.game.deck("guild","a","ground")["cards"][0]["id"]
        self.game.set_deck("guild","a","ground",[],now=self.now)
        trade = self.game.trade_create("guild","a","b",now=self.now)
        self.assert_error("protected_card",self.game.trade_offer,trade["id"],"a",[original],now=self.now)
        # Expiration releases raid protection through the normal transaction
        # path; no scheduler callback is needed to unlock the owner's asset.
        offered = self.game.trade_offer(trade["id"],"a",[original],now=self.now+201)
        self.assertEqual(offered["offers"]["a"][0]["id"],original)

    def test_legends_event_default_off_across_random_explicit_and_crafting(self):
        self.assertIn("legends",self.game.catalog)  # discovery remains available
        self.assertNotIn("legends",{row["id"] for row in self.game.spawn_pool(now=self.now)})
        self.assert_error("unavailable_card",self.game.create_drop,"guild","event","legends",now=self.now)
        ingredients = [self.seed_inventory("a",key)["id"] for key in ["alpha","bravo","charlie"]]
        self.game.daily("guild","a",now=self.now)
        self.assert_error("event_disabled",self.game.craft,"guild","a",ingredients,"legends",now=self.now)
        self.assertEqual(self.game.profile("guild","a")["total_cards"],3)
        self.assertEqual(self.game.profile("guild","a")["credits"],195)

    def test_event_owned_cards_do_not_distort_regular_completion(self):
        self.game = core.Game(self.db_path,self.catalog_path,enabled_events=["archive"])
        self.capture("a","legends",event_id="archive")
        self.capture("a","alpha")
        profile = self.game.profile("guild","a")
        self.assertEqual((profile["unique_cards"],profile["catalog_count"],profile["completion_percent"]),(1,8,12.5))
        self.assertEqual((profile["all_unique_cards"],profile["event_unique_cards"],profile["event_catalog_count"]),(2,1,3))

    def test_legends_require_matching_event_id_and_active_window(self):
        wrong = core.Game(self.db_path,self.catalog_path,enabled_events=["wrong"])
        self.assert_error("unavailable_card",wrong.create_drop,"guild","wrong","legends",now=self.now,event_id="archive")
        enabled = core.Game(self.db_path,self.catalog_path,enabled_events=["archive"])
        self.assertNotIn("legends",{row["id"] for row in enabled.spawn_pool(now=self.now)})
        self.assertIn("legends",{row["id"] for row in enabled.spawn_pool(now=self.now,event_id="archive")})
        self.assert_error("unavailable_card",enabled.create_drop,"guild","regular","legends",now=self.now)
        self.assert_error("unavailable_card",enabled.create_drop,"guild","early","legends",now=self.now-61,event_id="archive")
        self.assert_error("unavailable_card",enabled.create_drop,"guild","late","legends",now=core.timestamp(enabled.catalog["legends"]["release"]["ends_at"]),event_id="archive")
        drop = enabled.create_drop("guild","live","legends",now=self.now,event_id="archive")
        # An event disabled across restart closes unclaimed slots safely.
        disabled = core.Game(self.db_path,self.catalog_path)
        self.assert_error("drop_closed",disabled.claim,drop["id"],"a","Legends",now=self.now)

    def test_event_crafting_honors_supply_and_ingredient_gate(self):
        self.game = core.Game(self.db_path,self.catalog_path,enabled_events=["archive"])
        ingredients = [self.seed_inventory("a",key)["id"] for key in ["alpha","bravo","charlie"]]
        self.game.daily("guild","a",now=self.now)
        self.assert_error("event_disabled",self.game.craft,"guild","a",ingredients,"legends",now=self.now)
        crafted = self.game.craft("guild","a",ingredients,"legends",now=self.now,event_id="archive")
        self.assertEqual(crafted["instance"]["card_id"],"legends")
        held = [crafted["instance"]["id"],self.capture("a","legends",event_id="archive")["id"],self.capture("a","legends",event_id="archive")["id"]]
        disabled = core.Game(self.db_path,self.catalog_path)
        self.assert_error("event_disabled",disabled.craft,"guild","a",held,"echo",now=self.now,event_id="archive")
        # Fill the edition using real mint paths, including both reserved
        # capture slots, then prove a further craft cannot oversupply it.
        for drop in self.game.active_drops(now=self.now):
            if drop["card_id"]=="legends":
                self.game.claim(drop["id"],"peer","Legends",now=self.now)
        drop = self.game.create_drop("guild","fill","legends",now=self.now,event_id="archive")
        for user in ["extra","peer"]:
            self.game.claim(drop["id"],user,"Legends",now=self.now)
        normal = [self.seed_inventory("b",key)["id"] for key in ["alpha","bravo","charlie"]]
        self.game.daily("guild","b",now=self.now)
        self.game.craft("guild","b",normal,"legends",now=self.now,event_id="archive")
        normal = [self.seed_inventory("c",key)["id"] for key in ["alpha","bravo","charlie"]]
        self.game.daily("guild","c",now=self.now)
        self.assert_error("unavailable_card",self.game.craft,"guild","c",normal,"legends",now=self.now,event_id="archive")

    def test_legends_without_explicit_event_window_fail_closed(self):
        document = json.loads(self.catalog_path.read_text())
        document["cards"][-1].pop("event_id")
        self.catalog_path.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError,"Event-exclusive"):
            core.Game(self.db_path,self.catalog_path)

    def test_backup_is_consistent_restartable_and_wal_enabled(self):
        item = self.capture("a")
        backup = self.directory/"backup.sqlite"
        receipt = self.game.backup(backup)
        self.assertGreater(receipt["bytes"],0)
        restored = core.Game(backup,self.catalog_path)
        self.assertEqual(restored.info("guild","a",item["id"])["id"],item["id"])
        self.assertEqual(restored.health()["integrity"],"ok")
        self.assertEqual(restored.health()["journal_mode"],"wal")

    def test_catalog_cannot_remove_definitions_referenced_by_saved_state(self):
        owned = self.seed_inventory("a")
        self.game.set_favorite("guild","a",owned["id"],True)
        ingredients = [self.seed_inventory("a","bravo") for _ in range(3)]
        self.game.daily("guild","a",now=self.now)
        self.game.craft("guild","a",[item["id"] for item in ingredients],"delta",now=self.now)
        self.game.create_drop("guild","unclaimed-history","charlie",now=self.now,duration=1)
        self.game.active_drops(now=self.now+2)
        self.game.set_wishlist("guild","a","sith",True)
        with self.game._tx() as connection:
            connection.execute("UPDATE supply SET minted=1 WHERE card_id='echo'")
            before = list(connection.iterdump())
        original = json.loads(self.catalog_path.read_text())
        for card_id in ("alpha","bravo","charlie","delta","sith","echo"):
            with self.subTest(reference=card_id):
                changed = {**original,"cards":[card for card in original["cards"] if card["id"]!=card_id]}
                changed["cards"].append({**original["cards"][0],"id":"new-not-inserted"})
                self.catalog_path.write_text(json.dumps(changed))
                with self.assertRaisesRegex(ValueError,"referenced by saved state: "+card_id):
                    core.Game(self.db_path,self.catalog_path)
                with self.game._tx() as connection:
                    self.assertEqual(list(connection.iterdump()),before)
        self.catalog_path.write_text(json.dumps(original))
        restored = core.Game(self.db_path,self.catalog_path)
        self.assertTrue(restored.info("guild","a",owned["id"])["favorite"])
        self.assertEqual(restored.wishlist("guild","a")[0]["id"],"sith")

    def test_unused_definition_can_be_removed_and_new_card_added(self):
        original = json.loads(self.catalog_path.read_text())
        original["cards"]=[card for card in original["cards"] if card["id"]!="future"]
        original["cards"].append({**original["cards"][0],"id":"new-card"})
        self.catalog_path.write_text(json.dumps(original))
        restarted = core.Game(self.db_path,self.catalog_path)
        self.assertNotIn("future",restarted.catalog)
        created = restarted.create_drop("guild","new","new-card",now=self.now)
        self.assertEqual(created["card_id"],"new-card")

    def test_zero_weight_retirement_preserves_owned_copy_and_favorite(self):
        item = self.capture("a")
        self.game.set_favorite("guild","a",item["id"],True)
        original = json.loads(self.catalog_path.read_text())
        original["cards"][0].update(weight=0,name="Updated Alpha Name")
        self.catalog_path.write_text(json.dumps(original))
        restarted = core.Game(self.db_path,self.catalog_path)
        owned = restarted.info("guild","a",item["id"])
        self.assertEqual(owned["name"],"Updated Alpha Name")
        self.assertTrue(owned["favorite"])
        self.assertNotIn("alpha",{card["id"] for card in restarted.spawn_pool(now=self.now)})
        self.assert_error("unavailable_card",restarted.create_drop,"guild","retired","alpha",now=self.now)

    def test_shared_power_scale_and_rarity_bands_for_every_role_and_profile(self):
        for combat in range(1, 6):
            bands, previous = {}, {}
            for rarity in core.RARITIES:
                scores = []
                for role in core.ROLES:
                    ground = core.balance_stats("ground-fixture", rarity, role, "ground", combat)
                    fleet = core.balance_stats("fleet-fixture", rarity, role, "fleet", combat)
                    self.assertEqual(ground, fleet, (rarity, role, combat))
                    self.assertEqual(ground, core.balance_stats("renamed-fixture", rarity, role, "ground", combat))
                    if role in previous:
                        self.assertGreater(ground["attack"], previous[role]["attack"])
                        self.assertGreater(ground["health"], previous[role]["health"])
                        self.assertEqual(ground["speed"], previous[role]["speed"])
                    previous[role] = ground
                    scores.append(ground["attack"]*ground["health"])
                # Roles spend a common budget on damage versus durability.
                self.assertLess(max(scores)/min(scores), 1.012)
                bands[rarity] = (min(scores), max(scores))
            for lower, higher in zip(core.RARITIES, core.RARITIES[1:]):
                self.assertLess(bands[lower][1], bands[higher][0])

    def test_combat_ratings_increase_both_stats_and_preserve_role_tradeoffs(self):
        for rarity in core.RARITIES:
            for role in core.ROLES:
                previous = None
                for rating in range(1, 6):
                    stats = core.balance_stats("fixture", rarity, role, "ground", rating)
                    if previous:
                        self.assertGreater(stats["attack"], previous["attack"])
                        self.assertGreater(stats["health"], previous["health"])
                        self.assertEqual(stats["speed"], previous["speed"])
                    previous = stats
            roles = {role: core.balance_stats("fixture", rarity, role, "ground", 5) for role in core.ROLES}
            self.assertGreater(roles["striker"]["attack"], max(roles[role]["attack"] for role in core.ROLES if role != "striker"))
            self.assertGreater(roles["guardian"]["health"], max(roles[role]["health"] for role in core.ROLES if role != "guardian"))
            self.assertGreater(roles["tactician"]["speed"], max(roles[role]["speed"] for role in core.ROLES if role != "tactician"))
        # Pin outcomes independently of implementation constants and rounding.
        self.assertEqual(core.balance_stats("generic", "common", "striker", "fleet", 2),
                         {"attack": 168, "health": 672, "speed": 70})
        self.assertEqual(core.balance_stats("hero", "legendary", "striker", "ground", 5),
                         {"attack": 1613, "health": 6451, "speed": 70})

    def test_combat_mastery_can_outweigh_one_rarity_tier(self):
        # Rarity still matters, but a stronger fighter can exceed the next
        # collector tier. Compare each role so its budget split cannot hide it.
        for role in core.ROLES:
            stronger = core.balance_stats("trained", "legendary", role, "ground", 5)
            rarer = core.balance_stats("rarer", "mythic", role, "ground", 4)
            with self.subTest(role=role):
                self.assertGreater(stronger["attack"], rarer["attack"])
                self.assertGreater(stronger["health"], rarer["health"])

    def test_balance_ratings_reject_noninteger_boolean_and_out_of_range_values(self):
        for value in (None, True, False, 0, 6, -1, 2.0, "2", float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                core.balance_stats("fixture", "common", "striker", "ground", value)

    def test_story_importance_cannot_be_passed_into_combat_formula_or_profile(self):
        with self.assertRaises(TypeError):
            core.balance_stats("fixture", "common", "striker", "ground", 2, 5)
        with self.assertRaises(TypeError):
            core.balance_stats("fixture", "common", "striker", "ground", importance_rating=5)
        card = dict(self.game.catalog["alpha"], power_profile=dict(DEFAULT_POWER_PROFILE))
        card.update(core.balance_stats(card["id"], card["rarity"], card["role"], card["arena"]))
        document = {"schema_version": 1, "balance": {"schema_version": 3, "formula": "combat-power-v3"}, "cards": [card]}
        for importance in (1, 5):
            document["cards"][0]["power_profile"]["importance"] = importance
            self.catalog_path.write_text(json.dumps(document))
            with self.subTest(importance=importance), self.assertRaisesRegex(ValueError, "power profile"):
                core.Game(self.directory/f"stale-importance-{importance}.sqlite", self.catalog_path)

    def test_historical_v3_limited_supply_and_story_copy_do_not_add_combat_power(self):
        base = dict(self.game.catalog["alpha"], power_profile=dict(DEFAULT_POWER_PROFILE))
        base.update(core.balance_stats(base["id"], base["rarity"], base["role"], base["arena"]))
        edition = dict(base, id="limited-fixture", category="limited",
                       description="The most pivotal hero in the saga.",
                       release=self.game.catalog["limited"]["release"])
        document = {"schema_version": 1, "balance": {"schema_version": 3, "formula": "combat-power-v3"},
                    "cards": [base, edition]}
        for cap in (1, 10_000):
            edition["release"] = {**edition["release"], "supply_cap": cap}
            self.catalog_path.write_text(json.dumps(document))
            game = core.Game(self.directory/f"edition-{cap}.sqlite", self.catalog_path)
            with self.subTest(cap=cap):
                self.assertEqual(tuple(game.catalog[base["id"]][key] for key in ("attack", "health", "speed")),
                                 tuple(game.catalog[edition["id"]][key] for key in ("attack", "health", "speed")))

    def limited_balance_document(self, version=4):
        base = dict(self.game.catalog["alpha"], subject_id="alpha",
                    power_profile=dict(DEFAULT_POWER_PROFILE), ability="focused_strike",
                    ability_name="Focused Strike", ability_description="Synthetic striker ability.")
        base.update(core.balance_stats(base["id"],base["rarity"],base["role"],base["arena"],2))
        edition = copy.deepcopy(base)
        edition.update(id="limited",name="Limited",aliases=["limited"],category="limited",
                       variant_of="alpha",release=self.game.catalog["limited"]["release"])
        edition.update(core.balance_stats("limited",edition["rarity"],edition["role"],edition["arena"],2,
                                          limited_edition=version==4))
        balance = {"schema_version":version,"formula":f"combat-power-v{version}"}
        if version==4:
            balance["limited_edition_bonus_percent"] = 10
        return {"schema_version":1,"balance":balance,"cards":[base,edition]}

    def test_limited_bonus_rounds_after_base_and_uses_half_up_without_speed_bonus(self):
        # 157.5 first rounds to 158; its bonus then rounds 173.8 to 174.
        # A single combined rounding would incorrectly produce 173.
        self.assertEqual(core.balance_stats("edition","uncommon","guardian","ground",2,
                                            limited_edition=True),
                         {"attack":174,"health":1232,"speed":45})
        # 435 * 1.10 = 478.5 must round to 479, not banker's rounding to 478.
        self.assertEqual(core.balance_stats("edition","uncommon","striker","fleet",4,
                                            limited_edition=True),
                         {"attack":479,"health":1914,"speed":70})
        for rarity in core.RARITIES:
            for role in core.ROLES:
                for combat in range(1,6):
                    base = core.balance_stats("base",rarity,role,"ground",combat)
                    limited = core.balance_stats("edition",rarity,role,"ground",combat,limited_edition=True)
                    self.assertEqual(base,core.balance_stats("base",rarity,role,"fleet",combat,limited_edition=False))
                    self.assertEqual(limited["speed"],base["speed"])
                    self.assertGreater(limited["attack"],base["attack"])
                    self.assertGreater(limited["health"],base["health"])
        for value in (None,0,1,10,"true",[],{}):
            with self.subTest(flag=value),self.assertRaisesRegex(ValueError,"boolean"):
                core.balance_stats("bad","common","striker","ground",limited_edition=value)
        with self.assertRaises(TypeError):
            core.balance_stats("bad","common","striker","ground",2,True)

    def test_v4_limited_bonus_is_category_driven_and_v3_stays_unchanged(self):
        for version in (3,4):
            document = self.limited_balance_document(version)
            # A release window or event status alone must not grant the bonus.
            released = dict(document["cards"][0],id="event",category="legends",event_id="event",
                            release=document["cards"][1]["release"])
            document["cards"].append(released)
            document["cards"].reverse()  # The base need not appear first.
            self.catalog_path.write_text(json.dumps(document))
            game = core.Game(self.directory/f"version-{version}.sqlite",self.catalog_path)
            self.assertEqual((game.catalog["alpha"]["attack"],game.catalog["alpha"]["health"]),(168,672))
            self.assertEqual((game.catalog["event"]["attack"],game.catalog["event"]["health"]),(168,672))
            self.assertEqual((game.catalog["limited"]["attack"],game.catalog["limited"]["health"]),
                             (185,739) if version==4 else (168,672))
        document = self.limited_balance_document(4)
        document["cards"][1].update(attack=168,health=672)
        self.catalog_path.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError,"stats do not match combat-power-v4"):
            core.Game(self.directory/"unboosted-v4.sqlite",self.catalog_path)
        document = self.limited_balance_document(3)
        document["cards"][1].update(attack=185,health=739)
        self.catalog_path.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError,"stats do not match combat-power-v3"):
            core.Game(self.directory/"boosted-v3.sqlite",self.catalog_path)

    def test_balance_v4_metadata_requires_exact_bonus_and_rejects_schema_bools(self):
        prototype = self.limited_balance_document()
        invalid = [{"schema_version":4,"formula":"combat-power-v4"}]
        invalid += [{**prototype["balance"],"limited_edition_bonus_percent":v}
                    for v in (None,True,False,0,9,11,10.0,"10",[],{})]
        invalid += [{**prototype["balance"],"schema_version":v} for v in (True,False,4.0,"4")]
        invalid += [{**prototype["balance"],"formula":"combat-power-v3"},
                    {"schema_version":3,"formula":"combat-power-v4"},[],True,{}]
        for index,balance in enumerate(invalid):
            document = copy.deepcopy(prototype); document["balance"] = balance
            self.catalog_path.write_text(json.dumps(document))
            with self.subTest(balance=balance),self.assertRaises(ValueError):
                core.Game(self.directory/f"bad-v4-policy-{index}.sqlite",self.catalog_path)
        for value in (True,1.0,"1"):
            document = copy.deepcopy(prototype);document["schema_version"] = value
            self.catalog_path.write_text(json.dumps(document))
            with self.subTest(catalog_schema=value),self.assertRaisesRegex(ValueError,"catalogue schema"):
                core.Game(self.directory/"bad-document-schema.sqlite",self.catalog_path)
        for bonus in (0,10,True,0.0,None):
            document = self.limited_balance_document(3)
            document["balance"]["limited_edition_bonus_percent"] = bonus
            self.catalog_path.write_text(json.dumps(document))
            with self.subTest(v3_bonus=bonus):
                if type(bonus) is int and bonus==0:
                    core.Game(self.directory/"explicit-zero-v3.sqlite",self.catalog_path)
                else:
                    with self.assertRaisesRegex(ValueError,"bonus policy"):
                        core.Game(self.directory/"conflicting-v3-policy.sqlite",self.catalog_path)

    def test_v4_limited_requires_direct_comparable_base_not_missing_chain_or_cycle(self):
        prototype = self.limited_balance_document()
        cases = []
        for value in (None,"","missing",True,[],"limited"):
            document = copy.deepcopy(prototype);document["cards"][1]["variant_of"] = value
            cases.append(document)
        for field,value in (("subject_id",None),("subject_id",""),("subject_id","another"),("arena","fleet")):
            document = copy.deepcopy(prototype);document["cards"][1][field] = value
            cases.append(document)
        document = copy.deepcopy(prototype);document["cards"][0].pop("subject_id")
        cases.append(document)
        for reference in ("limited","another-base"):
            document = copy.deepcopy(prototype)
            document["cards"][0]["variant_of"] = reference
            if reference=="another-base":
                document["cards"].append({**copy.deepcopy(prototype["cards"][0]),"id":reference})
            cases.append(document)  # Even a nonlimited intermediary cannot form a chain/cycle.
        document = copy.deepcopy(prototype)
        base,edition = document["cards"]
        base.update(category="limited",release=edition["release"],variant_of="limited")
        base.update(core.balance_stats(base["id"],base["rarity"],base["role"],base["arena"],2,limited_edition=True))
        cases.append(document)  # Two-node cycle.
        chain = copy.deepcopy(document)
        original = copy.deepcopy(prototype["cards"][0]);original["id"] = "original"
        chain["cards"][0]["variant_of"] = "original";chain["cards"].append(original)
        cases.append(chain)  # A -> limited B -> nonlimited C is still forbidden.
        for index,document in enumerate(cases):
            self.catalog_path.write_text(json.dumps(document))
            with self.subTest(case=index),self.assertRaisesRegex(ValueError,"Limited edition"):
                core.Game(self.directory/f"bad-edition-reference-{index}.sqlite",self.catalog_path)

    def test_v4_comparable_limited_must_inherit_rarity_combat_role_and_ability_bundle(self):
        prototype = self.limited_balance_document()
        for field,value in (("rarity","rare"),("role","support"),("combat",5),
                            ("ability","field_repair"),("ability_name","Different"),
                            ("ability_description","Different description."),("ability",None)):
            document = copy.deepcopy(prototype);edition = document["cards"][1]
            if field=="combat":
                edition["power_profile"]["combat"] = value
            else:
                edition[field] = value
            edition.update(core.balance_stats(edition["id"],edition["rarity"],edition["role"],edition["arena"],
                                              edition["power_profile"]["combat"],limited_edition=True))
            self.catalog_path.write_text(json.dumps(document))
            with self.subTest(field=field,value=value),self.assertRaisesRegex(ValueError,"[Ll]imited edition"):
                core.Game(self.directory/f"bad-inheritance-{field}.sqlite",self.catalog_path)

    def test_v4_depiction_override_bonuses_its_own_phase_not_mature_base(self):
        document = self.limited_balance_document()
        base,edition = document["cards"]
        base.update(rarity="legendary")
        base["power_profile"]["combat"] = 5
        base.update(core.balance_stats("alpha","legendary","striker","ground",5))
        edition.update(role="support",ability="field_repair",ability_name="Field Repair",
                       ability_description="Synthetic support ability.")
        edition["power_profile"].update(basis="depiction_override",combat=1,rationale="A distinct earlier civilian phase.")
        edition.update(core.balance_stats("limited","common","support","ground",1,limited_edition=True))
        self.catalog_path.write_text(json.dumps(document))
        game = core.Game(self.directory/"override-v4.sqlite",self.catalog_path)
        self.assertEqual((game.catalog["limited"]["attack"],game.catalog["limited"]["health"],game.catalog["limited"]["speed"]),(106,660,55))
        self.assertEqual((game.catalog["alpha"]["attack"],game.catalog["alpha"]["health"]),(1613,6451))
        edition["power_profile"]["importance"] = 5
        self.catalog_path.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError,"power profile"):
            core.Game(self.directory/"v4-story-driver.sqlite",self.catalog_path)

    def test_v4_rebalance_updates_owned_decks_new_duels_and_raids_preserving_prior_snapshots(self):
        document = self.limited_balance_document(3)
        self.catalog_path.write_text(json.dumps(document))
        self.game = core.Game(self.db_path,self.catalog_path)
        edition = self.capture("owner","limited");base = self.capture("opponent","alpha")
        self.game.set_favorite("guild","owner",edition["id"],True)
        for user,item in (("owner",edition),("opponent",base)):
            self.game.set_deck("guild",user,"ground",[item["id"]],now=self.now)
        old_profile = self.game.profile("guild","owner")
        old_duel = self.game.duel_create("guild","owner","opponent","ground",now=self.now)
        old_raid = self.game.raid_create("guild","before",now=self.now,signup=10,duration=100)
        joined = self.game.raid_join(old_raid["id"],"owner",now=self.now)
        self.assertEqual(joined["participants"][0]["per_attack"],185)
        document["balance"] = {"schema_version":4,"formula":"combat-power-v4","limited_edition_bonus_percent":10}
        document["cards"][1].update(attack=185,health=739)
        self.catalog_path.write_text(json.dumps(document))
        self.game = core.Game(self.db_path,self.catalog_path)
        owned = self.game.info("guild","owner",edition["id"])
        for field in ("id","guild_id","user_id","card_id","acquired_at","origin"):
            self.assertEqual(owned[field],edition[field])
        self.assertTrue(owned["favorite"])
        self.assertEqual((owned["attack"],owned["health"]),(185,739))
        deck = self.game.deck("guild","owner","ground")
        self.assertEqual((deck["attack"],deck["health"]),(185,739))
        for field in ("credits","xp","total_cards"):
            self.assertEqual(self.game.profile("guild","owner")[field],old_profile[field])
        self.assert_error("stale_duel",self.game.duel_accept,old_duel["id"],"opponent",now=self.now)
        duel = self.game.duel_create("guild","owner","opponent","ground",now=self.now)
        result = self.game.duel_accept(duel["id"],"opponent",now=self.now)["result"]
        self.assertEqual(result["winner_id"],"owner")
        self.assertEqual(result["decks"][0]["cards"][0]["attack"],185)
        active_old = self.game.get_raid(old_raid["id"],now=self.now+10)
        self.assertEqual(active_old["participants"][0]["per_attack"],185)
        self.assertEqual(active_old["participants"][0]["deck"]["cards"][0]["attack"],168)
        new_raid = self.game.raid_create("guild","after",now=self.now+11,signup=10,duration=100)
        new_join = self.game.raid_join(new_raid["id"],"owner",now=self.now+11)
        self.assertEqual(new_join["participants"][0]["per_attack"],204)
        self.assertEqual(new_join["participants"][0]["deck"]["cards"][0]["attack"],185)

    def test_owned_instances_rebalance_without_resetting_ownership_or_saved_deck(self):
        item = self.capture("owner")
        self.game.set_favorite("guild", "owner", item["id"], True)
        self.game.set_deck("guild", "owner", "ground", [item["id"]], now=self.now)
        before = self.game.profile("guild", "owner")
        document = json.loads(self.catalog_path.read_text())
        document["balance"] = {"schema_version": 3, "formula": "combat-power-v3"}
        for card in document["cards"]:
            card["power_profile"] = dict(DEFAULT_POWER_PROFILE)
            if card["id"] == item["card_id"]:
                card["power_profile"].update(combat=5)
            profile = card["power_profile"]
            card.update(core.balance_stats(card["id"], card["rarity"], card["role"], card["arena"], profile["combat"]))
        self.catalog_path.write_text(json.dumps(document))
        restarted = core.Game(self.db_path, self.catalog_path)
        owned = restarted.info("guild", "owner", item["id"])
        for field in ("id", "guild_id", "user_id", "card_id", "acquired_at", "origin"):
            self.assertEqual(owned[field], item[field])
        self.assertTrue(owned["favorite"])
        self.assertEqual((owned["attack"], owned["health"], owned["speed"]), (504, 2016, 70))
        saved = restarted.deck("guild", "owner", "ground")
        self.assertEqual((saved["attack"], saved["health"]), (504, 2016))
        after = restarted.profile("guild", "owner")
        self.assertEqual((after["total_cards"], after["credits"], after["xp"]),
                         (before["total_cards"], before["credits"], before["xp"]))

    def test_balance_metadata_rejects_unpublished_stats_and_multiplier(self):
        card = dict(self.game.catalog["alpha"])
        card["power_profile"] = dict(DEFAULT_POWER_PROFILE)
        card.update(core.balance_stats(card["id"],card["rarity"],card["role"],card["arena"]))
        document = {"schema_version":1,"balance":{"schema_version":3,"formula":"combat-power-v3"},"rarities":{"common":{"power_multiplier":1.0}},"cards":[card]}
        self.catalog_path.write_text(json.dumps(document))
        core.Game(self.directory/"valid-balance.sqlite",self.catalog_path)
        document["cards"][0]["attack"] += 1
        self.catalog_path.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError,"stats do not match"):
            core.Game(self.directory/"bad-stats.sqlite",self.catalog_path)
        document["rarities"]["common"]["power_multiplier"] = 1.5
        self.catalog_path.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError,"multiplier"):
            core.Game(self.directory/"bad-multiplier.sqlite",self.catalog_path)

    def test_published_combat_table_must_match_the_executable_formula(self):
        card = dict(self.game.catalog["alpha"], power_profile=dict(DEFAULT_POWER_PROFILE))
        card.update(core.balance_stats(card["id"], card["rarity"], card["role"], card["arena"]))
        points = {"1": 100, "2": 140, "3": 200, "4": 290, "5": 420}
        document = {"schema_version": 1, "cards": [card], "balance": {
            "schema_version": 3, "formula": "combat-power-v3", "combat_power_points": points}}
        self.catalog_path.write_text(json.dumps(document))
        core.Game(self.directory/"valid-points.sqlite", self.catalog_path)
        for index, invalid in enumerate((None, [], {}, {**points, "5": 200},
                                         {**points, "1": 100.0}, {**points, "6": 600})):
            document["balance"]["combat_power_points"] = invalid
            self.catalog_path.write_text(json.dumps(document))
            with self.subTest(points=invalid), self.assertRaisesRegex(ValueError, "Combat points"):
                core.Game(self.directory/f"invalid-points-{index}.sqlite", self.catalog_path)

    def test_versioned_balance_requires_valid_explained_power_profiles(self):
        card = dict(self.game.catalog["alpha"], power_profile=dict(DEFAULT_POWER_PROFILE))
        card.update(core.balance_stats(card["id"], card["rarity"], card["role"], card["arena"]))
        prototype = {"schema_version": 1, "balance": {"schema_version": 3, "formula": "combat-power-v3"}, "cards": [card]}
        invalid = (None, {}, {"combat": True}, {"combat": 0}, {"combat": 6},
                   {"combat": 2.0}, {"sources": "https://example.test"}, {"sources": [1]},
                   {"sources": [" "]}, {"unexpected": "metadata"}, {"basis": ""}, {"rationale": ""},
                   {"basis": 1}, {"basis": []}, {"basis": "unpublished_basis"},
                   {"rationale": None}, {"rationale": "   "})
        for index, updates in enumerate(invalid):
            document = json.loads(json.dumps(prototype))
            profile = None if updates is None else {} if not updates else {**DEFAULT_POWER_PROFILE, **updates}
            document["cards"][0]["power_profile"] = profile
            self.catalog_path.write_text(json.dumps(document))
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                core.Game(self.directory/f"bad-profile-{index}.sqlite", self.catalog_path)
        for formula in ({"schema_version": 1, "formula": "rarity-role-v1"},
                        {"schema_version": 2, "formula": "design-power-v2"},
                        {"schema_version": 3, "formula": "design-power-v2"},
                        {"schema_version": 2, "formula": "combat-power-v3"},
                        {"schema_version": 3.0, "formula": "combat-power-v3"}):
            document = json.loads(json.dumps(prototype))
            document["balance"] = formula
            self.catalog_path.write_text(json.dumps(document))
            with self.subTest(formula=formula), self.assertRaisesRegex(ValueError, "balance formula"):
                core.Game(self.directory/"unsupported-formula.sqlite", self.catalog_path)

    def test_800_subject_catalog_inventory_latency_and_event_denominator(self):
        # Synthetic entries are a performance fixture only, never user content.
        prototype = self.game.catalog["alpha"]
        cards = []
        for index in range(800):
            card = dict(prototype)
            identity = f"fixture-entity-{index:04d}"
            card.update(id=identity,subject_id=identity,name=f"Fixture Entity {index}",aliases=[f"fixture {index}"],
                        rarity=core.RARITIES[(index//4)%6],role=core.ROLES[index%4],arena="fleet" if index%7==0 else "ground")
            card["power_profile"] = dict(DEFAULT_POWER_PROFILE)
            card.update(core.balance_stats(identity,card["rarity"],card["role"],card["arena"]))
            cards.append(card)
        for identity in ["limited","future","legends"]:
            card = dict(self.game.catalog[identity])
            card["subject_id"] = cards[0]["subject_id"]
            card["power_profile"] = dict(DEFAULT_POWER_PROFILE)
            card.update(core.balance_stats(identity,card["rarity"],card["role"],card["arena"]))
            cards.append(card)
        document = {"schema_version":1,"balance":{"schema_version":3,"formula":"combat-power-v3"},"rarities":{rarity:{"power_multiplier":value} for rarity,value in core.RARITY_POWER.items()},"cards":cards}
        self.catalog_path.write_text(json.dumps(document))
        engine = core.Game(self.directory/"large.sqlite",self.catalog_path)
        with engine._tx() as connection:
            for card in cards[:800]:
                engine._mint(connection,"guild","owner",card["id"],self.now,"test fixture")
        started = time.monotonic()
        profile = engine.profile("guild","owner")
        rows = engine.inventory("guild","owner",limit=100,offset=700)
        health = engine.health()
        pool = engine.spawn_pool(now=self.now)
        elapsed = time.monotonic()-started
        self.assertEqual((profile["catalog_count"],profile["unique_cards"],profile["completion_percent"]),(800,800,100))
        self.assertEqual((profile["regular_subject_count"],profile["regular_collected_subject_count"],profile["regular_subject_completion_percent"]),(800,800,100))
        self.assertEqual((profile["event_catalog_count"],profile["all_catalog_count"]),(3,803))
        self.assertEqual(len(rows),100)
        self.assertEqual(health["regular_subject_count"],800)
        self.assertTrue(health["regular_subject_metadata_complete"])
        self.assertNotIn("legends",{row["id"] for row in pool})
        self.assertLess(elapsed,5,"800-card read/pool cycle must remain bounded")
        # A second treatment of an existing subject adds a definition, never
        # another distinct subject toward the launch minimum.
        variant = dict(cards[0])
        variant.update(id="fixture-humor-variant",name="Fixture alternate treatment")
        variant.update(core.balance_stats(variant["id"],variant["rarity"],variant["role"],variant["arena"]))
        document["cards"].append(variant)
        self.catalog_path.write_text(json.dumps(document))
        expanded = core.Game(self.directory/"expanded.sqlite",self.catalog_path)
        self.assertEqual(expanded.health()["regular_catalog_cards"],801)
        self.assertEqual(expanded.health()["regular_subject_count"],800)
        with expanded._tx() as connection:
            for identity in [cards[0]["id"],variant["id"],cards[1]["id"]]:
                expanded._mint(connection,"guild","variant-owner",identity,self.now,"test fixture")
        progress = expanded.profile("guild","variant-owner")
        self.assertEqual((progress["regular_unique_cards"],progress["regular_collected_subject_count"]),(3,2))
        self.assertEqual(progress["regular_subject_count"],800)
        self.assertEqual(progress["regular_subject_completion_percent"],0.2)
        # An event treatment of an uncollected subject must not advance the
        # ordinary subject collection, even though its identity is known.
        with expanded._tx() as connection:
            expanded._mint(connection,"guild","event-owner","limited",self.now,"test fixture")
        event_progress = expanded.profile("guild","event-owner")
        self.assertEqual(event_progress["event_unique_cards"],1)
        self.assertEqual(event_progress["regular_collected_subject_count"],0)


if __name__ == "__main__":
    unittest.main()
