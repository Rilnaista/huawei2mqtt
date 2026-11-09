# CREDITS-ui

Das Portal-Frontend (`web/`) verwendet **keine Fremdbibliotheken**: statisches HTML, CSS und
Vanilla-JavaScript (ES-Module), kein Build-Schritt, keine CDN-Ressourcen, keine externen Schriften
(System-Font-Stack).

| Bestandteil | Herkunft | Lizenz |
|---|---|---|
| Icons (Inline-SVG-Sprite in `web/index.html`, `web/static/favicon.svg`) | eigene Zeichnungen | MIT (Projektlizenz) |
| Verlaufsdiagramm (`web/static/history.js`) und Awards (`web/static/awards.js`) | eigener Code, Diagramm als selbst gezeichnetes Inline-SVG, keine Diagramm-Bibliothek | MIT (Projektlizenz) |
| Übersetzungen `web/i18n/*.json` | eigene Texte | MIT (Projektlizenz) |
| `tests/ui/mock_server.py` | eigener Code, nur Python-Standardbibliothek | MIT (Projektlizenz) |
| Tests `tests/ui/test_i18n.py` | nutzt pytest (nur Entwicklung, nicht ausgeliefert) | pytest: MIT |
