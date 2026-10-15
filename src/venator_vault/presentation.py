"""Discord presentation; game decisions remain in the transactional engine."""
from __future__ import annotations

import asyncio
import math
import time
from pathlib import Path

import discord
from aiohttp import ClientError

from .references import lore_references

COLORS = {"common": 0x98A8B8, "uncommon": 0x5CC6A0, "rare": 0x67B9EF,
          "epic": 0xAD8FEF, "legendary": 0xE5B86B, "mythic": 0xEA818A, "celestial": 0xFFF1C7}
BRAND = 0xD8B780
DISCORD_IO_TIMEOUT = 15.0
DISCORD_ERRORS = (discord.HTTPException, discord.ConnectionClosed, OSError, ClientError)


async def discord_request(operation, *, timeout=None):
    """Bound network I/O only; never cancel a database transaction worker."""
    limit = DISCORD_IO_TIMEOUT if timeout is None else min(DISCORD_IO_TIMEOUT, timeout)
    async with asyncio.timeout(limit):
        return await operation


def panel(title, description="", color=BRAND):
    embed = discord.Embed(title=title[:256], description=description[:4096], color=color)
    embed.set_footer(text="KyberDex · Star Wars cards")
    return embed


def collection_label(card):
    """Describe provenance without changing the definition's eligibility."""
    if card["category"] == "canon" and "user-approved Old Republic" in str(card.get("continuity", "")):
        return "Approved game continuity · Legends"
    return card["category"].title()


def card_panel(card, instance=None):
    embed = panel(card["name"], card.get("description", ""), COLORS.get(card["rarity"], BRAND))
    embed.add_field(name="Edition", value=f"{card['rarity'].title()} · {collection_label(card)}\n{card.get('set', 'Core archive')}")
    embed.add_field(name="Identity", value=f"{card['faction']} · {card['era']}\n{card['arena'].title()} / {card['role'].title()}")
    stats = instance or card
    embed.add_field(name="Combat", value=f"ATK **{stats['attack']}** · HP **{stats['health']}** · SPD **{stats['speed']}**", inline=False)
    ability = card.get("ability", "")
    ability_text = card.get("ability_description") or (str(ability) if not isinstance(ability, dict) else ability.get("description", ability.get("name", "")))
    embed.add_field(name=card.get("ability_name", "Ability"), value=ability_text[:1024] or "Role ability", inline=False)
    names = ", ".join([card["name"], *card.get("aliases", [])])[:850]
    embed.add_field(name="Accepted names", value=names + "\nTitles are optional. Distinctive whole name parts also work. Generic titles/types and ambiguous surnames do not.", inline=False)
    if instance:
        if instance.get("historical_receipt"):
            embed.add_field(name="Capture receipt", value=f"`{instance['id']}` · Originally captured <t:{int(instance['acquired_at'])}:f>", inline=False)
        else:
            embed.add_field(name="Your copy", value=f"`{instance['id']}` · {'★ Protected favorite' if instance.get('favorite') else 'Standard copy'}", inline=False)
        if "attack_roll" in instance:
            embed.add_field(name="Copy stats", value=f"Attack {instance['attack_roll']-100:+d}% · Health {instance['health_roll']-100:+d}%"
                            + (" · Limited edition: +10% after these rolls" if card["category"]=="limited" else ""), inline=False)
    release = card.get("release")
    if card.get("event_only") or card.get("event_id") or card["category"] == "legends":
        embed.add_field(name="Event-exclusive", value="Available only during an explicitly enabled event. Excluded from ordinary drops and crafting.", inline=False)
    if release:
        embed.add_field(name="Limited release", value=f"{release.get('starts_at')} → {release.get('ends_at')}\nEdition cap: {release.get('supply_cap')} copies", inline=False)
    continuity_title = "Continuity & references"
    continuity = str(card.get("continuity") or "Continuity not specified")[:256]
    excess = max(0, len(embed) + len(continuity_title) + len(continuity) - 6000)
    if excess and embed.description:
        embed.description = embed.description[:max(0, len(embed.description) - excess)] or None
    available = min(1024, max(0, 6000 - len(embed) - len(continuity_title)))
    continuity = continuity[:available]
    for index, reference in enumerate(lore_references(card), 1):
        link = f"\n[Reference {index} · {reference['label']}]({reference['url']})"
        if len(continuity) + len(link) <= available:
            continuity += link
    if continuity:
        embed.add_field(name=continuity_title, value=continuity, inline=False)
    return embed


def drop_panel(drop, now=None):
    now = time.time() if now is None else now
    closed = drop["status"] != "active" or drop["expires_at"] <= now
    card = drop["card"]
    limit = drop["collector_limit"]
    capture_rule = ("**One collector can capture this drop.**" if limit == 1 else
                    f"**Up to {limit} distinct collectors can capture this drop. One copy each.**")
    title = "Card captured" if drop["claims"] and closed else "Encounter ended" if closed else "A card appeared"
    text = "This encounter has ended." if closed else (
        "Recognize the artwork, then press **Capture** and enter its name.\n"
        f"{capture_rule}\n"
        "Caught the last two cards? Another player must catch one before you can catch again.\n"
        f"Closes <t:{int(drop['expires_at'])}:R>."
    )
    if not closed and drop.get("delivery_pending"):
        text = "This encounter is getting ready. Capture opens shortly."
    embed = panel(title, text, COLORS.get(card["rarity"], BRAND))
    embed.add_field(name="Capture slots", value=f"**{len(drop['claims'])}/{limit}** claimed · **{drop['remaining']}** remaining")
    embed.add_field(name="Encounter", value=f"{card['rarity'].title()} · {card['arena'].title()}\n{collection_label(card)}")
    if drop.get('override_until'):
        event_label = 'Equal-chance hour' if drop.get('uniform') else 'Admin encounter during the event'
        embed.add_field(name='Special event', value=f"{event_label}\nEvent ends <t:{int(drop['override_until'])}:R>. Edition stock and capture rules still apply.", inline=False)
    if drop["claims"]:
        embed.add_field(name="Captured by", value="\n".join(f"{index}. <@{item['user_id']}>" for index,item in enumerate(drop['claims'],1)),inline=False)
        embed.add_field(name="Your card", value="Your card and stats are in your private capture receipt. Use `/vault card` if you want to show it off.",inline=False)
    embed.set_image(url="attachment://encounter.png")
    return embed


def action_view(items, *, disabled=False):
    view = discord.ui.View(timeout=None)
    for label, action, entity, style in items:
        view.add_item(discord.ui.Button(label=label[:80], custom_id=f"vv:{action}:{entity}", style=style, disabled=disabled))
    return view


def drop_view(drop):
    return action_view([("Capture", "capture", drop["id"], discord.ButtonStyle.primary),
                        ("Hint", "hint", drop["id"], discord.ButtonStyle.secondary)],
                       disabled=drop["status"] != "active" or drop["remaining"] == 0 or bool(drop.get("delivery_pending")))


def art_path(root: Path, card):
    if card.get("art_status") != "ready":
        raise ValueError(f"Artwork is not ready for card {card['id']}")
    candidate = (root / card["art"]).resolve()
    art_root = (root / "assets/art").resolve()
    if not candidate.is_relative_to(art_root) or not candidate.is_file():
        raise ValueError(f"Artwork is not ready for card {card['id']}")
    return candidate


def collection_panel(rows, user_id, page=0):
    description = "\n".join(
        f"{'★' if row.get('favorite') else '·'} **{row['name']}** · {row['card']['rarity'].title()}\n"
        f"`{row['id']}` · {row['attack']} ATK / {row['health']} HP · {row['card']['arena']}"
        for row in rows
    ) or "Your vault is ready for its first card. Watch a scheduled encounter and press Capture."
    embed = panel(f"Your collection · page {page + 1}", description)
    embed.set_footer(text="KYBERDEX · These are your owned copies. /vault catalog shows every definition.")
    return embed


def seconds_left(timestamp):
    return max(0, math.ceil(timestamp - time.time()))
