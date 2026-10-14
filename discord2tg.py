# -*- coding: utf-8 -*-
"""discord2tg.py - Telegram control bot: settings menu + runs the Discord bridge.
Telegram token: TG_TOKEN env var or a .tg_token file next to the script.
Optional: TG_ADMIN_CHAT=<chat id> so only that chat can claim admin with /start."""
import os, re, signal, subprocess, sys, threading, time, zlib
from discord2tg_common import *

# menu section -> (title, [keys from KEYS])
SECTIONS = {
    "channels": ("Channels", ["channel", "server", "ignore_channel"]),
    "users": ("Users", ["follow_user", "user", "ignore_user"]),
    "keywords": ("Keywords", ["keyword", "exclude"]),
    "notify": ("Notifications", ["notify_user", "notify_keyword"]),
}
SECTION_OF = {k: name for name, (_, keys) in SECTIONS.items() for k in keys}
LABELS = {
    "channel": "Channel (Server/#name)", "server": "Whole server", "ignore_channel": "Ignore channel",
    "follow_user": "Follow user (everywhere)", "user": "Only from user (in channels)", "ignore_user": "Ignore user",
    "keyword": "Keyword", "exclude": "Exclude keyword",
    "notify_user": "Notify user", "notify_keyword": "Notify keyword",
}
LINK_RE = re.compile(r"https?://(?:[\w-]+\.)?discord(?:app)?\.com/channels/(@me|\d+)(?:/(\d+))?", re.I)


# ---------- Discord process control ----------
class Bridge:
    proc = None
    lock = threading.RLock()
    started = 0.0
    fails = 0
    retry_at = None
    shutdown = False

    @staticmethod
    def script():
        has_user = bool(os.environ.get("DISCORD_USER_TOKEN", "").strip() or os.path.exists(USER_TOKEN_FILE))
        has_bot = bool(os.environ.get("DISCORD_TOKEN", "").strip() or os.path.exists(BOT_TOKEN_FILE))
        prefer = load_state().get("prefer")  # whichever token was sent last wins
        if prefer == "bot" and has_bot:
            return "discord2tg_discord.py"
        if has_user:
            return "discord2tg_self.py"
        if has_bot:
            return "discord2tg_discord.py"
        return None

    @classmethod
    def stop(cls):
        with cls.lock:
            p, cls.proc, cls.retry_at = cls.proc, None, None
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()

    @classmethod
    def start(cls, manual=True):
        with cls.lock:
            cls.stop()
            s = cls.script()
            if not s or cls.shutdown:
                return False
            if manual:
                cls.fails = 0
            update_state(bridge_error=None)
            log(f"starting {s}")
            cls.proc = subprocess.Popen([sys.executable, "-u", os.path.join(BASE, s)], cwd=BASE)
            cls.started, cls.retry_at = time.time(), None
        return True

    @classmethod
    def watchdog(cls):
        """Restart a crashed bridge with a growing delay. No restart on fatal errors (bad token etc)."""
        while True:
            time.sleep(5)
            notice = None
            with cls.lock:
                p = cls.proc
                if p is None:
                    continue
                if p.poll() is None:
                    if time.time() - cls.started > 60:
                        cls.fails = 0
                    continue
                if p.returncode == EXIT_FATAL or not cls.script():
                    cls.proc = None
                    notice = load_state().get("bridge_error") or f"Bridge stopped (code {p.returncode})."
                else:
                    if cls.retry_at is None:
                        cls.fails += 1
                        delay = min(300, 5 * 2 ** (cls.fails - 1))
                        cls.retry_at = time.time() + delay
                        log(f"bridge exited (code {p.returncode}), restart in {delay}s")
                        if cls.fails == 3:
                            notice = "Bridge keeps crashing, retrying with growing delay. Check the logs."
                    if time.time() >= cls.retry_at:
                        cls.start(manual=False)
            if notice:
                update_state(bridge_error=None)
                for a in admins():
                    tg_send("Warning: " + notice, chat=a)


# ---------- helpers ----------
def crc(s):
    """Short stable id for callback_data (Telegram caps it at 64 bytes)."""
    return zlib.crc32(str(s).lower().encode("utf-8"))


def servers_map():
    """{server: [(full_name, channel_id), ...]} built from known_channels."""
    out = {}
    for full, cid in load_state().get("known_channels", {}).items():
        g = full.rpartition("/#")[0]
        if g:
            out.setdefault(g, []).append((full, cid))
    for v in out.values():
        v.sort(key=lambda t: t[0].lower())
    return out


def chan_name_by_id(cid):
    for full, i in load_state().get("known_channels", {}).items():
        if str(i) == str(cid):
            return full
    return None


def server_name_by_id(gid):
    for name, i in load_state().get("known_servers", {}).items():
        if str(i) == str(gid):
            return name
    return None


def remember_label(cid, name):
    if name:
        labels = load_state().get("channel_labels", {})
        if labels.get(str(cid)) != name:
            update_state(channel_labels={**labels, str(cid): name})


def real_name(cid):
    return chan_name_by_id(cid) or load_state().get("channel_labels", {}).get(str(cid))


def label(entry):
    """Real name of a config entry (channels are stored as ids)."""
    e = str(entry)
    if not e.isdigit():
        return e
    return real_name(e) or f"channel {e}"


def disp(entry):
    """Like label, but shows the custom name too: 'My name (Server/#real)'."""
    e = str(entry)
    if not e.isdigit():
        return e
    alias, real = load_cfg().get("aliases", {}).get(e), real_name(e)
    if alias and real:
        return f"{alias} ({real})"
    return alias or real or f"channel {e}"


def remember_server(gid, name):
    if name:
        labels = load_state().get("server_labels", {})
        if labels.get(str(gid)) != name:
            update_state(server_labels={**labels, str(gid): name})


def srv_label(entry):
    e = str(entry)
    if not e.isdigit():
        return e
    return server_name_by_id(e) or load_state().get("server_labels", {}).get(e) or f"server {e}"


def srv_aliases(entry):
    e = str(entry).strip()
    al = {e.lower()}
    n = server_name_by_id(e) if e.isdigit() else None
    if n:
        al.add(n.lower())
    return al


def srv_on(g, ws, known=None):
    """Is the whole server g followed? ws is a set of lowercase config entries."""
    gid = (known if known is not None else load_state().get("known_servers", {})).get(g)
    return g.lower() in ws or bool(gid and str(gid) in ws)


def toggle_server(name):
    gid = load_state().get("known_servers", {}).get(name)
    cfg = load_cfg()
    lst = cfg.get("watch_servers", [])
    al = {name.lower()} | ({str(gid)} if gid else set())
    idx = [i for i, x in enumerate(lst) if str(x).lower() in al]
    for i in reversed(idx):
        lst.pop(i)
    if not idx:
        lst.append(str(gid) if gid else name)
        if gid:
            remember_server(gid, name)
    cfg["watch_servers"] = lst
    save_cfg(cfg)
    return not idx


def folders():
    return load_cfg().get("folders", {})


def find_folder(h):
    return next((n for n in folders() if str(crc(n)) == str(h)), None)


def folder_of(cid):
    return next((n for n, ids in folders().items() if str(cid) in ids), None)


def set_folder(cid, name):
    """Move a channel into folder `name`, or out of any folder when name is None."""
    cfg = load_cfg()
    fl = cfg.get("folders", {})
    for n in fl:
        fl[n] = [x for x in fl[n] if x != str(cid)]
    if name:
        fl.setdefault(name, []).append(str(cid))
    cfg["folders"] = fl
    save_cfg(cfg)


def chan_aliases(entry):
    """Everything that means the same channel: its id and its (normalized) name."""
    e = str(entry).strip()
    al = {norm_channel(e).lower()}
    n = chan_name_by_id(e) if e.isdigit() else None
    if n:
        al.add(norm_channel(n).lower())
    return al


def resolve_channel(val):
    """Link / numeric id / 'Server/chan' -> (entry, None) or (None, error).
    The entry is the channel id whenever we can get it, so renames don't matter."""
    val = val.strip()
    m = LINK_RE.search(val)
    if m:
        gid, cid = m.groups()
        if gid == "@me":
            return None, "DM links aren't supported. Use /follow user Name instead."
        if not cid:
            return None, "That's a server link, not a channel link. Use /add_server with it."
        return cid, None
    if val.isdigit():
        return val, None
    want = norm_channel(val).lower()
    for full, cid in load_state().get("known_channels", {}).items():
        if full.lower() == want:
            remember_label(cid, full)
            return str(cid), None
    return norm_channel(val), None  # unknown name, keep matching by name


def resolve_server(val):
    """Link / numeric id / server name -> (entry, None) or (None, error). Entry is the id when known."""
    val = val.strip()
    m = LINK_RE.search(val)
    if m:
        gid, cid = m.groups()
        if gid == "@me":
            return None, "DM links aren't supported. Use /follow user Name instead."
        name = server_name_by_id(gid)
        if not name and cid and chan_name_by_id(cid):
            name = chan_name_by_id(cid).rpartition("/#")[0]
        remember_server(gid, name)
        return gid, None
    if val.isdigit():
        return val, None
    for n, i in load_state().get("known_servers", {}).items():
        if n.lower() == val.lower():
            return str(i), None
    return val, None


def modify_list(op, kind, val):
    """op: add|remove. Returns the reply text."""
    val = val.strip()
    err = None
    if kind in ("channel", "ignore_channel"):
        val, err = resolve_channel(val)
    elif kind == "server":
        val, err = resolve_server(val)
    if err:
        return err
    cfg = load_cfg()
    key = KEYS[kind]
    lst = cfg.get(key, [])
    chan = kind in ("channel", "ignore_channel")
    if chan:
        al, norm = chan_aliases(val), lambda x: norm_channel(str(x)).lower()
    elif kind == "server":
        al, norm = srv_aliases(val), lambda x: str(x).lower()
    else:
        al, norm = {val.lower()}, lambda x: str(x).lower()
    shown = lambda x: disp(x) if chan else srv_label(x) if kind == "server" else x
    idx = [i for i, x in enumerate(lst) if norm(x) in al]
    if op == "add":
        if not idx:
            lst.append(val)
        msg = f"Added {LABELS[kind]}: {shown(val)}"
        if val.isdigit() and not (chan_name_by_id(val) if chan else server_name_by_id(val)):
            msg += ("\nI haven't seen it yet. It's tracked by its id, so if nothing shows up, check the "
                    "account is on that server (the list refreshes by itself).")
    elif idx:
        msg = f"Removed {LABELS[kind]}: {shown(lst[idx[0]])}"
        for i in reversed(idx):
            lst.pop(i)
    else:
        msg = f"Not in the list: {shown(val)}"
    cfg[key] = lst
    save_cfg(cfg)
    return msg


def toggle_in(key, name, norm=norm_channel):
    """Add or remove name in a config list. Returns True if it is in the list now."""
    cfg = load_cfg()
    lst = cfg.get(key, [])
    low = [norm(str(x)).lower() for x in lst]
    if norm(name).lower() in low:
        lst.pop(low.index(norm(name).lower()))
        now = False
    else:
        lst.append(name)
        now = True
    cfg[key] = lst
    save_cfg(cfg)
    return now


def channel_entries(key):
    return {norm_channel(str(x)).lower() for x in load_cfg().get(key, [])}


def has_channel(entries, cid, name=None):
    return str(cid) in entries or bool(name and norm_channel(name).lower() in entries)


def set_channel(key, cid, on):
    """Put a channel into (on=True) or take it out of a config list, old name entries included."""
    cfg = load_cfg()
    lst = cfg.get(key, [])
    name = real_name(cid)
    al = chan_aliases(str(cid)) | ({norm_channel(name).lower()} if name else set())
    idx = [i for i, x in enumerate(lst) if norm_channel(str(x)).lower() in al]
    if on and not idx:
        lst.append(str(cid))
    elif not on:
        for i in reversed(idx):
            lst.pop(i)
    cfg[key] = lst
    save_cfg(cfg)


def toggle_channel(key, cid, name=None):
    """Follow/ignore toggle by channel id. Returns True if it is on now."""
    now = not has_channel(channel_entries(key), cid, real_name(cid) or name)
    set_channel(key, cid, now)
    if now:
        remember_label(cid, name)
    return now


# ---------- menu ----------
def fmt_list(cfg, k):
    v = cfg.get(KEYS[k], [])
    if k in ("channel", "ignore_channel"):
        v = [disp(x) for x in v]
    elif k == "server":
        v = [srv_label(x) for x in v]
    return ", ".join(map(str, v)) if v else "-"


def main_menu():
    cfg, st = load_cfg(), load_state()
    mode = st.get("mode", "?")
    txt = (f"Discord -> TG bridge\nStatus: {'paused' if st.get('paused') else 'running'} (mode: {mode})\n"
           f"Servers: {len(cfg.get('watch_servers', []))}, channels: {len(cfg.get('watch_channels', []))}, "
           f"ignored: {len(cfg.get('ignore_channels', []))}\n"
           f"Users: {len(cfg.get('watch_users', []))}, followed: {len(cfg.get('follow_users', []))}, "
           f"keywords: {len(cfg.get('search_keywords', []))}")
    if not (cfg.get("watch_servers") or cfg.get("watch_channels") or cfg.get("follow_users")):
        txt += "\n\nNothing is forwarded yet. Add a channel, a server or follow a user first."
    kb = {"inline_keyboard": [
        [{"text": "Channels", "callback_data": "menu:channels"}, {"text": "Users", "callback_data": "menu:users"}],
        [{"text": "Keywords", "callback_data": "menu:keywords"}, {"text": "Notifications", "callback_data": "menu:notify"}],
        [{"text": "Folders", "callback_data": "fl:0"}, {"text": "Pause / Resume", "callback_data": "toggle_pause"}],
        [{"text": "Restart bridge", "callback_data": "restart"},
         {"text": "Reset filters", "callback_data": "reset_filters"}],
    ]}
    return txt, kb


def section_menu(name):
    cfg = load_cfg()
    title, keys = SECTIONS[name]
    txt = title + "\n\n" + "\n".join(f"{LABELS[k]}: {fmt_list(cfg, k)}" for k in keys)
    txt += "\n\nTap a button and send the value, or type /add_<type> value"
    if name == "channels":
        txt += ("\nYou can also paste a Discord channel or server link. Both are tracked by id, "
                "so renames don't break anything.")
    kb = []
    for k in keys:
        rm = "Unignore" if k.startswith("ignore") else "Remove"
        kb.append([{"text": f"Add: {LABELS[k]}", "callback_data": f"add:{k}"},
                   {"text": rm, "callback_data": f"rl:{k}"}])
    if name == "channels":
        kb.insert(0, [{"text": "Pick from list", "callback_data": "pick:0"},
                      {"text": "Folders", "callback_data": "fl:0"}])
    kb.append([{"text": "< Back", "callback_data": "menu:main"}])
    return txt, {"inline_keyboard": kb}


def remove_menu(kind):
    """Values as buttons, tapping one removes it."""
    items = load_cfg().get(KEYS[kind], [])
    back = [{"text": "< Back", "callback_data": f"menu:{SECTION_OF[kind]}"}]
    if not items:
        return f"{LABELS[kind]}: the list is empty.", {"inline_keyboard": [back]}
    show_as = disp if kind in ("channel", "ignore_channel") else srv_label if kind == "server" else str
    kb = [[{"text": f"Remove: {show_as(x)}"[:60], "callback_data": f"rm1:{kind}:{crc(x)}"}] for x in items[:40]]
    kb.append(back)
    return f"{LABELS[kind]}: tap one to remove it", {"inline_keyboard": kb}


PAGE = 8


def nav_row(page, pages, prefix):
    nav = []
    if page > 0:
        nav.append({"text": "<", "callback_data": f"{prefix}{page - 1}"})
    nav.append({"text": f"{page + 1}/{pages}", "callback_data": "noop"})
    if page < pages - 1:
        nav.append({"text": ">", "callback_data": f"{prefix}{page + 1}"})
    return nav


def pick_menu(page):
    """Step 1: list of servers."""
    servers = servers_map()
    back = [{"text": "< Back", "callback_data": "menu:channels"}]
    if not servers:
        return "No servers yet. The bridge hasn't connected to Discord.", {"inline_keyboard": [back]}
    cfg = load_cfg()
    ws = {str(x).lower() for x in cfg.get("watch_servers", [])}
    wc = channel_entries("watch_channels")
    names = sorted(servers, key=str.lower)
    known = load_state().get("known_servers", {})
    pages = (len(names) - 1) // PAGE + 1
    page = max(0, min(page, pages - 1))
    kb = []
    for g in names[page * PAGE:(page + 1) * PAGE]:
        n = sum(1 for full, cid in servers[g] if has_channel(wc, cid, full))
        mark = "[x]" if srv_on(g, ws, known) else ("[~]" if n else "[ ]")
        kb.append([{"text": f"{mark} {g} ({n}/{len(servers[g])})"[:60], "callback_data": f"srv:{crc(g)}:0"}])
    if pages > 1:
        kb.append(nav_row(page, pages, "pick:"))
    kb.append(back)
    return ("Pick a server, then choose its channels.\n"
            "[x] whole server, [~] some channels, [ ] nothing"), {"inline_keyboard": kb}


def server_menu(sid, page):
    """Step 2: channels of the chosen server."""
    servers = servers_map()
    g = next((n for n in servers if str(crc(n)) == str(sid)), None)
    if g is None:
        return pick_menu(0)
    cfg = load_cfg()
    ws = {str(x).lower() for x in cfg.get("watch_servers", [])}
    whole = srv_on(g, ws)
    wc, ign = channel_entries("watch_channels"), channel_entries("ignore_channels")
    names = cfg.get("aliases", {})
    chans = servers[g]
    pages = (len(chans) - 1) // PAGE + 1
    page = max(0, min(page, pages - 1))
    kb = [[{"text": f"{'[x]' if whole else '[ ]'} Whole server", "callback_data": f"tgs:{sid}:{page}"}]]
    for full, cid in chans[page * PAGE:(page + 1) * PAGE]:
        is_ign, is_wc = has_channel(ign, cid, full), has_channel(wc, cid, full)
        mark = "[-]" if is_ign else "[x]" if is_wc else "[s]" if whole else "[ ]"
        real = full.rpartition("/#")[2]
        alias = names.get(str(cid))
        kb.append([{"text": (f"{mark} {alias} - #{real}" if alias else f"{mark} #{real}")[:30],
                    "callback_data": f"tog:{cid}:{sid}:{page}"},
                   {"text": "unignore" if is_ign else "ignore", "callback_data": f"ign:{cid}:{sid}:{page}"},
                   {"text": "edit", "callback_data": f"ch:{cid}:s{sid}.{page}"}])
    if pages > 1:
        kb.append(nav_row(page, pages, f"srv:{sid}:"))
    kb.append([{"text": "< Servers", "callback_data": "pick:0"}])
    return (f"{g}\nTap a channel to follow or unfollow it, ignore to mute it, edit for rename and folder.\n"
            "[x] followed, [s] via whole server, [-] ignored, [ ] nothing"), \
        {"inline_keyboard": kb}


def ctx_back(ctx):
    """Callback of the Back button for a channel screen opened from server (s) or folder (f) view."""
    if ctx.startswith("s"):
        sid, _, page = ctx[1:].partition(".")
        return f"srv:{sid}:{page or 0}"
    if ctx.startswith("f"):
        return f"fo:{ctx[1:]}:0"
    return "menu:channels"


def channel_screen(cid, ctx):
    cid = str(cid)
    cfg = load_cfg()
    name = real_name(cid)
    alias, folder = cfg.get("aliases", {}).get(cid), folder_of(cid)
    is_ign = has_channel(channel_entries("ignore_channels"), cid, name)
    is_wc = has_channel(channel_entries("watch_channels"), cid, name)
    txt = (f"{alias or name or 'channel ' + cid}\n"
           f"Real name: {name or 'not seen yet'}\n"
           f"Folder: {folder or '-'}\n"
           f"Status: {'ignored' if is_ign else 'followed' if is_wc else 'not followed'}\n"
           f"ID: {cid}")
    kb = [[{"text": "Unfollow" if is_wc else "Follow", "callback_data": f"ctg:{cid}:{ctx}"},
           {"text": "Unignore" if is_ign else "Ignore", "callback_data": f"cig:{cid}:{ctx}"}],
          [{"text": "Rename", "callback_data": f"cren:{cid}:{ctx}"},
           {"text": "Folder", "callback_data": f"cmv:{cid}:{ctx}"}],
          [{"text": "< Back", "callback_data": ctx_back(ctx)}]]
    return txt, {"inline_keyboard": kb}


def move_menu(cid, ctx):
    cid = str(cid)
    cur = folder_of(cid)
    kb = [[{"text": f"{'* ' if n == cur else ''}{n}"[:60], "callback_data": f"cmt:{cid}:{crc(n)}:{ctx}"}]
          for n in folders()]
    kb.append([{"text": ("* " if cur is None else "") + "No folder", "callback_data": f"cmt:{cid}:0:{ctx}"}])
    kb.append([{"text": "New folder", "callback_data": f"cnf:{cid}:{ctx}"}])
    kb.append([{"text": "< Back", "callback_data": f"ch:{cid}:{ctx}"}])
    return f"Folder for {disp(cid)}:", {"inline_keyboard": kb}


def folders_menu(page=0):
    fl = folders()
    names = list(fl)
    pages = (len(names) - 1) // PAGE + 1 if names else 1
    page = max(0, min(page, pages - 1))
    wc = channel_entries("watch_channels")
    kb = []
    for n in names[page * PAGE:(page + 1) * PAGE]:
        on = sum(1 for c in fl[n] if has_channel(wc, c, real_name(c)))
        kb.append([{"text": f"{n} ({on}/{len(fl[n])})"[:60], "callback_data": f"fo:{crc(n)}:0"}])
    if pages > 1:
        kb.append(nav_row(page, pages, "fl:"))
    kb.append([{"text": "New folder", "callback_data": "fnew"}])
    kb.append([{"text": "< Back", "callback_data": "menu:main"}])
    txt = ("Folders group channels so you can follow or unfollow a whole set at once.\n"
           "Put a channel in a folder from its edit screen (Channels > Pick from list)."
           if names else "No folders yet. Create one, then put channels in it from their edit screen.")
    return txt, {"inline_keyboard": kb}


def folder_menu(fh, page=0):
    name = find_folder(fh)
    if name is None:
        return folders_menu(0)
    ids = folders()[name]
    pages = (len(ids) - 1) // PAGE + 1 if ids else 1
    page = max(0, min(page, pages - 1))
    wc, ign = channel_entries("watch_channels"), channel_entries("ignore_channels")
    kb = []
    for c in ids[page * PAGE:(page + 1) * PAGE]:
        nm = real_name(c)
        mark = "[-]" if has_channel(ign, c, nm) else "[x]" if has_channel(wc, c, nm) else "[ ]"
        kb.append([{"text": f"{mark} {disp(c)}"[:60], "callback_data": f"ch:{c}:f{fh}"}])
    if pages > 1:
        kb.append(nav_row(page, pages, f"fo:{fh}:"))
    kb += [[{"text": "Follow all", "callback_data": f"fall:{fh}:1"},
            {"text": "Unfollow all", "callback_data": f"fall:{fh}:0"}],
           [{"text": "Rename", "callback_data": f"fren:{fh}"},
            {"text": "Delete", "callback_data": f"fdel:{fh}"}],
           [{"text": "< Folders", "callback_data": "fl:0"}]]
    txt = f"Folder: {name}\n" + (f"{len(ids)} channels. Tap one for its settings." if ids else
                            "Empty. Open a channel's edit screen and pick this folder.")
    return txt, {"inline_keyboard": kb}


def show(chat_id, msg_id, txt, kb):
    r = tg("editMessageText", chat_id=chat_id, message_id=msg_id, text=txt, reply_markup=kb) if msg_id else None
    if r and r.get("ok"):
        return
    if r and "not modified" in str(r.get("description", "")).lower():
        return  # same content, don't send a duplicate
    tg("sendMessage", chat_id=chat_id, text=txt, reply_markup=kb)


# ---------- handlers ----------
def _callback(cb, chat_id, msg_id, data):
    """Returns a short toast text (or None)."""
    if data.startswith("menu:"):
        name = data[5:]
        show(chat_id, msg_id, *(main_menu() if name == "main" else section_menu(name)))
    elif data.startswith("pick:"):
        show(chat_id, msg_id, *pick_menu(int(data[5:])))
    elif data.startswith("srv:"):
        _, sid, page = data.split(":")
        show(chat_id, msg_id, *server_menu(sid, int(page)))
    elif data.startswith("tgs:"):
        _, sid, page = data.split(":")
        g = next((n for n in servers_map() if str(crc(n)) == sid), None)
        note = None
        if g:
            note = "Following the whole server" if toggle_server(g) else "Whole server removed"
        show(chat_id, msg_id, *server_menu(sid, int(page)))
        return note
    elif data.startswith(("tog:", "ign:")):
        op, cid, sid, page = data.split(":")
        name, note = chan_name_by_id(cid), None
        if name:
            if op == "ign":
                note = "Ignoring" if toggle_channel("ignore_channels", cid, name) else "Unignored"
            elif has_channel(channel_entries("ignore_channels"), cid, name):
                note = "It's ignored, unignore it first"
            else:
                note = "Following" if toggle_channel("watch_channels", cid, name) else "Removed"
        show(chat_id, msg_id, *server_menu(sid, int(page)))
        return note
    elif data.startswith("ch:"):
        _, cid, ctx = data.split(":", 2)
        show(chat_id, msg_id, *channel_screen(cid, ctx))
    elif data.startswith(("ctg:", "cig:")):
        op, cid, ctx = data.split(":", 2)
        if op == "cig":
            note = "Ignoring" if toggle_channel("ignore_channels", cid) else "Unignored"
        elif has_channel(channel_entries("ignore_channels"), cid, real_name(cid)):
            note = "It's ignored, unignore it first"
        else:
            note = "Following" if toggle_channel("watch_channels", cid) else "Unfollowed"
        show(chat_id, msg_id, *channel_screen(cid, ctx))
        return note
    elif data.startswith("cren:"):
        _, cid, ctx = data.split(":", 2)
        update_state(wait={"op": "alias", "kind": cid, "chat": chat_id, "ctx": ctx})
        tg_send(f"Send a new name for {real_name(cid) or 'channel ' + cid}.\n"
                "Send - to go back to the real name, /cancel to stop.", chat=chat_id)
    elif data.startswith("cmv:"):
        _, cid, ctx = data.split(":", 2)
        show(chat_id, msg_id, *move_menu(cid, ctx))
    elif data.startswith("cmt:"):
        _, cid, fh, ctx = data.split(":", 3)
        name = find_folder(fh) if fh != "0" else None
        set_folder(cid, name)
        show(chat_id, msg_id, *channel_screen(cid, ctx))
        return f"Moved to {name}" if name else "Taken out of the folder"
    elif data.startswith("cnf:"):
        _, cid, ctx = data.split(":", 2)
        update_state(wait={"op": "folder_new", "kind": "", "chat": chat_id, "cid": cid, "ctx": ctx})
        tg_send("Send a name for the new folder. /cancel to stop.", chat=chat_id)
    elif data.startswith("fl:"):
        show(chat_id, msg_id, *folders_menu(int(data[3:])))
    elif data.startswith("fo:"):
        _, fh, page = data.split(":")
        show(chat_id, msg_id, *folder_menu(fh, int(page)))
    elif data == "fnew":
        update_state(wait={"op": "folder_new", "kind": "", "chat": chat_id})
        tg_send("Send a name for the new folder. /cancel to stop.", chat=chat_id)
    elif data.startswith("fren:"):
        name = find_folder(data[5:])
        if name:
            update_state(wait={"op": "folder_rename", "kind": name, "chat": chat_id})
            tg_send(f"Send the new name for the folder '{name}'. /cancel to stop.", chat=chat_id)
    elif data.startswith("fall:"):
        _, fh, on = data.split(":")
        name, n = find_folder(fh), 0
        if name:
            ign = channel_entries("ignore_channels")
            for c in folders()[name]:
                if on == "1" and has_channel(ign, c, real_name(c)):
                    continue  # ignored channels stay ignored
                set_channel("watch_channels", c, on == "1")
                n += 1
        show(chat_id, msg_id, *folder_menu(fh, 0))
        return f"{'Following' if on == '1' else 'Unfollowed'} {n} channels"
    elif data.startswith("fdel:"):
        fh = data[5:]
        name = find_folder(fh)
        if name:
            show(chat_id, msg_id, f"Delete the folder '{name}'? The channels themselves stay as they are.",
                 {"inline_keyboard": [[{"text": "Yes, delete", "callback_data": f"fdely:{fh}"},
                                       {"text": "Cancel", "callback_data": f"fo:{fh}:0"}]]})
    elif data.startswith("fdely:"):
        name = find_folder(data[6:])
        if name:
            cfg = load_cfg()
            cfg["folders"].pop(name, None)
            save_cfg(cfg)
        show(chat_id, msg_id, *folders_menu(0))
        return "Folder deleted"
    elif data.startswith("rl:"):
        kind = data[3:]
        if kind in KEYS:
            show(chat_id, msg_id, *remove_menu(kind))
    elif data.startswith("rm1:"):
        _, kind, h = data.split(":")
        key, note = KEYS.get(kind), None
        if key:
            cfg = load_cfg()
            lst = cfg.get(key, [])
            for i, x in enumerate(lst):
                if str(crc(x)) == h:
                    note = f"Removed: {disp(x) if kind in ('channel', 'ignore_channel') else srv_label(x) if kind == 'server' else x}"[:150]
                    lst.pop(i)
                    break
            cfg[key] = lst
            save_cfg(cfg)
            show(chat_id, msg_id, *remove_menu(kind))
        return note
    elif data.startswith(("lnkw:", "lnki:", "lnks:")):
        kind = {"lnkw": "channel", "lnki": "ignore_channel", "lnks": "server"}[data[:4]]
        res = modify_list("add", kind, data[5:])
        tg("editMessageText", chat_id=chat_id, message_id=msg_id, text=res)
        return res.split("\n")[0][:150]
    elif data.startswith("add:"):
        kind = data[4:]
        if kind in KEYS:
            update_state(wait={"op": "add", "kind": kind, "chat": chat_id})
            tg_send(f"Send me the {LABELS[kind]} to add"
                    + (" (just paste a channel link)" if kind in ("channel", "ignore_channel")
                       else " (a server link works too)" if kind == "server" else "")
                    + ". /cancel to stop.", chat=chat_id)
    elif data.startswith("rm:"):  # buttons from old messages
        kind = data[3:]
        if kind in KEYS:
            show(chat_id, msg_id, *remove_menu(kind))
    elif data == "toggle_pause":
        st = update_state(paused=not load_state().get("paused", False))
        show(chat_id, msg_id, *main_menu())
        return "Paused" if st["paused"] else "Running"
    elif data == "reset_filters":
        show(chat_id, msg_id, "Delete ALL filters (channels, servers, users, keywords)?",
             {"inline_keyboard": [[{"text": "Yes, reset", "callback_data": "reset_yes"},
                                   {"text": "Cancel", "callback_data": "menu:main"}]]})
    elif data == "reset_yes":
        old = load_cfg()
        save_cfg({k: old[k] for k in ("notification_chat", "aliases", "folders") if k in old})  # not filters, keep them
        show(chat_id, msg_id, *main_menu())
        return "Filters cleared"
    elif data == "restart":
        return "Restarting" if Bridge.start() else "No Discord token yet"
    return None


def handle_callback(cb):
    msg = cb.get("message")
    answer = {"callback_query_id": cb["id"]}
    if msg and str(msg["chat"]["id"]) in admins():
        try:
            note = _callback(cb, str(msg["chat"]["id"]), msg["message_id"], cb.get("data", ""))
            if note:
                answer["text"] = note
        except Exception as e:
            log(f"callback err: {e!r}"[:200])
    tg("answerCallbackQuery", **answer)


def offer_link(chat_id, m):
    gid, cid = m.groups()
    if gid == "@me":
        return tg_send("DM links aren't supported. Use /follow user Name instead.", chat=chat_id)
    if cid:  # .../channels/<server>/<channel>
        name = chan_name_by_id(cid)
        kb = {"inline_keyboard": [[{"text": "Follow", "callback_data": f"lnkw:{cid}"},
                                   {"text": "Ignore", "callback_data": f"lnki:{cid}"}]]}
        text = f"Channel: {name}" if name else f"Channel {cid} (haven't seen it yet, it will be tracked by id)"
    else:  # .../channels/<server>/ opens the first channel of the server
        name = server_name_by_id(gid)
        kb = {"inline_keyboard": [[{"text": "Follow whole server", "callback_data": f"lnks:{gid}"}]]}
        text = f"Server: {name}" if name else f"Server {gid} (haven't seen it yet, it will be tracked by id)"
    tg("sendMessage", chat_id=chat_id, text=text, reply_markup=kb)


def handle_wait(w, text, chat_id, reply):
    """The user answered a 'send me the value' prompt."""
    op = w.get("op")
    if op in ("add", "remove"):
        return reply(modify_list(op, w["kind"], text))
    screen = lambda scr: tg("sendMessage", chat_id=chat_id, text=scr[0], reply_markup=scr[1])
    if op == "alias":
        cid, cfg = w["kind"], load_cfg()
        names = cfg.get("aliases", {})
        if text.strip() == "-":
            names.pop(cid, None)
            reply("Back to the real name.")
        else:
            names[cid] = text.strip()[:40]
            reply(f"Renamed to: {names[cid]}")
        cfg["aliases"] = names
        save_cfg(cfg)
        return screen(channel_screen(cid, w.get("ctx", "")))
    if op in ("folder_new", "folder_rename"):
        name = text.strip()[:40]
        old = w.get("kind") if op == "folder_rename" else None
        cfg = load_cfg()
        fl = cfg.get("folders", {})
        if not name:
            return reply("The name can't be empty.")
        if any(n.lower() == name.lower() and n != old for n in fl):
            return reply("There's already a folder with that name.")
        if op == "folder_new":
            fl.setdefault(name, [])
        else:
            fl = {(name if n == old else n): v for n, v in fl.items()}
        cfg["folders"] = fl
        save_cfg(cfg)
        if op == "folder_new" and w.get("cid"):
            set_folder(w["cid"], name)
            return screen(channel_screen(w["cid"], w.get("ctx", "")))
        return screen(folder_menu(str(crc(name)), 0))


def handle_message(msg):
    chat_id = str(msg["chat"]["id"])
    text = (msg.get("text") or "").strip()
    if not text:
        return
    cmd, _, arg = text.partition(" ")
    cmd, arg = cmd.split("@")[0].lower(), arg.strip()
    st = load_state()
    admin = str(st.get("admin_chat", ""))

    def reply(t, **kw):
        kw.pop("chat", None)
        return tg_send(t, chat=chat_id, **kw)

    if not admin:
        if cmd == "/start":
            pin = os.environ.get("TG_ADMIN_CHAT", "").strip()
            if pin and pin != chat_id:
                log(f"/start from {chat_id} rejected (TG_ADMIN_CHAT is set)")
                return
            update_state(admin_chat=chat_id)
            reply("Hi, you're set as admin.\n\nNow send your Discord token:\n/usertoken TOKEN for your account\n/bottoken TOKEN for a bot\n\n"
                  "Account token: open Discord in the browser, F12, Network tab, click any request, copy the Authorization header.\n"
                  "I'll delete your message with the token right away.")
            log(f"admin: {chat_id}")
        return
    if chat_id not in admins(st):
        return  # ignore strangers

    if cmd in ("/start", "/menu"):
        tg("sendMessage", chat_id=chat_id, text=main_menu()[0], reply_markup=main_menu()[1])
    elif cmd in ("/usertoken", "/bottoken"):
        r = tg("deleteMessage", chat_id=chat_id, message_id=msg["message_id"])
        deleted = bool(r and r.get("ok"))
        arg = arg.strip().strip("\"'`")
        if not arg:
            return reply(f"Usage: {cmd} TOKEN")
        save_secret(USER_TOKEN_FILE if cmd == "/usertoken" else BOT_TOKEN_FILE, arg)
        update_state(prefer="user" if cmd == "/usertoken" else "bot")
        ok = Bridge.start()
        reply(("Account token" if cmd == "/usertoken" else "Bot token") + " saved. "
              + ("Message deleted. " if deleted else "Warning: couldn't delete your message, please delete it yourself. ")
              + ("Starting the bridge." if ok else "Couldn't start the bridge."))
    elif cmd == "/token":
        tg("deleteMessage", chat_id=chat_id, message_id=msg["message_id"])
        reply("Use /usertoken TOKEN or /bottoken TOKEN.")
    elif cmd == "/reset":
        for f in (USER_TOKEN_FILE, BOT_TOKEN_FILE):
            try:
                os.remove(f)
            except OSError:
                pass
        Bridge.stop()
        reply("Tokens deleted, bridge stopped.")
    elif cmd in ("/addadmin", "/removeadmin", "/admins"):
        if cmd == "/admins":
            return reply("Admins: " + ", ".join(admins(st)))
        if chat_id != admin:
            return reply("Only the main admin can do that.")
        if not arg.lstrip("-").isdigit():
            return reply(f"Usage: {cmd} CHAT_ID (the person can get it from @userinfobot)")
        extra = [x for x in st.get("admins", []) if str(x) != arg]
        if cmd == "/addadmin":
            extra.append(arg)
        update_state(admins=extra)
        reply(("Added admin " if cmd == "/addadmin" else "Removed admin ") + arg)
        if cmd == "/addadmin":
            tg_send("You've been added as an admin. Send /menu to start.", chat=arg)
    elif cmd == "/cancel":
        update_state(wait=None)
        reply("Okay, cancelled.")
    elif cmd == "/status":
        cfg = load_cfg()
        reply(f"Paused: {st.get('paused')}\nMode: {st.get('mode', '?')}\n"
              f"Bridge: {'running' if Bridge.proc and Bridge.proc.poll() is None else 'not running'}\n"
              + "\n".join(f"{LABELS[k]}: {fmt_list(cfg, k)}" for k in KEYS))
    elif cmd in ("/pause", "/resume"):
        update_state(paused=(cmd == "/pause"))
        reply("Paused." if cmd == "/pause" else "Resumed.")
    elif cmd == "/channels":
        known = sorted(st.get("known_channels", {}))
        reply("Channels I can see:\n" + "\n".join(known) if known else "Nothing yet. The bridge hasn't connected.")
    elif cmd in ("/follow", "/unfollow"):
        # /follow user Name | /follow channel Server/#channel (or a link)
        kind, _, val = arg.partition(" ")
        kind = {"user": "follow_user", "channel": "channel"}.get(kind.lower())
        if not kind or not val.strip():
            return reply("Usage:\n/follow user Name\n/follow channel Server/#channel\n/unfollow does the opposite")
        reply(modify_list("add" if cmd == "/follow" else "remove", kind, val.strip()))
    elif cmd in ("/ignore", "/unignore"):
        if not arg:
            return reply(f"Usage: {cmd} Server/#channel (or a Discord channel link)")
        reply(modify_list("add" if cmd == "/ignore" else "remove", "ignore_channel", arg))
    elif cmd in ("/notify_user", "/notify_keyword"):
        reply(modify_list("add", cmd[1:], arg) if arg else f"Usage: {cmd} value")
    elif cmd.startswith(("/add_", "/remove_")):
        op, _, kind = cmd[1:].partition("_")
        if kind not in KEYS:
            return reply("Unknown type. Use one of: " + ", ".join(KEYS))
        reply(modify_list(op, kind, arg) if arg else f"Usage: {cmd} value")
    elif not cmd.startswith("/"):
        w = st.get("wait")
        if w and w.get("chat") in (None, chat_id):
            update_state(wait=None)
            handle_wait(w, text, chat_id, reply)
        else:
            m = LINK_RE.search(text)
            if m:
                offer_link(chat_id, m)


def tg_poll():
    offset = 0
    log("TG polling started")
    while True:
        try:
            d = tg("getUpdates", timeout=50, offset=offset, allowed_updates=["message", "callback_query"])
            if not d or not d.get("ok"):
                time.sleep(5)
                continue
            for u in d.get("result", []):
                offset = u["update_id"] + 1
                try:
                    if "callback_query" in u:
                        handle_callback(u["callback_query"])
                    elif u.get("message"):
                        handle_message(u["message"])
                except Exception as e:
                    log(f"update err: {e!r}"[:200])
        except Exception as e:
            log(f"poll err: {str(e)[:80]}")
            time.sleep(5)


if __name__ == "__main__":
    tg_token()  # fail fast if no token
    tg("deleteWebhook")  # getUpdates fails if a webhook is set
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # systemd stop: shut the bridge down too
    if Bridge.start():
        log("bridge started")
    else:
        log("no Discord token yet, waiting for /usertoken or /bottoken")
    threading.Thread(target=Bridge.watchdog, daemon=True).start()
    try:
        tg_poll()
    except KeyboardInterrupt:
        pass
    finally:
        Bridge.shutdown = True
        Bridge.stop()
