"""Reviewed economy, trade, deck, duel and co-op interaction flows."""
from __future__ import annotations

import asyncio
import io
import logging
import secrets
import sqlite3
import time

import discord
from discord import app_commands

from .core import (
    CRAFT_RECIPES,
    GAMBLE_MAX,
    GAMBLE_MIN,
    GIFT_MAX,
    RARITIES,
    SLOT_SYMBOLS,
    TRADE_MAX_PER_SIDE,
    WAGER_HOUSE_PERCENT,
    Game,
    GameError,
)
from .presentation import DISCORD_ERRORS, action_view, card_panel, discord_request, panel

log = logging.getLogger("venator_vault")
RECONCILE_BATCH = 8


def ids_from_text(text, limit=5):
    values = [item.strip() for item in text.replace(",", " ").split() if item.strip()]
    if len(values) > limit or len(values) != len(set(values)):
        raise ValueError(f"Choose at most {limit} distinct copy IDs from /vault collection.")
    if any(len(item) != 16 or any(char not in "0123456789abcdef" for char in item) for item in values):
        raise ValueError("Use the 16-character copy IDs shown by /vault collection, separated by spaces.")
    return values


async def copy_choices(bot, interaction, current, *, limit=5):
    """Autocomplete the final token while preserving copies already selected."""
    if interaction.guild_id != bot.settings.guild_id or interaction.user.bot:
        return []
    words = current.replace(",", " ").split()
    chosen = []
    while words and len(words[0]) == 16 and all(char in "0123456789abcdef" for char in words[0]):
        chosen.append(words.pop(0))
    query = " ".join(words)
    if len(chosen) >= limit:
        return []
    try:
        namespace = getattr(interaction,"namespace",None)
        arena = getattr(namespace,"arena",None)
        card_id = getattr(namespace,"card",None)
        rows = await asyncio.wait_for(
            bot.call("inventory", str(interaction.guild_id), str(interaction.user.id), search=query, limit=25,arena=arena,card_id=card_id),
            timeout=1.8,
        )
    except (TimeoutError, sqlite3.OperationalError):
        # Autocomplete cannot defer its initial Discord response. The owned-copy
        # command remains usable with an exact ID while a snapshot holds a lock.
        return []
    arena = getattr(getattr(interaction, "namespace", None), "arena", None)
    rows = [row for row in rows if row["id"] not in chosen and (not arena or row["card"]["arena"] == arena)]
    # Discord limits autocomplete values to 100 characters. Large recipes use
    # the native multi-select or explicit copy-ID input, not a truncated list.
    return [app_commands.Choice(name=f"{row['name']} · {row['attack']} ATK / {row['health']} HP · {row['id']}"[:100],
                               value=" ".join([*chosen, row["id"]])) for row in rows[:25]
            if len(" ".join([*chosen, row["id"]])) <= 100]


def offer_picker(trade, rows, user):
    view = discord.ui.View(timeout=180)
    offered = {item["id"] for item in trade["offers"].get(user, [])}
    rows = [row for row in rows if not row.get("locked_by") or row["id"] in offered]
    if rows:
        options = [discord.SelectOption(label=row["name"][:100], value=row["id"],
                    description=f"{row['card']['rarity'].title()} · {row['attack']} ATK / {row['health']} HP · {row['id']}"[:100],
                    default=row["id"] in offered) for row in rows[:25]]
        view.add_item(discord.ui.Select(custom_id=f"vv:offerpick:{trade['id']}", placeholder="Choose the copies you'll hand over",
                                       options=options, min_values=0, max_values=min(TRADE_MAX_PER_SIDE, len(options))))
    view.add_item(discord.ui.Button(label="Enter copy IDs instead", custom_id=f"vv:offercustom:{trade['id']}",
                                   style=discord.ButtonStyle.secondary))
    return view


def trade_panel(trade):
    embed = panel("Trade table", f"<@{trade['a']}> ⇄ <@{trade['b']}> · Status: **{trade['status']}**\n"
                  "Each side adds cards with **Choose my offer** (leaving your side empty is fine — that makes it a gift). "
                  "When both of you press **Confirm**, the cards swap. Changing an offer resets both confirmations.")
    for user in (trade["a"], trade["b"]):
        items = trade["offers"].get(user, [])
        text = "\n".join(f"**{i['name']}** · {i['card']['rarity'].title()} · {i['attack']} ATK / {i['health']} HP" for i in items) or "Nothing offered yet."
        text += f"\n{'✅ Confirmed' if trade['accepted'].get(user) else '⏳ Not confirmed'}"
        embed.add_field(name="Offer from", value=f"<@{user}>\n{text}"[:1024], inline=True)
    embed.description += f"\nExpires <t:{int(trade['expires_at'])}:R>."
    embed.set_footer(text=f"Trade {trade['id']} · revision {trade['version']} · No real-money value")
    return embed


def trade_view(trade):
    return action_view([
        ("Choose my offer", "offer", trade["id"], discord.ButtonStyle.secondary),
        ("Confirm", f"accept{trade['version']}", trade["id"], discord.ButtonStyle.success),
        ("Cancel trade", "canceltrade", trade["id"], discord.ButtonStyle.danger),
    ], disabled=trade["status"] != "open")


def deck_panel(deck):
    rows = [f"**{card['name']}** · {card['card']['role'].title()}\n"
            f"{card['effective_attack']} ATK / {card['effective_health']} HP / {card['speed']} SPD "
            f"· +{round(card['faction_bonus']*100)}% faction\n`{card['id']}`" for card in deck["cards"]]
    return panel(f"Your {deck['arena'].title()} squad", "\n\n".join(rows) or "No cards selected. Add up to three different card types from your collection.")


def duel_panel(duel):
    result = duel.get("result")
    if result:
        winner = f"<@{result['winner_id']}>" if result.get("winner_id") else "Draw"
        return panel(f"{duel['arena'].title()} duel complete", f"**Result: {winner}**\n{result['rounds']} rounds. "
                     "No cards or credits were wagered. The attached log explains every turn.")
    embed = panel(f"{duel['arena'].title()} challenge · {duel['status']}",
                  f"<@{duel['a']}> challenges <@{duel['b']}>.\n"
                  "These exact squads will fight. Changing either squad cancels acceptance until a fresh challenge is created. "
                  f"Only the challenged collector can accept. No assets are wagered.\nExpires <t:{int(duel['expires_at'])}:R>.")
    for user, deck in zip((duel["a"], duel["b"]), duel.get("preview") or []):
        lines = [f"**{card['name']}** · {card['card']['role'].title()}\n"
                 f"{card['effective_attack']} ATK / {card['effective_health']} HP / {card['speed']} SPD"
                 f" · +{round(card['faction_bonus']*100)}% faction" for card in deck["cards"]]
        embed.add_field(name=f"Collector {user}", value="\n".join(lines)[:1024], inline=False)
    return embed


def duel_view(duel):
    return action_view([("Accept challenge", "acceptduel", duel["id"], discord.ButtonStyle.success),
                        ("Cancel", "cancelduel", duel["id"], discord.ButtonStyle.secondary)],
                       disabled=duel["status"] != "pending")


def duel_log(duel):
    return discord.File(io.BytesIO("\n".join(str(line) for line in duel["result"]["log"]).encode()), filename="battle-log.txt")


def raid_panel(raid, image_url=None):
    signup = raid["status"] == "signup"
    text = (f"Join before <t:{int(raid['signup_ends_at'])}:R>. The encounter scales to the actual squad that joins."
            if signup else f"Ends <t:{int(raid['expires_at'])}:R>. Each joined collector gets three attacks, {raid['attack_cooldown']} seconds apart.")
    if raid["status"] == "defeated":
        text = "Boss defeated. Rewards have been delivered to the collectors who attacked."
    elif raid["status"] == "failed":
        text = "The squads ran out of HP or attacks. No rewards this time; try a stronger deck next raid."
    elif raid["status"] == "escaped":
        text = "This raid ended before the boss was defeated. No rewards this time."
    boss = raid.get("boss")
    title = (f"{boss['name']} · {boss['difficulty'].title()} {raid['arena'].title()} raid · {raid['status']}"
             if boss else f"{raid['arena'].title()} cooperative raid · {raid['status']}")
    if boss:
        text += f"\n**{boss['move_description']}**\nOwned cards are safe; defeat only ends this raid squad's attacks."
    embed = panel(title, text)
    if boss:
        embed.add_field(name="Boss threat", value=f"ATK **{boss['attack']:,}** · SPD **{boss['speed']}**\n"
                        f"Base retaliation: {round(boss['attack']*boss['counter']):,} before move/formation modifiers.")
        pool = boss.get("credit_pool")
        embed.add_field(name="Victory rewards", value=(f"Up to **{pool:,} credits** shared" if pool else
                        f"**{boss['credits']:,} base credits** in the shared pool, scaled to contributing squad power (max ×5)") +
                        f"\nUp to **{boss['xp']} XP each**, based on participation. Damage and effort determine your share. No payout for merely joining or losing.", inline=False)
        url = image_url or boss.get("image_url")
        if url:
            embed.set_image(url=url)
    if raid["max_hp"]:
        fraction = max(0, raid["hp"] / raid["max_hp"])
        filled = max(0, min(12, round(fraction*12)))
        embed.add_field(name="Encounter strength", value=f"{'▰'*filled}{'▱'*(12-filled)}\n**{raid['hp']:,} / {raid['max_hp']:,} HP**", inline=False)
    participants = sorted(raid["participants"], key=lambda item: item["damage"], reverse=True)
    embed.add_field(name=f"Squad · {len(participants)} collectors", value="\n".join(
        f"<@{member['user_id']}> · {member['damage']:,} damage · {member['attacks']}/3 attacks" +
        (f" · {member['combat']['hp']:,}/{member['combat']['max_hp']:,} squad HP" if member.get("combat") else "")
        for member in participants[:12]) or "Join with your saved squad to help scale the encounter.", inline=False)
    if boss and boss.get("roster_potential") and raid["max_hp"] > boss["roster_potential"]:
        embed.add_field(name="Squad warning", value="This roster's three-attack damage ceiling is below the boss's HP. Stronger decks were needed at signup.", inline=False)
    if raid.get("damage_dealt"):
        embed.add_field(name="Your attack", value=f"{raid['damage_dealt']:,} damage dealt · {raid.get('retaliation_damage',0):,} taken · {raid.get('repaired_hp',0):,} repaired", inline=False)
    if raid["rewards"]:
        embed.add_field(name="Rewards delivered", value="\n".join(
            f"<@{row['user_id']}> · +{row['credits']} credits / +{row['xp']} XP" for row in raid["rewards"][:12]), inline=False)
    embed.set_footer(text="Raid rewards are credits and XP. Card encounters keep their displayed collector limits.")
    return embed


def raid_view(raid):
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Join squad", custom_id=f"vv:joinraid:{raid['id']}",
                                   style=discord.ButtonStyle.primary, disabled=raid["status"] != "signup"))
    view.add_item(discord.ui.Button(label="Attack", custom_id=f"vv:attackraid:{raid['id']}",
                                   style=discord.ButtonStyle.success, disabled=raid["status"] != "active"))
    return view


async def refresh_raid(bot, raid):
    if not raid.get("message_id"):
        return
    lock = bot.refresh_lock("raid-"+raid["id"])
    async with lock:
        current = await bot.call("get_raid", raid["id"])
        try:
            channel = bot.get_channel(int(current["channel_id"])) or await discord_request(bot.fetch_channel(int(current["channel_id"])))
            await discord_request(channel.get_partial_message(int(current["message_id"])).edit(embed=raid_panel(current), view=raid_view(current)))
            return True
        except discord.NotFound as error:
            return error.code == 10008 and current["status"] not in ("signup","active")
        except DISCORD_ERRORS as error:
            log.warning("Raid display will retry: %s", type(error).__name__)
            return False


class OfferModal(discord.ui.Modal, title="Choose your trade offer"):
    copies = discord.ui.TextInput(label="Copy IDs (maximum ten)", placeholder="Paste IDs from /vault collection; blank clears your offer",
                                 required=False, max_length=200, style=discord.TextStyle.paragraph, custom_id="copies")

    def __init__(self, bot, trade_id):
        super().__init__(timeout=180, custom_id=f"vv:offer-submit:{trade_id}")
        self.bot, self.trade_id = bot, trade_id

    async def on_submit(self, interaction):
        from .bot import reply
        if not self.bot.begin_modal_submission(interaction):
            return
        if not await self.bot.valid_context(interaction):
            return
        await discord_request(interaction.response.defer(ephemeral=True))
        try:
            trade = await self.bot.call("get_trade", self.trade_id)
            self.bot.verify_guild(trade, interaction)
            trade = await self.bot.call("trade_offer", self.trade_id, str(interaction.user.id), ids_from_text(str(self.copies), TRADE_MAX_PER_SIDE))
            await reply(interaction, embed=trade_panel(trade), view=trade_view(trade))
            await refresh_trade(self.bot, trade)
        except Exception as error:  # noqa: BLE001 - modal errors use the central safe responder
            await self.bot.command_error(interaction, error)


async def show_deck_picker(bot, interaction, arena, raid_id=None, key=None, *, edit=False):
    from .bot import reply
    guild, user = str(interaction.guild_id), str(interaction.user.id)
    if key is None:
        key = secrets.token_hex(12)
        try:
            saved = await bot.call("deck", guild, user, arena)
            selected = [card["id"] for card in saved["cards"]]
        except GameError:
            selected = []
        bot.pending_confirmations = {k:v for k,v in bot.pending_confirmations.items() if v["expires"] > time.time()}
        bot.pending_confirmations[key] = {"kind":"deck_picker", "guild":guild, "user":user, "arena":arena,
                                         "raid_id":raid_id, "selected":selected, "offset":0, "expires":time.time()+180}
    pending = bot.pending_confirmations[key]
    page = await bot.call("deck_choices", guild, user, arena, pending["offset"])
    pending["page_ids"] = [card["id"] for card in page["cards"]]
    view = discord.ui.View(timeout=180)
    if page["cards"]:
        view.add_item(discord.ui.Select(custom_id=f"vv:deckpick:{key}", placeholder="Choose up to 3 different cards",
            min_values=0, max_values=min(3,len(page["cards"])), options=[discord.SelectOption(
                label=card["name"][:100],value=card["id"],default=card["id"] in pending["selected"],
                description=f"{card['card']['rarity'].title()} · {card['attack']} ATK / {card['health']} HP · {card['id']}"[:100])
                for card in page["cards"]]))
        view.add_item(discord.ui.Button(label="Previous cards",custom_id=f"vv:deckprev:{key}",disabled=pending["offset"]==0))
        view.add_item(discord.ui.Button(label="More cards",custom_id=f"vv:decknext:{key}",disabled=pending["offset"]+25>=page["total"]))
        view.add_item(discord.ui.Button(label="Save deck and join" if raid_id else "Save deck",custom_id=f"vv:decksave:{key}",
                                       style=discord.ButtonStyle.success,disabled=not pending["selected"]))
    chosen = [await bot.call("info",guild,user,copy_id) for copy_id in pending["selected"]]
    body = "Pick cards from your collection, then press **Save deck and join**." if raid_id else "Pick cards from your collection, then press **Save deck**."
    if not page["total"]:
        body = f"You don't own any available {arena.title()} cards yet. Capture a {arena.title()} card first, then come back."
    elif raid_id:
        raid = await bot.call("get_raid",raid_id)
        bot.verify_guild(raid,interaction)
        body += f" Join before <t:{int(raid['signup_ends_at'])}:R>."
    body += "\n\n**Selected:** " + (", ".join(card["name"] for card in chosen) or "None")
    body += "\nYour cards stay in your collection. Ground and Fleet decks are separate."
    embed=panel(f"Build your {arena.title()} deck",body)
    if edit:
        await discord_request(interaction.edit_original_response(embed=embed,view=view))
    else:
        await reply(interaction,embed=embed,view=view)


def start_view():
    return action_view([("My collection","opencollection","home",discord.ButtonStyle.secondary),
                        ("Ground deck","openground","home",discord.ButtonStyle.secondary),
                        ("Fleet deck","openfleet","home",discord.ButtonStyle.secondary),
                        ("Raids","openraid","home",discord.ButtonStyle.primary),
                        ("Market","openmarket","home",discord.ButtonStyle.primary)])


def market_panel(market):
    embed=panel("Daily market",f"Balance: **{market['credits']:,} credits**. One purchase per player per UTC day.\n"
                f"New offers <t:{int(market['resets_at'])}:R>. Stock is shared across this server.\n"
                "You choose the card; its exact attack and health roll is revealed after purchase (95–105% of base stats).")
    for offer in market["offers"]:
        availability=f"{offer['remaining']}/{offer['stock']} left" if offer["available"] else "Unavailable"
        embed.add_field(name=offer["name"],value=f"{offer['rarity'].title()} · {offer['arena'].title()} · {offer['role'].title()}\n{offer['faction']}\n"
                        f"**{offer['price']:,} credits** · {availability}",inline=False)
    if market["purchased_today"]:
        embed.description+=f"\nYou bought **{market['purchased_card']}** today. It's in /vault collection. Come back after the daily reset."
    if not market["offers"]:
        embed.description+="\nNo eligible offers today."
    embed.set_footer(text="Legendary, Mythic, limited and event cards are not sold here. No paid currency.")
    return embed


async def show_market(bot,interaction):
    from .bot import reply
    market=await bot.call("market",str(interaction.guild_id),str(interaction.user.id))
    view=discord.ui.View(timeout=None)
    available=[offer for offer in market["offers"] if offer["available"] and offer["remaining"]>0]
    if available and not market["purchased_today"]:
        view.add_item(discord.ui.Select(custom_id=f"vv:marketpick:{interaction.user.id}",placeholder="Choose a card to review its price",
            options=[discord.SelectOption(label=offer["name"][:100],value=offer["id"],
                description=f"{offer['rarity'].title()} · {offer['price']:,} credits · {offer['remaining']} left") for offer in available]))
    view.add_item(discord.ui.Button(label="Refresh market",custom_id="vv:openmarket:home",style=discord.ButtonStyle.secondary))
    await reply(interaction,embed=market_panel(market),view=view)


async def browse_cards(bot,interaction,kind,search="",rarity="",page=1,key=None,*,edit=False):
    from .bot import reply
    from .presentation import collection_panel
    guild,user=str(interaction.guild_id),str(interaction.user.id)
    if key is None:
        key=secrets.token_hex(12)
        bot.pending_confirmations={k:v for k,v in bot.pending_confirmations.items() if v["expires"]>time.time()}
        bot.pending_confirmations[key]={"kind":"browse", "browse_kind":kind,"search":search,"rarity":rarity,
                                       "page":page,"user":user,"guild":guild,"expires":time.time()+300}
    state=bot.pending_confirmations[key]
    if kind=="collection":
        notice=await starter_notice(bot,interaction) if page==1 and not edit else ""
        rows=await bot.call("inventory",guild,user,search=search,rarity=rarity,limit=13,offset=(page-1)*12)
        embed=collection_panel(rows[:12],user,page-1)
        if notice:
            embed.description=(notice+"\n\n"+(embed.description or ""))[:4096]
        has_next=len(rows)>12
    else:
        cards=[card for card in bot.game.catalog.values() if (not rarity or card["rarity"]==rarity)
               and (bot.game.matches_card(card["id"],search) or search.casefold() in f"{card['faction']} {card['era']}".casefold())]
        cards.sort(key=lambda card:card["name"])
        rows=cards[(page-1)*12:page*12]
        embed=panel(f"Card catalog · {len(cards)} matches", "\n".join(
            f"**{card['name']}** · {card['rarity'].title()} / {card['arena']}\n{card['faction']}" for card in rows) or "No cards match. Try another name or rarity.")
        embed.set_footer(text=f"Page {page}/{max(1,(len(cards)+11)//12)} · All cards, including ones you don't own")
        if bot.settings.vault_url:
            embed.url=bot.settings.vault_url
        has_next=page*12<len(cards)
    state["has_next"]=has_next
    view=action_view([("Previous","browseprev",key,discord.ButtonStyle.secondary),
                      ("Next","browsenext",key,discord.ButtonStyle.secondary),
                      ("Ground deck","openground","home",discord.ButtonStyle.secondary),
                      ("Fleet deck","openfleet","home",discord.ButtonStyle.secondary)])
    view.children[0].disabled=page<=1
    view.children[1].disabled=not has_next
    if edit:
        await discord_request(interaction.edit_original_response(embed=embed,view=view))
    else:
        await reply(interaction,embed=embed,view=view)


SLOT_ICONS = {"Kyber Crystal": "💎", "Death Star": "🌑", "Millennium Falcon": "🛸", "X-wing": "✈️", "TIE Fighter": "🔷", "Blaster": "🔫"}
GAMBLE_FOOTER = "Credits only · credits can't be bought or cashed out · no cards are ever at stake"


def wager_panel(wager):
    status = wager["status"]
    if status == "settled":
        result = wager.get("result") or {}
        text = (f"<@{wager['a']}> vs <@{wager['b']}> for **{wager['stake']:,} credits** each.\n"
                f"🏆 **<@{wager['winner']}> wins {result.get('prize', 0):,} credits** ({WAGER_HOUSE_PERCENT}% of the pot goes to the house).")
    elif status == "pending":
        text = (f"<@{wager['a']}> challenges <@{wager['b']}> to a **{wager['stake']:,}-credit** coin toss.\n"
                f"Winner takes the pot minus {WAGER_HOUSE_PERCENT}%. Expires <t:{int(wager['expires_at'])}:R>.")
    else:
        text = f"Wager between <@{wager['a']}> and <@{wager['b']}> was {status}. No credits moved."
    embed = panel("Credit wager", text)
    embed.set_footer(text=GAMBLE_FOOTER)
    return embed


def wager_view(wager):
    return action_view([("Accept", "wageraccept", wager["id"], discord.ButtonStyle.success),
                        ("Decline / cancel", "wagerdecline", wager["id"], discord.ButtonStyle.secondary)],
                       disabled=wager["status"] != "pending")


async def handle_component(bot, interaction, action, entity):
    from .bot import reply
    user = str(interaction.user.id)
    if action=="openmarket":
        await discord_request(interaction.response.defer(ephemeral=True,thinking=True))
        await show_market(bot,interaction)
        return
    if action in ("marketpick","marketbuy","marketcancel"):
        await discord_request(interaction.response.defer(ephemeral=True,thinking=True))
        if action=="marketpick":
            if entity!=user:
                raise ValueError("Open /vault market to shop with your own credits.")
            values=(interaction.data or {}).get("values",[])
            market=await bot.call("market",str(interaction.guild_id),user)
            offer=next((offer for offer in market["offers"] if values==[offer["id"]]),None)
            if not offer or not offer["available"] or not offer["remaining"]:
                raise ValueError("That offer is no longer available. Reopen /vault market.")
            if market["purchased_today"]:
                raise ValueError("You already bought a card today. The market resets at 00:00 UTC.")
            key=secrets.token_hex(12)
            bot.pending_confirmations={k:v for k,v in bot.pending_confirmations.items() if v["expires"]>time.time()}
            bot.pending_confirmations[key]={"kind":"market", "guild":str(interaction.guild_id),"user":user,
                                           "offer_id":offer["id"],"expires":min(time.time()+90,market["resets_at"])}
            needed=max(0,offer["price"]-market["credits"])
            body=f"**{offer['name']}** · {offer['rarity'].title()} · {offer['arena'].title()} · {offer['role'].title()}\n{offer['faction']}\n"
            body+=f"Cost: **{offer['price']:,} credits**. Balance: **{market['credits']:,}**.\n"
            body+="Exact stats are hidden until you buy. Attack and health each roll between 95% and 105% of the card's base stats. Speed stays the same.\n"
            body+="Stock isn't reserved until purchase. This uses your one market purchase for today."
            if needed:
                body+=f"\nYou need **{needed:,} more credits**. Try /vault daily, raids, or recycling duplicates."
            view=action_view([(f"Buy for {offer['price']:,}","marketbuy",key,discord.ButtonStyle.success),
                              ("Cancel","marketcancel",key,discord.ButtonStyle.secondary)])
            view.children[0].disabled=bool(needed)
            await reply(interaction,embed=panel("Review your purchase",body),view=view)
            return
        pending=bot.pending_confirmations.get(entity)
        if not pending or pending.get("kind")!="market" or pending["expires"]<time.time():
            raise ValueError("This offer review expired. Reopen /vault market.")
        if (pending["user"],pending["guild"])!=(user,str(interaction.guild_id)):
            raise ValueError("This purchase review belongs to another player.")
        if action=="marketcancel":
            bot.pending_confirmations.pop(entity,None)
            await reply(interaction,content="Cancelled. No credits spent.")
            return
        result=await bot.call("market_buy",str(interaction.guild_id),user,pending["offer_id"],request_id=str(interaction.id))
        instance=result["instance"]
        embed=card_panel(instance["card"],instance)
        embed.title=(("Previously purchased · " if result["replayed"] else "Purchased · ")+instance["name"])[:256]
        note=("No additional credits charged. This is your earlier purchase receipt." if result["replayed"] else
              f"Paid **{result['credits_spent']:,} credits**. Balance after purchase: **{result['credits']:,}**.")
        biography=embed.description or ""
        budget=min(4096,max(0,6000-(len(embed)-len(biography))))
        embed.description=(note+"\nUse /vault card if you want to show your copy publicly.\n"+biography)[:budget]
        file=await bot.card_image(instance["card"],instance)
        if file is not None:
            embed.set_image(url=f"attachment://{file.filename}")
        await reply(interaction,embed=embed,file=file)
        return
    if action in ("opencollection","openground","openfleet","openraid"):
        await discord_request(interaction.response.defer(ephemeral=True,thinking=True))
        if action in ("openground","openfleet"):
            await show_deck_picker(bot,interaction,"ground" if action=="openground" else "fleet")
        elif action=="opencollection":
            await browse_cards(bot,interaction,"collection")
        else:
            raids=await bot.call("active_raids",str(interaction.guild_id))
            await reply(interaction,embed=raid_panel(raids[0]) if raids else panel("No raid running", "Build a deck now so you're ready for the next raid."),
                        view=raid_view(raids[0]) if raids else start_view())
        return
    if action in ("browseprev","browsenext"):
        await discord_request(interaction.response.defer())
        state=bot.pending_confirmations.get(entity)
        if not state or state.get("kind")!="browse" or state["expires"]<time.time():
            raise ValueError("This page expired. Reopen /vault collection or /vault catalog.")
        if (state["user"],state["guild"])!=(user,str(interaction.guild_id)):
            raise ValueError("This page belongs to another player. Open the command yourself.")
        if action=="browseprev":
            state["page"]=max(1,state["page"]-1)
        elif state["has_next"]:
            state["page"]+=1
        await browse_cards(bot,interaction,state["browse_kind"],state["search"],state["rarity"],state["page"],entity,edit=True)
        return
    if action in ("deckpick","deckprev","decknext","decksave"):
        await discord_request(interaction.response.defer())
        pending=bot.pending_confirmations.get(entity)
        if not pending or pending.get("kind")!="deck_picker" or pending["expires"]<time.time():
            raise ValueError("This picker expired. Open /vault deck or press Join squad again.")
        if pending["user"]!=user or pending["guild"]!=str(interaction.guild_id):
            raise ValueError("This deck picker belongs to another player. Open /vault deck for your own cards.")
        if action=="deckpick":
            values=(interaction.data or {}).get("values",[])
            if not set(values)<=set(pending["page_ids"]):
                raise ValueError("Choose cards shown in this picker.")
            chosen=[key for key in pending["selected"] if key not in pending["page_ids"]]+values
            if len(chosen)>3 or len(set(chosen))!=len(chosen):
                raise ValueError("Choose up to three cards in total. Unselect a card before adding another.")
            cards=[await bot.call("info",str(interaction.guild_id),user,key) for key in chosen]
            if len({card["card_id"] for card in cards})!=len(cards):
                raise ValueError("Choose different card types. Two copies of the same character can't share a deck.")
            pending["selected"]=chosen
        elif action=="deckprev":
            pending["offset"]=max(0,pending["offset"]-25)
        elif action=="decknext":
            pending["offset"]+=25
        else:
            if not pending["selected"]:
                raise ValueError("Pick at least one card first.")
            if pending["raid_id"]:
                raid=await bot.call("get_raid",pending["raid_id"])
                bot.verify_guild(raid,interaction)
                raid=await bot.call("raid_join",raid["id"],user,instance_ids=pending["selected"])
                await discord_request(interaction.edit_original_response(embed=raid_panel(raid),view=raid_view(raid)))
                await refresh_raid(bot,raid)
            else:
                deck=await bot.call("set_deck",str(interaction.guild_id),user,pending["arena"],pending["selected"])
                await discord_request(interaction.edit_original_response(embed=deck_panel(deck),view=start_view()))
            bot.pending_confirmations.pop(entity,None)
            return
        await show_deck_picker(bot,interaction,pending["arena"],pending["raid_id"],key=entity,edit=True)
        return
    if action == "craftpick":
        pending = bot.pending_confirmations.get(entity)
        if not pending or pending.get("kind") != "craft_selection" or pending["expires"] < time.time():
            raise ValueError("This selection expired or the bot restarted. Reopen crafting to choose current copies.")
        if pending["user"] != user or pending["guild"] != str(interaction.guild_id):
            raise ValueError("Only the collector who opened this selection can use it.")
        bot.pending_confirmations.pop(entity, None)
        selected = (interaction.data or {}).get("values", [])
        await review_consumption(bot, interaction, "craft", " ".join(selected), pending["target"])
        return
    if action in ("confirm", "discard"):
        pending = bot.pending_confirmations.get(entity)
        if not pending or pending.get("kind", "confirmation") != "confirmation" or pending["expires"] < time.time():
            raise ValueError("This review expired or the bot restarted. Check /vault collection for completed actions, then reopen the command to review current copies.")
        if pending["user"] != user or pending["guild"] != str(interaction.guild_id):
            raise ValueError("Only the collector who opened this review can use it.")
        bot.pending_confirmations.pop(entity, None)
        if action == "discard":
            await reply(interaction, content="Cancelled. Your cards are unchanged.")
            return
        await discord_request(interaction.response.defer(ephemeral=True))
        result = await bot.call(pending["method"], *pending["args"], **pending.get("kwargs", {}))
        if pending["method"] == "give":
            await announce_gift(bot, interaction, result)
            return
        if "instance" in result:
            instance = result["instance"]
            embed = card_panel(instance["card"], instance)
            file = await bot.card_image(instance["card"], instance)
            if file is not None:
                embed.set_image(url=f"attachment://{file.filename}")
            await reply(interaction, embed=embed, file=file)
        else:
            await reply(interaction, content=f"Recycled {len(result['consumed'])} duplicate copies for **{result['credits_earned']} credits**. "
                        f"Balance: **{result['credits']}**.")
        return
    if action in ("offer", "offercustom", "offerpick", "canceltrade") or (action.startswith("accept") and action[6:].isdigit()):
        if action == "offercustom":
            await discord_request(interaction.response.send_modal(OfferModal(bot, entity)))
            return
        await discord_request(interaction.response.defer(ephemeral=True))
        trade = await bot.call("get_trade", entity)
        bot.verify_guild(trade, interaction)
        if user not in (trade["a"], trade["b"]):
            raise ValueError("This table belongs to its two named collectors.")
        if action == "offer":
            rows = await bot.call("inventory", str(interaction.guild_id), user, limit=40)
            await reply(interaction, content="Pick from your most recent copies (or use `/vault trade member:… offer:…` to search by name). Selecting nothing clears your side.",
                        view=offer_picker(trade, rows, user))
            return
        if action == "offerpick":
            trade = await bot.call("trade_offer", entity, user, (interaction.data or {}).get("values", []))
        elif action == "canceltrade":
            trade = await bot.call("trade_cancel", entity, user)
        else:
            revision = action.removeprefix("accept")
            if not revision.isdigit():
                raise ValueError("Reopen this trade to review the current offer.")
            trade = await bot.call("trade_accept", entity, user, int(revision))
        await reply(interaction, embed=trade_panel(trade), view=trade_view(trade))
        await refresh_trade(bot, trade)
        return
    if action in ("wageraccept", "wagerdecline"):
        await discord_request(interaction.response.defer())
        wager = await bot.call("get_wager", entity)
        bot.verify_guild(wager, interaction)
        wager = await bot.call("wager_respond", entity, user, action == "wageraccept")
        try:
            await discord_request(interaction.message.edit(embed=wager_panel(wager), view=wager_view(wager),
                                                           allowed_mentions=discord.AllowedMentions.none()))
        except (DISCORD_ERRORS + (AttributeError,)) as error:
            log.warning("Wager display skipped: %s", type(error).__name__)
        loser = wager["b"] if wager.get("winner") == wager["a"] else wager["a"]
        if wager["status"] == "settled":
            text = (f"🪙 <@{wager['winner']}> beat <@{loser}> and took **{wager['result']['prize']:,} credits** "
                    f"({wager['stake']:,} each, {WAGER_HOUSE_PERCENT}% to the house).")
        else:
            text = f"<@{user}> called off the {wager['stake']:,}-credit wager. No credits moved."
        await reply(interaction, content=text, ephemeral=False)
        return
    if action in ("acceptduel", "cancelduel"):
        await discord_request(interaction.response.defer(ephemeral=True))
        duel = await bot.call("get_duel", entity)
        bot.verify_guild(duel, interaction)
        duel = await bot.call("duel_accept" if action == "acceptduel" else "duel_cancel", entity, user)
        await reply(interaction, embed=duel_panel(duel), file=duel_log(duel) if duel.get("result") else None)
        await refresh_duel(bot, duel)
        return
    if action in ("joinraid", "attackraid"):
        await discord_request(interaction.response.defer(ephemeral=True, thinking=True))
        raid = await bot.call("get_raid", entity)
        bot.verify_guild(raid, interaction)
        if action == "joinraid" and raid["status"] == "signup" and not any(p["user_id"]==user for p in raid["participants"]):
            await show_deck_picker(bot,interaction,raid["arena"],raid["id"])
            return
        raid = await bot.call("raid_join" if action == "joinraid" else "raid_attack", entity, user)
        await reply(interaction, embed=raid_panel(raid), view=raid_view(raid))
        await refresh_raid(bot, raid)
        return
    raise ValueError("This control is no longer available. Reopen its /vault command.")


async def starter_notice(bot, interaction):
    """Grant the one-time starter pack if eligible; returns text to show, or ''."""
    if getattr(interaction, "guild_id", None) != bot.settings.guild_id:
        return ""
    try:
        pack = await bot.call("starter_pack", str(interaction.guild_id), str(interaction.user.id))
    except (GameError, sqlite3.OperationalError) as error:
        log.warning("Starter pack skipped: %s", type(error).__name__)
        return ""
    if not pack:
        return ""
    names = ", ".join(f"**{item['name']}** ({item['card']['rarity'].title()})" for item in pack["instances"])
    return (f"🎁 Welcome to KyberDex! Your starter pack: {names} and **{pack['credits']} credits**. "
            "They're in `/vault collection`, and together they make a full Ground deck (`/vault deck`).")


async def announce_gift(bot, interaction, result):
    from .bot import reply
    names = ", ".join(f"**{card['name']}** ({card['rarity'].title()})" for card in result["cards"])
    if result.get("replayed"):
        await reply(interaction, content=f"That gift already went through: {names}.")
        return
    text = f"<@{result['from_id']}> gave <@{result['to_id']}> {names}."
    if result.get("note"):
        text += f"\n> {discord.utils.escape_mentions(result['note'])}"
    try:
        await discord_request(interaction.channel.send(content=text, allowed_mentions=discord.AllowedMentions(
            users=[discord.Object(int(result["to_id"]))], everyone=False, roles=False)))
    except DISCORD_ERRORS as error:
        log.warning("Gift announcement skipped: %s", type(error).__name__)
    await reply(interaction, content=f"Sent {names} to <@{result['to_id']}>.")


async def review_consumption(bot, interaction, method, instance_ids, target=None):
    from .bot import reply
    await discord_request(interaction.response.defer(ephemeral=True))
    copies = ids_from_text(instance_ids, limit=7 if method == "craft" else 10)
    if not copies:
        raise ValueError("Choose at least one copy from /vault collection.")
    guild, user = str(interaction.guild_id), str(interaction.user.id)
    rows = [await bot.call("info", guild, user, key) for key in copies]
    description = "\n".join(f"**{row['name']}** · {row['attack']} ATK / {row['health']} HP · `{row['id']}`" for row in rows)
    recipe = await bot.call("recipes")
    credits = sum(recipe["salvage_values"][row["card"]["rarity"]] for row in rows)
    kwargs = {}
    if target:
        card = bot.game.catalog[target]
        if not regular_craft_card(card):
            raise ValueError("Launch crafting uses regular canon or approved game-continuity cards. Limited, meme and event cards cannot be crafted.")
        quote = await bot.call("craft_quote", target)
        if len(copies) != quote["ingredients"]:
            raise ValueError(f"Choose exactly {quote['ingredients']} copies for a {quote['target_rarity'].title()} result.")
        if any(not regular_craft_card(row["card"]) or
               (row["card"]["rarity"], row["card"]["faction"], row["card"]["arena"]) !=
               (quote["ingredient_rarity"], quote["faction"], quote["arena"]) for row in rows):
            raise ValueError("Ingredients must be regular canon or approved game-continuity copies one rarity below the target, with its faction and arena.")
        description += (f"\n\nConsume **{quote['ingredients']} {quote['ingredient_rarity'].title()} copies** and "
                        f"**{quote['cost']:,} credits** to craft **{quote['target_name']}** ({quote['target_rarity'].title()}). "
                        "Crafting can consume your final copy; favorite anything you want to keep.")
        kwargs["expected_recipe_fingerprint"] = quote["fingerprint"]
    else:
        description += f"\n\nRecycle these copies for **{credits} credits**. Your final copy and protected cards cannot be recycled."
    key = secrets.token_hex(12)
    bot.pending_confirmations = {key:value for key,value in bot.pending_confirmations.items() if value["expires"] > time.time()}
    args = [guild, user, copies] + ([target] if target else [])
    bot.pending_confirmations[key] = {"guild": guild, "user": user, "method": method, "args": args,
                                      "kwargs": kwargs, "kind": "confirmation", "expires": time.time()+90}
    view = action_view([("Confirm consumption", "confirm", key, discord.ButtonStyle.danger),
                        ("Keep my cards", "discard", key, discord.ButtonStyle.secondary)])
    await reply(interaction, embed=panel("Review before consuming cards", description), view=view)


def regular_craft_card(card):
    return card["category"] == "canon" and not card.get("release") and not Game._event_exclusive(card)


async def select_craft_ingredients(bot, interaction, target):
    from .bot import reply
    await discord_request(interaction.response.defer(ephemeral=True))
    if not regular_craft_card(bot.game.catalog[target]):
        raise ValueError("Launch crafting uses regular canon or approved game-continuity cards. Limited, meme and event cards cannot be crafted.")
    quote = await bot.call("craft_quote", target)
    guild, user = str(interaction.guild_id), str(interaction.user.id)
    rows = []
    # Find up to 25 recent matching copies with bounded reads; explicit IDs
    # remain available for collections larger than the scanned pages.
    for offset in range(0, 1000, 100):
        page = await bot.call("inventory", guild, user, rarity=quote["ingredient_rarity"], limit=100, offset=offset)
        rows.extend(row for row in page if regular_craft_card(row["card"])
                    and (row["card"]["faction"], row["card"]["arena"]) == (quote["faction"], quote["arena"])
                    and not row["favorite"] and not row["locked_by"])
        if len(rows) >= 25 or len(page) < 100:
            break
    rows = rows[:25]
    if len(rows) < quote["ingredients"]:
        raise ValueError(f"This recipe needs {quote['ingredients']} unprotected {quote['ingredient_rarity'].title()} "
                         f"{quote['faction']} {quote['arena']} copies. Not enough matching copies are available in the recent-copy selector; "
                         "use instance_ids for other owned copies.")
    key = secrets.token_hex(12)
    bot.pending_confirmations = {key: value for key, value in bot.pending_confirmations.items() if value["expires"] > time.time()}
    bot.pending_confirmations[key] = {"guild": guild, "user": user, "target": target,
                                      "kind": "craft_selection", "expires": time.time()+90}
    view = discord.ui.View(timeout=90)
    view.add_item(discord.ui.Select(custom_id=f"vv:craftpick:{key}",
                                   placeholder=f"Choose {quote['ingredients']} ingredient copies",
                                   min_values=quote["ingredients"], max_values=quote["ingredients"],
                                   options=[discord.SelectOption(label=row["name"][:100], value=row["id"],
                                            description=f"{row['card']['rarity'].title()} · {row['id']}") for row in rows]))
    await reply(interaction, content=f"Choose **{quote['ingredients']} copies** for **{quote['target_name']}** "
                f"and **{quote['cost']:,} credits**. You will review before consumption. "
                "This selector shows recent matching copies; use instance_ids in the command for other copies.", view=view)


def add_feature_commands(group, bot):
    from .bot import reply

    async def copy_autocomplete(interaction, current):
        return await copy_choices(bot, interaction, current)

    async def deck_autocomplete(interaction, current):
        return await copy_choices(bot, interaction, current, limit=3)

    async def craft_autocomplete(interaction, current):
        return await copy_choices(bot, interaction, current, limit=7)

    async def craft_target_autocomplete(interaction, current):
        if interaction.guild_id != bot.settings.guild_id or interaction.user.bot:
            return []
        query = current.casefold().strip()
        regular = [card for card in bot.game.catalog.values() if regular_craft_card(card)]
        groups = {(card["rarity"], card["faction"], card["arena"]) for card in regular}
        cards = [card for card in regular if card["rarity"] in CRAFT_RECIPES
                 and (RARITIES[RARITIES.index(card["rarity"])-1], card["faction"], card["arena"]) in groups
                 and bot.game.matches_card(card["id"],query)]
        return [app_commands.Choice(name=f"{card['name']} · {card['rarity']}"[:100], value=card["id"])
                for card in cards[:25]]

    async def trade_autocomplete(interaction,current):
        if interaction.guild_id!=bot.settings.guild_id or interaction.user.bot:
            return []
        try:
            records=await asyncio.wait_for(bot.call("active_trades",str(interaction.guild_id),str(interaction.user.id)),1.8)
        except (TimeoutError,sqlite3.OperationalError):
            return []
        return [app_commands.Choice(name=f"Open trade · {record['id']}",value=record['id']) for record in records
                if current.casefold() in record['id']][:25]

    @app_commands.command(name="market",description="Buy one daily market card with credits; review before paying")
    async def market(interaction:discord.Interaction):
        await discord_request(interaction.response.defer(ephemeral=True,thinking=True))
        await show_market(bot,interaction)

    @app_commands.command(name="profile", description="See your collection progress, balance and battle record")
    async def profile(interaction: discord.Interaction):
        await discord_request(interaction.response.defer(ephemeral=True))
        notice = await starter_notice(bot, interaction)
        row = await bot.call("profile", str(interaction.guild_id), str(interaction.user.id))
        embed = panel("Your collection progress", f"Level **{row['level']}** · **{row['xp']} XP**\n"
                      f"**{row['regular_collected_subject_count']} / {row['regular_subject_count']}** regular subjects · {row['regular_subject_completion_percent']}% complete\n"
                      f"**{row['unique_cards']} / {row['catalog_count']}** regular card treatments · **{row['event_unique_cards']} / {row['event_catalog_count']}** event cards\n"
                      f"**{row['total_cards']}** owned copies · **{row['credits']} credits**\n"
                      f"Duel record: {row['wins']} wins / {row['losses']} losses")
        if notice:
            embed.description = notice + "\n\n" + embed.description
        await reply(interaction, embed=embed)

    @app_commands.command(name="daily", description="Collect free daily credits; no voting or paid rolls")
    async def daily(interaction: discord.Interaction):
        await discord_request(interaction.response.defer(ephemeral=True))
        notice = await starter_notice(bot, interaction)
        row = await bot.call("daily", str(interaction.guild_id), str(interaction.user.id))
        await reply(interaction, embed=panel("Daily supply delivered", (notice + "\n\n" if notice else "") + f"**+{row['credits_earned']} credits**\n"
                    f"Balance: {row['credits']} · Streak: {row['streak']} days\nNext supply <t:{int(row['next_reset'])}:R>."))

    @app_commands.command(name="recipes", description="Read exact crafting costs and duplicate recycle values")
    async def recipes(interaction: discord.Interaction):
        await discord_request(interaction.response.defer(ephemeral=True))
        row = await bot.call("recipes")
        values = " · ".join(f"{name.title()} {value}" for name,value in row["salvage_values"].items())
        costs = "\n".join(f"**{rarity.title()}**: {recipe['ingredients']} copies + {recipe['cost']:,} credits"
                          for rarity, recipe in row["targets"].items())
        await reply(interaction, embed=panel("Crafting recipes", f"{row['requirements']}\n"
                    f"\n**Cost by target rarity**\n{costs}\n"
                    "Favorites, escrowed copies and active loadouts are protected. Crafting can consume final copies.\n\n"
                    f"**Duplicate recycling values**\n{values}\nThese are fixed in-game values, not market prices."))

    @app_commands.command(name="salvage", description="Review duplicate copies before recycling them into credits")
    @app_commands.autocomplete(instance_ids=copy_autocomplete)
    async def salvage(interaction: discord.Interaction, instance_ids: str):
        await review_consumption(bot, interaction, "salvage", instance_ids)

    @app_commands.command(name="craft", description="Review the copies and tier-based cost for your chosen upgrade")
    @app_commands.autocomplete(instance_ids=craft_autocomplete, target_card=craft_target_autocomplete)
    async def craft(interaction: discord.Interaction, target_card: str, instance_ids: str = ""):
        if target_card not in bot.game.catalog:
            raise ValueError("Use an exact target card ID from /vault catalog.")
        if instance_ids.strip():
            await review_consumption(bot, interaction, "craft", instance_ids, target_card)
        else:
            await select_craft_ingredients(bot, interaction, target_card)

    @app_commands.command(name="trade", description="Swap cards with another collector; either side may offer nothing")
    @app_commands.autocomplete(trade_id=trade_autocomplete, offer=copy_autocomplete)
    @app_commands.describe(member="Who you want to trade with", offer="Optional: copies you'll put on the table right away",
                           trade_id="Reopen one of your open trades")
    async def trade(interaction: discord.Interaction, member: discord.Member | None = None, offer: str = "", trade_id: str = ""):
        await discord_request(interaction.response.defer(ephemeral=True))
        guild, user = str(interaction.guild_id), str(interaction.user.id)
        if trade_id:
            record = await bot.call("get_trade", trade_id)
            bot.verify_guild(record, interaction)
            if user not in (record["a"],record["b"]):
                raise ValueError("Only the named collectors on this trade can open it.")
            await reply(interaction, embed=trade_panel(record), view=trade_view(record))
            return
        if member is None:
            records = await bot.call("active_trades", guild, user)
            if not records:
                await reply(interaction, content="You have no open trades. Use `/vault trade member:@someone` to start one — you can offer cards, ask for cards, or both.")
                return
            for record in records[:3]:
                await reply(interaction, embed=trade_panel(record), view=trade_view(record))
            return
        if member.bot:
            raise ValueError("Choose a person, not a bot.")
        copies = ids_from_text(offer, TRADE_MAX_PER_SIDE) if offer.strip() else []
        record = await bot.call("trade_create", guild, user, str(member.id))
        if copies:
            record = await bot.call("trade_offer", record["id"], user, copies)
        if record.get("reopened") and await bot.runtime_call("binding", "trade", record["id"]):
            await refresh_trade(bot, record)
            await reply(interaction, content="You already had a trade open with them — here it is.", embed=trade_panel(record), view=trade_view(record))
            return
        message = await discord_request(interaction.channel.send(
            content=f"{member.mention}, <@{user}> opened a trade with you.", embed=trade_panel(record), view=trade_view(record),
            nonce="vv-trade:"+record["id"], allowed_mentions=discord.AllowedMentions(users=[member], everyone=False, roles=False)))
        await bot.runtime_call("bind", "trade", record["id"], interaction.channel_id, message.id)
        await reply(interaction, content=f"Trade table posted and {member.display_name} was pinged: {message.jump_url}")

    @app_commands.command(name="give", description="Give one or more of your cards to another collector")
    @app_commands.autocomplete(copies=copy_autocomplete)
    @app_commands.describe(member="Who receives the cards", copies="Pick up to five copies here (search by name)",
                           note="Optional message shown with the gift")
    async def give(interaction: discord.Interaction, member: discord.Member, copies: str,
                   note: app_commands.Range[str, 0, 200] = ""):
        await discord_request(interaction.response.defer(ephemeral=True))
        if member.bot:
            raise ValueError("Choose a person, not a bot.")
        if member.id == interaction.user.id:
            raise ValueError("You can't give cards to yourself.")
        ids = ids_from_text(copies, GIFT_MAX)
        if not ids:
            raise ValueError("Pick at least one copy.")
        guild, user = str(interaction.guild_id), str(interaction.user.id)
        rows = [await bot.call("info", guild, user, key) for key in ids]
        lines = "\n".join(f"**{row['name']}** · {row['card']['rarity'].title()} · {row['attack']} ATK / {row['health']} HP"
                          + (" · ★ favorite" if row["favorite"] else "") for row in rows)
        key = secrets.token_hex(12)
        bot.pending_confirmations = {k: v for k, v in bot.pending_confirmations.items() if v["expires"] > time.time()}
        bot.pending_confirmations[key] = {"guild": guild, "user": user, "method": "give",
                                          "args": [guild, user, str(member.id), ids],
                                          "kwargs": {"note": note or None, "request_id": key},
                                          "kind": "confirmation", "expires": time.time()+120}
        view = action_view([(f"Give to {member.display_name}"[:80], "confirm", key, discord.ButtonStyle.success),
                            ("Keep my cards", "discard", key, discord.ButtonStyle.secondary)])
        await reply(interaction, embed=panel("Review your gift", f"{lines}\n\nThese copies will move to {member.mention} "
                    "immediately and leave any saved deck. Gifts can't be undone."), view=view)

    @app_commands.command(name="deck", description="View your saved squads or set up to three cards for Ground or Fleet")
    @app_commands.autocomplete(instance_ids=deck_autocomplete)
    @app_commands.choices(arena=[app_commands.Choice(name="Ground",value="ground"),app_commands.Choice(name="Fleet",value="fleet")])
    @app_commands.describe(clear="Remove all cards from this arena's saved deck; your collection stays intact")
    async def deck(interaction: discord.Interaction, arena: str | None = None, instance_ids: str | None = None, clear: bool = False):
        await discord_request(interaction.response.defer(ephemeral=True))
        guild, user = str(interaction.guild_id), str(interaction.user.id)
        if arena is None:
            if clear or instance_ids is not None:
                raise ValueError("Choose Ground or Fleet before changing a saved deck.")
            embed = panel("Your squads", "Ground and Fleet fight separately. Choose a deck below to edit its cards.")
            for selected_arena in ("ground", "fleet"):
                saved = await bot.call("deck", guild, user, selected_arena)
                names = ", ".join(card["name"] for card in saved["cards"]) or "No cards selected"
                embed.add_field(name=f"{selected_arena.title()} · {len(saved['cards'])}/3 cards",
                                value=f"{names}\n**{saved['attack']:,} ATK / {saved['health']:,} HP**", inline=False)
            await reply(interaction, embed=embed, view=action_view([
                ("Edit Ground deck", "openground", "home", discord.ButtonStyle.secondary),
                ("Edit Fleet deck", "openfleet", "home", discord.ButtonStyle.secondary)]))
            return
        if clear and instance_ids is not None:
            raise ValueError("Choose either clear or a new card selection for this deck.")
        if clear:
            record = await bot.call("set_deck", guild, user, arena, [])
        else:
            if instance_ids is None:
                saved = await bot.call("deck", guild, user, arena)
                await reply(interaction, embed=deck_panel(saved), view=action_view([
                    (f"Edit {arena.title()} deck", "openground" if arena == "ground" else "openfleet",
                     "home", discord.ButtonStyle.secondary)]))
                return
            record = (await bot.call("set_deck", guild, user, arena, ids_from_text(instance_ids,3)) if instance_ids is not None
                      else await bot.call("deck", guild, user, arena))
        await reply(interaction, embed=deck_panel(record))

    @app_commands.command(name="duel", description="Challenge a collector with your saved squad; no asset wagers")
    @app_commands.choices(arena=[app_commands.Choice(name="Ground",value="ground"),app_commands.Choice(name="Fleet",value="fleet")])
    async def duel(interaction: discord.Interaction, member: discord.Member, arena: str):
        await discord_request(interaction.response.defer(ephemeral=True))
        if member.bot:
            raise ValueError("Choose a collector, not another bot.")
        record = await bot.call("duel_create", str(interaction.guild_id), str(interaction.user.id), str(member.id), arena)
        message = await discord_request(interaction.channel.send(embed=duel_panel(record), view=duel_view(record),
                                      nonce="vv-duel:"+record["id"], allowed_mentions=discord.AllowedMentions.none()))
        await bot.runtime_call("bind", "duel", record["id"], interaction.channel_id, message.id)
        await reply(interaction, content=f"Challenge opened: {message.jump_url}")

    @app_commands.command(name="raid", description="Inspect the current cooperative encounter and join or attack")
    @app_commands.describe(bosses="Show the boss roster and raid rules instead of the active encounter")
    async def raid(interaction: discord.Interaction, bosses: bool = False):
        await discord_request(interaction.response.defer(ephemeral=True, thinking=True))
        if bosses:
            roster = await bot.call("raid_bosses")
            embed = panel("Raid bosses", "Ground and Fleet use separate saved decks. Three attacks per collector; "
                          "bosses retaliate against non-lethal hits. Guardian cards reduce damage, Support repairs "
                          "surviving squads, and faster squads take less damage. Your owned cards remain safe.")
            for arena in ("ground", "fleet"):
                embed.add_field(name=arena.title(), value="\n".join(
                    f"**{card['name']}** · {card['raid_boss']['difficulty'].title()}"
                    for card in roster if card["arena"] == arena) or "No named bosses configured.", inline=False)
            embed.add_field(name="Rewards", value="Victory only: a shared credit pool based on difficulty and contributing "
                            "squad power, divided by damage and participation. XP scales with effort. Joining alone earns "
                            "nothing; raids never mint cards. Use /vault deck to prepare.", inline=False)
            await reply(interaction, embed=embed)
            return
        rows = await bot.call("active_raids", str(interaction.guild_id))
        if not rows:
            await reply(interaction, embed=panel("No raid running", "Build a deck now so you're ready for the next raid. Choose Ground or Fleet below."),view=start_view())
        else:
            await reply(interaction, embed=raid_panel(rows[0]), view=raid_view(rows[0]))

    gamble = app_commands.Group(name="gamble", description="Bet credits on coin flips, slots or a wager with a friend")

    def bettor(interaction):
        return getattr(interaction.user, "mention", f"<@{interaction.user.id}>")

    @gamble.command(name="coinflip", description=f"Call it: win double your bet ({GAMBLE_MIN}–{GAMBLE_MAX:,} credits)")
    @app_commands.choices(side=[app_commands.Choice(name="Heads", value="heads"), app_commands.Choice(name="Tails", value="tails")])
    async def coinflip(interaction: discord.Interaction, amount: app_commands.Range[int, GAMBLE_MIN, GAMBLE_MAX], side: str):
        await discord_request(interaction.response.defer(thinking=True))
        result = await bot.call("coinflip", str(interaction.guild_id), str(interaction.user.id), amount, side,
                                request_id=str(interaction.id))
        head = (f"{bettor(interaction)} bet **{amount:,}** on {side}. 🪙 It landed **{result['landed']}**: "
                + (f"**won {result['payout']:,} credits!**" if result["won"] else "lost."))
        embed = panel("Coin flip", head + f"\nBalance: **{result['credits']:,}** · Bet today: {result['wagered_today']:,}/{result['daily_cap']:,}")
        embed.set_footer(text=GAMBLE_FOOTER)
        await reply(interaction, embed=embed, ephemeral=False)

    @gamble.command(name="slots", description=f"Spin the Kessel Run slots ({GAMBLE_MIN}–{GAMBLE_MAX:,} credits)")
    async def slots(interaction: discord.Interaction, amount: app_commands.Range[int, GAMBLE_MIN, GAMBLE_MAX]):
        await discord_request(interaction.response.defer(thinking=True))
        result = await bot.call("slots", str(interaction.guild_id), str(interaction.user.id), amount, request_id=str(interaction.id))
        reels = " ".join(SLOT_ICONS.get(name, "❔") for name in result["reels"])
        line = (f"**{result['multiplier']:g}× — won {result['payout']:,} credits!**" if result["payout"] else "No match this time.")
        if result.get("jackpot"):
            line = "🎰 **JACKPOT!** " + line
        table = " · ".join(f"{SLOT_ICONS[name]}×3 = {pay}×" for name, _, pay in SLOT_SYMBOLS)
        embed = panel("Kessel Run slots", f"{bettor(interaction)} bet **{amount:,}**\n# {reels}\n{line}\nBalance: **{result['credits']:,}** · Bet today: "
                      f"{result['wagered_today']:,}/{result['daily_cap']:,}\n-# {table} · any pair 1.2× · 💎 pair 5×")
        embed.set_footer(text=GAMBLE_FOOTER)
        await reply(interaction, embed=embed, ephemeral=False)

    @gamble.command(name="wager", description="Challenge someone to a coin toss for credits; winner takes the pot")
    async def wager(interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, GAMBLE_MIN, GAMBLE_MAX]):
        await discord_request(interaction.response.defer(thinking=True))
        if member.bot or member.id == interaction.user.id:
            raise ValueError("Choose another person, not yourself or a bot.")
        record = await bot.call("wager_create", str(interaction.guild_id), str(interaction.user.id), str(member.id), amount)
        await discord_request(interaction.followup.send(
            content=f"{member.mention}, you've been challenged!", embed=wager_panel(record), view=wager_view(record),
            allowed_mentions=discord.AllowedMentions(users=[member], everyone=False, roles=False)))

    @gamble.command(name="stats", description="Your betting record and today's remaining limit")
    async def gamble_stats(interaction: discord.Interaction):
        await discord_request(interaction.response.defer(thinking=True))
        row = await bot.call("gamble_stats", str(interaction.guild_id), str(interaction.user.id))
        embed = panel("Betting record", f"{bettor(interaction)}\n**{row['bets']}** bets · {row['wagered']:,} wagered · {row['won']:,} paid out\n"
                      f"Net: **{row['net']:+,} credits** · Biggest payout: {row['biggest_payout']:,}\n"
                      f"Wagers: {row['wager_wins']} won of {row['wagers']}\n"
                      f"Bet today: {row['wagered_today']:,}/{row['daily_cap']:,} · Balance: **{row['credits']:,}**")
        embed.set_footer(text=GAMBLE_FOOTER)
        await reply(interaction, embed=embed, ephemeral=False)

    for command in (profile,daily,recipes,salvage,craft,trade,give,deck,duel,raid,market,gamble):
        group.add_command(command)


async def reconcile_raids(bot):
    """Fair bounded restore passes; one missing channel cannot starve others."""
    if not hasattr(bot, "display_versions"):
        bot.display_versions = {}
    if not hasattr(bot, "display_attempts"):
        bot.display_attempts = {}
    if not hasattr(bot, "display_failures"):
        bot.display_failures = set()
    if not hasattr(bot, "recovery_started_at"):
        bot.recovery_started_at = time.time()
    jobs = []
    for drop in await bot.call("recent_drops", str(bot.settings.guild_id), limit=30):
        signature = (drop["status"], len(drop["claims"]), drop.get("delivery_pending"), drop["expires_at"])
        if drop.get("message_id") and bot.display_versions.get("drop:"+drop["id"]) != signature:
            jobs.append(("drop", drop, signature))
    for raid in await bot.call("recent_raids", str(bot.settings.guild_id), limit=10):
        signature = (raid["status"], raid["hp"], len(raid["participants"]))
        if raid.get("message_id") and bot.display_versions.get("raid:"+raid["id"]) != signature:
            jobs.append(("raid", raid, signature))
    for kind in ("trade", "duel"):
        jobs.extend((kind, row, None) for row in await bot.runtime_call("bindings", kind))

    def key(job):
        return job[0] + ":" + (job[1].get("entity_id") or job[1]["id"])

    def attempted(job):
        # Binding queries rotate through the full durable backlog. Their saved
        # attempt clock survives eviction from the bounded presentation cache.
        return key(job) in bot.display_attempts or (
            job[0] in ("trade", "duel")
            and job[1].get("last_refresh", 0) >= bot.recovery_started_at)

    jobs.sort(key=lambda job: (attempted(job), bot.display_attempts.get(key(job), 0)))
    slots = asyncio.Semaphore(4)

    async def restore(job):
        kind, record, signature = job
        cache_key = key(job)
        terminal = False
        success = False
        async with slots:
            try:
                if kind == "drop":
                    success = await bot.refresh_drop(record)
                elif kind == "raid":
                    success = await refresh_raid(bot, record)
                else:
                    record = await bot.call("get_"+kind, record["entity_id"])
                    if str(record["guild_id"]) != str(bot.settings.guild_id):
                        raise ValueError("Saved controls belong to another server.")
                    signature = ((record["status"], record["version"], tuple(record["accepted"].values()))
                                 if kind == "trade" else record["status"])
                    terminal = record["status"] != ("open" if kind == "trade" else "pending")
                    success = (True if bot.display_versions.get(cache_key) == signature else
                               await (refresh_trade(bot, record) if kind == "trade" else refresh_duel(bot, record)))
                if success:
                    bot.display_versions[cache_key] = signature
                    bot.display_failures.discard(cache_key)
                else:
                    bot.display_failures.add(cache_key)
            except (GameError, ValueError, *DISCORD_ERRORS) as error:
                bot.display_failures.add(cache_key)
                log.warning("Saved %s controls will retry: %s", kind, type(error).__name__)
            finally:
                bot.display_attempts[cache_key] = time.monotonic()
                bot.mark_scheduler_progress()
                if kind in ("trade", "duel"):
                    await bot.runtime_call("refreshed", kind, job[1]["entity_id"], terminal=bool(success and terminal))

    await asyncio.gather(*(restore(job) for job in jobs[:RECONCILE_BATCH]))
    first_pass_finished = not bot.recovery_pending or all(attempted(job) for job in jobs)
    for mapping in (bot.display_versions, bot.display_attempts):
        while len(mapping) > 150:
            mapping.pop(next(iter(mapping)))
    bot.display_failures.intersection_update(bot.display_attempts)
    return first_pass_finished


async def refresh_trade(bot, trade):
    binding = await bot.runtime_call("binding", "trade", trade["id"])
    if not binding:
        return
    lock = bot.refresh_lock("trade-"+trade["id"])
    async with lock:
        trade = await bot.call("get_trade", trade["id"])
        try:
            channel = bot.get_channel(int(binding["channel_id"])) or await discord_request(bot.fetch_channel(int(binding["channel_id"])))
            await discord_request(channel.get_partial_message(int(binding["message_id"])).edit(embed=trade_panel(trade), view=trade_view(trade)))
            return True
        except DISCORD_ERRORS as error:
            log.warning("Trade display will retry: %s", type(error).__name__)
            return False


async def refresh_duel(bot, duel):
    binding = await bot.runtime_call("binding", "duel", duel["id"])
    if not binding:
        return
    async with bot.refresh_lock("duel-"+duel["id"]):
        current = await bot.call("get_duel", duel["id"])
        try:
            channel = bot.get_channel(int(binding["channel_id"])) or await discord_request(bot.fetch_channel(int(binding["channel_id"])))
            await discord_request(channel.get_partial_message(int(binding["message_id"])).edit(
                embed=duel_panel(current), view=duel_view(current),
                attachments=[duel_log(current)] if current.get("result") else [],
                allowed_mentions=discord.AllowedMentions.none()))
            return True
        except DISCORD_ERRORS as error:
            log.warning("Duel display will retry: %s", type(error).__name__)
            return False
