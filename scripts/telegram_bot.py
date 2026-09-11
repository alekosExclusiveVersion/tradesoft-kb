#!/usr/bin/env python3
"""Telegram-бот для поиска по базе знаний Tradesoft (unified cross_search).

Запуск:
  export TELEGRAM_BOT_TOKEN="токен от @BotFather"
  python3 scripts/telegram_bot.py

Команды:
  /start            — приветствие
  /help             — справка
  /search <запрос>  — поиск
  <любой текст>     — поиск без команды

Результаты объединяют документацию (docs), решения техподдержки (solution)
и CRM-сделки (crm). Под результатами кнопки 1–N открывают подробно.
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

from search import clean_content, clean_snippet, load_chunks

import cross_search

TOP = 5
MAX_DETAIL_CHARS = 12000
MSG_LIMIT = 3900
RETRYABLE = (NetworkError, RetryAfter, TimedOut)

TYPE_LABEL = {"docs": "📄 Документация", "solution": "💡 Решение",
              "crm": "💼 Сделка"}


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


def _split_docs_path(path):
    """'product__page.htm.md' → (product, page)."""
    if "__" in path:
        return tuple(path.split("__", 1))
    return None, path


def _flatten_results(res):
    """Строит плоский список результатов из ответов cross_search.

    Каждый элемент: dict(kind, product, page, title, snippet, url, extra).
    """
    items = []
    for ans in res.get("answers", []):
        for b in ans.get("blocks", []):
            kind = b.get("source", "")
            title = b.get("title") or ""
            snippet = clean_snippet(b.get("content") or "", html=True)
            if kind == "docs":
                product, page = _split_docs_path(b.get("path") or "")
                items.append({
                    "kind": "docs", "product": product or "", "page": page or "",
                    "title": title, "snippet": snippet,
                    "url": b.get("url") or "",
                    "source_urls": ans.get("source_urls") or [],
                })
            elif kind == "solution":
                items.append({
                    "kind": "solution",
                    "product": b.get("product") or "", "page": b.get("path") or "",
                    "title": title, "snippet": snippet,
                    "url": b.get("url") or "",
                    "solution_id": b.get("deal_id"),
                    "source_urls": ans.get("source_urls") or [],
                })
            elif kind == "crm":
                items.append({
                    "kind": "crm",
                    "product": "", "page": b.get("path") or "",
                    "title": title, "snippet": snippet,
                    "url": b.get("url") or "",
                    "deal_id": b.get("deal_id"),
                    "source_urls": ans.get("source_urls") or [],
                })
        # Похожие документы/решения из cross-links идут доп. строками
        for rd in ans.get("related_docs", [])[:1]:
            path = rd.get("path") or ""
            product, page = _split_docs_path(path)
            items.append({
                "kind": "docs", "product": product or "", "page": page or "",
                "title": rd.get("title") or "", "snippet": "",
                "url": "", "related": True, "source_urls": [],
            })
        for rs in ans.get("related_solutions", [])[:1]:
            items.append({
                "kind": "solution",
                "product": rs.get("product") or "", "page": f"solution_{rs.get('id')}",
                "title": rs.get("title") or "", "snippet": "",
                "url": "", "related": True, "solution_id": rs.get("id"),
                "source_urls": [],
            })
    return items[:TOP]


def format_results(items, res, query):
    esc = _esc
    lines = [f"Запрос: <b>{esc(query)}</b>"]
    product = res.get("product_display") or res.get("product")
    if product:
        lines.append(f"Продукт: <i>{esc(product)}</i>")
    counts = res.get("counts") or {}
    lines.append(f"Найдено: <b>{len(items)}</b> · {res.get('latency_ms', 0)} мс"
                 f" · docs {counts.get('docs', 0)} / решения {counts.get('solutions', 0)}")
    for i, it in enumerate(items, 1):
        lines.append("")
        tag = TYPE_LABEL.get(it["kind"], it["kind"])
        if it.get("related"):
            tag = "🔗 Связано"
        name = it["title"] or it["page"]
        prod = f" — <i>{esc(it['product'])}</i>" if it["product"] else ""
        lines.append(f"{i}. [{tag}]{prod}\n<b>{esc(name)}</b>")
        if it["snippet"]:
            lines.append(esc(snippet_text(it["snippet"])))
    text = "\n".join(lines)
    return text[:4000]


def snippet_text(snippet):
    """Очищает HTML-сниппет до plain-текста для вывода."""
    import re
    return re.sub(r"<[^>]+>", "", snippet)


def results_keyboard(n):
    buttons = [[InlineKeyboardButton(str(i), callback_data=f"d:{i}")
                for i in range(1, n + 1)]]
    return InlineKeyboardMarkup(buttons)


def detail_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("◀ Пред", callback_data="nav:prev"),
            InlineKeyboardButton("Список", callback_data="nav:list"),
            InlineKeyboardButton("След ▶", callback_data="nav:next"),
        ]
    ])


def no_results_message(query):
    esc = _esc
    return (f"Запрос: <b>{esc(query)}</b>\n"
            "По введенным данным нет результатов.\n"
            "Попробуйте изменить формулировку запроса.")


async def render_results(context, query, target, edit=False):
    """Единый поиск (cross_search) и показ списка результатов."""
    if not query.strip():
        return None
    res = cross_search.cross_search(query, max_answers=3, limit_per_source=5)
    items = _flatten_results(res)
    context.chat_data["last_query"] = query
    context.chat_data["last_res"] = res
    context.chat_data["last_items"] = items
    if not items:
        text = no_results_message(query)
        markup = None
    else:
        text = format_results(items, res, query)
        markup = results_keyboard(len(items))
    if edit:
        await _send(target.edit_message_text, text, reply_markup=markup,
                    parse_mode="HTML")
    else:
        await _send(target.reply_text, text, reply_markup=markup,
                    parse_mode="HTML")
    return items


async def _doc_detail_body(item):
    raw = load_chunks(item["product"], item["page"], MAX_DETAIL_CHARS)
    body = clean_content(raw)
    if len(raw) > MAX_DETAIL_CHARS:
        body = body[:MAX_DETAIL_CHARS].rstrip() + "\n\n…(текст обрезан)"
    return body


def _solution_detail_body(item):
    sol = cross_search._get_solutions_source().get_solution(item.get("solution_id"))
    if not sol:
        return item.get("snippet") or "Решение не найдено."
    parts = []
    if sol["question"]:
        parts.append("Вопрос:\n" + sol["question"])
    if sol["resolution"]:
        parts.append("Решение:\n" + sol["resolution"])
    return "\n\n".join(parts) or "Решение не найдено."


async def send_detail(message, context, idx):
    """Подробно по результату idx (0-based): полный текст или снаппет."""
    items = context.chat_data.get("last_items") or []
    if not items or not (0 <= idx < len(items)):
        await _send(message.reply_text, "Результаты не найдены — выполните поиск заново.")
        return
    it = items[idx]
    context.chat_data["last_detail_idx"] = idx
    tag = TYPE_LABEL.get(it["kind"], it["kind"])
    prod = f" ({it['product']})" if it["product"] else ""
    header = f"{tag}{prod}: <b>{_esc(it['title'] or it['page'])}</b> ({idx + 1}/{len(items)})\n\n"

    if it["kind"] == "docs":
        try:
            body = await _doc_detail_body(it)
        except Exception as e:
            print(f"[detail-docs] {e}", file=sys.stderr, flush=True)
            body = it.get("snippet") or ""
    elif it["kind"] == "solution":
        try:
            body = _solution_detail_body(it)
        except Exception as e:
            print(f"[detail-solution] {e}", file=sys.stderr, flush=True)
            body = it.get("snippet") or ""
    else:
        body = snippet_text(it.get("snippet") or "")

    if it.get("related"):
        body = body or it.get("snippet") or "Связанный материал."

    full = header + (body or "Пустая страница.")
    parts = [full[i:i + MSG_LIMIT] for i in range(0, len(full), MSG_LIMIT)]
    await _send(message.reply_text, parts[0], reply_markup=detail_keyboard())
    for part in parts[1:]:
        await _send(message.reply_text, part)


async def run_search(update, context, query):
    if not query.strip():
        await _send(update.effective_message.reply_text, "Пустой запрос.")
        return
    await render_results(context, query, update.effective_message)


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
        "«подключить оплаты через яндекс pay»\n\n"
        "Результаты собираются из документации, решений техподдержки и сделок.\n"
        "Кнопки 1–N под результатами открывают подробно."
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
        await render_results(context, query, q, edit=True)
        return
    idx = context.chat_data.get("last_detail_idx", 0)
    items = context.chat_data.get("last_items") or []
    if not items:
        await q.answer()
        await _send(q.message.reply_text, "Результаты не найдены — выполните поиск заново.")
        return
    nidx = idx - 1 if act == "prev" else idx + 1
    if not (0 <= nidx < len(items)):
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
    app.add_handler(CallbackQueryHandler(on_detail_callback, pattern=r"^d:\d+$"))
    app.add_handler(CallbackQueryHandler(on_nav_callback, pattern=r"^nav:"))
    app.add_error_handler(on_error)
    print("Бот запущен (polling). Остановка: Ctrl+C", flush=True)
    app.run_polling()


if __name__ == "__main__":
    main()