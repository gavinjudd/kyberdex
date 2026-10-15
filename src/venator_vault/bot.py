"""Discord gateway adapter. No message-content or member-list intent is needed."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import os
import re
import secrets
import signal
import socket
import time
from contextlib import closing

import discord
from discord import app_commands
from discord.ext import tasks

from .card_render import CardRenderer
from .config import Settings
from .core import ALL_RARITIES, RARITIES, Game, GameError, collector_limit
from .presentation import (
    DISCORD_ERRORS,
    art_path,
    card_panel,
    discord_request,
    drop_panel,
    drop_view,
    panel,
)
from .runtime import Runtime, spawn_bounds

log = logging.getLogger("venator_vault")
LOGIN_TIMEOUT = 45.0
LOGIN_RETRY_DELAY = 30.0
SYNC_RETRY_DELAY = 60.0
CLOSE_TIMEOUT = 30.0
SCHEDULER_STALE_AFTER = 75.0
ACTIVITY_ADMISSION_COOLDOWN = 3.0
MAX_ACTIVITY_AUTHORS = 512


def notify_systemd(message):
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
            channel.setblocking(False)
            channel.connect(address)
            channel.send(message.encode())
    except OSError as error:
        log.warning("Service notification unavailable: %s", type(error).__name__)

CARD_IMAGE_FIELDS = (
    "name", "id", "rarity", "category", "availability", "event_id", "release", "arena", "role",
    "faction", "era", "set", "attack", "health", "speed", "ability", "ability_name", "ability_description",
    "art", "art_status", "art_layout",
)


HINT_UNLOCK = 20
HINT_LETTER = 45
HINT_MASK = 110


def capture_hint(drop, age):
    """Progressive private hints; they never change the capture window."""
    card = drop["card"]
    hint = f"**{card['faction']}** · {card['era']} · {card['arena'].title()}"
    name = re.split(r"\s+[—–]\s+", card["name"], maxsplit=1)[0]
    words = name.split()
    if age >= HINT_MASK:
        masked = " ".join(word[0] + "".join("·" if ch.isalnum() else ch for ch in word[1:]) for word in words)
        hint += f"\nName pattern: **{masked}**"
    elif age >= HINT_LETTER:
        hint += (f"\nThe name starts with **{name[0]}** and has **{len(words)}** word{'s' if len(words) != 1 else ''}.")
    hint += ("\nPunctuation and capitalization don't matter, and a small typo in a longer name is fine."
             f"\nCapture stays open until <t:{int(drop['expires_at'])}:R>, unless all slots are claimed.")
    return hint


def collector_rules():
    limits = " · ".join(f"{rarity.title()} {collector_limit({'rarity': rarity, 'category': 'canon'})}"
                        for rarity in RARITIES)
    return (f"Collector limits: {limits}. One copy per distinct collector. "
            "Limited editions follow rarity; an event may publish an override. "
            "The encounter shows its actual slots, including any reduction for remaining edition stock.")


async def reply(interaction: discord.Interaction, *, content=None, embed=None, view=None, file=None, ephemeral=True):
    kwargs = {"ephemeral": ephemeral, "allowed_mentions": discord.AllowedMentions.none()}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if view is not None:
        kwargs["view"] = view
    if file is not None:
        kwargs["file"] = file
    if interaction.response.is_done():
        return await discord_request(interaction.followup.send(**kwargs))
    return await discord_request(interaction.response.send_message(**kwargs))


class CaptureModal(discord.ui.Modal, title="Identify this encounter"):
    answer = discord.ui.TextInput(label="Card name", placeholder="Name, nickname, or a unique first/last name", max_length=100,
                                  custom_id="card-name")

    def __init__(self, bot: VaultBot, drop_id: str):
        super().__init__(timeout=180, custom_id=f"vv:capture-submit:{drop_id}")
        self.bot, self.drop_id = bot, drop_id

    async def on_submit(self, interaction):
        if not self.bot.begin_modal_submission(interaction):
            return
        if not await self.bot.valid_context(interaction):
            return
        await discord_request(interaction.response.defer(ephemeral=True, thinking=True))
        try:
            drop = await self.bot.call("get_drop", self.drop_id)
            self.bot.verify_guild(drop, interaction)
            result = await self.bot.call("claim", self.drop_id, str(interaction.user.id), str(self.answer),
                                         request_id=str(interaction.id) if getattr(interaction, "id", None) else None)
            instance = result["instance"]
            embed = card_panel(instance["card"], instance)
            embed.title = (("Previously captured · " if result.get("replayed") else "Captured · ") + instance["name"])[:256]
            receipt = (f"{result['remaining']} of {result['drop']['collector_limit']} capture slots remain.\n"
                       if result["drop"]["status"] == "active" else "This encounter has ended.\n")
            if result.get("replayed"):
                receipt += "No additional card or rewards issued. Use `/vault collection` for current ownership.\n"
            biography = embed.description or ""
            budget = min(4096, max(0, 6000 - (len(embed) - len(biography))))
            embed.description = (receipt + biography)[:budget] or None
            file = await self.bot.card_image(instance["card"],instance)
            if file is not None:
                embed.set_image(url=f"attachment://{file.filename}")
            await reply(interaction, embed=embed,file=file)
            await self.bot.refresh_drop(result["drop"])
        except (GameError, ValueError) as error:
            await reply(interaction, content=str(error))

    async def on_error(self, interaction, error):
        log.error("Capture interaction failed: %s", type(error).__name__)
        await reply(interaction, content="That capture could not finish. Please try again; existing captures are safe.")


class VaultBot(discord.Client):
    def __init__(self, settings: Settings):
        settings.require_discord_configuration()
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none(), max_messages=100)
        self.settings = settings
        self.game = Game(settings.database, settings.catalog, enabled_events=settings.enabled_events,
                         enabled_releases=settings.enabled_releases)
        self.runtime = Runtime(settings.database.with_name("runtime.sqlite3"), minimum_delay=settings.drop_duration)
        configured = self.runtime.get(settings.guild_id)
        if configured and configured["interval_seconds"] < settings.drop_duration:
            raise ValueError("The saved encounter interval cannot be shorter than the capture window.")
        self.card_renderer = CardRenderer(settings.root)
        self.card_render_slots = asyncio.Semaphore(2)
        self.tree = app_commands.CommandTree(self)
        self.post_lock = asyncio.Lock()
        self._admin_card_cache = None
        self._admin_card_task = None
        self._activity_task = None
        self._activity_last_admitted = {}
        # Fixed lock stripes bound memory while serializing updates to one entity.
        self.refresh_locks = tuple(asyncio.Lock() for _ in range(64))
        self.pending_confirmations = {}
        self.started_at = time.time()
        self.last_tick = None
        self.last_error = None
        self.commands_synced = False
        self.next_command_sync = 0.0
        self.display_versions = {}
        self.display_attempts = {}
        self.display_failures = set()
        self.recovery_pending = True
        self.recovery_started_at = time.time()
        self.handled_modals = {}
        self._driver_task = None
        self._heartbeat_task = None
        self._stopping = False
        self._stopped = False
        self._scheduler_started = False
        self._scheduler_entered = False
        self.scheduler_progress = time.monotonic()
        self.tree.add_command(VaultCommands(self))
        self.tree.add_command(AdminCommands(self))
        self.tree.on_error = self.command_error

    async def call(self, method, *args, **kwargs):
        return await asyncio.to_thread(getattr(self.game, method), *args, **kwargs)

    async def runtime_call(self, method, *args, **kwargs):
        # Snapshot writer locks can briefly block either SQLite database.
        # Keep those waits away from Discord's interaction/heartbeat event loop.
        return await asyncio.to_thread(getattr(self.runtime, method), *args, **kwargs)

    async def admin_card_ids(self):
        """Cache reviewed catalog IDs; recheck the chosen asset before delivery."""
        manifest = self.settings.root / 'assets/art/manifest.json'
        stamp = (manifest.stat().st_mtime_ns, manifest.stat().st_size)
        if self._admin_card_cache and self._admin_card_cache[0] == stamp:
            return self._admin_card_cache[1]

        def reviewed():
            from .__main__ import _art_manifest, verified_art_path
            assets = _art_manifest(self.settings.root)
            accepted = set()
            for card in self.game.catalog.values():
                try:
                    verified_art_path(self.settings.root, card, assets)
                except (ValueError, OSError):
                    continue
                accepted.add(card['id'])
            return frozenset(accepted)

        if self._admin_card_task is None or self._admin_card_task[0] != stamp:
            self._admin_card_task = (stamp, asyncio.create_task(asyncio.to_thread(reviewed)))
        accepted = await asyncio.shield(self._admin_card_task[1])
        self._admin_card_cache = (stamp, accepted)
        return accepted

    async def card_image(self, card, instance=None):
        """Render public card details only; image delivery cannot undo a committed action."""
        public = {key: card[key] for key in CARD_IMAGE_FIELDS if key in card}
        if instance:
            public.update({key: instance[key] for key in ("attack", "health", "speed") if key in instance})
        try:
            kind = self.card_renderer.media_kind(public)
            async with self.card_render_slots:
                try:
                    rendered = await asyncio.to_thread(self.card_renderer.render, public, kind=kind)
                except Exception as error:
                    if kind == "png":
                        raise
                    log.warning("Animated card unavailable, using still: %s", type(error).__name__)
                    kind = "png"
                    rendered = await asyncio.to_thread(self.card_renderer.render, public, kind=kind)
            return discord.File(io.BytesIO(rendered), filename=f"card.{kind}")
        except Exception as error:  # noqa: BLE001 - committed captures/crafts keep their successful receipt
            log.warning("Composed card image unavailable: %s", type(error).__name__)
            return None

    def refresh_lock(self, key):
        return self.refresh_locks[hash(key) % len(self.refresh_locks)]

    def begin_modal_submission(self, interaction):
        # The view dispatcher and restart-safe raw router see the same event.
        key = getattr(interaction, "id", id(interaction))
        if key in self.handled_modals:
            return False
        self.handled_modals[key] = None
        if len(self.handled_modals) > 256:
            self.handled_modals.pop(next(iter(self.handled_modals)))
        return True

    async def start(self, token, *, reconnect=True):
        self._driver_task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        signal_installed = False
        try:
            loop.add_signal_handler(signal.SIGTERM, self.request_shutdown)
            signal_installed = True
        except (NotImplementedError, RuntimeError, ValueError):
            pass
        self._heartbeat_task = asyncio.create_task(self.service_heartbeat(), name="vault-service-heartbeat")
        self.write_health()
        notify_systemd("READY=1\nSTATUS=Local state ready; connecting to Discord")
        try:
            while not self._stopping:
                try:
                    async with asyncio.timeout(LOGIN_TIMEOUT):
                        await self.login(token)
                    break
                except DISCORD_ERRORS as error:
                    self.last_error = type(error).__name__
                    log.warning("Discord login postponed: %s", type(error).__name__)
                    await asyncio.sleep(LOGIN_RETRY_DELAY)
            if not self._stopping:
                await self.connect(reconnect=reconnect)
        except asyncio.CancelledError:
            if not self._stopping:
                raise
        finally:
            await self.close()
            if signal_installed:
                loop.remove_signal_handler(signal.SIGTERM)

    def request_shutdown(self):
        self._stopping = True
        if self._driver_task and not self._driver_task.done():
            self._driver_task.cancel()

    def mark_scheduler_progress(self):
        self.scheduler_progress = time.monotonic()

    def watchdog_healthy(self):
        required = self._scheduler_started and (self._scheduler_entered or not self.scheduler.is_running())
        return not required or time.monotonic() - self.scheduler_progress <= SCHEDULER_STALE_AFTER

    async def service_heartbeat(self):
        try:
            watchdog_seconds = int(os.environ.get("WATCHDOG_USEC", "120000000")) / 1_000_000
        except ValueError:
            watchdog_seconds = 120
        interval = min(25, max(0.1, watchdog_seconds / 3))
        while not self._stopping:
            self.write_health()
            if self.watchdog_healthy():
                notify_systemd("WATCHDOG=1")
            else:
                notify_systemd("STATUS=Scheduler stalled; awaiting supervisor recovery")
            await asyncio.sleep(interval)

    async def sync_commands(self):
        try:
            await discord_request(self.tree.sync(guild=discord.Object(id=self.settings.guild_id)))
        except DISCORD_ERRORS as error:
            self.last_error = type(error).__name__
            self.next_command_sync = time.monotonic() + SYNC_RETRY_DELAY
            log.warning("Command registration will retry: %s", type(error).__name__)
            return False
        self.commands_synced = True
        return True

    async def setup_hook(self):
        # Guild-only registration keeps development changes immediate and scoped.
        await self.admin_card_ids()
        guild = discord.Object(id=self.settings.guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.sync_commands()
        if not self.scheduler.is_running():
            self.mark_scheduler_progress()
            self.scheduler.start()
            self._scheduler_started = True

    async def on_ready(self):
        self.display_versions.clear()
        self.display_attempts.clear()
        self.display_failures.clear()
        self.recovery_pending = True
        self.recovery_started_at = time.time()
        log.info("Gateway ready; restoring saved encounter controls.")
        try:
            await discord_request(self.change_presence(activity=discord.Game(name="/vault help · collect Star Wars cards")))
        except DISCORD_ERRORS as error:
            log.warning("Presence update postponed: %s", type(error).__name__)

    async def on_disconnect(self):
        self.recovery_pending = True
        self.write_health()

    async def on_resumed(self):
        self.display_versions.clear()
        self.display_attempts.clear()
        self.display_failures.clear()
        self.recovery_pending = True
        self.recovery_started_at = time.time()

    async def on_message(self, message):
        # Discord supplies these metadata fields without message-content or
        # member-list access. No text, attachments or private channels are read.
        if (self._stopping or not message.guild or message.guild.id != self.settings.guild_id
            or message.channel.id not in self.settings.activity_channel_ids
            or message.author.bot or message.webhook_id is not None
            or message.type not in (discord.MessageType.default, discord.MessageType.reply)
            or getattr(message, "interaction_metadata", None) is not None):
            return
        if not message.channel.permissions_for(message.guild.default_role).view_channel:
            return
        now = time.time()
        if now-message.author.created_at.timestamp() < 7*86400:
            return
        if not 0 <= now-message.created_at.timestamp() <= 60:
            return
        # Chat is expendable scheduling input: never queue it behind a slow
        # storage worker or let it occupy every worker needed by captures.
        if self._activity_task is not None and not self._activity_task.done():
            return
        monotonic_now = time.monotonic()
        user_id = message.author.id
        previous = self._activity_last_admitted.get(user_id)
        if previous is not None and monotonic_now-previous < ACTIVITY_ADMISSION_COOLDOWN:
            return
        self._activity_last_admitted.pop(user_id, None)
        self._activity_last_admitted[user_id] = monotonic_now
        if len(self._activity_last_admitted) > MAX_ACTIVITY_AUTHORS:
            self._activity_last_admitted.pop(next(iter(self._activity_last_admitted)))
        self._activity_task = asyncio.create_task(
            self.record_message_activity(message.guild.id, user_id, message.id, now))
        # Cancelling a handler cannot free admission while its SQLite thread
        # still runs. The tracked task owns that slot until the call completes.
        await asyncio.shield(self._activity_task)

    async def record_message_activity(self, guild_id, user_id, message_id, now):
        try:
            await self.runtime_call("record_activity", guild_id, user_id, message_id, now=now)
        except Exception as error:  # noqa: BLE001 - activity failure must not disrupt gateway dispatch
            log.warning("Activity contribution skipped: %s", type(error).__name__)

    async def close(self):
        if self._stopped:
            return
        self._stopping = True
        notify_systemd("STOPPING=1\nSTATUS=Closing Discord; saved state retained")
        self.scheduler.cancel()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)
        try:
            async with asyncio.timeout(CLOSE_TIMEOUT):
                await super().close()
        except TimeoutError:
            log.warning("Discord close timed out; process supervisor will finish shutdown.")
        finally:
            self._stopped = True
            self.write_health()

    async def valid_context(self, interaction):
        if interaction.guild_id != self.settings.guild_id or interaction.user.bot:
            await reply(interaction, content="KyberDex is available to members in its configured server.")
            return False
        return True

    @staticmethod
    def verify_guild(record, interaction):
        if str(record["guild_id"]) != str(interaction.guild_id):
            raise ValueError("That encounter belongs to another server.")

    async def command_error(self, interaction, error):
        underlying = getattr(error, "original", error)
        if isinstance(underlying, (GameError, ValueError)):
            message = str(underlying)
        elif isinstance(underlying, app_commands.MissingPermissions):
            message = "This operation needs Manage Server permission."
        elif isinstance(underlying, app_commands.CommandOnCooldown):
            message = f"Please wait {underlying.retry_after:.0f} seconds before trying again."
        else:
            log.error("Command failed: %s", type(underlying).__name__)
            message = "That action could not finish. Your collection is safe; please try again shortly."
        await reply(interaction, content=message)

    async def on_interaction(self, interaction):
        if interaction.type == discord.InteractionType.modal_submit:
            custom_id = (interaction.data or {}).get("custom_id", "")
            if custom_id.startswith("vv:capture-submit:"):
                drop_id = custom_id.removeprefix("vv:capture-submit:")
                values = [item.get("value", "") for row in interaction.data.get("components", [])
                          for item in row.get("components", []) if item.get("custom_id") == "card-name"]
                if len(values) == 1 and len(values[0]) <= 100:
                    modal = CaptureModal(self, drop_id)
                    modal.answer._value = values[0]
                    try:
                        await modal.on_submit(interaction)
                    except Exception as error:  # noqa: BLE001 - match Discord's modal error boundary
                        await modal.on_error(interaction, error)
                return
            if custom_id.startswith("vv:offer-submit:"):
                from .features import OfferModal
                values = [item.get("value", "") for row in interaction.data.get("components", [])
                          for item in row.get("components", []) if item.get("custom_id") == "copies"]
                if len(values) == 1 and len(values[0]) <= 200:
                    modal = OfferModal(self, custom_id.removeprefix("vv:offer-submit:"))
                    modal.copies._value = values[0]
                    await modal.on_submit(interaction)
            return
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = (interaction.data or {}).get("custom_id", "")
        if not custom_id.startswith("vv:") or not await self.valid_context(interaction):
            return
        try:
            _, action, entity = custom_id.split(":", 2)
            if action in ("capture", "hint"):
                if action == "capture":
                    # Opening a blank modal discloses nothing; validation happens
                    # after its submit is acknowledged, outside Discord's 3s limit.
                    await discord_request(interaction.response.send_modal(CaptureModal(self, entity)))
                    return
                # Component defers otherwise target the shared encounter message.
                # Hints must have their own private response, like capture receipts.
                await discord_request(interaction.response.defer(ephemeral=True, thinking=True))
                drop = await self.call("get_drop", entity)
                self.verify_guild(drop, interaction)
                if drop.get("delivery_pending"):
                    message = ("This encounter is getting ready. Capture and hints open shortly."
                               if drop["status"] == "active" else
                               "This encounter could not be delivered in time. Captures did not open.")
                    await reply(interaction, content=message)
                    return
                if drop["status"] != "active" or drop["expires_at"] <= time.time():
                    await reply(interaction, content=f"This encounter has ended. It was **{drop['card']['name']}**.")
                    return
                age = time.time() - drop["created_at"]
                if age < HINT_UNLOCK:
                    await reply(interaction, content=f"A hint unlocks in {max(1, int(HINT_UNLOCK-age))} seconds. You can still capture now.")
                else:
                    await reply(interaction, content=capture_hint(drop, age))
                return
            if action in ("confirmclear", "cancelclear"):
                await self.handle_inventory_clear(interaction, action, entity)
                return
            from .features import handle_component
            await handle_component(self, interaction, action, entity)
        except (GameError, ValueError) as error:
            await reply(interaction, content=str(error))
        except Exception as error:  # noqa: BLE001 - interaction boundary must return a safe response
            log.error("Component failed: %s", type(error).__name__)
            await reply(interaction, content="That control could not finish. Use the matching /vault command to reopen it.")

    async def handle_inventory_clear(self, interaction, action, key):
        pending = self.pending_confirmations.get(key)
        if not pending or pending.get("kind") != "inventory_clear" or pending["expires"] < time.time():
            raise ValueError("This confirmation expired. Run /vault-admin clear-inventory again.")
        if (pending["user"], pending["guild"]) != (str(interaction.user.id), str(interaction.guild_id)):
            raise ValueError("Only the manager who started this clear can confirm it.")
        if not getattr(getattr(interaction, "permissions", None), "manage_guild", False):
            raise ValueError("This needs Manage Server permission.")
        self.pending_confirmations.pop(key, None)
        if action == "cancelclear":
            await reply(interaction, content="Cancelled. Nothing was removed.")
            return
        await discord_request(interaction.response.defer(ephemeral=True))
        result = await self.call("admin_clear_inventory", pending["guild"], pending["user"], pending["target"], request_id=key)
        await self.runtime_call("audit", interaction.user.id, "inventory_clear",
                                {"guild_id": pending["guild"], "target": pending["target"], "clear_id": result["id"],
                                 "cards": len(result["instance_ids"])})
        await reply(interaction, content=(f"Cleared **{len(result['instance_ids'])}** cards from <@{pending['target']}>. "
                                          f"Undo reference: `{result['id']}` (use `/vault-admin restore-inventory`)."))

    async def post_drop(self, guild_id, channel_id, card_id=None, *, force=False, rarity=None, request_id=None):
        async with self.post_lock:
            return await self._post_drop_locked(guild_id, channel_id, card_id,
                                               force=force, rarity=rarity, request_id=request_id)

    async def _post_drop_locked(self, guild_id, channel_id, card_id=None, *, force=False, rarity=None, request_id=None):
        config = await self.runtime_call("get", guild_id)
        now = time.time()
        event = await self.runtime_call("event", guild_id)
        if request_id:
            prior = await self.call('drop_for_request', request_id, str(guild_id), str(channel_id))
            if prior:
                intent = json.loads(prior['request_signature'])
                if intent['card_id'] != card_id or intent['rarity'] != rarity:
                    raise GameError('request_conflict', 'This request already selected a different card or rarity.')
                if prior.get('message_id') and not prior.get('delivery_pending'):
                    channel = self.get_channel(int(channel_id)) or await discord_request(self.fetch_channel(int(channel_id)))
                    return channel.get_partial_message(int(prior['message_id']))
                if prior['status'] != 'active':
                    raise GameError('delivery_expired', 'That encounter delivery has expired. Run the command again to make a new request.')
        if config and config["paused"] and not force:
            raise GameError("spawns_paused", "Scheduled encounters are paused.")
        if not force and config and config["next_drop"] <= now:
            latest = await self.call("last_published_drop", str(guild_id))
            if (latest and latest["created_at"] > config["cycle_started_at"]
                and (not event or latest['created_at'] >= event['starts_at'])):
                await self.runtime_call("advance", guild_id, now=latest["created_at"])
                config = await self.runtime_call("get", guild_id)
        if not force and config and config["next_drop"] > now:
            remaining = max(1, math.ceil(config['next_drop'] - now))
            raise GameError("spawn_not_due", f"The next scheduled encounter is due in {remaining} seconds.")
        active = await self.call("active_drops", str(guild_id))
        pending = next((row for row in active if str(row["channel_id"]) == str(channel_id)), None)
        if (pending and pending.get('delivery_pending') and pending.get('override_until')
            and (not event or pending['override_until'] != event['ends_at'])):
            await self.call('cancel_pending_overrides', str(guild_id), pending['override_until'])
            if request_id and pending.get('request_id') == str(request_id):
                raise GameError('delivery_expired', 'That pending event encounter was cancelled when the event stopped.')
            pending = None
        if pending and pending.get("message_id") and not pending.get("delivery_pending"):
            if request_id and pending.get("request_id") == str(request_id):
                channel = self.get_channel(int(channel_id)) or await discord_request(self.fetch_channel(int(channel_id)))
                return channel.get_partial_message(int(pending["message_id"]))
            raise GameError("active_drop", "A capture is already open here. Let it finish before spawning another card.")
        if pending and card_id and pending["card_id"] != card_id:
            raise GameError("active_drop", "Another card is waiting for delivery. Retry it before choosing a different card.")
        if pending and rarity and pending['card']['rarity'] != rarity:
            raise GameError("active_drop", "The pending card has a different rarity. Let its delivery finish first.")
        if card_id and rarity and self.game.catalog.get(card_id, {}).get('rarity') != rarity:
            raise GameError("invalid_rarity", "That card does not match the selected rarity.")
        if not force and config and not await self.runtime_call("activity_ready", guild_id):
            raise GameError("activity_required", "Scheduled encounters wait for recent chat from at least one person.")
        allowed = await self.admin_card_ids() if force or event else None
        if card_id and allowed is not None and card_id not in allowed:
            raise GameError("unavailable_card", "That card has no reviewed artwork ready for spawning.")
        drop = pending or await self.call("create_drop", str(guild_id), str(channel_id), card_id,
                                         duration=self.settings.drop_duration, defer_publication=True,
                                         override_until=event['ends_at'] if event else None,
                                         uniform=bool(event), rarity=rarity, request_id=request_id,
                                         allowed_card_ids=allowed,
                                         anchors_schedule=not bool(config and config['paused']))
        if drop.get('message_id') and not drop.get('delivery_pending'):
            channel = self.get_channel(int(channel_id)) or await discord_request(self.fetch_channel(int(channel_id)))
            return channel.get_partial_message(int(drop['message_id']))
        if force or event or drop.get('override_until'):
            from .__main__ import _art_manifest, verified_art_path
            art = await asyncio.to_thread(verified_art_path, self.settings.root, drop['card'],
                                          _art_manifest(self.settings.root))
        else:
            art = art_path(self.settings.root, drop['card'])
        remaining = drop["delivery_deadline"] - time.time() if drop.get("delivery_pending") else None
        if remaining is not None and remaining <= 0:
            await self.call("get_drop", drop["id"])
            raise GameError("delivery_expired", "This encounter could not be delivered in time.")
        async with asyncio.timeout(remaining):
            channel = self.get_channel(int(channel_id)) or await discord_request(self.fetch_channel(int(channel_id)))
            if not force and config and not await self.runtime_call("activity_ready", guild_id):
                raise GameError("activity_required", "Scheduled encounters wait for recent chat from at least one person.")
            with closing(discord.File(art, filename="encounter.png")) as attachment:
                message = await discord_request(channel.send(embed=drop_panel(drop), view=drop_view(drop),
                                             file=attachment, nonce="vv:" + drop["id"],
                                             allowed_mentions=discord.AllowedMentions.none()))
        if drop.get("delivery_pending"):
            published = await self.call("publish_drop", drop["id"], str(message.id))
        else:
            await self.call("attach_message", drop["id"], str(message.id))
            published = await self.call("get_drop", drop["id"])
        if published.get('schedule_anchor', 1):
            await self.runtime_call("advance", guild_id, now=published["created_at"])
        try:
            await discord_request(message.edit(embed=drop_panel(published), view=drop_view(published),
                               allowed_mentions=discord.AllowedMentions.none()))
        except DISCORD_ERRORS as error:
            log.warning("Published encounter display will retry: %s", type(error).__name__)
        return message

    async def refresh_drop(self, drop):
        if not drop.get("message_id"):
            return
        lock = self.refresh_lock(drop["id"])
        async with lock:
            # Re-read inside the UI lock so racing claims cannot regress the displayed count.
            drop = await self.call("get_drop", drop["id"])
            try:
                channel = self.get_channel(int(drop["channel_id"])) or await discord_request(self.fetch_channel(int(drop["channel_id"])))
                message = channel.get_partial_message(int(drop["message_id"]))
                embed = drop_panel(drop)
                file = discord.File(art_path(self.settings.root,drop["card"]),filename="encounter.png") if drop["claims"] else None
                kwargs = {"embed":embed,"view":drop_view(drop),"allowed_mentions":discord.AllowedMentions.none()}
                if file is not None:
                    kwargs["attachments"]=[file]
                try:
                    await discord_request(message.edit(**kwargs))
                finally:
                    if file is not None:
                        file.close()
                return True
            except discord.NotFound as error:
                # A deleted, finished post cannot be repaired; its saved captures
                # remain valid. Treat it as reconciled instead of retrying forever.
                if error.code == 10008 and drop["status"] != "active":
                    return True
                log.warning("Encounter display is missing; saved captures retained.")
                return False
            except DISCORD_ERRORS as error:
                log.warning("Encounter display will retry; saved captures retained: %s", type(error).__name__)
                return False

    @tasks.loop(seconds=15)
    async def scheduler(self):
        try:
            self._scheduler_entered = True
            self.mark_scheduler_progress()
            if not self.is_ready() or self._stopping:
                self.write_health()
                return
            if not self.commands_synced and time.monotonic() >= self.next_command_sync:
                await self.sync_commands()
                self.mark_scheduler_progress()
            # Restore persisted controls before publishing the next encounter.
            from .features import reconcile_raids
            self.recovery_pending = not await reconcile_raids(self)
            self.mark_scheduler_progress()
            if self.recovery_pending:
                self.last_tick = time.time()
                self.write_health()
                return
            for config in await self.runtime_call("due"):
                if str(config["guild_id"]) != str(self.settings.guild_id):
                    continue
                try:
                    await self.post_drop(config["guild_id"], config["channel_id"])
                except (GameError, ValueError, *DISCORD_ERRORS) as error:
                    if isinstance(error, GameError) and error.code in ("spawn_not_due", "spawns_paused", "activity_required"):
                        continue  # A newer post or pause already changed this due snapshot.
                    self.last_error = type(error).__name__
                    await self.runtime_call("advance", config["guild_id"], retry=True)
                    log.warning("Scheduled encounter postponed: %s", type(error).__name__)
                finally:
                    self.mark_scheduler_progress()
            for config in await self.runtime_call("due_raids"):
                if str(config["guild_id"]) == str(self.settings.guild_id):
                    try:
                        await self.post_raid(config["guild_id"], config["channel_id"])
                    except (GameError, ValueError, *DISCORD_ERRORS) as error:
                        if isinstance(error, GameError) and error.code in ("raid_not_due", "raids_paused"):
                            continue
                        log.warning("Raid postponed: %s", type(error).__name__)
                        await self.runtime_call("advance_raid", config["guild_id"], retry=True)
                    finally:
                        self.mark_scheduler_progress()
            self.last_tick = time.time()
            self.write_health()
        except Exception as error:  # noqa: BLE001 - task loop survives transient failures
            self.last_error = type(error).__name__
            log.error("Scheduler tick failed: %s", type(error).__name__)
            self.write_health()

    @scheduler.before_loop
    async def before_scheduler(self):
        await self.wait_until_ready()

    async def post_raid(self, guild_id, channel_id, arena=None):
        from .features import raid_panel, raid_view
        async with self.post_lock:
            config = await self.runtime_call("get", guild_id)
            if config and config["paused"]:
                raise GameError("raids_paused", "Raids are paused. Resume the schedule before posting a raid.")
            if config and config["next_raid"] <= time.time():
                # Recover the schedule write after a durable Discord attachment,
                # even when the raid already ended during the outage.
                recent = await self.call("recent_raids", str(guild_id), limit=200)
                latest = next((row for row in recent if row.get("message_id")), None)
                if latest and latest["created_at"] + 7200 > config["next_raid"]:
                    await self.runtime_call("advance_raid", guild_id, now=latest["created_at"])
                    config = await self.runtime_call("get", guild_id)
            if config and config["next_raid"] > time.time():
                remaining = max(1, math.ceil(config["next_raid"] - time.time()))
                raise GameError("raid_not_due", f"The next raid is due in {remaining} seconds. "
                                "Manual raids follow the configured schedule.")
            active = await self.call("active_raids", str(guild_id))
            pending = next((row for row in active if str(row["channel_id"]) == str(channel_id)), None)
            if pending and pending.get("message_id"):
                raise ValueError("There is already an active raid in this channel.")
            if arena is None:
                previous = await self.call("recent_raids", str(guild_id), limit=1)
                arena = "fleet" if previous and previous[0]["arena"] == "ground" else "ground"
            raid = pending or await self.call("raid_create", str(guild_id), str(channel_id), arena)
            channel = self.get_channel(int(channel_id)) or await discord_request(self.fetch_channel(int(channel_id)))
            if raid.get("boss"):
                with closing(discord.File(art_path(self.settings.root,raid["boss"]), filename="boss.png")) as attachment:
                    message = await discord_request(channel.send(embed=raid_panel(raid,image_url="attachment://boss.png"),
                        view=raid_view(raid), file=attachment, nonce="vv-raid:" + raid["id"],
                        allowed_mentions=discord.AllowedMentions.none()))
            else:
                message = await discord_request(channel.send(embed=raid_panel(raid), view=raid_view(raid),
                                            nonce="vv-raid:" + raid["id"], allowed_mentions=discord.AllowedMentions.none()))
            attachments = getattr(message,"attachments",[])
            await self.call("attach_raid_message", raid["id"], str(message.id), attachments[0].url if attachments else None)
            await self.runtime_call("advance_raid", guild_id, now=raid["created_at"])
            return message

    def write_health(self):
        path = self.settings.database.parent / "health.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        report = {"ready": self.is_ready() and not self._stopping, "guild_connected": self.get_guild(self.settings.guild_id) is not None,
                  "last_tick": self.last_tick, "last_recorded_error_type": self.last_error,
                  "lifecycle": "stopped" if self._stopped else ("stopping" if self._stopping else ("connected" if self.is_ready() else "reconnecting")),
                  "commands_synced": self.commands_synced, "recovery_pending": self.recovery_pending,
                  "message_refresh_retries": len(self.display_failures),
                  "scheduler_progress_age_seconds": round(time.monotonic() - self.scheduler_progress, 1),
                  "watchdog_healthy": self.watchdog_healthy(),
                  "uptime_seconds": round(time.time() - self.started_at), "catalog_cards": len(self.game.catalog)}
        temporary = path.with_suffix(".new")
        temporary.write_text(json.dumps(report))
        temporary.replace(path)


class VaultCommands(app_commands.Group, name="vault", description="Your Venator card collection, encounters and adventures"):
    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        from .features import add_feature_commands
        add_feature_commands(self, bot)

    async def interaction_check(self, interaction):
        return await self.bot.valid_context(interaction)

    async def autocomplete_card(self, interaction, current):
        if interaction.guild_id != self.bot.settings.guild_id or interaction.user.bot:
            return []
        cards = [c for c in self.bot.game.catalog.values() if self.bot.game.matches_card(c["id"],current)]
        return [app_commands.Choice(name=f"{c['name']} · {c['rarity']}"[:100], value=c["id"]) for c in cards[:25]]

    async def autocomplete_copy(self, interaction, current):
        from .features import copy_choices
        return await copy_choices(self.bot, interaction, current, limit=1)

    async def autocomplete_owned_card(self, interaction, current):
        if interaction.guild_id != self.bot.settings.guild_id or interaction.user.bot:
            return []
        try:
            async with asyncio.timeout(2.0):
                cards = await self.bot.call("owned_cards", str(interaction.guild_id), str(interaction.user.id), current)
        except TimeoutError:
            return []
        return [app_commands.Choice(name=f"{card['name']} · {card['rarity']}"[:100], value=card["id"])
                for card in cards]

    @app_commands.command(name="help", description="Learn captures, collection, crafting and battles")
    async def help_command(self, interaction):
        embed = panel("Welcome to KyberDex", "**A galaxy of cards. Rarer encounters, fewer collector slots.**\n"
                      "Scheduled drops appear in the configured channel. Press Capture and identify the image. "
                      "Each encounter shows how many distinct collectors can win. Names ignore case, spacing and punctuation; "
                      "titles are optional, distinctive name parts work, and a small typo in a longer name is forgiven. Hints unlock after 20 seconds and get stronger over time.")
        for name, value in [
            ("Start collecting", "`/vault collection` — your cards\n`/vault catalog` — the full archive\n`/vault card` — publicly show a card you own"),
            ("Build your vault", "`/vault daily` · `/vault market` · `/vault favorite` · `/vault wishlist`\n`/vault recipes` · `/vault craft` · `/vault salvage`"),
            ("Play together", "`/vault trade` — swap cards (one-sided offers are fine)\n`/vault give` — gift cards to someone\n`/vault gamble` — coin flips, slots and wagers (credits only)\n`/vault deck` · `/vault duel` — Ground and Fleet arenas\n`/vault raid` — join a cooperative encounter"),
            ("Capture rules", collector_rules()),
            ("How drops work", ("Regular drops draw rarity first, then a card within that tier. An admin's equal-chance hour gives each available card the same odds. Favorites stay protected. "
             "After two consecutive captures in this server, wait for another member to capture successfully; taking a second slot counts too. "
             "Trades require both players to confirm the current offer. Automatic card drops require chat from at least one person "
             "chatting in approved public channels within the last five minutes, after the previous drop. Quiet chat means no "
             "automatic cards. Eligible contributions shorten the hidden 12–20 minute base window by five seconds each, "
             "at most one minute total; per-member cooldowns and contribution caps limit repeated messages.")),
        ]:
            embed.add_field(name=name, value=value, inline=False)
        from .features import start_view, starter_notice
        notice = await starter_notice(self.bot, interaction)
        if notice:
            embed.description = notice + "\n\n" + embed.description
        embed.add_field(name="Report a problem",value="`/vault complain message:...` — send feedback privately to the bot's maintainers",inline=False)
        await reply(interaction, embed=embed,view=start_view())

    @app_commands.command(name="catalog", description="Search every card definition, including cards you do not own")
    @app_commands.choices(rarity=[app_commands.Choice(name=rarity.title(),value=rarity) for rarity in ALL_RARITIES])
    @app_commands.autocomplete(search=autocomplete_card)
    async def catalog(self, interaction, search: str = "", rarity: str = "", page: app_commands.Range[int, 1, 100] = 1):
        await discord_request(interaction.response.defer(ephemeral=True))
        from .features import browse_cards
        await browse_cards(self.bot,interaction,"catalog",search,rarity,page)

    @app_commands.command(name="card", description="Publicly show a card you own, including its stats and lore")
    @app_commands.autocomplete(card=autocomplete_owned_card,copy=autocomplete_copy)
    @app_commands.describe(copy="Optional: choose a particular owned copy with its own stat rolls")
    async def card(self, interaction, card: str, copy: str = ""):
        await discord_request(interaction.response.defer(thinking=True))
        instance = (await self.bot.call("info",str(interaction.guild_id),str(interaction.user.id),copy) if copy else
                    await self.bot.call("owned_card", str(interaction.guild_id), str(interaction.user.id), card))
        if instance["card_id"]!=card:
            raise ValueError("Choose a copy of the selected card.")
        definition = instance["card"]
        embed = card_panel(definition, instance)
        file = await self.bot.card_image(definition, instance)
        if file is not None:
            embed.set_image(url=f"attachment://{file.filename}")
        # Rendering runs in a worker; recheck ownership before publishing its result.
        await self.bot.call("info", str(interaction.guild_id), str(interaction.user.id), instance["id"])
        await reply(interaction, embed=embed, file=file, ephemeral=False)

    @app_commands.command(name="collection", description="Browse only the card copies you own")
    @app_commands.choices(rarity=[app_commands.Choice(name=rarity.title(),value=rarity) for rarity in ALL_RARITIES])
    @app_commands.autocomplete(search=autocomplete_owned_card)
    async def collection(self, interaction, search: str = "", rarity: str = "", page: app_commands.Range[int, 1, 1000] = 1):
        await discord_request(interaction.response.defer(ephemeral=True))
        from .features import browse_cards
        await browse_cards(self.bot,interaction,"collection",search,rarity,page)

    @app_commands.command(name="complain",description="Report a problem or suggestion privately to the bot's maintainers")
    @app_commands.describe(message="What went wrong? Include the card or command if you can. Do not include passwords.")
    async def complain(self,interaction,message: app_commands.Range[str,4,1500]):
        await discord_request(interaction.response.defer(ephemeral=True,thinking=True))
        receipt=await self.bot.call("complain",str(interaction.guild_id),str(interaction.user.id),message,request_id=str(interaction.id))
        await reply(interaction,content=f"Saved your report for review. Reference: `{receipt['id']}`. Only server managers and bot maintainers can read it.")

    @app_commands.command(name="favorite", description="Protect or unprotect one of your copies from crafting and salvage")
    @app_commands.autocomplete(instance_id=autocomplete_copy)
    async def favorite(self, interaction, instance_id: str, protected: bool = True):
        await discord_request(interaction.response.defer(ephemeral=True))
        await self.bot.call("set_favorite", str(interaction.guild_id), str(interaction.user.id), instance_id, protected)
        await reply(interaction, content=f"`{instance_id}` is now {'★ protected' if protected else 'unprotected'}.")

    @app_commands.command(name="wishlist", description="Track a card you want, or view your wishlist")
    @app_commands.autocomplete(card=autocomplete_card)
    async def wishlist(self, interaction, card: str = "", wanted: bool = True):
        await discord_request(interaction.response.defer(ephemeral=True))
        if card:
            await self.bot.call("set_wishlist", str(interaction.guild_id), str(interaction.user.id), card, wanted)
        rows = await self.bot.call("wishlist", str(interaction.guild_id), str(interaction.user.id))
        names = [row["name"] for row in rows]
        await reply(interaction, embed=panel("Your wishlist", "\n".join(f"· {name}" for name in names) or "Add a card with the card option."))

    @app_commands.command(name="status", description="See encounter availability and collector limits")
    async def status(self, interaction):
        await discord_request(interaction.response.defer(ephemeral=True))
        config = await self.bot.runtime_call("get", interaction.guild_id)
        text = "A server manager needs to select a channel with `/vault-admin setup`."
        if config:
            active_chat = await self.bot.runtime_call("activity_ready", interaction.guild_id)
            text = ("Spawns are paused." if config["paused"] else
                    "The next encounter arrives within a hidden, variable window." if active_chat else
                    "Automatic card spawns are waiting for recent chat activity; their timing uses a hidden, variable window.")
            text += f"\nChannel: <#{config['channel_id']}>."
            if getattr(getattr(interaction, "permissions", None), "manage_guild", False):
                text += f"\nManager view: earliest scheduled time <t:{int(config['next_drop'])}:R>; chat is also required."
        text += (f"\n{collector_rules()} Automatic cards require conversation from at least one person in approved public "
                 "channels within the last five minutes, after the previous drop. Eligible chat shortens the wait by five "
                 "seconds per contribution, at most one minute total. Per-member cooldowns and contribution caps apply.")
        event = await self.bot.runtime_call('event', interaction.guild_id)
        if event:
            text += (f"\nEqual-chance hour is active until <t:{int(event['ends_at'])}:F>. "
                     "Each available card has equal odds, including limited editions outside their seasonal dates.")
        await reply(interaction, embed=panel("Encounter schedule", text))


class AdminCommands(app_commands.Group, name="vault-admin", description="Configure KyberDex for this server",
                    default_permissions=discord.Permissions(manage_guild=True)):
    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def interaction_check(self, interaction):
        if not await self.bot.valid_context(interaction):
            return False
        if not interaction.permissions.manage_guild:
            await reply(interaction, content="This command requires Manage Server permission.")
            return False
        return True

    async def autocomplete_complaint(self,interaction,current):
        if (interaction.guild_id!=self.bot.settings.guild_id or interaction.user.bot
            or not interaction.permissions.manage_guild):
            return []
        try:
            records=await asyncio.wait_for(self.bot.call("complaints",str(interaction.guild_id)),1.8)
        except (TimeoutError,OSError):
            return []
        return [app_commands.Choice(name=f"Report {record['id']} · player {record['user_id']}",value=record['id'])
                for record in records if current in record['id']][:25]

    async def autocomplete_failed_card(self, interaction, current):
        if (interaction.guild_id != self.bot.settings.guild_id or interaction.user.bot
            or not interaction.permissions.manage_guild):
            return []
        cards = [card for card in self.bot.game.catalog.values()
                 if self.bot.game.matches_card(card['id'], current)]
        cards.sort(key=lambda card: (not card['name'].casefold().startswith(current.casefold()), card['name']))
        return [app_commands.Choice(name=card['name'][:100], value=card['id']) for card in cards[:25]]

    @app_commands.command(name="failed-names", description="Review rejected capture names for missing valid aliases")
    @app_commands.describe(page="Five name groups per page, newest first", card="Optionally filter by expected card")
    @app_commands.autocomplete(card=autocomplete_failed_card)
    async def failed_names(self, interaction, page: app_commands.Range[int, 1, 1000000] = 1, card: str = ""):
        await discord_request(interaction.response.defer(ephemeral=True, thinking=True))
        rows = await self.bot.call("failed_guesses", str(interaction.guild_id), page, card)
        description = (f"Page {page} · Rejected names grouped by expected card and normalized guess.\n"
                       "Review candidates only; logging does not change accepted names. "
                       "The attached JSON includes matching rules for comparison.") if rows else (
                           "No rejected names on this page. Logging starts with this update; earlier guesses were not saved.")
        embed = panel("Rejected capture names", description)
        for row in rows:
            guess = discord.utils.escape_mentions(discord.utils.escape_markdown(row['latest_guess']))
            status = "Accepted by current rules" if row['accepted_now'] else "Still rejected by current rules"
            value = (f"Guess: {guess or '(empty)'}\n"
                     f"{row['attempts']} attempts · {row['players']} players\n"
                     f"First <t:{int(row['first_at'])}:f> · Latest <t:{int(row['last_at'])}:f>\n{status}")
            embed.add_field(name=f"Expected: {row['card_name']}"[:256], value=value[:1024], inline=False)
        payload = {"page": page, "page_size": 5, "card_id": card or None, "groups": rows}
        await reply(interaction, embed=embed, file=discord.File(
            io.BytesIO(json.dumps(payload, ensure_ascii=False, indent=2).encode()), filename="rejected-capture-names.json"))

    @app_commands.command(name="complaints",description="Read private player reports or mark one reviewed")
    @app_commands.autocomplete(resolved_id=autocomplete_complaint)
    async def complaints(self,interaction,page:app_commands.Range[int,1,1000]=1,resolved_id:str=""):
        await discord_request(interaction.response.defer(ephemeral=True,thinking=True))
        if resolved_id:
            await self.bot.call("resolve_complaint",str(interaction.guild_id),str(interaction.user.id),resolved_id)
        rows=await self.bot.call("complaints",str(interaction.guild_id),page)
        embed=panel("Player reports", "No open reports." if not rows else f"Page {page} · Use resolved_id to mark a report reviewed.")
        for row in rows:
            embed.add_field(name=f"{row['id']} · player {row['user_id']}",value=discord.utils.escape_markdown(row['message'])[:1024],inline=False)
        await reply(interaction,embed=embed,file=discord.File(io.BytesIO(json.dumps(rows,ensure_ascii=False,indent=2).encode()),filename="player-reports.json"))

    @app_commands.command(name="setup", description="Select a drop channel and nominal variable spawn interval")
    async def setup(self, interaction, channel: discord.TextChannel,
                    interval_minutes: app_commands.Range[int, 5, 1440] | None = None):
        interval = self.bot.settings.spawn_interval if interval_minutes is None else interval_minutes * 60
        if interval < self.bot.settings.drop_duration:
            raise ValueError("The encounter interval cannot be shorter than the capture window.")
        permissions = channel.permissions_for(interaction.guild.me)
        if not all((permissions.view_channel, permissions.send_messages, permissions.embed_links,
                    permissions.attach_files, permissions.read_message_history)):
            raise ValueError("The bot needs View Channel, Send Messages, Embed Links, Attach Files and Read Message History here.")
        await discord_request(interaction.response.defer(ephemeral=True))
        config = await self.bot.runtime_call("configure", interaction.guild_id, channel.id, interval, interaction.user.id)
        lower, upper = spawn_bounds(interval, self.bot.settings.drop_duration)
        await reply(interaction, content=f"Encounters configured in {channel.mention}. Future cycles randomly sample "
                    f"{lower/60:g}–{upper/60:g} minutes before bounded activity adjustments. Automatic cards also require "
                    f"at least one person chatting within five minutes; speedup is capped at one minute. "
                    f"The saved next deadline is <t:{int(config['next_drop'])}:R>; repeating setup preserves it. "
                    f"{'Spawns remain paused. ' if config['paused'] else ''}{collector_rules()}")

    async def autocomplete_spawn_card(self, interaction, current):
        if (interaction.guild_id != self.bot.settings.guild_id or interaction.user.bot
            or not interaction.permissions.manage_guild):
            return []
        rarity = getattr(getattr(interaction, 'namespace', None), 'rarity', '') or None
        if rarity and rarity not in RARITIES:
            return []
        try:
            async def available():
                allowed = await self.bot.admin_card_ids()
                event = await self.bot.runtime_call('event', interaction.guild_id)
                return await self.bot.call('spawn_pool', override_until=event['ends_at'] if event else None,
                                           uniform=bool(event), rarity=rarity, allowed_card_ids=allowed)
            rows = await asyncio.wait_for(available(), 1.8)
        except (TimeoutError, OSError, ValueError, GameError):
            return []
        rows = [row for row in rows if self.bot.game.matches_card(row['id'], current)]
        rows.sort(key=lambda row: (not row['name'].casefold().startswith(current.casefold()), row['name']))
        return [app_commands.Choice(
            name=(f"{row['name']} · {self.bot.game.catalog[row['id']]['rarity'].title()}"
                  + (f" · {row['remaining_supply']} left" if row['remaining_supply'] is not None else ''))[:100],
            value=row['id']) for row in rows[:25]]

    @app_commands.command(name="spawn", description="Spawn a card immediately; choose a card or rarity")
    @app_commands.autocomplete(card=autocomplete_spawn_card)
    @app_commands.choices(rarity=[app_commands.Choice(name=value.title(), value=value) for value in RARITIES])
    async def spawn(self, interaction, card: str = '', rarity: str = ''):
        await discord_request(interaction.response.defer(ephemeral=True))
        config = await self.bot.runtime_call("get", interaction.guild_id)
        if not config:
            raise ValueError("Choose a channel with /vault-admin setup first.")
        message = await self.bot.post_drop(interaction.guild_id, config["channel_id"], card or None,
                                           force=True, rarity=rarity or None,
                                           request_id=str(interaction.id) if getattr(interaction, 'id', None) else None)
        await self.bot.runtime_call("audit", interaction.user.id, "manual_spawn",
                                    {"guild_id": str(interaction.guild_id), "message_id": str(message.id),
                                     "card_id": card or None, "rarity": rarity or None})
        await reply(interaction, content=f"Encounter posted: {message.jump_url}")

    @app_commands.command(name="give", description="Put fresh copies of any card straight into a member's collection")
    @app_commands.autocomplete(card=autocomplete_failed_card)
    @app_commands.describe(member="Who receives the card", card="Any card in the catalog", count="How many copies (1–10)",
                           note="Optional reason, shown in the public announcement", announce="Post a public message about it")
    async def give(self, interaction, member: discord.Member, card: str, count: app_commands.Range[int, 1, 10] = 1,
                   note: app_commands.Range[str, 0, 200] = "", announce: bool = True):
        await discord_request(interaction.response.defer(ephemeral=True, thinking=True))
        if member.bot:
            raise ValueError("Choose a person, not a bot.")
        if card not in self.bot.game.catalog:
            raise ValueError("Pick a card from the autocomplete list.")
        result = await self.bot.call("admin_grant", str(interaction.guild_id), str(interaction.user.id), str(member.id),
                                     card, count, note=note or None, request_id=str(interaction.id))
        await self.bot.runtime_call("audit", interaction.user.id, "admin_grant",
                                    {"guild_id": str(interaction.guild_id), "to": str(member.id), "card_id": card,
                                     "count": count, "transfer_id": result["id"]})
        definition = self.bot.game.catalog[card]
        label = f"{count}× **{definition['name']}**" if count > 1 else f"**{definition['name']}**"
        if announce and not result.get("replayed"):
            text = f"🎁 {member.mention} received {label} ({definition['rarity'].title()}) from the server team."
            if note:
                text += f"\n> {discord.utils.escape_mentions(note)}"
            try:
                await discord_request(interaction.channel.send(content=text, allowed_mentions=discord.AllowedMentions(
                    users=[member], everyone=False, roles=False)))
            except DISCORD_ERRORS as error:
                log.warning("Grant announcement skipped: %s", type(error).__name__)
        await reply(interaction, content=(f"Already granted earlier (no duplicate made): {label} → {member.mention}." if result.get("replayed")
                                          else f"Granted {label} to {member.mention}. Copy IDs: " +
                                          ", ".join(f"`{key}`" for key in result["instance_ids"])))

    async def autocomplete_clear(self, interaction, current):
        if (interaction.guild_id != self.bot.settings.guild_id or interaction.user.bot
                or not interaction.permissions.manage_guild):
            return []
        try:
            rows = await asyncio.wait_for(self.bot.call("inventory_clears", str(interaction.guild_id)), 1.8)
        except (TimeoutError, OSError):
            return []
        return [app_commands.Choice(name=f"{row['id']} · member {row['target_id']} · {len(row['instance_ids'])} cards"
                                    + (" · undone" if row["restored_at"] else ""), value=row["id"])
                for row in rows if current in row["id"]][:25]

    @app_commands.command(name="clear-inventory", description="Remove every card a member owns (asks for confirmation; can be undone)")
    @app_commands.describe(member="Whose collection to clear")
    async def clear_inventory(self, interaction, member: discord.Member):
        await discord_request(interaction.response.defer(ephemeral=True))
        rows = await self.bot.call("inventory", str(interaction.guild_id), str(member.id), limit=100)
        if not rows:
            raise ValueError(f"{member.display_name} has no cards to clear.")
        key = secrets.token_hex(12)
        self.bot.pending_confirmations = {k: v for k, v in self.bot.pending_confirmations.items() if v["expires"] > time.time()}
        self.bot.pending_confirmations[key] = {"kind": "inventory_clear", "guild": str(interaction.guild_id),
                                               "user": str(interaction.user.id), "target": str(member.id),
                                               "expires": time.time() + 120}
        count = f"{len(rows)}{'+' if len(rows) == 100 else ''}"
        view = discord.ui.View(timeout=120)
        view.add_item(discord.ui.Button(label=f"Clear {count} cards", style=discord.ButtonStyle.danger,
                                        custom_id=f"vv:confirmclear:{key}"))
        view.add_item(discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id=f"vv:cancelclear:{key}"))
        await reply(interaction, content=(f"This removes all **{count}** cards from {member.mention}'s collection, "
                                          "empties their decks and cancels their open trades. Credits stay. "
                                          "You'll get a reference to undo it with `/vault-admin restore-inventory`."), view=view)

    @app_commands.command(name="restore-inventory", description="Undo an inventory clear")
    @app_commands.autocomplete(clear_id=autocomplete_clear)
    async def restore_inventory(self, interaction, clear_id: str):
        await discord_request(interaction.response.defer(ephemeral=True))
        result = await self.bot.call("admin_restore_inventory", str(interaction.guild_id), str(interaction.user.id), clear_id)
        await self.bot.runtime_call("audit", interaction.user.id, "inventory_restore",
                                    {"guild_id": str(interaction.guild_id), "clear_id": clear_id, "restored": result["restored"]})
        await reply(interaction, content=f"Restored **{result['restored']}** cards to <@{result['target_id']}>.")

    @app_commands.command(name="gambling", description="Turn credit gambling on or off for this server")
    async def gambling(self, interaction, enabled: bool):
        await discord_request(interaction.response.defer(ephemeral=True))
        await self.bot.call("set_gambling", str(interaction.guild_id), enabled)
        await self.bot.runtime_call("audit", interaction.user.id, "gambling", {"guild_id": str(interaction.guild_id), "enabled": enabled})
        await reply(interaction, content="Gambling is on." if enabled else "Gambling is paused. Pending wagers can't be accepted.")

    @app_commands.command(name='event', description='Start, inspect or stop a one-hour equal-chance card event')
    @app_commands.choices(action=[app_commands.Choice(name='Start one-hour event', value='start'),
                                 app_commands.Choice(name='Event status', value='status'),
                                 app_commands.Choice(name='Stop event', value='stop')])
    async def event(self, interaction, action: str = 'status'):
        await discord_request(interaction.response.defer(ephemeral=True, thinking=True))
        if action not in ('start', 'status', 'stop'):
            raise ValueError('Choose Start, Status or Stop.')
        async with self.bot.post_lock:
            config = await self.bot.runtime_call('get', interaction.guild_id)
            if not config:
                raise ValueError('Choose a channel with /vault-admin setup first.')
            current = await self.bot.runtime_call('event', interaction.guild_id)
            if action == 'stop':
                stopped = await self.bot.runtime_call('stop_event', interaction.guild_id, interaction.user.id)
                if stopped:
                    await self.bot.call('cancel_pending_overrides', str(interaction.guild_id), stopped['ends_at'])
                await reply(interaction, content=('Event stopped. Future drops use normal odds and release windows. '
                                                  'Any capture already posted keeps its saved expiry.'
                                                  if stopped else 'No equal-chance event is running.'))
                return
            if action == 'status' or current:
                await reply(interaction, content=(f"Equal-chance event active until <t:{int(current['ends_at'])}:F>. "
                                                  'Each available card has equal odds; edition stock and capture rules still apply.'
                                                  if current else 'No equal-chance event is running.'))
                return
            if config['paused']:
                raise ValueError('Scheduled drops are paused. Resume them before starting an event.')
            current = await self.bot.runtime_call('start_event', interaction.guild_id, interaction.user.id,
                                                  str(interaction.id) if getattr(interaction, 'id', None) else None,
                                                  immediate=True)
            if not current['active']:
                await reply(interaction, content='That event has already ended. A replay does not start another hour.')
                return
            first = ''
            try:
                message = await self.bot._post_drop_locked(interaction.guild_id, config['channel_id'], force=True,
                                                           request_id=f"event-first:{current['id']}")
                first = f" First encounter: {message.jump_url}"
            except (GameError, ValueError, OSError, TimeoutError, *DISCORD_ERRORS):
                await self.bot.runtime_call('event_retry', interaction.guild_id)
                first = ' The first event drop will retry after any open capture finishes or delivery recovers.'
            await reply(interaction, content=(f"Equal-chance hour started. Ends <t:{int(current['ends_at'])}:F>. "
                                               'Every available card has the same chance, including limited editions outside '
                                               'their usual dates. Lifetime stock, collector limits and capture streaks stay in effect. '
                                               'Drops keep the normal variable pace.' + first))

    @app_commands.command(name="pause", description="Pause or resume automatic encounters")
    async def pause(self, interaction, paused: bool = True):
        await discord_request(interaction.response.defer(ephemeral=True))
        async with self.bot.post_lock:
            await self.bot.runtime_call("pause", interaction.guild_id, interaction.user.id, paused)
            await reply(interaction, content="Scheduled drops paused." if paused else "Scheduled drops resumed.")

    @app_commands.command(name="health", description="Inspect gateway, storage and scheduler readiness")
    async def health(self, interaction):
        self.bot.write_health()
        result = json.loads((self.bot.settings.database.parent / "health.json").read_text())
        await reply(interaction, embed=panel("Service health", "\n".join(f"**{key.replace('_',' ')}:** {value}" for key,value in result.items())))

    @app_commands.command(name="raid", description="Start a scaled cooperative encounter in the configured channel")
    @app_commands.choices(arena=[app_commands.Choice(name="Ground",value="ground"),app_commands.Choice(name="Fleet",value="fleet")])
    async def raid(self, interaction, arena: str = "ground"):
        await discord_request(interaction.response.defer(ephemeral=True))
        config = await self.bot.runtime_call("get", interaction.guild_id)
        if not config:
            raise ValueError("Choose a channel with /vault-admin setup first.")
        message = await self.bot.post_raid(interaction.guild_id, config["channel_id"], arena)
        await self.bot.runtime_call("audit", interaction.user.id, "manual_raid", {"message_id": str(message.id)})
        await reply(interaction, content=f"Raid opened: {message.jump_url}")
