#!/usr/bin/env python3
"""Poll WG-Gesucht Berlin room offers and send matching ones to Telegram.

Criteria:
  * within RADIUS_KM of one of the U-Bahn stations in STATIONS
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
RADIUS_KM = float(os.environ.get("RADIUS_KM", 1.0))
INTERVAL = int(os.environ.get("INTERVAL", 60))
MAX_PAGES = int(os.environ.get("MAX_PAGES", 3))
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
    ext = _any(EXTENSION, text)
    reason = temp or "has 'frei bis' end date"
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


class Blocked(Exception):
    pass


session = requests.Session()
session.headers.update(HEADERS)


def fetch(url):
    r = session.get(url, timeout=20)
    if r.status_code in (403, 429) or "captcha" in r.url.lower() or "g-recaptcha" in r.text[:20000]:
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
        ads.append({
            "id": a["data-id"],
            "url": BASE + href if href.startswith("/") else href,
            "title": clean(a.get("aria-label")),
            "price": price,
            "size": clean(size_el.get_text()) if size_el else "",
            "details": clean(details.get("title")) if details else "",
        })
    return ads


def parse_ad(page_html):
    soup = BeautifulSoup(page_html, "html.parser")
    out = {}

    m = re.search(r'markers:\s*\[\{"lat":([\d.]+),"lng":([\d.]+)', page_html)
    out["coord"] = (float(m.group(1)), float(m.group(2))) if m else None

    addr = None
    for h in soup.select("h2.section_panel_title"):
        if h.get_text(strip=True) == "Adresse":
            span = h.find_next("span", class_="section_panel_detail")
            addr = clean(span.get_text(" ")) if span else None
            break
    out["address"] = addr

    # frei ab / frei bis
    out["free_from"] = out["free_until"] = None
    for d in soup.select("span.section_panel_detail"):
        label = d.get_text(strip=True).lower()
        if label in ("frei ab:", "frei bis:"):
            val = d.find_next("span", class_="section_panel_value")
            v = clean(val.get_text()) if val else ""
            if re.search(r"\d{2}\.\d{2}\.\d{4}", v):
                out["free_from" if label == "frei ab:" else "free_until"] = v

    # "Gesucht wird:" list
    wanted = []
    for h in soup.find_all("h3"):
        if "gesucht wird" in h.get_text().lower():
            ul = h.find_next("ul")
            if ul:
                wanted = [clean(li.get_text(" ")) for li in ul.find_all("li")]
            break
    out["wanted"] = wanted

    texts = [clean(el.get_text(" ")) for el in soup.select(".section_freetext")]
    title_el = soup.find("h1")
    out["title"] = clean(title_el.get_text(" ")) if title_el else ""
    out["text"] = (out["title"] + "\n" + "\n".join(texts)).lower()
    return out


def female_only(wanted):
    joined = " ".join(wanted).lower()
    if not joined:
        return False
    has_f = re.search(r"\bfrau|\bfrauen|\bweiblich", joined)
    has_m = re.search(r"\bmann|\bmänner|\bmaennlich|\bmännlich|egal|divers", joined)
    return bool(has_f and not has_m)


def geocode(address):
    """Fallback when the ad has no map marker."""
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": address + ", Berlin", "format": "json", "limit": 1},
            headers={"User-Agent": "wg-checker/1.0 (personal use)"},
            timeout=15,
        )
        d = r.json()
        if d:
            return float(d[0]["lat"]), float(d[0]["lon"])
    except Exception as e:
        log.warning("geocode failed for %r: %s", address, e)
    return None


def evaluate(listing, ad):
    """Return (ok, reasons, info)."""
    info = {}
    address = ad["address"]
    if not address and "·" in listing.get("details", ""):
        address = listing["details"].split("·")[-1].strip()  # street from the list card
    coord = ad["coord"] or (geocode(address) if address else None)
    if not coord:
        return False, ["no location"], info
    station, dist = nearest_station(coord)
    info.update(station=station, dist=dist)
    if dist > RADIUS_KM:
        return False, [f"{dist:.2f} km from {station}"], info

    if listing.get("price") is not None and listing["price"] > MAX_RENT:
        return False, [f"rent {listing['price']}"], info

    if female_only(ad["wanted"]):
        return False, ["female only"], info

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
    apply_url = listing["url"].replace(BASE + "/", BASE + "/nachricht-senden/", 1) if ad["coord"] else listing["url"]
    period = f"ab {ad['free_from'] or '?'}" + (f" bis {ad['free_until']}" if ad["free_until"] else " (unbefristet)")
    lines = [
        f"🏠 <b>{e(ad['title'] or listing['title'])}</b>",
        f"💶 {listing['price']} € · {e(listing['size'])}",
        f"📍 {e(ad['address'] or '')}",
        f"🚇 {info['dist']*1000:.0f} m to U {e(info['station'])}",
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
    for l in new:
        if not notify:
            seen.add(l["id"])
            continue
        if l["price"] is not None and l["price"] > MAX_RENT:
            seen.add(l["id"])
            continue
        time.sleep(random.uniform(1.5, 4))
        try:
            ad = parse_ad(fetch(l["url"]))
        except Blocked:
            raise
        except Exception as ex:
            log.warning("failed to load %s: %s", l["url"], ex)
            continue  # retry next round
        ok, reasons, info = evaluate(l, ad)
        if ok:
            log.info("MATCH %s %s", l["id"], l["title"])
            if not send_telegram(format_message(l, ad, info)):
                continue  # retry next round
        else:
            log.info("skip  %s (%s) %s", l["id"], "; ".join(reasons), l["title"][:60])
        seen.add(l["id"])
    save_seen(seen)


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
    global MAX_RENT, RADIUS_KM, INTERVAL
    MAX_RENT = int(os.environ.get("MAX_RENT", MAX_RENT))
    RADIUS_KM = float(os.environ.get("RADIUS_KM", RADIUS_KM))
    INTERVAL = int(os.environ.get("INTERVAL", INTERVAL))

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
        try:
            check(seen)
            backoff = INTERVAL
        except Blocked as ex:
            backoff = min(backoff * 2, 1800)
            log.warning("blocked by wg-gesucht (%s); backing off %ds", ex, backoff)
        except Exception as ex:
            log.exception("check failed: %s", ex)
        if args.once:
            break
        time.sleep(backoff + random.uniform(-5, 5))


if __name__ == "__main__":
    main()
