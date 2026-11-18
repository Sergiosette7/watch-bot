"""Estrazione gratuita basata su regole (nessuna API a pagamento)."""
import re

MATERIALS = ["titanium", "titanio", "stainless steel", "acciaio", "bronze", "bronzo",
             "gold", "oro", "platinum", "platino", "ceramic", "ceramica", "carbon",
             "carbonio", "sapphire", "zaffiro", "silver", "argento"]
MOVEMENTS = ["automatic", "automatico", "manual", "carica manuale", "quartz", "quarzo",
             "spring drive", "tourbillon", "chronograph", "cronografo", "co-axial", "calibre", "caliber"]
CHANNELS = {
    "lottery": ["lottery", "raffle", "ballot", "lotteria", "sorteggio"],
    "boutique": ["boutique exclusive", "boutique only", "only in boutique", "solo in boutique"],
    "online": ["online exclusive", "online only", "esclusiva online", "available online"],
}
LIMITED = ["limited edition", "limited series", "numbered", "edizione limitata",
           "serie limitata", "unique piece"]


def _find(text, words):
    t = text.lower()
    return sorted({w for w in words if w in t})


def extract_watch(brand, url, text):
    t = text.lower()
    size = None
    for pat in [r"limited to (?:only )?(\d[\d,.']*)", r"(\d[\d,.']*)\s*(?:pieces|pezzi|exemplaires|units)",
                r"(\d[\d,.']*)\s*(?:numbered|esemplari)"]:
        m = re.search(pat, t)
        if m:
            digits = re.sub(r"\D", "", m.group(1))
            if digits and 1 < int(digits) < 100000:
                size = int(digits)
                break
    price, cur = None, None
    m = re.search(r"(€|eur|\$|usd|chf|£|gbp)\s?(\d{1,3}(?:[.,' ]\d{3})*)|(\d{1,3}(?:[.,' ]\d{3})+)\s?(€|eur|\$|usd|chf|£|gbp)", t)
    if m:
        cur = m.group(1) or m.group(4)
        raw = m.group(2) or m.group(3)
        price = int(re.sub(r"\D", "", raw))
    mm = re.search(r"(\d{2}(?:[.,]\d)?)\s?mm", t)
    channel = "unknown"
    for ch, words in CHANNELS.items():
        if _find(t, words):
            channel = ch
            break
    return {
        "is_limited_edition": bool(size) or bool(_find(t, LIMITED)),
        "model_name": None, "edition_size": size, "price": price, "currency": cur,
        "case_material": ", ".join(_find(t, MATERIALS)) or None,
        "case_size_mm": float(mm.group(1).replace(",", ".")) if mm else None,
        "movement": ", ".join(_find(t, MOVEMENTS)) or None,
        "release_date": None, "sales_channel": channel,
        "summary_it": text[:200].strip(), "confidence": "low",
    }


def format_alert(brand, url, d):
    v = lambda x, s="": f"{x}{s}" if x not in (None, "") else "n/d"
    return (f"⌚ {brand}: possibile edizione limitata\n"
            f"Tiratura: {v(d.get('edition_size'), ' pezzi')}\n"
            f"Prezzo: {v(d.get('price'))} {d.get('currency') or ''}\n"
            f"Cassa: {v(d.get('case_material'))} {v(d.get('case_size_mm'), ' mm')}\n"
            f"Movimento: {v(d.get('movement'))}\n"
            f"Canale: {d.get('sales_channel')}\n{url}")
