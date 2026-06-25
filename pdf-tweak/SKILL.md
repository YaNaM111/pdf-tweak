---
name: pdf-tweak
description: >
  Make small, surgical text edits to existing PDFs — fix a typo, replace a
  word, append a note. Works by patching the PDF content stream directly.
  NOT for creating PDFs, merging/splitting, or extracting text — use the pdf
  skill instead.
---

# PDF Tweak

## Workflow

### 1. Map the document

Find every occurrence of the target text with page number, position, and font:

```python
import pdfplumber
with pdfplumber.open('doc.pdf') as pdf:
    for page_num, page in enumerate(pdf.pages):
        for w in page.extract_words():
            if 'target_text' in w['text']:
                print(f"page={page_num} x0={w['x0']:.0f} top={w['top']:.0f} "
                      f"x1={w['x1']:.0f} bottom={w['bottom']:.0f} "
                      f"text='{w['text']}'")
        # Get font reference from the chars on this page
        for c in page.chars:
            if 'target_text_char' in c['text'] and c['top'] > TARGET_TOP:
                print(f"  font={c.get('fontname','?')}")
```

On Windows, add `sys.stdout.reconfigure(encoding='utf-8')` before printing to
avoid `UnicodeEncodeError` on non-ASCII text. Prefer `python` over `python3` —
the latter may be a Microsoft Store stub on Windows.

Replace `target_text` with the text to find, and `TARGET_TOP` with the
approximate top coordinate from the word output above.

**Done when**: you know, for every occurrence: the page number, bounding box
(`x0,top,x1,bottom`), and the base font name (e.g. `TSPJMR` from
`TSPJMR+Noto-Sans-CJK-SC`).

### 2. Check the font

First, check the target text's own font:

```bash
python .claude/skills/pdf-tweak/scripts/font_map.py doc.pdf PAGE FONTNAME --chars "new text"
```

If any character shows `NOT IN FONT`, **immediately** scan all fonts on the page
to find one that covers every needed character:

```bash
python .claude/skills/pdf-tweak/scripts/font_map.py doc.pdf PAGE --scan-all --chars "new text"
```

This prints a coverage table ranked from best to worst, so you can pick the
right font in one shot rather than guessing.

**Done when**: you have a font name that covers every character in the
replacement/insertion text. If `--scan-all` shows no font has full coverage:
- Try full-width variants of the missing characters (e.g. `（` / `）` for `(` / `)`)
- If still nothing, warn the user — the font subsets embedded in this PDF
  don't include the needed glyphs, and this approach won't work.

### 3. Locate in the content stream

```bash
python .claude/skills/pdf-tweak/scripts/edit_pdf.py doc.pdf PAGE --find "text to find"
```

Returns a JSON array. Each match has: `font`, `cids`, `byte_start`, `byte_end`,
`approx_x`, `approx_y`, `font_size`.

**Done when**: you've picked the correct match. If multiple matches exist, use
the `approx_x` and `approx_y` fields to identify the right one by comparing
with the bounding box from Step 1 (right-column text has `approx_x` > 300 in
typical two-column layouts). Then note its array index (`[0]`, `[1]`, etc.).

### 4a. Replace or delete (same-font)

Always `--dry-run` first, always back up before the real run:

```bash
# Preview
python .claude/skills/pdf-tweak/scripts/edit_pdf.py doc.pdf PAGE \
  --replace "old text" --with "new text" --match-index N --dry-run

# Apply
cp doc.pdf doc_backup.pdf
python .claude/skills/pdf-tweak/scripts/edit_pdf.py doc.pdf PAGE \
  --replace "old text" --with "new text" --match-index N

# Delete variant
python .claude/skills/pdf-tweak/scripts/edit_pdf.py doc.pdf PAGE \
  --delete "text to delete" --match-index N
```

**Done when**: the command prints `[OK] replaced ... on page N` (or `deleted`).

### 4b. Insert after (cross-font append)

When you need to **add** text after existing text — especially when the new
characters live in a different font than the target — use `--insert-after`:

```bash
# Preview
python .claude/skills/pdf-tweak/scripts/edit_pdf.py doc.pdf PAGE \
  --insert-after "target text" --insert-text "inserted text" --font FONTNAME --dry-run

# Apply
python .claude/skills/pdf-tweak/scripts/edit_pdf.py doc.pdf PAGE \
  --insert-after "target text" --insert-text "inserted text" --font FONTNAME
```

- `--font` is optional; when omitted, the inserted text uses the same font as
  the match (and fails if that font lacks the needed characters).
- The tool automatically splits the TJ array, inserts font-switch commands,
  and switches back to the original font for any text that follows.
- The inserted text gets the same font size as the target.

**Done when**: the command prints `[OK] insert-afterd ...`.

### 5. Verify

Re-extract and check only the intended text changed:

```python
import pdfplumber
with pdfplumber.open('doc.pdf') as pdf:
    text = pdf.pages[TARGET_PAGE].extract_text()
    for line in text.split('\n'):
        if 'new text' in line or 'nearby_context' in line:
            print(line.strip())
```

**Done when**: the target line shows the change AND all other occurrences
of the old text (that were NOT targets) remain unchanged.

## Reference

### Non-ASCII filenames

Both scripts use `pathlib.Path` for file I/O and reconfigure stdout/stderr
to UTF-8 at startup, so Unicode filenames and console output work on all
platforms. If you hit encoding issues in an unusual terminal, pipe output
through a UTF-8-aware tool or redirect to file.

### The `page.get_contents()` trap

If you write custom content-stream code, `page.get_contents().set_data()`
modifies a **copy**, not the page. Always use the raw object:

```python
raw = page['/Contents']        # the real DecodedStreamObject
raw.set_data(new_bytes)        # modifies the page
```

### Multiple content streams

Some pages store `/Contents` as an array of streams rather than a single one.
The scripts handle the single-stream case. For arrays, check each stream
individually:

```python
contents = page['/Contents']
if isinstance(contents, list):
    for i, stream in enumerate(contents):
        data = stream.get_data()
        if target_bytes in data:
            # modify stream[i]
```

### TJ array structure

PDF text is drawn via the `TJ` operator, which takes an array alternating
between hex CID strings and numeric kerning adjustments:

```
[<CID1> kerning <CID2> kerning ... <CIDn>] TJ
```

To insert text mid-array you split at a CID boundary, close with `] TJ`,
issue a new `/FontName size Tf` to switch fonts, and reopen with `[...`:

```
[<CID1>...<CIDn>] TJ /NewFont 12 Tf [<new_cids>] TJ /OrigFont 12 Tf [0<CIDn+1>...] TJ
```

The leading `0` in the reopened array preserves the original kerning value.
This is what `--insert-after` automates.
