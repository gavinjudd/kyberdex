"""Transactional game rules. No Discord, network access, or account secrets.

Every operation opens its own SQLite connection. BEGIN IMMEDIATE serializes
ownership/economy transitions across threads and processes; constraints provide
an additional per-drop collector-capacity boundary. Times are UTC epoch seconds.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import itertools
import json
import math
import random
import re
import secrets
import sqlite3
import time
import unicodedata
from collections import Counter
from pathlib import Path

RARITIES = ("common", "uncommon", "rare", "epic", "legendary", "mythic")
VALUES = dict(zip(RARITIES, (20, 40, 80, 160, 320, 640)))
ARENAS = ("ground", "fleet")
ROLES = ("striker", "guardian", "support", "tactician")
NEUTRAL_FACTIONS = frozenset(("Independent", "Wildlife"))
RAID_TIERS = {
    "standard": {"weight": 40, "effort": 0.65, "counter": 0.45, "credits": 200, "xp": 20},
    "veteran": {"weight": 35, "effort": 0.75, "counter": 0.65, "credits": 400, "xp": 30},
    "elite": {"weight": 20, "effort": 0.85, "counter": 0.85, "credits": 700, "xp": 45},
    "apex": {"weight": 5, "effort": 0.90, "counter": 1.0, "credits": 1000, "xp": 60},
}
RAID_MOVES = {
    "onslaught": "Onslaught · every second retaliation hits 20% harder.",
    "choke": "Force Choke · surviving squads deal 10% less damage on subsequent attacks.",
    "lightning": "Force Lightning · retaliation hits 15% harder.",
    "ion": "Ion Barrage · attacks have a 15-second cooldown.",
    "superlaser": "Superlaser · the second retaliation deals double damage.",
    "counterstrike": "Counterstrike · retaliates against every surviving attack.",
}
MARKET_PRICES = {"common":250,"uncommon":600,"rare":1500,"epic":4000}
MARKET_SLOTS = (("common",4),("common",4),("uncommon",4),("rare",2))
MARKET_EPIC_CHANCE = 0.25
MAX_COLLECTORS = 6
COLLECTOR_LIMITS = dict(zip(RARITIES, (2, 2, 2, 2, 1, 1)))
RARITY_SPAWN_WEIGHTS = dict(zip(RARITIES, (5400, 2200, 1780, 420, 160, 40)))
SPAWN_WEIGHTS_V3_REVISION_1 = dict(zip(RARITIES, (5150, 2500, 1800, 400, 110, 40)))
SPAWN_WEIGHTS_V3_REVISION_2 = dict(zip(RARITIES, (5090, 2500, 1800, 420, 150, 40)))
LEGACY_SPAWN_WEIGHTS = dict(zip(RARITIES, (5800, 2500, 1000, 500, 150, 50)))
LIMITED_SHARE_PERCENT = 25
WRONG_COOLDOWN = 3
CRAFT_RECIPES = {
    "uncommon": {"ingredients": 3, "cost": 150},
    "rare": {"ingredients": 3, "cost": 150},
    "epic": {"ingredients": 3, "cost": 300},
    "legendary": {"ingredients": 5, "cost": 750},
    "mythic": {"ingredients": 7, "cost": 2000},
}
RARITY_POWER = dict(zip(RARITIES,(1.0,1.25,1.65,2.3,3.2,4.5)))
COMBAT_POWER_POINTS = {1:100,2:140,3:200,4:290,5:420}
ROLE_BASELINES = {"striker":(120,480,70),"guardian":(90,640,45),"support":(96,600,55),"tactician":(108,533,80)}
POWER_PROFILE_BASES = frozenset(("default_design", "curated_design", "depiction_override"))
POWER_PROFILE_FIELDS = frozenset(("combat", "basis", "rationale", "sources"))
LIMITED_EDITION_BONUS_PERCENT = 10
DELIVERY_TIMEOUT = 120


def _is_event_exclusive(card):
    return (card.get("category") == "legends" or bool(card.get("event_only"))
            or card.get("availability") == "event_only" or bool(card.get("event_id")))


def collector_limit(card):
    """Published default capacity; explicit 1–6 overrides belong to events."""
    rarity = card.get("rarity")
    if not isinstance(rarity, str) or rarity not in COLLECTOR_LIMITS:
        raise ValueError("Unknown rarity in collector policy")
    limit = COLLECTOR_LIMITS[rarity]
    if "collector_limit" in card:
        override = card["collector_limit"]
        if type(override) is not int or not 1 <= override <= MAX_COLLECTORS:
            raise ValueError("Collector override must be an integer from 1 through 6")
        if not _is_event_exclusive(card):
            raise ValueError("Collector overrides require an event-exclusive definition")
        return override
    return limit


def balance_stats(card_id,rarity,role,arena,combat_rating=2,*,limited_edition=False):
    """Shared combat scale, with the schema-4 limited bonus explicitly enabled.

    Card IDs, story prominence and physical size do not affect power. Combat
    ratings are game-design bands, not measurements of canonical strength.
    The default preserves schema-3 results. Limited attack and health receive
    ten percent after the base rounding; initiative is unchanged.
    """
    if type(limited_edition) is not bool:
        raise ValueError("Limited edition flag must be a boolean")
    if rarity not in RARITY_POWER or role not in ROLE_BASELINES or arena not in ARENAS:
        raise ValueError("Unknown rarity, role or arena in balance formula")
    base_attack,base_health,speed = ROLE_BASELINES[role]
    if type(combat_rating) is not int or combat_rating not in COMBAT_POWER_POINTS:
        raise ValueError("Combat rating must be an integer from 1 through 5")
    rarity_points = round(RARITY_POWER[rarity]*100)
    profile_points = COMBAT_POWER_POINTS[combat_rating]
    def stat(base):
        value = (base*rarity_points*profile_points + 5000)//10000
        return (value*(100+LIMITED_EDITION_BONUS_PERCENT)+50)//100 if limited_edition else value
    return {"attack": stat(base_attack), "health": stat(base_health), "speed": speed}


class GameError(Exception):
    def __init__(self, code, message, *, details=None, persist=False):
        super().__init__(message)
        self.code, self.message = code, message
        self.details, self.persist = details or {}, persist


def normalize_alias(value):
    value = unicodedata.normalize("NFKD", str(value)).casefold()
    return "".join(char for char in value if char.isalnum())


NAME_TITLES = frozenset((
    "admiral", "captain", "commander", "general", "lieutenant", "colonel", "major", "sergeant",
    "marshal", "grand", "vice", "supreme", "chancellor", "senator", "queen", "king", "princess",
    "prince", "lord", "lady", "darth", "jedi", "sith", "master", "knight", "padawan", "count",
    "moff", "director", "doctor", "dr", "professor", "clone", "trooper", "the", "agent", "boss",
    "chief", "commandant", "duchess", "governor", "inspector", "keeper", "leader", "magistrate",
    "mother", "nightsister", "old", "taskmaster", "vice-chair", "arc", "pilot",
))
GENERIC_NAME_PARTS = NAME_TITLES | frozenset((
    "of", "and", "a", "an", "class", "star", "destroyer", "starfighter", "fighter", "interceptor",
    "bomber", "cruiser", "corvette", "frigate", "shuttle", "transport", "gunship", "droid", "battle",
    "assault", "heavy", "light", "republic", "imperial", "rebel", "resistance", "alliance", "guard",
    "elite", "scout", "pilot", "officer", "soldier", "security", "warrior", "hunter", "shadow",
    "meme", "power", "greeting", "hello", "limited", "edition", "outpost", "battle", "siege",
    "one", "two", "three", "prototype", "modified", "unit", "series", "ship", "capital", "character", "ct", "cc", "rc",
))
TITLE_PREFIXES = tuple(sorted((normalize_alias(title) for title in NAME_TITLES), key=lambda title:(-len(title),title)))
# These documented equipment subtypes share their base model's ordinary name.
# This is capture taxonomy only; collectible subject IDs and completion stay separate.
CAPTURE_MODEL_FAMILIES = {
    "b1-series-rocket-battle-droid": "b1-battle-droid",
    "b2-rp-battle-droid": "b2-super-battle-droid",
    "super-battle-droid-rocket-trooper": "b2-super-battle-droid",
}
GENERIC_CAPTURE_ANSWERS = frozenset(("battledroid",))
GENERIC_MODEL_CODES = frozenset(normalize_alias(word) for word in GENERIC_NAME_PARTS)


def capture_identity(card):
    """The deliberate model family used for answer ambiguity, never ownership."""
    subject = card.get("subject_id", card["id"])
    return CAPTURE_MODEL_FAMILIES.get(subject, subject)


def name_words(value):
    text = unicodedata.normalize("NFKD", str(value)).casefold()
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.findall(r"[^\W_]+(?:[-'’][^\W_]+)*", text)


def untitled_name(value, *, compact=True):
    words = name_words(value)
    while words and words[0] in NAME_TITLES:
        words.pop(0)
    answer = normalize_alias(" ".join(words))
    if not compact:
        return answer
    # Honor the same rule when spaces/hyphens are omitted: CommanderJaves.
    # Longest prefix first prevents 'grand' splitting 'grandadmiral' incorrectly.
    while answer:
        prefix = next((title for title in TITLE_PREFIXES if answer.startswith(title) and len(answer)>len(title)),None)
        if prefix is None:
            break
        answer = answer[len(prefix):]
    return answer


def identity_forms(value, *, character=False):
    """Deterministic full-name and title/name combinations, never fuzzy substrings."""
    original = normalize_alias(value)
    value = re.split(r"\s+[—–]\s+", value, maxsplit=1)[0]
    # A quoted callsign does not have to be included in a formal name.
    plain = re.sub(r'["“][^"”]+["”]', " ", value)
    forms = {original, normalize_alias(value), normalize_alias(plain), untitled_name(value,compact=False), untitled_name(plain,compact=False)}
    if character:
        words = name_words(plain)
        titles = []
        while words and words[0] in NAME_TITLES:
            titles.append(words.pop(0))
        personal = [word for word in words if word not in GENERIC_NAME_PARTS]
        if len(personal) >= 2:
            forms.add(normalize_alias(personal[0] + " " + personal[-1]))
            forms.add(normalize_alias(personal[-1] + " " + personal[0]))
        # Title chains and their suffixes: Grand Admiral / Admiral + first / last.
        for index in range(len(titles)):
            title = " ".join(titles[index:])
            for part in personal[:1] + personal[-1:]:
                forms.add(normalize_alias(title + " " + part))
    return forms - {""}


def logical_identity_forms(value):
    """Omit class/series connectors and accept complete, unambiguous model codes."""
    value = re.split(r"\s+[—–]\s+", value, maxsplit=1)[0]
    forms = set()
    for identity in (value, re.sub(r'["“][^"”]+["”]', " ", value)):
        stripped = re.sub(r"(?<=\w)[-\s]+(?:series|class)\b", "", identity, flags=re.IGNORECASE)
        if stripped != identity:
            forms.add(normalize_alias(stripped))
        for word in name_words(identity):
            model = re.sub(r"-(?:series|class)$", "", word)
            coded = (any(char.isdigit() for char in model) and any(char.isalpha() for char in model))
            connector = model != word
            if (coded or connector) and normalize_alias(model) not in GENERIC_MODEL_CODES:
                forms.add(normalize_alias(model))
        if "droid" in identity.casefold():
            # Alphabetic model series such as KX and ASP retain their whole code.
            forms.update(normalize_alias(word) for word in re.findall(r"(?<![\w-])[A-Z]{2,5}(?![\w-])", identity)
                         if normalize_alias(word) not in GENERIC_MODEL_CODES)
    return forms - {""}


def capture_names(catalog):
    """Match reviewed identities; ambiguous shortcuts and generic ranks are not accepted."""
    names, identity_names, parts, part_subjects, logical = {}, {}, {}, {}, {}
    for key, card in catalog.items():
        if card["category"] == "canon":
            identity_names.setdefault(card.get("subject_id", key), set()).update(
                [card["name"], *card.get("aliases", [])])
    for key, card in catalog.items():
        subject = card.get("subject_id", key)
        identity = capture_identity(card)
        aliases = [card["name"], *card.get("aliases", []), *identity_names.get(subject, [])]
        if identity != subject:
            aliases += list(identity_names.get(identity, []))
        names[key] = set().union(*(identity_forms(name, character=card["arena"] == "ground")
                                  for name in aliases))
        logical[key] = set().union(*(logical_identity_forms(name) for name in aliases))
        parts[key] = set()
        # Edition/phase captions do not become shortcuts such as 'trench' or 'limited'.
        identities = identity_names.get(subject) or [card["name"]]
        for source_name in identities:
            source_name = re.split(r"\s+[—–]\s+", source_name, maxsplit=1)[0]
            parts[key].update(normalize_alias(word.removesuffix("-class")) for word in name_words(source_name)
                              if word not in GENERIC_NAME_PARTS and len(normalize_alias(word)) >= 3
                              and not normalize_alias(word).isdigit())
        for word in parts[key]:
            part_subjects.setdefault(word, set()).add(identity)
        names[key].discard("")
    explicit_subjects = {}
    for key, accepted in names.items():
        for answer in accepted:
            explicit_subjects.setdefault(answer, set()).add(capture_identity(catalog[key]))
    for key, accepted in names.items():
        # Preserve exact printed identities, but do not turn overlapping nicknames
        # (e.g. Admiral Raddus and the Raddus ship) into ambiguous shortcuts.
        canonical = normalize_alias(catalog[key]["name"])
        accepted.difference_update(answer for answer in tuple(accepted)
                                   if answer != canonical and len(explicit_subjects[answer]) > 1)
        accepted.update(word for word in parts[key] if len(part_subjects[word]) == 1)
        accepted.difference_update(GENERIC_CAPTURE_ANSWERS - {canonical})
    # A newly generated code may not suppress a reviewed specific alias. It is
    # added only when the complete code has one person or documented model family.
    logical_subjects = {}
    for key in catalog:
        for answer in names[key] | logical[key]:
            logical_subjects.setdefault(answer, set()).add(capture_identity(catalog[key]))
    for key, accepted in names.items():
        accepted.update(answer for answer in logical[key] if len(logical_subjects[answer]) == 1)
    return names


def timestamp(value=None):
    if value is None:
        return time.time()
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            raise ValueError("Times must include a UTC offset")
        return value.timestamp()
    if isinstance(value, str):
        return timestamp(dt.datetime.fromisoformat(value))
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Time must be finite")
    return result


SCHEMA = """
CREATE TABLE IF NOT EXISTS players(
 guild_id TEXT NOT NULL,user_id TEXT NOT NULL,credits INTEGER NOT NULL DEFAULT 0 CHECK(credits>=0),
 xp INTEGER NOT NULL DEFAULT 0 CHECK(xp>=0),daily_day TEXT,streak INTEGER NOT NULL DEFAULT 0,
 wins INTEGER NOT NULL DEFAULT 0,losses INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(guild_id,user_id));
CREATE TABLE IF NOT EXISTS supply(card_id TEXT PRIMARY KEY,minted INTEGER NOT NULL DEFAULT 0 CHECK(minted>=0),reserved INTEGER NOT NULL DEFAULT 0 CHECK(reserved>=0));
CREATE TABLE IF NOT EXISTS drops(
 id TEXT PRIMARY KEY,guild_id TEXT NOT NULL,channel_id TEXT NOT NULL,card_id TEXT NOT NULL,
 message_id TEXT,created_at REAL NOT NULL,expires_at REAL NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('active','claimed','expired')),
 collector_limit INTEGER NOT NULL DEFAULT 2 CHECK(collector_limit BETWEEN 1 AND 6),
 delivery_pending INTEGER NOT NULL DEFAULT 0 CHECK(delivery_pending IN (0,1)),
 delivery_deadline REAL, capture_duration REAL,override_until REAL,request_id TEXT,request_signature TEXT,
 schedule_anchor INTEGER NOT NULL DEFAULT 1 CHECK(schedule_anchor IN (0,1)),
 uniform INTEGER NOT NULL DEFAULT 0 CHECK(uniform IN (0,1)));
CREATE UNIQUE INDEX IF NOT EXISTS one_active_drop ON drops(guild_id,channel_id) WHERE status='active';
CREATE TABLE IF NOT EXISTS instances(
 id TEXT PRIMARY KEY,guild_id TEXT NOT NULL,user_id TEXT NOT NULL,card_id TEXT NOT NULL,
 acquired_at REAL NOT NULL,origin TEXT NOT NULL,favorite INTEGER NOT NULL DEFAULT 0 CHECK(favorite IN (0,1)),
 locked_by TEXT,consumed_at REAL,
 attack_roll INTEGER NOT NULL DEFAULT 100 CHECK(attack_roll BETWEEN 95 AND 105),
 health_roll INTEGER NOT NULL DEFAULT 100 CHECK(health_roll BETWEEN 95 AND 105),
 stat_roll_version INTEGER NOT NULL DEFAULT 0 CHECK(stat_roll_version IN (0,1)));
CREATE INDEX IF NOT EXISTS inventory_owner ON instances(guild_id,user_id,consumed_at);
CREATE TABLE IF NOT EXISTS complaints(
 id TEXT PRIMARY KEY,guild_id TEXT NOT NULL,user_id TEXT NOT NULL,message TEXT NOT NULL,
 created_at REAL NOT NULL,request_id TEXT UNIQUE,resolved_at REAL,resolved_by TEXT);
CREATE INDEX IF NOT EXISTS complaint_queue ON complaints(guild_id,created_at DESC);
CREATE TABLE IF NOT EXISTS market_days(
 guild_id TEXT NOT NULL,day TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(guild_id,day));
CREATE TABLE IF NOT EXISTS market_offers(
 id TEXT PRIMARY KEY,guild_id TEXT NOT NULL,day TEXT NOT NULL,slot INTEGER NOT NULL,
 card_id TEXT NOT NULL,name TEXT NOT NULL,rarity TEXT NOT NULL,price INTEGER NOT NULL CHECK(price>0),
 stock INTEGER NOT NULL CHECK(stock>0),sold INTEGER NOT NULL DEFAULT 0 CHECK(sold>=0 AND sold<=stock),
 FOREIGN KEY(guild_id,day) REFERENCES market_days(guild_id,day),UNIQUE(guild_id,day,slot),UNIQUE(guild_id,day,card_id));
CREATE TABLE IF NOT EXISTS market_purchases(
 id TEXT PRIMARY KEY,guild_id TEXT NOT NULL,day TEXT NOT NULL,user_id TEXT NOT NULL,
 offer_id TEXT NOT NULL REFERENCES market_offers(id),instance_id TEXT NOT NULL REFERENCES instances(id),
 price INTEGER NOT NULL CHECK(price>0),created_at REAL NOT NULL,request_id TEXT UNIQUE,receipt TEXT NOT NULL,
 UNIQUE(guild_id,day,user_id),UNIQUE(instance_id));
CREATE TABLE IF NOT EXISTS claims(
 drop_id TEXT NOT NULL REFERENCES drops(id),user_id TEXT NOT NULL,instance_id TEXT NOT NULL UNIQUE REFERENCES instances(id),claimed_at REAL NOT NULL,
 PRIMARY KEY(drop_id,user_id));
CREATE TABLE IF NOT EXISTS capture_history(
 sequence INTEGER PRIMARY KEY,guild_id TEXT NOT NULL,drop_id TEXT NOT NULL,user_id TEXT NOT NULL,
 UNIQUE(drop_id,user_id),FOREIGN KEY(drop_id,user_id) REFERENCES claims(drop_id,user_id));
CREATE INDEX IF NOT EXISTS guild_capture_sequence ON capture_history(guild_id,sequence DESC);
CREATE TABLE IF NOT EXISTS attempts(drop_id TEXT NOT NULL REFERENCES drops(id),user_id TEXT NOT NULL,next_at REAL NOT NULL,PRIMARY KEY(drop_id,user_id));
CREATE TABLE IF NOT EXISTS wishlist(guild_id TEXT NOT NULL,user_id TEXT NOT NULL,card_id TEXT NOT NULL,PRIMARY KEY(guild_id,user_id,card_id));
CREATE TABLE IF NOT EXISTS ledger(
 id INTEGER PRIMARY KEY,guild_id TEXT NOT NULL,user_id TEXT NOT NULL,delta INTEGER NOT NULL,reason TEXT NOT NULL,reference TEXT NOT NULL,created_at REAL NOT NULL,
 UNIQUE(guild_id,user_id,reason,reference));
CREATE TABLE IF NOT EXISTS trades(
 id TEXT PRIMARY KEY,guild_id TEXT NOT NULL,a TEXT NOT NULL,b TEXT NOT NULL,created_at REAL NOT NULL,expires_at REAL NOT NULL,
 status TEXT NOT NULL DEFAULT 'open',version INTEGER NOT NULL DEFAULT 0,accepted_a INTEGER NOT NULL DEFAULT 0,accepted_b INTEGER NOT NULL DEFAULT 0,result TEXT,review_fingerprint TEXT,CHECK(a<>b));
CREATE TABLE IF NOT EXISTS trade_items(trade_id TEXT NOT NULL REFERENCES trades(id),instance_id TEXT NOT NULL UNIQUE REFERENCES instances(id),owner_id TEXT NOT NULL,PRIMARY KEY(trade_id,instance_id));
CREATE TABLE IF NOT EXISTS decks(guild_id TEXT NOT NULL,user_id TEXT NOT NULL,arena TEXT NOT NULL,instance_ids TEXT NOT NULL,PRIMARY KEY(guild_id,user_id,arena));
CREATE TABLE IF NOT EXISTS duels(
 id TEXT PRIMARY KEY,guild_id TEXT NOT NULL,a TEXT NOT NULL,b TEXT NOT NULL,arena TEXT NOT NULL,
 created_at REAL NOT NULL,expires_at REAL NOT NULL,status TEXT NOT NULL DEFAULT 'pending',proposal TEXT,result TEXT,CHECK(a<>b));
CREATE TABLE IF NOT EXISTS raids(
 id TEXT PRIMARY KEY,guild_id TEXT NOT NULL,channel_id TEXT NOT NULL,arena TEXT NOT NULL,message_id TEXT,
 created_at REAL NOT NULL,signup_ends_at REAL NOT NULL,expires_at REAL NOT NULL,status TEXT NOT NULL DEFAULT 'signup',max_hp INTEGER,hp INTEGER,boss TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_raid ON raids(guild_id,channel_id) WHERE status IN ('signup','active');
CREATE TABLE IF NOT EXISTS raid_members(
 raid_id TEXT NOT NULL REFERENCES raids(id),user_id TEXT NOT NULL,deck TEXT NOT NULL,per_attack INTEGER NOT NULL,
 damage INTEGER NOT NULL DEFAULT 0,attacks INTEGER NOT NULL DEFAULT 0 CHECK(attacks BETWEEN 0 AND 3),last_attack_at REAL,combat TEXT,
 PRIMARY KEY(raid_id,user_id));
CREATE TABLE IF NOT EXISTS raid_rewards(raid_id TEXT NOT NULL REFERENCES raids(id),user_id TEXT NOT NULL,credits INTEGER NOT NULL,xp INTEGER NOT NULL,PRIMARY KEY(raid_id,user_id));
"""


class Game:
    def __init__(self, db_path, catalog_path, *, enabled_events=(), enabled_releases=()):
        self.db_path = str(db_path)
        if isinstance(enabled_events,str):
            raise TypeError("Enabled events must be an explicit collection of event ids")
        event_ids = tuple(enabled_events)
        if any(not isinstance(key,str) or not key.strip() for key in event_ids):
            raise ValueError("Enabled events must be an explicit collection of event ids")
        self.enabled_events = frozenset(event_ids)
        if isinstance(enabled_releases, str):
            raise TypeError("Enabled releases must be an explicit collection of release keys")
        release_ids = tuple(enabled_releases)
        if any(not isinstance(key, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", key)
               for key in release_ids):
            raise ValueError("Enabled releases must contain valid release keys")
        self.enabled_releases = frozenset(release_ids)
        if self.db_path == ":memory:":
            raise ValueError("Use a durable SQLite file; per-operation connections are required")
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        document = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
        if (not isinstance(document,dict) or type(document.get("schema_version")) is not int
            or document.get("schema_version") != 1 or not isinstance(document.get("cards"), list)):
            raise ValueError("Unsupported catalogue schema")
        release_policy = document.get("release_policy")
        if "release_policy" in document and (
            not isinstance(release_policy, dict)
            or type(release_policy.get("schema_version")) is not int
            or release_policy.get("schema_version") != 1
            or release_policy.get("activation") != "explicit-release-keys"
        ):
            raise ValueError("Unsupported catalogue release policy")
        balance = document.get("balance")
        if balance is not None and (not isinstance(balance,dict)
                                    or type(balance.get("schema_version")) is not int
                                    or balance.get("schema_version") not in (3,4)
                                    or balance.get("formula")!=f"combat-power-v{balance['schema_version']}"):
            raise ValueError("Unsupported catalogue balance formula")
        limited_bonus = balance is not None and balance["schema_version"] == 4
        self.limited_bonus = limited_bonus
        if balance is not None and (limited_bonus or "limited_edition_bonus_percent" in balance):
            bonus = balance.get("limited_edition_bonus_percent")
            if type(bonus) is not int or bonus != (LIMITED_EDITION_BONUS_PERCENT if limited_bonus else 0):
                raise ValueError("Limited edition bonus policy does not match the balance formula")
        if balance is not None and "combat_power_points" in balance:
            published = balance["combat_power_points"]
            expected_points = {str(rating): points for rating, points in COMBAT_POWER_POINTS.items()}
            if (not isinstance(published, dict) or published != expected_points
                or any(type(points) is not int for points in published.values())):
                raise ValueError("Combat points do not match the published balance formula")
        spawn_model = document.get("spawn_model")
        if spawn_model is not None and (not isinstance(spawn_model, dict)
                                      or type(spawn_model.get("schema_version")) is not int
                                      or (spawn_model.get("schema_version"), spawn_model.get("formula"))
                                      not in ((2, "tier-first-v2"), (3, "tier-first-release-lanes-v3"))):
            raise ValueError("Unsupported catalogue spawn formula")
        legacy_spawn = spawn_model is not None and spawn_model["schema_version"] == 2
        rate_revision = 3
        if spawn_model is not None and not legacy_spawn:
            # Catalogs saved before explicit rate revisions retain their odds
            # when opened from backups; the lane formula itself is unchanged.
            rate_revision = spawn_model.get("rate_revision", 1)
            if type(rate_revision) is not int or rate_revision not in (1, 2, 3):
                raise ValueError("Unsupported catalogue spawn rate revision")
        self.spawn_weights = dict(LEGACY_SPAWN_WEIGHTS if legacy_spawn else
                                  SPAWN_WEIGHTS_V3_REVISION_1 if rate_revision == 1 else
                                  SPAWN_WEIGHTS_V3_REVISION_2 if rate_revision == 2 else
                                  RARITY_SPAWN_WEIGHTS)
        self.limited_share_percent = 0 if legacy_spawn else LIMITED_SHARE_PERCENT
        if spawn_model is not None and not legacy_spawn and (
            type(spawn_model.get("limited_share_percent")) is not int
            or spawn_model.get("limited_share_percent") != LIMITED_SHARE_PERCENT
        ):
            raise ValueError("Limited allocation does not match the published spawn formula")
        self.catalog = {}
        self.rarity_values = dict(VALUES)
        for rarity, data in document.get("rarities", {}).items():
            if rarity in VALUES and isinstance(data, dict) and isinstance(data.get("value"), int) and data["value"] > 0:
                self.rarity_values[rarity] = data["value"]
            if isinstance(data,dict) and "power_multiplier" in data and (rarity not in RARITY_POWER or data["power_multiplier"]!=RARITY_POWER[rarity]):
                raise ValueError("Rarity multiplier does not match the published balance formula")
            if (isinstance(data, dict) and "spawn_weight" in data
                and (rarity not in self.spawn_weights or type(data["spawn_weight"]) is not int
                     or data["spawn_weight"] != self.spawn_weights[rarity])):
                raise ValueError("Rarity spawn weight does not match the published spawn formula")
            if (isinstance(data, dict) and "collector_limit" in data
                and (rarity not in COLLECTOR_LIMITS or type(data["collector_limit"]) is not int
                     or data["collector_limit"] != COLLECTOR_LIMITS[rarity])):
                raise ValueError("Rarity collector limit does not match the published collector policy")
        for source in document["cards"]:
            card = dict(source)
            if not card.get("id") or card["id"] in self.catalog:
                raise ValueError("Card ids must be unique and nonempty")
            if card.get("rarity") not in RARITIES or card.get("arena") not in ARENAS or card.get("role") not in ROLES:
                raise ValueError("Invalid rarity, arena or role for " + card["id"])
            if card.get("category") not in ("canon", "legends", "meme", "limited"):
                raise ValueError("Invalid card category")
            for field in ("attack", "health", "speed"):
                if not isinstance(card.get(field), int) or card[field] <= 0:
                    raise ValueError("Stats must be positive integers")
            if balance is not None:
                profile = card.get("power_profile")
                if (not isinstance(profile, dict)
                    or not profile.keys() <= POWER_PROFILE_FIELDS
                    or not isinstance(profile.get("basis"), str)
                    or profile.get("basis") not in POWER_PROFILE_BASES
                    or not isinstance(profile.get("rationale"), str) or not profile["rationale"].strip()
                    or ("sources" in profile and (not isinstance(profile["sources"], list)
                        or any(not isinstance(url, str) or not url.strip() for url in profile["sources"])))):
                    raise ValueError("Card requires an explicit game-design power profile: "+card["id"])
                expected = balance_stats(card["id"],card["rarity"],card["role"],card["arena"],
                                         profile.get("combat"),
                                         limited_edition=limited_bonus and card["category"]=="limited")
                if any(card[field]!=expected[field] for field in expected):
                    raise ValueError("Card stats do not match "+balance["formula"]+": "+card["id"])
            weight = float(card.get("weight", 0))
            if not math.isfinite(weight) or weight < 0:
                raise ValueError("Weights must be finite and nonnegative")
            card["weight"] = weight
            release = card.get("release")
            if release:
                starts, ends = timestamp(release["starts_at"]), timestamp(release["ends_at"])
                if starts >= ends or type(release.get("supply_cap")) is not int or release["supply_cap"] < 1:
                    raise ValueError("Release requires a positive window and at least one copy")
            elif card["category"] == "limited":
                raise ValueError("Limited cards require an explicit release window and supply cap")
            release_id = card.get("release_id")
            if (("release_id" in card or (card["category"] == "limited" and (
                    release_policy is not None or (spawn_model is not None and not legacy_spawn))))
                and (not isinstance(release_id, str)
                     or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", release_id) or not release)):
                raise ValueError("Seasonal cards require a valid release key and release window")
            if self._event_exclusive(card) and (not isinstance(card.get("event_id"),str) or not card["event_id"].strip() or not release):
                raise ValueError("Event-exclusive cards require an event id and release window/supply cap")
            collector_limit(card)
            boss = card.get("raid_boss")
            if boss is not None and (not isinstance(boss, dict) or boss.get("difficulty") not in RAID_TIERS
                                     or boss.get("move") not in RAID_MOVES or card["category"] != "canon"):
                raise ValueError("Invalid regular raid boss definition: " + card["id"])
            self.catalog[card["id"]] = card
        if not self.catalog:
            raise ValueError("The catalogue cannot be empty")
        known_releases = {card["release_id"] for card in self.catalog.values() if card.get("release_id")}
        if self.enabled_releases - known_releases:
            raise ValueError("Enabled release key does not exist in the catalogue")
        if limited_bonus:
            for card in self.catalog.values():
                if card["category"] != "limited":
                    continue
                base_id = card.get("variant_of")
                if not isinstance(base_id,str) or not base_id or base_id not in self.catalog:
                    raise ValueError("Limited edition requires an existing variant_of base: "+card["id"])
                base = self.catalog[base_id]
                if base["category"] == "limited":
                    raise ValueError("Limited edition base cannot itself be limited: "+card["id"])
                if base.get("variant_of") is not None:
                    raise ValueError("Limited edition requires a direct base without variant chains: "+card["id"])
                subject = card.get("subject_id")
                if (not isinstance(subject,str) or not subject.strip()
                    or subject != base.get("subject_id") or card["arena"] != base["arena"]):
                    raise ValueError("Limited edition base must share subject and arena: "+card["id"])
                ability_fields = ("ability","ability_name","ability_description")
                if any(not isinstance(item.get(field),str) or not item[field].strip()
                       for item in (card,base) for field in ability_fields):
                    raise ValueError("Limited edition and base require an ability bundle: "+card["id"])
                if card["power_profile"]["basis"] != "depiction_override":
                    shared_fields = ("rarity","role",*ability_fields)
                    if (any(card.get(field)!=base.get(field) for field in shared_fields)
                        or card["power_profile"]["combat"]!=base["power_profile"]["combat"]):
                        raise ValueError("Comparable limited edition must match base rarity, combat, role and ability: "+card["id"])
        self.capture_names = capture_names(self.catalog)
        self.rng = random.SystemRandom()
        with contextlib.closing(self._connect()) as connection, connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4, 5, 6, 7):
                raise ValueError("Database schema is newer than this engine")
            if ([row[0] for row in connection.execute("PRAGMA integrity_check")] != ["ok"]
                or connection.execute("PRAGMA foreign_key_check").fetchone() is not None):
                raise ValueError("The game database failed integrity or foreign-key validation; nothing migrated")
            if connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                raise ValueError("The game database could not enable WAL durability")
            # executescript commits any existing transaction first. Put BEGIN
            # inside the script so base tables and migrations commit together.
            connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
            if "result" not in {row["name"] for row in connection.execute("PRAGMA table_info(trades)")}:
                connection.execute("ALTER TABLE trades ADD COLUMN result TEXT")
            if "review_fingerprint" not in {row["name"] for row in connection.execute("PRAGMA table_info(trades)")}:
                connection.execute("ALTER TABLE trades ADD COLUMN review_fingerprint TEXT")
            if "proposal" not in {row["name"] for row in connection.execute("PRAGMA table_info(duels)")}:
                connection.execute("ALTER TABLE duels ADD COLUMN proposal TEXT")
            if "collector_limit" not in {row["name"] for row in connection.execute("PRAGMA table_info(drops)")}:
                # Legacy encounters reserved two slots; preserve that commitment.
                connection.execute("ALTER TABLE drops ADD COLUMN collector_limit INTEGER NOT NULL DEFAULT 2 CHECK(collector_limit BETWEEN 1 AND 6)")
            drop_columns = {row["name"] for row in connection.execute("PRAGMA table_info(drops)")}
            for table, column in (("raids", "boss"), ("raid_members", "combat")):
                if column not in {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
            instance_columns = {row["name"] for row in connection.execute("PRAGMA table_info(instances)")}
            for column, definition in (("attack_roll", "INTEGER NOT NULL DEFAULT 100 CHECK(attack_roll BETWEEN 95 AND 105)"),
                                       ("health_roll", "INTEGER NOT NULL DEFAULT 100 CHECK(health_roll BETWEEN 95 AND 105)"),
                                       ("stat_roll_version", "INTEGER NOT NULL DEFAULT 0 CHECK(stat_roll_version IN (0,1))")):
                if column not in instance_columns:
                    connection.execute(f"ALTER TABLE instances ADD COLUMN {column} {definition}")
            for name, definition in (("delivery_pending", "INTEGER NOT NULL DEFAULT 0 CHECK(delivery_pending IN (0,1))"),
                                     ("delivery_deadline", "REAL"), ("capture_duration", "REAL"),
                                     ("override_until", "REAL"), ("request_id", "TEXT"), ("request_signature", "TEXT"),
                                     ("schedule_anchor", "INTEGER NOT NULL DEFAULT 1 CHECK(schedule_anchor IN (0,1))"),
                                     ("uniform", "INTEGER NOT NULL DEFAULT 0 CHECK(uniform IN (0,1))")):
                if name not in drop_columns:
                    connection.execute(f"ALTER TABLE drops ADD COLUMN {name} {definition}")
            connection.execute("""CREATE INDEX IF NOT EXISTS published_drop_time ON drops(guild_id,created_at DESC)
                WHERE message_id IS NOT NULL AND delivery_pending=0""")
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS drop_request ON drops(request_id) WHERE request_id IS NOT NULL")
            connection.execute("DROP TRIGGER IF EXISTS two_collectors")
            connection.execute("""CREATE TRIGGER IF NOT EXISTS collector_capacity BEFORE INSERT ON claims
                WHEN (SELECT COUNT(*) FROM claims WHERE drop_id=NEW.drop_id)>=
                     (SELECT collector_limit FROM drops WHERE id=NEW.drop_id)
                BEGIN SELECT RAISE(ABORT,'collector limit'); END""")
            connection.execute("""CREATE TRIGGER IF NOT EXISTS published_capture BEFORE INSERT ON claims
                WHEN (SELECT delivery_pending FROM drops WHERE id=NEW.drop_id)=1
                BEGIN SELECT RAISE(ABORT,'encounter not published'); END""")
            if version < 4:
                # Preserve the stored insertion order of legacy claims, including
                # receipts without credit-ledger entries. Future ordering uses an
                # explicit integer key, independent of clocks and VACUUM rowids.
                connection.execute("""INSERT OR IGNORE INTO capture_history(guild_id,drop_id,user_id)
                    SELECT d.guild_id,c.drop_id,c.user_id FROM claims c
                    JOIN drops d ON d.id=c.drop_id ORDER BY c.rowid""")
            connection.execute("""CREATE TRIGGER IF NOT EXISTS record_capture_history AFTER INSERT ON claims
                BEGIN INSERT INTO capture_history(guild_id,drop_id,user_id)
                    VALUES((SELECT guild_id FROM drops WHERE id=NEW.drop_id),NEW.drop_id,NEW.user_id); END""")
            referenced = connection.execute("""
                SELECT card_id FROM instances
                UNION SELECT card_id FROM drops
                UNION SELECT card_id FROM wishlist
                UNION SELECT card_id FROM supply WHERE minted>0 OR reserved>0
                UNION SELECT card_id FROM market_offers
            """).fetchall()
            missing = sorted({row["card_id"] for row in referenced} - self.catalog.keys())
            if missing:
                sample = ", ".join(missing[:10])
                raise ValueError(
                    f"Catalog removes {len(missing)} card definition(s) referenced by saved state: {sample}. "
                    "Restore those definitions; use weight 0 to retire them from draws without deleting player history."
                )
            connection.executemany("INSERT OR IGNORE INTO supply(card_id) VALUES(?)", [(key,) for key in self.catalog])
            connection.execute("PRAGMA user_version=7")
            connection.commit()

    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")
            connection.execute("PRAGMA synchronous=FULL")
            if (connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
                or connection.execute("PRAGMA synchronous").fetchone()[0] != 2):
                raise ValueError("The game database requires foreign keys and FULL synchronization")
        except BaseException:
            connection.close()
            raise
        return connection

    @contextlib.contextmanager
    def _tx(self):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except GameError as error:
            connection.commit() if error.persist else connection.rollback()
            raise
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _card(self, card_id):
        try:
            return self.catalog[str(card_id)]
        except KeyError:
            raise GameError("unknown_card", "That card is not in the catalogue.")

    def _player(self, connection, guild_id, user_id):
        guild_id, user_id = str(guild_id), str(user_id)
        connection.execute("INSERT OR IGNORE INTO players(guild_id,user_id) VALUES(?,?)", (guild_id, user_id))
        return connection.execute("SELECT * FROM players WHERE guild_id=? AND user_id=?", (guild_id, user_id)).fetchone()

    def _credit(self, connection, guild_id, user_id, amount, reason, reference, now, xp=0):
        player = self._player(connection, guild_id, user_id)
        if player["credits"] + amount < 0:
            raise GameError("insufficient_credits", "You do not have enough credits.")
        inserted = connection.execute("INSERT OR IGNORE INTO ledger(guild_id,user_id,delta,reason,reference,created_at) VALUES(?,?,?,?,?,?)",
                                      (str(guild_id), str(user_id), amount, reason, str(reference), now)).rowcount
        if inserted:
            connection.execute("UPDATE players SET credits=credits+?,xp=xp+? WHERE guild_id=? AND user_id=?", (amount, xp, str(guild_id), str(user_id)))
        return inserted

    def _expire(self, connection, now):
        rows = connection.execute("SELECT id,card_id,expires_at,collector_limit,override_until FROM drops WHERE status='active'").fetchall()
        for row in rows:
            allowed = (now < row["override_until"] if row["override_until"] is not None
                       else self._event_allowed(self._card(row["card_id"]),now))
            if row["expires_at"] > now and allowed:
                continue
            claims = connection.execute("SELECT COUNT(*) FROM claims WHERE drop_id=?", (row["id"],)).fetchone()[0]
            connection.execute("UPDATE supply SET reserved=reserved-? WHERE card_id=?", (row["collector_limit"] - claims, row["card_id"]))
            connection.execute("UPDATE drops SET status='expired' WHERE id=?", (row["id"],))
        for row in connection.execute("SELECT id FROM trades WHERE status='open' AND expires_at<=?", (now,)).fetchall():
            self._close_trade(connection, row["id"], "expired")
        for row in connection.execute("SELECT id FROM raids WHERE status IN ('signup','active')").fetchall():
            self._advance_raid(connection,row["id"],now)

    def _release_allowed(self, card, now):
        """Explicit seasonal activation and calendar gate; supply is transactional."""
        release_id = card.get("release_id")
        if not release_id:
            return True  # Historical catalogues without release keys keep their rules.
        release = card.get("release")
        return bool(release_id in self.enabled_releases and release
                    and timestamp(release["starts_at"]) <= now < timestamp(release["ends_at"]))

    def _event_allowed(self,card,now):
        if not self._release_allowed(card, now):
            return False
        if not self._event_exclusive(card):
            return True
        release = card.get("release")
        return bool(card.get("event_id") in self.enabled_events and release and timestamp(release["starts_at"]) <= now < timestamp(release["ends_at"]))

    @staticmethod
    def _event_exclusive(card):
        return _is_event_exclusive(card)

    def _in_pool(self,card,event_id):
        return not self._event_exclusive(card) if event_id is None else card.get("event_id")==event_id

    def _regular_definition(self,card):
        return card["category"]!="limited" and not card.get("release") and not self._event_exclusive(card)

    def _eligible(self, connection, card, now, reserve=1, *, override=False):
        release = card.get("release")
        if card["weight"] <= 0 or (not override and not self._event_allowed(card,now)):
            return False
        if release:
            if not override and not timestamp(release["starts_at"]) <= now < timestamp(release["ends_at"]):
                return False
            supply = connection.execute("SELECT minted,reserved FROM supply WHERE card_id=?", (card["id"],)).fetchone()
            if supply["minted"] + supply["reserved"] + reserve > release["supply_cap"]:
                return False
        return True

    def _drop_capacity(self, connection, card):
        limit = collector_limit(card)
        if card.get("release"):
            supply = connection.execute("SELECT minted,reserved FROM supply WHERE card_id=?", (card["id"],)).fetchone()
            limit = min(limit, card["release"]["supply_cap"]-supply["minted"]-supply["reserved"])
        return max(0, limit)

    @staticmethod
    def _spawn_probabilities(cards, *, tier_weights=None, limited_share_percent=LIMITED_SHARE_PERCENT):
        """Rarity first; eligible seasonal editions share a published tier budget."""
        tier_weights = RARITY_SPAWN_WEIGHTS if tier_weights is None else tier_weights
        totals = Counter()
        lanes = Counter()
        for card in cards:
            if card["weight"] > 0:
                totals[card["rarity"]] += card["weight"]
                lanes[(card["rarity"], card.get("category") == "limited")] += card["weight"]
        total_mass = sum(tier_weights[rarity] for rarity in totals)
        if not total_mass:
            return [0.0 for _ in cards]
        result = []
        for card in cards:
            if card["weight"] <= 0:
                result.append(0.0)
                continue
            rarity, limited = card["rarity"], card.get("category") == "limited"
            probability = tier_weights[rarity] / total_mass
            if limited_share_percent:
                share = (limited_share_percent if limited else 100-limited_share_percent)/100
                probability *= share if lanes[(rarity, not limited)] else 1
                probability *= card["weight"]/lanes[(rarity, limited)]
            else:
                probability *= card["weight"]/totals[rarity]
            result.append(probability)
        return result

    @staticmethod
    def _draw_options(now, override_until, uniform, rarity, *, replay=False):
        if type(uniform) is not bool:
            raise ValueError("Uniform selection must be a boolean")
        if rarity is not None and rarity not in RARITIES:
            raise GameError("invalid_rarity", "Choose a catalogue rarity.")
        if override_until is not None:
            if isinstance(override_until, bool):
                raise ValueError("Encounter overrides need a finite deadline")
            override_until = timestamp(override_until)
            if not replay and not now < override_until <= now+3600:
                raise GameError("invalid_override", "An encounter override must end within the next hour.")
        elif uniform:
            raise GameError("invalid_override", "Equal-chance encounters need a bounded event deadline.")
        return override_until

    @staticmethod
    def _allowed_card_ids(allowed_card_ids):
        if isinstance(allowed_card_ids, str):
            raise TypeError("Allowed card ids must be a collection, not one string")
        return None if allowed_card_ids is None else frozenset(str(key) for key in allowed_card_ids)

    def _draw_cards(self, connection, now, event_id, override_until, rarity, allowed_card_ids):
        override = override_until is not None
        return [card for card in self.catalog.values()
                if (override or self._in_pool(card,event_id))
                and (rarity is None or card["rarity"] == rarity)
                and (allowed_card_ids is None or card["id"] in allowed_card_ids)
                and self._eligible(connection, card, now, override=override)]

    def _draw_probabilities(self, cards, uniform):
        return ([1/len(cards)]*len(cards) if cards else []) if uniform else self._spawn_probabilities(
            cards, tier_weights=self.spawn_weights, limited_share_percent=self.limited_share_percent)

    def spawn_pool(self, now=None, *, event_id=None, override_until=None, uniform=False, rarity=None, allowed_card_ids=None):
        now = timestamp(now)
        override_until = self._draw_options(now, override_until, uniform, rarity)
        allowed_card_ids = self._allowed_card_ids(allowed_card_ids)
        with self._tx() as connection:
            self._expire(connection, now)
            cards = self._draw_cards(connection, now, event_id, override_until, rarity, allowed_card_ids)
            probabilities = self._draw_probabilities(cards, uniform)
            result = []
            for card, probability in zip(cards, probabilities):
                stock = connection.execute("SELECT minted,reserved FROM supply WHERE card_id=?", (card["id"],)).fetchone()
                cap = card.get("release", {}).get("supply_cap") if card.get("release") else None
                result.append({"id": card["id"], "name": card["name"], "weight": card["weight"],
                               "probability": probability, "remaining_supply": cap - stock["minted"] - stock["reserved"] if cap else None,
                               "collector_limit": self._drop_capacity(connection, card),
                               "configured_collector_limit": collector_limit(card)})
            return result

    def create_drop(self, guild_id, channel_id, card_id=None, now=None, duration=180, *, event_id=None,
                    defer_publication=False, override_until=None, uniform=False, rarity=None, request_id=None,
                    allowed_card_ids=None, anchors_schedule=True):
        now = timestamp(now)
        # An acknowledged interaction remains replayable after its deadline, even
        # when the original encounter has already been captured or expired.
        override_until = self._draw_options(now, override_until, uniform, rarity, replay=True)
        allowed_card_ids = self._allowed_card_ids(allowed_card_ids)
        if type(defer_publication) is not bool:
            raise ValueError("Deferred publication must be a boolean")
        if type(anchors_schedule) is not bool:
            raise ValueError("Schedule anchoring must be a boolean")
        if not 1 <= duration <= 3600:
            raise GameError("invalid_duration", "Drops last between 1 and 3600 seconds.")
        if request_id is not None:
            request_id = str(request_id)
            if not request_id.strip():
                raise ValueError("An encounter request id cannot be blank")
        signature = json.dumps({"guild_id": str(guild_id), "channel_id": str(channel_id),
            "card_id": str(card_id) if card_id is not None else None, "event_id": event_id,
            "override": override_until is not None, "uniform": uniform, "rarity": rarity,
            "duration": duration, "defer_publication": defer_publication,
            "anchors_schedule": anchors_schedule}, sort_keys=True, separators=(",", ":"))
        with self._tx() as connection:
            self._expire(connection, now)
            if request_id is not None:
                previous = connection.execute("SELECT id,request_signature FROM drops WHERE request_id=?", (request_id,)).fetchone()
                if previous:
                    if previous["request_signature"] != signature:
                        raise GameError("request_conflict", "This request already created a different encounter.")
                    return self._drop(connection, previous["id"])
            self._draw_options(now, override_until, uniform, rarity)
            existing = connection.execute("SELECT id FROM drops WHERE guild_id=? AND channel_id=? AND status='active'", (str(guild_id), str(channel_id))).fetchone()
            if existing:
                raise GameError("active_drop", "A drop is already active in this channel.", details={"drop_id": existing["id"]})
            if card_id is None:
                cards = self._draw_cards(connection, now, event_id, override_until, rarity, allowed_card_ids)
                if not cards:
                    raise GameError("no_available_cards", "No cards are eligible to spawn right now.")
                card = self.rng.choices(cards, weights=self._draw_probabilities(cards, uniform), k=1)[0]
            else:
                card = self._card(card_id)
                if ((override_until is None and not self._in_pool(card,event_id))
                    or (rarity is not None and card["rarity"] != rarity)
                    or (allowed_card_ids is not None and card["id"] not in allowed_card_ids)
                    or not self._eligible(connection, card, now, override=override_until is not None)):
                    raise GameError("unavailable_card", "That card is outside its release/supply, or requires a matching explicitly enabled event.")
            expiry = now + (DELIVERY_TIMEOUT if defer_publication else duration)
            if override_until is not None:
                expiry = min(expiry, override_until)
            elif card.get("release"):
                expiry = min(expiry, timestamp(card["release"]["ends_at"]))
            limit = self._drop_capacity(connection, card)
            if not limit:
                raise GameError("unavailable_card", "That edition has no unreserved copies remaining.")
            drop_id = secrets.token_hex(8)
            connection.execute("""INSERT INTO drops(id,guild_id,channel_id,card_id,created_at,expires_at,status,
                collector_limit,delivery_pending,delivery_deadline,capture_duration,override_until,request_id,request_signature,schedule_anchor,uniform)
                VALUES(?,?,?,?,?,?,'active',?,?,?,?,?,?,?,?,?)""",
                (drop_id, str(guild_id), str(channel_id), card["id"], now, expiry, limit,
                 int(defer_publication), expiry if defer_publication else None, duration if defer_publication else None,
                 override_until, request_id, signature if request_id is not None else None,
                 int(anchors_schedule), int(uniform and card_id is None and rarity is None)))
            connection.execute("UPDATE supply SET reserved=reserved+? WHERE card_id=?", (limit, card["id"]))
            return self._drop(connection, drop_id)

    def _drop(self, connection, drop_id):
        row = connection.execute("SELECT * FROM drops WHERE id=?", (str(drop_id),)).fetchone()
        if not row:
            raise GameError("unknown_drop", "That drop no longer exists.")
        result = dict(row)
        result["claims"] = [dict(claim) for claim in connection.execute("SELECT c.user_id,c.instance_id,c.claimed_at FROM claims c JOIN capture_history h ON h.drop_id=c.drop_id AND h.user_id=c.user_id WHERE c.drop_id=? ORDER BY h.sequence", (str(drop_id),))]
        result.update(card=self._card(row["card_id"]), remaining=max(0, row["collector_limit"]-len(result["claims"])),
                      max_collectors=row["collector_limit"], event_id=self._card(row["card_id"]).get("event_id"))
        for claim in result["claims"]:
            copy = connection.execute("SELECT * FROM instances WHERE id=?", (claim["instance_id"],)).fetchone()
            claim.update(self.copy_stats(result["card"], copy))
        return result

    def get_drop(self, drop_id):
        with self._tx() as connection:
            self._expire(connection, timestamp())
            return self._drop(connection, drop_id)

    def drop_for_request(self, request_id, guild_id, channel_id):
        """Recover an acknowledged admin draw without consulting a changed event mode."""
        with self._tx() as connection:
            self._expire(connection, timestamp())
            row = connection.execute("SELECT id,guild_id,channel_id FROM drops WHERE request_id=?",
                                     (str(request_id),)).fetchone()
            if row is None:
                return None
            if (row["guild_id"], row["channel_id"]) != (str(guild_id), str(channel_id)):
                raise GameError("request_conflict", "This request belongs to a different server or channel.")
            return self._drop(connection, row["id"])

    def cancel_pending_overrides(self, guild_id, override_until, now=None):
        """Stopping an event cancels only its unpublished reservations, never captures."""
        now, override_until = timestamp(now), timestamp(override_until)
        with self._tx() as connection:
            self._expire(connection, now)
            rows = connection.execute("""SELECT id,card_id,collector_limit FROM drops
                WHERE guild_id=? AND override_until=? AND status='active' AND delivery_pending=1""",
                (str(guild_id), override_until)).fetchall()
            for row in rows:
                claims = connection.execute("SELECT COUNT(*) FROM claims WHERE drop_id=?", (row["id"],)).fetchone()[0]
                connection.execute("UPDATE supply SET reserved=reserved-? WHERE card_id=?",
                                   (row["collector_limit"]-claims, row["card_id"]))
                connection.execute("UPDATE drops SET status='expired' WHERE id=?", (row["id"],))
            return [self._drop(connection, row["id"]) for row in rows]

    def active_drops(self, guild_id=None, now=None):
        with self._tx() as connection:
            self._expire(connection, timestamp(now))
            query, args = "SELECT id FROM drops WHERE status='active'", []
            if guild_id is not None:
                query += " AND guild_id=?"
                args.append(str(guild_id))
            return [self._drop(connection, row["id"]) for row in connection.execute(query, args)]

    def recent_drops(self,guild_id,limit=25,now=None):
        with self._tx() as connection:
            self._expire(connection,timestamp(now))
            rows = connection.execute("SELECT id FROM drops WHERE guild_id=? ORDER BY created_at DESC,id DESC LIMIT ?",(str(guild_id),max(1,min(200,int(limit))))).fetchall()
            return [self._drop(connection,row["id"]) for row in rows]

    def attach_message(self, drop_id, message_id):
        with self._tx() as connection:
            if self._drop(connection, drop_id)["delivery_pending"]:
                raise GameError("publication_required", "Publish this encounter before enabling captures.")
            connection.execute("UPDATE drops SET message_id=? WHERE id=?", (str(message_id), str(drop_id)))

    def last_published_drop(self, guild_id):
        with self._tx() as connection:
            row = connection.execute("""SELECT id FROM drops WHERE guild_id=?
                AND message_id IS NOT NULL AND delivery_pending=0 AND schedule_anchor=1 ORDER BY created_at DESC LIMIT 1""",
                (str(guild_id),)).fetchone()
            return self._drop(connection, row["id"]) if row else None

    def publish_drop(self, drop_id, message_id, now=None):
        """Activate one acknowledged message once; retries never extend its clock."""
        now = timestamp(now)
        message_id = str(message_id)
        if not message_id:
            raise ValueError("Publication requires a message id")
        with self._tx() as connection:
            self._expire(connection, now)
            drop = self._drop(connection, drop_id)
            if not drop["delivery_pending"]:
                if drop.get("message_id") == message_id:
                    return drop
                raise GameError("already_published", "This encounter is already bound to its published message.")
            if drop.get("message_id") not in (None, message_id):
                raise GameError("already_published", "This encounter is already bound to another message.")
            # Remember late acknowledgements too, so reconciliation can disable
            # an expired placeholder without allocating or minting anything.
            connection.execute("UPDATE drops SET message_id=? WHERE id=?", (message_id, str(drop_id)))
            if drop["status"] != "active" or now >= drop["delivery_deadline"]:
                raise GameError("delivery_expired", "This encounter could not be delivered in time.", persist=True)
            expiry = now + drop["capture_duration"]
            release = drop["card"].get("release")
            if drop["override_until"] is not None:
                expiry = min(expiry, drop["override_until"])
            elif release:
                expiry = min(expiry, timestamp(release["ends_at"]))
            connection.execute("""UPDATE drops SET delivery_pending=0,created_at=?,expires_at=?
                WHERE id=?""", (now, expiry, str(drop_id)))
            return self._drop(connection, drop_id)

    def _instance(self, connection, instance_id, guild_id=None, user_id=None):
        row = connection.execute("SELECT * FROM instances WHERE id=? AND consumed_at IS NULL", (str(instance_id),)).fetchone()
        if not row or (guild_id is not None and row["guild_id"] != str(guild_id)) or (user_id is not None and row["user_id"] != str(user_id)):
            raise GameError("not_owned", "That active card is not in your inventory.")
        result, card = dict(row), self._card(row["card_id"])
        result.update(instance_id=row["id"], card=card, name=card["name"], favorite=bool(row["favorite"]))
        result.update(self.copy_stats(card, row))
        return result

    def copy_stats(self, card, row):
        """Persisted copy percentages survive transfers/restarts and follow catalog rebalance."""
        base = card
        special = self.limited_bonus and card["category"] == "limited"
        if row["stat_roll_version"] and special:
            base = balance_stats(card["id"], card["rarity"], card["role"], card["arena"], card["power_profile"]["combat"])
        stats = {"speed": card["speed"], "attack_roll": row["attack_roll"], "health_roll": row["health_roll"]}
        for stat in ("attack", "health"):
            value = (base[stat]*row[stat+"_roll"]+50)//100 if row["stat_roll_version"] else card[stat]
            stats[stat] = (value*110+50)//100 if row["stat_roll_version"] and special else value
        return stats

    def _mint(self, connection, guild_id, user_id, card_id, now, origin):
        instance_id = secrets.token_hex(8)
        attack_roll, health_roll = 95+secrets.randbelow(11), 95+secrets.randbelow(11)
        connection.execute("INSERT INTO instances(id,guild_id,user_id,card_id,acquired_at,origin,attack_roll,health_roll,stat_roll_version) VALUES(?,?,?,?,?,?,?,?,1)",
                           (instance_id, str(guild_id), str(user_id), card_id, now, origin, attack_roll, health_roll))
        connection.execute("UPDATE supply SET minted=minted+1 WHERE card_id=?", (card_id,))
        self._player(connection, guild_id, user_id)
        return self._instance(connection, instance_id)

    def claim(self, drop_id, user_id, guess, now=None):
        now, user_id = timestamp(now), str(user_id)
        with self._tx() as connection:
            self._expire(connection, now)
            drop = self._drop(connection, drop_id)
            previous = next((claim for claim in drop["claims"] if claim["user_id"] == user_id), None)
            if previous:
                # Ownership may since have changed, but a replay never mints.
                instance = {"id": previous["instance_id"],"instance_id": previous["instance_id"],"guild_id": drop["guild_id"],"user_id": user_id,
                                "card_id": drop["card_id"],"card": drop["card"],"name": drop["card"]["name"],"attack": drop["card"]["attack"],
                                "health": drop["card"]["health"],"speed": drop["card"]["speed"],"favorite": False,"locked_by": None,
                                "acquired_at": previous["claimed_at"],"origin": "drop:"+str(drop_id),"historical_receipt": True}
                instance.update(self.copy_stats(drop["card"], connection.execute("SELECT * FROM instances WHERE id=?", (previous["instance_id"],)).fetchone()))
                return {"instance": instance, "remaining": drop["remaining"], "drop": drop, "credits_earned": 0, "replayed": True}
            if drop["status"] != "active":
                raise GameError("drop_closed", "This drop is full or expired.", persist=True)
            if drop["delivery_pending"]:
                raise GameError("drop_not_published", "This encounter is getting ready. Capture opens shortly.", persist=True)
            recent = connection.execute("""SELECT user_id FROM capture_history
                WHERE guild_id=? ORDER BY sequence DESC LIMIT 2""", (drop["guild_id"],)).fetchall()
            if len(recent) == 2 and all(row["user_id"] == user_id for row in recent):
                raise GameError("capture_streak_limit", "You caught the last two cards. Let another collector catch one before you catch again.", persist=True)
            attempt = connection.execute("SELECT next_at FROM attempts WHERE drop_id=? AND user_id=?", (str(drop_id), user_id)).fetchone()
            if attempt and now < attempt["next_at"]:
                wait = max(1, math.ceil(attempt["next_at"]-now))
                raise GameError("guess_cooldown", f"Wait {wait} seconds before another guess.", details={"retry_after": wait}, persist=True)
            accepted = self.capture_names[drop["card_id"]]
            if not normalize_alias(guess) or not {normalize_alias(guess), untitled_name(guess)} & accepted:
                connection.execute("INSERT INTO attempts(drop_id,user_id,next_at) VALUES(?,?,?) ON CONFLICT(drop_id,user_id) DO UPDATE SET next_at=excluded.next_at", (str(drop_id), user_id, now+WRONG_COOLDOWN))
                raise GameError("wrong_name", "That name is not accepted. Use its name, nickname, or a matching name part; titles are optional. Try again in 3 seconds.", details={"retry_after": 3}, persist=True)
            instance = self._mint(connection, drop["guild_id"], user_id, drop["card_id"], now, "drop:"+str(drop_id))
            connection.execute("INSERT INTO claims(drop_id,user_id,instance_id,claimed_at) VALUES(?,?,?,?)", (str(drop_id), user_id, instance["id"], now))
            connection.execute("UPDATE supply SET reserved=reserved-1 WHERE card_id=?", (drop["card_id"],))
            remaining = drop["remaining"]-1
            if remaining == 0:
                connection.execute("UPDATE drops SET status='claimed' WHERE id=?", (str(drop_id),))
            self._credit(connection, drop["guild_id"], user_id, 25, "capture", str(drop_id), now, xp=10)
            return {"instance": instance, "remaining": remaining, "drop": self._drop(connection, drop_id), "credits_earned": 25, "replayed": False}

    def inventory(self, guild_id, user_id, search="", rarity="", limit=20, offset=0, *, arena=None, card_id=None):
        if rarity and rarity not in RARITIES:
            raise GameError("invalid_rarity", "Choose a catalogue rarity.")
        limit, offset = max(1,min(100,int(limit))), max(0,int(offset))
        with self._tx() as connection:
            ids = connection.execute("SELECT id,card_id FROM instances WHERE guild_id=? AND user_id=? AND consumed_at IS NULL ORDER BY acquired_at DESC,id", (str(guild_id),str(user_id))).fetchall()
            matches = [row["id"] for row in ids if (not rarity or self._card(row["card_id"])["rarity"] == rarity)
                       and (not arena or self._card(row["card_id"])["arena"]==arena)
                       and (not card_id or row["card_id"]==card_id)
                       and (self.matches_card(row["card_id"], search) or str(search).casefold() in row["id"])]
            return [self._instance(connection, key) for key in matches[offset:offset+limit]]

    def matches_card(self, card_id, search):
        query = normalize_alias(search)
        return not query or query in normalize_alias(card_id) or any(query in name for name in self.capture_names[card_id])

    def owned_cards(self, guild_id, user_id, search="", limit=25):
        with self._tx() as connection:
            ids = connection.execute("""SELECT DISTINCT card_id FROM instances
                WHERE guild_id=? AND user_id=? AND consumed_at IS NULL""", (str(guild_id), str(user_id))).fetchall()
            cards = [self._card(row["card_id"]) for row in ids]
            return sorted((card for card in cards if self.matches_card(card["id"], search)), key=lambda card: card["name"])[:max(1, min(25, int(limit)))]

    def owned_card(self, guild_id, user_id, card_id):
        with self._tx() as connection:
            row = connection.execute("""SELECT id FROM instances
                WHERE guild_id=? AND user_id=? AND card_id=? AND consumed_at IS NULL
                ORDER BY acquired_at,id LIMIT 1""", (str(guild_id), str(user_id), str(card_id))).fetchone()
            if not row:
                raise GameError("not_owned", "You can only show cards you currently own. Choose one from /vault collection.")
            return self._instance(connection, row["id"], guild_id, user_id)

    def info(self, guild_id, user_id, instance_id):
        with self._tx() as connection:
            return self._instance(connection, instance_id, guild_id, user_id)

    def complain(self, guild_id, user_id, message, now=None, request_id=None):
        now, message = timestamp(now), str(message).strip()
        if not 4 <= len(message) <= 1500:
            raise GameError("invalid_complaint", "Write 4–1,500 characters describing what went wrong.")
        with self._tx() as connection:
            if request_id:
                prior = connection.execute("SELECT * FROM complaints WHERE request_id=?", (str(request_id),)).fetchone()
                if prior:
                    if (prior["guild_id"], prior["user_id"]) != (str(guild_id), str(user_id)):
                        raise GameError("invalid_complaint", "That feedback receipt belongs to another player.")
                    return dict(prior)
            recent = connection.execute("SELECT created_at FROM complaints WHERE guild_id=? AND user_id=? AND created_at>? ORDER BY created_at DESC",
                                        (str(guild_id), str(user_id), now-86400)).fetchall()
            if recent and now-recent[0]["created_at"] < 60:
                raise GameError("complaint_cooldown", "Your feedback was saved. Wait a minute before sending another report.")
            if len(recent) >= 5:
                raise GameError("complaint_limit", "You have sent five reports in the last 24 hours. Try again tomorrow.")
            key = secrets.token_hex(8)
            connection.execute("INSERT INTO complaints(id,guild_id,user_id,message,created_at,request_id) VALUES(?,?,?,?,?,?)",
                               (key, str(guild_id), str(user_id), message, now, str(request_id) if request_id else None))
            return dict(connection.execute("SELECT * FROM complaints WHERE id=?", (key,)).fetchone())

    def complaints(self, guild_id, page=1):
        with self._tx() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM complaints WHERE guild_id=? AND resolved_at IS NULL ORDER BY created_at,id LIMIT 3 OFFSET ?",
                                                           (str(guild_id), max(0, int(page)-1)*3))]

    def resolve_complaint(self, guild_id, user_id, complaint_id, now=None):
        with self._tx() as connection:
            if not connection.execute("SELECT 1 FROM complaints WHERE guild_id=? AND id=?", (str(guild_id), str(complaint_id))).fetchone():
                raise GameError("unknown_complaint", "That report does not exist in this server.")
            connection.execute("UPDATE complaints SET resolved_at=?,resolved_by=? WHERE guild_id=? AND id=? AND resolved_at IS NULL",
                               (timestamp(now), str(user_id), str(guild_id), str(complaint_id)))

    @staticmethod
    def _market_card(card):
        return (card["category"]=="canon" and card["rarity"] in MARKET_PRICES and card["weight"]>0
                and not card.get("release") and not _is_event_exclusive(card))

    def _market_day(self,connection,guild_id,now):
        day=dt.datetime.fromtimestamp(now,dt.UTC).date().isoformat()
        if connection.execute("SELECT 1 FROM market_days WHERE guild_id=? AND day=?",(guild_id,day)).fetchone():
            return day
        connection.execute("INSERT INTO market_days(guild_id,day,created_at) VALUES(?,?,?)",(guild_id,day,now))
        slots=list(MARKET_SLOTS)
        if self.rng.random()<MARKET_EPIC_CHANCE:
            slots.append(("epic",1))
        selected=set()
        for slot,(rarity,stock) in enumerate(slots,1):
            cards=[card for card in self.catalog.values() if self._market_card(card) and card["rarity"]==rarity
                   and card.get("subject_id",card["id"]) not in selected]
            if not cards:
                continue
            card=self.rng.choices(cards,weights=[card["weight"] for card in cards],k=1)[0]
            selected.add(card.get("subject_id",card["id"]))
            connection.execute("INSERT INTO market_offers(id,guild_id,day,slot,card_id,name,rarity,price,stock) VALUES(?,?,?,?,?,?,?,?,?)",
                (secrets.token_hex(8),guild_id,day,slot,card["id"],card["name"],rarity,MARKET_PRICES[rarity],stock))
        return day

    def market(self,guild_id,user_id,now=None):
        now,guild_id,user_id=timestamp(now),str(guild_id),str(user_id)
        with self._tx() as connection:
            day=self._market_day(connection,guild_id,now)
            player=self._player(connection,guild_id,user_id)
            offers=[]
            for row in connection.execute("SELECT * FROM market_offers WHERE guild_id=? AND day=? ORDER BY slot",(guild_id,day)):
                item=dict(row)
                card=self._card(row["card_id"])
                item.update(remaining=row["stock"]-row["sold"],available=self._market_card(card) and card["rarity"]==row["rarity"],
                            arena=card["arena"],role=card["role"],faction=card["faction"])
                offers.append(item)
            purchase=connection.execute("SELECT id,receipt FROM market_purchases WHERE guild_id=? AND day=? AND user_id=?",(guild_id,day,user_id)).fetchone()
            reset=dt.datetime.combine(dt.date.fromisoformat(day)+dt.timedelta(days=1),dt.time(),dt.UTC).timestamp()
            bought=json.loads(purchase["receipt"])["instance"]["name"] if purchase else None
            return {"guild_id":guild_id,"day":day,"offers":offers,"credits":player["credits"],"purchased_today":bool(purchase),"purchased_card":bought,"resets_at":reset}

    def market_buy(self,guild_id,user_id,offer_id,now=None,*,request_id=None):
        now,guild_id,user_id,offer_id=timestamp(now),str(guild_id),str(user_id),str(offer_id)
        with self._tx() as connection:
            if request_id:
                old=connection.execute("SELECT * FROM market_purchases WHERE request_id=?",(str(request_id),)).fetchone()
                if old:
                    if (old["guild_id"],old["user_id"],old["offer_id"])!=(guild_id,user_id,offer_id):
                        raise GameError("invalid_purchase","That purchase receipt belongs to another player or offer.")
                    result=json.loads(old["receipt"])
                    result.update(replayed=True)
                    result["instance"]["historical_receipt"]=True
                    return result
            day=dt.datetime.fromtimestamp(now,dt.UTC).date().isoformat()
            old=connection.execute("SELECT * FROM market_purchases WHERE guild_id=? AND day=? AND user_id=?",(guild_id,day,user_id)).fetchone()
            if old:
                if old["offer_id"]==offer_id:
                    result=json.loads(old["receipt"])
                    result.update(replayed=True)
                    result["instance"]["historical_receipt"]=True
                    return result
                raise GameError("market_daily_limit","You already bought a card today. The market resets at 00:00 UTC.")
            offer=connection.execute("SELECT * FROM market_offers WHERE id=? AND guild_id=?",(offer_id,guild_id)).fetchone()
            if not offer or offer["day"]!=day:
                raise GameError("market_expired","That offer expired. Open /vault market for today's cards.")
            card=self._card(offer["card_id"])
            if not self._market_card(card) or card["rarity"]!=offer["rarity"]:
                raise GameError("market_unavailable","This offer is no longer available. Choose another card.")
            if offer["sold"]>=offer["stock"]:
                raise GameError("market_sold_out","That card sold out. Open /vault market to see what's left.")
            purchase_id=secrets.token_hex(8)
            self._credit(connection,guild_id,user_id,-offer["price"],"market",purchase_id,now)
            instance=self._mint(connection,guild_id,user_id,card["id"],now,"market:"+purchase_id)
            connection.execute("UPDATE market_offers SET sold=sold+1 WHERE id=?",(offer_id,))
            balance=self._player(connection,guild_id,user_id)["credits"]
            result={"instance":instance,"credits_spent":offer["price"],"credits":balance,"purchase_id":purchase_id,"replayed":False}
            connection.execute("INSERT INTO market_purchases(id,guild_id,day,user_id,offer_id,instance_id,price,created_at,request_id,receipt) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (purchase_id,guild_id,day,user_id,offer_id,instance["id"],offer["price"],now,str(request_id) if request_id else None,json.dumps(result)))
            return result

    def profile(self, guild_id, user_id, now=None):
        with self._tx() as connection:
            player = dict(self._player(connection, guild_id, user_id))
            rows = connection.execute("SELECT card_id FROM instances WHERE guild_id=? AND user_id=? AND consumed_at IS NULL", (str(guild_id),str(user_id))).fetchall()
            unique = {row["card_id"] for row in rows}
            regular = {card_id for card_id,card in self.catalog.items() if self._regular_definition(card)}
            event = set(self.catalog)-regular
            completed = len(unique & regular)
            subjects = {self.catalog[key]["subject_id"] for key in regular if isinstance(self.catalog[key].get("subject_id"),str) and self.catalog[key]["subject_id"]}
            collected_subjects = {self.catalog[key]["subject_id"] for key in unique & regular if isinstance(self.catalog[key].get("subject_id"),str) and self.catalog[key]["subject_id"]}
            player.update(total_cards=len(rows),unique_cards=completed,catalog_count=len(regular),completion_percent=round(100*completed/len(regular),1) if regular else 0,
                          regular_unique_cards=completed,regular_catalog_count=len(regular),all_unique_cards=len(unique),all_catalog_count=len(self.catalog),
                          regular_subject_count=len(subjects),regular_collected_subject_count=len(collected_subjects),
                          regular_subject_completion_percent=round(100*len(collected_subjects)/len(subjects),1) if subjects else 0,
                          regular_subject_metadata_complete=all(isinstance(self.catalog[key].get("subject_id"),str) and bool(self.catalog[key]["subject_id"]) for key in regular),
                          event_unique_cards=len(unique & event),event_catalog_count=len(event),level=1+player["xp"]//100,
                          arena_counts=dict(Counter(self._card(row["card_id"])["arena"] for row in rows)))
            return player

    def daily(self, guild_id, user_id, now=None):
        now = timestamp(now)
        day = dt.datetime.fromtimestamp(now,dt.UTC).date()
        with self._tx() as connection:
            player = self._player(connection,guild_id,user_id)
            if player["daily_day"] == day.isoformat():
                raise GameError("daily_claimed", "Today's credits were already claimed. Daily resets at 00:00 UTC.")
            yesterday = (day-dt.timedelta(days=1)).isoformat()
            streak = player["streak"]+1 if player["daily_day"] == yesterday else 1
            award = 120+min(streak-1,6)*10
            self._credit(connection,guild_id,user_id,award,"daily",day.isoformat(),now,xp=15)
            connection.execute("UPDATE players SET daily_day=?,streak=? WHERE guild_id=? AND user_id=?", (day.isoformat(),streak,str(guild_id),str(user_id)))
            balance = self._player(connection,guild_id,user_id)["credits"]
            return {"credits_earned": award,"credits": balance,"streak": streak,"next_reset": dt.datetime.combine(day+dt.timedelta(days=1),dt.time(),dt.UTC).timestamp()}

    def set_favorite(self, guild_id, user_id, instance_id, favorite):
        with self._tx() as connection:
            item = self._instance(connection,instance_id,guild_id,user_id)
            if item["locked_by"]:
                raise GameError("escrow_locked", "Cancel the trade before changing this card.")
            connection.execute("UPDATE instances SET favorite=? WHERE id=?", (int(bool(favorite)),str(instance_id)))
            return self._instance(connection,instance_id)

    def set_wishlist(self, guild_id, user_id, card_id, wanted):
        self._card(card_id)
        with self._tx() as connection:
            if wanted:
                connection.execute("INSERT OR IGNORE INTO wishlist(guild_id,user_id,card_id) VALUES(?,?,?)",(str(guild_id),str(user_id),str(card_id)))
            else:
                connection.execute("DELETE FROM wishlist WHERE guild_id=? AND user_id=? AND card_id=?",(str(guild_id),str(user_id),str(card_id)))
        return self.wishlist(guild_id,user_id)

    def wishlist(self, guild_id, user_id):
        with self._tx() as connection:
            return [self._card(row["card_id"]) for row in connection.execute("SELECT card_id FROM wishlist WHERE guild_id=? AND user_id=? ORDER BY card_id",(str(guild_id),str(user_id)))]

    def _unprotected(self, connection, guild_id, user_id, instance_ids):
        ids = [str(key) for key in instance_ids]
        if not 1 <= len(ids) <= 10 or len(ids) != len(set(ids)):
            raise GameError("invalid_selection", "Select 1–10 distinct owned card instances.")
        deck_ids = set()
        for row in connection.execute("SELECT instance_ids FROM decks WHERE guild_id=? AND user_id=?",(str(guild_id),str(user_id))):
            deck_ids.update(json.loads(row["instance_ids"]))
        for row in connection.execute("SELECT m.deck FROM raid_members m JOIN raids r ON r.id=m.raid_id WHERE r.guild_id=? AND m.user_id=? AND r.status IN ('signup','active')",(str(guild_id),str(user_id))):
            deck_ids.update(item["id"] for item in json.loads(row["deck"])["cards"])
        items = [self._instance(connection,key,guild_id,user_id) for key in ids]
        for item in items:
            if item["favorite"] or item["locked_by"] or item["id"] in deck_ids:
                raise GameError("protected_card", "Favorites, deck cards, active raid loadouts and trade escrow cannot be consumed or offered.")
        return items

    def recipes(self):
        return {"targets": {rarity: dict(recipe) for rarity, recipe in CRAFT_RECIPES.items()},
                "requirements": "Use unprotected regular canon or approved game-continuity copies of the same rarity, faction and arena. Choose a regular canon or approved game-continuity result one rarity higher in the same faction and arena. Duplicate copies of one definition are allowed. The target rarity determines the required count and credit cost. Limited, meme and event cards are unavailable in launch crafting.",
                "salvage_values": dict(self.rarity_values), "daily_base": 120}

    def craft_quote(self, target_card_id):
        """Snapshot the exact target and published recipe for a private review."""
        target = self._card(target_card_id)
        recipe = CRAFT_RECIPES.get(target["rarity"])
        if recipe is None or target["category"] not in ("canon", "legends"):
            raise GameError("invalid_recipe", "Choose a non-limited canon result from Uncommon through Mythic.")
        quote = {"target_card_id": target["id"], "target_name": target["name"],
                 "target_rarity": target["rarity"], "ingredient_rarity": RARITIES[RARITIES.index(target["rarity"])-1],
                 "faction": target["faction"], "arena": target["arena"], **recipe}
        evidence = {"quote": quote, "category": target["category"],
                    "event_id": target.get("event_id"), "release": target.get("release")}
        quote["fingerprint"] = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return quote

    def salvage(self,guild_id,user_id,instance_ids,now=None):
        now = timestamp(now)
        with self._tx() as connection:
            self._expire(connection,now)
            items = self._unprotected(connection,guild_id,user_id,instance_ids)
            selected = Counter(item["card_id"] for item in items)
            for card_id,count in selected.items():
                owned = connection.execute("SELECT COUNT(*) FROM instances WHERE guild_id=? AND user_id=? AND card_id=? AND consumed_at IS NULL",(str(guild_id),str(user_id),card_id)).fetchone()[0]
                if owned <= count:
                    raise GameError("last_copy", "Salvage preserves your final copy of every card. Select duplicates only.")
            award = sum(self.rarity_values[item["card"]["rarity"]] for item in items)
            for item in items:
                connection.execute("UPDATE instances SET consumed_at=? WHERE id=?",(now,item["id"]))
            self._credit(connection,guild_id,user_id,award,"salvage",secrets.token_hex(8),now)
            return {"consumed": [item["id"] for item in items],"credits_earned": award,"credits": self._player(connection,guild_id,user_id)["credits"]}

    def craft(self,guild_id,user_id,instance_ids,target_card_id,now=None, *, event_id=None, expected_recipe_fingerprint=None):
        now = timestamp(now)
        target = self._card(target_card_id)
        with self._tx() as connection:
            self._expire(connection,now)
            quote = self.craft_quote(target_card_id)
            if expected_recipe_fingerprint is not None and expected_recipe_fingerprint != quote["fingerprint"]:
                raise GameError("stale_recipe", "The recipe or target changed. Reopen crafting to review the current copies and credit cost.")
            items = self._unprotected(connection,guild_id,user_id,instance_ids)
            if len(items) != quote["ingredients"] or any(item["card"]["category"] not in ("canon","legends") for item in items):
                raise GameError("invalid_recipe", f"A {target['rarity'].title()} result needs exactly {quote['ingredients']} canon ingredients; limited and meme copies cannot be used.")
            event_cards = [card for card in [target,*[item["card"] for item in items]] if self._event_exclusive(card)]
            if (event_id is not None and event_id not in self.enabled_events) or any(card.get("event_id") != event_id or not self._event_allowed(card,now) for card in event_cards):
                raise GameError("event_disabled","Legends/event-exclusive crafting requires a matching explicit enabled event and active release window; regular crafting excludes it.")
            if target.get("release") and not self._event_exclusive(target):
                raise GameError("invalid_recipe","Limited seasonal editions cannot be crafted.")
            if target.get("release") and not self._eligible(connection,target,now,reserve=1):
                raise GameError("unavailable_card","That event result has no remaining edition supply.")
            first = items[0]["card"]
            if any((item["card"]["rarity"],item["card"]["faction"],item["card"]["arena"]) != (first["rarity"],first["faction"],first["arena"]) for item in items):
                raise GameError("invalid_recipe", "Ingredients must share rarity, faction and arena.")
            rank = RARITIES.index(first["rarity"])
            if rank == len(RARITIES)-1 or (target["rarity"],target["faction"],target["arena"]) != (RARITIES[rank+1],first["faction"],first["arena"]):
                raise GameError("invalid_recipe", "The result must be one rarity higher in the same faction and arena.")
            self._credit(connection,guild_id,user_id,-quote["cost"],"craft",secrets.token_hex(8),now)
            for item in items:
                connection.execute("UPDATE instances SET consumed_at=? WHERE id=?",(now,item["id"]))
            result = self._mint(connection,guild_id,user_id,target["id"],now,"craft")
            return {"instance": result,"consumed": [item["id"] for item in items],"credits_spent": quote["cost"],"credits": self._player(connection,guild_id,user_id)["credits"], "recipe_fingerprint": quote["fingerprint"]}

    def _trade_fingerprint(self,trade):
        fields = ("name","rarity","category","arena","faction","era","role","ability",
                  "ability_name","ability_description","subject_id","continuity","depiction")
        offers = [
            [owner, [
                {"instance_id":item["id"],"guild_id":item["guild_id"],"owner_id":item["user_id"],
                 "card_id":item["card_id"],"attack":item["attack"],"health":item["health"],"speed":item["speed"],
                 "card":{field:item["card"].get(field) for field in fields},
                 "reference_value":self.rarity_values[item["card"]["rarity"]]}
                for item in sorted(items,key=lambda item:item["id"])
            ]]
            for owner,items in sorted(trade["offers"].items())
        ]
        return hashlib.sha256(json.dumps(offers,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()

    def _trade(self,connection,trade_id,*,new_revision=False):
        row = connection.execute("SELECT * FROM trades WHERE id=?",(str(trade_id),)).fetchone()
        if not row:
            raise GameError("unknown_trade","That trade does not exist.")
        result = dict(row)
        result.update(user_id=row["a"],target_id=row["b"],accepted={row["a"]:bool(row["accepted_a"]),row["b"]:bool(row["accepted_b"])},offers={row["a"]:[],row["b"]:[]},values={row["a"]:0,row["b"]:0})
        if row["result"]:
            result["offers"] = json.loads(row["result"])
        else:
            for item in connection.execute("SELECT * FROM trade_items WHERE trade_id=? ORDER BY owner_id,instance_id",(str(trade_id),)):
                result["offers"][item["owner_id"]].append(self._instance(connection,item["instance_id"]))
        for owner,items in result["offers"].items():
            result["values"][owner] = sum(self.rarity_values[item["card"]["rarity"]] for item in items)
        if result["status"] == "open":
            fingerprint = self._trade_fingerprint(result)
            if fingerprint != row["review_fingerprint"]:
                # Offer edits already increment their revision; reads after a
                # catalog update or legacy migration must invalidate old UI too.
                version = result["version"] + (0 if new_revision else 1)
                connection.execute(
                    "UPDATE trades SET version=?,accepted_a=0,accepted_b=0,review_fingerprint=? WHERE id=?",
                    (version,fingerprint,str(trade_id)))
                result.update(version=version,accepted_a=0,accepted_b=0,review_fingerprint=fingerprint,
                              accepted={row["a"]:False,row["b"]:False})
        result["valuation_note"] = "Displayed values are fixed rarity salvage credits, not market prices. Thematic value is yours to judge."
        return result

    def _trade_actor(self,trade,user_id,require_open=True):
        user_id = str(user_id)
        if user_id not in (trade["a"],trade["b"]):
            raise GameError("not_participant","Only the two named traders can change or confirm this trade.")
        if require_open and trade["status"] != "open":
            raise GameError("trade_closed","This trade is completed, cancelled or expired.",persist=True)
        return user_id

    def _close_trade(self,connection,trade_id,status):
        connection.execute("UPDATE instances SET locked_by=NULL WHERE locked_by=?",(str(trade_id),))
        connection.execute("UPDATE trades SET status=? WHERE id=?",(status,str(trade_id)))
        connection.execute("DELETE FROM trade_items WHERE trade_id=?",(str(trade_id),))

    def trade_create(self,guild_id,user_id,target_id,now=None,duration=900):
        now,user_id,target_id = timestamp(now),str(user_id),str(target_id)
        if user_id == target_id:
            raise GameError("self_trade","Choose a different collector.")
        if not 1 <= duration <= 3600:
            raise GameError("invalid_duration","A trade must expire within one hour.")
        with self._tx() as connection:
            self._expire(connection,now)
            if connection.execute("SELECT 1 FROM trades WHERE guild_id=? AND status='open' AND (a IN (?,?) OR b IN (?,?))",(str(guild_id),user_id,target_id,user_id,target_id)).fetchone():
                raise GameError("active_trade","One of you already has an open trade. Finish or cancel it first.")
            trade_id = secrets.token_hex(8)
            connection.execute("INSERT INTO trades(id,guild_id,a,b,created_at,expires_at) VALUES(?,?,?,?,?,?)",(trade_id,str(guild_id),user_id,target_id,now,now+duration))
            return self._trade(connection,trade_id,new_revision=True)

    def trade_offer(self,trade_id,user_id,instance_ids,now=None):
        now = timestamp(now)
        with self._tx() as connection:
            self._expire(connection,now)
            trade = self._trade(connection,trade_id)
            user_id = self._trade_actor(trade,user_id)
            # Replace the caller's escrow atomically; failed validation restores
            # the previous offer because this entire transaction rolls back.
            connection.execute("UPDATE instances SET locked_by=NULL WHERE id IN (SELECT instance_id FROM trade_items WHERE trade_id=? AND owner_id=?)",(str(trade_id),user_id))
            connection.execute("DELETE FROM trade_items WHERE trade_id=? AND owner_id=?",(str(trade_id),user_id))
            items = self._unprotected(connection,trade["guild_id"],user_id,instance_ids) if instance_ids else []
            if len(items) > 5:
                raise GameError("invalid_selection","Offer at most five cards per side.")
            for item in items:
                connection.execute("UPDATE instances SET locked_by=? WHERE id=?",(str(trade_id),item["id"]))
                connection.execute("INSERT INTO trade_items(trade_id,instance_id,owner_id) VALUES(?,?,?)",(str(trade_id),item["id"],user_id))
            connection.execute("UPDATE trades SET version=version+1,accepted_a=0,accepted_b=0 WHERE id=?",(str(trade_id),))
            return self._trade(connection,trade_id,new_revision=True)

    def trade_accept(self,trade_id,user_id,version,now=None):
        now = timestamp(now)
        with self._tx() as connection:
            self._expire(connection,now)
            trade = self._trade(connection,trade_id)
            user_id = self._trade_actor(trade,user_id)
            if int(version) != trade["version"]:
                raise GameError("stale_trade","The offer or its card details changed. Review both sides and accept the new version.",persist=True)
            if not trade["offers"][trade["a"]] or not trade["offers"][trade["b"]]:
                raise GameError("empty_trade","Both sides must offer at least one card before accepting.")
            field = "accepted_a" if user_id == trade["a"] else "accepted_b"
            connection.execute("UPDATE trades SET "+field+"=1 WHERE id=?",(str(trade_id),))
            refreshed = self._trade(connection,trade_id)
            if all(refreshed["accepted"].values()):
                connection.execute("UPDATE trades SET result=? WHERE id=?",(json.dumps(refreshed["offers"]),str(trade_id)))
                for owner in (trade["a"],trade["b"]):
                    target = trade["b"] if owner == trade["a"] else trade["a"]
                    for item in refreshed["offers"][owner]:
                        if item["user_id"] != owner or item["locked_by"] != str(trade_id):
                            raise GameError("invalid_escrow","Escrow ownership changed; cancel and review the trade.")
                        connection.execute("UPDATE instances SET user_id=?,locked_by=NULL WHERE id=?",(target,item["id"]))
                self._close_trade(connection,trade_id,"completed")
            return self._trade(connection,trade_id)

    def trade_cancel(self,trade_id,user_id,now=None):
        with self._tx() as connection:
            self._expire(connection,timestamp(now))
            trade = self._trade(connection,trade_id)
            self._trade_actor(trade,user_id)
            self._close_trade(connection,trade_id,"cancelled")
            return self._trade(connection,trade_id)

    def get_trade(self,trade_id,now=None):
        with self._tx() as connection:
            self._expire(connection,timestamp(now))
            return self._trade(connection,trade_id)

    def active_trades(self, guild_id, user_id, now=None):
        """Recover the caller's open trade when publication was interrupted."""
        with self._tx() as connection:
            self._expire(connection, timestamp(now))
            rows = connection.execute(
                "SELECT id FROM trades WHERE guild_id=? AND status='open' AND (a=? OR b=?) ORDER BY created_at",
                (str(guild_id), str(user_id), str(user_id))).fetchall()
            return [self._trade(connection, row["id"]) for row in rows]

    def _deck(self,connection,guild_id,user_id,arena):
        if arena not in ARENAS:
            raise GameError("invalid_arena","Choose ground or fleet; they never mix.")
        row = connection.execute("SELECT instance_ids FROM decks WHERE guild_id=? AND user_id=? AND arena=?",(str(guild_id),str(user_id),arena)).fetchone()
        ids = json.loads(row["instance_ids"]) if row else []
        cards = [self._instance(connection,key,guild_id,user_id) for key in ids]
        if any(item["card"]["arena"] != arena for item in cards):
            raise GameError("stale_deck", "A card's arena changed. Use /vault deck with clear:True or select a new squad for this arena.")
        counts = Counter(item["card"]["faction"] for item in cards)
        for item in cards:
            faction = item["card"]["faction"]
            bonus = 0 if faction in NEUTRAL_FACTIONS else (0.1 if counts[faction] >= 3 else (0.05 if counts[faction] == 2 else 0))
            item.update(effective_attack=round(item["attack"]*(1+bonus)),effective_health=round(item["health"]*(1+bonus)),faction_bonus=bonus)
        return {"guild_id": str(guild_id),"user_id": str(user_id),"arena": arena,"cards": cards,
                    "attack": sum(item["effective_attack"] for item in cards),"health": sum(item["effective_health"] for item in cards),"max_cards": 3,
                    "faction_bonus_rule": "Two/three cards of an explicit faction gain 5%/10% attack and health. Independent and Wildlife are neutral and gain no faction bonus."}

    def set_deck(self,guild_id,user_id,arena,instance_ids,now=None):
        if arena not in ARENAS:
            raise GameError("invalid_arena","Choose ground or fleet; they never mix.")
        ids = [str(key) for key in instance_ids]
        if len(ids) > 3 or len(ids) != len(set(ids)):
            raise GameError("invalid_deck","A deck has at most three distinct instances.")
        with self._tx() as connection:
            self._expire(connection,timestamp(now))
            return self._save_deck(connection,guild_id,user_id,arena,ids)

    def _save_deck(self,connection,guild_id,user_id,arena,ids):
        if len(ids) > 3 or len(ids) != len(set(ids)):
            raise GameError("invalid_deck","Choose one to three different card copies.")
        items = [self._instance(connection,key,guild_id,user_id) for key in ids]
        if len({item["card_id"] for item in items}) != len(items):
            raise GameError("duplicate_card","Use distinct card types within a deck.")
        if any(item["card"]["arena"] != arena for item in items):
            raise GameError("mixed_arena","Fleet and ground cards cannot share a deck.")
        if any(item["locked_by"] for item in items):
            raise GameError("escrow_locked","Trade escrow cannot be placed in a deck.")
        connection.execute("INSERT INTO decks(guild_id,user_id,arena,instance_ids) VALUES(?,?,?,?) ON CONFLICT(guild_id,user_id,arena) DO UPDATE SET instance_ids=excluded.instance_ids",(str(guild_id),str(user_id),arena,json.dumps(ids)))
        return self._deck(connection,guild_id,user_id,arena)
    def deck_choices(self, guild_id, user_id, arena, offset=0):
        if arena not in ARENAS:
            raise GameError("invalid_arena", "Choose Ground or Fleet.")
        with self._tx() as connection:
            rows = connection.execute("SELECT id,card_id FROM instances WHERE guild_id=? AND user_id=? AND consumed_at IS NULL AND locked_by IS NULL ORDER BY card_id,acquired_at DESC,id", (str(guild_id), str(user_id))).fetchall()
            keys = [row["id"] for row in rows if self._card(row["card_id"])["arena"] == arena]
            offset = max(0,int(offset))
            return {"cards": [self._instance(connection,key) for key in keys[offset:offset+25]], "total": len(keys), "offset": offset, "arena": arena}

    def deck(self,guild_id,user_id,arena):
        with self._tx() as connection:
            return self._deck(connection,guild_id,user_id,arena)

    @staticmethod
    def combat(left,right):
        """Deterministic role combat; separate arenas are enforced by callers."""
        if left["arena"] != right["arena"]:
            raise GameError("mixed_arena","Ground and fleet cannot battle each other.")
        teams = []
        for deck in (left,right):
            teams.append([{"id": item["id"],"name": item["name"],"role": item["card"]["role"],
                               "attack": item["effective_attack"],"max_hp": item["effective_health"],"hp": item["effective_health"],
                               "speed": item["speed"],"turns": 0,"guard_used": False} for item in deck["cards"]])
        log = []
        for round_no in range(1,31):
            if not all(any(card["hp"]>0 for card in team) for team in teams):
                break
            order = [(side,card) for side,team in enumerate(teams) for card in team if card["hp"]>0]
            initiative = lambda pair,round_index=round_no:pair[1]["speed"]*(1.2 if pair[1]["role"]=="tactician" and round_index==1 else 1)
            order.sort(key=lambda pair:(-initiative(pair),pair[1]["name"],pair[0],pair[1]["id"]))
            # Equal-speed actions resolve as a volley. A mirror match cannot
            # be won simply by being the challenge initiator / left side.
            for _,group in itertools.groupby(order,key=initiative):
                pending,targets,healers = {},{},[]
                for side,actor in group:
                    opponents = [card for card in teams[1-side] if card["hp"]>0]
                    if actor["hp"] <= 0 or not opponents:
                        continue
                    actor["turns"] += 1
                    target = next((card for card in opponents if card["role"]=="guardian"),opponents[0])
                    damage,effects = actor["attack"],[]
                    if actor["role"] == "striker" and actor["turns"] % 3 == 0:
                        damage = round(damage*1.25)
                        effects.append("focused strike +25%")
                    if target["role"] == "guardian" and not target["guard_used"]:
                        damage = round(damage*0.7)
                        target["guard_used"] = True
                        effects.append("bulwark −30%")
                    key = (1-side,target["id"])
                    dealt = max(0,min(target["hp"]-pending.get(key,0),max(1,damage)))
                    pending[key] = pending.get(key,0)+dealt
                    targets[key] = target
                    log.append({"round": round_no,"side": side,"actor": actor["name"],"target": target["name"],"damage": dealt,"effects": effects})
                    if actor["role"] == "support" and actor["turns"] % 2 == 0:
                        healers.append((side,actor))
                for key,damage in pending.items():
                    targets[key]["hp"] -= damage
                for side,actor in healers:
                    if actor["hp"] <= 0:
                        continue
                    allies = [card for card in teams[side] if 0<card["hp"]<card["max_hp"]]
                    if allies:
                        ally = min(allies,key=lambda card:card["hp"]/card["max_hp"])
                        healed = min(ally["max_hp"]-ally["hp"],max(1,round(ally["max_hp"]*0.12)))
                        ally["hp"] += healed
                        log.append({"round": round_no,"side": side,"actor": actor["name"],"target": ally["name"],"healing": healed,"effects": ["field repair 12% max HP"]})
        fractions = [sum(card["hp"] for card in team)/sum(card["max_hp"] for card in team) if team else 0 for team in teams]
        winner = None if abs(fractions[0]-fractions[1])<1e-9 else (0 if fractions[0]>fractions[1] else 1)
        return {"winner_side": winner,"rounds": max((entry["round"] for entry in log),default=0),"remaining_hp": [sum(card["hp"] for card in team) for team in teams],"remaining_fraction": fractions,"log": log,
                    "rules": "Max 30 rounds; initiative by speed (tactician +20% first round). Equal-speed damage resolves simultaneously; within a volley attacks are listed by card name and target living guardians first, then deck order. First guardian hit −30%; striker every third attack +25%; surviving support every second attack heals weakest living ally 12% max HP. Timeout uses remaining HP fraction; equal fractions draw. No critical/dodge randomness."}

    def _duel(self,connection,duel_id):
        row = connection.execute("SELECT * FROM duels WHERE id=?",(str(duel_id),)).fetchone()
        if not row:
            raise GameError("unknown_duel","That challenge does not exist.")
        result = dict(row)
        result["preview"] = json.loads(row["proposal"]) if row["proposal"] else None
        result["result"] = json.loads(row["result"]) if row["result"] else None
        return result

    @staticmethod
    def _deck_fingerprint(deck):
        return (deck["arena"],tuple((item["id"],item["guild_id"],item["user_id"],item["card_id"],
                                    item["attack"],item["health"],item["speed"],item["effective_attack"],item["effective_health"],item["faction_bonus"],
                                    item["card"]["name"],item["card"]["category"],item["card"]["faction"],item["card"]["role"],item["card"].get("ability")) for item in deck["cards"]))

    def duel_create(self,guild_id,user_id,opponent_id,arena,now=None,duration=300):
        now,user_id,opponent_id = timestamp(now),str(user_id),str(opponent_id)
        if user_id == opponent_id:
            raise GameError("self_battle","Choose a different opponent.")
        if not 1 <= duration <= 3600:
            raise GameError("invalid_duration","Challenges must expire within one hour.")
        with self._tx() as connection:
            left,right = self._deck(connection,guild_id,user_id,arena),self._deck(connection,guild_id,opponent_id,arena)
            if not left["cards"] or len(left["cards"]) != len(right["cards"]):
                raise GameError("unequal_decks","Both players need the same number of cards (1–3) in this arena.")
            duel_id = secrets.token_hex(8)
            connection.execute("INSERT INTO duels(id,guild_id,a,b,arena,created_at,expires_at,proposal) VALUES(?,?,?,?,?,?,?,?)",(duel_id,str(guild_id),user_id,opponent_id,arena,now,now+duration,json.dumps([left,right])))
            return self._duel(connection,duel_id)

    def duel_accept(self,duel_id,user_id,now=None):
        now = timestamp(now)
        with self._tx() as connection:
            duel = self._duel(connection,duel_id)
            if str(user_id) != duel["b"]:
                raise GameError("not_opponent","Only the named opponent can accept this challenge.")
            if duel["status"] == "completed":
                return duel
            if duel["status"] != "pending" or now >= duel["expires_at"]:
                if duel["status"] == "pending":
                    connection.execute("UPDATE duels SET status='expired' WHERE id=?",(str(duel_id),))
                raise GameError("duel_closed","This challenge is cancelled or expired.",persist=True)
            try:
                left,right = self._deck(connection,duel["guild_id"],duel["a"],duel["arena"]),self._deck(connection,duel["guild_id"],duel["b"],duel["arena"])
            except GameError:
                raise GameError("stale_duel","A proposed card or deck changed. Review a new challenge.")
            if not duel["preview"] or any(self._deck_fingerprint(current) != self._deck_fingerprint(proposed) for current,proposed in zip((left,right),duel["preview"])):
                raise GameError("stale_duel","A proposed deck or its card stats changed. Review a new challenge.")
            if not left["cards"] or len(left["cards"]) != len(right["cards"]):
                raise GameError("unequal_decks","Both players must have equal-sized, nonempty decks.")
            result = self.combat(left,right)
            result.update(decks=[left,right],winner_id=(duel["a"] if result["winner_side"]==0 else duel["b"]) if result["winner_side"] is not None else None)
            connection.execute("UPDATE duels SET status='completed',result=? WHERE id=?",(json.dumps(result),str(duel_id)))
            if result["winner_id"]:
                loser = duel["b"] if result["winner_id"]==duel["a"] else duel["a"]
                self._player(connection,duel["guild_id"],result["winner_id"])
                self._player(connection,duel["guild_id"],loser)
                connection.execute("UPDATE players SET wins=wins+1 WHERE guild_id=? AND user_id=?",(duel["guild_id"],result["winner_id"]))
                connection.execute("UPDATE players SET losses=losses+1 WHERE guild_id=? AND user_id=?",(duel["guild_id"],loser))
            return self._duel(connection,duel_id)

    def duel_cancel(self,duel_id,user_id,now=None):
        with self._tx() as connection:
            duel = self._duel(connection,duel_id)
            if str(user_id) not in (duel["a"],duel["b"]):
                raise GameError("not_participant","Only the named players can cancel.")
            if duel["status"] != "pending":
                raise GameError("duel_closed","This challenge is already closed.")
            connection.execute("UPDATE duels SET status='cancelled' WHERE id=?",(str(duel_id),))
            return self._duel(connection,duel_id)

    def get_duel(self,duel_id):
        with self._tx() as connection:
            duel = self._duel(connection,duel_id)
            if duel["status"] == "pending" and timestamp() >= duel["expires_at"]:
                connection.execute("UPDATE duels SET status='expired' WHERE id=?",(str(duel_id),))
                return self._duel(connection,duel_id)
            return duel

    @staticmethod
    def raid_damage(deck):
        return max(1,sum(round(item["effective_attack"]*(1.1 if item["card"]["role"]=="striker" else (1.05 if item["card"]["role"]=="tactician" else 1))) for item in deck["cards"]))

    def _advance_raid(self,connection,raid_id,now):
        row = connection.execute("SELECT * FROM raids WHERE id=?",(str(raid_id),)).fetchone()
        if not row:
            raise GameError("unknown_raid","That raid does not exist.")
        if row["status"] == "signup" and now >= row["signup_ends_at"]:
            total = connection.execute("SELECT SUM(per_attack) FROM raid_members WHERE raid_id=?",(str(raid_id),)).fetchone()[0]
            if total:
                boss = json.loads(row["boss"]) if row["boss"] else None
                hp = max(1,math.ceil(total*3*0.85))
                if boss:
                    # Boss stats impose a real minimum; roster size does not erase its power.
                    hp = max(math.ceil(boss["health"] * 0.20), math.ceil(total*3*boss["effort"]))
                    strongest = connection.execute("SELECT MAX(per_attack) FROM raid_members WHERE raid_id=?", (str(raid_id),)).fetchone()[0]
                    boss["credit_pool"] = math.floor(boss["credits"] * math.sqrt(total / strongest))
                    boss["roster_potential"] = total * 3
                    connection.execute("UPDATE raids SET boss=? WHERE id=?", (json.dumps(boss), str(raid_id)))
                connection.execute("UPDATE raids SET status='active',max_hp=?,hp=? WHERE id=?",(hp,hp,str(raid_id)))
            else:
                connection.execute("UPDATE raids SET status='escaped',max_hp=0,hp=0 WHERE id=?",(str(raid_id),))
        connection.execute("UPDATE raids SET status='escaped' WHERE id=? AND status IN ('signup','active') AND expires_at<=?",(str(raid_id),now))

    def _raid(self,connection,raid_id):
        row = connection.execute("SELECT * FROM raids WHERE id=?",(str(raid_id),)).fetchone()
        if not row:
            raise GameError("unknown_raid","That raid does not exist.")
        result = dict(row)
        result["boss"] = json.loads(result["boss"]) if result["boss"] else None
        members = []
        for member in connection.execute("SELECT * FROM raid_members WHERE raid_id=? ORDER BY damage DESC,user_id",(str(raid_id),)):
            item = dict(member)
            item["deck"] = json.loads(item["deck"])
            item["combat"] = json.loads(item["combat"]) if item["combat"] else None
            members.append(item)
        result.update(participants=members,rewards=[dict(reward) for reward in connection.execute("SELECT user_id,credits,xp FROM raid_rewards WHERE raid_id=? ORDER BY user_id",(str(raid_id),))],max_attacks=3,attack_cooldown=10,max_participants=25,
                      rules="Join for 60 seconds (configured signup window), then roster locks. HP = ceil(sum roster per-attack damage × 3 × 0.85). Each collector can attack three times, 10 seconds apart. Striker raid ATK ×1.1; tactician ×1.05. Victory grants contributing collectors 100 + proportional share of 100 credits and 25 XP; no cards are awarded.")
        if result["boss"]:
            boss = result["boss"]
            result["attack_cooldown"] = 15 if boss["move"] == "ion" else 10
            result["rules"] = ("60-second signup, then the squad locks. Three attacks each. Boss retaliates after non-lethal hits. "
                               "Guardian cards reduce retaliation by 10% each (30% cap); Support cards repair 5% squad HP each "
                               "after surviving a hit (15% cap). Faster squads take 10% less retaliation. Defeated squads cannot attack. "
                               "Victory divides a bounded credit pool by damage (70%) and power-weighted participation (30%). "
                               "XP scales with your participation. Joining alone and losing earn no rewards. Owned cards are never damaged or lost.")
        return result

    def raid_bosses(self, arena=None):
        return [card for card in self.catalog.values() if card.get("raid_boss") and (arena is None or card["arena"] == arena)]

    def raid_create(self,guild_id,channel_id,arena="ground",now=None,duration=600,signup=60, *, boss_id=None):
        now = timestamp(now)
        if arena not in ARENAS:
            raise GameError("invalid_arena","Choose ground or fleet.")
        if not 1 <= signup < duration <= 3600:
            raise GameError("invalid_duration","Raid signup must end before a raid expiry of at most one hour.")
        candidates = self.raid_bosses(arena)
        boss = None
        if boss_id is not None:
            candidates = [card for card in candidates if card["id"] == boss_id]
            if not candidates:
                raise GameError("invalid_boss", "Choose a configured boss in this raid's arena.")
        if candidates:
            if boss_id is None:
                tiers = [tier for tier in RAID_TIERS if any(card["raid_boss"]["difficulty"] == tier for card in candidates)]
                tier = self.rng.choices(tiers, weights=[RAID_TIERS[key]["weight"] for key in tiers], k=1)[0]
                candidates = [card for card in candidates if card["raid_boss"]["difficulty"] == tier]
            card = self.rng.choice(candidates)
            boss = {key: card[key] for key in ("id", "name", "attack", "health", "speed", "art", "art_status", "continuity") if key in card}
            boss.update(card["raid_boss"])
            boss.update(RAID_TIERS[boss["difficulty"]])
            boss["move_description"] = RAID_MOVES[boss["move"]]
            boss["credit_pool"] = None
        with self._tx() as connection:
            for row in connection.execute("SELECT id FROM raids WHERE status IN ('signup','active')").fetchall():
                self._advance_raid(connection,row["id"],now)
            if connection.execute("SELECT 1 FROM raids WHERE guild_id=? AND channel_id=? AND status IN ('signup','active')",(str(guild_id),str(channel_id))).fetchone():
                raise GameError("active_raid","A raid is already running in this channel.")
            raid_id = secrets.token_hex(8)
            connection.execute("INSERT INTO raids(id,guild_id,channel_id,arena,created_at,signup_ends_at,expires_at,boss) VALUES(?,?,?,?,?,?,?,?)",(raid_id,str(guild_id),str(channel_id),arena,now,now+signup,now+duration,json.dumps(boss) if boss else None))
            return self._raid(connection,raid_id)

    def attach_raid_message(self,raid_id,message_id,image_url=None):
        with self._tx() as connection:
            raid = self._raid(connection,raid_id)
            if raid["boss"] and image_url:
                raid["boss"]["image_url"] = str(image_url)
                connection.execute("UPDATE raids SET boss=? WHERE id=?", (json.dumps(raid["boss"]), str(raid_id)))
            connection.execute("UPDATE raids SET message_id=? WHERE id=?",(str(message_id),str(raid_id)))

    def raid_join(self,raid_id,user_id,now=None, *, instance_ids=None):
        now,user_id = timestamp(now),str(user_id)
        with self._tx() as connection:
            self._advance_raid(connection,raid_id,now)
            raid = self._raid(connection,raid_id)
            if any(member["user_id"]==user_id for member in raid["participants"]):
                return raid
            if raid["status"] != "signup":
                raise GameError("roster_locked","The raid roster is locked. Join the next raid.",persist=True)
            if len(raid["participants"]) >= 25:
                raise GameError("raid_full","This raid already has 25 collectors.")
            if instance_ids is not None:
                self._save_deck(connection,raid["guild_id"],user_id,raid["arena"],[str(key) for key in instance_ids])
            deck = self._deck(connection,raid["guild_id"],user_id,raid["arena"])
            if not deck["cards"]:
                raise GameError("empty_deck","Build a deck in this arena before joining.")
            combat = None
            if raid["boss"]:
                health = sum(card["effective_health"] for card in deck["cards"])
                roles = Counter(card["card"]["role"] for card in deck["cards"])
                combat = {"max_hp": health, "hp": health, "damage_taken": 0, "healing": 0,
                          "mitigation": min(0.30, roles["guardian"]*0.10), "repair": min(0.15, roles["support"]*0.05),
                          "speed": sum(card["speed"] for card in deck["cards"])/len(deck["cards"]), "damage_factor": 1.0}
            connection.execute("INSERT INTO raid_members(raid_id,user_id,deck,per_attack,combat) VALUES(?,?,?,?,?)",(str(raid_id),user_id,json.dumps(deck),self.raid_damage(deck),json.dumps(combat) if combat else None))
            return self._raid(connection,raid_id)

    def raid_attack(self,raid_id,user_id,now=None):
        now,user_id = timestamp(now),str(user_id)
        with self._tx() as connection:
            self._advance_raid(connection,raid_id,now)
            raid = self._raid(connection,raid_id)
            if raid["status"] != "active":
                raise GameError("raid_not_active","The raid is signing up, defeated or expired.",persist=True)
            member = connection.execute("SELECT * FROM raid_members WHERE raid_id=? AND user_id=?",(str(raid_id),user_id)).fetchone()
            if not member:
                raise GameError("not_participant","Join during signup before attacking.")
            if member["attacks"] >= 3:
                raise GameError("attack_limit","You have used all three raid attacks.")
            cooldown = raid["attack_cooldown"]
            if member["last_attack_at"] is not None and now < member["last_attack_at"]+cooldown:
                wait = math.ceil(member["last_attack_at"]+cooldown-now)
                raise GameError("attack_cooldown",f"Your next raid attack is ready in {wait} seconds.",details={"retry_after":wait})
            combat = json.loads(member["combat"]) if member["combat"] else None
            if combat and combat["hp"] <= 0:
                raise GameError("squad_defeated", "Your raid squad is defeated. Its owned cards remain safe; other squads can continue.")
            damage = min(raid["hp"],max(1,round(member["per_attack"]*(combat["damage_factor"] if combat else 1))))
            connection.execute("UPDATE raid_members SET attacks=attacks+1,damage=damage+?,last_attack_at=? WHERE raid_id=? AND user_id=?",(damage,now,str(raid_id),user_id))
            connection.execute("UPDATE raids SET hp=hp-? WHERE id=?",(damage,str(raid_id)))
            retaliation, repaired = 0, 0
            if combat and raid["hp"] > damage:
                boss = raid["boss"]
                multiplier = boss["counter"]
                attack_number = member["attacks"] + 1
                if boss["move"] == "lightning":
                    multiplier *= 1.15
                elif boss["move"] == "superlaser" and attack_number == 2:
                    multiplier *= 2
                elif boss["move"] == "onslaught" and attack_number == 2:
                    multiplier *= 1.20
                if combat["speed"] > boss["speed"]:
                    multiplier *= 0.90
                retaliation = min(combat["hp"], max(1, round(boss["attack"]*multiplier*(1-combat["mitigation"]))))
                combat["hp"] -= retaliation
                combat["damage_taken"] += retaliation
                if combat["hp"] > 0:
                    repaired = min(combat["max_hp"]-combat["hp"], round(combat["max_hp"]*combat["repair"]))
                    combat["hp"] += repaired
                    combat["healing"] += repaired
                if boss["move"] == "choke":
                    combat["damage_factor"] = 0.90
                connection.execute("UPDATE raid_members SET combat=? WHERE raid_id=? AND user_id=?", (json.dumps(combat), str(raid_id), user_id))
            if raid["hp"] == damage:
                connection.execute("UPDATE raids SET status='defeated' WHERE id=?",(str(raid_id),))
                contributing = connection.execute("SELECT user_id,damage,per_attack,attacks FROM raid_members WHERE raid_id=? AND damage>0 ORDER BY user_id",(str(raid_id),)).fetchall()
                total = sum(row["damage"] for row in contributing)
                payouts = None
                if raid["boss"]:
                    boss = raid["boss"]
                    # Only contributing power grows the budget, not idle bodies or weak alts.
                    power = sum(row["per_attack"] for row in contributing)/max(row["per_attack"] for row in contributing)
                    pool = min(boss["credit_pool"], math.floor(boss["credits"]*math.sqrt(power)))
                    effort = {row["user_id"]: min(row["attacks"]/3, row["damage"]/(row["per_attack"]*2)) for row in contributing}
                    effort_power = sum(effort[row["user_id"]]*row["per_attack"] for row in contributing)
                    shares = {row["user_id"]: pool*(0.70*row["damage"]/total +
                              0.30*effort[row["user_id"]]*row["per_attack"]/effort_power) for row in contributing}
                    payouts = {key: math.floor(value) for key,value in shares.items()}
                    order = sorted(shares, key=lambda key: (-(shares[key]-payouts[key]),key))
                    for key in order[:pool-sum(payouts.values())]:
                        payouts[key] += 1
                for row in contributing:
                    credits = payouts[row["user_id"]] if payouts is not None else 100+round(100*row["damage"]/total)
                    xp = max(1,math.floor(raid["boss"]["xp"]*effort[row["user_id"]])) if payouts is not None else 25
                    connection.execute("INSERT INTO raid_rewards(raid_id,user_id,credits,xp) VALUES(?,?,?,?)",(str(raid_id),row["user_id"],credits,xp))
                    self._credit(connection,raid["guild_id"],row["user_id"],credits,"raid",str(raid_id),now,xp=xp)
            elif combat:
                exhausted = all(item["combat"]["hp"] <= 0 or item["attacks"] >= 3
                                for item in self._raid(connection,raid_id)["participants"])
                if exhausted:
                    connection.execute("UPDATE raids SET status='failed' WHERE id=?", (str(raid_id),))
            result = self._raid(connection,raid_id)
            result["damage_dealt"] = damage
            result.update(retaliation_damage=retaliation, repaired_hp=repaired)
            return result

    def get_raid(self,raid_id,now=None):
        with self._tx() as connection:
            self._advance_raid(connection,raid_id,timestamp(now))
            return self._raid(connection,raid_id)

    def active_raids(self,guild_id=None,now=None):
        now = timestamp(now)
        with self._tx() as connection:
            for row in connection.execute("SELECT id FROM raids WHERE status IN ('signup','active')").fetchall():
                self._advance_raid(connection,row["id"],now)
            query,args = "SELECT id FROM raids WHERE status IN ('signup','active')",[]
            if guild_id is not None:
                query += " AND guild_id=?"
                args.append(str(guild_id))
            return [self._raid(connection,row["id"]) for row in connection.execute(query,args)]

    def recent_raids(self,guild_id,limit=25,now=None):
        now = timestamp(now)
        with self._tx() as connection:
            rows = connection.execute("SELECT id FROM raids WHERE guild_id=? ORDER BY created_at DESC,id DESC LIMIT ?",(str(guild_id),max(1,min(200,int(limit))))).fetchall()
            for row in rows:
                self._advance_raid(connection,row["id"],now)
            return [self._raid(connection,row["id"]) for row in rows]

    def health(self):
        with self._tx() as connection:
            regular = [card for card in self.catalog.values() if self._regular_definition(card)]
            subjects = {card["subject_id"] for card in regular if isinstance(card.get("subject_id"),str) and card["subject_id"]}
            return {"schema_version": connection.execute("PRAGMA user_version").fetchone()[0],"journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
                        "synchronous": connection.execute("PRAGMA synchronous").fetchone()[0],
                        "foreign_keys": bool(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
                        "foreign_key_violations": len(connection.execute("PRAGMA foreign_key_check").fetchall()),
                        "integrity": connection.execute("PRAGMA quick_check").fetchone()[0],"catalog_cards": len(self.catalog),
                        "regular_catalog_cards": len(regular),"event_catalog_cards": len(self.catalog)-len(regular),"regular_subject_count": len(subjects),
                        "regular_subject_metadata_complete": all(isinstance(card.get("subject_id"),str) and bool(card["subject_id"]) for card in regular),
                        "active_instances": connection.execute("SELECT COUNT(*) FROM instances WHERE consumed_at IS NULL").fetchone()[0],
                        "total_claims": connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0]}

    def backup(self,destination):
        destination = str(destination)
        if Path(destination).resolve() == Path(self.db_path).resolve():
            raise GameError("invalid_backup","Backup destination must differ from the active database.")
        Path(destination).parent.mkdir(parents=True,exist_ok=True)
        with contextlib.closing(self._connect()) as source,contextlib.closing(sqlite3.connect(destination)) as target:
            source.backup(target)
        return {"path": destination,"bytes": Path(destination).stat().st_size}
