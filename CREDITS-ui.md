# CREDITS-ui

Das Portal-Frontend (`web/`) verwendet **keine Fremdbibliotheken**: statisches HTML, CSS und
Vanilla-JavaScript (ES-Module), kein Build-Schritt, keine CDN-Ressourcen, keine externen Schriften
(System-Font-Stack).

| Bestandteil | Herkunft | Lizenz |
|---|---|---|
| Icons (Inline-SVG-Sprite in `web/index.html`, `web/static/favicon.svg`) | eigene Zeichnungen | MIT (Projektlizenz) |
| Verlaufsdiagramm (`web/static/history.js`), Ertragssäulen (`web/static/yields.js`) und Awards (`web/static/awards.js`) | eigener Code, Diagramme als selbst gezeichnetes Inline-SVG, keine Diagramm-Bibliothek | MIT (Projektlizenz) |
| PWA (`web/manifest.webmanifest`, `web/sw.js`) | eigener Code, nur Browser-APIs (Service Worker, Cache API), keine Bibliothek | MIT (Projektlizenz) |
| PWA-Icons (`web/static/icons/*.png`) | aus dem eigenen Logo `favicon.svg` gerendert (`tests/ui/make_icons.py`, nur Entwicklung, nutzt Playwright/Chrome) | MIT (Projektlizenz) |
| Übersetzungen `web/i18n/*.json` | eigene Texte | MIT (Projektlizenz) |
| `tests/ui/mock_server.py` | eigener Code, nur Python-Standardbibliothek | MIT (Projektlizenz) |
| Tests `tests/ui/test_i18n.py` | nutzt pytest (nur Entwicklung, nicht ausgeliefert) | pytest: MIT |
| Screenshots/Icons `tests/ui/make_screenshots.py`, `make_icons.py` | nutzen Playwright (Python) und Chrome (nur Entwicklung, nicht ausgeliefert) | Playwright 1.57.0: Apache-2.0 |
