# -*- coding: utf-8 -*-
"""Convert a PDF into structured Markdown for LLM / agent consumption.

What it does:
  * drops repeated running headers / footers and page numbers
  * rebuilds heading levels from numbering (1, 1.2, 1.2.3 ...)
  * keeps tables as Markdown tables (bordered ones via find_tables, borderless
    ones rebuilt from text coordinates)
  * detects two-column pages (table of contents, legal boilerplate) and emits
    the left column before the right column
  * inserts `<!-- page N -->` anchors so answers can cite the source page
  * prints a per-page coverage check (flags pages that lost too much text)

Usage:
    python to_markdown.py clean.pdf            # -> clean.md
    python to_markdown.py clean.pdf -o out.md --no-page-markers

No absolute paths or machine-specific values are stored in this file.
"""
import argparse
import html
import os
import re
import sys
from collections import Counter

import fitz

LIGATURES = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi",
             "\ufb04": "ffl", "\u00ac": " ", "\u00a0": " "}


def norm(t):
    for k, v in LIGATURES.items():
        t = t.replace(k, v)
    return t


def rows_of(page):
    out = []
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for ln in b["lines"]:
            spans = [s for s in ln["spans"] if s["text"].strip()]
            if not spans:
                continue
            text = "".join(s["text"] for s in spans).strip()
            if not text:
                continue
            out.append({"text": norm(text),
                        "x0": ln["bbox"][0], "y0": ln["bbox"][1],
                        "x1": ln["bbox"][2], "y1": ln["bbox"][3],
                        "size": max(s["size"] for s in spans),
                        "bold": any(s["flags"] & 16 for s in spans),
                        "color": spans[0]["color"]})
    out.sort(key=lambda r: (round(r["y0"], 1), r["x0"]))
    return out


def join_frags(frags):
    frags = sorted(frags, key=lambda r: r["x0"])
    txt = frags[0]["text"]
    for prev, cur in zip(frags, frags[1:]):
        if txt.endswith("-") and cur["text"][:1].islower():
            txt = txt[:-1] + cur["text"]
        elif cur["x0"] - prev["x1"] > 1.5:
            txt += " " + cur["text"]
        else:
            txt += cur["text"]
    return txt


def merge_same_row(rows):
    merged, bucket = [], []
    for r in rows:
        if bucket and abs(r["y0"] - bucket[0]["y0"]) < 3.5:
            bucket.append(r)
        else:
            if bucket:
                merged.append(_collapse(bucket))
            bucket = [r]
    if bucket:
        merged.append(_collapse(bucket))
    return merged


def _collapse(bucket):
    base = dict(bucket[0])
    base["text"] = join_frags(bucket)
    base["x0"] = min(b["x0"] for b in bucket)
    base["x1"] = max(b["x1"] for b in bucket)
    base["y1"] = max(b["y1"] for b in bucket)
    base["size"] = max(b["size"] for b in bucket)
    base["bold"] = any(b["bold"] for b in bucket)
    return base


def norm_key(t):
    t = re.sub(r"\s+", " ", t).strip()
    return re.sub(r"\d", "#", t)


def cluster(vals, tol=7.0):
    groups = []
    for v in sorted(vals):
        if groups and v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [sum(g) / len(g) for g in groups]


def cell_text(t):
    t = (t or "").replace("&amp;#45;", "-").replace("&#45;", "-")
    t = html.unescape(t).replace("\n", "<br>")
    t = re.sub(r"\s+", " ", t).strip()
    return re.sub(r"^Col\d+$", "", t)


def md_table(rows):
    rows = [[c.replace("|", "\\|") for c in r] for r in rows]
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["|" + "|".join(rows[0]) + "|", "|" + "|".join(["---"] * width) + "|"]
    out += ["|" + "|".join(r) + "|" for r in rows[1:]]
    return "\n".join(out)


def extend_table(rows, header_bbox, header_cells, bottom_limit):
    """Rebuild rows for a table whose drawn frame covers only the header row."""
    body, prev = [], None
    for r in rows:
        if r["y1"] <= header_bbox[3] + 1 or r["y0"] < header_bbox[1] - 1:
            continue
        if r["y0"] > bottom_limit:
            break
        if r["x0"] > header_bbox[2] + 6:
            continue
        if r["size"] >= 9.7 and r["x0"] < 60:
            break
        if r["bold"] and re.match(r"^(Tab\.|Table)", r["text"]):
            break
        if prev is not None and (r["y0"] - prev["y1"]) > 34:
            break
        body.append(r)
        prev = r
    if not body:
        return None, header_bbox[3]

    cols = cluster([c[0] for c in header_cells if c])
    if len(cols) < 2:
        cols = cluster([r["x0"] for r in body])
    else:
        for c in cluster([r["x0"] for r in body]):
            if all(abs(c - h) > 26 for h in cols):
                if sum(1 for r in body if abs(r["x0"] - c) < 8) >= 3:
                    cols.append(c)
        cols.sort()

    lines, bucket = [], []
    for r in body:
        if bucket and abs(r["y0"] - bucket[0]["y0"]) < 6:
            bucket.append(r)
        else:
            if bucket:
                lines.append(bucket)
            bucket = [r]
    if bucket:
        lines.append(bucket)

    table = []
    for ln in lines:
        cells = [""] * len(cols)
        for r in ln:
            idx = min(range(len(cols)), key=lambda i: abs(cols[i] - r["x0"]))
            cells[idx] = (cells[idx] + " " + r["text"]).strip() if cells[idx] else r["text"]
        table.append(cells)
    return table, body[-1]["y1"]


def convert(src, dst, top_ratio, bottom_ratio, code_size, use_tables, page_markers, cover_page):
    doc = fitz.open(src)
    n = doc.page_count
    raw_rows = [rows_of(p) for p in doc]
    all_rows = [merge_same_row(r) for r in raw_rows]
    heights = [p.rect.height for p in doc]
    widths = [p.rect.width for p in doc]

    # repeated running header / footer detection
    counter = Counter()
    for rows, h in zip(all_rows, heights):
        top, bottom = h * top_ratio, h * bottom_ratio
        seen = set()
        for r in rows:
            if r["y1"] < top or r["y0"] > bottom:
                k = norm_key(r["text"])
                if k and k not in seen:
                    seen.add(k)
                    counter[k] += 1
    drop = {k for k, c in counter.items() if c >= max(8, int(0.08 * n))}

    title = (doc.metadata or {}).get("title") or ""
    if not title and doc.page_count:
        spans = [s for b in doc[0].get_text("dict")["blocks"] if b["type"] == 0
                 for ln in b["lines"] for s in ln["spans"]]
        big = [s["text"].strip() for s in spans if s["size"] >= 18 and s["text"].strip()]
        title = " ".join(big[:2])
    if not title:
        title = os.path.splitext(os.path.basename(src))[0]

    md = ["# " + title, "", "> Converted from PDF by to_markdown.py.", ""]

    low_cov = []
    for pno, page in enumerate(doc, start=1):
        h, w = heights[pno - 1], widths[pno - 1]
        top, bottom = h * top_ratio, h * bottom_ratio
        rows = all_rows[pno - 1]
        items, used = [], []

        if use_tables:
            for t in page.find_tables().tables:
                tb = t.bbox
                hdr = [cell_text(c) for c in (t.extract()[0] if t.row_count else [])]
                if t.row_count <= 1:
                    ext, last_y = extend_table(raw_rows[pno - 1], tb,
                                               [c for c in t.rows[0].cells if c], bottom)
                    if ext:
                        items.append((tb[1], "md", md_table([hdr] + ext)))
                        used.append((tb[0], tb[1], tb[2], last_y))
                        continue
                items.append((tb[1], "md", md_table([[cell_text(c) for c in row] for row in t.extract()])))
                used.append((tb[0], tb[1], tb[2], tb[3]))

        def in_table(r):
            cx, cy = (r["x0"] + r["x1"]) / 2, (r["y0"] + r["y1"]) / 2
            return any(b[0] - 2 <= cx <= b[2] + 2 and b[1] - 2 <= cy <= b[3] + 2 for b in used)

        body = []
        for r in rows:
            if in_table(r) or r["y0"] > bottom:
                continue
            is_cover = cover_page and pno == 1
            if r["y1"] < top and not is_cover:
                continue
            if (is_cover or r["y1"] >= top) and norm_key(r["text"]) in drop:
                continue
            body.append(r)

        buckets = {}
        for r in raw_rows[pno - 1]:
            if in_table(r) or r["y1"] < top or r["y0"] > bottom:
                continue
            buckets.setdefault(round(r["y0"] / 5), []).append(r)
        split_rows = sum(1 for v in buckets.values()
                         if any(x["x1"] < w * 0.497 for x in v) and any(x["x0"] > w * 0.497 for x in v))
        two_col = split_rows >= 8

        def emit(chunk, code_ok=True, para_merge=True, headings_ok=True):
            i = 0
            while i < len(chunk):
                r = chunk[i]
                text = r["text"].strip()
                if not text:
                    i += 1
                    continue
                if headings_ok and r["bold"] and re.match(r"^(\d+(?:\.\d+)*)\s+\S", text) and len(text) < 130:
                    level = text.split(" ")[0].count(".") + 1
                    items.append((r["y0"], "md", "#" * min(level, 6) + " " + text))
                    i += 1
                    continue
                if headings_ok and r["bold"] and r["size"] >= 13.5 and len(text) < 130:
                    items.append((r["y0"], "md", "## " + text))
                    i += 1
                    continue
                if headings_ok and r["bold"] and r["size"] >= 11.5 and len(text) < 130:
                    items.append((r["y0"], "md", "### " + text))
                    i += 1
                    continue
                if r["bold"] and re.match(r"^(Tab\.|Table|Figure|Fig\.)", text):
                    items.append((r["y0"], "md", "**" + text + "**"))
                    i += 1
                    continue
                if code_ok and r["size"] <= code_size:
                    code = [text]
                    j = i + 1
                    while j < len(chunk) and chunk[j]["size"] <= code_size and (chunk[j]["y0"] - chunk[j - 1]["y1"]) < 12:
                        code.append(chunk[j]["text"].strip())
                        j += 1
                    items.append((r["y0"], "md", "```\n" + "\n".join(code) + "\n```"))
                    i = j
                    continue
                if text[0] in "•-–—" and text[1:2] == " ":
                    marker = "  - " if text[0] in "-–—" else "- "
                    item = marker + text[2:].strip()
                    j = i + 1
                    while (j < len(chunk) and chunk[j]["size"] == r["size"]
                           and chunk[j]["x0"] >= r["x0"] + 4 and chunk[j]["text"][0] not in "•–—"
                           and (chunk[j]["y0"] - chunk[j - 1]["y1"]) < 8):
                        item += " " + chunk[j]["text"].strip()
                        j += 1
                    items.append((r["y0"], "md", item))
                    i = j
                    continue

                para = [text]
                j = i + 1
                while (para_merge and j < len(chunk) and chunk[j]["size"] == r["size"]
                       and chunk[j]["bold"] == r["bold"] and abs(chunk[j]["x0"] - r["x0"]) < 6
                       and not (chunk[j]["bold"] and re.match(r"^\d+(\.\d+)*\s+\S", chunk[j]["text"]))
                       and chunk[j]["text"][0] not in "•–—"
                       and (chunk[j]["y0"] - chunk[j - 1]["y1"]) < r["size"] * 0.8):
                    prev_t, nxt = para[-1], chunk[j]["text"].strip()
                    if prev_t.endswith("-") and nxt[:1].islower():
                        para[-1] = prev_t[:-1] + nxt
                    else:
                        para.append(nxt)
                    j += 1
                items.append((r["y0"], "md", " ".join(para)))
                i = j

        if two_col:
            raw_body = [r for r in raw_rows[pno - 1]
                        if not in_table(r) and r["y0"] <= bottom and r["y1"] >= top
                        and norm_key(r["text"]) not in drop]
            emit(merge_same_row([r for r in raw_body if (r["x0"] + r["x1"]) / 2 < w * 0.5]),
                 code_ok=False, para_merge=False, headings_ok=False)
            mark = len(items)
            emit(merge_same_row([r for r in raw_body if (r["x0"] + r["x1"]) / 2 >= w * 0.5]),
                 code_ok=False, para_merge=False, headings_ok=False)
            items[mark:] = [(y + 10000, k, p) for y, k, p in items[mark:]]
        else:
            emit(body)

        items.sort(key=lambda x: x[0])
        if page_markers:
            md.append("<!-- page %d -->" % pno)
            md.append("")
        for _, _, payload in items:
            md.append(payload)
            md.append("")

        body_chars = len(re.sub(r"\s+", "", "".join(r["text"] for r in body)))
        out_chars = len(re.sub(r"\s+", "", "".join(p for _, _, p in items)))
        if body_chars > 200 and out_chars < body_chars * 0.55:
            low_cov.append((pno, body_chars, out_chars))

    out = "\n".join(md)
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = re.sub(r"[ \t]+\n", "\n", out)
    with open(dst, "w", encoding="utf-8") as f:
        f.write(out)
    print("wrote:", dst, "|", len(out), "chars")
    if low_cov:
        print("WARNING low text coverage (review these pages):", low_cov[:10])
    else:
        print("coverage check: no page lost more than 45% of its text")
    return 0


def main():
    ap = argparse.ArgumentParser(description="PDF -> structured Markdown.")
    ap.add_argument("pdf")
    ap.add_argument("-o", "--out")
    ap.add_argument("--top-ratio", type=float, default=0.15, help="running-header band (fraction of page height)")
    ap.add_argument("--bottom-ratio", type=float, default=0.93, help="running-footer band (fraction of page height)")
    ap.add_argument("--code-size", type=float, default=8.6)
    ap.add_argument("--no-tables", action="store_true")
    ap.add_argument("--no-page-markers", action="store_true")
    ap.add_argument("--no-cover", action="store_true", help="apply header filtering to page 1 as well")
    args = ap.parse_args()

    src = os.path.abspath(args.pdf)
    dst = os.path.abspath(args.out) if args.out else os.path.splitext(src)[0] + ".md"
    return convert(src, dst, args.top_ratio, args.bottom_ratio, args.code_size,
                   not args.no_tables, not args.no_page_markers, not args.no_cover)


if __name__ == "__main__":
    sys.exit(main())
