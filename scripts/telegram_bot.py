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

Под результатами — кнопки выбора продукта для сужения поиска.
"""
import os
import sys

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from search import PRODUCTS, clean_snippet, search

TOP = 5


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


def no_results_message(query, product=None):
    esc = _esc
    lines = [f"Запрос: <b>{esc(query)}</b>"]
    if product:
        lines.append(f"Продукт: <i>{esc(PRODUCTS[product])}</i>")
    lines.append("По введенным данным нет результатов.")
    lines.append("Попробуйте изменить формулировку запроса или укажите продукт.")
    return "\n".join(lines)


async def run_search(update, context, query, product=None):
    terms = [t for t in query.split() if t]
    if not terms:
        await update.effective_message.reply_text("Пустой запрос.")
        return

    rows, elapsed = search(terms, product, TOP)
    context.chat_data["last_query"] = query
    if not rows:
        await update.effective_message.reply_text(
            no_results_message(query, product), parse_mode="HTML"
        )
        return
    text = format_results(rows, elapsed, query, product)
    await update.effective_message.reply_text(
        text, reply_markup=keyboard_for(), parse_mode="HTML"
    )


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


async def on_product_callback(update, context):
    q = update.callback_query
    await q.answer()
    key = q.data.split(":", 1)[1]
    query = context.chat_data.get("last_query")
    if not query:
        await q.edit_message_text("Запрос не найден — отправьте новый.")
        return
    product = None if key == "all" else key
    terms = [t for t in query.split() if t]
    rows, elapsed = search(terms, product, TOP)
    if not rows:
        await q.edit_message_text(
            no_results_message(query, product), parse_mode="HTML",
            reply_markup=keyboard_for(),
        )
        return
    text = format_results(rows, elapsed, query, product)
    await q.edit_message_text(text, reply_markup=keyboard_for(), parse_mode="HTML")


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("Ошибка: задайте TELEGRAM_BOT_TOKEN (токен от @BotFather)")
    builder = (
        ApplicationBuilder()
        .token(token)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(10)
    )
    api_base = os.environ.get("TELEGRAM_API_BASE")
    if api_base:
        builder = builder.base_url(api_base)
        print(f"Бот: API через {api_base}", flush=True)
    app = builder.build()
    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("help", on_help))
    app.add_handler(CommandHandler("search", on_search_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_product_callback))
    print("Бот запущен (polling). Остановка: Ctrl+C", flush=True)
    app.run_polling()


if __name__ == "__main__":
    main()
