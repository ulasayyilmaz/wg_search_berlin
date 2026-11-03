#!/usr/bin/env python3
"""Poll WG-Gesucht Berlin room offers and send matching ones to Telegram.

Criteria:
  * inside one of AREAS: 2 km of the meditation flat, or 1 km of the U1/U3 stations further west
  * total rent <= MAX_RENT
  * Anmeldung not ruled out (explicit "possible" or no mention is fine)
  * long term: no end date / Zwischenmiete / temporary, unless extension is possible
  * not female-only

Usage:
  python wg_checker.py            # loop forever, one check per INTERVAL seconds
  python wg_checker.py --once     # single check
  python wg_checker.py --seed     # mark current listings as seen without notifying
  python wg_checker.py --test URL # evaluate a single ad and print the verdict
"""

import argparse
import html
import json
import logging
import math
import os
import random
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------- config

MAX_RENT = int(os.environ.get("MAX_RENT", 650))
INTERVAL = int(os.environ.get("INTERVAL", 60))
MAX_PAGES = int(os.environ.get("MAX_PAGES", 5))
LATEST_START_STR = os.environ.get("LATEST_START", "01.12.2026")  # latest acceptable move-in date
STATE_FILE = Path(os.environ.get("STATE_FILE", Path(__file__).with_name("seen.json")))

# Coordinates from OpenStreetMap
STATIONS = {
    "Schlesisches Tor": (52.5013471, 13.4412846),
    "Görlitzer Bahnhof": (52.4988887, 13.4280477),
    "Kottbusser Tor": (52.4989901, 13.4183886),
    "Prinzenstraße": (52.4986978, 13.4067152),
    "Hallesches Tor": (52.4990257, 13.3917126),
    "Möckernbrücke": (52.4986927, 13.3831230),
}
MEDITATION_FLAT = (52.501670, 13.431872)  # Eisenbahnstraße 2, Wrangelkiez

# Where a room may be: (centre, radius km). The flat's 2 km circle (~10 min by bike) already
# contains the 1 km circles of Schlesisches Tor, Görlitzer Bhf and Kottbusser Tor; the stations
# further west are a few U1/U3 stops away.
AREAS = {
    "meditation flat": (MEDITATION_FLAT, 2.0),
    "U Prinzenstraße": (STATIONS["Prinzenstraße"], 1.0),
    "U Hallesches Tor": (STATIONS["Hallesches Tor"], 1.0),
    "U Möckernbrücke": (STATIONS["Möckernbrücke"], 1.0),
}

BASE = "https://www.wg-gesucht.de"
# Berlin (city_id 8), WG rooms, newest first, max rent pre-filtered server side
LIST_URL = BASE + "/wg-zimmer-in-Berlin.8.0.1.{page}.html?rMax={rmax}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}

log = logging.getLogger("wg")

# ---------------------------------------------------------------- text rules
# Each rule set is matched against lower-cased title + description.

# Anmeldung explicitly NOT possible -> reject
ANMELDUNG_NEGATIVE = [
    r"(keine|ohne)\s+(\w+\s+){0,2}(anmeldung|meldebescheinigung|wohnungsgeberbest)",
    r"anmeld\w*\s+(\w+\s+){0,4}(nicht|leider nicht|nicht mehr)\s+(\w+\s+){0,2}(möglich|moeglich|machbar|erlaubt|gestattet|drin|gehen)",
    r"anmeld\w*\s+(\w+\s+){0,3}(ist|sind|wäre|waere)\s+(leider\s+)?(nicht|keine)\b",
    r"(nicht|kein\w*)\s+(\w+\s+){0,3}anmelden\b",
    r"(anmeldung|registration)\s*[:?-]?\s*(nein|no|not possible|nicht möglich)\b",
    r"\banmelden\s+(\w+\s+){0,3}(nicht|geht nicht)\b",
    r"\bno\s+(\w+\s+){0,2}(anmeldung|registration|registering)",
    r"without\s+(\w+\s+){0,2}(anmeldung|registration)",
    r"(anmeldung|registration|registering|register)\s+(\w+\s+){0,4}(is\s+|are\s+)?(not|isn'?t|won'?t be|unfortunately not)\s+(\w+\s+){0,2}(possible|allowed|available|permitted|an option)",
    r"(can'?t|cannot|can not|won'?t be able to|not able to|unable to|not possible to)\s+(\w+\s+){0,3}(register|do (the |an )?anmeldung|get (an |the )?anmeldung)",
]

# Temporary rental signals -> reject unless an extension signal is present
TEMPORARY = [
    r"zwischenmiete", r"zwischenmieter", r"zwischen\s*-?\s*vermiet",
    r"untervermiet\w*\s+(\w+\s+){0,5}(für|for)\s+(\w+\s+){0,2}(monat|woche|month|week)",
    r"\bbefristet", r"\btemporär", r"\btemporar", r"\btemporary\b", r"\btemporarily\b",
    r"\bsublet\b", r"\bsublease\b", r"\bsubletting\b", r"\bshort[\s-]?term\b",
    r"\bkurzzeit", r"\bfür\s+(\w+\s+){0,2}(ein paar|einige|wenige|\d+)\s+(monate|wochen)\b",
    r"\bfor\s+(\w+\s+){0,2}(a few|few|some|\d+)\s+(months|weeks)\b",
]

# Extension / long-term possible -> accept a temporary ad after all
EXTENSION = [
    r"verlänger\w*\s+(\w+\s+){0,4}(möglich|moeglich|denkbar|nicht ausgeschlossen|option|machbar)",
    r"verlängerungsoption", r"option\s+(auf|zur)\s+verlängerung",
    r"(möglich|moeglich|eventuell|evtl\.?|vielleicht)\s+(\w+\s+){0,4}verlänger",
    r"(unbefristet|langfristig)\s+(\w+\s+){0,3}(möglich|moeglich|denkbar|option)",
    r"(übernahme|uebernahme|nachmieter\w*)\s+(\w+\s+){0,4}(möglich|moeglich|denkbar)",
    r"extension\s+(\w+\s+){0,4}(possible|available|option|negotiable)",
    r"(possib\w+|option|chance)\s+(\w+\s+){0,4}(to\s+)?(extend|extension|stay longer|long[\s-]?term)",
    r"(can|could|might|may)\s+(\w+\s+){0,3}(be\s+)?(extend|extended|prolong)",
    r"long[\s-]?term\s+(\w+\s+){0,3}(possible|option|negotiable)",
]


# Extension explicitly ruled out -> the ad stays temporary
NO_EXTENSION = [
    r"verlänger\w*\s+(\w+\s+){0,4}(ist\s+)?(leider\s+)?(nicht|ausgeschlossen|kein)",
    r"(keine|ohne)\s+(\w+\s+){0,2}verlänger",
    r"(nicht|kein\w*)\s+(\w+\s+){0,2}verlänger",
    r"extension\s+(\w+\s+){0,5}(is\s+|are\s+)?(not|isn'?t|won'?t|unfortunately not)\b",
    r"\bno\s+(\w+\s+){0,2}(extension|prolongation)",
    r"(can'?t|cannot|can not|won'?t|will not|not)\s+(\w+\s+){0,2}(be\s+)?(extend|prolong)",
]

# Room restricted to women / FLINTA -> reject
FEMALE_ONLY = [
    r"(ausschlie(ß|ss)lich|nur)\s+(\w+\s+){0,3}(an\s+|für\s+)?(eine\s+|einer\s+)?(\w+\s+)?(frau|frauen|weiblich\w*|mitbewohnerin(nen)?|mädels|mädchen|studentin(nen)?|flinta\w*)\b",
    r"\b(frauen|mädels|mädchen)[\s-]*(wg|haushalt)\b",
    r"\b(female|women|woman|girls?|ladies)[\s-]*(only|flat\s*share|flat|household|wg)\b",
    r"\b(only|exclusively)\s+(\w+\s+){0,3}(to\s+|for\s+)?(a\s+|an?\s+)?(\w+\s+)?(female|females|women|woman|girls?|ladies|flinta)\b",
    r"(looking for|searching for|seeking|suchen|sucht|suche)\s+(\w+\s+){0,3}(a\s+|an?\s+|eine\s+)?(\w+\s+)?(female|weiblich\w*)\b",
    r"(suchen|sucht|suche)\s+(\w+\s+){0,3}(eine\s+)(\w+\s+)?(mitbewohnerin|frau|studentin|nachmieterin|untermieterin)\b",
    r"(looking for|searching for|seeking)\s+(\w+\s+){0,3}(a\s+)(\w+\s+)?(woman|girl|lady)\b",
    r"\bflinta",
    r"\b(keine|no)\s+(männer|men|guys|jungs|boys)\b",
]


def parse_date(s):
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", s or "")
    return (int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None


LATEST_START = parse_date(LATEST_START_STR)


def _any(patterns, text):
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(0)
    return None


def anmeldung_ok(text):
    hit = _any(ANMELDUNG_NEGATIVE, text)
    return (hit is None), hit


def long_term_ok(text, has_end_date):
    temp = _any(TEMPORARY, text)
    if not temp and not has_end_date:
        return True, None
    no_ext = _any(NO_EXTENSION, text)
    ext = None if no_ext else _any(EXTENSION, text)
    reason = temp or "has 'frei bis' end date"
    if no_ext:
        reason += f" ('{no_ext}')"
    if ext:
        return True, f"{reason} but extension: '{ext}'"
    return False, reason


# ---------------------------------------------------------------- helpers


def haversine_km(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def nearest_station(coord):
    name, dist = min(((n, haversine_km(coord, c)) for n, c in STATIONS.items()), key=lambda x: x[1])
    return name, dist


def area_in_reach(coord, slack=0.0):
    """Name of the first area in AREAS whose radius (+slack) covers coord, or None."""
    for name, (centre, radius) in AREAS.items():
        if haversine_km(coord, centre) <= radius + slack:
            return name
    return None


class Blocked(Exception):
    pass


session = requests.Session()
session.headers.update(HEADERS)


BLOCK_MARKERS = ("dass sie ein mensch sind", "that you are human", "<title>überprüfung", "g-recaptcha", "cf-challenge")


def fetch(url):
    r = session.get(url, timeout=20)
    head = r.text[:30000].lower()
    if (r.status_code in (403, 429) or "cuba.html" in r.url or "captcha" in r.url.lower()
            or any(m in head for m in BLOCK_MARKERS)):
        raise Blocked(f"{r.status_code} at {r.url}")
    r.raise_for_status()
    return r.text


def clean(s):
    return re.sub(r"\s+", " ", html.unescape(s or "")).strip()


# ---------------------------------------------------------------- parsing


def parse_list(page_html):
    """Return list of dicts with id, url, title, price, size, details."""
    soup = BeautifulSoup(page_html, "html.parser")
    ads = []
    for a in soup.select("a.offer_list_item[data-id]"):
        href = a.get("href", "")
        if "wg-zimmer" not in href:
            continue
        price_el = a.select_one(".price-row .card-main-text")
        price = None
        if price_el:
            m = re.search(r"(\d+)", price_el.get_text().replace(".", ""))
            price = int(m.group(1)) if m else None
        size_el = a.select_one(".size-value")
        details = a.select_one(".card-details-text")
        badge = a.select_one(".user-badge")
        ads.append({
            "id": a["data-id"],
            "url": BASE + href if href.startswith("/") else href,
            "title": clean(a.get("aria-label")),
            "price": price,
            "size": clean(size_el.get_text()) if size_el else "",
            "details": clean(details.get("title")) if details else "",
            # "Privat", "Privat PRO", "Sofortbuchung" (partner sites), "Unternehmen PRO"
            "badge": clean(badge.get_text(" ")) if badge else "",
            # partner listings link back to the search page instead of an ad
            "partner": "asset_id=" in href,
        })
    return ads


def parse_ad(page_html):
    soup = BeautifulSoup(page_html, "html.parser")
    out = {}

    m = re.search(r'markers:\s*\[\{"lat":([\d.]+),"lng":([\d.]+)', page_html)
    out["coord"] = (float(m.group(1)), float(m.group(2))) if m else None

    addr = None
    for h in soup.select("h2.section_panel_title"):
        if h.get_text(strip=True) in ("Adresse", "Address"):
            span = h.find_next("span", class_="section_panel_detail")
            addr = clean(span.get_text(" ")) if span else None
            break
    out["address"] = addr

    # frei ab / frei bis
    out["free_from"] = out["free_until"] = None
    for d in soup.select("span.section_panel_detail"):
        label = d.get_text(strip=True).lower()
        if re.fullmatch(r"(frei|free|available)\s+(ab|from|bis|until|till|to):", label):
            val = d.find_next("span", class_="section_panel_value")
            v = clean(val.get_text()) if val else ""
            if re.search(r"\d{2}\.\d{2}\.\d{4}", v):
                until = re.search(r"\b(bis|until|till|to):", label)
                out["free_until" if until else "free_from"] = v

    # "Gesucht wird:" list
    wanted = []
    for h in soup.find_all("h3"):
        if re.search(r"gesucht wird|looking for|wanted|searching for", h.get_text().lower()):
            ul = h.find_next("ul")
            if ul:
                wanted = [clean(li.get_text(" ")) for li in ul.find_all("li")]
            break
    out["wanted"] = wanted

    texts = [clean(el.get_text(" ")) for el in soup.select(".section_freetext")]
    title_el = soup.find("h1")
    out["title"] = clean(title_el.get_text(" ")) if title_el else ""
    out["raw"] = out["title"] + "\n" + "\n".join(texts)
    out["text"] = out["raw"].lower()
    # a real ad always has a description; anything else is an error/verification page
    out["valid"] = bool(texts) and "ad_description_text" in page_html
    return out


def female_only_field(wanted):
    joined = " ".join(wanted).lower()
    if not joined:
        return False
    has_f = re.search(r"\bfrau|\bfrauen|\bweiblich|\bwom[ae]n|\bfemale", joined)
    has_m = re.search(r"\bmann|\bmänner|\bmaennlich|\bmännlich|\bm[ae]n\b|\bmale|egal|divers|irrelevant|any\b|doesn", joined)
    return bool(has_f and not has_m)


def female_only_text(text, raw):
    """Return the matching phrase if the description restricts the room to women/FLINTA."""
    for p in FEMALE_ONLY:
        for m in re.finditer(p, text):
            after = text[m.end():m.end() + 60]
            # "eine Mitbewohnerin oder einen Mitbewohner", "female or male" -> not restricted
            if re.match(r"\s*(,|oder|or|/|und|and|bzw\.?)\s*(\w+\s+){0,2}(mitbewohner\b|mann|männ|male|m[ae]n\b|jung|guy|boy)", after):
                continue
            # "MitbewohnerIn" is the gender-neutral form
            if "mitbewohnerin" in m.group(0) and "MitbewohnerIn" in raw[m.start():m.end() + 2]:
                continue
            return m.group(0)
    return None


def _nominatim(params):
    time.sleep(1.1)  # Nominatim allows 1 request/second
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={**params, "format": "json", "limit": 1, "countrycodes": "de"},
            headers={"User-Agent": "wg-checker/1.0 (personal use)"},
            timeout=15,
        )
        d = r.json()
        if d and 52.3 < float(d[0]["lat"]) < 52.7:  # must be in Berlin
            return float(d[0]["lat"]), float(d[0]["lon"])
    except Exception as e:
        log.warning("nominatim failed for %r: %s", params, e)
    return None


def _photon(q):
    """Second geocoder, in case Nominatim refuses the request."""
    try:
        r = requests.get("https://photon.komoot.io/api/", params={"q": q, "limit": 1, "lat": 52.5, "lon": 13.41},
                         headers={"User-Agent": "wg-checker/1.0 (personal use)"}, timeout=15)
        f = r.json().get("features")
        if f:
            lon, lat = f[0]["geometry"]["coordinates"]
            if 52.3 < lat < 52.7:
                return lat, lon
    except Exception as e:
        log.warning("photon failed for %r: %s", q, e)
    return None


def split_address(address):
    """'Oranienstraße180 10999 Berlin Kreuzberg' -> ('Oranienstraße 180', '10999', 'Kreuzberg')."""
    a = re.sub(r"\bn\.\s?v\.?", " ", address or "", flags=re.I)  # "n.v." = not specified
    # ad pages: "<street> <plz> Berlin <district>"; list-card fallback: "<street>, <district>"
    if re.search(r"\bBerlin\b", a):
        street, _, rest = a.partition(re.search(r"\bBerlin\b", a).group(0))
    else:
        street, _, rest = a.partition(",")
    m = re.search(r"\b1\d{4}\b", a)
    plz = m.group(0) if m else ""
    street = street.replace(plz, " ") if plz else street
    district = re.sub(r"-", " ", rest).strip(" ,")
    district = re.sub(r"\s+", " ", district)
    street = re.split(r"/|\bEcke\b|\bcorner\b|\bnähe\b|\bnear\b|\(", street, flags=re.I)[0]
    street = re.sub(r"([a-zäöüß.])(\d)", r"\1 \2", street)  # "Oranienstraße180" -> "Oranienstraße 180"
    street = re.sub(r"\bBerlin\b", " ", street)
    street = re.sub(r"\s+", " ", street).strip(" ,.-")
    if not re.search(r"[a-zäöüß]{3}", street, re.I):
        street = ""
    return street, plz, district


def geocode(address):
    """Fallback when the ad has no map marker. Returns (coord, precision) or (None, None)."""
    street, plz, district = split_address(address)
    tries = []
    if street:
        if plz:
            tries.append(("street", {"street": street, "postalcode": plz, "city": "Berlin"}))
        tries.append(("street", {"street": street, "city": "Berlin"} if not district
                      else {"q": f"{street}, {district}, Berlin"}))
    for precision, params in tries:
        c = _nominatim(params)
        if c:
            return c, precision
    if street:
        c = _photon(f"{street}, {plz} {district} Berlin".replace("  ", " "))
        if c:
            return c, "street"
    if plz:
        c = _nominatim({"q": f"{plz} Berlin"}) or _photon(f"{plz} Berlin")
        if c:
            return c, "postcode"
    return None, None


def evaluate(listing, ad):
    """Return (ok, reasons, info)."""
    info = {}
    if listing.get("partner") or (listing.get("badge") and not listing["badge"].startswith("Privat")):
        return False, [f"not private ({listing.get('badge') or 'partner'})"], info
    if not ad.get("valid", True):
        return False, ["ad page could not be read"], info
    address = ad["address"]
    if not address and "·" in listing.get("details", ""):
        # "3er WG · Berlin Kreuzberg · Mittenwalder Straße" -> "Mittenwalder Straße, Kreuzberg"
        # (street names repeat across Berlin, so the district matters)
        parts = [p.strip() for p in listing["details"].split("·")]
        district = re.sub(r"\bBerlin\b|-", " ", parts[-2]).strip() if len(parts) >= 3 else ""
        address = parts[-1] + (", " + district if district else "")
    coord, precision = ad["coord"], "pin"
    if not coord and address:
        coord, precision = geocode(address)
    if not coord:
        # no street and no postcode: a Kreuzberg ad could still be right next to a station,
        # so pass it on with a warning instead of dropping it
        # (plain "Kreuzberg" only; "Friedrichshain-Kreuzberg" is the whole borough)
        if re.search(r"(?<!friedrichshain.)kreuzberg", ((address or "") + " " + listing.get("details", "")).lower()):
            precision = "district"
        else:
            return False, [f"no location ({address or 'no address'})"], info
    info["precision"] = precision
    if precision == "district":
        info.update(station="?", dist=None)
        coord = None
    if coord:
        # a postcode centroid can be ~1 km off, so allow some slack there
        station, dist = nearest_station(coord)
        info.update(station=station, dist=dist, flat_km=haversine_km(coord, MEDITATION_FLAT))
        area = area_in_reach(coord, 0.5 if precision == "postcode" else 0)
        if not area:
            return False, [f"{info['flat_km']:.2f} km from flat, {dist:.2f} km from {station} ({precision})"], info
        info["area"] = area

    if listing.get("price") is not None and listing["price"] > MAX_RENT:
        return False, [f"rent {listing['price']}"], info

    if female_only_field(ad["wanted"]):
        return False, ["female only"], info
    hit = female_only_text(ad["text"], ad.get("raw", ""))
    if hit:
        return False, [f"female only: '{hit}'"], info

    start = parse_date(ad["free_from"])
    if start and start > LATEST_START:
        return False, [f"starts {ad['free_from']}"], info

    ok, hit = anmeldung_ok(ad["text"])
    if not ok:
        return False, [f"anmeldung: '{hit}'"], info
    info["anmeldung"] = "explicitly possible" if re.search(r"anmeld|registration", ad["text"]) else "not mentioned"

    ok, why = long_term_ok(ad["text"], bool(ad["free_until"]))
    if not ok:
        return False, [f"temporary: {why}"], info
    info["term_note"] = why
    return True, [], info


# ---------------------------------------------------------------- telegram


def send_telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False},
        timeout=20,
    )
    if not r.ok:
        log.error("telegram error %s: %s", r.status_code, r.text[:300])
    return r.ok


def format_message(listing, ad, info):
    e = html.escape
    apply_url = listing["url"] if "asset_id=" in listing["url"] else listing["url"].replace(BASE + "/", BASE + "/nachricht-senden/", 1)
    period = f"ab {ad['free_from'] or '?'}" + (f" bis {ad['free_until']}" if ad["free_until"] else " (unbefristet)")
    lines = [
        f"🏠 <b>{e(ad['title'] or listing['title'])}</b>",
        f"💶 {listing['price']} € · {e(listing['size'])}",
        f"📍 {e(ad['address'] or '')}",
        f"🧘 {info['flat_km']:.1f} km to the meditation flat (~{info['flat_km'] * 1.3 / 15 * 60:.0f} min by bike)"
        if info.get("dist") is not None else "🧘 ⚠️ exact location unknown (only 'Kreuzberg' given) — check the ad",
        *([f"🚇 {info['dist']*1000:.0f} m to U1/U3 {e(info['station'])}" + (
            "" if info.get("precision") == "pin" else f" (location from {info.get('precision')}, no map pin)")]
          if info.get("dist") is not None else []),
        f"📅 {e(period)}",
        f"📝 Anmeldung: {info['anmeldung']}",
    ]
    if info.get("term_note"):
        lines.append(f"⏳ {e(info['term_note'])}")
    # plain URLs: Telegram asks "Open link?" for hidden links, and visible
    # wg-gesucht.de URLs open straight in the WG-Gesucht app (universal links)
    lines += ["", "✉️ Apply:", apply_url, "", "🔗 Ad:", listing["url"]]
    return "\n".join(lines)


# ---------------------------------------------------------------- state & loop


def load_seen():
    try:
        return set(json.loads(STATE_FILE.read_text()))
    except (FileNotFoundError, ValueError):
        return set()


def save_seen(seen):
    # keep the file bounded; ids are increasing so keep the largest
    ids = sorted(seen, key=int)[-5000:]
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(ids))
    tmp.replace(STATE_FILE)


stats = {}
decisions = []  # this run's verdicts, appended to DECISIONS_FILE


def decide(listing, verdict, reasons=""):
    log.info("%-5s %s (%s) %s", verdict, listing["id"], reasons, listing["title"][:60])
    decisions.append({"time": time.strftime("%Y-%m-%d %H:%M", time.gmtime()), "id": listing["id"],
                      "verdict": verdict, "why": reasons, "title": listing["title"][:80]})


def save_decisions():
    """Keep the last 500 verdicts in a public file, so 'why wasn't X sent?' can be answered without logs."""
    path = os.environ.get("DECISIONS_FILE")
    if not path:
        return  # (always write, so the workflow's git add finds the file)
    try:
        old = json.loads(Path(path).read_text())
    except (FileNotFoundError, ValueError):
        old = []
    Path(path).write_text(json.dumps((old + decisions)[-500:], ensure_ascii=False, indent=0))
    decisions.clear()


def write_status(result):
    """Small public status file so a failing setup is visible without logs."""
    path = os.environ.get("STATUS_FILE")
    if not path:
        return
    Path(path).write_text(json.dumps(
        {"time": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), "result": result, **stats}, indent=1))


def check(seen, notify=True):
    listings = parse_list(fetch(LIST_URL.format(page=0, rmax=MAX_RENT)))
    if not listings:
        log.warning("no listings parsed - page layout may have changed")
        return
    new = [l for l in listings if l["id"] not in seen]
    # with minutes between runs new ads can spill onto later pages (pinned old
    # ads stay on page 1), so keep paging until a page brings nothing new
    page = 0
    while seen and new and page < MAX_PAGES - 1:
        page += 1
        time.sleep(random.uniform(1, 3))
        ids = {l["id"] for l in new}
        more = [l for l in parse_list(fetch(LIST_URL.format(page=page, rmax=MAX_RENT)))
                if l["id"] not in seen and l["id"] not in ids]
        if not more:
            break
        new += more

    log.info("%d listings, %d new", len(listings), len(new))
    stats.update(new=len(new), matched=0, skipped=0, unread=0)
    try:
        for l in new:
            if not notify:
                seen.add(l["id"])
                continue
            # cheap checks from the list card first, no extra request needed
            pre = None
            if l["price"] is not None and l["price"] > MAX_RENT:
                pre = f"rent {l['price']}"
            elif l["partner"] or (l["badge"] and not l["badge"].startswith("Privat")):
                pre = f"not private ({l['badge'] or 'partner'})"
            if pre:
                decide(l, "skip", pre)
                stats["skipped"] += 1
                seen.add(l["id"])
                continue
            time.sleep(random.uniform(2, 5))
            try:
                ad = parse_ad(fetch(l["url"]))
            except Blocked:
                raise
            except Exception as ex:
                log.warning("failed to load %s: %s", l["url"], ex)
                stats["unread"] += 1
                continue  # retry next round
            if not ad["valid"]:
                log.warning("unreadable ad page %s, retrying next round", l["url"])
                stats["unread"] += 1
                continue
            ok, reasons, info = evaluate(l, ad)
            where = ""
            if info.get("station"):
                d = info.get("dist")
                where = (f" [flat {info['flat_km']:.2f} km, U {info['station']} {d:.2f} km, {info.get('precision')}]"
                         if d is not None else " [location unknown]")
            if ok:
                if not send_telegram(format_message(l, ad, info)):
                    continue  # retry next round
                decide(l, "SENT", (info.get("term_note") or "") + where)
                stats["matched"] += 1
            else:
                decide(l, "skip", "; ".join(reasons) + ("" if " km from " in reasons[0] else where))
                stats["skipped"] += 1
            seen.add(l["id"])
    finally:
        save_seen(seen)
        save_decisions()


def load_env():
    env = Path(__file__).with_name(".env")
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main():
    load_env()
    global MAX_RENT, INTERVAL
    MAX_RENT = int(os.environ.get("MAX_RENT", MAX_RENT))
    INTERVAL = int(os.environ.get("INTERVAL", INTERVAL))
    global LATEST_START
    LATEST_START = parse_date(os.environ.get("LATEST_START", LATEST_START_STR))

    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    p.add_argument("--seed", action="store_true")
    p.add_argument("--test", metavar="URL")
    p.add_argument("--telegram-test", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.telegram_test:
        sys.exit(0 if send_telegram("✅ WG checker is connected.") else 1)

    if args.test:
        ad = parse_ad(fetch(args.test))
        price = None
        listing = {"url": args.test, "title": ad["title"], "price": price, "size": ""}
        ok, reasons, info = evaluate(listing, ad)
        print(json.dumps({"match": ok, "reasons": reasons, "info": info, "address": ad["address"],
                          "free_from": ad["free_from"], "free_until": ad["free_until"],
                          "wanted": ad["wanted"]}, ensure_ascii=False, indent=2))
        return

    seen = load_seen()
    if args.seed:
        check(seen, notify=False)
        log.info("seeded %d ids", len(seen))
        return

    backoff = INTERVAL
    while True:
        stats.clear()
        try:
            check(seen)
            backoff = INTERVAL
            write_status("ok")
        except Blocked as ex:
            backoff = min(backoff * 2, 1800)
            log.warning("blocked by wg-gesucht (%s); backing off %ds", ex, backoff)
            write_status("blocked")
        except Exception as ex:
            log.exception("check failed: %s", ex)
            write_status(f"error: {ex}")
        if args.once:
            break
        time.sleep(backoff + random.uniform(-5, 5))


if __name__ == "__main__":
    main()
