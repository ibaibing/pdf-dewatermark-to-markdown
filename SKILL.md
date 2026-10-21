---
name: "pdf-dewatermark-to-markdown"
description: "Remove watermarks from PDF files and convert them into structured Markdown for LLM/Agent reading or RAG ingestion. Handles form-layer watermarks (NDA personal copies, company-confidential stamps, UUID tracking codes), annotation/stamp watermarks, and repeated headers/footers. Use when the user asks to remove a watermark from a PDF, clean a PDF, strip confidential markings, convert a PDF to Markdown, feed a PDF to an AI, or mentions 'dewatermark', 'pdf to markdown for LLM'. Requires PyMuPDF (pip install pymupdf)."
---

# PDF Watermark Removal and Markdown Conversion

Turn a watermarked PDF into two deliverables: `*_clean.pdf` (no watermark in either the visual layer or the text layer) and `*_clean.md` (structured text suitable for LLMs/RAG). The core principle is **diagnose the watermark carrier first, then remove exactly that layer** — never use image inpainting or white-box covering.

## When to use / when not to

Use for:
- Diagonal text watermarks repeated on every page (NDA, Confidential, Personal copy, email, UUID)
- Hidden tracking headers (invisible text, UUID) that pollute `get_text()` output
- Annotation- or stamp-based watermarks
- Ordinary electronic PDFs with an extractable text layer (i.e. not scans)

Do not use for:
- Watermarks baked into scanned page images (pixel level) — use image inpainting or OCR instead
- Encrypted / DRM-protected PDFs you have no permission to re-save

## Environment

```bash
pip install pymupdf
```

`pdfplumber` is optional and only useful for manual comparison; this workflow does not need it.

## Workflow

### Step 1: Diagnose (mandatory, do not skip)

```bash
python scripts/dewatermark.py --dry-run input.pdf
```

The script lists Form XObjects referenced by every page, their coverage, and whether they hit watermark keywords (confidential / personal copy / under nda / do not copy…) or a UUID. If nothing is reported, the watermark is not a form layer — go to step 6.

Also confirm whether the watermark is visible in the text layer:

```bash
python -c "import fitz;d=fitz.open('input.pdf');print(d[0].get_text()[:800])"
```

### Step 2: Remove the watermark

```bash
python scripts/dewatermark.py input.pdf                # writes input_clean.pdf
python scripts/dewatermark.py input.pdf -o out.pdf --keywords "internal use,do not circulate"
```

How it works: the watermark Form XObject stream is replaced with `q Q` and its `/OC` binding is detached. This clears both the visual and the text layer while leaving body text, tables, and images untouched.

Only use `--forms 123,456` to force specific xrefs (visible in the diagnostic output) when you have confirmed them by hand.

### Step 3: Verify (automatic check + one visual pass)

The script prints:
- `residual watermark text on pages` — must be `none`
- `text check` — must report that only watermark lines were removed; any `WARNING` means stop and investigate

Then render 2–3 pages for a visual comparison (cover, a table page, a body page):

```bash
python -c "import fitz;d=fitz.open('input_clean.pdf');[d[i].get_pixmap(dpi=110).save(f'p{i+1}.png') for i in (0,4,40)]"
```

Confirm with an image viewer: watermark gone, headers/footers, table rules, images, and logos intact.

### Step 4: Convert to Markdown

```bash
python scripts/to_markdown.py input_clean.pdf            # writes input_clean.md
```

It handles heading levels (reconstructed from `1 / 1.2 / 1.2.3` numbering), lists, code blocks, tables, header/footer filtering, two-column splitting, and `<!-- page N -->` page anchors.

Tunable flags: `--top-ratio` / `--bottom-ratio` (header/footer bands, default 15% / 93% of page height), `--code-size` (monospace/small-font threshold), `--no-tables`, `--no-page-markers`, `--no-cover` (treat page 1 as a normal page).

### Step 5: Verify

The script prints per-page text coverage and warns for any page below 55%. On a warning, open that page in the PDF and compare manually — usually a misdetected table or an unsplit two-column layout.

Spot-check: cover, table of contents, a page with tables, a chapter opening page, and the final legal page.

### Step 6: Handling non-form watermarks

| Carrier | How to tell | What to do |
|---|---|---|
| Text inside the page content stream | `--dry-run` reports nothing but `get_text()` contains the watermark | Do not hand-delete `BT…ET` blocks (fonts are CID-encoded). Compare the text layer, then apply `add_redact_annot` by coordinates, confirming region by region that no body text is covered |
| Annotations / stamps | `page.annots()` contains Stamp/FreeText | The script deletes keyword matches by default; disable with `--keep-annotations` |
| Image watermark | `get_images()` hits, text layer is clean | Switch to the image-inpainting workflow; do not force it in this skill |
| OCG layer | A dedicated layer exists in `/OCProperties` | Remove the layer, or empty the XObjects drawn on it |

## Lessons learned (pitfalls)

1. **Never cover with a white rectangle**: the watermark sits above the body text, so covering eats the content, and the original text is still extractable — that is fake removal.
2. **Never redact a full page**: a diagonal watermark's bounding box spans the whole page and would delete large amounts of body text.
3. **Emptying the form is the safest option**: it only affects that XObject's references, leaves content streams byte-for-byte intact, and makes the text diff exactly verifiable.
4. **Remove hidden UUID headers too**: they are invisible but show up in `get_text()` and directly pollute RAG results.
5. **Filter headers/footers by frequency, not fixed coordinates**: one document may use several templates; "appears on ≥ 8% of pages" is a more robust repetition test.
6. **Rebuild borderless tables**: `find_tables()` relies on ruling lines and returns only the header for tables that use shading to separate rows. Rebuild them from the header column x-coordinates plus clustering of the text rows below.
7. **Sort two-column pages per column**: tables of contents and legal pages are two-column; a global sort by y interleaves the columns. Split by x first, then emit each column sorted by y.
8. **Hyphenation and ligatures**: join `Adminis-` + `trator`; convert `ﬁ/ﬂ` ligatures back to `fi/fl`.

## Output conventions

- `<original-name>_clean.pdf`
- `<original-name>_clean.md`
- The source file is never modified; the converters never write any path, hostname, or username into the output.

## Verification checklist

- [ ] Carrier count and covered page count from `--dry-run` match expectations
- [ ] Residual scan after removal reports none
- [ ] No `WARNING: text outside the watermark layer changed`
- [ ] 3 rendered pages show no watermark and an intact layout
- [ ] Markdown coverage produces no warnings
- [ ] Spot-checked tables / two-column pages / code blocks are formatted correctly
- [ ] Deliverables contain no personal watermark text (name, email, UUID)
