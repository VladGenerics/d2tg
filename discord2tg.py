# -*- coding: utf-8 -*-
"""discord2tg.py — управляющий бот в Telegram: меню настроек + запуск Discord-моста.
Токен Telegram: переменная TG_TOKEN или файл .tg_token рядом со скриптом."""
import os, sys, time, subprocess, threading
from discord2tg_common import *

# раздел меню -> (заголовок, [ключи из KEYS])
SECTIONS = {
    "channels": ("📡 Каналы", ["channel", "server", "ignore_channel"]),
    "users": ("👤 Юзеры", ["follow_user", "user", "ignore_user"]),
    "keywords": ("🔍 Слова", ["keyword", "exclude"]),
    "notify": ("🔔 Уведомления", ["notify_user", "notify_keyword"]),
}
LABELS = {
    "channel": "Канал (Сервер/#имя)", "server": "Сервер целиком", "ignore_channel": "Игнор-канал",
    "follow_user": "👁 Следить за юзером (везде)", "user": "Только от юзера (в каналах)", "ignore_user": "Игнор-юзер",
    "keyword": "Ключевое слово", "exclude": "Слово-исключение",
    "notify_user": "🔔 Юзер", "notify_keyword": "🔔 Слово",
}


# ---------- управление Discord-процессом ----------
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
            log(f"запуск {s}")
            cls.proc = subprocess.Popen([sys.executable, os.path.join(BASE, s)], cwd=BASE)
        return True

    @classmethod
    def watchdog(cls):
        while True:
            time.sleep(15)
            p = cls.proc
            if p is not None and p.poll() is not None and cls.script():
                log(f"мост упал (код {p.returncode}), перезапуск")
                time.sleep(5)
                cls.start()


# ---------- меню ----------
def fmt_list(cfg, k):
    v = cfg.get(KEYS[k], [])
    return ", ".join(v) if v else "—"


def main_menu():
    cfg, st = load_cfg(), load_state()
    mode = st.get("mode", "?")
    txt = (f"🎛 Discord → TG\n\nСтатус: {'⏸ ПАУЗА' if st.get('paused') else '🟢 работает'} (режим: {mode})\n"
           f"Серверов: {len(cfg.get('watch_servers', []))}, каналов: {len(cfg.get('watch_channels', []))}\n"
           f"Юзеров: {len(cfg.get('watch_users', []))}, слов: {len(cfg.get('search_keywords', []))}\n\n"
           "Пока нет ни каналов, ни серверов — ничего не пересылается.")
    kb = {"inline_keyboard": [
        [{"text": "📡 Каналы", "callback_data": "menu:channels"}, {"text": "👤 Юзеры", "callback_data": "menu:users"}],
        [{"text": "🔍 Слова", "callback_data": "menu:keywords"}, {"text": "🔔 Уведомления", "callback_data": "menu:notify"}],
        [{"text": "⏸ Пауза / ▶️ Продолжить", "callback_data": "toggle_pause"}],
        [{"text": "🔄 Перезапустить мост", "callback_data": "restart"},
         {"text": "❌ Сбросить фильтры", "callback_data": "reset_filters"}],
    ]}
    return txt, kb


def section_menu(name):
    cfg = load_cfg()
    title, keys = SECTIONS[name]
    txt = title + "\n\n" + "\n".join(f"{LABELS[k]}: {fmt_list(cfg, k)}" for k in keys)
    txt += "\n\nНажми кнопку и пришли значение. Или командой: /add_<тип> значение, /remove_<тип> значение"
    kb = []
    for k in keys:
        kb.append([{"text": f"➕ {LABELS[k]}", "callback_data": f"add:{k}"},
                   {"text": "➖", "callback_data": f"rm:{k}"}])
    if name == "channels":
        kb.insert(0, [{"text": "📋 Выбрать каналы из списка", "callback_data": "pick:0"}])
    kb.append([{"text": "◀️ Назад", "callback_data": "menu:main"}])
    return txt, {"inline_keyboard": kb}


PAGE = 8


def pick_menu(page):
    """Список известных каналов с галочками; нажатие = follow/unfollow."""
    known = sorted(load_state().get("known_channels", {}))
    watched = {x.lower() for x in load_cfg().get("watch_channels", [])}
    if not known:
        return "Список каналов пуст — мост ещё не подключился к Discord.", \
            {"inline_keyboard": [[{"text": "◀️ Назад", "callback_data": "menu:channels"}]]}
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
    kb.append([{"text": "◀️ Назад", "callback_data": "menu:channels"}])
    return "Нажми на канал, чтобы следить (✅) или перестать:", {"inline_keyboard": kb}


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
        msg = f"✅ Добавлено ({LABELS[kind]}): {val}"
    else:
        low = [x.lower() for x in lst]
        if val.lower() in low:
            lst.pop(low.index(val.lower()))
            msg = f"✅ Убрано ({LABELS[kind]}): {val}"
        else:
            msg = f"Не найдено: {val}"
    cfg[key] = lst
    save_cfg(cfg)
    return msg


# ---------- обработчики ----------
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
                note = "Убран"
            else:
                lst.append(ch)
                note = "Следим ✅"
            cfg["watch_channels"] = lst
            save_cfg(cfg)
        show(chat_id, msg_id, *pick_menu(int(page)))
    elif data.startswith(("add:", "rm:")):
        op, kind = data.split(":", 1)
        update_state(wait={"op": "add" if op == "add" else "remove", "kind": kind})
        tg_send(f"Пришли значение: {LABELS[kind]} ({'добавить' if op == 'add' else 'убрать'}). /cancel — отмена", chat=chat_id)
    elif data == "toggle_pause":
        st = update_state(paused=not load_state().get("paused", False))
        note = "⏸ Пауза" if st["paused"] else "▶️ Работает"
        show(chat_id, msg_id, *main_menu())
    elif data == "reset_filters":
        save_cfg({})
        note = "Фильтры сброшены"
        show(chat_id, msg_id, *main_menu())
    elif data == "restart":
        note = "Перезапуск…" if Bridge.start() else "Нет токена Discord"
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
            tg_send("✅ Ты админ.\n\nТокен Discord:\n/usertoken ТОКЕН — твой аккаунт\n/bottoken ТОКЕН — обычный бот\n\n"
                    "Как взять токен аккаунта: Discord в браузере → F12 → Network → любой запрос → Authorization.\n"
                    "Сообщение с токеном я сразу удалю.", chat=chat_id)
            log(f"admin: {chat_id}")
        return
    if chat_id != admin:
        return  # чужие игнорируем

    if cmd in ("/start", "/menu"):
        tg("sendMessage", chat_id=chat_id, text=main_menu()[0], reply_markup=main_menu()[1])
    elif cmd in ("/usertoken", "/bottoken"):
        tg("deleteMessage", chat_id=chat_id, message_id=msg["message_id"])
        if not arg:
            return tg_send(f"Формат: {cmd} ТОКЕН")
        save_secret(USER_TOKEN_FILE if cmd == "/usertoken" else BOT_TOKEN_FILE, arg)
        ok = Bridge.start()
        tg_send(("👤 Токен аккаунта" if cmd == "/usertoken" else "🤖 Токен бота") +
                " сохранён, сообщение удалено. " + ("Мост перезапущен." if ok else "Мост не запущен."))
    elif cmd == "/token":
        tg("deleteMessage", chat_id=chat_id, message_id=msg["message_id"])
        tg_send("Используй явно: /usertoken ТОКЕН (аккаунт) или /bottoken ТОКЕН (бот).")
    elif cmd == "/reset":
        for f in (USER_TOKEN_FILE, BOT_TOKEN_FILE):
            try:
                os.remove(f)
            except OSError:
                pass
        Bridge.stop()
        tg_send("🗑 Токены удалены, мост остановлен.")
    elif cmd == "/cancel":
        update_state(wait=None)
        tg_send("Отменено.")
    elif cmd == "/status":
        cfg = load_cfg()
        tg_send(f"⏸ Пауза: {st.get('paused')}\nРежим: {st.get('mode', '?')}\n"
                f"Мост: {'жив' if Bridge.proc and Bridge.proc.poll() is None else 'не запущен'}\n"
                + "\n".join(f"{LABELS[k]}: {fmt_list(cfg, k)}" for k in KEYS))
    elif cmd in ("/pause", "/resume"):
        update_state(paused=(cmd == "/pause"))
        tg_send("⏸ Пауза включена" if cmd == "/pause" else "▶️ Пауза снята")
    elif cmd == "/channels":
        known = sorted(st.get("known_channels", {}))
        tg_send("Известные каналы:\n" + "\n".join(known) if known else "Список пуст (мост ещё не подключился).")
    elif cmd in ("/follow", "/unfollow"):
        # /follow user Имя | /follow channel Сервер/#канал
        kind, _, val = arg.partition(" ")
        kind = {"user": "follow_user", "channel": "channel"}.get(kind.lower())
        if not kind or not val.strip():
            return tg_send("Формат:\n/follow user Имя\n/follow channel Сервер/#канал\n(/unfollow — убрать)")
        tg_send(modify_list("add" if cmd == "/follow" else "remove", kind, val.strip()))
    elif cmd in ("/notify_user", "/notify_keyword"):
        tg_send(modify_list("add", cmd[1:], arg) if arg else f"Формат: {cmd} значение")
    elif cmd.startswith(("/add_", "/remove_")):
        op, _, kind = cmd[1:].partition("_")
        if kind not in KEYS:
            return tg_send("Неизвестный тип. Доступно: " + ", ".join(KEYS))
        tg_send(modify_list(op, kind, arg) if arg else f"Формат: {cmd} значение")
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
    tg_token()  # fail fast, если токена нет
    if Bridge.start():
        log("мост запущен")
    else:
        log("нет токена Discord — жду /usertoken или /bottoken в TG")
    threading.Thread(target=Bridge.watchdog, daemon=True).start()
    try:
        tg_poll()
    except KeyboardInterrupt:
        Bridge.stop()
