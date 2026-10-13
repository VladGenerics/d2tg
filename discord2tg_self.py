# -*- coding: utf-8 -*-
"""Discord -> TG, account mode. Needs discord.py-self (plain discord.py can't log in with a user token).
Note: this breaks Discord ToS, the account can get banned."""
import asyncio, os
import discord
from discord2tg_common import *


def get_token():
    env = os.environ.get("DISCORD_USER_TOKEN", "").strip()
    if env:
        return env
    try:
        return open(USER_TOKEN_FILE).read().strip()
    except OSError:
        return ""


class SelfBridge(discord.Client):
    async def on_ready(self):
        chans = {}
        for g in self.guilds:
            for c in g.text_channels:
                try:
                    if c.permissions_for(g.me).read_messages:
                        chans[f"{g.name}/#{c.name}"] = c.id
                except Exception:
                    pass
        update_state(known_channels=chans, mode="user")
        log(f"connected: {self.user} | servers: {len(self.guilds)} | channels: {len(chans)}", "[self]")
        await asyncio.to_thread(
            tg_send, f"Connected as {self.user}. Servers: {len(self.guilds)}, channels: {len(chans)}.\n\n"
                     "Only forwarding what's in your lists.\n"
                     "Add with /add_channel Server/#channel or /add_server Server")

    async def on_message(self, message):
        if message.author.id == self.user.id:
            return
        guild = message.guild.name if message.guild else None
        await asyncio.to_thread(forward, message, guild, str(message.channel.name if guild else message.channel))


if __name__ == "__main__":
    tok = get_token()
    if not tok:
        log("no user token", "[self]")
        raise SystemExit(1)
    SelfBridge().run(tok)
