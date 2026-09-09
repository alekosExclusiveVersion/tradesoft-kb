"""Общее слияние FTS5 + Typesense-vector (RRF) для гибридного поиска.

Единый источник правды для search_hybrid() (search.py) и _hybrid_from_hits()
(eval_server.py), чтобы RRF-логика не расходилась между двумя файлами.

Ключевая идея уверенного слияния: когда RRF-скор нескольких страниц равен или
близок, победитель определяется СЕМАНТИЧЕСКОЙ уверенностью (vector _score), а
не порядком выдачи FTS. Страница, найденная только FTS и отсутствующая в
векторном top-N, получает vec_score=0 и проигрывает ничью реально релевантному
векторному хиту (это чинит топ-1 вида «подключить нового поставщика», где
FTS-AND ловил нерелевантную «выгрузку»).
"""
from search import (  # noqa: E402
    RRF_K, normalize_terms, terms, _discriminators, _page_has_terms,
    detect_products, detect_product_name, _correct_terms,
    ULTRA_COMMON,
)
import rank_model  # noqa: E402

API_PRODUCTS = {"service-api", "parts-resource-rest-api"}
API_RRF_BONUS = 0.02
# API-запросы («…по api», «service api») — семантически однозначные справочные
# страницы методов. Эмбеддинг на них точен (векторный топ-1 = искомая страница),
# а FTS5 путает их между собой по частым словам («заказ клиента» vs «клиент»).
# Поэтому векторной ноге при api-intent даём больший вес в RRF-сумме.
VEC_API_WEIGHT = 3.0

# --- Самообучающееся ранжирование -------------------------------------------
# Порядок признаков результата (должен совпадать с rank_train.py и
# rank_model.FEAT_NAMES). Признаки считаются в fuse() и кладутся в
# meta["features"][(product,page)] = [f0..f5]. Если обученные веса отсутствуют
# или не активны, все веса = 0 → поведение ровно как раньше (без изменений).
FEAT_NAMES = [
    "fts_rrf",      # 1/(RRF_K+fts_rank+1), 0 если результат только из вектора
    "vec_rrf",      # 1/(RRF_K+vec_rank+1), 0 если результат только из FTS
    "vec_score",    # косинусная уверенность вектора (0, если вектора нет)
    "product_match",  # 1 если продукт результата = детектированному продукту
    "is_api",         # 1 если продукт в API_PRODUCTS
    "is_changelog",   # 1 если продукт в CHANGELOG_PRODUCTS
]
FEAT_DIM = len(FEAT_NAMES)
# Историко-измененные (changelog) продукты: содержат обзорные записи по версиям,
# которые шумят в общем поиске. Получают РRF-понижение, чтобы не вытеснять
# реальные страницы руководств для общих запросов, но оставались находимыми,
# когда продукт назван явно или подходящих руководств нет.
CHANGELOG_PRODUCTS = {"parts-resource-changes", "parts-intellect-changes"}
CHANGELOG_RRF_PENALTY = 0.035
# Бонус странице, чей слэг содержит ВСЕ цифровые токены запроса (точное
# совпадение номера версии: «…версия 6.74» -> versiya_6_74). Выше штрафа
# changelog, чтобы обходить отрицательный вклад пенализации.
VERSION_MATCH_BONUS = 0.08
RBUF_TOP_N = 30  # сколько векторных хитов участвует в слиянии


def _vk(h, key):
    """Достаёт поле из вектора-хита, работая и со словарём, и с кортежем."""
    if isinstance(h, dict):
        return h.get(key)
    idx = {"product": 0, "page": 1, "_score": 5}.get(key)
    return h[idx] if idx is not None and idx < len(h) else None


def fuse(query, fts_rows, vec_hits, product=None):
    """Сливает FTS- и векторные результаты в упорядоченный список ключей.

    fts_rows: [(product, page, title, path, snippet, score), ...]
    vec_hits: последовательность словарей {"product","page","_score",...}
              или кортежей (product, page, ...). _score извлекается по индексу 5.

    Возвращает (ordered_keys, meta):
      ordered_keys: [(product, page), ...] — дедуплицировано, топ впереди
      meta: dict с параметрами слияния (rare, api_intent, norm, boost)
    """
    norm = _correct_terms(normalize_terms(terms(query)))

    candidates = list(dict.fromkeys(
        [(r[0], r[1]) for r in fts_rows] +
        [(_vk(h, "product"), _vk(h, "page")) for h in vec_hits[:RBUF_TOP_N]]
    ))
    rare = _discriminators(norm, candidates)
    boost = 12 if rare else 0

    det = detect_products(query)
    api_intent = bool(det and det[0][0] in API_PRODUCTS) or "api" in norm
    detected_product = det[0][0] if det else None

    rrf, order = {}, {}
    vec_score = {}

    fts_rank = {r[0]: i for i, r in enumerate(fts_rows)}
    # векторные ранги с учётом boost-сдвига (как в цикле ниже)
    vec_rank = {}

    def add(key, rank):
        rrf[key] = rrf.get(key, 0.0) + 1.0 / (RRF_K + rank + 1)
        order.setdefault(key, 0)

    for i, r in enumerate(fts_rows):
        k = (r[0], r[1])
        add(k, i)
        order[k] = i

    for i, h in enumerate(vec_hits[:RBUF_TOP_N]):
        k = (_vk(h, "product"), _vk(h, "page"))
        eff = i
        if boost:
            eff = max(0, i - boost) if _page_has_terms(k[0], k[1], rare) \
                else i + boost
        vec_w = VEC_API_WEIGHT if "api" in norm else 1.0
        rrf[k] = rrf.get(k, 0.0) + vec_w / (RRF_K + eff + 1)
        order.setdefault(k, 0)
        vec_rank[k] = eff
        sc = _vk(h, "_score") or 0.0
        vec_score[k] = max(vec_score.get(k, 0.0), sc)
        if api_intent and k[0] in API_PRODUCTS:
            rrf[k] = rrf.get(k, 0.0) + API_RRF_BONUS
        if k not in order:
            order[k] = len(fts_rows) + i

    # Историко-измененные продукты — понижение суммарного RRF (не вытесняют
    # реальные страницы руководств для общих запросов).
    for k in rrf:
        if k[0] in CHANGELOG_PRODUCTS:
            rrf[k] = rrf.get(k, 0.0) - CHANGELOG_RRF_PENALTY
    # Точное совпадение версии: если в запросе есть цифровые токены (6.74 -> 6,74)
    # и слэг страницы содержит ровно их («versiya_6_74»), странице верится больше
    # всех семантических соседей («версия 6.70» семантически близка к «6.74», но
    # не релевантна). Эмбеддинг «смазывает» номера версий — это компенсируем
    # объективным совпадением.
    query_digits = {t for t in norm if t.isdigit()}
    if query_digits:
        import re as _re_digits
        for k in rrf:
            slug_digits = set(_re_digits.findall(r"\d+", k[1]))
            if query_digits <= slug_digits:
                rrf[k] = rrf.get(k, 0.0) + VERSION_MATCH_BONUS

    # --- Самообучающееся ранжирование --------------------------------------
    # Признаки результата + аддитивная поправка от обученных весов (см.
    # rank_model). Когда модель выключена, поправка = 0 и ранжирование ровно
    # такое же, как до её введения.
    features: dict[tuple, list[float]] = {}
    for k in rrf:
        fr = fts_rank.get(k)
        vr = vec_rank.get(k)
        features[k] = [
            1.0 / (RRF_K + fr + 1) if fr is not None else 0.0,
            1.0 / (RRF_K + vr + 1) if vr is not None else 0.0,
            vec_score.get(k, 0.0),
            1.0 if detected_product and detected_product == k[0] else 0.0,
            1.0 if k[0] in API_PRODUCTS else 0.0,
            1.0 if k[0] in CHANGELOG_PRODUCTS else 0.0,
        ]
        rrf[k] = rrf.get(k, 0.0) + rank_model.weight_adjustment(features[k])

    # Сортировка: RRF вниз, затем по семантической уверенности (vector score),
    # затем порядок. Семантическая уверенность развязывает ничьи в пользу
    # релевантного векторного хита, а не строгого (но часто нерелевантного) FTS.
    def keyfn(kv):
        return (-kv[1], -vec_score.get(kv[0], 0.0), order[kv[0]])

    merged = sorted(rrf.items(), key=keyfn)

    # Обрезание до релевантных: если у запроса есть дискриминативные термины,
    # показываем только страницы, содержащие хотя бы один из них.
    if rare:
        merged = [kv for kv in merged
                  if _page_has_terms(kv[0][0], kv[0][1], rare)]
        if len(merged) < 2:
            merged = sorted(rrf.items(), key=keyfn)

    return [kv[0] for kv in merged], {
        "rare": rare,
        "api_intent": api_intent,
        "norm": norm,
        "boost": boost,
        "vec_score": vec_score,
        "features": features,
        "rank_status": rank_model.dump_status(),
    }
