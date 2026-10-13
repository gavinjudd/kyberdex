"""Behavioral scarcity, seasonal allocation and launch-gate regressions."""
import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from venator_vault import __main__ as ops
from venator_vault.config import Settings
from venator_vault.core import (
    LEGACY_SPAWN_WEIGHTS,
    RARITY_SPAWN_WEIGHTS,
    SPAWN_WEIGHTS_V3_REVISION_1,
    SPAWN_WEIGHTS_V3_REVISION_2,
    Game,
)
from venator_vault.economy import audit_economy, cadence_forecast

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 15, tzinfo=UTC).timestamp()


@pytest.fixture
def document():
    return json.loads((ROOT / "data/cards.json").read_text())


def build(tmp_path, document, **kwargs):
    path = tmp_path / "cards.json"
    path.write_text(json.dumps(document))
    return Game(tmp_path / "game.sqlite", path, **kwargs)


def test_production_scarcity_holds_across_lane_exhaustion_states(document):
    report = audit_economy(document["cards"])
    assert report["ready"]
    assert sum(r["definitions"] for r in report["tiers"].values()) == 835
    assert min(row["ratio"] for row in report["adjacent_scarcity_checks"]) >= 1.12
    assert report["adjacent_scarcity_checks"][-1]["ratio"] == pytest.approx(33/29)
    # Five additional Legendary definitions exceed the remaining margin.
    extra = copy.deepcopy(next(c for c in document["cards"] if c["rarity"] == "legendary" and not c.get("release")))
    additions = [dict(extra, id=f"additional-reviewed-legendary-{index}") for index in range(5)]
    report = audit_economy(document["cards"] + additions)
    assert not report["ready"]
    assert "ordinary_scarcity_inversion:legendary:mythic" in report["violations"]


def test_old_odds_fail_even_without_seasons(document):
    regular = [c for c in document["cards"] if c["category"] != "limited"]
    report = audit_economy(regular, tier_weights=LEGACY_SPAWN_WEIGHTS, limited_share_percent=0)
    assert not report["ready"]
    assert "ordinary_scarcity_inversion:rare:epic" in report["violations"]


def test_activation_preserves_tier_mass_and_reserved_supply_returns_lane_budget(tmp_path, document):
    game = build(tmp_path, document, enabled_releases=("autumn-2026",))
    pool = {r["id"]: r for r in game.spawn_pool(now=NOW)}
    assert pool["rex-umbara"]["probability"] == pytest.approx(.178*.25)
    assert pool["vader-mustafar"]["probability"] == pytest.approx(.004*.25)
    for rarity, mass in game.spawn_weights.items():
        assert sum(r["probability"] for r in pool.values()
                   if game.catalog[r["id"]]["rarity"] == rarity) == pytest.approx(mass/10000)
    # Reserve the entire finite stock without minting; all Rare mass returns to
    # normal definitions. Expiry returns stock and reinstates the seasonal lane.
    for i in range(60):
        game.create_drop("guild", str(i), "rex-umbara", now=NOW, duration=10)
    reserved = {r["id"]: r for r in game.spawn_pool(now=NOW)}
    assert "rex-umbara" not in reserved
    regular_rare = next(c["id"] for c in game.catalog.values() if c["rarity"] == "rare" and not c.get("release"))
    assert reserved[regular_rare]["probability"] == pytest.approx(pool[regular_rare]["probability"]/.75)
    recovered = {r["id"]: r for r in game.spawn_pool(now=NOW+10)}
    assert recovered["rex-umbara"]["probability"] == pytest.approx(pool["rex-umbara"]["probability"])


def test_disabled_release_never_enters_sampler_and_enabled_sampler_matches_report(tmp_path, document):
    game = build(tmp_path, document)
    assert len(game.spawn_pool(now=NOW)) == 835
    game = Game(tmp_path / "game.sqlite", tmp_path / "cards.json", enabled_releases=("autumn-2026",))
    published = {r["id"]: r["probability"] for r in game.spawn_pool(now=NOW)}

    class Recorder:
        def choices(self, population, weights, k):
            assert dict(zip((c["id"] for c in population), weights)) == pytest.approx(published)
            return [next(c for c in population if c["id"] == "venator-coruscant")]

    game.rng = Recorder()
    assert game.create_drop("guild", "channel", now=NOW)["card_id"] == "venator-coruscant"


def test_legacy_schema2_retains_old_weights_and_flat_distribution(tmp_path, document):
    document["spawn_model"] = {"schema_version": 2, "formula": "tier-first-v2"}
    for rarity, weight in LEGACY_SPAWN_WEIGHTS.items():
        document["rarities"][rarity]["spawn_weight"] = weight
    game = build(tmp_path, document, enabled_releases=("autumn-2026",))
    pool = {r["id"]: r for r in game.spawn_pool(now=NOW)}
    eligible_rare_count = sum(game.catalog[key]["rarity"] == "rare" for key in pool)
    assert pool["rex-umbara"]["probability"] == pytest.approx(.10/eligible_rare_count)


@pytest.mark.parametrize("explicit_revision", [False, True])
def test_saved_schema3_catalog_reopens_with_original_rates_and_lane_split(tmp_path, document, explicit_revision):
    document["spawn_model"].pop("rate_revision", None)
    if explicit_revision:
        document["spawn_model"]["rate_revision"] = 1
    for rarity, weight in SPAWN_WEIGHTS_V3_REVISION_1.items():
        document["rarities"][rarity]["spawn_weight"] = weight
    game = build(tmp_path, document, enabled_releases=("autumn-2026",))
    pending = game.create_drop("guild", "channel", "venator-coruscant", now=NOW)
    reopened = Game(tmp_path/"game.sqlite", tmp_path/"cards.json", enabled_releases=("autumn-2026",))
    assert reopened.spawn_weights == SPAWN_WEIGHTS_V3_REVISION_1
    assert reopened.get_drop(pending["id"])["collector_limit"] == 1
    pool = reopened.spawn_pool(now=NOW)
    for rarity, weight in SPAWN_WEIGHTS_V3_REVISION_1.items():
        rows = [row for row in pool if reopened.catalog[row["id"]]["rarity"] == rarity]
        assert sum(row["probability"] for row in rows) == pytest.approx(weight/10000)
    assert next(row for row in pool if row["id"] == "venator-coruscant")["probability"] == pytest.approx(.011*.25)
    ordinary_legendary = sum(row["probability"] for row in pool
                             if reopened.catalog[row["id"]]["rarity"] == "legendary"
                             and reopened.catalog[row["id"]]["category"] != "limited")
    assert ordinary_legendary == pytest.approx(.011*.75)


def test_current_catalog_explicitly_selects_rate_revision_three(tmp_path, document):
    assert document["spawn_model"]["rate_revision"] == 3
    game = build(tmp_path, document)
    assert game.spawn_weights == RARITY_SPAWN_WEIGHTS
    assert game.spawn_weights == dict(zip(("common", "uncommon", "rare", "epic", "legendary", "mythic"),
                                        (5400, 2200, 1780, 420, 160, 40)))


@pytest.mark.parametrize("revision,weights", [
    (2, SPAWN_WEIGHTS_V3_REVISION_2), (3, RARITY_SPAWN_WEIGHTS),
])
def test_saved_explicit_rates_reopen_with_their_own_tier_mass(tmp_path, document, revision, weights):
    document["spawn_model"]["rate_revision"] = revision
    for rarity, weight in weights.items():
        document["rarities"][rarity]["spawn_weight"] = weight
    game = build(tmp_path, document, enabled_releases=("autumn-2026",))
    pending = game.create_drop("guild", "channel", "venator-coruscant", now=NOW)
    reopened = Game(tmp_path / "game.sqlite", tmp_path / "cards.json", enabled_releases=("autumn-2026",))
    assert reopened.spawn_weights == weights
    assert reopened.get_drop(pending["id"])["collector_limit"] == 1
    pool = reopened.spawn_pool(now=NOW)
    for rarity, weight in weights.items():
        assert sum(row["probability"] for row in pool
                   if reopened.catalog[row["id"]]["rarity"] == rarity) == pytest.approx(weight/10000)
    assert next(row for row in pool if row["id"] == "venator-coruscant")["probability"] == pytest.approx(
        weights["legendary"]/10000*.25)


@pytest.mark.parametrize("revision", [0, 4, 99, True, False, 2.0, "2", None])
def test_unknown_or_malformed_rate_revision_is_rejected(tmp_path, document, revision):
    document["spawn_model"]["rate_revision"] = revision
    with pytest.raises(ValueError, match="spawn rate revision"):
        build(tmp_path, document)


def test_activity_cadence_forecast_keeps_tier_odds_but_bounds_faster_issuance(document):
    report = audit_economy(document["cards"])
    cadence = report["cadence"]
    assert cadence["base_delay_seconds"] == [720, 1200]
    assert cadence["minimum_delay_seconds"] == 660
    assert cadence["mean_active_no_credit_delay_seconds"] == 960
    assert cadence["mean_full_credit_delay_seconds"] == 900
    assert cadence["activity_required"] and cadence["activity_min_users"] == 1
    assert cadence["activity_window_seconds"] == 300
    assert cadence["idle_encounters_per_day"] == 0
    assert cadence["active_no_credit_encounters_per_day"] == 90
    assert cadence["full_credit_encounters_per_day"] == 96
    assert cadence["absolute_pacing_bound_per_day"] == pytest.approx(86400/660)
    assert report["tiers"]["mythic"]["idle_expected_copies_per_day"] == 0
    assert report["tiers"]["mythic"]["active_no_credit_expected_copies_per_day"] == pytest.approx(.36)
    assert report["tiers"]["mythic"]["max_expected_copies_per_day"] == pytest.approx(.384)
    assert report["tiers"]["mythic"]["absolute_pacing_copy_bound_per_day"] == pytest.approx(86400/660*.004)


@pytest.mark.parametrize("interval,minimum", [(300, 480), (900, 900), (86400, 900)])
def test_cadence_forecast_handles_minimum_clamping_exactly(interval, minimum):
    report = cadence_forecast(interval, minimum_delay=minimum)
    low, high = report["base_delay_seconds"]
    samples = [max(480, minimum, delay-60) for delay in range(low, high+1)]
    assert report["mean_full_credit_delay_seconds"] == pytest.approx(sum(samples)/len(samples))
    assert report["mean_full_credit_delay_seconds"] <= report["mean_active_no_credit_delay_seconds"]


@pytest.mark.parametrize("share", [0, 26, 25.0, True, None])
def test_spawn_policy_rejects_unpublished_seasonal_share(tmp_path, document, share):
    document["spawn_model"]["limited_share_percent"] = share
    with pytest.raises(ValueError, match="Limited allocation"):
        build(tmp_path, document)


def test_schema3_cannot_omit_release_keys_by_removing_top_level_policy(tmp_path, document):
    document.pop("release_policy")
    next(c for c in document["cards"] if c["category"] == "limited").pop("release_id")
    with pytest.raises(ValueError, match="release key"):
        build(tmp_path, document)


def test_upcoming_enabled_release_art_is_required_before_calendar_opens(document):
    card = next(c for c in document["cards"] if c["id"] == "grogu-nevarro")
    now = datetime.fromtimestamp(NOW, UTC)
    assert not ops.art_required(card, (), now)
    assert ops.art_required(card, (), now, enabled_releases=("winter-2026",))
    assert not ops.art_required(card, (), datetime(2028, 1, 1, tzinfo=UTC), enabled_releases=("winter-2026",))


def test_catalog_with_synthetic_art_passes_profile_artwork_and_economy_gates(tmp_path, synthetic_asset_root):
    settings = Settings(synthetic_asset_root, tmp_path / "check.sqlite", synthetic_asset_root / "data/cards.json", None, 42)
    report = ops.readiness(settings)
    assert report["launch_catalog_ready"] and report["launch_economy_ready"]
    provisional = sum(card["power_profile"]["basis"] == "default_design"
                      for card in json.loads(settings.catalog.read_text())["cards"])
    assert report["launch_balance_ready"] is (provisional == 0)
    assert report["provisional_profile_count"] == provisional
    assert report["launch_art_ready"]
    assert report["missing_art_count"] == 0


@pytest.mark.parametrize("gate", ["launch_balance_ready", "launch_economy_ready", "launch_storage_ready"])
@pytest.mark.parametrize("command", [["run"], ["check", "--require-art"]])
def test_new_gates_refuse_launch_before_any_gateway(tmp_path, monkeypatch, gate, command):
    settings = Settings(ROOT, tmp_path / "check.sqlite", ROOT / "data/cards.json", None, 42)
    gates = dict.fromkeys(("launch_balance_ready", "launch_economy_ready", "launch_catalog_ready",
                          "launch_art_ready", "launch_presentation_ready", "launch_storage_ready"), True)
    gates[gate] = False
    monkeypatch.setattr(ops.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(ops, "readiness", lambda *a, **kw: gates)
    assert ops.main(command) == 1


def test_public_checkout_without_art_fails_launch_readiness(tmp_path):
    settings = Settings(ROOT, tmp_path / "public-check.sqlite", ROOT / "data/cards.json", None, 42)
    report = ops.readiness(settings)
    assert not report["launch_art_ready"]
    assert report["missing_art_count"] > 0
    assert not (ROOT / "assets/art/manifest.json").exists()
