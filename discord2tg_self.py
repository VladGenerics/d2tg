# -*- coding: utf-8 -*-
"""discord2tg_self.py — Discord → TG (режим аккаунта).
Требует форк:  pip install discord.py-self   (обычный discord.py токены юзеров не поддерживает)
ВНИМАНИЕ: селфбот нарушает ToS Discord, аккаунт могут забанить.
По умолчанию ничего не пересылается — только каналы/сервера из конфига."""
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
            tg_send, f"👤 Discord: {self.user}\n📡 Серверов: {len(self.guilds)}, каналов: {len(chans)}\n\n"
                     "Пересылаю только из watch_channels / watch_servers.\n"
                     "Добавить: /add_channel Сервер/#канал  или  /add_server Сервер")

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
