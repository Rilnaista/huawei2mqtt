"""Tests fuer web/i18n: Vollstaendigkeit, verwendete Keys, deutsche Rechtschreibung."""
import json
import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "web"
LANGS = ["de", "en", "nl", "fr", "it", "es", "pl"]
NAMESPACES = ("app|a11y|common|nav|lang|auth|banner|ov|status|light|val|group|log|ctl|cmd|res|"
              "reason|err|login|logout|set|field|hist|aw|conn|sys|mem")


def load(lang):
    return json.loads((WEB / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def dicts():
    return {lang: load(lang) for lang in LANGS}


def test_files_are_utf8_without_bom():
    for lang in LANGS:
        raw = (WEB / "i18n" / f"{lang}.json").read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), lang
    for f in [WEB / "index.html", *(WEB / "static").glob("*.js"), *(WEB / "static").glob("*.css")]:
        assert not f.read_bytes().startswith(b"\xef\xbb\xbf"), f
        f.read_bytes().decode("utf-8")


def test_same_keys_in_all_languages(dicts):
    ref = set(dicts["de"])
    for lang in LANGS:
        assert set(dicts[lang]) == ref, (lang, ref ^ set(dicts[lang]))


def test_no_empty_values(dicts):
    for lang, d in dicts.items():
        for k, v in d.items():
            assert isinstance(v, str) and v.strip(), f"{lang}:{k}"


def test_placeholders_consistent(dicts):
    for k, v in dicts["de"].items():
        ref = set(re.findall(r"\{(\w+)\}", v))
        for lang in LANGS:
            assert set(re.findall(r"\{(\w+)\}", dicts[lang][k])) == ref, f"{lang}:{k}"


def used_keys():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    keys = set(re.findall(r'data-i18n(?:-[\w-]+)?="([^"]+)"', html))
    for js in (WEB / "static").glob("*.js"):
        src = js.read_text(encoding="utf-8")
        keys |= set(re.findall(rf"""['"]((?:{NAMESPACES})\.[\w.]*\w)['"]""", src))
    return keys


def test_used_keys_exist(dicts):
    missing = sorted(k for k in used_keys() if k not in dicts["de"])
    assert not missing, missing


def test_dynamic_key_groups_present(dicts):
    # dynamisch zusammengesetzte Keys (status.<key>, light.<name>.<state>, ...)
    for prefix in ("status.", "reason.", "field.", "group.", "cmd.", "light."):
        assert any(k.startswith(prefix) for k in dicts["de"]), prefix
    for name in ("inverter", "dongle", "mqtt", "write"):
        assert f"light.{name}" in dicts["de"]
    for key in ("standby_no_irradiation", "on_grid", "shutdown_command"):
        assert f"status.{key}" in dicts["de"]
    for key in ("shutdown_not_allowed", "rate_limited"):
        assert f"reason.{key}" in dicts["de"]
    for k in ("hist.preset.today", "hist.preset.24h", "hist.preset.days", "aw.rank.best_day", "aw.rank.best_week",
              "aw.rank.best_month", "aw.rank.best_year", "aw.rank.peak_power", "aw.rank.longest_day",
              "aw.rank.earliest_start"):
        assert k in dicts["de"], k
    for f in ("history_days", "feed_in_tariff", "currency", "co2_factor", "milestone_step_kwh"):
        assert f"field.{f}" in dicts["de"], f
    for f in ("poll_interval_live", "poll_interval_static", "log_level", "mqtt_retain", "mqtt_flat_topics",
              "huawei_host", "huawei_port", "huawei_unit_id", "mqtt_host", "mqtt_port", "mqtt_username",
              "mqtt_password", "mqtt_tls", "portal_password", "allow_shutdown", "base_topic"):
        assert f"field.{f}" in dicts["de"], f


def test_no_unused_keys(dicts):
    dynamic = ("status.", "reason.", "field.", "group.", "cmd.", "light.", "hist.preset.", "aw.rank.")
    used = used_keys()
    unused = [k for k in dicts["de"] if k not in used and not k.startswith(dynamic)]
    assert not unused, unused


def test_no_hardcoded_text_in_html():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    html = re.sub(r"<svg.*?</svg>|<script.*?</script>|<!--.*?-->", "", html, flags=re.S)
    # Textknoten ausserhalb von data-i18n-Elementen (ausser Produktname, Sprachnamen)
    allowed = {"huawei2mqtt", "Deutsch", "English", "Nederlands", "Français", "Italiano", "Español", "Polski",
               "DEBUG", "INFO", "WARNING", "ERROR", "–", "kWh", "kW", "W", "V", "A", "Hz", "%", "°C"}
    for m in re.finditer(r"<(\w+)([^>]*)>([^<]+)</\1>", html):
        tag, attrs, text = m.groups()
        text = text.strip()
        if not text or tag in ("title",) or "data-i18n" in attrs or text in allowed:
            continue
        pytest.fail(f"Fest verdrahteter Text im HTML: <{tag}> {text!r}")


# ---- Deutsche Rechtschreibung: keine ae/oe/ue/ss-Ersatzschreibweise ----
# Stammlisten typischer Ersatzschreibungen (immer falsch in nutzersichtbarem Text)
DENY_STEMS = [
    "fuer", "ueber", "zurueck", "aender", "pruef", "koenn", "moecht", "muess", "waehl", "waehr", "naech",
    "gross", "schliess", "heiss", "ausser", "strasse", "weiss", "spass", "bloss", "geraet", "verfueg",
    "unterstuetz", "loesch", "woech", "saeub", "gueltig", "begruess", "oeffn", "ungueltig", "zuerst",
    "stoer", "faell", "spaet", "traeg", "haeng", "erklaer", "wuensch", "ruecks",
    "Groess", "Maessig", "Gebuehr", "Menue", "Ueber", "Aender",
]
# Woerter, in denen "ae/oe/ue" legitim ist (Praefix-Vergleich, kleingeschrieben)
LEGIT_UE = ("aktuell", "neu", "steuer", "quelle", "queue", "frequenz", "sequenz", "konsequen", "dauer",
            "eventuell", "individuell", "manuell", "virtuell", "visuell", "sexuell", "residuell", "treue",
            "feuer", "blue", "statue", "revue", "venue", "tuer")
LEGIT_AE = ("israel", "michael", "aerosol", "aero")
LEGIT_OE = ("poesie", "koeffizient", "goethe", "poet", "boe")


def german_words(text):
    return re.findall(r"[A-Za-zÄÖÜäöüß]+", text)


def suspicious(word):
    w = word.lower()
    if any(s.lower() in w for s in DENY_STEMS):
        return True
    # "ue" nach a/e/q ist legitim (Mauer, neue, Steuer, Frequenz); Rest ueber Ausnahmeliste
    for pat, legit in ((r"(?<![aeq])ue", LEGIT_UE), (r"ae", LEGIT_AE), (r"oe", LEGIT_OE)):
        if re.search(pat, w) and not any(p in w for p in legit):
            return True
    return False


def test_german_has_no_ascii_umlaut_substitutes(dicts):
    bad = {}
    for k, v in dicts["de"].items():
        words = [w for w in german_words(re.sub(r"\{\w+\}", "", v)) if suspicious(w)]
        if words:
            bad[k] = words
    assert not bad, bad


def test_german_uses_real_umlauts(dicts):
    joined = " ".join(dicts["de"].values())
    for ch in "äöüß":
        assert ch in joined, ch


@pytest.mark.parametrize("word", ["fuer", "Aenderung", "schliessen", "grosse", "Geraet", "pruefen",
                                  "ueberpruefen", "Groesse", "zurueck", "Verfuegbarkeit"])
def test_detector_flags_substitutes(word):
    assert suspicious(word)


@pytest.mark.parametrize("word", ["Mauer", "Dauer", "aktuelle", "Netzfrequenz", "neue", "Steuerung",
                                  "Quelle", "Passwort", "muss", "dass", "Schließen", "Größe", "für", "Stärke"])
def test_detector_accepts_valid_words(word):
    assert not suspicious(word)
