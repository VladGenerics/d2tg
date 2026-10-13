# -*- coding: utf-8 -*-
"""discord2tg.py — управляющий бот в Telegram: меню настроек + запуск Discord-моста.
Токен Telegram: переменная TG_TOKEN или файл .tg_token рядом со скриптом."""
import os, sys, time, subprocess, threading
from discord2tg_common import *

# menu section -> (title, [keys from KEYS])
SECTIONS = {
    "channels": ("Channels", ["channel", "server", "ignore_channel"]),
    "users": ("Users", ["follow_user", "user", "ignore_user"]),
    "keywords": ("Keywords", ["keyword", "exclude"]),
    "notify": ("Notifications", ["notify_user", "notify_keyword"]),
}
LABELS = {
    "channel": "Channel (Server/#name)", "server": "Whole server", "ignore_channel": "Ignore channel",
    "follow_user": "Follow user (everywhere)", "user": "Only from user (in channels)", "ignore_user": "Ignore user",
    "keyword": "Keyword", "exclude": "Exclude keyword",
    "notify_user": "Notify user", "notify_keyword": "Notify keyword",
}


# ---------- Discord process control ----------
class Bridge:
    proc = None
    lock = threading.Lock()

    @staticmethod
    def script():
        if os.path.exists(USER_TOKEN_FILE) or os.environ.get("DISCORD_USER_TOKEN"):
            return "discord2tg_self.py"
        if os.path.exists(BOT_TOKEN_FILE) or os.environ.get("DISCORD_TOKEN"):
            return "discord2tg_discord.py"
        return None

    @classmethod
    def stop(cls):
        with cls.lock:
            if cls.proc and cls.proc.poll() is None:
                cls.proc.terminate()
                try:
                    cls.proc.wait(10)
                except subprocess.TimeoutExpired:
                    cls.proc.kill()
            cls.proc = None

    @classmethod
    def start(cls):
        cls.stop()
        s = cls.script()
        if not s:
            return False
        with cls.lock:
            log(f"starting {s}")
            cls.proc = subprocess.Popen([sys.executable, os.path.join(BASE, s)], cwd=BASE)
        return True

    @classmethod
    def watchdog(cls):
        while True:
            time.sleep(15)
            p = cls.proc
            if p is not None and p.poll() is not None and cls.script():
                log(f"bridge exited (code {p.returncode}), restarting")
                time.sleep(5)
                cls.start()


# ---------- menu ----------
def fmt_list(cfg, k):
    v = cfg.get(KEYS[k], [])
    return ", ".join(v) if v else "—"


def main_menu():
    cfg, st = load_cfg(), load_state()
    mode = st.get("mode", "?")
    txt = (f"Discord → TG bridge\nStatus: {'paused' if st.get('paused') else 'running'} (mode: {mode})\n"
           f"Servers: {len(cfg.get('watch_servers', []))}, channels: {len(cfg.get('watch_channels', []))}\n"
           f"Users: {len(cfg.get('watch_users', []))}, keywords: {len(cfg.get('search_keywords', []))}\n\n"
           "Nothing is forwarded yet. Add a channel or a server first.")
    kb = {"inline_keyboard": [
        [{"text": "Channels", "callback_data": "menu:channels"}, {"text": "Users", "callback_data": "menu:users"}],
        [{"text": "Keywords", "callback_data": "menu:keywords"}, {"text": "Notifications", "callback_data": "menu:notify"}],
        [{"text": "Pause / Resume", "callback_data": "toggle_pause"}],
        [{"text": "Restart bridge", "callback_data": "restart"},
         {"text": "Reset filters", "callback_data": "reset_filters"}],
    ]}
    return txt, kb


def section_menu(name):
    cfg = load_cfg()
    title, keys = SECTIONS[name]
    txt = title + "\n\n" + "\n".join(f"{LABELS[k]}: {fmt_list(cfg, k)}" for k in keys)
    txt += "\n\nTap a button and send the value, or type /add_<type> value"
    kb = []
    for k in keys:
        kb.append([{"text": f"➕ {LABELS[k]}", "callback_data": f"add:{k}"},
                   {"text": "➖", "callback_data": f"rm:{k}"}])
    if name == "channels":
        kb.insert(0, [{"text": "Pick from list", "callback_data": "pick:0"}])
    kb.append([{"text": "◀️ Back", "callback_data": "menu:main"}])
    return txt, {"inline_keyboard": kb}


PAGE = 8


def pick_menu(page):
    """Список известных каналов с галочками; нажатие = follow/unfollow."""
    known = sorted(load_state().get("known_channels", {}))
    watched = {x.lower() for x in load_cfg().get("watch_channels", [])}
    if not known:
        return "No channels yet. The bridge hasn't connected to Discord.", \
            {"inline_keyboard": [[{"text": "◀️ Back", "callback_data": "menu:channels"}]]}
    pages = (len(known) - 1) // PAGE + 1
    page = max(0, min(page, pages - 1))
    kb = []
    for i in range(page * PAGE, min(len(known), (page + 1) * PAGE)):
        mark = "✅" if known[i].lower() in watched else "▫️"
        kb.append([{"text": f"{mark} {known[i]}"[:60], "callback_data": f"tog:{i}:{page}"}])
    nav = []
    if page > 0:
        nav.append({"text": "⬅️", "callback_data": f"pick:{page - 1}"})
    nav.append({"text": f"{page + 1}/{pages}", "callback_data": "noop"})
    if page < pages - 1:
        nav.append({"text": "➡️", "callback_data": f"pick:{page + 1}"})
    kb.append(nav)
    kb.append([{"text": "◀️ Back", "callback_data": "menu:channels"}])
    return "Tap a channel to follow or unfollow:", {"inline_keyboard": kb}


def show(chat_id, msg_id, txt, kb):
    r = tg("editMessageText", chat_id=chat_id, message_id=msg_id, text=txt, reply_markup=kb) if msg_id else None
    if not r or not r.get("ok"):
        tg("sendMessage", chat_id=chat_id, text=txt, reply_markup=kb)


def modify_list(op, kind, val):
    """op: add|remove. Возвращает текст ответа."""
    cfg = load_cfg()
    key = KEYS[kind]
    lst = cfg.get(key, [])
    if op == "add":
        if val not in lst:
            lst.append(val)
        msg = f"Added {LABELS[kind]}: {val}"
    else:
        low = [x.lower() for x in lst]
        if val.lower() in low:
            lst.pop(low.index(val.lower()))
            msg = f"Removed {LABELS[kind]}: {val}"
        else:
            msg = f"Not in the list: {val}"
    cfg[key] = lst
    save_cfg(cfg)
    return msg


# ---------- handlers ----------
def handle_callback(cb):
    chat_id = str(cb["message"]["chat"]["id"])
    msg_id = cb["message"]["message_id"]
    data = cb.get("data", "")
    answer = {"callback_query_id": cb["id"]}
    if chat_id != str(load_state().get("admin_chat", "")):
        tg("answerCallbackQuery", **answer)
        return
    note = None
    if data.startswith("menu:"):
        name = data[5:]
        show(chat_id, msg_id, *(main_menu() if name == "main" else section_menu(name)))
    elif data.startswith("pick:"):
        show(chat_id, msg_id, *pick_menu(int(data[5:])))
    elif data.startswith("tog:"):
        _, i, page = data.split(":")
        known = sorted(load_state().get("known_channels", {}))
        if int(i) < len(known):
            ch = known[int(i)]
            cfg = load_cfg()
            lst = cfg.get("watch_channels", [])
            low = [x.lower() for x in lst]
            if ch.lower() in low:
                lst.pop(low.index(ch.lower()))
                note = "Removed"
            else:
                lst.append(ch)
                note = "Following"
            cfg["watch_channels"] = lst
            save_cfg(cfg)
        show(chat_id, msg_id, *pick_menu(int(page)))
    elif data.startswith(("add:", "rm:")):
        op, kind = data.split(":", 1)
        update_state(wait={"op": "add" if op == "add" else "remove", "kind": kind})
        tg_send(f"Send me the {LABELS[kind]} to {'add' if op == 'add' else 'remove'}. /cancel to stop.", chat=chat_id)
    elif data == "toggle_pause":
        st = update_state(paused=not load_state().get("paused", False))
        note = "Paused" if st["paused"] else "Running"
        show(chat_id, msg_id, *main_menu())
    elif data == "reset_filters":
        save_cfg({})
        note = "Filters cleared"
        show(chat_id, msg_id, *main_menu())
    elif data == "restart":
        note = "Restarting" if Bridge.start() else "No Discord token yet"
    if note:
        answer["text"] = note
    tg("answerCallbackQuery", **answer)


def handle_message(msg):
    chat_id = str(msg["chat"]["id"])
    text = (msg.get("text") or "").strip()
    if not text:
        return
    cmd, _, arg = text.partition(" ")
    cmd, arg = cmd.split("@")[0].lower(), arg.strip()
    st = load_state()
    admin = str(st.get("admin_chat", ""))

    if not admin:
        if cmd == "/start":
            update_state(admin_chat=chat_id)
            tg_send("Hi, you're set as admin.\n\nNow send your Discord token:\n/usertoken TOKEN for your account\n/bottoken TOKEN for a bot\n\n"
                    "Account token: open Discord in the browser, F12, Network tab, click any request, copy the Authorization header.\n"
                    "I'll delete your message with the token right away.", chat=chat_id)
            log(f"admin: {chat_id}")
        return
    if chat_id != admin:
        return  # ignore strangers

    if cmd in ("/start", "/menu"):
        tg("sendMessage", chat_id=chat_id, text=main_menu()[0], reply_markup=main_menu()[1])
    elif cmd in ("/usertoken", "/bottoken"):
        tg("deleteMessage", chat_id=chat_id, message_id=msg["message_id"])
        if not arg:
            return tg_send(f"Usage: {cmd} TOKEN")
        save_secret(USER_TOKEN_FILE if cmd == "/usertoken" else BOT_TOKEN_FILE, arg)
        ok = Bridge.start()
        tg_send(("Account token" if cmd == "/usertoken" else "Bot token") +
                " saved, message deleted. " + ("Starting the bridge." if ok else "Couldn't start the bridge."))
    elif cmd == "/token":
        tg("deleteMessage", chat_id=chat_id, message_id=msg["message_id"])
        tg_send("Use /usertoken TOKEN or /bottoken TOKEN.")
    elif cmd == "/reset":
        for f in (USER_TOKEN_FILE, BOT_TOKEN_FILE):
            try:
                os.remove(f)
            except OSError:
                pass
        Bridge.stop()
        tg_send("Tokens deleted, bridge stopped.")
    elif cmd == "/cancel":
        update_state(wait=None)
        tg_send("Okay, cancelled.")
    elif cmd == "/status":
        cfg = load_cfg()
        tg_send(f"Paused: {st.get('paused')}\nMode: {st.get('mode', '?')}\n"
                f"Bridge: {'running' if Bridge.proc and Bridge.proc.poll() is None else 'not running'}\n"
                + "\n".join(f"{LABELS[k]}: {fmt_list(cfg, k)}" for k in KEYS))
    elif cmd in ("/pause", "/resume"):
        update_state(paused=(cmd == "/pause"))
        tg_send("Paused." if cmd == "/pause" else "Resumed.")
    elif cmd == "/channels":
        known = sorted(st.get("known_channels", {}))
        tg_send("Channels I can see:\n" + "\n".join(known) if known else "Nothing yet. The bridge hasn't connected.")
    elif cmd in ("/follow", "/unfollow"):
        # /follow user Name | /follow channel Server/#channel
        kind, _, val = arg.partition(" ")
        kind = {"user": "follow_user", "channel": "channel"}.get(kind.lower())
        if not kind or not val.strip():
            return tg_send("Usage:\n/follow user Name\n/follow channel Server/#channel\n/unfollow does the opposite")
        tg_send(modify_list("add" if cmd == "/follow" else "remove", kind, val.strip()))
    elif cmd in ("/notify_user", "/notify_keyword"):
        tg_send(modify_list("add", cmd[1:], arg) if arg else f"Usage: {cmd} value")
    elif cmd.startswith(("/add_", "/remove_")):
        op, _, kind = cmd[1:].partition("_")
        if kind not in KEYS:
            return tg_send("Unknown type. Use one of: " + ", ".join(KEYS))
        tg_send(modify_list(op, kind, arg) if arg else f"Usage: {cmd} value")
    elif not cmd.startswith("/") and st.get("wait"):
        w = st["wait"]
        update_state(wait=None)
        tg_send(modify_list(w["op"], w["kind"], text))


def tg_poll():
    offset = 0
    log("TG polling started")
    while True:
        try:
            d = tg("getUpdates", timeout=50, offset=offset, allowed_updates=["message", "callback_query"])
            if d is None:
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
    if Bridge.start():
        log("bridge started")
    else:
        log("no Discord token yet, waiting for /usertoken or /bottoken")
    threading.Thread(target=Bridge.watchdog, daemon=True).start()
    try:
        tg_poll()
    except KeyboardInterrupt:
        Bridge.stop()
