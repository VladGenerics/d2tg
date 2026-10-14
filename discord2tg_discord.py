# -*- coding: utf-8 -*-
"""discord2tg_discord.py - Discord -> TG in plain bot mode (discord.py 2.x).
The bot only sees servers it was added to. Config filters apply."""
import asyncio, os
import discord
from discord2tg_common import *


def get_token():
    return os.environ.get("DISCORD_TOKEN", "").strip() or read_secret(BOT_TOKEN_FILE)


class Bridge(RefreshMixin, discord.Client):
    mode = "bot"
    announced = False  # on_ready also fires on reconnects, greet only once

    async def on_ready(self):
        chans = await self.refresh()
        log(f"connected: {self.user} | servers: {len(self.guilds)} | channels: {len(chans)}", "[d2t]")
        if self.announced:
            return
        self.announced = True
        await asyncio.to_thread(
            tg_send, f"Bot {self.user.name} connected. Servers: {len(self.guilds)}, channels: {len(chans)}.")

    async def on_message(self, message):
        if message.author.bot:
            return
        await forward_async(message)


if __name__ == "__main__":
    tok = get_token()
    if not tok:
        fatal("No Discord bot token. Send /bottoken TOKEN to the Telegram bot.", "[d2t]")
    warn_libs("discord.py", "discord.py-self")
    intents = discord.Intents.default()
    intents.message_content = True
    try:
        Bridge(intents=intents).run(tok)
    except discord.LoginFailure:
        fatal("Discord rejected the bot token. Send a fresh /bottoken TOKEN.", "[d2t]")
    except discord.PrivilegedIntentsRequired:
        fatal("Enable MESSAGE CONTENT INTENT for the bot: Discord Developer Portal > Bot > "
              "Privileged Gateway Intents.", "[d2t]")
