# Card catalog, rarity odds and balance

The public [`data/cards.json`](../data/cards.json) is the source of card definitions. It includes 871 definitions: 835 regular treatments covering 818 distinct non-event subjects, 16 seasonal editions and 20 staff-only Celestial cards. A treatment is a specific depiction or variant; a subject is the underlying collection identity. Several treatments of the same character or vehicle do not create extra distinct subjects.

## Continuity and provenance

Definitions record names, accepted aliases, faction, era, arena, role, ability, source references and continuity. Regular collection includes canon plus explicitly selected alternate/game continuity, including LEGO alternate stories, 2003 Clone Wars, Knights of the Old Republic, Knights of the Old Republic II and The Old Republic. The games' published Legends provenance remains visible. Their inclusion is a collection-design decision, not a canon reclassification.

Humorous treatments use established scenes or alternate stories. Event-exclusive material uses deliberate eligibility rules. Content references explain provenance; they do not grant permission to redistribute source illustrations. No third-party subject art is bundled in Git; `setup-art` downloads the recorded sources locally. See [artwork setup](ASSETS.md).

## Rarity probabilities

The included catalog uses spawn schema 3, rate revision 3:

| Rarity | Default tier probability | Ordinary collector limit |
| --- | ---: | ---: |
| Common | 54% | 2 |
| Uncommon | 22% | 2 |
| Rare | 17.8% | 2 |
| Epic | 4.2% | 2 |
| Legendary | 1.6% | 1 |
| Mythic | 0.4% | 1 |
| Celestial | Never drops (staff-granted) | 1 |

The engine chooses a rarity first, then a release lane, then a card using relative weights inside that lane. When both ordinary and activated limited cards are eligible in a tier, limited editions receive 25% of that tier's probability. If only one lane is eligible it receives the entire tier. Empty tiers are removed and the remaining probabilities renormalize.

Adding card definitions does not increase their rarity tier's aggregate probability. Actual draw eligibility also depends on release windows, enabled events, remaining supply and active reservations. The public catalog is not a view of live reserved stock. Administrator equal-chance events use their explicitly different selection rule.

The engine preserves historical rate revisions for compatible older catalogs and rejects unknown revisions. Do not relabel a catalog revision without understanding its probability contract.

## Notability weighting

Every regular card has a `notability` score from 1 (obscure) to 5 (iconic). Inside a rarity tier, a card's weight is the tier base weight × a notability multiplier (5: 3.0, 4: 2.0, 3: 1.25, 2: 0.7, 1: 0.35), so iconic subjects drop about 8.6× as often as obscure ones in the same tier. Tier probabilities are unchanged. The policy is recorded in `spawn_model.card_weight_policy`.

The offline scarcity audit compares like with like: within each notability band, every card of a higher tier must be rarer than every card of the tier below in all seasonal lane states, and each tier's average card must be rarer than the tier below. Catalogs without notability scores keep the original strict per-card check.

## Celestial (staff-only)

Celestial sits above Mythic: 20 cards at 7.0× rarity power (combat 5). They use category `staff` and weight 0, so they never drop, appear in events, sell in the market or serve as crafting results. They're obtainable only through `/vault-admin give` and stay with the recipient: they can't be traded, gifted or recycled. They are excluded from regular completion counts.

## Combat stats

`combat-power-v4` uses a shared numeric scale. A combat capability rating of 1–5 maps to points 100/140/200/290/420. Role baselines and rarity multipliers determine attack and health using half-up rounding. Capability profiles consider the depicted phase and fighting abilities; popularity, narrative importance and physical size are not extra multipliers.

Roles retain distinct baselines and abilities. Rarity increases base stats, but does not establish an absolute matchup ranking across every role and ability. New individual copies independently roll attack and health between 95% and 105% of baseline. Limited editions add 10% attack and health after matching their intended depiction baseline and copy rolls. There is no limited-edition speed bonus.

Provisional profiles must remain visibly provisional. Launch readiness requires reviewed profiles for required cards. If you change card stats or capability profiles, change the catalog and relevant behavioral tests together; the engine checks consistency with the declared formula.

## Seasonal releases and lifetime supply

Seasonal and exclusive-event keys are disabled on fresh installations. Explicitly enable only the releases you have reviewed in `VAULT_ENABLED_RELEASES` or `VAULT_ENABLED_EVENTS`. Read exact UTC boundaries and caps from each card's `release` object; the included editions are dated, not forever-recurring seasons.

Lifetime stock does not reset with restarts or new calendar years. Missed downtime does not generate catch-up cards. Calendar eligibility is evaluated by the engine; enabled upcoming releases must pass required artwork review before startup. An enabled key alone does not make an out-of-window or exhausted edition eligible.

## Changing the catalog

Maintain unique card IDs, shared subject identities where appropriate, accurate aliases, valid roles/arenas, explicit continuity and tested stats. The shipped startup policy requires at least 800 distinct regular subjects. It is designed for this full collection, not as a configuration-only small-deck framework.

Run the offline suite and readiness report after edits:

```bash
uv run pytest
uv run python -m venator_vault check
```

Run `check --require-art` when preparing a complete reviewed deployment. Scarcity, presentation, artwork, balance and durable storage are separate readiness gates.
