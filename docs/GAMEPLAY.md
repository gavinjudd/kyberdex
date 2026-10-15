# Gameplay and Discord commands

KyberDex is a server-local collection game. Players identify encounters, build a collection, trade and craft with explicit confirmation, then use separate Ground and Fleet squads in duels and raids. Credits are earned in game and can't be bought or cashed out. There are no real-money purchases or paid card draws; the optional credit games never put cards at stake.

## Encounters and captures

The default automatic encounter window is randomized between 12 and 20 minutes. It is hidden from players. A due timer also needs fresh activity from an eligible human in an approved public channel within the last five minutes and after the previous publication. Quiet servers receive no automatic cards. Eligible messages reduce the sampled wait by five seconds each, up to one minute per cycle, with per-member limits and cooldowns. The schedule and credited contributions survive restarts.

Press **Capture** and identify the artwork by name or an accepted alias. Capitalization, spacing and punctuation are normalized. Distinctive names can work; ambiguous surnames and generic titles do not. A small typo in a longer name is accepted (one edit up to nine letters, two beyond) when no other card is as close, and possessive names such as "TIE whisper" for "Kylo Ren's modified TIE whisper" work when unique. Model numbers are never dropped (B1 is not B2). Hints unlock after 20 seconds, add the first letter and word count at 45 seconds and a masked name pattern at 110 seconds. Wrong guesses trigger a persisted cooldown and are logged for `/vault-admin failed-names`.

| Rarity | Distinct collectors per ordinary encounter |
| --- | --- |
| Common, Uncommon, Rare, Epic | 2 |
| Legendary, Mythic | 1 |

Within each rarity, well-known subjects appear more often than obscure ones (see [CATALOG.md](CATALOG.md#notability-weighting)); the rarity odds themselves don't change.

Each collector can claim one copy per encounter. After two consecutive successful direct captures across the guild, that player must let someone else capture before claiming again. Another collector taking the second slot also breaks the streak. Failed guesses, restarts, trades and crafting do not reset it. In the last 90 seconds of an encounter, if nobody else has guessed or captured, the streak holder may take the card rather than let it expire. Explicit event overrides may set a different published capacity.

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

A member holding fewer than three cards receives a one-time starter pack the first time they open `/vault help`, `collection`, `profile` or `daily`: three well-known Ground Commons (a full deck) and 250 credits.

The public encounter shows collectors and remaining slots. The successful capture receipt is private; `/vault card` makes sharing a deliberate choice.

## Credits, market, crafting and trades

| Command | Behavior |
| --- | --- |
| `/vault daily` | Claim free daily credits. |
| `/vault market` | Review the daily market card before buying with credits. |
| `/vault recipes` | Inspect exact crafting costs and recycle values. |
| `/vault craft` | Review the selected result, exact consumed copies and cost. |
| `/vault salvage` | Review duplicates and credit return before recycling. |
| `/vault trade` | Open a trade table with someone (they're pinged); optionally pre-load your offer. One-sided offers work. |
| `/vault give` | Gift up to five copies to another member after a review step. |
| `/vault gamble coinflip` · `slots` · `wager` · `stats` | Optional credit games. See below. |

Trades allow up to 10 copies per side, three open tables per player and a one-hour window. Favorites and saved deck cards may be traded or given; they leave the deck and lose favorite status. Copies in another trade or an active raid stay locked. Favorites, escrow and active loadouts are still protected from crafting and recycling. Changing a trade invalidates both parties' confirmations; both must accept the same offer revision. The ownership exchange is atomic. Published rarity reference values are game reference values, not a real-world price.

## Credit games

All games use in-game credits only. Credits can't be bought or cashed out, and cards are never wagered.

| Game | Rules |
| --- | --- |
| Coin flip | Call heads or tails. A 48% chance to win double the bet. |
| Kessel Run slots | Three reels. Three of a kind pays 3× to 100× (💎 Kyber Crystal); any pair pays 1.2×, a Kyber pair 5×. Long-run return is about 91%. |
| Wager | Challenge a member to a coin toss. Both stakes settle when they accept; the winner takes the pot minus 5%. Expires after two minutes. |

Bets are 10–1,000 credits, with at most 5,000 credits wagered per member per UTC day. Every game keeps a small house edge, so gambling removes credits from the economy over time. Server managers can switch it off with `/vault-admin gambling`.

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
| `/vault-admin failed-names` | Review rejected capture names for missing valid aliases. |
| `/vault-admin give` | Mint 1–10 copies of any card for a member, with an optional public announcement. Numbered editions keep their cap. |
| `/vault-admin clear-inventory` | Remove every card a member owns after a button confirmation; cancels their open trades. |
| `/vault-admin restore-inventory` | Undo a clear. |
| `/vault-admin gambling` | Turn credit games on or off. |

Manual forced spawns and the first event-start draw override the automatic fresh-activity requirement. Supply and capture protections still apply. Ordinary collectors use `/vault complain` to submit a private report to server managers; those messages are stored and are not anonymous to managers.

See [catalog and balance](CATALOG.md) for rarity odds, continuity and release limits.
