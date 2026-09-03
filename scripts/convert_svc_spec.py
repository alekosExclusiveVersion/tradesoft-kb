#!/usr/bin/env python3
"""Конвертирует OpenAPI-спецификацию Tradesoft service API в страницы БЗ.

Источник: OpenAPI 3.0.1 (YAML), обычно /tmp/svc_spec.json (файл-«json», но
фактически YAML). Генерирует страницы в cache/service-api/parsed/*.htm.md
в том же формате, что и остальные продукты (заголовки ##/### + GFM-таблицы).

Использование:
  python3 convert_svc_spec.py [путь_к_спецификации]
"""
import html
import os
import re
import sys

try:
    import yaml
except ImportError:
    sys.exit("Нужен PyYAML: ./.venv/bin/pip install pyyaml")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
KB_ROOT = os.path.dirname(SCRIPT_DIR)
PARSED_DIR = os.path.join(KB_ROOT, "cache", "service-api", "parsed")

SERVICES = {
    "Provider": "Provider (сервис поставщиков)",
    "Info": "Info (сервис информации о детали)",
    "Analog": "Analog (сервис аналогов/взаимозамен)",
    "Messenger": "Messenger (сервис уведомлений/SMS)",
}

DOC_URL = "https://service.tradesoft.ru/3/docs/"

# Русские заголовки/описания эндпоинтов (добавляются к англ. контенту, чтобы
# страницы совпадали с русскими запросами и по FTS, и по векторной семантике).
RUS_ENDPOINT = {
    "get-producer-list": {
        "title": "Получить список производителей",
        "desc": "Возвращает список производителей по коду детали. Сервис поставщиков.",
    },
    "get-price-list": {
        "title": "Получить список предложений (цен) по производителям",
        "desc": "Возвращает список предложений (цен) по коду детали и производителю. "
                "Поставщик, прайс-лист, цена, наличие.",
    },
    "get-additional-part-info": {
        "title": "Получить дополнительную информацию о детали",
        "desc": "Дополнительная информация о детали, в т.ч. о б/у деталях.",
    },
    "get-options-list": {
        "title": "Получить список опций поставщика",
        "desc": "Возвращает список опций, доступных поставщику через API.",
    },
    "get-provider-list": {
        "title": "Получить список доступных поставщиков (подключение)",
        "desc": "Возвращает список поставщиков, подключённых к аккаунту: активен ли сервис "
                "поиска цен, доступен ли онлайн-заказ, требуется ли согласование доступа, "
                "условия и логин/пароль для подключения поставщика.",
    },
    "pre-order-search": {
        "title": "Предварительный поиск заказа",
        "desc": "Возвращает список позиций для заказа; обновляет цены и наличие "
                "перед оформлением заказа поставщику.",
    },
    "make-order-offline": {
        "title": "Оформить заказ поставщику (офлайн)",
        "desc": "Оформление заказа поставщику. Доступно с сервисом Онлайн-заказ.",
    },
    "get-status-list": {
        "title": "Получить список доступных статусов заказа",
        "desc": "Возвращает список статусов заказа (включая технические статусы). "
                "Онлайн-заказ, статусы заказа.",
    },
    "get-items-status": {
        "title": "Получить статусы заказанных позиций",
        "desc": "Возвращает статусы заказанных позиций. Онлайн-заказ.",
    },
    "get-part-info": {
        "title": "Получить информацию о детали",
        "desc": "Информация о детали. Сервис Web info, лимит 500 000 запросов в день.",
    },
    "get-brands-by-article": {
        "title": "Получить список брендов по артикулу (номеру детали)",
        "desc": "Возвращает список брендов по номеру детали. Сервис Web info.",
    },
    "get-brands-by-barcode": {
        "title": "Получить список брендов по штрих-коду",
        "desc": "Возвращает список брендов по штрих-коду. Сервис Web info.",
    },
    "get-analogs": {
        "title": "Получить список аналогов (взаимозамен) по номеру и бренду",
        "desc": "Возвращает список аналогов по номеру детали и бренду. Сервис Онлайн-аналоги.",
    },
    "get-analogs-adv": {
        "title": "Получить расширенный список аналогов",
        "desc": "Расширенный список аналогов (взаимозамен) детали. Сервис Онлайн-аналоги.",
    },
    "get-producers-adv": {
        "title": "Получить список доступных производителей по номеру",
        "desc": "Список доступных производителей по номеру детали. Сервис Онлайн-аналоги.",
    },
    "send-sms": {
        "title": "Отправить SMS на мобильный номер",
        "desc": "Отправка SMS-сообщения. Сервис уведомлений (Messenger).",
    },
    "get-sms-status": {
        "title": "Получить информацию об отправленных SMS",
        "desc": "Статус отправленных SMS-сообщений. Сервис уведомлений.",
    },
    "get-sms-balance": {
        "title": "Проверить SMS-баланс",
        "desc": "Остаток SMS-баланса. Сервис уведомлений.",
    },
    "get-sms-balance2": {
        "title": "Проверить SMS-баланс (вариант 2)",
        "desc": "Остаток SMS-баланса. Сервис уведомлений.",
    },
}

# Русские названия полей (к общему полю подключения).
RU_FIELD = {
    "user": "Логин на сайте TradeSoft",
    "password": "Пароль на сайте TradeSoft",
    "service": "Имя сервиса",
    "action": "Код операции",
    "timelimit": "Лимит времени",
    "container": "Корзина запросов к поставщикам",
    "param": "Параметры запроса",
    "params": "Параметры запроса",
    "data": "Данные ответа",
    "error": "Ошибка (код/текст)",
    "time": "Время обработки",
    "geoDictionary": "Словарь географических данных",
    "active": "Сервис поиска цен доступен (подключён)",
    "orderActive": "Сервис онлайн-заказа доступен (подключён)",
    "contractRequired": "Для подключения требуется согласование доступа",
    "agreementText": "Текст согласования с поставщиком",
    "loginInfoText": "Правила ввода логина/пароля на сервисе поставщика",
    "siteUrl": "Ссылка на сайт поставщика",
    "title": "Название",
    "description": "Описание",
}


def slug(s: str) -> str:
    return "_".join(w for w in s.split() if w).strip()

def esc(v) -> str:
    if v is None:
        return ""
    return str(v).replace("|", "\\|")


def resolve_ref(schema, schemas):
    """Возвращает (dict-схема, имя_схемы)."""
    if isinstance(schema, dict) and "$ref" in schema:
        name = schema["$ref"].split("/")[-1]
        return schemas.get(name, {}), name
    return schema, None


def field_row(name, pv):
    ref = pv.get("$ref", "")
    typ = pv.get("type", "")
    if not typ and ref:
        typ = ref.split("/")[-1]
    if typ == "array":
        items = pv.get("items", {})
        if isinstance(items, dict) and "$ref" in items:
            typ = "массив (" + items["$ref"].split("/")[-1] + ")"
        else:
            typ = "массив"
    desc = clean_text(pv.get("description") or "")
    ex = pv.get("example")
    label = RU_FIELD.get(name, "")
    parts = []
    if label:
        parts.append(f"**{label}**.")
    if desc:
        parts.append(desc)
    desc_s = " ".join(parts)
    ex_s = f" Например: `{esc(ex)}`." if ex not in (None, "") else ""
    return f"| {esc(name)} | {esc(typ)} | {esc(desc_s)}{ex_s} |"


def props_table(schema, schemas, title="Поле"):
    props = (schema or {}).get("properties")
    if not props:
        return ""
    lines = [f"| {title} | Тип | Описание |", "| --- | --- | --- |"]
    for name, pv in props.items():
        lines.append(field_row(name, pv))
    return "\n".join(lines)


def clean_text(s: str) -> str:
    s = html.unescape(s).replace("<br/>", " ").replace("<br>", " ").strip()
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


def endpoint_page(path, op, schemas):
    summary = clean_text(op.get("summary") or "")
    desc = clean_text(op.get("description") or "")
    seg = path.strip("/").split("/")[-1]
    rus = RUS_ENDPOINT.get(seg)
    lines = [f"## Эндпоинт `{path}`"]
    if rus:
        lines.append("")
        lines.append(f"### {rus['title']}")
        lines.append("")
        lines.append(rus["desc"])
        lines.append("")
        lines.append("---")
    if summary:
        lines.append("")
        lines.append(f"### {summary} ({rus['title'] if rus else ''})".rstrip())
    if desc:
        lines.append("")
        lines.append(desc)
    lines.append("")
    lines.append(f"Метод: **POST**. Базовый URL: **https://service.tradesoft.ru/3**.")
    lines.append("")
    req = op.get("requestBody", {})
    schema, sname = resolve_ref(
        req.get("content", {}).get("*/*", {}).get("schema", {}), schemas)
    if sname:
        lines.append("")
        lines.append("### Поля запроса")
        lines.append("")
        lines.append(props_table(schema, schemas, "Параметр"))
        if not (schema or {}).get("properties"):
            lines.append(f"Схема запроса: `{sname}`.")
    resp_codes = op.get("responses", {})
    resp = {}
    for code in ("200", 200, "default", "2XX"):
        if code in resp_codes:
            resp = resp_codes[code]
            break
    rs, rsname = resolve_ref(
        resp.get("content", {}).get("application/json", {}).get("schema", {}), schemas)
    if rsname:
        lines.append("")
        lines.append("### Поля ответа")
        lines.append("")
        lines.append(props_table(rs, schemas, "Поле"))
        # вложенные массивы (напр. data -> supplierData: данные о подключении поставщиков)
        for pname, pv in (rs.get("properties") or {}).items():
            if isinstance(pv, dict) and pv.get("type") == "array":
                items = pv.get("items", {})
                if isinstance(items, dict) and "$ref" in items:
                    rn = items["$ref"].split("/")[-1]
                    nested = schemas.get(rn, {})
                    nested_t = props_table(nested, schemas, "Поле")
                    if nested_t:
                        lines.append("")
                        lines.append(f"Поля элементов массива `{pname}` (схема `{rn}`):")
                        lines.append("")
                        lines.append(nested_t)
        if not (rs or {}).get("properties"):
            lines.append(f"Схема ответа: `{rsname}`.")
    lines.append("")
    return "\n".join(lines)


def group_page(tag, ops, schemas):
    label = SERVICES.get(tag, tag)
    lines = [f"## Tradesoft service API — {label}"]
    lines.append("")
    lines.append(f"Группа эндпоинтов: **{tag}**.")
    lines.append("")
    for path, path_ops in ops.items():
        op = path_ops.get("post", {})
        summary = (op.get("summary") or "").strip()
        lines.append(f"- `{path}` — {summary}")
    lines.append("")
    return "\n".join(lines)


def overview_page(spec, schemas):
    info = spec.get("info", {})
    title = info.get("title", "Tradesoft service API")
    desc = clean_text(info.get("description") or "").replace("\n", " ")
    lines = [
        f"## {title} — обзор и подключение по API",
        "",
        "Документация по адресу: {0} (примеры использования — в разделе Examples default).".format(DOC_URL),
        "",
    ]
    if desc:
        lines.append(desc.strip())
        lines.append("")
    lines.append("### Доступ к сервису")
    lines.append("")
    lines.append(
        "Для использования сервиса требуется аккаунт на сайте **tradesoft.pro**. "
        "Все запросы выполняются методом **POST** на базовый URL **https://service.tradesoft.ru/3**."
    )
    lines.append("")
    lines.append("### Авторизация во всех запросах")
    lines.append("")
    lines.append(
        "Каждый запрос содержит общий набор полей подключения:"
    )
    lines.append("")
    lines.append("| Поле | Тип | Описание |")
    lines.append("| --- | --- | --- |")
    lines.append("| user | string | Логин на сайте TradeSoft (tradesoft.pro) |")
    lines.append("| password | string | Пароль на сайте TradeSoft (tradesoft.pro) |")
    lines.append("| service | string | Имя сервиса, которому отправляется запрос (`provider`, `info`, `analog`, `messenger`) |")
    lines.append("| action | string | Код операции (например `getProviderList`, `getPriceList`) |")
    lines.append("| timelimit | integer | Лимит времени на выполнение запроса (необязательно) |")
    lines.append("| container | массив | «Корзина» запросов к поставщикам (для сервиса поставщиков) |")
    lines.append("| param | массив/объект | Параметры запроса (зависит от эндпоинта) |")
    lines.append("")
    lines.append("### Группы (сервисы)")
    lines.append("")
    for tag, opi in spec.get("paths", {}).items():
        pass
    grouped = {}
    for path, path_ops in spec.get("paths", {}).items():
        op = path_ops.get("post", {})
        for t in op.get("tags") or ["Other"]:
            grouped.setdefault(t, []).append(path)
    for tag, label in SERVICES.items():
        lines.append(f"- **{tag}** — {label.split('(')[1].rstrip(')')}: {len(grouped.get(tag, []))} эндпоинтов")
    lines.append("")
    lines.append("### Формат ответа")
    lines.append("")
    lines.append("Общий формат ответа: поле `data` с результатом, поле `error` с кодом/текстом ошибки "
                 "и поле `time` со временем обработки (для сервиса поставщиков также `geoDictionary`).")
    lines.append("")
    return "\n".join(lines)


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "/tmp/svc_spec.json"
    with open(src, encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    schemas = spec.get("components", {}).get("schemas", {})

    os.makedirs(PARSED_DIR, exist_ok=True)
    written = []

    def write(name, content, title):
        path = os.path.join(PARSED_DIR, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content + "\n")
        written.append(name)

    write("0__obzor_i_podklyuchenie.htm.md", overview_page(spec, schemas), "overview")

    grouped = {}
    path_by_tag = {}
    for path, path_ops in spec.get("paths", {}).items():
        op = path_ops.get("post", {})
        tag = (op.get("tags") or ["Other"])[0]
        grouped.setdefault(tag, []).append((path, path_ops))

    # отдельные страницы групп
    order = {"provider": 1, "info": 2, "analog": 3, "messenger": 4}
    for tag, ops_list in grouped.items():
        ops = dict(ops_list)
        idx = order.get(tag.lower(), 9)
        write(f"{idx}__{tag.lower()}_service.htm.md", group_page(tag, ops, schemas), "group")
        # страница на каждый эндпоинт
        for path, path_ops in ops_list:
            op = path_ops.get("post", {})
            seg = path.strip("/").replace("/", "_")
            write(f"{tag.lower()}__{seg}.htm.md", endpoint_page(path, op, schemas), "endpoint")

    print(f"Сгенерировано страниц: {len(written)} -> {PARSED_DIR}")


if __name__ == "__main__":
    main()
