"""
Extract Unicode <-> CID bidirectional mapping from a PDF font's ToUnicode CMap.

Usage:
    python font_map.py <pdf_path> <page_num> <font_substr> [--chars CHARS] [--json]
    python font_map.py <pdf_path> <page_num> --scan-all --chars CHARS [--json]

Examples:
    # Get full mapping for a font
    python font_map.py doc.pdf 0 FONT1

    # Get CID codes for specific Unicode characters
    python font_map.py doc.pdf 0 FONT1 --chars "test"

    # Scan ALL fonts on a page for coverage of characters
    python font_map.py doc.pdf 0 --scan-all --chars "(test)"

    # Output as JSON
    python font_map.py doc.pdf 0 FONT1 --json

Output (text mode):
    Unicode -> CID:
      U+0061 a -> <0041>
      U+0062 b -> <0042>
      U+0063 c -> <0043>

Output (scan-all mode):
    Font coverage for "(test)" on page 0:

      Char     FONT1      FONT2
      ------  ---------  ---------
      U+0028 (  ✗          ✓ 0028
      U+0074 t  ✓ 0074     ✓ 0074
      U+0065 e  ✓ 0065     ✓ 0065
      U+0073 s  ✓ 0073     ✓ 0073
      U+0074 t  ✓ 0074     ✓ 0074
      U+0029 )  ✗          ✓ 0029

    Best: FONT2 (6/6 chars -- complete)

Output (JSON mode):
    {"unicode_to_cid": {"0061": "0041", ...}, "cid_to_unicode": {"0041": "0061", ...}}
"""

import sys
import json
import re
from pypdf import PdfReader
from pypdf.generic import IndirectObject


def parse_cmap(cmap_data: bytes) -> dict:
    """
    Parse a ToUnicode CMap stream.
    Returns dict with 'unicode_to_cid' and 'cid_to_unicode' mappings.
    """
    text = cmap_data.decode('latin-1', errors='replace')

    unicode_to_cid = {}  # key: unicode_hex (lower), value: cid_hex (lower)
    cid_to_unicode = {}  # key: cid_hex (lower), value: unicode_hex (lower)

    # Parse bfchar entries: <CID> <Unicode>
    # Each entry maps one CID to one Unicode code point
    bfchar_pattern = r'<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>'
    for match in re.finditer(bfchar_pattern, text):
        cid = match.group(1).lower()
        unicode_val = match.group(2).lower()
        unicode_to_cid[unicode_val] = cid
        cid_to_unicode[cid] = unicode_val

    return {
        'unicode_to_cid': unicode_to_cid,
        'cid_to_unicode': cid_to_unicode,
    }


def unicode_char_to_hex(char: str) -> str:
    """Convert a Unicode character to its 4-digit hex representation."""
    return format(ord(char), '04x')


def find_font_resource(page, font_substr: str):
    """
    Find a font resource on a page whose name contains font_substr.
    Returns (font_name, font_object).
    """
    resources = page['/Resources']
    if '/Font' not in resources:
        return None, None
    fonts = resources['/Font']

    for font_name in fonts:
        if font_substr in font_name:
            font = fonts[font_name]
            if isinstance(font, IndirectObject):
                font = font.get_object()
            return font_name, font

    return None, None


def get_font_cmap(page, font_substr: str) -> dict:
    """
    Extract the Unicode↔CID mapping for a font on a page.
    """
    font_name, font = find_font_resource(page, font_substr)

    if font is None:
        # Try listing available fonts
        available = []
        resources = page['/Resources']
        if '/Font' in resources:
            available = list(resources['/Font'].keys())
        raise ValueError(
            f"No font matching '{font_substr}' found on page. "
            f"Available fonts: {available}"
        )

    if '/ToUnicode' not in font:
        raise ValueError(f"Font {font_name} has no ToUnicode CMap")

    tu = font['/ToUnicode']
    if isinstance(tu, IndirectObject):
        tu = tu.get_object()

    cmap_data = tu.get_data()
    return parse_cmap(cmap_data)


def get_all_font_cmaps(page) -> dict:
    """
    Extract Unicode→CID mappings for ALL fonts on a page that have ToUnicode CMaps.
    Returns {font_name: {'unicode_to_cid': {...}, 'cid_to_unicode': {...}}}
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
            cmap = parse_cmap(tu.get_data())
            clean_name = font_name.lstrip('/')
            result[clean_name] = cmap

    return result


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8')
        except Exception:
            pass

    # Parse required args: pdf_path, page_num, [font_substr | --scan-all]
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    pdf_path = sys.argv[1]
    page_num = int(sys.argv[2])

    scan_all = False
    font_substr = None
    chars_to_lookup = None
    output_json = False

    i = 3
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg == '--scan-all':
            scan_all = True
            i += 1
        elif arg == '--chars' and i + 1 < len(sys.argv):
            chars_to_lookup = sys.argv[i + 1]
            i += 2
        elif arg == '--json':
            output_json = True
            i += 1
        elif not arg.startswith('--') and font_substr is None and not scan_all:
            font_substr = arg
            i += 1
        else:
            i += 1

    if not scan_all and not font_substr:
        print("Error: must specify a font name or --scan-all", file=sys.stderr)
        sys.exit(1)

    reader = PdfReader(pdf_path)
    if page_num >= len(reader.pages):
        print(f"Error: page {page_num} out of range (PDF has {len(reader.pages)} pages)")
        sys.exit(1)

    page = reader.pages[page_num]

    # ── --scan-all mode: check all fonts for the requested characters ──
    if scan_all:
        all_cmaps = get_all_font_cmaps(page)
        if not all_cmaps:
            print("Error: no fonts with ToUnicode CMaps found on this page", file=sys.stderr)
            sys.exit(1)

        if output_json:
            print(json.dumps(all_cmaps, indent=2, ensure_ascii=False))
            return

        if chars_to_lookup:
            # Build coverage table
            print(f"Font coverage for \"{chars_to_lookup}\" on page {page_num}:\n")

            # Header row
            font_names = sorted(all_cmaps.keys())
            col_w = max(max(len(n) for n in font_names), 8)
            print(f"  {'Char':<6}", end="")
            for fn in font_names:
                print(f"  {fn:<{col_w}}", end="")
            print()
            print(f"  {'':-<6}", end="")
            for fn in font_names:
                print(f"  {'':-<{col_w}}", end="")
            print()

            # Per-char rows
            font_scores = {fn: 0 for fn in font_names}
            for char in chars_to_lookup:
                hex_key = unicode_char_to_hex(char)
                print(f"  U+{hex_key.upper()} {char} ", end="")
                for fn in font_names:
                    u2c = all_cmaps[fn]['unicode_to_cid']
                    if hex_key in u2c:
                        cid = u2c[hex_key]
                        print(f"  {'✓ ' + cid:<{col_w}}", end="")
                        font_scores[fn] += 1
                    else:
                        print(f"  {'✗':<{col_w}}", end="")
                print()

            # Summary
            print()
            ranked = sorted(font_scores.items(), key=lambda x: -x[1])
            best_name, best_score = ranked[0]
            total = len(chars_to_lookup)
            if best_score == total:
                print(f"Best: {best_name} ({best_score}/{total} chars — complete)")
            elif best_score > 0:
                print(f"Best: {best_name} ({best_score}/{total} chars — partial)")
                print(f"Missing: {', '.join(c for c in chars_to_lookup if unicode_char_to_hex(c) not in all_cmaps[best_name]['unicode_to_cid'])}")
            else:
                print("No font has any of the requested characters.")

            # List fonts with partial coverage as alternatives
            partials = [(n, s) for n, s in ranked[1:] if s > 0]
            if partials:
                print(f"Alternates: {', '.join(f'{n} ({s}/{total})' for n, s in partials)}")
        else:
            # No --chars: list all fonts with their character counts
            print(f"Fonts on page {page_num} with ToUnicode CMaps:\n")
            for fn in sorted(all_cmaps.keys()):
                count = len(all_cmaps[fn]['unicode_to_cid'])
                print(f"  {fn}: {count} characters")
        return

    # ── Single-font mode ──
    try:
        cmap = get_font_cmap(page, font_substr)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if output_json:
        print(json.dumps(cmap, indent=2))
        return

    # Text output
    unicode_to_cid = cmap['unicode_to_cid']

    if chars_to_lookup:
        print(f"Font matching '{font_substr}' on page {page_num}:")
        print()
        for char in chars_to_lookup:
            hex_key = unicode_char_to_hex(char)
            if hex_key in unicode_to_cid:
                cid = unicode_to_cid[hex_key]
                print(f"  U+{hex_key.upper()} {char} → <{cid}>")
            else:
                print(f"  U+{hex_key.upper()} {char} → NOT IN FONT", end="")
                # Suggest case variants for ASCII letters
                suggestions = []
                if char.isalpha() and len(char) == 1:
                    swapped = char.swapcase()
                    swapped_hex = unicode_char_to_hex(swapped)
                    if swapped_hex in unicode_to_cid:
                        suggestions.append(f"'{swapped}' (U+{swapped_hex.upper()}) is in font")
                if suggestions:
                    print(f"  — try: {', '.join(suggestions)}")
                else:
                    print()
    else:
        print(f"Font matching '{font_substr}' on page {page_num}:")
        print(f"  {len(unicode_to_cid)} characters mapped")
        print()
        # Show some entries as examples
        items = list(unicode_to_cid.items())[:20]
        for unicode_hex, cid_hex in items:
            char = chr(int(unicode_hex, 16))
            if char.isprintable():
                print(f"  U+{unicode_hex.upper()} {char} → <{cid_hex}>")
            else:
                print(f"  U+{unicode_hex.upper()} → <{cid_hex}>")
        if len(unicode_to_cid) > 20:
            print(f"  ... and {len(unicode_to_cid) - 20} more")


if __name__ == '__main__':
    main()
