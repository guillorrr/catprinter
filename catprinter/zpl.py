'''Renders a small subset of ZPL (Zebra label language) to printable images.

Enough for Mercado Libre's product and shipping (Flex) labels: ^XA/^XZ, ^LH,
^FO, ^FD/^FS, ^FH (hex escapes), ^CI (charset; UTF-8 is assumed), ^A0 (scalable
font), ^FB (text block), ^GB (boxes and lines), ^GFA (bitmaps), ^BY/^BC
(Code 128) and ^BQ (QR code). Other commands are ignored.

ZPL coordinates are printer dots at 203 dpi, the same resolution as the cat
printers, so labels that fit are drawn 1:1 and barcodes keep their exact module
width. Labels wider than the paper (e.g. two stickers side by side) are split
into their columns, and each column is printed as a separate sticker. A sticker
that is still too wide (e.g. a 10 cm shipping label) is redrawn scaled down to
the paper width, with barcode and QR modules rounded to whole dots.
'''
import re

import numpy as np
import qrcode
from PIL import Image, ImageDraw, ImageFont

from catprinter import logger

# ^A0 is a bold condensed sans; use the closest one that is installed.
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/liberation/LiberationSansNarrow-Bold.ttf",
    "/usr/share/fonts/opentype/urw-base35/NimbusSansNarrow-Bold.otf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
    "/Library/Fonts/Arial Narrow Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Narrow Bold.ttf",
]

# Blank space between stickers, in dots.
STICKER_GAP = 24
# Minimum blank band that separates two side-by-side stickers, in dots.
COLUMN_GAP = 16
# Drawing area for one label, in dots (a 4x6" label at 203 dpi is 812x1218).
CANVAS_SIZE = (2000, 3000)

CODE128_PATTERNS = [
    "212222", "222122", "222221", "121223", "121322", "131222", "122213", "122312",
    "132212", "221213", "221312", "231212", "112232", "122132", "122231", "113222",
    "123122", "123221", "223211", "221132", "221231", "213212", "223112", "312131",
    "311222", "321122", "321221", "312212", "322112", "322211", "212123", "212321",
    "232121", "111323", "131123", "131321", "112313", "132113", "132311", "211313",
    "231113", "231311", "112133", "112331", "132131", "113123", "113321", "133121",
    "313121", "211331", "231131", "213113", "213311", "213131", "311123", "311321",
    "331121", "312113", "312311", "332111", "314111", "221411", "431111", "111224",
    "111422", "121124", "121421", "141122", "141221", "112214", "112412", "122114",
    "122411", "142112", "142211", "241211", "221114", "413111", "241112", "134111",
    "111242", "121142", "121241", "114212", "124112", "124211", "411212", "421112",
    "421211", "212141", "214121", "412121", "111143", "111341", "131141", "114113",
    "114311", "411113", "411311", "113141", "114131", "311141", "411131", "211412",
    "211214", "211232", "2331112",
]
CODE128_START_B = 104
CODE128_STOP = 106


def code128_modules(data):
    '''Encodes data with Code 128 subset B. Returns bar/space widths in modules.'''
    codes = [CODE128_START_B]
    for ch in data:
        value = ord(ch) - 32
        if not 0 <= value < 95:
            raise ValueError(f"Character {ch!r} cannot be encoded in Code 128 subset B")
        codes.append(value)
    checksum = (codes[0] + sum(i * c for i, c in enumerate(codes[1:], start=1))) % 103
    codes += [checksum, CODE128_STOP]
    return [int(w) for c in codes for w in CODE128_PATTERNS[c]]


def find_font():
    for path in FONT_CANDIDATES:
        try:
            ImageFont.truetype(path, 10)
            return path
        except OSError:
            continue
    return None


def decode_field_hex(text, indicator="_"):
    '''Applies ^FH: "_C3_A1" -> the bytes 0xC3 0xA1. Text is UTF-8 (^CI28).'''
    raw = bytearray()
    i = 0
    while i < len(text):
        if text[i] == indicator and re.fullmatch(r"[0-9A-Fa-f]{2}", text[i + 1:i + 3]):
            raw.append(int(text[i + 1:i + 3], 16))
            i += 3
        else:
            raw.extend(text[i].encode("utf-8"))
            i += 1
    return raw.decode("utf-8", errors="replace")


def parse_labels(zpl):
    '''Splits ZPL into labels; each label is a list of (command, params) tuples.'''
    labels = []
    for chunk in re.split(r"\^XZ", zpl):
        if "^XA" not in chunk:
            continue
        chunk = chunk[chunk.index("^XA"):]
        cmds = []
        # ^FD data runs until ^FS and may contain anything but the caret.
        for m in re.finditer(r"\^(FD)([^^]*)|\^([A-Z][A-Z0-9@])([^^]*)", chunk):
            if m.group(1):
                cmds.append(("FD", m.group(2).replace("\n", "").replace("\r", "")))
            else:
                cmds.append((m.group(3), m.group(4).strip()))
        labels.append(cmds)
    return labels


def _ints(params, defaults):
    out = []
    parts = params.split(",") if params else []
    for i, default in enumerate(defaults):
        try:
            out.append(int(parts[i]))
        except (IndexError, ValueError):
            out.append(default)
    return out


def _wrap(draw, text, font, width):
    lines, line = [], ""
    for word in text.split():
        candidate = f"{line} {word}".strip()
        if line and draw.textlength(candidate, font=font) > width:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    return lines


def _draw_text(canvas, x, y, text, height, width, font_path):
    '''Draws ^A0 text, height and width in dots, scaled horizontally by width/height.'''
    font = ImageFont.truetype(font_path, height) if font_path else ImageFont.load_default()
    mask = Image.new("L", (int(font.getlength(text)) + 4, height + 4), 0)
    ImageDraw.Draw(mask).text((0, 0), text, font=font, fill=255)
    if height and abs(width / height - 1) > 0.01:
        mask = mask.resize((max(1, round(mask.width * width / height)), mask.height))
    canvas.paste(0, (x, y), mask)
    return mask.width


def decode_graphic_field(data, bytes_per_row):
    '''Decodes ^GFA ASCII hex data, including ZPL's run-length compression.

    G-Y repeat the next hex digit 1-19 times, g-z 20-400 times; "," fills the rest of
    the row with 0s, "!" with 1s, and ":" repeats the previous row.
    '''
    row_digits = bytes_per_row * 2
    rows, row, count = [], "", 0
    for ch in data:
        if "G" <= ch <= "Y":
            count += ord(ch) - ord("G") + 1
        elif "g" <= ch <= "z":
            count += (ord(ch) - ord("g") + 1) * 20
        elif ch in ",!":
            rows.append(row + ("0" if ch == "," else "F") * (row_digits - len(row)))
            row, count = "", 0
        elif ch == ":":
            if row:
                rows.append(row.ljust(row_digits, "0"))
            rows.append(rows[-1] if rows else "0" * row_digits)
            row, count = "", 0
        elif ch in "0123456789ABCDEFabcdef":
            row += ch * (count or 1)
            count = 0
            if len(row) >= row_digits:
                rows.append(row[:row_digits])
                row = ""
    if row:
        rows.append(row.ljust(row_digits, "0"))
    bits = np.unpackbits(np.frombuffer(bytes.fromhex("".join(rows)), dtype=np.uint8))
    return bits.reshape(len(rows), bytes_per_row * 8).astype(bool)


def _paste_bits(canvas, x, y, bits):
    '''Draws a boolean array (True = black) at x, y.'''
    mask = Image.fromarray(np.where(bits, 255, 0).astype(np.uint8))
    canvas.paste(0, (x, y), mask)


def _qr_matrix(ec, data):
    level = {"L": qrcode.constants.ERROR_CORRECT_L,
             "M": qrcode.constants.ERROR_CORRECT_M,
             "Q": qrcode.constants.ERROR_CORRECT_Q,
             "H": qrcode.constants.ERROR_CORRECT_H}.get(ec[:1].upper(),
                                                        qrcode.constants.ERROR_CORRECT_M)
    qr = qrcode.QRCode(error_correction=level, border=0, box_size=1)
    qr.add_data(data)
    qr.make(fit=True)
    return np.array(qr.get_matrix(), dtype=bool)


def parse_fields(cmds):
    '''Turns a label's commands into a list of fields (dicts) with absolute positions.'''
    fields = []
    home = (0, 0)
    origin = (0, 0)
    font_h, font_w = 18, 18
    block = None  # (width, max lines, line spacing, justification)
    module, bar_height = 2, 10
    barcode = None
    qr_magnification = None
    hex_escape = False

    for cmd, params in cmds:
        if cmd == "LH":
            home = tuple(_ints(params, [0, 0]))
        elif cmd == "FO":
            x, y = _ints(params, [0, 0])
            origin = (home[0] + x, home[1] + y)
        elif cmd == "A0":
            # "^A0N,h,w": orientation, then height and width.
            font_h, font_w = _ints(params.partition(",")[2], [font_h, 0])
            font_w = font_w or font_h
        elif cmd == "FB":
            width, lines, spacing = _ints(params, [0, 1, 0])
            justification = (params.split(",") + ["", "", "", "L"])[3].strip() or "L"
            block = (width, lines, spacing, justification)
        elif cmd == "FH":
            hex_escape = True
        elif cmd == "BY":
            module = _ints(params, [2])[0]
        elif cmd == "BC":
            # "^BCN,h,...": orientation, then bar height.
            barcode = _ints(params.partition(",")[2], [bar_height])[0]
        elif cmd == "BQ":
            # "^BQN,model,magnification".
            qr_magnification = _ints(params.partition(",")[2], [2, 3])[1]
        elif cmd == "GB":
            w, h, t = _ints(params, [1, 1, 1])
            fields.append(dict(kind="box", x=origin[0], y=origin[1], w=w, h=h, t=t))
        elif cmd == "GF":
            parts = params.split(",", 4)
            if len(parts) == 5 and parts[0].strip() == "A":
                fields.append(dict(kind="graphic", x=origin[0], y=origin[1],
                                   bits=decode_graphic_field(parts[4], int(parts[3]))))
        elif cmd == "FD":
            text = decode_field_hex(params) if hex_escape else params
            x, y = origin
            if qr_magnification is not None:
                # "<error correction><input mode>,<data>", e.g. "LA,{...}".
                ec, _, data = text.partition(",")
                fields.append(dict(kind="qr", x=x, y=y, mag=qr_magnification,
                                   matrix=_qr_matrix(ec, data)))
            elif barcode is not None:
                fields.append(dict(kind="barcode", x=x, y=y, module=module, h=barcode,
                                   modules=code128_modules(text)))
            elif text.strip():
                fields.append(dict(kind="text", x=x, y=y, h=font_h, w=font_w, text=text,
                                   block=block))
        elif cmd == "FS":
            barcode, qr_magnification, block, hex_escape = None, None, None, False
    return fields


def _draw_barcode(draw, x, y, modules, m, height):
    for i, w in enumerate(modules):
        if i % 2 == 0:
            draw.rectangle((x, y, x + w * m - 1, y + height - 1), fill=0)
        x += w * m


def render_label(fields, font_path, scale=1.0):
    '''Draws a label as laid out in the ZPL. scale < 1 shrinks it, keeping barcode
    modules whole dots.'''
    canvas = Image.new("L", CANVAS_SIZE, 255)
    draw = ImageDraw.Draw(canvas)

    def sc(v):
        return round(v * scale)

    def module_size(v):
        # Barcodes and QR codes need whole-dot modules. Rounding up would make them overlap
        # the fields around them, so round to the nearest dot.
        return max(1, round(v * scale))

    for f in fields:
        x, y = sc(f["x"]), sc(f["y"])
        if f["kind"] == "box":
            t = max(1, sc(f["t"]))
            x1, y1 = x + max(sc(f["w"]), t) - 1, y + max(sc(f["h"]), t) - 1
            draw.rectangle((x, y, x1, y1), fill=0)
            if x1 - x + 1 > 2 * t and y1 - y + 1 > 2 * t:
                draw.rectangle((x + t, y + t, x1 - t, y1 - t), fill=255)
        elif f["kind"] == "graphic":
            bits = f["bits"]
            if scale != 1:
                h, w = bits.shape
                img = Image.fromarray(np.where(bits, 255, 0).astype(np.uint8))
                img = img.resize((max(1, sc(w)), max(1, sc(h))), Image.Resampling.BOX)
                bits = np.array(img) >= 128
            _paste_bits(canvas, x, y, bits)
        elif f["kind"] == "qr":
            m = module_size(f["mag"])
            _paste_bits(canvas, x, y, np.kron(f["matrix"], np.ones((m, m), bool)))
        elif f["kind"] == "barcode":
            _draw_barcode(draw, x, y, f["modules"], module_size(f["module"]), sc(f["h"]))
        elif f["kind"] == "text":
            h, w = max(1, sc(f["h"])), max(1, sc(f["w"]))
            font = ImageFont.truetype(font_path, h) if font_path else None
            block = f["block"]
            lines, justification, block_w, spacing = [f["text"]], "L", 0, 0
            if block and block[0] and font:
                block_w, spacing, justification = sc(block[0]), sc(block[2]), block[3]
                lines = _wrap(draw, f["text"], font, block_w * h / w)[:max(1, block[1])]
            for n, line in enumerate(lines):
                line_x = x
                if justification in "CR" and block_w and font:
                    line_w = draw.textlength(line, font=font) * w / h
                    offset = block_w - line_w
                    line_x = x + round(offset / 2 if justification == "C" else offset)
                _draw_text(canvas, line_x, y + n * (h + spacing), line, h, w, font_path)
    return canvas


# --- Reflow -------------------------------------------------------------------------------------
# Rebuilds a label that is too wide for the paper as a single column: every field gets the full
# width, one under the other, instead of shrinking the whole drawing.

# Text and QR codes keep their original size in dots (at 203 dpi that is already large on
# 48 mm paper); only what does not fit the width is wrapped or shrunk.
REFLOW_TEXT_SCALE = 1.0
REFLOW_MARGIN = 4
REFLOW_ROW_GAP = 2
REFLOW_LINE_GAP = 1
REFLOW_RULE_GAP = 3
# White space kept around a QR code, in modules, so scanners can find its edges. The paper's
# own unprinted margins add to it on the sides.
REFLOW_QR_QUIET_MODULES = 2


def _merge_overlays(texts):
    '''ZPL fakes bold by printing a field twice, 1 dot apart, or by printing just its
    "Label:" part again on top. Drop those copies and remember how much of the field
    was bold instead.'''
    kept = []
    for f in sorted(texts, key=lambda f: -len(f["text"])):
        for k in kept:
            if (abs(k["x"] - f["x"]) <= 2 and abs(k["y"] - f["y"]) <= 2
                    and k["text"].startswith(f["text"].strip())):
                k["bold"] = max(k["bold"], len(f["text"].strip()))
                break
        else:
            kept.append(dict(f, bold=0))
    return kept


def _visual_x(f, font_path):
    '''Where the field's text actually starts, accounting for centered/right blocks.'''
    block = f["block"]
    if not block or not block[0] or block[3] not in "CR":
        return f["x"]
    text_w = ImageFont.truetype(font_path, f["h"]).getlength(f["text"]) * f["w"] / f["h"]
    offset = block[0] - text_w
    return f["x"] + (offset / 2 if block[3] == "C" else offset)


def _text_rows(texts, font_path):
    '''Groups text fields whose vertical extents overlap: they sat side by side.'''
    rows = []
    for f in sorted(texts, key=lambda f: (f["y"], f["x"])):
        top, bottom = f["y"], f["y"] + f["h"]
        if rows and top < rows[-1]["bottom"] - 2:
            rows[-1]["fields"].append(f)
            rows[-1]["bottom"] = max(rows[-1]["bottom"], bottom)
        else:
            rows.append(dict(kind="text", y=top, bottom=bottom, fields=[f]))
    for row in rows:
        row["fields"].sort(key=lambda f: _visual_x(f, font_path))
    return rows


def _fit_font(font_path, h, w, text, width):
    '''Shrinks h/w until the longest word of text fits in width.'''
    longest = max(text.split(), key=len, default=text)
    while h > 8:
        font = ImageFont.truetype(font_path, h)
        if font.getlength(longest) * w / h <= width:
            break
        h, w = h - 1, max(1, round(w * (h - 1) / h))
    return h, w


def _layout_text_row(row, font_path, width):
    '''Returns the lines of a text row: lists of (text, h, w, bold chars) segments.'''
    lines, line, line_w = [], [], 0
    for f in row["fields"]:
        h = max(1, round(f["h"] * REFLOW_TEXT_SCALE))
        w = max(1, round(f["w"] * REFLOW_TEXT_SCALE))
        h, w = _fit_font(font_path, h, w, f["text"], width)
        font = ImageFont.truetype(font_path, h)
        space = font.getlength(" ") * w / h
        seg_w = font.getlength(f["text"]) * w / h
        if line and line_w + space + seg_w <= width:
            line.append((f["text"], h, w, f["bold"]))
            line_w += space + seg_w
            continue
        if line:
            lines.append(line)
        if seg_w <= width:
            line, line_w = [(f["text"], h, w, f["bold"])], seg_w
            continue
        # Too long for one line: wrap the field's words.
        bold = f["bold"]
        wrapped = _wrap(ImageDraw.Draw(Image.new("L", (1, 1))), f["text"], font, width * h / w)
        for part in wrapped[:-1]:
            lines.append([(part, h, w, min(bold, len(part)))])
            bold = max(0, bold - len(part) - 1)
        line = [(wrapped[-1], h, w, bold)]
        line_w = font.getlength(wrapped[-1]) * w / h
    if line:
        lines.append(line)
    return lines


def reflow_label(fields, font_path, width):
    '''Lays a label out again as one column of full-width rows.'''
    texts = _merge_overlays([dict(f, text=f["text"].strip())
                             for f in fields if f["kind"] == "text"])
    label_w = max((f["x"] + f.get("w", 0) for f in fields if f["kind"] == "box"), default=width)
    rows = _text_rows(texts, font_path)
    for f in fields:
        if f["kind"] in ("qr", "barcode", "graphic"):
            rows.append(dict(kind=f["kind"], y=f["y"], field=f))
        elif f["kind"] == "box" and f["w"] >= label_w / 2 and f["h"] <= max(f["t"], 4):
            # Full-width rules separate sections; vertical ones only split columns.
            rows.append(dict(kind="rule", y=f["y"], field=f))
    rows.sort(key=lambda r: r["y"])

    canvas = Image.new("L", (width, CANVAS_SIZE[1]), 255)
    draw = ImageDraw.Draw(canvas)
    y = 0
    # A graphic that sat beside text (e.g. a logo next to the sender) floats at the left of
    # those text rows instead of taking a row of its own.
    indent, indent_until = 0, 0
    for row in rows:
        f = row.get("field")
        if row["kind"] != "text":
            y = max(y, indent_until)
        if row["kind"] == "graphic":
            h, w = f["bits"].shape
            beside = any(r["kind"] == "text" and f["y"] <= r["y"] < f["y"] + h
                         and min(t["x"] for t in r["fields"]) >= f["x"] + w
                         for r in rows)
            if beside and w <= width // 3:
                _paste_bits(canvas, REFLOW_MARGIN, y, f["bits"])
                indent, indent_until = w + 2 * REFLOW_MARGIN, y + h
                continue
        if row["kind"] == "rule":
            y += REFLOW_RULE_GAP - REFLOW_ROW_GAP
            t = max(2, f["t"])
            draw.rectangle((0, y, width - 1, y + t - 1), fill=0)
            y += t + REFLOW_RULE_GAP
            continue
        if row["kind"] == "graphic":
            h, w = f["bits"].shape
            _paste_bits(canvas, (width - w) // 2, y, f["bits"])
            y += h
        elif row["kind"] == "qr":
            n = f["matrix"].shape[0]
            # Original module size, unless that would not fit the paper.
            m = max(1, min(f["mag"], width // (n + 2 * REFLOW_QR_QUIET_MODULES)))
            y += REFLOW_QR_QUIET_MODULES * m
            _paste_bits(canvas, (width - n * m) // 2, y, np.kron(f["matrix"], np.ones((m, m), bool)))
            y += (n + REFLOW_QR_QUIET_MODULES) * m
        elif row["kind"] == "barcode":
            total = sum(f["modules"])
            # Original module size, unless that would not fit the paper.
            m = max(1, min(f["module"], (width - 2 * REFLOW_MARGIN) // total))
            height = round(f["h"] * m / f["module"])
            _draw_barcode(draw, (width - total * m) // 2, y, f["modules"], m, height)
            y += height
        else:
            centered = all(fld["block"] and fld["block"][3] == "C" for fld in row["fields"])
            left = REFLOW_MARGIN + (indent if y < indent_until else 0)
            avail = width - left - REFLOW_MARGIN
            for line in _layout_text_row(row, font_path, avail):
                line_h = max(seg[1] for seg in line)
                widths = [ImageFont.truetype(font_path, h).getlength(t) * w / h
                          for t, h, w, _ in line]
                spaces = [ImageFont.truetype(font_path, h).getlength(" ") * w / h
                          for _, h, w, _ in line]
                total = sum(widths) + sum(spaces[1:])
                x = left + (round((avail - total) / 2) if centered else 0)
                for i, (text, h, w, bold) in enumerate(line):
                    if i:
                        x += spaces[i]
                    seg_y = y + line_h - h  # align segments at the bottom
                    _draw_text(canvas, round(x), seg_y, text, h, w, font_path)
                    if bold:
                        _draw_text(canvas, round(x) + 1, seg_y, text[:bold], h, w, font_path)
                    x += widths[i]
                y += line_h + REFLOW_LINE_GAP
            y -= REFLOW_LINE_GAP
        y += REFLOW_ROW_GAP
    y = max(y, indent_until)
    return canvas.crop((0, 0, width, max(1, y)))


def _trim(img):
    ink = np.argwhere(img)
    if not len(ink):
        return img[:0, :0]
    (y0, x0), (y1, x1) = ink.min(0), ink.max(0)
    return img[y0:y1 + 1, x0:x1 + 1]


def split_columns(img):
    '''Splits side-by-side stickers using the blank vertical bands between them.'''
    cols = img.any(axis=0)
    segments, start, blank = [], None, 0
    for x, has_ink in enumerate(cols):
        if has_ink:
            if start is None:
                start = x
            blank = 0
        elif start is not None:
            blank += 1
            if blank >= COLUMN_GAP:
                segments.append((start, x - blank + 1))
                start, blank = None, 0
    if start is not None:
        segments.append((start, len(cols) - blank))
    return [_trim(img[:, a:b]) for a, b in segments]


def zpl_to_img(zpl, print_width, layout="reflow"):
    '''Returns a single boolean image (True = black) with every sticker stacked.

    Labels wider than the paper are either rebuilt as one full-width column
    (layout="reflow") or shrunk as drawn (layout="scale").
    '''
    font_path = find_font()
    if font_path is None:
        raise RuntimeError("No TrueType font found to draw the label's text.")

    stickers = []
    for cmds in parse_labels(zpl):
        fields = parse_fields(cmds)
        label_stickers = split_columns(np.array(render_label(fields, font_path)) < 128)
        widest = max((s.shape[1] for s in label_stickers), default=0)
        if widest > print_width and layout == "reflow":
            logger.info(f"ℹ️ Label is {widest} dots wide; laying it out again to fit.")
            label_stickers = [np.array(reflow_label(fields, font_path, print_width)) < 128]
        elif widest > print_width:
            scale = print_width / widest
            logger.info(f"ℹ️ Label is {widest} dots wide; scaling it to {scale:.0%} to fit.")
            # Rounding can still overshoot by a dot or two; shrink a bit more until it fits.
            while True:
                label_stickers = split_columns(
                    np.array(render_label(fields, font_path, scale)) < 128)
                if max(s.shape[1] for s in label_stickers) <= print_width:
                    break
                scale *= 0.99
        stickers.extend(label_stickers)
    if not stickers:
        raise RuntimeError("The ZPL file has no printable labels.")

    rows = []
    for sticker in stickers:
        h, w = sticker.shape
        left = (print_width - w) // 2
        padded = np.zeros((h, print_width), dtype=bool)
        padded[:, left:left + w] = sticker
        rows += [padded, np.zeros((STICKER_GAP, print_width), dtype=bool)]
    logger.info(f"✅ Rendered {len(stickers)} sticker(s) from ZPL")
    return np.vstack(rows[:-1])


def is_zpl(filename):
    try:
        with open(filename, "rb") as f:
            return f.read(512).lstrip().startswith(b"^XA")
    except OSError:
        return False
