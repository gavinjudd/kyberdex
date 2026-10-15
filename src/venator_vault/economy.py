"""Offline direct-drop scarcity audit; no player state and no random simulation."""
from collections import Counter
from itertools import pairwise

from .core import COLLECTOR_LIMITS, LIMITED_SHARE_PERCENT, RARITIES, RARITY_SPAWN_WEIGHTS, _is_event_exclusive
from .runtime import ACTIVITY_MIN_USERS, ACTIVITY_WINDOW, MAX_ACTIVITY_DISCOUNT, MINIMUM_SPAWN_DELAY, spawn_bounds


def cadence_forecast(interval=900, *, minimum_delay=MINIMUM_SPAWN_DELAY):
    """Long-run opportunity bounds for uniform cycles, before scheduling delays."""
    low, high = spawn_bounds(interval, minimum_delay)
    floor = max(MINIMUM_SPAWN_DELAY, int(minimum_delay))
    # Exact mean across inclusive integer draws, including the minimum clamp.
    count = high-low+1
    clipped = max(0, min(high, floor+MAX_ACTIVITY_DISCOUNT)-low+1)
    first_unclipped = low+clipped
    rest = count-clipped
    full_credit_mean = (clipped*floor + rest*(first_unclipped+high-2*MAX_ACTIVITY_DISCOUNT)/2)/count
    active_mean = (low+high)/2
    return {
        "nominal_interval_seconds": interval, "base_delay_seconds": [low, high],
        "maximum_activity_discount_seconds": MAX_ACTIVITY_DISCOUNT,
        "minimum_delay_seconds": max(floor, low-MAX_ACTIVITY_DISCOUNT),
        "activity_required": True, "activity_min_users": ACTIVITY_MIN_USERS, "activity_window_seconds": ACTIVITY_WINDOW,
        "mean_active_no_credit_delay_seconds": active_mean, "mean_full_credit_delay_seconds": full_credit_mean,
        "idle_encounters_per_day": 0, "active_no_credit_encounters_per_day": 86400/active_mean,
        "full_credit_encounters_per_day": 86400/full_credit_mean,
        "absolute_pacing_bound_per_day": 86400/max(floor, low-MAX_ACTIVITY_DISCOUNT),
        "scope": "Conditional on recent qualifying chat before every deadline; quiet periods produce no automatic cards. Scheduler and delivery delays excluded. Absolute pacing bound is not a sustained expected rate.",
    }


def audit_economy(cards, *, tier_weights=None, limited_share_percent=LIMITED_SHARE_PERCENT, interval=900,
                  minimum_delay=MINIMUM_SPAWN_DELAY):
    """Bound each ordinary definition's maximum minted copies per opportunity.

    Compare the least frequent lower-tier card under seasonal dilution with the
    most frequent next-tier card without dilution. Independent lane exhaustion
    can produce either extreme. Editions and crafting are reported separately;
    this criterion is deliberately about ordinary fully claimed encounter supply.
    """
    cards = list(cards)
    cadence = cadence_forecast(interval, minimum_delay=minimum_delay)
    tier_weights = RARITY_SPAWN_WEIGHTS if tier_weights is None else tier_weights
    regular = [c for c in cards if c.get("category") != "limited" and not c.get("release")
               and not _is_event_exclusive(c) and c.get("weight", 0) > 0]
    seasonal_tiers = {c.get("rarity") for c in cards if c.get("category") == "limited"
                      and c.get("weight", 0) > 0 and not _is_event_exclusive(c)}
    counts = Counter(c.get("rarity") for c in regular)
    totals = Counter()
    for card in regular:
        totals[card["rarity"]] += card["weight"]
    mass = sum(tier_weights[r] for r in RARITIES if counts[r])
    violations, tiers = [], {}
    for rarity in RARITIES:
        if not counts[rarity]:
            violations.append(f"missing_regular_tier:{rarity}")
            continue
        probability = tier_weights[rarity] / mass
        mint_rates = [probability * c["weight"] / totals[rarity] * COLLECTOR_LIMITS[rarity]
                      for c in regular if c["rarity"] == rarity]
        lane_min = 1-limited_share_percent/100 if rarity in seasonal_tiers else 1
        tiers[rarity] = {
            "definitions": counts[rarity], "encounter_probability": probability,
            "collectors": COLLECTOR_LIMITS[rarity],
            "mean_encounters_to_see_tier": 1/probability,
            "idle_expected_copies_per_day": cadence["idle_encounters_per_day"] * probability * COLLECTOR_LIMITS[rarity],
            "active_no_credit_expected_copies_per_day": cadence["active_no_credit_encounters_per_day"] * probability * COLLECTOR_LIMITS[rarity],
            "max_expected_copies_per_day": cadence["full_credit_encounters_per_day"] * probability * COLLECTOR_LIMITS[rarity],
            "absolute_pacing_copy_bound_per_day": cadence["absolute_pacing_bound_per_day"] * probability * COLLECTOR_LIMITS[rarity],
            "ordinary_per_card_mint_rate_min": min(mint_rates) * lane_min,
            "ordinary_per_card_mint_rate_max": max(mint_rates),
        }
    banded = bool(regular) and all(isinstance(c.get("notability"), int) and 1 <= c["notability"] <= 5 for c in regular)
    rates = {}
    for card in regular:
        rarity = card["rarity"]
        rates[card["id"]] = (tier_weights[rarity] / mass * card["weight"] / totals[rarity] * COLLECTOR_LIMITS[rarity])
    comparisons = []
    for lower, higher in pairwise(RARITIES):
        if lower not in tiers or higher not in tiers:
            continue
        lane_min = 1-limited_share_percent/100 if lower in seasonal_tiers else 1
        if banded:
            # notability-v1: like-for-like scarcity. Within each notability band a
            # higher tier is strictly rarer per card, in every seasonal lane state,
            # and each tier's average card is rarer than the tier below.
            ratios = []
            for band in range(1, 6):
                low = [rates[c["id"]] * lane_min for c in regular if c["rarity"] == lower and c["notability"] == band]
                high = [rates[c["id"]] for c in regular if c["rarity"] == higher and c["notability"] == band]
                if low and high:
                    ratios.append(min(low) / max(high))
            mean = lambda rarity: sum(rates[c["id"]] for c in regular if c["rarity"] == rarity) / counts[rarity]
            ratio = min(ratios + [mean(lower) / mean(higher)])
            strict = tiers[lower]["ordinary_per_card_mint_rate_min"] / tiers[higher]["ordinary_per_card_mint_rate_max"]
            passed = ratio + 1e-12 >= 1
            comparisons.append({"lower": lower, "higher": higher, "ratio": ratio, "passed": passed,
                                "basis": "notability-band", "cross_band_ratio": strict})
        else:
            floor = tiers[lower]["ordinary_per_card_mint_rate_min"]
            ceiling = tiers[higher]["ordinary_per_card_mint_rate_max"]
            ratio = floor/ceiling
            passed = floor+1e-12 >= ceiling
            comparisons.append({"lower": lower, "higher": higher, "ratio": ratio, "passed": passed})
        if not passed:
            violations.append(f"ordinary_scarcity_inversion:{lower}:{higher}")
    return {
        "ready": not violations, "violations": violations,
        "scope": "ordinary maximum direct-drop copies; all seasonal lane/exhaustion states",
        "assumptions": "one configured channel; qualifying recent chat each cycle; all collector slots filled; no downtime; excludes crafting and trades",
        "interval_seconds": interval, "encounters_per_day": cadence["active_no_credit_encounters_per_day"],
        "cadence": cadence,
        "cadence_basis": "randomized configured cycle requiring recent chat; bounded activity acceleration; saved legacy cycle may retain its existing deadline",
        "limited_share_percent": limited_share_percent,
        "card_weight_basis": "notability-v1" if banded else "flat",
        "tiers": tiers, "adjacent_scarcity_checks": comparisons,
    }
