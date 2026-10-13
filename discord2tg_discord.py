# -*- coding: utf-8 -*-
"""discord2tg_discord.py — Discord → TG (режим обычного бота, discord.py 2.x).
Бот видит только серверы, куда его добавили. Фильтры из конфига применяются."""
import asyncio, os
import discord
from discord2tg_common import *


def get_token():
    env = os.environ.get("DISCORD_TOKEN", "").strip()
    if env:
        return env
    try:
        return open(BOT_TOKEN_FILE).read().strip()
    except OSError:
        return ""


class Bridge(discord.Client):
    async def on_ready(self):
        chans = {}
        for g in self.guilds:
            for c in g.text_channels:
                if c.permissions_for(g.me).read_messages:
                    chans[f"{g.name}/#{c.name}"] = c.id
        update_state(known_channels=chans, mode="bot")
        log(f"connected: {self.user} | servers: {len(self.guilds)} | channels: {len(chans)}", "[d2t]")
        await asyncio.to_thread(
            tg_send, f"🤖 Discord: {self.user.name}\n📡 Серверов: {len(self.guilds)}, каналов: {len(chans)}")

    async def on_message(self, message):
        if message.author.bot:
            return
        guild = message.guild.name if message.guild else None
        await asyncio.to_thread(forward, message, guild, str(message.channel.name if guild else message.channel))


if __name__ == "__main__":
    tok = get_token()
    if not tok:
        log("no bot token", "[d2t]")
        raise SystemExit(1)
    intents = discord.Intents.default()
    intents.message_content = True
    Bridge(intents=intents).run(tok)
