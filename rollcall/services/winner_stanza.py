"""Render the FIRST AMONG HEROES stanza from a week's ode to a PNG image.

Used to syndicate the winner's stanza to Telegram + X with consistent
Substack-like typography, without relying on a hand-cropped screenshot.

Public API:
    extract_winner_stanza(ode_text) -> (winner_name, stanza_lines, header_text)
    render_winner_image(ode_text, output_path) -> output_path
"""
from __future__ import annotations

import re
from pathlib import Path


# Substack-leaning typography: black on white, serif body, sans-ish heavy heading.
# Macs ship Georgia which approximates Substack's body font close enough.
FONT_DIR = Path("/System/Library/Fonts/Supplemental")
HEADING_FONT = str(FONT_DIR / "Georgia Bold.ttf")
BODY_FONT = str(FONT_DIR / "Georgia.ttf")

# Layout constants tuned to look like the Substack screenshot you shared.
WIDTH = 1280
PADDING_X = 80
PADDING_TOP = 60
PADDING_BOTTOM = 80
HEADING_SIZE = 48
BODY_SIZE = 26
LINE_SPACING = 14         # extra pixels between body lines
STANZA_SPACING = 32       # extra pixels between stanzas (blank lines in source)
HEADING_BODY_GAP = 48     # gap below the heading
BG = (255, 255, 255)
FG = (0, 0, 0)


# The archive uses bold section headings, while older generated odes used
# Markdown ``##`` headings. Keep the social image renderer compatible with
# both forms.
HEADER_PATTERN = re.compile(
    r"^(?:##\s*|\*\*)\s*FIRST AMONG HEROES:\s*(.+?)\s*(?:\*\*)?\s*$",
    re.MULTILINE | re.IGNORECASE,
)

NEXT_SECTION_PATTERN = re.compile(
    r"^(?:##\s|\*\*.+\*\*\s*$|---\s*$)",
    re.MULTILINE,
)


def extract_winner_stanza(ode_text: str) -> tuple[str, list[str], str]:
    """Pull the FIRST AMONG HEROES section out of an ode.

    Returns (winner_name, body_lines, header_text). body_lines preserves blank
    lines as empty strings so the renderer can space stanzas.
    """
    match = HEADER_PATTERN.search(ode_text)
    if not match:
        raise ValueError("ode does not contain a 'FIRST AMONG HEROES:' heading")
    winner = match.group(1).strip()
    header_text = f"FIRST AMONG HEROES: {winner.upper()}"

    # Body = everything from end-of-header line up to the next section.
    body_start = match.end()
    rest = ode_text[body_start:]
    end_idx = len(rest)
    next_section = NEXT_SECTION_PATTERN.search(rest)
    if next_section:
        end_idx = next_section.start()
    body = rest[:end_idx].strip("\n")

    # Strip the Substack-style trailing double-space; we lay out lines ourselves.
    lines = [line.rstrip() for line in body.split("\n")]
    # Drop leading/trailing blank lines but preserve internal stanza breaks.
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return winner, lines, header_text


def _wrap_paragraph(draw, text: str, font, max_width: int) -> list[str]:
    """Greedy word-wrap that respects max_width in pixels."""
    if not text:
        return [""]
    words = text.split(" ")
    lines, current = [], ""
    for word in words:
        candidate = (current + " " + word).strip() if current else word
        w = draw.textlength(candidate, font=font)
        if w <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def render_winner_image(ode_text: str, output_path: str | Path) -> Path:
    """Render the FIRST AMONG HEROES stanza to a PNG at output_path."""
    from PIL import Image, ImageDraw, ImageFont

    winner, body_lines, header_text = extract_winner_stanza(ode_text)

    heading_font = ImageFont.truetype(HEADING_FONT, HEADING_SIZE)
    body_font = ImageFont.truetype(BODY_FONT, BODY_SIZE)

    # First pass on a throwaway image to measure heights with wrap.
    measure_img = Image.new("RGB", (WIDTH, 10), BG)
    measure = ImageDraw.Draw(measure_img)
    max_text_width = WIDTH - 2 * PADDING_X

    # Wrap heading (rarely needed but safe)
    heading_wrapped = _wrap_paragraph(measure, header_text, heading_font, max_text_width)

    # Wrap each body line; blanks remain blank.
    wrapped_body: list[str] = []
    for line in body_lines:
        if not line.strip():
            wrapped_body.append("")
            continue
        for w in _wrap_paragraph(measure, line, body_font, max_text_width):
            wrapped_body.append(w)

    # Compute total height
    h_ascent = heading_font.getbbox("Hg")[3]
    b_ascent = body_font.getbbox("Hg")[3]
    total_h = PADDING_TOP
    total_h += h_ascent * len(heading_wrapped)
    total_h += HEADING_BODY_GAP
    for line in wrapped_body:
        if line:
            total_h += b_ascent + LINE_SPACING
        else:
            total_h += STANZA_SPACING
    total_h += PADDING_BOTTOM

    # Real render
    img = Image.new("RGB", (WIDTH, total_h), BG)
    draw = ImageDraw.Draw(img)
    y = PADDING_TOP
    for hline in heading_wrapped:
        draw.text((PADDING_X, y), hline, font=heading_font, fill=FG)
        y += h_ascent
    y += HEADING_BODY_GAP
    for line in wrapped_body:
        if line:
            draw.text((PADDING_X, y), line, font=body_font, fill=FG)
            y += b_ascent + LINE_SPACING
        else:
            y += STANZA_SPACING

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, "PNG", optimize=True)
    return output_path
