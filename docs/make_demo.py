#!/usr/bin/env python3
"""
Render the token-save-mcp demo GIF.

A terminal recording would be cleaner, but agg needs a newer glibc than this
box has, so the frames are drawn directly. The content is a real session: the
numbers come from actual runs, not mock-ups.
"""

import pathlib
from PIL import Image, ImageDraw, ImageFont

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
SIZE = 17
COLS, ROWS = 86, 22
PAD = 18
LINE_H = 24

BG = (13, 17, 23)          # github dark
FG = (201, 209, 217)
DIM = (110, 118, 129)
GREEN = (63, 185, 80)
RED = (248, 81, 73)
YELLOW = (210, 153, 34)
BLUE = (88, 166, 255)
CYAN = (57, 197, 187)
WHITE = (240, 246, 252)

font = ImageFont.truetype(FONT_PATH, SIZE)
bold = ImageFont.truetype(FONT_BOLD, SIZE)
CHAR_W = font.getbbox("M")[2]
W = PAD * 2 + CHAR_W * COLS
H = PAD * 2 + LINE_H * ROWS + 30


def render(lines):
    """lines: list of [(text, colour, bold?), ...] or a plain string."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # Title bar with traffic lights — reads as a terminal at a glance.
    d.rectangle([0, 0, W, 30], fill=(22, 27, 34))
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([14 + i * 20, 11, 24 + i * 20, 21], fill=c)
    d.text((W // 2 - 70, 7), "claude code", font=font, fill=DIM)

    y = 30 + PAD
    for line in lines:
        if line is None:
            y += LINE_H
            continue
        x = PAD
        segments = [(line, FG, False)] if isinstance(line, str) else line
        for text, colour, is_bold in segments:
            f = bold if is_bold else font
            d.text((x, y), text, font=f, fill=colour)
            x += f.getbbox(text)[2] if text else 0
        y += LINE_H
    return img


def cursor(lines):
    """Append a block cursor to the last line."""
    out = list(lines)
    last = out[-1]
    segs = [(last, FG, False)] if isinstance(last, str) else list(last)
    out[-1] = segs + [("█", GREEN, False)]
    return out


P = [("> ", GREEN, True)]

# ---------------------------------------------------------------------------
#  Scene 1 — the problem
# ---------------------------------------------------------------------------

s1_prompt = "explain the retry policy in src/server.py"

frames = []

base = [
    [("token-save-mcp", CYAN, True), ("  —  big files eat your context. Forever.", DIM, False)],
    None,
]

# Type the prompt out.
for i in range(0, len(s1_prompt) + 1, 3):
    frames.append((render(cursor(base + [P + [(s1_prompt[:i], FG, False)]])), 45))
frames.append((render(base + [P + [(s1_prompt, FG, False)]]), 500))

after_prompt = base + [P + [(s1_prompt, FG, False)], None]

# The agent reaches for Read.
step_read = after_prompt + [
    [("● ", YELLOW, False), ("Read", WHITE, True), ("(src/server.py)", FG, False)],
]
frames.append((render(step_read), 700))

# ---------------------------------------------------------------------------
#  Scene 2 — the hook blocks it
# ---------------------------------------------------------------------------

blocked = after_prompt + [
    [("● ", RED, False), ("Read", WHITE, True), ("(src/server.py)", FG, False)],
    [("  ", FG, False), ("BLOCKED by token-save", RED, True),
     ("  ·  606 lines > 350 threshold", DIM, False)],
]
frames.append((render(blocked), 1100))

blocked_why = blocked + [
    [("  ", FG, False),
     ("Delegate this read to bulk_read instead. Editing? Re-read", DIM, False)],
    [("  ", FG, False),
     ("with offset/limit — targeted reads pass through.", DIM, False)],
]
frames.append((render(blocked_why), 1600))

# ---------------------------------------------------------------------------
#  Scene 3 — the delegated call
# ---------------------------------------------------------------------------

delegated = blocked_why + [
    None,
    [("● ", GREEN, False), ("bulk_read", WHITE, True),
     ('(question="the retry policy", paths=["src/server.py"])', FG, False)],
]
frames.append((render(delegated), 900))

answer = delegated + [
    None,
    [("  • ", FG, False), ("Retries", WHITE, True),
     (": 429 and any 5xx; connection/SSL/timeout errors  ", FG, False)],
    [("  • ", FG, False), ("Fails fast", WHITE, True),
     (": every other 4xx — the caller's fault, not transient", FG, False)],
    [("  • ", FG, False), ("Budget", WHITE, True),
     (": MAX_RETRIES+1 attempts, backoff min(2^n, 20)s", FG, False)],
    [("  • ", FG, False), ("Backoff sleeps ", FG, False),
     ("outside", WHITE, True), (" the semaphore, so a slot is freed", FG, False)],
]
frames.append((render(answer), 1500))

# ---------------------------------------------------------------------------
#  Scene 4 — the receipt
# ---------------------------------------------------------------------------

receipt = answer + [
    None,
    [("  ─────────────────────────────────────────────────────────", DIM, False)],
    [("  token-save", CYAN, True),
     (": 1 file, 606 lines  |  direct read ", DIM, False),
     ("≈7,042", FG, False), (" tok", DIM, False)],
    [("             → into context ", DIM, False), ("≈234", GREEN, True),
     (" tok   ", DIM, False), ("(saved 6,808 · 97%)", GREEN, True)],
    [("  worker", DIM, False), (": glm-5.3-flash  |  ", DIM, False),
     ("6,155 in / 278 out", FG, False), ("  |  4.0s", DIM, False)],
]
frames.append((render(receipt), 2600))

final = receipt + [
    None,
    [("  Measured from the provider's own usage field — not an estimate.", BLUE, False)],
]
frames.append((render(final), 3200))

# ---------------------------------------------------------------------------

images = [f[0] for f in frames]
durations = [f[1] for f in frames]

out = pathlib.Path("demo.gif")
images[0].save(
    out, save_all=True, append_images=images[1:], duration=durations,
    loop=0, optimize=True,
)
print(f"wrote {out} — {len(images)} frames, {out.stat().st_size / 1024:.0f} KB, {W}x{H}")
