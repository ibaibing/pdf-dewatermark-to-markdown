# -*- coding: utf-8 -*-
"""Remove overlay watermarks from a PDF without touching page content.

Primary strategy: watermark layers (NDA "personal copy" stamps, tracking IDs,
company-confidential strips) are usually Form XObjects referenced from every
page, often wrapped in an optional-content group. Emptying those forms removes
the watermark from the visual layer *and* from the text layer, while keeping
body text, tables and images byte-for-byte intact.

Usage:
    python dewatermark.py --dry-run input.pdf        # diagnose carriers only
    python dewatermark.py input.pdf                  # -> input_clean.pdf
    python dewatermark.py input.pdf -o out.pdf --keywords "internal use,do not circulate"

No absolute paths or machine-specific values are stored in this file.
"""
import argparse
import difflib
import os
import re
import sys

import fitz

DEFAULT_KEYWORDS = [
    "confidential", "do not copy", "do not reproduce", "do not distribute",
    "personal copy", "personalised", "personalized", "under nda", "nda only",
    "watermark", "not for distribution", "for internal use", "copy for",
]
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
# phrases distinctive enough to be treated as leftover watermark text
RESIDUAL_PATTERNS = [r"provided under nda", r"personal copy", r"do not copy",
                     r"do not reproduce", r"do not distribute", r"under nda",
                     r"for internal use"]
RESIDUAL_RE = re.compile("|".join(RESIDUAL_PATTERNS + [UUID_RE.pattern]), re.I)
TEXT_OP_RE = re.compile(r"(?:^|\s)(BT|ET|Tj|TJ|Td|TD|Tm)(?:\s|$)")


def form_payload(doc, xref):
    """Decoded stream text of a Form XObject (latin-1 fallback)."""
    try:
        raw = doc.xref_stream(xref)
    except Exception:
        return ""
    if not raw:
        return ""
    return raw.decode("latin-1", "ignore")


def literal_strings(stream):
    return " ".join(re.findall(r"\(((?:\\.|[^()\\])*)\)", stream))


def scan_carriers(doc, keywords, forced, raw_bytes):
    """Find form xobjects whose removal actually makes watermark text disappear.

    Watermark text is often CID-encoded, so keyword-matching the raw stream is
    unreliable. Instead: locate pages whose *extracted text* matches watermark
    patterns, then try emptying each repeated text-bearing form in a scratch
    copy and see whether those matches vanish.
    """
    uses, names = {}, {}
    for i, page in enumerate(doc):
        for item in page.get_xobjects():
            xref = item[0]
            uses.setdefault(xref, set()).add(i)
            names.setdefault(xref, item[1] if len(item) > 1 else "")

    kw_pat = re.compile("|".join(re.escape(k) for k in keywords), re.I)
    pages_with_text = set()
    for i in range(doc.page_count):
        t = doc[i].get_text()
        if kw_pat.search(t) or UUID_RE.search(t):
            pages_with_text.add(i)
    if not pages_with_text:
        return []

    def metrics(scratch, pages):
        kw = uuid = 0
        for i in pages:
            t = scratch[i].get_text()
            kw += len(kw_pat.findall(t))
            uuid += len(UUID_RE.findall(t))
        return kw, uuid

    n = doc.page_count
    carriers = []
    ordered = sorted(uses.items(), key=lambda kv: -len(kv[1]))
    tested = 0
    for xref, pages in ordered:
        stream = form_payload(doc, xref)
        if not stream or not TEXT_OP_RE.search(stream):
            continue
        coverage = len(pages) / float(n)
        forced_hit = xref in forced
        if not forced_hit and len(pages) < 2:
            continue
        if not forced_hit:
            if tested >= 40:
                continue
            tested += 1

        # compare watermark-pattern counts on the pages where this form appears
        local = [i for i in sorted(pages) if i in pages_with_text][:10]
        if not forced_hit and not local:
            continue
        if forced_hit:
            hits, uuid_hit = ["forced"], False
        else:
            kw0, uuid0 = metrics(doc, local)
            scratch = fitz.open("pdf", raw_bytes)
            scratch.update_stream(xref, b"q Q", compress=True)
            kw1, uuid1 = metrics(scratch, local)
            scratch.close()
            d_kw, d_uuid = kw0 - kw1, uuid0 - uuid1
            if d_kw < 1 and d_uuid < 1:
                continue
            hits = ["keyword-text"] if d_kw else []
            uuid_hit = d_uuid > 0
        carriers.append({
            "xref": xref,
            "name": names.get(xref, ""),
            "pages": len(pages),
            "coverage": coverage,
            "hits": hits,
            "uuid": uuid_hit,
            "forced": forced_hit,
        })
    return carriers


def remove_annotations(doc, keywords, uuid_re):
    removed = 0
    for page in doc:
        for annot in list(page.annots() or []):
            info = annot.info or {}
            blob = " ".join(str(info.get(k, "")) for k in ("content", "title", "subject")).lower()
            if any(k in blob for k in keywords) or uuid_re.search(blob):
                page.delete_annot(annot)
                removed += 1
    return removed


def main():
    ap = argparse.ArgumentParser(description="Remove PDF overlay watermarks (form-layer / annotation).")
    ap.add_argument("pdf")
    ap.add_argument("-o", "--out", help="output PDF (default: <input>_clean.pdf)")
    ap.add_argument("--dry-run", action="store_true", help="only report detected watermark carriers")
    ap.add_argument("--keywords", default="", help="extra comma separated keywords")
    ap.add_argument("--forms", default="", help="force removal of these xrefs (comma separated)")
    ap.add_argument("--keep-oc", action="store_true", help="do not detach the optional-content tag")
    ap.add_argument("--keep-annotations", action="store_true", help="do not remove matching annotations")
    args = ap.parse_args()

    keywords = list(DEFAULT_KEYWORDS)
    keywords += [k.strip().lower() for k in args.keywords.split(",") if k.strip()]
    forced = {int(x) for x in args.forms.replace(" ", "").split(",") if x}

    src = os.path.abspath(args.pdf)
    dst = os.path.abspath(args.out) if args.out else os.path.splitext(src)[0] + "_clean.pdf"

    doc = fitz.open(src)
    raw_bytes = open(src, "rb").read()

    carriers = scan_carriers(doc, keywords, forced, raw_bytes)
    print("pages: %d | form carriers found: %d" % (doc.page_count, len(carriers)))
    for c in carriers:
        tag = ",".join(c["hits"]) if c["hits"] else ""
        if c["uuid"]:
            tag = (tag + ",uuid").lstrip(",")
        print("  xref %-6d /%-8s pages %-4d coverage %5.1f%%  %s" % (
            c["xref"], c["name"], c["pages"], c["coverage"] * 100, tag or "forced"))

    if args.dry_run:
        print("dry run: nothing written")
        return 0
    if not carriers:
        print("no form-layer watermark detected; check text-layer or image watermarks manually")
        return 2

    for c in carriers:
        doc.update_stream(c["xref"], b"q Q", compress=True)
        if not args.keep_oc:
            doc.xref_set_key(c["xref"], "OC", "null")

    n_annot = 0
    if not args.keep_annotations:
        n_annot = remove_annotations(doc, keywords, UUID_RE)

    doc.save(dst, garbage=4, deflate=True)
    doc.close()
    print("saved:", dst, "| annotations removed:", n_annot)

    # ---- verification ----
    # 1) expected text = a scratch copy in which only the carriers were emptied;
    #    the saved file must match it exactly (guards against collateral loss)
    scratch = fitz.open("pdf", raw_bytes)
    for c in carriers:
        scratch.update_stream(c["xref"], b"q Q", compress=True)
    expected = [p.get_text() for p in scratch]
    scratch.close()

    doc = fitz.open(dst)
    residual = [i + 1 for i, p in enumerate(doc) if RESIDUAL_RE.search(p.get_text())]
    if residual:
        print("residual watermark text on %d pages: %s%s" % (
            len(residual), residual[:5], " ..." if len(residual) > 5 else ""))
    else:
        print("residual watermark text: none")

    unexpected = []
    for i, page in enumerate(doc):
        for line in difflib.unified_diff(expected[i].splitlines(), page.get_text().splitlines(),
                                         lineterm="", n=0):
            if line.startswith("-") and not line.startswith("---"):
                body = line[1:].strip()
                if body:
                    unexpected.append((i + 1, body[:80]))
    if unexpected:
        print("WARNING: text outside the watermark layer changed (review the result):")
        for pg, txt in unexpected[:10]:
            print("  p%d: %r" % (pg, txt))
    else:
        print("text check: only the watermark layer was removed")

    return 1 if (residual or unexpected) else 0


if __name__ == "__main__":
    sys.exit(main())
