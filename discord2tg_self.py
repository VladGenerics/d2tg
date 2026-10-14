# -*- coding: utf-8 -*-
"""Discord -> TG, account mode. Needs discord.py-self (plain discord.py can't log in with a user token).
Note: this breaks Discord ToS, the account can get banned."""
import asyncio, os
import discord
from discord2tg_common import *


def get_token():
    return os.environ.get("DISCORD_USER_TOKEN", "").strip() or read_secret(USER_TOKEN_FILE)


class SelfBridge(RefreshMixin, discord.Client):
    mode = "user"
    announced = False  # on_ready also fires on reconnects, greet only once

    async def on_ready(self):
        chans = await self.refresh()
        log(f"connected: {self.user} | servers: {len(self.guilds)} | channels: {len(chans)}", "[self]")
        if self.announced:
            return
        self.announced = True
        await asyncio.to_thread(
            tg_send, f"Connected as {self.user}. Servers: {len(self.guilds)}, channels: {len(chans)}.\n\n"
                     "Only forwarding what's in your lists.\n"
                     "Paste a channel link to follow it, or use /add_server Server")

    async def on_message(self, message):
        if message.author.id == self.user.id:
            return
        await forward_async(message)


if __name__ == "__main__":
    tok = get_token()
    if not tok:
        fatal("No Discord account token. Send /usertoken TOKEN to the Telegram bot.", "[self]")
    warn_libs("discord.py-self", "discord.py")
    try:
        SelfBridge().run(tok)
    except discord.LoginFailure:
        fatal("Discord rejected the account token (expired/wrong). Send a fresh /usertoken TOKEN. "
              "Also make sure discord.py-self (not discord.py) is installed.", "[self]")
