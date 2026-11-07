"""Erzeugt die Doku-Bilder (docs/images/*.png, portal-tour.gif) aus dem Mock-Server.

Nutzt nur Mock-Daten (Fake-Seriennummer), niemals das echte Geraet.
Aufruf:  python tests/ui/make_screenshots.py     (benoetigt playwright + Chrome, ffmpeg fuer das GIF)
Optional: PNG-Optimierung per pngquant, falls installiert.
"""
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "images"
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
    page.goto(URL + "#overview")
    page.click("#btn-auth")
    page.fill("#login-pw", "demo")
    page.keyboard.press("Enter")
    page.wait_for_selector("#btn-auth-label:has-text('Abmelden')")


def shot(page, route, name, wait=1200):
    page.goto(URL + "#" + route)
    page.wait_for_timeout(wait)
    page.screenshot(path=str(OUT / name))
    print("ok", name)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    srv = subprocess.Popen([sys.executable, str(ROOT / "tests/ui/mock_server.py"), "--port", str(PORT),
                            "--shutdown-allowed"], stdout=subprocess.DEVNULL)
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
                shot(pg, "overview", f"overview-{scheme}.png", 1500)
                if scheme == "light":
                    shot(pg, "values", "values.png")
                    shot(pg, "log", "log.png")
                ctx.close()
            ctx = b.new_context(storage_state=str(state), **desk)
            shot(ctx.new_page(), "settings", "settings.png", 1500)
            ctx.close()
            ctx = b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, locale="de-DE")
            shot(ctx.new_page(), "overview", "mobile.png", 1500)
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
            pg.select_option("#lang", "en")
            pg.wait_for_timeout(1400)
            pg.click('a[data-route="settings"]')
            pg.wait_for_timeout(2200)
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
