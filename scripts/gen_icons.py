#!/usr/bin/env python3
"""Generate the PWA app icons from the brand mark.

The mark is the word "Rf" in Georgia Bold on the Readfine blue, the same thing
``backend/app/static/favicon.svg`` draws with a ``<text>`` element. Here the letters
are baked into a ``<path>`` instead, because nothing guarantees the machine rendering
an icon has Georgia: a ``<text>`` element would come out as whatever serif happened
to be around, or as nothing at all.

Both the SVG sources and the PNGs are laid out by the arithmetic below, so the two
cannot drift apart the way hand-exported assets do.

Georgia ships with Windows and macOS but not with Linux, so this is a run-it-on-your-
own-machine tool, not something CI can check. It needs two libraries the app itself
does not use:

    uv run --with fonttools --with pillow python scripts/gen_icons.py

Commit the regenerated PNGs and SVGs together. If you touch the geometry, look at the
results: an icon that is subtly wrong still passes every test we have.
"""
import argparse
import math
import sys
from pathlib import Path

from fontTools.misc.transform import Transform
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "backend" / "app" / "static" / "icons"

FONT_CANDIDATES = [
    Path("C:/Windows/Fonts/georgiab.ttf"),
    Path("/System/Library/Fonts/Supplemental/Georgia Bold.ttf"),
    Path("/Library/Fonts/Georgia Bold.ttf"),
    Path.home() / "Library/Fonts/Georgia Bold.ttf",
]

TEXT = "Rf"
BLUE = (59, 130, 246)      # #3b82f6, the favicon blue
SIZE = 512                 # everything is laid out at 512 and scaled down
RADIUS = 7 / 32 * SIZE     # favicon.svg's corner radius, scaled up
TRACKING_EM = -0.5 / 21    # favicon.svg: letter-spacing -0.5 at font-size 21
SS = 4                     # supersampling factor: draw big, shrink, get clean edges

# A plain icon keeps its own rounded square, so nothing crops it, but the word still
# wants margin: pushed wider it collides with the rounded corners. This is what a
# Windows desktop shortcut and the Chrome install dialog draw.
PLAIN_INK_WIDTH = 0.60 * SIZE
# A maskable one is cropped by the launcher, so the same width reads much bigger: the
# corners of the square are gone and the word ends up against the edge of the circle.
# This is picked by eye against a launcher-style crop, not derived.
MASKABLE_INK_WIDTH = 0.48 * SIZE
# The hard limit behind that choice: a launcher may crop to the circle inscribed in the
# middle 80%, and past it the word is cut off rather than merely tight.
SAFE_RADIUS = 0.40 * SIZE * 0.94


def find_font(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            sys.exit(f"No font at {path}")
        return path
    for candidate in FONT_CANDIDATES:
        if candidate.is_file():
            return candidate
    sys.exit(
        "Georgia Bold not found. It ships with Windows and macOS; on Linux, point\n"
        "--font at a copy of georgiab.ttf."
    )


class Mark:
    """The word "Rf" measured in one font, ready to be placed at any size."""

    def __init__(self, font_path: Path):
        self.path = font_path
        self.font = TTFont(font_path)
        self.upm = self.font["head"].unitsPerEm
        self.glyphs = self.font.getGlyphSet()
        cmap = self.font.getBestCmap()
        missing = [ch for ch in TEXT if ord(ch) not in cmap]
        if missing:
            sys.exit(f"{font_path.name} has no glyph for {missing}")
        self.names = [cmap[ord(ch)] for ch in TEXT]

    def _pens(self, font_size: float) -> list[float]:
        """Where each glyph's origin sits, relative to the first."""
        scale = font_size / self.upm
        tracking = TRACKING_EM * font_size
        pens, x = [], 0.0
        for i, name in enumerate(self.names):
            pens.append(x)
            x += self.font["hmtx"][name][0] * scale
            if i < len(self.names) - 1:
                x += tracking
        return pens

    def _ink(self, font_size: float) -> tuple[float, float, float, float]:
        """The word's inked box in font orientation (y up from the baseline).

        Not the advance width: Georgia's R kicks its leg out past its own advance,
        and the f leans over the same way, so centring on advances leaves the word
        visibly off to the left.
        """
        scale = font_size / self.upm
        xs, ys = [], []
        for name, pen_x in zip(self.names, self._pens(font_size)):
            bounds = BoundsPen(self.glyphs)
            self.glyphs[name].draw(bounds)
            x0, y0, x1, y1 = bounds.bounds
            xs += [pen_x + x0 * scale, pen_x + x1 * scale]
            ys += [y0 * scale, y1 * scale]
        return min(xs), min(ys), max(xs), max(ys)

    def place(self, font_size: float) -> tuple[list[float], float, float]:
        """Glyph origins plus the offset and baseline that centre the ink."""
        x0, y0, x1, y1 = self._ink(font_size)
        origin = SIZE / 2 - (x0 + x1) / 2
        baseline = SIZE / 2 + (y0 + y1) / 2
        return self._pens(font_size), origin, baseline

    def size_for_width(self, width: float) -> float:
        x0, _, x1, _ = self._ink(100.0)
        return 100.0 * width / (x1 - x0)

    def size_for_circle(self, radius: float, start: float) -> float:
        x0, y0, x1, y1 = self._ink(start)
        corner = math.hypot((x1 - x0) / 2, (y1 - y0) / 2)
        return start * min(1.0, radius / corner)

    def svg(self, font_size: float, rounded: bool) -> str:
        pens, origin, baseline = self.place(font_size)
        scale = font_size / self.upm
        parts = []
        for name, pen_x in zip(self.names, pens):
            pen = SVGPathPen(self.glyphs, ntos=lambda v: f"{v:.2f}".rstrip("0").rstrip("."))
            # font units -> px, and y flips: font space counts up, SVG counts down
            move = Transform().translate(origin + pen_x, baseline).scale(scale, -scale)
            self.glyphs[name].draw(TransformPen(pen, move))
            parts.append(pen.getCommands())
        rx = f' rx="{RADIUS:g}"' if rounded else ""
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SIZE} {SIZE}" '
            f'width="{SIZE}" height="{SIZE}">\n'
            f'  <rect width="{SIZE}" height="{SIZE}"{rx} fill="#3b82f6"/>\n'
            f'  <path d="{" ".join(parts)}" fill="#ffffff"/>\n'
            f"</svg>\n"
        )

    def png(self, font_size: float, rounded: bool) -> Image.Image:
        pens, origin, baseline = self.place(font_size)
        big = SIZE * SS
        image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        if rounded:
            draw.rounded_rectangle((0, 0, big - 1, big - 1), RADIUS * SS, fill=BLUE + (255,))
        else:
            draw.rectangle((0, 0, big - 1, big - 1), fill=BLUE + (255,))
        face = ImageFont.truetype(str(self.path), font_size * SS)
        for char, pen_x in zip(TEXT, pens):
            draw.text(
                ((origin + pen_x) * SS, baseline * SS), char,
                font=face, fill=(255, 255, 255, 255), anchor="ls",
            )
        return image


def write(mark: Mark, stem: str, font_size: float, rounded: bool) -> None:
    (OUT / f"{stem}.svg").write_text(mark.svg(font_size, rounded), encoding="utf-8")
    image = mark.png(font_size, rounded)
    for px in (512, 192):
        out = image.resize((px, px), Image.LANCZOS)
        # A maskable icon is opaque edge to edge; only the rounded one needs alpha.
        if not rounded:
            out = out.convert("RGB")
        out.save(OUT / f"{stem}-{px}.png", optimize=True)
    print(f"Wrote {stem}.svg, {stem}-512.png, {stem}-192.png (font-size {font_size:.1f})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--font", help="path to georgiab.ttf, if it is somewhere unusual")
    args = parser.parse_args()

    mark = Mark(find_font(args.font))
    write(mark, "icon", mark.size_for_width(PLAIN_INK_WIDTH), rounded=True)
    # Chosen size first, then clamped, so raising MASKABLE_INK_WIDTH too far cannot
    # quietly push the word outside what the crop guarantees.
    maskable = mark.size_for_circle(SAFE_RADIUS, mark.size_for_width(MASKABLE_INK_WIDTH))
    write(mark, "icon-maskable", maskable, rounded=False)


if __name__ == "__main__":
    main()
