# pdf-dewatermark-to-markdown

Remove watermarks from PDFs and turn them into structured Markdown for LLMs, agents, and RAG pipelines.

The watermark is removed at the PDF object level: the Form XObject that draws it is emptied and detached from its optional-content group. Body text, tables, images, and logos stay untouched, and both the visual layer and the extracted text layer are cleaned — no redaction boxes, no white rectangles, no image inpainting.

## Why not redaction or white boxes

- **White rectangles** hide the watermark visually but leave the text extractable — fake removal.
- **Full-page redaction** deletes body text, because a diagonal watermark's bounding box spans the whole page.
- **Inpainting** is only for watermarks baked into scanned page images.

Emptying the drawing form only affects that form's references. Content streams stay byte-for-byte intact, so the result is exactly verifiable.

## What it handles

- Diagonal text watermarks repeated on every page (NDA, Confidential, Personal copy, email)
- Hidden tracking headers (invisible text, UUID) that pollute `get_text()` output
- Annotation and stamp watermarks
- Repeated headers and footers during Markdown conversion

Not for: watermarks baked into scanned images, encrypted or DRM-protected files.

## Requirements

Python 3.8+ and PyMuPDF:

```bash
pip install pymupdf
```

## Usage

```bash
# 1. Diagnose: list form-layer carriers, page coverage, and keyword/UUID hits
python scripts/dewatermark.py --dry-run input.pdf

# 2. Remove: writes input_clean.pdf, then self-verifies
python scripts/dewatermark.py input.pdf

# 3. Convert: writes input_clean.md
python scripts/to_markdown.py input_clean.pdf
```

The removal step prints two checks that must both pass:

```
residual watermark text: none
text check: only the watermark layer was removed
```

Any `WARNING: text outside the watermark layer changed` means the diff contains unexpected deletions — stop and inspect.

### Options

`dewatermark.py`

| Flag | Purpose |
|---|---|
| `-o, --out` | Output path (default `<input>_clean.pdf`) |
| `--dry-run` | Report carriers only, write nothing |
| `--keywords` | Extra comma-separated watermark keywords |
| `--forms` | Force removal of specific xrefs |
| `--keep-oc` | Keep the optional-content tag |
| `--keep-annotations` | Do not delete matching annotations |

Exit codes: `0` success, `1` residual text or unexpected changes, `2` no form-layer watermark found.

`to_markdown.py`

| Flag | Purpose |
|---|---|
| `-o, --out` | Output path (default `<input>.md`) |
| `--top-ratio` / `--bottom-ratio` | Header/footer bands as a fraction of page height (default 0.15 / 0.93) |
| `--code-size` | Font-size threshold for monospace/code blocks |
| `--no-tables` | Disable table detection |
| `--no-page-markers` | Omit `<!-- page N -->` anchors |
| `--no-cover` | Treat page 1 as a normal page |

Markdown output includes heading levels rebuilt from `1 / 1.2 / 1.2.3` numbering, lists, code blocks, tables (including borderless tables rebuilt from header column coordinates), two-column layout splitting, filtered headers/footers, and page anchors.

## Verification

The conversion prints per-page text coverage and warns below 55%. On a warning, compare that page in the PDF by hand — usually a misdetected table or an unsplit two-column layout.

Recommended spot checks: cover, table of contents, a table page, a chapter opening, and the final legal page. Render a few pages to confirm visually:

```bash
python -c "import fitz;d=fitz.open('input_clean.pdf');[d[i].get_pixmap(dpi=110).save(f'p{i+1}.png') for i in (0,4,40)]"
```

## Using it as an agent skill

`SKILL.md` is a self-contained agent skill: it describes when to use the workflow, the step order, the non-form watermark fallbacks, and the pitfalls. Copy this directory into your agent's skills folder (`~/.claude/skills/`, or your client's equivalent) and it will be picked up from the frontmatter description.

## Scope and responsibility

Only use this on documents you own or are authorized to modify. The scripts never modify the input file and never write machine paths, hostnames, or usernames into the output.

## License

MIT
