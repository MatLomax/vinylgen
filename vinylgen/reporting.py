from __future__ import annotations

import math

from PIL import Image, ImageDraw, ImageFont


def create_report(flattened: Image.Image, palette: list[dict[str, object]]) -> Image.Image:
    """Append a black palette legend beneath a flattened image."""
    width, height = flattened.size
    margin = max(18, width // 70)
    column_width = max(245, width // 5)
    columns = max(1, width // column_width)
    rows = math.ceil(len(palette) / columns)
    title_height = 58
    row_height = 70
    band_height = title_height + rows * row_height + margin
    report = Image.new("RGB", (width, height + band_height), "black")
    report.paste(flattened, (0, 0))
    draw = ImageDraw.Draw(report)
    try:
        title_font = ImageFont.truetype("DejaVuSans-Bold.ttf", 24)
        item_font = ImageFont.truetype("DejaVuSansMono.ttf", 19)
    except OSError:
        title_font = ImageFont.load_default()
        item_font = ImageFont.load_default()
    top = height
    draw.text(
        (margin, top + 14),
        f"INFERRED PALETTE — {len(palette)} COLOURS — ORDERED BY IMAGE AREA",
        fill="white",
        font=title_font,
    )
    for index, colour in enumerate(palette):
        row, column = divmod(index, columns)
        x = margin + column * column_width
        y = top + title_height + row * row_height
        swatch_size = 44
        draw.rectangle(
            (x, y, x + swatch_size, y + swatch_size),
            fill=str(colour["hex"]),
            outline="white",
            width=1,
        )
        label = f'{colour["hex"]}  {float(colour["share_percent"]):.4f}%'
        draw.text((x + swatch_size + 12, y + 10), label, fill="white", font=item_font)
    return report


