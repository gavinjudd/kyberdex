# Gameplay and Discord commands

KyberDex is a server-local collection game. Players identify encounters, build a collection, trade and craft with explicit confirmation, then use separate Ground and Fleet squads in duels and raids. Credits are earned in game; the bot has no real-money purchases, paid random draws or asset wagers.

## Encounters and captures

The default automatic encounter window is randomized between 12 and 20 minutes. It is hidden from players. A due timer also needs fresh activity from an eligible human in an approved public channel within the last five minutes and after the previous publication. Quiet servers receive no automatic cards. Eligible messages reduce the sampled wait by five seconds each, up to one minute per cycle, with per-member limits and cooldowns. The schedule and credited contributions survive restarts.

Press **Capture** and identify the artwork by name or an accepted alias. Capitalization, spacing and punctuation are normalized. Distinctive names can work; ambiguous surnames and generic titles do not. Hints unlock after 35 seconds; wrong guesses trigger a persisted cooldown.

| Rarity | Distinct collectors per ordinary encounter |
| --- | --- |
| Common, Uncommon, Rare, Epic | 2 |
| Legendary, Mythic | 1 |

Each collector can claim one copy per encounter. After two consecutive successful direct captures across the guild, that player must let someone else capture before claiming again. Another collector taking the second slot also breaks the streak. Failed guesses, restarts, trades and crafting do not reset it. Explicit event overrides may set a different published capacity.

Captures are committed transactionally. Replayed interactions do not grant duplicate copies. New copies independently roll attack and health from 95% to 105% of their baseline, so either stat can be below baseline. Limited editions receive their 10% edition bonus afterward. The resulting copy stats persist.

## Browse and collect

| Command | Behavior |
| --- | --- |
| `/vault help` | In-Discord guide to the main rules and actions. |
| `/vault status` | Encounter availability and capture limits. |
| `/vault catalog` | Search all definitions, including unowned cards. |
| `/vault collection` | Browse copies you currently own. |
| `/vault card` | Publicly display an owned card and its copy stats. |
| `/vault favorite` | Protect or unprotect a copy from consumption. |
| `/vault wishlist` | Track a desired card or inspect your wishlist. |
| `/vault profile` | Collection progress, balance and battle record. |

The public encounter shows collectors and remaining slots. The successful capture receipt is private; `/vault card` makes sharing a deliberate choice.

## Credits, market, crafting and trades

| Command | Behavior |
| --- | --- |
| `/vault daily` | Claim free daily credits. |
| `/vault market` | Review the daily market card before buying with credits. |
| `/vault recipes` | Inspect exact crafting costs and recycle values. |
| `/vault craft` | Review the selected result, exact consumed copies and cost. |
| `/vault salvage` | Review duplicates and credit return before recycling. |
| `/vault trade` | Open or inspect a trade table with escrow. |

Favorites, escrow and active loadouts are checked again when a consuming operation executes. Changing a trade invalidates both parties' confirmations; both must accept the same offer revision. The ownership exchange is atomic. Published rarity reference values are game reference values, not a real-world price.

## Ground and Fleet battles

Use `/vault deck` to view or save up to three distinct card types in each arena. `/vault duel` challenges another collector with the selected squad. Ground units and fleets do not fight across arenas. Role abilities, persistent copy stats and faction composition influence outcomes. Two allied faction cards grant a 5% faction bonus; three grant 10%. Independent and Wildlife classifications provide no faction bonus.

Use `/vault raid` to inspect a cooperative encounter, choose a saved squad, join and attack. The catalog includes named bosses and scaled encounters. Victory awards credits and XP; it does not mint cards outside encounter supply rules. Duels and raids do not wager players' cards or credits.

## Server management

These commands require **Manage Server**:

| Command | Purpose |
| --- | --- |
| `/vault-admin setup` | Select the drop channel and nominal scheduling interval. |
| `/vault-admin pause` | Pause or resume automatic encounters. |
| `/vault-admin spawn` | Force an immediate encounter, optionally selecting a card or rarity. |
| `/vault-admin event` | Start, inspect or stop a one-hour equal-chance event. |
| `/vault-admin raid` | Start a cooperative encounter in Ground or Fleet. |
| `/vault-admin health` | Inspect gateway, storage and scheduler readiness. |
| `/vault-admin complaints` | Read private reports and mark them reviewed. |

Manual forced spawns and the first event-start draw override the automatic fresh-activity requirement. Supply and capture protections still apply. Ordinary collectors use `/vault complain` to submit a private report to server managers; those messages are stored and are not anonymous to managers.

See [catalog and balance](CATALOG.md) for rarity odds, continuity and release limits.
