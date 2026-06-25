"""
Find, replace, delete, or insert text in a PDF by directly manipulating the content stream.

Usage:
    # Find text locations (JSON output to stdout)
    python edit_pdf.py doc.pdf 0 --find "hello" [--bounds 100,200,300,220]

    # Replace text (modifies PDF in-place, same-font only)
    python edit_pdf.py doc.pdf 0 --replace "hello" --with "world" [--bounds 100,200,300,220]

    # Delete text (modifies PDF in-place)
    python edit_pdf.py doc.pdf 0 --delete "hello" [--bounds 100,200,300,220]

    # Insert text after a match (supports cross-font via --font)
    python edit_pdf.py doc.pdf 0 --insert-after "Company" --insert-text " (edited)" --font FONT2

    # Dry-run: preview changes without modifying the file
    python edit_pdf.py doc.pdf 0 --replace "hello" --with "world" --dry-run

Bounds format: x0,top,x1,bottom (pdfplumber coordinates, comma-separated).
Bounds are optional but recommended for disambiguation when text appears multiple times.
"""

import sys
import json
import re
from pathlib import Path
from pypdf import PdfReader, PdfWriter
from pypdf.generic import IndirectObject

# ── CMap parsing (same as font_map.py) ────────────────────────────────────────

def parse_cmap(cmap_data: bytes) -> dict:
    """Parse ToUnicode CMap, return {unicode_hex: cid_hex} mapping."""
    text = cmap_data.decode('latin-1', errors='replace')
    mapping = {}
    for match in re.finditer(r'<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>', text):
        cid = match.group(1).lower()
        unicode_val = match.group(2).lower()
        mapping[unicode_val] = cid
    return mapping


def get_font_cmaps(page) -> dict:
    """
    Extract all Unicode→CID mappings for all fonts on a page.
    Returns {font_name: {'unicode_to_cid': {..., '719f': '6359', ...}, 'cid_to_unicode': {...}}}
    """
    result = {}
    resources = page['/Resources']
    if '/Font' not in resources:
        return result

    for font_name in resources['/Font']:
        font = resources['/Font'][font_name]
        if isinstance(font, IndirectObject):
            font = font.get_object()
        if '/ToUnicode' in font:
            tu = font['/ToUnicode']
            if isinstance(tu, IndirectObject):
                tu = tu.get_object()
            mapping = parse_cmap(tu.get_data())
            reverse = {v: k for k, v in mapping.items()}
            # Clean font name (remove leading /)
            clean_name = font_name.lstrip('/')
            result[clean_name] = {
                'unicode_to_cid': mapping,
                'cid_to_unicode': reverse,
            }
    return result


# ── Content stream analysis ───────────────────────────────────────────────────

def find_text_in_stream(content_data: bytes, font_cmaps: dict,
                         search_text: str) -> list:
    """
    Search for text in a PDF content stream.

    Args:
        content_data: Raw content stream bytes
        font_cmaps: Font CMap mappings from get_font_cmaps()
        search_text: Unicode text to search for

    Returns:
        List of match dicts with keys: font, text, cids, byte_start,
        byte_end, approx_x, approx_y
    """
    text = content_data.decode('latin-1', errors='replace')
    results = []

    # Build search pattern: the target text encoded as CID hex codes
    # For each font, find the CID sequence that would render search_text
    search_patterns = {}
    for font_name, cmap in font_cmaps.items():
        u2c = cmap['unicode_to_cid']
        cids = []
        complete = True
        for char in search_text:
            char_hex = format(ord(char), '04x')
            if char_hex in u2c:
                cids.append(u2c[char_hex])
            else:
                complete = False
                break
        if complete and cids:
            # Build regex to find this CID sequence in TJ/Tj arrays
            # Format in stream: <CID1>0<CID2>0<CID3> or <CID1>kern<CID2>...
            # We match: <CID1> followed by optional kerning, then <CID2>, etc.
            pattern_parts = []
            for cid in cids:
                pattern_parts.append(f'<{cid}>')
            # Allow optional kerning values between CIDs (digits, dots, minus)
            sep = r'[0-9.\-\s]*'
            stream_pattern = sep.join(pattern_parts)
            search_patterns[font_name] = (stream_pattern, cids)

    # Find all BT/ET text blocks and check each one
    # We need to find BT/ET pairs and look for text matching our patterns
    bt_positions = [(m.start(), m.end()) for m in re.finditer(r'\bBT\b', text)]
    et_positions = [(m.start(), m.end()) for m in re.finditer(r'\bET\b', text)]

    # Simple pairing: each BT matches the next ET
    blocks = []
    et_idx = 0
    for bt_start, bt_end in bt_positions:
        while et_idx < len(et_positions) and et_positions[et_idx][0] < bt_start:
            et_idx += 1
        if et_idx < len(et_positions):
            blocks.append((bt_start, et_positions[et_idx][1]))

    for block_start, block_end in blocks:
        block_text = text[block_start:block_end]

        # Find Tm operators to get position context
        tm_pattern = r'([\-\d.]+)\s+([\-\d.]+)\s+([\-\d.]+)\s+([\-\d.]+)\s+([\-\d.]+)\s+([\-\d.]+)\s+Tm'
        tm_matches = list(re.finditer(tm_pattern, block_text))
        approx_x = approx_y = None
        if tm_matches:
            approx_x = float(tm_matches[0].group(5))
            approx_y = float(tm_matches[0].group(6))

        # Find font reference in this block: /FontName Tf
        font_match = re.search(r'/(\S+)\s+([\d.]+)\s+Tf', block_text)
        block_font = font_match.group(1) if font_match else None
        block_font_size = float(font_match.group(2)) if font_match else None

        # Check each font's pattern against this block
        for font_name, (pattern, cids) in search_patterns.items():
            if block_font and block_font != font_name:
                # Different font in this block, skip
                continue

            for match in re.finditer(pattern, block_text, re.IGNORECASE):
                abs_start = block_start + match.start()
                abs_end = block_start + match.end()

                results.append({
                    'font': font_name,
                    'text': search_text,
                    'cids': cids,
                    'byte_start': abs_start,
                    'byte_end': abs_end,
                    'approx_x': approx_x,
                    'approx_y': approx_y,
                    'font_size': block_font_size,
                    'block_start': block_start,
                    'block_end': block_end,
                })

    return results


# ── Edit operations ───────────────────────────────────────────────────────────

def apply_replace(content_data: bytes, match_info: dict, new_text: str,
                  font_cmaps: dict) -> bytes:
    """
    Replace text in the content stream.

    Args:
        content_data: Raw content stream bytes
        match_info: A match dict from find_text_in_stream()
        new_text: Replacement Unicode text
        font_cmaps: Font CMap mappings

    Returns:
        Modified content stream bytes
    """
    text = content_data.decode('latin-1', errors='replace')

    font_name = match_info['font']
    cmap = font_cmaps.get(font_name)
    if not cmap:
        raise ValueError(f"Font {font_name} not found in CMap data")

    u2c = cmap['unicode_to_cid']

    # Build replacement CID sequence
    new_cids = []
    for char in new_text:
        char_hex = format(ord(char), '04x')
        if char_hex not in u2c:
            raise ValueError(
                f"Character '{char}' (U+{char_hex.upper()}) not found in font {font_name}. "
                f"This character is not in the font subset embedded in the PDF. "
                f"Check if the character appears elsewhere in the document."
            )
        new_cids.append(u2c[char_hex])

    # Build the replacement string: <CID1>0<CID2>0...
    replacement = '0'.join(f'<{cid}>' for cid in new_cids)

    # Replace in content stream
    start, end = match_info['byte_start'], match_info['byte_end']
    new_text_data = text[:start] + replacement + text[end:]

    return new_text_data.encode('latin-1', errors='replace')


def apply_delete(content_data: bytes, match_info: dict) -> bytes:
    """Delete text from the content stream."""
    text = content_data.decode('latin-1', errors='replace')
    start, end = match_info['byte_start'], match_info['byte_end']
    clean_start = start
    clean_end = end

    before = text[max(0, start-10):start]
    kerning_before = re.search(r'([\-\d.]+)\s*$', before)
    if kerning_before:
        clean_start = start - len(kerning_before.group(1))

    after = text[end:end+10]
    kerning_after = re.search(r'^\s*([\-\d.]+)', after)
    if kerning_after:
        clean_end = end + len(kerning_after.group(0))

    new_text_data = text[:clean_start] + text[clean_end:]
    return new_text_data.encode('latin-1', errors='replace')


def apply_insert_after(content_data: bytes, match_info: dict, insert_text: str,
                       insert_font: str, font_cmaps: dict) -> bytes:
    """
    Insert text after a match in the content stream, switching fonts if needed.

    Finds the TJ array containing the match, splits it at the match end point,
    and inserts a new TJ operator with the insert font. If the original font
    differs from the insert font, font-switch commands are inserted.

    Args:
        content_data: Raw content stream bytes
        match_info: A match dict from find_text_in_stream()
        insert_text: Unicode text to insert after the match
        insert_font: Font name to use for the inserted text
        font_cmaps: Font CMap mappings

    Returns:
        Modified content stream bytes
    """
    text = content_data.decode('latin-1', errors='replace')

    orig_font = match_info['font']
    font_size = match_info.get('font_size', 12)

    # Build CIDs for the inserted text in the insert font
    cmap = font_cmaps.get(insert_font)
    if not cmap:
        raise ValueError(f"Font '{insert_font}' not found in CMap data. "
                         f"Available: {list(font_cmaps.keys())}")

    u2c = cmap['unicode_to_cid']
    new_cids = []
    for char in insert_text:
        char_hex = format(ord(char), '04x')
        if char_hex not in u2c:
            raise ValueError(
                f"Character '{char}' (U+{char_hex.upper()}) not found in font '{insert_font}'. "
                f"Run font_map.py --scan-all to find a suitable font."
            )
        new_cids.append(u2c[char_hex])

    insert_cid_str = '0'.join(f'<{cid}>' for cid in new_cids)

    # Locate the TJ array boundaries
    match_end = match_info['byte_end']

    # Search backward for opening '['
    tj_open = text.rfind('[', 0, match_end)
    if tj_open == -1:
        raise ValueError("Could not find opening '[' of TJ array")

    # Search forward for closing '] TJ'
    tj_close_match = re.search(r'\]\s*TJ', text[match_end:])
    if not tj_close_match:
        raise ValueError("Could not find closing '] TJ' of TJ array")
    tj_close = match_end + tj_close_match.end()

    # Split the stream
    before_insert = text[:match_end]       # up to end of matched CIDs
    after_insert = text[match_end:tj_close] # remainder of TJ array
    rest_of_stream = text[tj_close:]        # after '] TJ'

    # Check if there are remaining CIDs in the TJ array after the match
    has_remaining = bool(re.search(r'<[0-9a-fA-F]+>', after_insert))

    if insert_font == orig_font:
        # Same font — just insert the CIDs inline, no font switch needed
        font_cmd = f'0{insert_cid_str}'
        new_text = before_insert + font_cmd + after_insert + rest_of_stream
    elif has_remaining:
        # Split TJ: close, switch font, insert, switch back, continue
        font_cmd = (f'] TJ /{insert_font} {font_size} Tf [{insert_cid_str}] TJ '
                    f'/{orig_font} {font_size} Tf [')
        new_text = before_insert + font_cmd + after_insert + rest_of_stream
    else:
        # Match at end of TJ — no need to switch back
        font_cmd = f'] TJ /{insert_font} {font_size} Tf [{insert_cid_str}] TJ'
        new_text = before_insert + font_cmd + rest_of_stream

    return new_text.encode('latin-1', errors='replace')


# ── Main CLI ──────────────────────────────────────────────────────────────────

def main():
    # Ensure Unicode output works on all platforms. Non-UTF-8 codecs
    # (Windows GBK/cp932/cp1252) cannot encode arbitrary CJK or emoji
    # that appear in PDFs. Piped/redirected stdout raises OSError and
    # is silently skipped — the pipe's own encoding takes over.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8')
        except Exception:
            pass

    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)

    pdf_path = sys.argv[1]
    page_num = int(sys.argv[2])

    # Parse command
    mode = None
    search_text = None
    replace_with = None
    insert_text = None
    insert_font = None
    bounds_str = None
    match_index = None
    dry_run = False

    i = 3
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg == '--find' and i + 1 < len(sys.argv):
            mode = 'find'
            search_text = sys.argv[i + 1]
            i += 2
        elif arg == '--replace' and i + 1 < len(sys.argv):
            mode = 'replace'
            search_text = sys.argv[i + 1]
            i += 2
        elif arg == '--with' and i + 1 < len(sys.argv):
            replace_with = sys.argv[i + 1]
            i += 2
        elif arg == '--delete' and i + 1 < len(sys.argv):
            mode = 'delete'
            search_text = sys.argv[i + 1]
            i += 2
        elif arg == '--insert-after' and i + 1 < len(sys.argv):
            mode = 'insert-after'
            search_text = sys.argv[i + 1]
            i += 2
        elif arg == '--insert-text' and i + 1 < len(sys.argv):
            insert_text = sys.argv[i + 1]
            i += 2
        elif arg == '--font' and i + 1 < len(sys.argv):
            insert_font = sys.argv[i + 1]
            i += 2
        elif arg == '--bounds' and i + 1 < len(sys.argv):
            bounds_str = sys.argv[i + 1]
            i += 2
        elif arg == '--match-index' and i + 1 < len(sys.argv):
            match_index = int(sys.argv[i + 1])
            i += 2
        elif arg == '--dry-run':
            dry_run = True
            i += 1
        else:
            print(f"Unknown argument: {arg}", file=sys.stderr)
            sys.exit(1)

    if not mode or not search_text:
        print("Error: must specify --find, --replace, --delete, or --insert-after",
              file=sys.stderr)
        sys.exit(1)

    if mode == 'replace' and not replace_with:
        print("Error: --replace requires --with", file=sys.stderr)
        sys.exit(1)

    if mode == 'insert-after' and not insert_text:
        print("Error: --insert-after requires --insert-text", file=sys.stderr)
        sys.exit(1)

    # Parse bounds
    bounds = None
    if bounds_str:
        try:
            parts = [float(x.strip()) for x in bounds_str.split(',')]
            if len(parts) == 4:
                bounds = tuple(parts)
        except ValueError:
            print(f"Error: invalid bounds format '{bounds_str}'", file=sys.stderr)
            sys.exit(1)

    # Read PDF
    reader = PdfReader(pdf_path)
    if page_num >= len(reader.pages):
        print(f"Error: page {page_num} out of range", file=sys.stderr)
        sys.exit(1)

    page = reader.pages[page_num]
    raw_contents = page['/Contents']
    content_data = raw_contents.get_data()

    # Get font mappings
    font_cmaps = get_font_cmaps(page)

    if not font_cmaps:
        print("Error: no fonts with ToUnicode CMaps found on this page", file=sys.stderr)
        sys.exit(1)

    # Find text in content stream
    matches = find_text_in_stream(content_data, font_cmaps, search_text)

    if not matches:
        print(f"Text '{search_text}' not found in content stream.", file=sys.stderr)
        if bounds:
            print("Try without --bounds or check that the coordinates are correct.", file=sys.stderr)
        sys.exit(1)

    # For --find mode: output match info
    if mode == 'find':
        output = []
        for m in matches:
            output.append({
                'font': m['font'],
                'text': m['text'],
                'cids': m['cids'],
                'byte_start': m['byte_start'],
                'byte_end': m['byte_end'],
                'approx_x': m['approx_x'],
                'approx_y': m['approx_y'],
            })
        print(json.dumps(output, indent=2, ensure_ascii=False))
        return

    # For replace/delete: select which match to modify
    target = None

    # Try --match-index first
    if match_index is not None:
        if 0 <= match_index < len(matches):
            target = matches[match_index]
        else:
            print(f"Error: --match-index {match_index} out of range (0-{len(matches)-1})",
                  file=sys.stderr)
            sys.exit(1)
    elif bounds and len(matches) > 1:
        # Use pdfplumber to find which match falls within bounds
        import pdfplumber as _pdfplumber
        with _pdfplumber.open(pdf_path) as pdf:
            p = pdf.pages[page_num]
            bx0, btop, bx1, bbottom = bounds
            for m in matches:
                for word in p.extract_words():
                    if search_text in word['text']:
                        wx0, wtop, wx1, wbottom = word['x0'], word['top'], word['x1'], word['bottom']
                        # Check if word overlaps with bounds
                        overlap_x = wx0 < bx1 and wx1 > bx0
                        overlap_y = wtop < bbottom and wbottom > btop
                        if overlap_x and overlap_y:
                            target = m
                            break
                if target:
                    break

    if target is None:
        if len(matches) == 1:
            target = matches[0]
        else:
            print(f"Found {len(matches)} matches for '{search_text}'.", file=sys.stderr)
            print("Use --match-index N to pick one, or --bounds to filter.", file=sys.stderr)
            for i, m in enumerate(matches):
                print(f"  [{i}] font={m['font']}, x≈{m['approx_x']:.0f}, y≈{m['approx_y']:.0f}, "
                      f"bytes={m['byte_start']}-{m['byte_end']}", file=sys.stderr)
            sys.exit(1)

    # Apply the edit
    if mode == 'replace':
        try:
            new_data = apply_replace(content_data, target, replace_with, font_cmaps)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    elif mode == 'delete':
        new_data = apply_delete(content_data, target)
    elif mode == 'insert-after':
        # Default to match font if --font not specified
        use_font = insert_font if insert_font else target['font']
        try:
            new_data = apply_insert_after(content_data, target, insert_text,
                                          use_font, font_cmaps)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print(f"Unknown mode: {mode}", file=sys.stderr)
        sys.exit(1)

    if dry_run:
        old_segment = content_data[target['byte_start']:target['byte_end']]
        new_segment = None
        if mode in ('replace', 'insert-after'):
            # For insert-after, show a broader view of the change
            ctx_start = max(0, target['byte_start'] - 20)
            ctx_end = min(len(new_data), target['byte_end'] + 200)
            new_segment = new_data[ctx_start:ctx_end]
            old_segment = content_data[ctx_start:ctx_end]
        else:
            new_segment = new_data[target['byte_start']:target['byte_start'] + len(old_segment)]
        print(f"Would modify near byte {target['byte_start']}:")
        print(f"Old: {old_segment}")
        print(f"New: {new_segment}")
        if new_segment == old_segment:
            print("WARNING: old and new are identical — no change would be made.")
        print("Dry run -- no changes made.")
        return

    # Write back
    raw_contents.set_data(new_data)

    writer = PdfWriter()
    for i, p in enumerate(reader.pages):
        writer.add_page(p)

    with Path(pdf_path).open('wb') as f:
        writer.write(f)

    msg = f"[OK] {mode}d '{search_text}'"
    if mode == 'replace':
        msg += f" -> '{replace_with}'"
    elif mode == 'insert-after':
        msg += f" + '{insert_text}' (font: {use_font})"
    msg += f" on page {page_num}"
    print(msg)


if __name__ == '__main__':
    main()
