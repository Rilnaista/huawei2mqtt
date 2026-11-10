"""Erzeugt die Doku-Bilder (docs/images/*.png, portal-tour.gif) aus dem Mock-Server (rc4: inkl. Vergleich/Erträge, Warnhinweis).

Nutzt nur Mock-Daten (Fake-Seriennummer), niemals das echte Geraet.
Aufruf:  python tests/ui/make_screenshots.py     (benoetigt playwright + Chrome, ffmpeg fuer das GIF)
Optional: PNG-Optimierung per pngquant, falls installiert.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(os.environ.get("H2M_SHOTS_OUT") or ROOT / "docs" / "images")   # Ziel überschreibbar (Probelauf)
PORT = 8199
URL = f"http://127.0.0.1:{PORT}/"
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
LOG_LINES = [("INFO", "bridge", "huawei2mqtt gestartet"), ("INFO", "modbus", "Verbunden mit 192.168.1.45:502")]


def wait_up():
    for _ in range(50):
        try:
            urllib.request.urlopen(URL + "healthz", timeout=1)
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("Mock startet nicht")


def login(page):
    page.goto(URL + "#settings")  # Login-Button liegt in den Einstellungen
    page.click("#btn-auth")
    page.fill("#login-pw", "demo")
    page.keyboard.press("Enter")
    page.wait_for_selector("#btn-auth-label:has-text('Abmelden')")


def shot(page, route, name, wait=1200, countdown=False):
    page.goto(URL + "#" + route)
    page.wait_for_timeout(wait)
    if countdown:  # Countdown mit Zahl abwarten (nicht "Aktualisierung läuft")
        page.wait_for_function("/\\d+\\s*s/.test(document.getElementById('age').textContent)", timeout=15000)
    page.screenshot(path=str(OUT / name))
    print("ok", name)


def shot_range(page, preset, name, wait=1500):
    """Verlauf mit anderem Zeitraum (Knopf per Index in #hist-presets: y1 = '1 Jahr')."""
    page.goto(URL + "#history")
    page.wait_for_timeout(800)
    page.click(f"#hist-presets button:has-text('{preset}')")
    page.wait_for_timeout(wait)
    page.screenshot(path=str(OUT / name))
    print("ok", name)


def shot_compare(page, prefix):
    """rc4: Tagesverlauf mit Vorjahreslinien und Erträge (Säulen, Monat mit Vorjahren); stellt danach den Normalzustand her."""
    page.goto(URL + "#history")
    page.wait_for_timeout(800)
    page.click("#hist-presets button:nth-child(1)")   # Heute
    page.wait_for_timeout(600)
    page.select_option("#hist-cmp", "all")
    page.wait_for_timeout(1800)
    page.screenshot(path=str(OUT / f"{prefix}history-compare-light.png"))
    print("ok", f"{prefix}history-compare-light.png")
    page.click("#hist-mode button:nth-child(2)")      # Erträge
    page.wait_for_timeout(500)
    page.click("#yld-periods button:nth-child(2)")    # Monat
    page.wait_for_timeout(900)
    page.screenshot(path=str(OUT / f"{prefix}yields-light.png"))
    print("ok", f"{prefix}yields-light.png")
    page.click("#hist-mode button:nth-child(1)")
    page.select_option("#hist-cmp", "off")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    srv = subprocess.Popen([sys.executable, str(ROOT / "tests/ui/mock_server.py"), "--port", str(PORT),
                            "--shutdown-allowed", "--sun-now", "13:20", "--long", "--warnings"], stdout=subprocess.DEVNULL)
    tmp = Path(tempfile.mkdtemp())
    try:
        wait_up()
        with sync_playwright() as p:
            b = p.chromium.launch(channel="chrome")
            desk = {"viewport": {"width": 1280, "height": 800}, "locale": "de-DE"}
            # eingeloggten Zustand einmal herstellen
            ctx = b.new_context(**desk)
            pg = ctx.new_page()
            login(pg)
            state = tmp / "state.json"
            ctx.storage_state(path=str(state))
            ctx.close()

            for scheme in ("light", "dark"):
                ctx = b.new_context(color_scheme=scheme, **desk)
                pg = ctx.new_page()
                shot(pg, "overview", f"overview-{scheme}.png", 1500, countdown=True)
                if scheme == "light":
                    shot(pg, "values", "values.png")
                    shot(pg, "log", "log.png")
                shot(pg, "history", f"history-{scheme}.png", 2000)
                if scheme == "light":
                    shot_range(pg, "1 Jahr", "history-year-light.png")
                    shot_compare(pg, "")
                shot(pg, "awards", f"awards-{scheme}.png", 1500)
                ctx.close()
            ctx = b.new_context(storage_state=str(state), **desk)
            shot(ctx.new_page(), "settings", "settings.png", 1500)
            ctx.close()
            for scheme in ("light", "dark"):
                ctx = b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, locale="de-DE",
                                    color_scheme=scheme, has_touch=True, is_mobile=True)
                pg = ctx.new_page()
                if scheme == "light":
                    shot(pg, "overview", "mobile.png", 1500, countdown=True)
                shot(pg, "history", f"history-mobile-{scheme}.png", 2000)
                if scheme == "light":
                    shot_compare(pg, "mobile-")
                shot(pg, "awards", f"awards-mobile-{scheme}.png", 1500)
                ctx.close()

            # Tour als Video
            vdir = tmp / "video"
            ctx = b.new_context(storage_state=str(state), viewport={"width": 1100, "height": 700},
                                record_video_dir=str(vdir), record_video_size={"width": 1100, "height": 700},
                                locale="de-DE")
            pg = ctx.new_page()
            pg.goto(URL + "#overview")
            pg.wait_for_timeout(5600)  # Live-Aktualisierung (Poll 5 s) sichtbar
            pg.click('a[data-route="values"]')
            pg.wait_for_timeout(900)
            pg.click("#val-filter")
            pg.keyboard.type("pv1", delay=220)
            pg.wait_for_timeout(1400)
            pg.click('a[data-route="history"]')
            pg.wait_for_timeout(1800)
            pg.click('a[data-route="awards"]')
            pg.wait_for_timeout(1800)
            pg.click('a[data-route="settings"]')
            pg.wait_for_timeout(1200)
            pg.select_option("#lang", "en")  # Sprachwahl liegt in den Einstellungen
            pg.wait_for_timeout(1800)
            ctx.close()
            video = next(vdir.glob("*.webm"))
            b.close()
        gif = OUT / "portal-tour.gif"
        pal = tmp / "pal.png"
        vf = "fps=10,scale=1000:-1:flags=lanczos"
        subprocess.run([FFMPEG, "-y", "-v", "error", "-ss", "0.6", "-i", str(video), "-vf", vf + ",palettegen=max_colors=96",
                        str(pal)], check=True)
        subprocess.run([FFMPEG, "-y", "-v", "error", "-ss", "0.6", "-i", str(video), "-i", str(pal), "-lavfi",
                        vf + " [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=4", str(gif)], check=True)
        print("ok portal-tour.gif", gif.stat().st_size // 1024, "KB")
        if shutil.which("pngquant"):
            for f in OUT.glob("*.png"):
                subprocess.run(["pngquant", "--force", "--skip-if-larger", "--quality=70-95", "--output", str(f), str(f)])
    finally:
        srv.terminate()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
