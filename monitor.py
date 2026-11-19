"""
Watch Limited Edition Monitor
Controlla le pagine ufficiali dei brand, rileva link nuovi e
avvisa su Telegram se sembrano edizioni limitate.
"""
import json
import os
import re
import sqlite3
import time
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
    {"name": "Orient", "js": True,
     "urls": ["https://www.orient-watch.com/"]},
    {"name": "Seiko", "js": False,
     "urls": ["https://www.seikowatches.com/global-en/special/limited"]},
    {"name": "Swatch", "js": True,
     "urls": ["https://www.swatch.com/en-us/new-arrivals"]},
]


# ---------- database ----------
def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("""CREATE TABLE IF NOT EXISTS links (
        brand TEXT, url TEXT, title TEXT, first_seen TEXT,
        is_limited INTEGER, PRIMARY KEY (brand, url))""")
    db.execute("""CREATE TABLE IF NOT EXISTS editions (
        brand TEXT, url TEXT PRIMARY KEY, found_at TEXT, data TEXT)""")
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


# ---------- logica principale ----------
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


def main():
    db = init_db()
    for brand in BRANDS:
        try:
            check_brand(db, brand)
        except Exception as e:
            print(f"! {brand['name']} fallito: {e}")
    if _pw:
        _pw[1].close()
        _pw[0].stop()


if __name__ == "__main__":
    main()
