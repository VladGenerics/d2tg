# -*- coding: utf-8 -*-
"""Общий код: пути, Telegram API, state/config, фильтры."""
import json, os, time, urllib.request, urllib.error, tempfile, threading

BASE = os.environ.get("D2T_DIR") or os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, "discord2tg_state.json")
CONFIG_FILE = os.path.join(BASE, "discord2tg_config.json")
BOT_TOKEN_FILE = os.path.join(BASE, ".discord_token")
USER_TOKEN_FILE = os.path.join(BASE, ".discord_user_token")
TG_TOKEN_FILE = os.path.join(BASE, ".tg_token")

# command key -> config key
KEYS = {
    "channel": "watch_channels", "server": "watch_servers", "user": "watch_users",
    "keyword": "search_keywords", "exclude": "search_exclude",
    "ignore_channel": "ignore_channels", "ignore_user": "ignore_users",
    "notify_user": "notify_users", "notify_keyword": "notify_keywords",
    "follow_user": "follow_users",   # все сообщения юзера из ЛЮБЫХ каналов
}

_lock = threading.Lock()


def log(m, tag=""):
    print(time.strftime("[%H:%M:%S]"), tag, m, flush=True)


def tg_token():
    t = os.environ.get("TG_TOKEN", "").strip()
    if not t:
        try:
            t = open(TG_TOKEN_FILE).read().strip()
        except OSError:
            t = ""
    if not t:
        raise SystemExit(f"No Telegram token. Set TG_TOKEN or put it in {TG_TOKEN_FILE}")
    return t


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, data, secret=False):
    """Атомарная запись (tmp + rename)."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


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
    """Читает свежий state, меняет только переданные ключи — процессы не затирают друг друга."""
    with _lock:
        st = load_state()
        st.update(kw)
        write_json(STATE_FILE, st)
        return st


def load_cfg():
    return read_json(CONFIG_FILE, {})


def save_cfg(cfg):
    with _lock:
        write_json(CONFIG_FILE, cfg)


def save_secret(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value.strip())
    os.chmod(path, 0o600)


def tg(method, **params):
    url = f"https://api.telegram.org/bot{tg_token()}/{method}"
    req = urllib.request.Request(url, data=json.dumps(params).encode(),
                                 headers={"Content-Type": "application/json"})
    for attempt in range(3):
        try:
            return json.loads(urllib.request.urlopen(req, timeout=75).read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    wait = json.loads(e.read())["parameters"]["retry_after"]
                except Exception:
                    wait = 3
                time.sleep(min(wait, 30))
                continue
            log(f"tg {method} HTTP {e.code}", "[tg]")
            return None
        except Exception as e:
            log(f"tg {method} err: {str(e)[:80]}", "[tg]")
            time.sleep(1 + attempt)
    return None


def chunks(text, n=4000):
    text = text or ""
    return [text[i:i + n] for i in range(0, len(text), n)] or [""]


def tg_send(text, chat=None, **kw):
    chat = chat or load_state().get("admin_chat")
    if not chat:
        return None
    r = None
    for part in chunks(text):
        r = tg("sendMessage", chat_id=chat, text=part, **kw)
    return r


def should_forward(guild, channel, author, content):
    """Возвращает (пересылать?, это notify?).
    По умолчанию (нет watch_channels и watch_servers) НЕ пересылаем ничего."""
    cfg = load_cfg()
    low = lambda xs: [str(x).lower() for x in xs]
    wc, ws = low(cfg.get("watch_channels", [])), low(cfg.get("watch_servers", []))
    ch_full = f"{guild}/#{channel}".lower() if guild else f"dm/{channel}".lower()
    a = str(author).lower()
    if ch_full in low(cfg.get("ignore_channels", [])):
        return False, False
    # follow_user: let everything from this user through, skip other filters
    if a in low(cfg.get("follow_users", [])):
        return True, a in low(cfg.get("notify_users", []))
    if not ((guild and guild.lower() in ws) or ch_full in wc):
        return False, False
    if a in low(cfg.get("ignore_users", [])):
        return False, False
    wu = low(cfg.get("watch_users", []))
    if wu and a not in wu:
        return False, False
    text = (content or "").lower()
    kws = low(cfg.get("search_keywords", []))
    if kws and not any(k in text for k in kws):
        return False, False
    if any(k in text for k in low(cfg.get("search_exclude", []))):
        return False, False
    notify = a in low(cfg.get("notify_users", [])) or \
        any(k in text for k in low(cfg.get("notify_keywords", [])))
    return True, notify


def format_message(message, notify=False):
    """Общий формат сообщения Discord -> текст для TG."""
    guild = message.guild.name if message.guild else None
    where = f"{guild}/#{message.channel.name}" if guild else f"DM/{message.channel}"
    head = f"{'🔔' if notify else '💬'} {message.author.display_name} ({where})"
    body = getattr(message, "clean_content", None) or message.content or ""
    parts = [head]
    ref = getattr(getattr(message, "reference", None), "resolved", None)
    if ref is not None and hasattr(ref, "author"):
        parts.append(f"↩️ {ref.author.display_name}: {(ref.clean_content or '')[:100]}")
    if body:
        parts.append(body)
    for e in message.embeds[:3]:
        t = " — ".join(x for x in (e.title, e.description) if x)
        if t:
            parts.append("▫️ " + t[:500])
    for s in getattr(message, "stickers", [])[:3]:
        parts.append(f"[sticker: {s.name}]")
    parts += ["📎 " + a.url for a in message.attachments[:5]]
    return "\n".join(parts)


def forward(message, guild_name, channel_name):
    """Синхронная часть: фильтры + отправка. Вызывать через asyncio.to_thread."""
    st = load_state()
    if st.get("paused") or not st.get("admin_chat"):
        return
    ok, notify = should_forward(guild_name, channel_name, message.author.display_name,
                                message.clean_content or message.content)
    if not ok:
        return
    text = format_message(message, notify)
    for a in admins(st):
        tg_send(text, chat=a)
    nc = load_cfg().get("notification_chat")
    if notify and nc and str(nc) not in admins(st):
        tg_send(text, chat=nc)
