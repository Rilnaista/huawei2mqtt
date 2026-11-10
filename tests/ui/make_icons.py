"""Erzeugt die PWA-Icons (web/static/icons/*.png) aus web/static/favicon.svg per Playwright/Chrome.

Aufruf: python3 tests/ui/make_icons.py   (einmalig bzw. wenn sich das Logo aendert)
  icon-192/512        Logo mit abgerundeten Ecken (purpose any)
  maskable-192/512    vollflaechig Amber, Sonne in der Safe-Zone (80 %) (purpose maskable)
  apple-touch-icon    180 x 180, vollflaechig (iOS rundet selbst)
"""
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
SVG = (ROOT / "web/static/favicon.svg").read_text(encoding="utf-8")
OUT = ROOT / "web/static/icons"
GLYPH = re.search(r"<g .*</g>", SVG, re.S).group(0)


def full_bleed(scale):
    """Quadrat ohne Rundung; Sonne um den Mittelpunkt auf `scale` verkleinert."""
    off = 12 * (1 - scale)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><rect width="24" height="24" fill="#f59e0b"/>'
            f'<g transform="translate({off} {off}) scale({scale})">{GLYPH}</g></svg>')


JOBS = [("icon-192.png", 192, SVG), ("icon-512.png", 512, SVG),
        ("maskable-192.png", 192, full_bleed(0.62)), ("maskable-512.png", 512, full_bleed(0.62)),
        ("apple-touch-icon.png", 180, full_bleed(0.8))]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome")
        for name, size, svg in JOBS:
            pg = b.new_page(viewport={"width": size, "height": size})
            pg.set_content(f'<html><body style="margin:0;background:transparent"><div style="width:{size}px;height:{size}px">'
                           f'{svg.replace("<svg ", "<svg width=%d height=%d " % (size, size), 1)}</div></body></html>')
            pg.screenshot(path=str(OUT / name), omit_background=True)
            pg.close()
            print("ok", name)
        b.close()


if __name__ == "__main__":
    main()
