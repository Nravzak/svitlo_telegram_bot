import logging
from typing import Optional, List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from config import BOT_TOKEN, ADMIN_ID
from database import db
from scraper import get_schedule, Interval


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("bot")


VALID_GROUPS = [
    "1.1", "1.2",
    "2.1", "2.2",
    "3.1", "3.2",
    "4.1", "4.2",
    "5.1", "5.2",
    "6.1", "6.2",
]


def _fmt_minutes(total_min: Optional[int]) -> str:
    if total_min is None:
        return "—"
    h = total_min // 60
    m = total_min % 60
    if h and m:
        return f"{h} год {m} хв"
    if h:
        return f"{h} год"
    return f"{m} хв"


def _fmt_intervals(intervals: List[Interval]) -> str:
    if not intervals:
        return "✅ Відключень не заплановано"
    lines = []
    for it in intervals:
        start = it.start.strftime("%H:%M")
        end = it.end.strftime("%H:%M")
        lines.append(f"🔌 {start}–{end}")
    return "\n".join(lines)


def build_keyboard(group_name: str, notify_enabled: bool) -> InlineKeyboardMarkup:
    notify_label = "🔔 Сповіщення: ВКЛ" if notify_enabled else "🔕 Сповіщення: ВИКЛ"

    kb = []
    kb.append([InlineKeyboardButton("🔄 Оновити", callback_data="refresh")])
    kb.append([InlineKeyboardButton(notify_label, callback_data="toggle_notify")])
    kb.append([InlineKeyboardButton("🧩 Обрати групу", callback_data="open_groups")])
    return InlineKeyboardMarkup(kb)


def build_groups_keyboard(current_group: str) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for g in VALID_GROUPS:
        text = f"✅ {g}" if g == current_group else g
        row.append(InlineKeyboardButton(text, callback_data=f"group:{g}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


def format_card(data: dict, notify_enabled: bool) -> str:
    status_line = "🟢 Зараз світло є" if data["now_has_power"] else "🔴 Зараз світла нема"
    next_min = data.get("next_outage_in_minutes")

    next_line = ""
    if isinstance(next_min, int):
        next_line = f"⏰ Наступне відключення через: {_fmt_minutes(next_min)}"

    total_line = f"⏱ Всього без світла сьогодні: {_fmt_minutes(data.get('total_off_today_minutes'))}"
    notify_line = "🔔 Сповіщення: ВКЛ" if notify_enabled else "🔕 Сповіщення: ВИКЛ"

    text = (
        f"⚡ Графік відключень\n"
        f"📍 Регіон: {data.get('region_name_ua') or data['region']}\n"
        f"🧩 Група: {data['group']}\n"
        f"{status_line}\n"
        f"{notify_line}\n\n"
        f"📅 Сьогодні ({data['date_today']}):\n"
        f"{_fmt_intervals(data['today_off'])}\n\n"
        f"{total_line}\n\n"
        f"📅 Завтра ({data['date_tomorrow']}):\n"
        f"{_fmt_intervals(data['tomorrow_off'])}\n\n"
        f"{next_line}"
    ).strip()

    return text


async def show_group_picker(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Show group picker and store message_id so we can edit it later into schedule card.
    """
    chat = await db.get_or_create_chat(chat_id)
    current_group = chat["group_name"]

    msg = await context.bot.send_message(
        chat_id=chat_id,
        text="🧩 Обери групу відключень:",
        reply_markup=build_groups_keyboard(current_group),
    )
    await db.set_last_message_id(chat_id, msg.message_id)


async def render_or_edit_main_message(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Render schedule card and edit the last_message_id if present.
    Otherwise send a new message and store last_message_id.
    """
    chat = await db.get_or_create_chat(chat_id)

    # Если группа еще не выбрана — не показываем график, а просим выбрать.
    if int(chat.get("group_selected", 0)) == 0:
        await show_group_picker(chat_id, context)
        return

    group_name = chat["group_name"]
    notify_enabled = bool(int(chat["notify_enabled"]))

    data = await get_schedule(group_name)
    text = format_card(data, notify_enabled)
    keyboard = build_keyboard(group_name, notify_enabled)

    last_message_id = chat.get("last_message_id")

    if last_message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=int(last_message_id),
                text=text,
                reply_markup=keyboard,
            )
            await db.update_schedule_hash(chat_id, data["schedule_hash"])
            return
        except Exception as e:
            log.warning("edit_message_text failed for chat_id=%s: %s", chat_id, e)

    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=keyboard,
    )
    await db.set_last_message_id(chat_id, msg.message_id)
    await db.update_schedule_hash(chat_id, data["schedule_hash"])


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    chat = await db.get_or_create_chat(chat_id)

    # Первый старт: показываем выбор группы
    if int(chat.get("group_selected", 0)) == 0:
        await show_group_picker(chat_id, context)
        return

    await render_or_edit_main_message(chat_id, context)


async def schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await db.get_or_create_chat(chat_id)
    await render_or_edit_main_message(chat_id, context)


async def group_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /group 3.2  -> set group
    /group      -> show group picker
    """
    chat_id = update.effective_chat.id
    chat = await db.get_or_create_chat(chat_id)

    if context.args:
        g = context.args[0].strip()
        if g not in VALID_GROUPS:
            await update.message.reply_text(
                "❌ Невірна група.\n"
                "Доступні: " + ", ".join(VALID_GROUPS)
            )
            return
        await db.set_group(chat_id, g)
        await render_or_edit_main_message(chat_id, context)
        return

    current_group = chat["group_name"]
    await update.message.reply_text(
        "Обери групу:",
        reply_markup=build_groups_keyboard(current_group),
    )


async def notify_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await db.get_or_create_chat(chat_id)
    enabled = await db.toggle_notify(chat_id)
    await update.message.reply_text("🔔 Увімкнено" if enabled else "🔕 Вимкнено")
    await render_or_edit_main_message(chat_id, context)


async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id

    if ADMIN_ID <= 0 or chat_id != ADMIN_ID:
        await update.message.reply_text("⛔️ Немає доступу.")
        return

    chats = await db.list_chats()
    notify_chats = [c for c in chats if int(c["notify_enabled"]) == 1]

    counts = {}
    for c in chats:
        g = c["group_name"]
        counts[g] = counts.get(g, 0) + 1

    top = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:5]
    top_str = "\n".join([f"• {g}: {n}" for g, n in top]) if top else "—"

    await update.message.reply_text(
        "📊 Admin info\n"
        f"Всього чатів: {len(chats)}\n"
        f"Notify ON: {len(notify_chats)}\n"
        f"Топ груп:\n{top_str}"
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    chat = await db.get_or_create_chat(chat_id)

    data = query.data or ""

    if data == "refresh":
        await render_or_edit_main_message(chat_id, context)
        return

    if data == "toggle_notify":
        await db.toggle_notify(chat_id)
        await render_or_edit_main_message(chat_id, context)
        return

    if data == "open_groups":
        current_group = chat["group_name"]
        try:
            # Меняем только клавиатуру текущего сообщения
            await query.message.edit_reply_markup(
                reply_markup=build_groups_keyboard(current_group)
            )
        except Exception:
            await context.bot.send_message(
                chat_id=chat_id,
                text="Обери групу:",
                reply_markup=build_groups_keyboard(current_group),
            )
        return

    if data == "back_main":
        # если группа не выбрана — показываем выбор; иначе карточку
        await render_or_edit_main_message(chat_id, context)
        return

    if data.startswith("group:"):
        g = data.split(":", 1)[1].strip()
        if g not in VALID_GROUPS:
            await query.message.reply_text("❌ Невірна група.")
            return

        # сохраняем выбор + отмечаем group_selected=1 в БД (в database.py)
        await db.set_group(chat_id, g)

        # Важно: зафиксируем message_id (если пользователь нажимал в другом сообщении)
        try:
            await db.set_last_message_id(chat_id, query.message.message_id)
        except Exception:
            pass

        # Теперь перерисуем карточку (она заменит это же сообщение)
        await render_or_edit_main_message(chat_id, context)
        return


async def on_startup(app: Application) -> None:
    await db.init()
    log.info("DB initialized")


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty. Put BOT_TOKEN into .env")

    app = Application.builder().token(BOT_TOKEN).post_init(on_startup).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("schedul", schedule_cmd))   # как ты просил
    app.add_handler(CommandHandler("schedule", schedule_cmd))  # чтобы по-человечески
    app.add_handler(CommandHandler("group", group_cmd))
    app.add_handler(CommandHandler("notify", notify_cmd))
    app.add_handler(CommandHandler("info", info_cmd))

    app.add_handler(CallbackQueryHandler(on_callback))

    log.info("Bot started")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
