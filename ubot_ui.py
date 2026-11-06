"""Copy Chats panel (owner only): account login + chat -> channel copy, sab inline buttons se."""
import asyncio
import html
import logging
import re
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db
import userbot
from autoposter import panel as ap_panel
from config import OWNER_ID

log = logging.getLogger("ubot_ui")
HTML = ParseMode.HTML
KEY = "ub"            # context.user_data[KEY] = flow state
PAGE = 8
_jobs: dict[int, dict] = {}     # chat_id -> {"stop": bool}   (ek time par ek hi copy job)
_tasks: set = set()
_ICON = {"saved": "💾", "channel": "📣", "group": "👥", "user": "👤", "bot": "🤖"}
_owner_private = filters.User(user_id=OWNER_ID) & filters.ChatType.PRIVATE


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def bg(coro):
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def clear(context: ContextTypes.DEFAULT_TYPE):
    """Koi bhi dusra menu button = Copy Chats setup/login cancel (chalta hua job nahi rukta)."""
    u = context.user_data.pop(KEY, None)
    if u and u.get("step") in ("phone", "code", "pwd") and userbot.has_pending():
        bg(userbot.login_cancel())


async def _edit(bot, panel, text: str, kb=None):
    try:
        await bot.edit_message_text(text, chat_id=panel[0], message_id=panel[1], parse_mode=HTML, reply_markup=kb)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("panel edit failed: %s", e)
    except TelegramError as e:
        log.warning("panel edit failed: %s", e)


async def _del(msg):
    try:
        await msg.delete()
    except TelegramError:
        pass


def _menu_kb():
    return InlineKeyboardMarkup([[btn("🏠 Menu", "m_home")]])


# ===================== screens =====================
async def _home_screen():
    if not userbot.ENABLED:
        return (
            "👤📥 <b>Copy Chats</b>\n\n"
            "⚠️ Ye feature band hai. Pehle env me <code>TG_API_ID</code> aur <code>TG_API_HASH</code> daalo "
            "(my.telegram.org → API development tools) aur redeploy karo.",
            _menu_kb(),
        )
    accs = await db.list_userbots()
    rows = [[btn(f"👤 {a['name'][:24]}", f"ub:a:{a['uid']}"), btn("🗑", f"ub:del:{a['uid']}")] for a in accs]
    rows.append([btn("➕ Add Account", "ub:add")])
    rows.append([btn("🏠 Menu", "m_home")])
    text = (
        "👤📥 <b>Copy Chats</b>\n\n"
        "Apne Telegram account se kisi bhi chat ke <b>photo / video / file (caption ke saath)</b> copy karo — "
        "<b>forward tag ke bina</b>, download/upload ke bina ⚡\n"
        "🎯 Destination: <b>channel</b>, <b>💾 Saved Messages</b> ya koi bhi <b>👤 personal chat</b>\n\n"
        + (f"Accounts: <b>{len(accs)}</b> — jisse copy karna hai usko chuno 👇" if accs else "Abhi koi account nahi. ➕ Add Account dabao.")
    )
    return text, InlineKeyboardMarkup(rows)


def _setup_text(u: dict, resume_from: int) -> str:
    def nm(x):
        return f"{_ICON.get(x['kind'], '💬')} <b>{html.escape(x['title'][:40])}</b>" if x else "<i>chuna nahi</i>"

    k = u["kinds"]
    kinds = " ".join(n for f, n in (("photo", "📷"), ("video", "🎬"), ("file", "📁")) if f in k) or "—"
    lines = [
        f"👤 <b>{html.escape(u['acc_name'])}</b>\n",
        f"📂 Source: {nm(u['src'])}",
        f"🎯 Destination: {nm(u['dest'])}\n",
        f"🎯 Copy: {kinds}",
        f"📝 Sirf caption wale: <b>{'ON' if u['cap'] else 'OFF'}</b>",
    ]
    if resume_from:
        lines.append(f"🔁 Resume (msg #{resume_from} ke baad se): <b>{'ON' if u['resume'] else 'OFF'}</b>")
    lines.append("\nℹ️ Source me 'Restrict saving content' on ho to Telegram copy nahi karne deta (bypass nahi hota).")
    return "\n".join(lines)


def _setup_kb(u: dict, resume_from: int) -> InlineKeyboardMarkup:
    k = u["kinds"]
    t = lambda f, n: btn(("✅ " if f in k else "▫️ ") + n, f"ub:k:{f}")  # noqa: E731
    rows = [
        [btn("📂 Source chuno", "ub:pick:src"), btn("🎯 Destination chuno", "ub:pick:dest")],
        [t("photo", "Photos"), t("video", "Videos"), t("file", "Files")],
        [btn(("✅ " if u["cap"] else "▫️ ") + "Sirf caption wale", "ub:cap")],
    ]
    if resume_from:
        rows.append([btn(("✅ " if u["resume"] else "▫️ ") + "Resume", "ub:res")])
    ready = u["src"] and u["dest"]
    rows.append([btn("▶️ Start Copy", "ub:go")] if ready else [btn("⬆️ Pehle source + destination chuno", "ub:noop")])
    rows.append([btn("⬅️ Accounts", "ub:home")])
    return InlineKeyboardMarkup(rows)


async def _show_setup(bot, u: dict):
    resume_from = 0
    if u["src"] and u["dest"]:
        resume_from = await db.get_ub_progress(u["acc"], u["src"]["id"], u["dest"]["id"])
    u["resume_from"] = resume_from
    await _edit(bot, u["panel"], _setup_text(u, resume_from), _setup_kb(u, resume_from))


def _view(u: dict) -> list:
    q = (u.get("query") or "").lower()
    # Destination: Saved Messages, personal chats, aur wo channels jaha account post kar sakta hai
    base = [d for d in u["all"] if d.get("dest", d["post"])] if u["mode"] == "dest" else u["all"]
    return [d for d in base if q in d["title"].lower()] if q else base


async def _show_pick(bot, u: dict):
    view = u["view"] = _view(u)
    pages = max(1, -(-len(view) // PAGE))
    u["page"] = min(u.get("page", 0), pages - 1)
    chunk = view[u["page"] * PAGE:(u["page"] + 1) * PAGE]
    rows = [[btn(f"{_ICON.get(d['kind'], '💬')} {d['title'][:32]}", f"ub:p:{u['page'] * PAGE + i}")] for i, d in enumerate(chunk)]
    nav = []
    if u["page"] > 0:
        nav.append(btn("◀️", f"ub:pg:{u['page'] - 1}"))
    nav.append(btn(f"{u['page'] + 1}/{pages}", "ub:noop"))
    if u["page"] < pages - 1:
        nav.append(btn("▶️", f"ub:pg:{u['page'] + 1}"))
    rows.append(nav)
    rows.append([btn("⬅️ Back", "ub:back")])
    what = "📂 <b>Source chat chuno</b>" if u["mode"] == "src" else "🎯 <b>Destination chuno</b>\n<i>(💾 Saved Messages, 👤 personal chats, aur wo 📣 channels jaha tumhare account ko post karne ka haq hai)</i>"
    hint = "\n\n🔎 Naam likh ke search karo, ya <code>@username</code> / t.me link bhejo."
    empty = "\n\n😕 Kuch nahi mila." if not view else ""
    await _edit(bot, u["panel"], f"{what}{hint}{empty}", InlineKeyboardMarkup(rows))


async def _open_account(bot, context: ContextTypes.DEFAULT_TYPE, panel, uid: int):
    doc = await db.get_userbot(uid)
    if not doc:
        text, kb = await _home_screen()
        await _edit(bot, panel, "❌ Account nahi mila.\n\n" + text, kb)
        return
    await _edit(bot, panel, "⏳ <b>Account connect ho raha hai…</b>")
    client = await userbot.get_client(uid)
    if not client:
        await _edit(
            bot, panel,
            "❌ Is account ka session expire ho gaya (Telegram se logout ho gaya). 🗑 se hata ke dobara add karo.",
            InlineKeyboardMarkup([[btn("⬅️ Accounts", "ub:home")]]),
        )
        return
    await _edit(bot, panel, "⏳ <b>Chats load ho rahe hain…</b>")
    try:
        allc = await userbot.list_dialogs(client)
    except Exception as e:  # noqa: BLE001
        await _edit(bot, panel, f"❌ Chats load nahi hue: <code>{html.escape(str(e)[:200])}</code>", InlineKeyboardMarkup([[btn("⬅️ Accounts", "ub:home")]]))
        return
    u = {
        "step": "setup", "acc": uid, "acc_name": doc["name"], "panel": panel, "all": allc,
        "src": None, "dest": None, "kinds": {"photo", "video", "file"}, "cap": False, "resume": True,
        "mode": None, "query": "", "page": 0, "view": [],
    }
    context.user_data[KEY] = u
    await _show_setup(bot, u)


# ===================== callbacks =====================
async def ub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer("🚫 Owner only", show_alert=True)
        return
    bot = context.bot
    await ap_panel.clear_mode()  # Copy Chats khola = Auto Posting ka pending mode band (text input yahin aayega)
    panel = (q.message.chat_id, q.message.message_id)
    parts = q.data.split(":")
    act = parts[1] if len(parts) > 1 else ""
    u = context.user_data.get(KEY)

    if act == "noop":
        await q.answer()
        return

    if act == "home":
        await q.answer()
        clear(context)
        text, kb = await _home_screen()
        await _edit(bot, panel, text, kb)
        return

    if act == "add":
        if not userbot.ENABLED:
            await q.answer("TG_API_ID / TG_API_HASH set nahi hain", show_alert=True)
            return
        await q.answer()
        clear(context)
        context.user_data[KEY] = {"step": "phone", "panel": panel}
        await _edit(
            bot, panel,
            "➕👤 <b>Account add</b>\n\n"
            "Us account ka <b>phone number</b> bhejo (country code ke saath), jaise <code>+919876543210</code>\n\n"
            "🔒 Session encrypt hokar save hoti hai. Ye account ka poora access hai, isliye sirf apna account add karo.\n"
            "🧹 Tumhare bheje number/code/password ke messages main turant delete kar dunga.",
            InlineKeyboardMarkup([[btn("❌ Cancel", "ub:home")]]),
        )
        return

    if act == "a" and len(parts) == 3:
        await q.answer()
        clear(context)
        await _open_account(bot, context, panel, int(parts[2]))
        return

    if act == "del" and len(parts) == 3:
        await q.answer()
        await _edit(
            bot, panel,
            "🗑⚠️ <b>Ye account hata dein?</b>\n\nTelegram se is session ko logout kar dunga aur save kiya hua data delete ho jayega.",
            InlineKeyboardMarkup([[btn("✅ Haan, hata do", f"ub:delc:{parts[2]}"), btn("❌ Nahi", "ub:home")]]),
        )
        return

    if act == "delc" and len(parts) == 3:
        await q.answer("Hata raha hu…")
        await userbot.remove_account(int(parts[2]))
        text, kb = await _home_screen()
        await _edit(bot, panel, "✅ Account hata diya.\n\n" + text, kb)
        return

    if act == "stop":
        job = _jobs.get(panel[0])
        if job:
            job["stop"] = True
            await q.answer("⏹ Rok raha hu…")
        else:
            await q.answer()
        return

    # ---- neeche sab ke liye setup state chahiye ----
    if not u or u.get("step") not in ("setup", "pick"):
        await q.answer("Session expire ho gaya, account dobara kholo", show_alert=True)
        text, kb = await _home_screen()
        await _edit(bot, panel, text, kb)
        return
    u["panel"] = panel

    if act == "pick" and len(parts) == 3:
        await q.answer()
        u.update(step="pick", mode=parts[2], query="", page=0)
        await _show_pick(bot, u)
    elif act == "pg" and len(parts) == 3:
        await q.answer()
        u["page"] = int(parts[2])
        await _show_pick(bot, u)
    elif act == "p" and len(parts) == 3:
        view = u.get("view") or []
        i = int(parts[2])
        if i >= len(view):
            await q.answer("List purani ho gayi, dobara kholo", show_alert=True)
            return
        await q.answer()
        u["src" if u["mode"] == "src" else "dest"] = view[i]
        u.update(step="setup", mode=None, query="")
        await _show_setup(bot, u)
    elif act == "back":
        await q.answer()
        u.update(step="setup", mode=None, query="")
        await _show_setup(bot, u)
    elif act == "k" and len(parts) == 3:
        f = parts[2]
        if f in u["kinds"] and len(u["kinds"]) == 1:
            await q.answer("Kam se kam ek type chaahiye", show_alert=True)
            return
        await q.answer()
        u["kinds"] ^= {f}
        await _show_setup(bot, u)
    elif act == "cap":
        await q.answer()
        u["cap"] = not u["cap"]
        await _show_setup(bot, u)
    elif act == "res":
        await q.answer()
        u["resume"] = not u["resume"]
        await _show_setup(bot, u)
    elif act == "go":
        await _start_job(q, context, u)
    else:
        await q.answer()


async def _start_job(q, context, u: dict):
    chat_id = q.message.chat_id
    if not (u["src"] and u["dest"]):
        await q.answer("Source aur destination chuno", show_alert=True)
        return
    if u["src"]["id"] == u["dest"]["id"]:
        await q.answer("Source aur destination alag hone chahiye", show_alert=True)
        return
    if chat_id in _jobs:
        await q.answer("⏳ Ek copy job abhi chal raha hai", show_alert=True)
        return
    await q.answer("🚀 Shuru!")
    job = {"stop": False}
    _jobs[chat_id] = job
    context.user_data.pop(KEY, None)
    bg(_run(context.bot, chat_id, q.message.message_id, u, job))


def _progress_text(u: dict, r: dict) -> str:
    total = r["total"] or "?"
    return (
        "📥⚡ <b>Copy ho raha hai…</b>\n\n"
        f"📂 {html.escape(u['src']['title'][:30])} ➜ {_ICON.get(u['dest']['kind'], '💬')} {html.escape(u['dest']['title'][:30])}\n\n"
        f"🔎 Scanned: <b>{r['scanned']}</b> / {total}\n"
        f"✅ Copied: <b>{r['copied']}</b>\n"
        f"⏭ Skipped: {r['skipped']}" + (f"\n⚠️ Errors: {r['errors']}" if r["errors"] else "")
    )


async def _run(bot, chat_id: int, panel_id: int, u: dict, job: dict):
    panel = (chat_id, panel_id)
    stop_kb = InlineKeyboardMarkup([[btn("⏹ Stop", "ub:stop")]])
    last = 0.0

    async def progress(r: dict, force: bool = False):
        nonlocal last
        now = time.monotonic()
        if force or now - last >= 3:
            last = now
            await _edit(bot, panel, _progress_text(u, r), stop_kb)

    try:
        client = await userbot.get_client(u["acc"])
        if not client:
            await _edit(bot, panel, "❌ Account connect nahi hua. Dobara add karo.", InlineKeyboardMarkup([[btn("⬅️ Accounts", "ub:home")]]))
            return
        min_id = u.get("resume_from", 0) if u["resume"] else 0
        await _edit(bot, panel, "🔎 <b>Chat scan shuru…</b>", stop_kb)
        r = await userbot.copy_chat(
            client, u["src"]["id"], u["dest"]["id"], set(u["kinds"]), u["cap"], min_id, job, progress, acc=u["acc"]
        )
        head = "⏹ <b>Roka gaya</b>" if r["stopped"] else ("❌ <b>Ruk gaya</b>" if r["aborted"] else "🎉 <b>Copy complete!</b>")
        text = (
            f"{head}\n\n"
            f"✅ Copied: <b>{r['copied']}</b>\n🔎 Scanned: {r['scanned']}\n⏭ Skipped: {r['skipped']}"
            + (f"\n⚠️ Errors: {r['errors']} (<code>{html.escape(r['last_error'][:100])}</code>)" if r["errors"] else "")
            + (f"\n\n{html.escape(r['aborted'])}" if r["aborted"] else "")
            + ("\n\n🔁 Dobara chalaoge to Resume ON se wahi se aage badhega." if r["copied"] else "")
        )
        kb = InlineKeyboardMarkup([[btn("👤 Copy Chats", "ub:home"), btn("🏠 Menu", "m_home")]])
        try:
            await bot.delete_message(chat_id, panel_id)
        except TelegramError:
            pass
        await bot.send_message(chat_id, text, parse_mode=HTML, reply_markup=kb)
    except Exception as e:  # noqa: BLE001
        log.exception("copy job failed")
        try:
            await bot.send_message(chat_id, f"❌ <b>Copy error</b>\n<code>{html.escape(str(e)[:300])}</code>", parse_mode=HTML, reply_markup=_menu_kb())
        except TelegramError:
            pass
    finally:
        _jobs.pop(chat_id, None)


# ===================== text input (login + search) =====================
async def ub_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = context.user_data.get(KEY)
    if not u:
        return                      # kuch active nahi: normal owner_text handler chalega
    msg = update.effective_message
    step = u.get("step")
    bot = context.bot
    text = (msg.text or "").strip()

    if step == "phone":
        await _del(msg)
        phone = re.sub(r"[^\d+]", "", text)
        if not phone.startswith("+") or len(phone) < 8:
            await _edit(bot, u["panel"], "❌ Number <code>+91…</code> jaise country code ke saath bhejo.", InlineKeyboardMarkup([[btn("❌ Cancel", "ub:home")]]))
            raise ApplicationHandlerStop
        await _edit(bot, u["panel"], "⏳ <b>Code bhej raha hu…</b>")
        try:
            await userbot.login_send_code(phone)
        except userbot.UbError as e:
            context.user_data.pop(KEY, None)
            await _edit(bot, u["panel"], f"❌ {html.escape(str(e))}", InlineKeyboardMarkup([[btn("⬅️ Accounts", "ub:home")]]))
            raise ApplicationHandlerStop
        u["step"] = "code"
        await _edit(
            bot, u["panel"],
            "📲 <b>Code bhej diya</b>\n\nTelegram app me us account par code aayega. Code ko <b>spaces ke saath</b> bhejo, jaise <code>1 2 3 4 5</code>\n\n"
            "⚠️ Bina spaces ke bhejoge to Telegram code ko block kar deta hai.",
            InlineKeyboardMarkup([[btn("❌ Cancel", "ub:home")]]),
        )
        raise ApplicationHandlerStop

    if step == "code":
        await _del(msg)
        code = re.sub(r"\D", "", text)
        if not 4 <= len(code) <= 7:
            await _edit(bot, u["panel"], "❌ Code galat lag raha hai. Sirf digits, spaces ke saath: <code>1 2 3 4 5</code>", InlineKeyboardMarkup([[btn("❌ Cancel", "ub:home")]]))
            raise ApplicationHandlerStop
        try:
            r = await userbot.login_code(code)
            if r == "2fa":
                u["step"] = "pwd"
                await _edit(bot, u["panel"], "🔐 <b>2-step password</b> bhejo (Telegram ka cloud password).\nMessage turant delete ho jayega.", InlineKeyboardMarkup([[btn("❌ Cancel", "ub:home")]]))
                raise ApplicationHandlerStop
            info = await userbot.login_finish()
        except userbot.UbError as e:
            await _edit(bot, u["panel"], f"❌ {html.escape(str(e))}", InlineKeyboardMarkup([[btn("❌ Cancel", "ub:home")]]))
            raise ApplicationHandlerStop
        context.user_data.pop(KEY, None)
        await _open_account(bot, context, u["panel"], info["uid"])
        raise ApplicationHandlerStop

    if step == "pwd":
        await _del(msg)
        try:
            await userbot.login_password(text)
            info = await userbot.login_finish()
        except userbot.UbError as e:
            await _edit(bot, u["panel"], f"❌ {html.escape(str(e))}", InlineKeyboardMarkup([[btn("❌ Cancel", "ub:home")]]))
            raise ApplicationHandlerStop
        context.user_data.pop(KEY, None)
        await _open_account(bot, context, u["panel"], info["uid"])
        raise ApplicationHandlerStop

    if step == "pick":
        await _del(msg)
        client = await userbot.get_client(u["acc"])
        if client and (text.startswith("@") or "t.me/" in text):
            d = await userbot.resolve(client, text)
            if d:
                u["all"] = [d] + [x for x in u["all"] if x["id"] != d["id"]]
                u["query"] = d["title"].lower()
            else:
                u["query"] = text.lower()
        else:
            u["query"] = text.lower()
        u["page"] = 0
        await _show_pick(bot, u)
        raise ApplicationHandlerStop
    # setup step: text ignore (normal handler chalega)


def register(app: Application):
    """handlers.register_main_handlers se pehle call hota hai."""
    # group -1: owner ka text pehle yahan aata hai; flow active na ho to aage normal handler ko mil jata hai
    app.add_handler(MessageHandler(_owner_private & filters.TEXT & ~filters.COMMAND, ub_text), group=-1)
    app.add_handler(CallbackQueryHandler(ub_callback, pattern=r"^ub:"))
