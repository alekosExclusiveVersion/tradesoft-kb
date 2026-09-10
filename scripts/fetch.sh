#!/bin/bash
# Обновление/сборка базы знаний Tradesoft.
# Использование: fetch.sh [--force] [--products name1,name2]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KB_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CACHE_DIR="$KB_ROOT/cache"
PRODUCTS_DIR="$KB_ROOT/products"
LOG_DIR="$KB_ROOT/logs"
PARSE_PY="$SCRIPT_DIR/parse.py"

CONCURRENCY="${CONCURRENCY:-12}"
FORCE=0
PRODUCT_FILTER=""
LOG_FILE="$LOG_DIR/update.log"
UA="Mozilla/5.0 (Macintosh) tsoft-kb/1.0"

usage() {
    echo "Использование: $0 [--force] [--products name1,name2]"
    echo "  --force            перекачать всё заново"
    echo "  --products p1,p2   обновить только указанные продукты"
    exit 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        --force) FORCE=1; shift ;;
        --products) PRODUCT_FILTER="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "Неизвестный аргумент: $1" >&2; usage ;;
    esac
done

mkdir -p "$CACHE_DIR" "$PRODUCTS_DIR" "$LOG_DIR"
touch "$LOG_FILE"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

# защита от параллельных запусков (дня нет flock в macOS)
LOCK_DIR="$CACHE_DIR/fetch.lock.d"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    if [ -f "$LOCK_DIR/pid" ] && kill -0 "$(cat "$LOCK_DIR/pid" 2>/dev/null)" 2>/dev/null; then
        log "fetch.sh уже выполняется (pid $(cat "$LOCK_DIR/pid" 2>/dev/null)) — пропуск"
        exit 0
    fi
    rm -rf "$LOCK_DIR"
    mkdir "$LOCK_DIR"
fi
echo "$$" > "$LOCK_DIR/pid"
trap 'rm -rf "$LOCK_DIR"' EXIT

PRODUCTS=$(cat <<'EOF'
parts-intellect-synch|https://product-doc.tradesoft.ru/ai/synch/|Синхронизатор Parts.Intellect
parts-resource-rest-api|https://product-doc.tradesoft.ru/ar/rest_api/|REST API Parts.Resource
parts-resource-guide|https://product-doc.tradesoft.ru/ar/ar/|Руководство пользователя Parts.Resource
parts-intellect-guide|https://product-doc.tradesoft.ru/ai/ai/|Руководство пользователя Parts.Intellect
parts-index-rest-api|https://product-doc.tradesoft.ru/ai/rest_api/|PartsIndex — REST API
seo-guide|https://product-doc.tradesoft.ru/ar/online_guides/seo_guide/|SEO-руководство
diadok|https://product-doc.tradesoft.ru/ai/diadok/|Диадок
delivery_schedule|https://product-doc.tradesoft.ru/ai/delivery_schedule/|График поставок
wazzup|https://product-doc.tradesoft.ru/ai/other/wazzup/|Wazzup
tsd|https://product-doc.tradesoft.ru/ai/tsd/|ТСД
marketplace|https://product-doc.tradesoft.ru/ai/marketplace/|Маркетплейсы
parts-resource-changes|https://product-doc.tradesoft.ru/ar/changes/|Изменения Parts.Resource (версии)
parts-intellect-changes|https://product-doc.tradesoft.ru/ai/changes/|Изменения Parts.Intellect (версии)
EOF
)

# ---------------------------------------------------------------- helpers ----
download_alljs() { # name base
    local name="$1" base="$2"
    local out="$CACHE_DIR/$name/all.js"
    mkdir -p "$CACHE_DIR/$name"
    curl -sS --connect-timeout 10 --max-time 60 --retry 3 --retry-delay 2 -A "$UA" -z "$out" -o "$out" "$base"js/all.js \
        || log "  [warn] не удалось получить all.js для $name"
}

toc() { # name
    local name="$1"
    local out="$CACHE_DIR/$name/toc.json"
    python3 "$PARSE_PY" toc "$CACHE_DIR/$name/all.js" > "$out" 2>/dev/null || {
        log "  [error] TOC не извлечён для $name"; return 1
    }
}

product_changed() { # name  -> 0 если all.js обновился/force
    local name="$1"
    [ "$FORCE" = "1" ] && return 0
    local sha_file="$CACHE_DIR/$name/alljs.sha256"
    local cur new
    cur="$(shasum -a 256 "$CACHE_DIR/$name/all.js" | awk '{print $1}')"
    new="$(cat "$sha_file" 2>/dev/null || echo "")"
    if [ "$cur" != "$new" ]; then
        echo "$cur" > "$sha_file"
        return 0
    fi
    return 1
}

download_pages() { # name base
    local name="$1" base="$2"
    local html_dir="$CACHE_DIR/$name/html" list="$CACHE_DIR/$name/page_urls.txt"
    mkdir -p "$html_dir"
    : > "$list"
    python3 - "$CACHE_DIR/$name/toc.json" "$base" "$html_dir" <<'PY' >> "$list"
import json, sys, os
toc = json.load(open(sys.argv[1]))
base, html_dir = sys.argv[2], sys.argv[3]
for p in toc["pages"]:
    print(f"{base}{p['link']}\t{os.path.join(html_dir, p['link'])}")
PY
    local total; total="$(wc -l < "$list" | tr -d ' ')"
    log "  страниц: $total"
    export FORCE LOG_FILE UA
    tr '\t' ' ' < "$list" | xargs -n2 -P "$CONCURRENCY" bash -c '
        url="$1"; out="$2"
        if [ "$FORCE" = "1" ]; then rm -f "$out"; fi
        curl -sS --connect-timeout 10 --max-time 60 --retry 3 --retry-delay 2 -A "$UA" --fail -z "$out" -o "$out" "$url" 2>/dev/null \
            || { [ -s "$out" ] || echo "  [fail] $url" >> "$LOG_FILE"; }
    ' _
    log "  html-страницы загружены"
}

parse_pages() { # name base
    local name="$1" base="$2"
    local html_dir="$CACHE_DIR/$name/html" parsed_dir="$CACHE_DIR/$name/parsed"
    mkdir -p "$parsed_dir"
    local list="$CACHE_DIR/$name/parse_list.txt"
    : > "$list"
    while IFS= read -r page_file; do
        page="${page_file##*/}"
        md="$parsed_dir/$page.md"
        if [ -f "$md" ] && [ "$md" -nt "$page_file" ]; then
            continue
        fi
        echo "$page_file" >> "$list"
    done < <(find "$html_dir" -name '*.htm' | sort)
    local total; total="$(wc -l < "$list" | tr -d ' ')"
    if [ "$total" -gt 0 ]; then
        log "  перепарсивается: $total"
        export PARSE_PY LOG_FILE PARSED_DIR="$parsed_dir" PAGE_BASE="$base"
        cat "$list" | xargs -n1 -P "$CONCURRENCY" bash -c '
            page_file="$1"
            page="${page_file##*/}"
            pagebase="${page%.htm}"
            out="$PARSED_DIR/$page.md"
            python3 "$PARSE_PY" html "$page_file" "$out" \
                --img-root "images/$pagebase" --base "$PAGE_BASE$page" 2>/dev/null \
                || echo "  [fail] парсинг $page" >> "$LOG_FILE"
        ' _
    else
        log "  изменений нет, парсинг пропущен"
    fi
    apply_patches "$name"
}

apply_patches() { # name — накладывает файлы из patches/<name>/parsed/ поверх cache/<name>/parsed/
    local name="$1"
    local patch_dir="$KB_ROOT/patches/$name/parsed"
    local parsed_dir="$CACHE_DIR/$name/parsed"
    if [ -d "$patch_dir" ]; then
        local count=0
        while IFS= read -r -d '' pf; do
            local bn; bn="$(basename "$pf")"
            cp "$pf" "$parsed_dir/$bn"
            count=$((count+1))
        done < <(find "$patch_dir" -name '*.md' -print0)
        log "  патчей применено: $count"
    fi
}

download_images() { # name
    local name="$1"
    local parsed_dir="$CACHE_DIR/$name/parsed" images_dir="$PRODUCTS_DIR/$name/images"
    mkdir -p "$images_dir"
    local list="$CACHE_DIR/$name/img_urls.txt"
    : > "$list"
    if [ -d "$parsed_dir" ]; then
        find "$parsed_dir" -name '*.imgs' -print0 | while IFS= read -r -d '' f; do
            pagebase="$(basename "${f%.imgs}")"
            pagebase="${pagebase%.md}"
            pagebase="${pagebase%.htm}"
            while IFS= read -r url; do
                [ -z "$url" ] && continue
                fname="${url##*/}"
                [ -z "$fname" ] && continue
                printf '%s\t%s\n' "$url" "$images_dir/$pagebase/$fname" >> "$list"
            done < "$f"
        done
    fi
    sort -u "$list" -o "$list"
    local total; total="$(wc -l < "$list" | tr -d ' ')"
    log "  изображений: $total"
    if [ "$total" -gt 0 ]; then
        export FORCE LOG_FILE UA
        local pass todo=0
        for pass in 1 2 3; do
            : > "$list.pass"
            todo=0
            while IFS= read -r line; do
                local out; out="${line#*$'\t'}"
                [ -s "$out" ] || { echo "$line" >> "$list.pass"; todo=$((todo+1)); }
            done < "$list"
            [ "$todo" -eq 0 ] && break
            [ "$pass" -gt 1 ] && log "  повторная загрузка (проход $pass): $todo"
            sort -u "$list.pass" | tr '\t' ' ' | xargs -n2 -P "$CONCURRENCY" bash -c '
                url="$1"; out="$2"
                mkdir -p "$(dirname "$out")"
                if [ "$FORCE" = "1" ]; then rm -f "$out"; fi
                curl -sS --connect-timeout 10 --max-time 60 --retry 3 --retry-delay 2 -A "$UA" --fail -z "$out" -o "$out" "$url" 2>/dev/null \
                    || { [ -s "$out" ] || echo "  [fail] img $url" >> "$LOG_FILE"; }
            ' _
        done
        [ "$todo" -gt 0 ] && log "  [warn] не скачано: $todo"
    fi
    # удалить каталоги изображений, не соответствующие текущим страницам
    if [ -d "$parsed_dir" ]; then
        find "$images_dir" -mindepth 1 -maxdepth 1 -type d -print0 | while IFS= read -r -d '' d; do
            pagebase="$(basename "$d")"
            [ -f "$parsed_dir/$pagebase.htm.md" ] || rm -rf "$d"
        done
    fi
    log "  изображения загружены"
}

detect_version() { # name
    local name="$1" html_dir="$CACHE_DIR/$name/html"
    python3 - "$html_dir" <<'PY'
import re, sys, os
d = sys.argv[1]
pat = re.compile(r'верси[яи]\s*([\d]+(?:\.[\d]+)*)', re.I)
for fn in ("index.htm",):
    p = os.path.join(d, fn)
    if os.path.exists(p):
        src = open(p, encoding="utf-8", errors="replace").read()
        m = pat.search(src)
        if m:
            print(m.group(1)); sys.exit(0)
print("")
PY
}

build_index() { # name base display version
    local name="$1" base="$2" display="$3" version="$4"
    local out="$PRODUCTS_DIR/$name/index.md"
    {
        echo "# $display"
        echo ""
        echo "- Продукт: $display"
        echo "- Источник: $base"
        [ -n "$version" ] && echo "- Версия: $version"
        echo "- Страниц: $(python3 -c "import json;print(len(json.load(open('$CACHE_DIR/$name/toc.json'))['pages']))")"
        echo "- Изображений: $(find "$PRODUCTS_DIR/$name/images" -type f 2>/dev/null | wc -l | tr -d ' ')"
        echo ""
        echo "## Оглавление"
        echo ""
        python3 - "$CACHE_DIR/$name/toc.json" "$base" <<'PY'
import json, sys
toc = json.load(open(sys.argv[1]))
base = sys.argv[2]
pages = toc["pages"]
by_parent = {}
for p in pages:
    by_parent.setdefault(p["parent"], []).append(p)

def walk(idx, prefix):
    for p in by_parent.get(idx, []):
        print(f"{prefix}- [{p['title']}]({base}{p['link']})")
        walk(p["index"], prefix + "  ")

walk(-1, "")
PY
    } > "$out"
    log "  index.md собран"
}

build_content() { # name base
    local name="$1" base="$2"
    local out="$PRODUCTS_DIR/$name/content.md"
    local parsed_dir="$CACHE_DIR/$name/parsed"
    : > "$out"
    python3 - "$CACHE_DIR/$name/toc.json" "$base" "$parsed_dir" "$out" <<'PY'
import json, sys, os
toc = json.load(open(sys.argv[1]))
base = sys.argv[2]
parsed = sys.argv[3]
out = sys.argv[4]
parts = []
for p in toc["pages"]:
    md = os.path.join(parsed, p["link"] + ".md")
    if not os.path.exists(md):
        continue
    body = open(md, encoding="utf-8").read().strip()
    if not body:
        continue
    parts.append(f"## {p['title']}\n\n> Источник: {base}{p['link']}\n\n{body}")
open(out, "w", encoding="utf-8").write("\n\n---\n\n".join(parts) + "\n")
PY
    log "  content.md собран"
}

update_manifest() {
    python3 - "$KB_ROOT" "$CACHE_DIR" "$PRODUCTS_DIR" <<'PY'
import json, os, sys, time, subprocess
kb, cache, prods = sys.argv[1], sys.argv[2], sys.argv[3]
products = {}
for name in sorted(os.listdir(prods)):
    if not os.path.isdir(os.path.join(prods, name)):
        continue
    img_dir = os.path.join(prods, name, "images")
    n_img = 0; img_bytes = 0
    for root, _, files in os.walk(img_dir):
        for f in files:
            n_img += 1
            img_bytes += os.path.getsize(os.path.join(root, f))
    alljs = os.path.join(cache, name, "all.js")
    alljs_hash = ""
    if os.path.exists(alljs):
        alljs_hash = subprocess.run(["shasum", "-a", "256", alljs], capture_output=True, text=True).stdout.split()[0]
    meta = {}
    meta_file = os.path.join(cache, name, "meta.json")
    if os.path.exists(meta_file):
        meta = json.load(open(meta_file, encoding="utf-8"))
    products[name] = {
        "display": meta.get("display", name),
        "source": meta.get("source", ""),
        "version": meta.get("version", ""),
        "pages": len(json.load(open(os.path.join(cache, name, "toc.json")))["pages"]),
        "images": n_img,
        "images_bytes": img_bytes,
        "alljs_hash": alljs_hash,
    }
manifest = {
    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "products": products,
}
json.dump(manifest, open(os.path.join(kb, "manifest.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("manifest обновлён")
PY
}

# ---------------------------------------------------------------- main ------
declare -a NAME_ARR URL_ARR DISP_ARR
while IFS='|' read -r n u d; do
    NAME_ARR+=("$n"); URL_ARR+=("$u"); DISP_ARR+=("$d")
done <<< "$PRODUCTS"

for i in "${!NAME_ARR[@]}"; do
    name="${NAME_ARR[$i]}"; base="${URL_ARR[$i]}"; display="${DISP_ARR[$i]}"
    if [ -n "$PRODUCT_FILTER" ]; then
        case ",$PRODUCT_FILTER," in
            *",$name,"*) ;;
            *) continue ;;
        esac
    fi
    log "=== $name ($display) ==="
    download_alljs "$name" "$base" || continue
    toc "$name" || continue
    if product_changed "$name"; then
        log "  all.js изменился — полная пересборка"
        rm -rf "$CACHE_DIR/$name/parsed"
    fi
    download_pages "$name" "$base"
    parse_pages "$name" "$base"
    download_images "$name"
    version="$(detect_version "$name")"
    python3 - "$CACHE_DIR/$name/meta.json" "$display" "$base" "$version" <<'PY'
import json, sys
json.dump({"display": sys.argv[2], "source": sys.argv[3], "version": sys.argv[4]},
          open(sys.argv[1], "w", encoding="utf-8"), ensure_ascii=False)
PY
    build_index "$name" "$base" "$display" "$version"
    build_content "$name" "$base"
done

python3 "$SCRIPT_DIR/build_index.py"
update_manifest
log "=== Готово ==="
