"""Telegram I/O only. Every decision is made in core.py."""
import logging
import sys

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import core
from config import MODE_LABELS, TELEGRAM_BOT_TOKEN

# Windows consoles default to cp1252 and Hong Kong place names are not ASCII.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("detour.bot")

# Reminder phrasing: "about 3 min walk" / "about 3 min by MTR".
RIDE_LABELS = {"walk": "walk", "mtr": "by MTR", "taxi": "by taxi"}

MODE_KEYBOARD = InlineKeyboardMarkup([[
    InlineKeyboardButton("Walking", callback_data="mode:walk"),
    InlineKeyboardButton("MTR", callback_data="mode:mtr"),
    InlineKeyboardButton("Taxi", callback_data="mode:taxi"),
]])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    core.ensure_user(chat_id)
    await update.effective_message.reply_text(
        "I'm Detour. Tell me an errand once and I'll stay quiet until you're "
        "actually near the place with time to spare.\n\nHow do you usually get around?",
        reply_markup=MODE_KEYBOARD,
    )


async def on_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    mode = query.data.split(":", 1)[1]
    core.set_travel_mode(query.message.chat_id, mode)
    await query.edit_message_text(
        f"Travel mode set to {MODE_LABELS.get(mode, mode)}.\n\n"
        "Now connect a calendar so I know when you're free:\n"
        "/calendar <your secret ICS url>\n\n"
        "No calendar? Skip it — I'll assume you're free."
    )


async def calendar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not context.args:
        await message.reply_text("Send it like this: /calendar https://.../basic.ics")
        return
    await context.bot.send_chat_action(message.chat_id, ChatAction.TYPING)
    next_event = core.set_calendar(message.chat_id, context.args[0])
    if not next_event:
        await message.reply_text(
            "Saved, but I couldn't read anything from that calendar. "
            "Check the URL is the secret ICS address, or carry on without it."
        )
        return
    await message.reply_text(f"Calendar connected. Your next event is \"{next_event}\".")


async def tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    open_tasks = core.open_tasks(message.chat_id)
    if not open_tasks:
        await message.reply_text("No open errands. Tell me one in plain English.")
        return
    lines = [
        f"- {t['title']} at {t['place_name']}" + (" (reminded)" if t["fired_at"] else "")
        for t in open_tasks
    ]
    await message.reply_text("Open errands:\n" + "\n".join(lines))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    await context.bot.send_chat_action(message.chat_id, ChatAction.TYPING)
    result = core.handle_message(message.chat_id, message.text)

    if isinstance(result, core.Err):
        core.remember_reply(message.chat_id, result.message)
        await message.reply_text(result.message)
        return

    if isinstance(result, core.Completed):
        reply = f"Closed: {result.task['title']} at {result.task['place_name']}."
        core.remember_reply(message.chat_id, reply)
        await message.reply_text(reply)
        return

    if isinstance(result, core.Choices):
        core.remember_reply(
            message.chat_id,
            f"offered: {', '.join(o['place_name'] for o in result.options)}",
        )
        await message.reply_text(
            f"{result.title} — which one?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"{o['place_name']} · {_pretty_distance(o['distance_m'])}",
                    callback_data=f"pick:{o['id']}",
                )]
                for o in result.options
            ]),
        )
        return

    await _confirm_task(message, result)


def _pretty_distance(metres: int) -> str:
    return f"{metres}m" if metres < 1000 else f"{metres / 1000:.1f}km"


async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    result = core.choose_candidate(query.message.chat_id, int(query.data.split(":", 1)[1]))
    if isinstance(result, core.Err):
        await query.edit_message_text(result.message)
        return
    await query.edit_message_text(f"{result['title']} at {result['place_name']}.")
    await _confirm_task(query.message, result)


async def _confirm_task(message, result: dict) -> None:
    card = [f"Got it: {result['title']}", f"At {result['place_name']}"]
    if result["place_address"]:
        card.append(result["place_address"])
    if result["hours"]:
        card.append(f"Closes at {result['hours']}.")
    card.append("\nShare Live Location and I'll remind you when you're close "
                "with enough time to spare.")

    core.remember_reply(
        message.chat_id, f"confirmed: {result['title']} at {result['place_name']}"
    )
    await message.reply_text(
        "\n".join(card),
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton("Share Live Location", request_location=True)]],
            resize_keyboard=True,
            one_time_keyboard=True,
        ),
    )


async def on_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message  # live location arrives as an edited message
    location = message.location
    if not location:
        return
    await _deliver(context, message.chat_id, location.latitude, location.longitude)


async def sim(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inject a location through the same path. Demo insurance for venue GPS."""
    message = update.effective_message
    try:
        lat, lng = float(context.args[0]), float(context.args[1])
    except (IndexError, ValueError):
        await message.reply_text("Usage: /sim 22.2830 114.1580")
        return
    fired = await _deliver(context, message.chat_id, lat, lng)
    if not fired:
        await message.reply_text("Nothing fired for those coordinates.")


async def _deliver(context: ContextTypes.DEFAULT_TYPE, chat_id: int, lat: float,
                   lng: float) -> int:
    fires = core.on_location(chat_id, lat, lng)
    for fire in fires:
        await context.bot.send_message(
            chat_id, _reminder_text(fire), reply_markup=_reminder_keyboard(fire["task"])
        )
    return len(fires)


def _reminder_text(fire: dict) -> str:
    task = fire["task"]
    ride = RIDE_LABELS.get(fire["mode"], fire["mode"])
    lines = [
        f"You're {fire['distance_m']}m from {task['place_name']} "
        f"— about {fire['eta_min']} min {ride}.",
    ]
    if fire["event_title"]:
        lines.append(
            f"You have {fire['free_min']} minutes before \"{fire['event_title']}\"."
        )
    else:
        lines.append("Nothing on your calendar to get in the way.")
    if task["hours"]:
        lines.append(f"Closes at {task['hours']}.")
    lines.append(f"\n{task['title']}?")
    return "\n".join(lines)


def _reminder_keyboard(task: dict) -> InlineKeyboardMarkup:
    directions = (
        "https://www.google.com/maps/dir/?api=1&destination="
        f"{task['lat']},{task['lng']}"
    )
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Done", callback_data=f"done:{task['id']}"),
        InlineKeyboardButton("Directions", url=directions),
    ]])


async def on_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Closed.")
    task_id = int(query.data.split(":", 1)[1])
    core.complete_task(task_id)
    try:
        await query.edit_message_text(f"{query.message.text}\n\n✅ Done.")
    except Exception:
        pass  # already edited — a second tap is harmless


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("handler error", exc_info=context.error)
    chat = getattr(update, "effective_chat", None)
    if chat:
        await context.bot.send_message(chat.id, "Something went wrong on my side. Try again.")


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN missing — copy .env.example to .env first.")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("calendar", calendar))
    app.add_handler(CommandHandler("tasks", tasks))
    app.add_handler(CommandHandler("sim", sim))
    app.add_handler(CallbackQueryHandler(on_mode, pattern=r"^mode:"))
    app.add_handler(CallbackQueryHandler(on_done, pattern=r"^done:"))
    app.add_handler(CallbackQueryHandler(on_pick, pattern=r"^pick:"))

    loc_filter = filters.LOCATION & (
        filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE
    )
    app.add_handler(MessageHandler(loc_filter, on_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    log.info("Detour polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
