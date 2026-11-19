"""
Watch Limited Edition Monitor
1) Controlla le pagine ufficiali dei brand e rileva link nuovi.
2) Controlla i feed RSS delle riviste per anticipare le uscite.
Avvisa su Telegram se sembrano edizioni limitate.
"""
import json
import os
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from extractor import extract_watch, format_alert

DB_PATH = "watches.db"
MAX_NEW_LINKS_TO_CHECK = 20
REQUEST_DELAY = 2
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

KEYWORDS = [
    "limited edition", "limited-edition", "limited series", "numbered",
    "pieces", "special edition", "edizione limitata", "serie limitata",
    "exemplaires", "pezzi", "boutique edition", "online exclusive",
    "only watch", "anniversary edition", "unique piece",
    "pre-order", "preorder", "small batch",
]

# js=True usa il browser (siti pesanti), js=False è più veloce.
BRANDS = [
    {"name": "Venezianico", "js": False,
     "urls": ["https://venezianico.com/collections/limited-editions",
              "https://venezianico.com/"]},
    {"name": "Kurono Tokyo", "js": False,
     "urls": ["https://kuronotokyo.com/"]},
    {"name": "Out of Order", "js": False,
     "urls": ["https://outoforderwatches.com/"]},
    {"name": "Kuoe Kyoto", "js": True,
     "urls": ["https://www.kuoe-en.com/"]},
    {"name": "Orient", "js": False,
     "urls": ["https://shop.orientwatchusa.com/collections/limited-edition-watches"]},
    {"name": "Seiko", "js": False,
     "urls": ["https://www.seikowatches.com/global-en/special/limited"]},
    {"name": "Swatch", "js": True,
     "urls": ["https://www.swatch.com/en-us/new-arrivals"]},
]

# ---------- riviste (feed RSS) ----------
# Se un feed non risponde, il bot lo salta e continua con gli altri.
FEEDS = [
    ("Monochrome", "https://monochrome-watches.com/feed/"),
    ("Fratello", "https://fratellowatches.com/feed/"),
    ("Time+Tide", "https://timeandtidewatches.com/feed/"),
    ("Worn & Wound", "https://wornandwound.com/feed/"),
    ("A Blog To Watch", "https://www.ablogtowatch.com/feed/"),
    ("Hodinkee", "https://www.hodinkee.com/articles/rss.xml"),
]

# Brand che ti interessano: gli articoli che li citano arrivano con una ⭐.
# Aggiungi qui altri microbrand quando vuoi (minuscolo).
WATCH_BRANDS = [
    "venezianico", "kurono", "out of order", "kuoe", "orient", "seiko",
    "swatch", "baltic", "unimatic", "lorier", "nodus", "farer", "serica",
    "studio underd0g", "zelos", "dan henry",
]

# Brand fuori budget: gli articoli che parlano SOLO di questi vengono ignorati.
# Aggiungi (minuscolo) quelli che non ti interessano, per esempio "omega".
IGNORE_BRANDS = [
    "rolex", "patek", "audemars", "richard mille", "vacheron", "a. lange",
    "lange & söhne", "cartier", "hublot", "breguet", "blancpain",
    "jaeger-lecoultre", "f.p. journe", "greubel", "h. moser", "urwerk",
    "mb&f", "van cleef", "chopard", "piaget",
]

# Segnali di edizione limitata (valgono per QUALSIASI brand).
FEED_PATTERNS = [
    r"\blimited[- ](?:edition|series|run|release|production)\b",
    r"\blimited to\b",
    r"\bpre-?orders?\b",
    r"\bsmall[- ]batch\b",
    r"\bnumbered\b",
    r"\b\d[\d,.']*\s+(?:pieces|units|examples|pezzi)\b",
    r"\bedizione limitata\b",
    r"\bserie limitata\b",
]
MAX_FEED_ALERTS = 15  # tetto di sicurezza per ogni giro


# ---------- database ----------
def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("""CREATE TABLE IF NOT EXISTS links (
        brand TEXT, url TEXT, title TEXT, first_seen TEXT,
        is_limited INTEGER, PRIMARY KEY (brand, url))""")
    db.execute("""CREATE TABLE IF NOT EXISTS editions (
        brand TEXT, url TEXT PRIMARY KEY, found_at TEXT, data TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS feed_items (
        url TEXT PRIMARY KEY, feed TEXT, title TEXT, seen TEXT)""")
    return db


# ---------- fetch ----------
_pw = None


def fetch_static(url):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    return r.text


def fetch_js(url):
    global _pw
    from playwright.sync_api import sync_playwright
    if _pw is None:
        p = sync_playwright().start()
        _pw = (p, p.chromium.launch(headless=True))
    page = _pw[1].new_page(user_agent=UA)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(6000)
        return page.content()
    finally:
        page.close()


def fetch(url, js):
    return fetch_js(url) if js else fetch_static(url)


# ---------- parsing ----------
def extract_links(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    host = urlparse(base_url).netloc.replace("www.", "")
    found = {}
    for a in soup.find_all("a", href=True):
        url = urljoin(base_url, a["href"]).split("#")[0].split("?")[0]
        if urlparse(url).netloc.replace("www.", "") != host:
            continue
        title = (a.get_text(" ", strip=True) or a.get("title") or "")[:150]
        if url not in found or len(title) > len(found[url]):
            found[url] = title
    return found


def looks_limited(text):
    t = text.lower()
    hits = [k for k in KEYWORDS if k in t]
    if re.search(r"limited to \d+|\d+\s*(pieces|pezzi|exemplaires)", t):
        hits.append("numero pezzi")
    return hits


def page_text(url, js):
    try:
        html = fetch(url, js)
    except Exception as e:
        print(f"  ! errore apertura {url}: {e}")
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()
    return soup.get_text(" ", strip=True)[:20000]


# ---------- notifiche ----------
def notify(text):
    token = os.getenv("TELEGRAM_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[ALERT]", text)
        return
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": text, "disable_web_page_preview": False},
        timeout=20,
    )
    if r.status_code != 200:
        print(f"  ! Telegram ha risposto {r.status_code}: {r.text[:200]}")


# ---------- controllo siti dei brand ----------
def check_brand(db, brand):
    name, js = brand["name"], brand["js"]
    seeded = db.execute("SELECT 1 FROM links WHERE brand=? LIMIT 1", (name,)).fetchone()
    all_links = {}
    for url in brand["urls"]:
        try:
            all_links.update(extract_links(fetch(url, js), url))
        except Exception as e:
            print(f"  ! {name}: errore su {url}: {e}")
        time.sleep(REQUEST_DELAY)

    known = {r[0] for r in db.execute("SELECT url FROM links WHERE brand=?", (name,))}
    new = {u: t for u, t in all_links.items() if u not in known}
    print(f"{name}: {len(all_links)} link, {len(new)} nuovi")

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for i, (url, title) in enumerate(new.items()):
        limited = 0
        if seeded:  # niente alert al primo giro
            hits = looks_limited(f"{title} {url}")
            text = ""
            if i < MAX_NEW_LINKS_TO_CHECK:
                text = page_text(url, js)
                time.sleep(REQUEST_DELAY)
                hits = hits or looks_limited(text)
            if hits and text:
                data = extract_watch(name, url, text)
                if data and data.get("is_limited_edition"):
                    limited = 1
                    db.execute("INSERT OR REPLACE INTO editions VALUES (?,?,?,?)",
                               (name, url, now, json.dumps(data, ensure_ascii=False)))
                    notify(format_alert(name, url, data))
        db.execute("INSERT OR IGNORE INTO links VALUES (?,?,?,?,?)",
                   (name, url, title, now, limited))
    db.commit()


# ---------- controllo riviste (RSS) ----------
def _text(el, tag):
    found = el.find(tag)
    return (found.text or "").strip() if found is not None and found.text else ""


def parse_feed(xml_text):
    """Ritorna una lista di (titolo, link, riassunto) da un feed RSS o Atom."""
    root = ET.fromstring(xml_text)
    items = []
    for it in root.iter("item"):
        items.append((_text(it, "title"), _text(it, "link"), _text(it, "description")))
    ns = "{http://www.w3.org/2005/Atom}"
    for en in root.iter(ns + "entry"):
        link = en.find(ns + "link")
        href = link.get("href", "") if link is not None else ""
        items.append((_text(en, ns + "title"), href, _text(en, ns + "summary")))
    return items


def _has(word, text):
    return re.search(r"(?<![\w])" + re.escape(word) + r"(?![\w])", text) is not None


def match_article(title, summary):
    """Ritorna (miei_brand, brand_ignorati, segnali trovati)."""
    clean = BeautifulSoup(summary, "html.parser").get_text(" ", strip=True)
    text = f"{title} {clean[:600]}".lower()
    mine = [b for b in WATCH_BRANDS if _has(b, text)]
    ignored = [b for b in IGNORE_BRANDS if _has(b, text)]
    signals = []
    for p in FEED_PATTERNS:
        m = re.search(p, text)
        if m:
            signals.append(m.group(0))
    return mine, ignored, signals


def check_feeds(db):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    sent = 0
    for feed_name, feed_url in FEEDS:
        try:
            items = parse_feed(fetch_static(feed_url))
        except Exception as e:
            print(f"  ! feed {feed_name}: {e}")
            continue
        seeded = db.execute("SELECT 1 FROM feed_items WHERE feed=? LIMIT 1",
                            (feed_name,)).fetchone()
        new = 0
        for title, link, summary in items:
            if not link or db.execute("SELECT 1 FROM feed_items WHERE url=?",
                                      (link,)).fetchone():
                continue
            new += 1
            db.execute("INSERT OR IGNORE INTO feed_items VALUES (?,?,?,?)",
                       (link, feed_name, title, now))
            if not seeded or sent >= MAX_FEED_ALERTS:  # primo giro silenzioso
                continue
            mine, ignored, signals = match_article(title, summary)
            if not signals:
                continue
            if ignored and not mine:  # solo brand fuori budget
                continue
            icon = "⭐" if mine else "📰"
            brand_line = f"Brand tuo: {', '.join(mine)}\n" if mine else ""
            notify(f"{icon} {feed_name}: possibile edizione limitata\n{title}\n"
                   f"{brand_line}Segnali: {', '.join(signals)}\n{link}")
            sent += 1
        db.commit()
        print(f"{feed_name} (rivista): {len(items)} articoli, {new} nuovi")


def main():
    db = init_db()
    for brand in BRANDS:
        try:
            check_brand(db, brand)
        except Exception as e:
            print(f"! {brand['name']} fallito: {e}")
    try:
        check_feeds(db)
    except Exception as e:
        print(f"! riviste fallite: {e}")
    if _pw:
        _pw[1].close()
        _pw[0].stop()


if __name__ == "__main__":
    main()
