#!/usr/bin/env python3
"""HTML -> Markdown converter and TOC extractor for Drexplain help sites.

Usage:
  parse.py toc <alljs>            -> prints JSON with the page tree
  parse.py html <htmlfile> <outfile> [--img-root <prefix>] [--base <url>]
                                   -> writes markdown body to outfile and a list
                                      of image URLs (one per line) to outfile + ".imgs"
"""
import sys
import json
import re
import html as htmlmod
from html.parser import HTMLParser
from urllib.parse import urljoin


# ---------------------------------------------------------------- TOC -------
def extract_toc(alljs_text):
    def grab(name):
        m = re.search(
            re.escape(name) + r":\s*(\[[\s\S]*?\])(?=,\s*[A-Z_]+:|\n?\s*\})",
            alljs_text,
        )
        if not m:
            return None
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            return None

    names = grab("DREX_NODE_NAMES")
    links = grab("DREX_NODE_LINKS")
    cs = grab("DREX_NODE_CHILD_START")
    ce = grab("DREX_NODE_CHILD_END")
    if not (names and links and cs and ce):
        return None

    parent = [-1] * len(names)
    deep = [0] * len(names)
    for i in range(len(names)):
        for j in range(cs[i], ce[i]):
            if j < len(names):
                parent[j] = i
                deep[j] = deep[i] + 1

    pages = [{"index": i, "title": names[i], "link": links[i], "deep": deep[i], "parent": parent[i]}
             for i in range(len(names))]
    return {"pages": pages}


def toc_cmd(args):
    with open(args[0], encoding="utf-8") as f:
        data = f.read()
    toc = extract_toc(data)
    if toc is None:
        print("ERROR: TOC not found in " + args[0], file=sys.stderr)
        sys.exit(1)
    print(json.dumps(toc, ensure_ascii=False))


# ---------------------------------------------------------------- HTML ------
SKIP_TAGS = {"script", "style", "noscript", "map", "area", "head", "nav", "iframe"}
# block elements that flush the current inline text
FLUSH_TAGS = {"div", "p", "h1", "h2", "h3", "h4", "h5", "h6",
              "li", "ul", "ol", "table", "tr", "td", "th", "section", "article", "blockquote"}


class MdParser(HTMLParser):
    def __init__(self, img_root):
        super().__init__(convert_charrefs=True)
        self.img_root = img_root
        self.para = []          # lines of current paragraph
        self.out = []           # list of paragraphs (strings)
        self.cur = []           # current inline text chars
        self.stack = []         # (tag, start_cur_len) tuples
        self.skip = 0           # depth of skipped subtree
        self.title = None
        self.images = []        # list of (abs_url, cls)
        self.li_level = 0
        self._base = ""
        self._in_article = 0

    # -- helpers --------------------------------------------------------------
    def _flush(self):
        txt = "".join(self.cur).strip()
        self.cur = []
        if txt:
            self.para.append(re.sub(r"\s+", " ", txt))

    def _end_para(self):
        self._flush()
        if self.para:
            self.out.append("\n".join(self.para))
        self.para = []

    def _text(self, s):
        if not self.skip and self._in_article > 0:
            self.cur.append(s)

    def _cur_len(self):
        return sum(len(ch) for ch in self.cur)

    def _push(self, tag):
        self.stack.append((tag, self._cur_len()))

    def _pop(self, tag):
        for k in range(len(self.stack) - 1, -1, -1):
            if self.stack[k][0] == tag:
                del self.stack[k]
                return
        return

    def _close_inline(self, tag, marker):
        for k in range(len(self.stack) - 1, -1, -1):
            if self.stack[k][0] == tag:
                start_len = self.stack[k][1]
                if self._cur_len() > start_len:
                    self.cur.append(marker)
                del self.stack[k]
                return

    # -- parser callbacks -----------------------------------------------------
    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip += 1
            return
        if self.skip:
            return
        a = dict(attrs)

        if tag == "article":
            self._in_article += 1
            return

        if self._in_article <= 0:
            return

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._end_para()
            self._h_tag = tag
            self._push(tag)
            return

        if tag in ("ul", "ol"):
            self._end_para()
            self.li_level += 1
            self._push(tag)
            return

        if tag == "li":
            self._end_para()
            self._push("li")
            return

        if tag == "table":
            self._end_para()
            self._push("table")
            return

        if tag == "tr":
            self._flush()
            self._push("tr")
            return

        if tag in ("td", "th"):
            self._flush()
            self._push(tag)
            return

        if tag == "br":
            self._text(" ")
            return

        if tag == "img":
            src = a.get("src", "")
            cls = a.get("class", "")
            if src:
                full = urljoin(self._base, src)
                if full not in [i[0] for i in self.images]:
                    self.images.append((full, cls))
                if "de_wndimg" in cls.split():
                    alt = a.get("alt", "")
                    fname = full.split("/")[-1]
                    ref = f"{self.img_root}/{fname}" if self.img_root else fname
                    self._flush()
                    self.para.append(f"![{alt}]({ref})")
            return

        if tag == "a":
            href = a.get("href", "")
            if href in ("#", "#top") or href.startswith("#"):
                self._push(("skip_a", self._cur_len()))
            else:
                self._push("a")
            return

        if tag in ("strong", "b"):
            self.cur.append("**")
            self._push("strong")
            return
        if tag in ("em", "i"):
            self.cur.append("_")
            self._push("em")
            return
        if tag == "code":
            self.cur.append("`")
            self._push("code")
            return
        if tag in ("sub", "sup"):
            self._push(tag)
            return
        if tag == "font":
            self._push("font")
            return
        if tag in ("span", "label", "b", "i"):
            self._push(tag)
            return
        if tag in FLUSH_TAGS:
            self._flush()
            self._push(tag)
            return
        self._push(tag)

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            if self.skip:
                self.skip -= 1
            return
        if self.skip:
            return
        if tag == "article":
            self._in_article -= 1
            return
        if self._in_article <= 0:
            return

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush()
            level = int(tag[1]) + 2  # page section heading is "##", h1 -> ###
            txt = " ".join(self.para)
            self.para = []
            if txt:
                self.out.append("#" * level + " " + txt)
            self._pop(tag)
            return

        if tag in ("ul", "ol"):
            self._end_para()
            if self.li_level > 0:
                self.li_level -= 1
            self._pop(tag)
            return

        if tag == "li":
            txt = " ".join(self.para)
            self.para = []
            if txt and txt != "Наверх":
                indent = "  " * max(self.li_level - 1, 0)
                self.out.append(indent + "- " + txt)
            self._pop("li")
            return

        if tag == "table":
            self._end_para()
            self._pop("table")
            return

        if tag == "tr":
            self._flush()
            self._end_para()
            self._pop("tr")
            return

        if tag in ("td", "th"):
            self._flush()
            self._pop(tag)
            return

        if tag == "a":
            for k in range(len(self.stack) - 1, -1, -1):
                top = self.stack[k][0]
                if top == "a":
                    del self.stack[k]
                    return
                if isinstance(top, tuple) and top[0] == "skip_a":
                    del self.stack[k]
                    return
            return

        if tag in ("strong", "b"):
            self._close_inline("strong", "**")
            return
        if tag in ("em", "i"):
            self._close_inline("em", "_")
            return
        if tag == "code":
            self._close_inline("code", "`")
            return
        if tag in ("sub", "sup", "font", "span", "label"):
            self._pop(tag)
            return

        if tag in FLUSH_TAGS:
            self._flush()
            self._pop(tag)
            return
        self._pop(tag)

    def handle_data(self, data):
        if self.skip:
            return
        if self._in_article <= 0:
            return
        for k in range(len(self.stack) - 1, -1, -1):
            if isinstance(self.stack[k][0], tuple) and self.stack[k][0][0] == "skip_a":
                return
            if self.stack[k][0] == "skip_a":
                return
        self._text(data)

    def set_base(self, base):
        self._base = base

    def result(self):
        self._end_para()
        text = "\n\n".join(self.out)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"^\s*Наверх\s*$\n?", "", text, flags=re.M)
        text = re.sub(r"[\ue000-\uf8ff\ufff0-\uffff\u200b\ufeff]", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip() + "\n"


def html_cmd(args):
    infile, outfile = args[0], args[1]
    img_root = None
    base = None
    if "--img-root" in args:
        img_root = args[args.index("--img-root") + 1]
    if "--base" in args:
        base = args[args.index("--base") + 1]

    with open(infile, encoding="utf-8", errors="replace") as f:
        src = f.read()

    m = re.search(r"<title>\s*(.*?)\s*</title>", src, re.S)
    title = htmlmod.unescape(m.group(1)).strip() if m else ""

    p = MdParser(img_root)
    p.set_base(base or ("file://" + infile))
    p.feed(src)
    p.close()

    body = p.result()

    with open(outfile + ".imgs", "w", encoding="utf-8") as f:
        for url, cls in p.images:
            f.write(url + "\n")

    with open(outfile, "w", encoding="utf-8") as f:
        f.write(body)


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "toc":
        toc_cmd(sys.argv[2:])
    elif cmd == "html":
        html_cmd(sys.argv[2:])
    else:
        print(__doc__, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
