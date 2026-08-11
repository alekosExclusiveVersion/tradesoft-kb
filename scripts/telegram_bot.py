#!/usr/bin/env python3
"""Telegram-бот для поиска по базе знаний Tradesoft.

Запуск:
  export TELEGRAM_BOT_TOKEN="токен от @BotFather"
  python3 scripts/telegram_bot.py

Команды:
  /start            — приветствие
  /help             — справка
  /search <запрос>  — поиск
  <любой текст>     — поиск без команды

Под результатами — кнопки 1–5 открывают полный текст страницы,
кнопки выбора продукта сужают поиск.
"""
import asyncio
import os
import sys

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from telegram.request import HTTPXRequest

from search import PRODUCTS, clean_content, clean_snippet, load_chunks, search

TOP = 5
MAX_DETAIL_CHARS = 12000
MSG_LIMIT = 3900
RETRYABLE = (NetworkError, RetryAfter, TimedOut)


async def _send(func, *args, retries=4, **kwargs):
    """Вызов метода отправки с повтором при сетевых сбоях (VPN-моргания и т.п.)."""
    delay = 2
    last = None
    for attempt in range(retries):
        try:
            return await func(*args, **kwargs)
        except RETRYABLE as e:
            last = e
            if isinstance(e, RetryAfter):
                await asyncio.sleep(getattr(e, "retry_after", 1))
            else:
                await asyncio.sleep(delay)
                delay *= 2
    raise last


def _esc(text):
    import html
    return html.escape(str(text), quote=False)


def format_results(rows, elapsed, query, product=None):
    esc = _esc
    lines = [f"Запрос: <b>{esc(query)}</b>"]
    if product:
        lines.append(f"Продукт: <i>{esc(PRODUCTS[product])}</i>")
    lines.append(f"Найдено: <b>{len(rows)}</b> · {elapsed:.1f} мс")
    for i, (product, page, title, path, snippet, _score) in enumerate(rows, 1):
        name = title or page
        lines.append("")
        lines.append(f"{i}. <b>{esc(name)}</b> — <i>{esc(PRODUCTS.get(product, product))}</i>")
        if snippet:
            lines.append(clean_snippet(snippet, html=True))
    text = "\n".join(lines)
    return text[:4000]


def keyboard_for():
    buttons = [[InlineKeyboardButton(PRODUCTS[p], callback_data=f"p:{p}")]
               for p in PRODUCTS]
    buttons.append([InlineKeyboardButton("Все продукты",
                                         callback_data="p:all")])
    return InlineKeyboardMarkup(buttons)


def results_keyboard(n):
    """Кнопки 1..N (полный текст результата) + выбор продукта."""
    buttons = [[InlineKeyboardButton(str(i), callback_data=f"d:{i}")
                for i in range(1, n + 1)]]
    buttons += [[InlineKeyboardButton(PRODUCTS[p], callback_data=f"p:{p}")]
                for p in PRODUCTS]
    buttons.append([InlineKeyboardButton("Все продукты",
                                         callback_data="p:all")])
    return InlineKeyboardMarkup(buttons)


def detail_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("◀ Пред", callback_data="nav:prev"),
            InlineKeyboardButton("Список", callback_data="nav:list"),
            InlineKeyboardButton("След ▶", callback_data="nav:next"),
        ]
    ])


def no_results_message(query, product=None):
    esc = _esc
    lines = [f"Запрос: <b>{esc(query)}</b>"]
    if product:
        lines.append(f"Продукт: <i>{esc(PRODUCTS[product])}</i>")
    lines.append("По введенным данным нет результатов.")
    lines.append("Попробуйте изменить формулировку запроса или укажите продукт.")
    return "\n".join(lines)


async def render_results(context, query, product, target, edit=False):
    """Показывает список результатов (reply_text или edit_message_text)."""
    terms = [t for t in query.split() if t]
    if not terms:
        return None
    rows, elapsed = search(terms, product, TOP)
    context.chat_data["last_query"] = query
    context.chat_data["last_product"] = product
    if not rows:
        text = no_results_message(query, product)
        markup = keyboard_for()
    else:
        context.chat_data["last_results"] = [
            (r[0], r[1], r[2] or r[1]) for r in rows
        ]
        text = format_results(rows, elapsed, query, product)
        markup = results_keyboard(len(rows))
    if edit:
        await _send(target.edit_message_text, text, reply_markup=markup,
                    parse_mode="HTML")
    else:
        await _send(target.reply_text, text, reply_markup=markup,
                    parse_mode="HTML")
    return rows


async def send_detail(message, context, idx):
    """Полный текст страницы результата idx (0-based), несколькими сообщениями."""
    results = context.chat_data.get("last_results") or []
    if not results or not (0 <= idx < len(results)):
        await _send(message.reply_text, "Результаты не найдены — выполните поиск заново.")
        return
    product, page, title = results[idx]
    context.chat_data["last_detail_idx"] = idx
    raw = load_chunks(product, page, MAX_DETAIL_CHARS)
    body = clean_content(raw)
    if len(raw) > MAX_DETAIL_CHARS:
        body = body[:MAX_DETAIL_CHARS].rstrip() + "\n\n…(текст обрезан)"
    header = f"📄 {title} — {PRODUCTS.get(product, product)} ({idx + 1}/{len(results)})\n\n"
    if not body:
        await _send(message.reply_text, header + "Пустая страница.",
                    reply_markup=detail_keyboard())
        return
    parts = [(header + body)[i:i + MSG_LIMIT]
             for i in range(0, len(header + body), MSG_LIMIT)]
    await _send(message.reply_text, parts[0], reply_markup=detail_keyboard())
    for part in parts[1:]:
        await _send(message.reply_text, part)


async def run_search(update, context, query, product=None):
    terms = [t for t in query.split() if t]
    if not terms:
        await _send(update.effective_message.reply_text, "Пустой запрос.")
        return
    await render_results(context, query, product, update.effective_message)


async def on_start(update, context):
    await update.message.reply_text(
        "Привет! Я ищу по базе знаний Tradesoft.\n"
        "Просто напишите запрос — например, «НДС эквайринг».\n"
        "Или используйте /search <запрос>. Полная справка: /help"
    )


async def on_help(update, context):
    await update.message.reply_text(
        "Как пользоваться:\n"
        "/search <запрос> — поиск по базе знаний\n"
        "любой текст — то же самое, без команды\n\n"
        "Примеры: «передача аналогов», «ставка НДС онлайн касса»,\n"
        "«API метод JSON», «nastrojka onlajn kassy»\n\n"
        "Кнопки 1–5 под результатами открывают полный текст страницы.\n"
        "Кнопки под результатами сужают поиск до конкретного продукта."
    )


async def on_search_command(update, context):
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("Использование: /search <запрос>")
        return
    await run_search(update, context, query)


async def on_text(update, context):
    await run_search(update, context, update.message.text)


async def on_error(update, context):
    err = context.error
    print(f"Ошибка обработки: {type(err).__name__}: {err}", file=sys.stderr, flush=True)
    try:
        if update and update.effective_message:
            await _send(
                update.effective_message.reply_text,
                "Извините, произошла ошибка. Попробуйте ещё раз.",
            )
    except RETRYABLE:
        pass


async def on_product_callback(update, context):
    q = update.callback_query
    await q.answer()
    key = q.data.split(":", 1)[1]
    query = context.chat_data.get("last_query")
    if not query:
        await _send(q.edit_message_text, "Запрос не найден — отправьте новый.")
        return
    product = None if key == "all" else key
    await render_results(context, query, product, q, edit=True)


async def on_detail_callback(update, context):
    q = update.callback_query
    await q.answer()
    try:
        idx = int(q.data.split(":", 1)[1]) - 1
    except (ValueError, IndexError):
        await _send(q.message.reply_text, "Неверный номер результата.")
        return
    await send_detail(q.message, context, idx)


async def on_nav_callback(update, context):
    q = update.callback_query
    act = q.data.split(":", 1)[1]
    if act == "list":
        await q.answer()
        query = context.chat_data.get("last_query")
        if not query:
            await _send(q.edit_message_text, "Запрос не найден — выполните поиск заново.")
            return
        product = context.chat_data.get("last_product")
        await render_results(context, query, product, q, edit=True)
        return
    idx = context.chat_data.get("last_detail_idx", 0)
    results = context.chat_data.get("last_results") or []
    if not results:
        await q.answer()
        await _send(q.message.reply_text, "Результаты не найдены — выполните поиск заново.")
        return
    nidx = idx - 1 if act == "prev" else idx + 1
    if not (0 <= nidx < len(results)):
        await q.answer(
            "Нет " + ("предыдущего" if act == "prev" else "следующего")
            + " результата", show_alert=True
        )
        return
    await q.answer()
    await send_detail(q.message, context, nidx)


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("Ошибка: задайте TELEGRAM_BOT_TOKEN (токен от @BotFather)")
    builder = ApplicationBuilder().token(token)
    api_base = os.environ.get("TELEGRAM_API_BASE")
    if api_base:
        builder = (
            builder
            .connect_timeout(30)
            .read_timeout(30)
            .write_timeout(30)
            .pool_timeout(10)
            .base_url(api_base)
        )
        print(f"Бот: API через {api_base}", flush=True)
    else:
        # Провайдер/VPN фильтрует по SNI api.telegram.org (TCP/TLS режется).
        # Подключаемся напрямую к IP, отправляя Host: api.telegram.org.
        # Проверка TLS-сертификата при этом отключается (SNI-фильтр и так её ломает).

        def _sni_req():
            return HTTPXRequest(
                connect_timeout=30, read_timeout=30, write_timeout=30,
                pool_timeout=10,
                httpx_kwargs={
                    "verify": False,
                    "headers": {"Host": "api.telegram.org"},
                },
            )

        builder = (
            builder
            .base_url("https://149.154.167.220/bot")
            .request(_sni_req())
            .get_updates_request(_sni_req())
        )
        print("Бот: API через 149.154.167.220 (Host: api.telegram.org, SNI-обход)",
              flush=True)
    app = builder.build()
    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("help", on_help))
    app.add_handler(CommandHandler("search", on_search_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_product_callback, pattern=r"^p:"))
    app.add_handler(CallbackQueryHandler(on_detail_callback, pattern=r"^d:\d+$"))
    app.add_handler(CallbackQueryHandler(on_nav_callback, pattern=r"^nav:"))
    app.add_error_handler(on_error)
    print("Бот запущен (polling). Остановка: Ctrl+C", flush=True)
    app.run_polling()


if __name__ == "__main__":
    main()
