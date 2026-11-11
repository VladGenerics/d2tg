# -*- coding: utf-8 -*-
"""Shared code: paths, Telegram API, state/config, filters."""
import asyncio, contextlib, json, os, tempfile, threading, time
import urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor

try:
    import fcntl  # cross-process lock (Linux/macOS)
except ImportError:
    fcntl = None

BASE = os.environ.get("D2T_DIR") or os.path.dirname(os.path.abspath(__file__))
os.makedirs(BASE, exist_ok=True)
STATE_FILE = os.path.join(BASE, "discord2tg_state.json")
CONFIG_FILE = os.path.join(BASE, "discord2tg_config.json")
BOT_TOKEN_FILE = os.path.join(BASE, ".discord_token")
USER_TOKEN_FILE = os.path.join(BASE, ".discord_user_token")
TG_TOKEN_FILE = os.path.join(BASE, ".tg_token")
LOCK_FILE = os.path.join(BASE, ".d2t.lock")

EXIT_FATAL = 78  # bridge hit an unrecoverable error (bad token etc), don't restart

# command key -> config key
KEYS = {
    "channel": "watch_channels", "server": "watch_servers", "user": "watch_users",
    "keyword": "search_keywords", "exclude": "search_exclude",
    "ignore_channel": "ignore_channels", "ignore_user": "ignore_users",
    "notify_user": "notify_users", "notify_keyword": "notify_keywords",
    "follow_user": "follow_users",   # everything from this user, in ANY channel
}

_lock = threading.Lock()


def log(m, tag=""):
    print(time.strftime("[%H:%M:%S]"), tag, m, flush=True)


def read_secret(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def tg_token():
    t = os.environ.get("TG_TOKEN", "").strip() or read_secret(TG_TOKEN_FILE)
    if not t:
        raise SystemExit(f"No Telegram token. Set TG_TOKEN or put it in {TG_TOKEN_FILE}")
    return t


def fatal(msg, tag=""):
    """Unrecoverable bridge error: save the reason for the admin and exit with EXIT_FATAL."""
    log(msg, tag)
    update_state(bridge_error=msg)
    raise SystemExit(EXIT_FATAL)


def warn_libs(want, other):
    """Warn about the wrong library (both packages install the `discord` module)."""
    try:
        from importlib import metadata

        def has(*names):
            for n in names:
                try:
                    metadata.version(n)
                    return True
                except metadata.PackageNotFoundError:
                    pass
            return False
        spell = lambda n: (n, n.replace(".", "_"), n.replace(".", "-"))
        if has(*spell(other)):
            log(f"WARNING: {other} is installed, it conflicts with {want} (same 'discord' module)", "[lib]")
        if not has(*spell(want)):
            log(f"WARNING: {want} not found in installed packages", "[lib]")
    except Exception:
        pass


@contextlib.contextmanager
def _locked():
    """Lock across threads and processes (supervisor + bridge)."""
    with _lock:
        if fcntl is None:
            yield
            return
        with open(LOCK_FILE, "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, data, secret=False):
    """Atomic write (tmp + rename), mode 0600."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def load_state():
    st = read_json(STATE_FILE, {})
    st.setdefault("admin_chat", "")
    st.setdefault("paused", False)
    return st


def admins(st=None):
    """Primary admin plus extra admins, as a list of chat ids (strings)."""
    st = st or load_state()
    out = []
    for x in [st.get("admin_chat")] + list(st.get("admins", [])):
        if x and str(x) not in out:
            out.append(str(x))
    return out


def update_state(**kw):
    """Reload state, change only the given keys, so processes don't overwrite each other."""
    with _locked():
        st = load_state()
        st.update(kw)
        write_json(STATE_FILE, st)
        return st


def load_cfg():
    return read_json(CONFIG_FILE, {})


def save_cfg(cfg):
    with _locked():
        write_json(CONFIG_FILE, cfg)


def save_secret(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value.strip())
    os.chmod(path, 0o600)


def tg(method, **params):
    """Call the Telegram API. {"ok": False, ...} means an API error, None means network/429 retries failed."""
    url = f"https://api.telegram.org/bot{tg_token()}/{method}"
    data = json.dumps(params).encode()
    for attempt in range(3):
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=75) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read())
            except Exception:
                body = {}
            if e.code == 429:
                time.sleep(min((body.get("parameters") or {}).get("retry_after", 3), 30))
                continue
            if e.code >= 500:
                time.sleep(1 + attempt)
                continue
            desc = str(body.get("description", ""))
            log(f"tg {method} HTTP {e.code}: {desc[:100]}", "[tg]")
            return {"ok": False, "error_code": e.code, "description": desc}
        except Exception as e:
            log(f"tg {method} err: {str(e)[:80]}", "[tg]")
            time.sleep(1 + attempt)
    return None


def chunks(text, n=3500):
    """Split to fit the TG limit (4096), preferably at a line break."""
    text = text or ""
    out = []
    while len(text) > n:
        cut = text.rfind("\n", n // 2, n)
        if cut == -1:
            cut = n
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    out.append(text)
    return out


def tg_send(text, chat=None, **kw):
    chat = chat or load_state().get("admin_chat")
    if not chat:
        return None
    r = None
    for part in chunks(text):
        if part.strip():
            r = tg("sendMessage", chat_id=chat, text=part, **kw)
    return r


def norm_channel(v):
    """'Server/chan' -> 'Server/#chan' (the config stores the form with #)."""
    v = (v or "").strip()
    g, sep, c = v.rpartition("/")
    if not sep or g.strip().lower() == "dm":
        return v
    return f"{g.strip()}/#{c.strip().lstrip('#').strip()}"


def should_forward(guild, channel, author, content, parent=None, ids=(), guild_id=None,
                   message_id=None, embed_text=""):
    """Returns (forward?, is it a notify?).
    author is a string or a set of identifiers (display name, username, id).
    parent is the parent channel name when the message comes from a thread.
    ids holds the channel id (and the parent id for threads); entries in the config
    that are plain numbers are matched against it, names are the old way.
    guild_id does the same for watch_servers.
    With no watch_channels, watch_servers or follow_users nothing is forwarded."""
    cfg = load_cfg()
    low = lambda xs: [str(x).lower() for x in xs if str(x).strip()]
    # normalize to Server/#chan so a plain 'Server/chan' in the config works too
    low_ch = lambda xs: {norm_channel(str(x)).lower() for x in xs if str(x).strip()}
    wc, ws = low_ch(cfg.get("watch_channels", [])), low(cfg.get("watch_servers", []))
    ign = low_ch(cfg.get("ignore_channels", []))
    chans = {f"{guild}/#{channel}".lower() if guild else f"dm/{channel}".lower()}
    if guild and parent:
        chans.add(f"{guild}/#{parent}".lower())
    who = {author} if isinstance(author, str) else set(author)
    who = {str(x).lower() for x in who if x}
    chan_ids = {str(i) for i in ids if i}
    by_id = lambda key: bool(chan_ids & {str(x).strip() for x in cfg.get(key, [])})
    hit = lambda key: bool(who & set(low(cfg.get(key, []))))
    text = (content or "").lower()
    notify = hit("notify_users") or any(k in text for k in low(cfg.get("notify_keywords", [])))

    if chans & ign or by_id("ignore_channels"):  # ignored channel (or thread parent) wins over everything, even follow_users
        return False, False
    if message_id is not None and str(message_id) in {str(x) for x in cfg.get("ignore_messages", [])}:
        return False, False
    # Explicit scoped ignores win over follow_users too. Include embeds here,
    # without changing the existing global keyword/watch/notification semantics.
    scoped_text = "\n".join((text, embed_text or "")).lower()
    for rule in cfg.get("ignore_text", []):
        scope = norm_channel(str(rule.get("channel", ""))).lower()
        needle = str(rule.get("text", "")).strip().lower()
        if scope in chans | chan_ids and needle and needle in scoped_text:
            return False, False
    # follow_user: let everything from this user through, skip the other filters
    if hit("follow_users"):
        return True, notify
    srv_ok = bool(guild and (guild.lower() in ws or (guild_id and str(guild_id) in ws)))
    if not (srv_ok or chans & wc or by_id("watch_channels")):
        return False, False
    if hit("ignore_users"):
        return False, False
    if low(cfg.get("watch_users", [])) and not hit("watch_users"):
        return False, False
    kws = low(cfg.get("search_keywords", []))
    if kws and not any(k in text for k in kws):
        return False, False
    if any(k in text for k in low(cfg.get("search_exclude", []))):
        return False, False
    return True, notify


def chan_tag(cfg, ids):
    """(folder, alias) for a channel (or the parent of a thread), from the config."""
    for i in ids:
        if not i:
            continue
        i = str(i)
        alias = (cfg.get("aliases") or {}).get(i)
        folder = next((n for n, members in (cfg.get("folders") or {}).items() if i in members), None)
        if alias or folder:
            return folder, alias
    return None, None


def format_message(message, notify=False, tag=(None, None)):
    """Discord message -> text for TG."""
    guild = message.guild.name if message.guild else None
    where = f"{guild}/#{message.channel.name}" if guild else f"DM/{message.channel}"
    folder, alias = tag
    if folder or alias:
        where = (f"{folder} / " if folder else "") + (alias or where)
    head = f"{'[!] ' if notify else ''}{message.author.display_name} ({where})"
    body = getattr(message, "clean_content", None) or message.content or ""
    parts = [head]
    ref = getattr(getattr(message, "reference", None), "resolved", None)
    if ref is not None and hasattr(ref, "author"):
        parts.append(f"reply to {ref.author.display_name}: {(ref.clean_content or '')[:100]}")
    if body:
        parts.append(body)
    for e in message.embeds[:3]:
        t = " - ".join(x for x in (e.title, e.description) if x)
        if t:
            parts.append("> " + t[:500])
    for s in getattr(message, "stickers", [])[:3]:
        parts.append(f"[sticker: {s.name}]")
    parts += [a.url for a in message.attachments[:5]]
    return "\n".join(parts)


def message_embed_text(message):
    """Search all embed text, including fields used by statistics bots."""
    parts = []
    for embed in getattr(message, "embeds", []):
        parts.extend(x for x in (embed.title, embed.description) if x)
        for field in getattr(embed, "fields", []):
            parts.extend(x for x in (field.name, field.value) if x)
    return "\n".join(parts)


def forward(message, guild_name, channel_name, parent_name=None, ids=(), guild_id=None):
    """Blocking part: filters + sending. Call it through forward_async."""
    st = load_state()
    if st.get("paused") or not st.get("admin_chat"):
        return
    a = message.author
    who = (a.display_name, getattr(a, "name", None), str(getattr(a, "id", "")))
    ok, notify = should_forward(guild_name, channel_name, who,
                                getattr(message, "clean_content", None) or message.content,
                                parent=parent_name, ids=ids, guild_id=guild_id,
                                message_id=getattr(message, "id", None),
                                embed_text=message_embed_text(message))
    if not ok:
        return
    text = format_message(message, notify, chan_tag(load_cfg(), ids))
    cid = str(getattr(message.channel, "id", ""))
    # Keep source ids on the button, even when the displayed channel is aliased.
    kb = {"inline_keyboard": [[{"text": "Ignore similar messages…", "callback_data": f"msgignore:{cid}"}]]} if cid.isdigit() else None
    for adm in admins(st):
        tg_send(text, chat=adm, **({"reply_markup": kb} if kb else {}))
    nc = load_cfg().get("notification_chat")
    if notify and nc and str(nc) not in admins(st):
        tg_send(text, chat=nc)


# Single sender thread so messages reach TG in the same order they came from Discord.
_FWD = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fwd")


async def forward_async(message):
    guild = message.guild.name if message.guild else None
    chan = getattr(message.channel, "name", None) if guild else str(message.channel)
    par = getattr(message.channel, "parent", None)  # set for threads
    ids = (getattr(message.channel, "id", None), getattr(par, "id", None))
    await asyncio.get_running_loop().run_in_executor(
        _FWD, forward, message, guild, str(chan), getattr(par, "name", None), ids,
        getattr(message.guild, "id", None))


def collect_channels(guilds):
    """Readable text channels and servers: ({'Server/#chan': id}, {'Server': id})."""
    chans, servers = {}, {}
    for g in guilds:
        servers[g.name] = g.id
        for c in g.text_channels:
            try:
                if c.permissions_for(g.me).read_messages:
                    chans[f"{g.name}/#{c.name}"] = c.id
            except Exception:
                pass
    return chans, servers


class RefreshMixin:
    """Keeps known_channels fresh, so a server you just joined shows up without a restart."""
    mode = "?"
    _task = None

    async def refresh(self):
        chans, servers = collect_channels(self.guilds)
        await asyncio.to_thread(update_state, known_channels=chans, known_servers=servers, mode=self.mode)
        return chans

    def _soon(self):
        # events come in bursts, so wait a bit and refresh once
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._later())

    async def _later(self):
        await asyncio.sleep(5)
        try:
            await self.refresh()
        except Exception as e:
            log(f"refresh failed: {e!r}"[:150])

    async def on_guild_join(self, guild):
        self._soon()

    async def on_guild_remove(self, guild):
        self._soon()

    async def on_guild_channel_create(self, channel):
        self._soon()

    async def on_guild_channel_delete(self, channel):
        self._soon()

    async def on_guild_channel_update(self, before, after):
        if getattr(before, "name", None) != getattr(after, "name", None):
            self._soon()
