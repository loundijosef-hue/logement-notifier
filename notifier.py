#!/usr/bin/env python3
"""
Notificateur logements Île-de-France — Crous + ARPEJ.

Vérifie les logements disponibles et envoie une notification pour chaque
NOUVEAU logement (ou résidence ARPEJ qui gagne des chambres libres).

Sources :
  - Crous : API de trouverunlogement.lescrous.fr (POST /api/fr/search/<tool>)
  - ARPEJ : API WordPress arpej.fr (/wp-json/sn/residences), qui ne liste que
            les résidences ayant au moins une chambre disponible.

Notifications (configurer au moins une, via variables d'environnement) :
  NTFY_TOPIC          -> push sur ton téléphone via l'app ntfy (gratuit, sans compte)
  TELEGRAM_BOT_TOKEN  + TELEGRAM_CHAT_ID -> message Telegram

Filtres optionnels :
  DEPTS               départements surveillés (défaut : 75,92,93,94)
  MAX_RENT            loyer max en € (ex: 600)
  CROUS_MODES         alone,house_sharing,couple (vide = tous)
  ARPEJ_SKIP_RESTRICTED=0  inclut aussi les résidences réservées aux écoles partenaires (ignorées par défaut)
  NOTIFY_ON_FIRST_RUN=1    envoie un récap de l'existant au 1er lancement

Usage :  python3 notifier.py            (une vérification)
         python3 notifier.py --test     (envoie une notif de test)
         python3 notifier.py --dry-run  (affiche sans notifier ni sauvegarder)
         python3 notifier.py --demo     (envoie 1 vraie offre Crous + 1 ARPEJ au format alerte)
"""

import base64
import html as htmlmod
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

STATE_FILE = Path(os.environ.get("STATE_FILE", Path(__file__).with_name("state.json")))
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"

# Île-de-France : rectangle englobant (lon/lat) — celui utilisé par le site du Crous
IDF_BOUNDS = [{"lon": 1.4462445, "lat": 49.241431}, {"lon": 3.5592208, "lat": 48.1201456}]
IDF_DEPTS = ("75", "77", "78", "91", "92", "93", "94", "95")
# Départements à surveiller (Crous + ARPEJ) : par défaut Paris + petite couronne
DEPTS = tuple(d.strip() for d in (os.environ.get("DEPTS") or "75,92,93,94").split(",") if d.strip())
# Rectangle Paris + petite couronne, utilisé seulement si une adresse Crous n'a pas de code postal
INNER_BOX = (2.14, 48.68, 2.62, 48.99)  # lon_min, lat_min, lon_max, lat_max

CROUS_BASE = "https://trouverunlogement.lescrous.fr"
ARPEJ_BASE = "https://www.arpej.fr"

MAX_RENT = float(os.environ.get("MAX_RENT") or 0) or None
CROUS_MODES = {m.strip() for m in os.environ.get("CROUS_MODES", "").split(",") if m.strip()}
ARPEJ_SKIP_RESTRICTED = (os.environ.get("ARPEJ_SKIP_RESTRICTED") or "1") == "1"
NOTIFY_ON_FIRST_RUN = os.environ.get("NOTIFY_ON_FIRST_RUN", "1") == "1"

MODE_LABELS = {"alone": "seul", "house_sharing": "colocation", "couple": "couple"}


def _ssl_context():
    """Utilise le magasin de certificats de certifi s'il est installé (corrige
    CERTIFICATE_VERIFY_FAILED avec le Python de python.org sur Mac), sinon celui du système."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


SSL_CTX = _ssl_context()


# ---------------------------------------------------------------- HTTP utils
def http(url, data=None, headers=None, timeout=30, retries=2):
    h = {"User-Agent": UA, "Accept": "application/json, text/html;q=0.9", "Accept-Language": "fr-FR,fr;q=0.9"}
    h.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=h, method="POST" if body else "GET")
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"{url}: {last}")


# ---------------------------------------------------------------- Crous
def crous_tool():
    """L'identifiant d'outil change chaque année (48 en 2026-27) : on le lit sur la page d'accueil,
    avec l'année universitaire affichée à côté du lien (ex. « Pour cette année 2026-2027 »)."""
    page = http(CROUS_BASE + "/")
    if os.environ.get("CROUS_TOOL_ID"):
        tool = int(os.environ["CROUS_TOOL_ID"])
    else:
        ids = [int(x) for x in re.findall(r"/tools/(\d+)/search", page)]
        if not ids:
            raise RuntimeError("Crous : identifiant d'outil introuvable sur la page d'accueil")
        tool = max(ids)
    m = re.search(rf"/tools/{tool}/search[^>]*>[^<]*?(20\d\d)\D{{1,3}}(20\d\d)", page)
    return tool, (f"{m[1]}/{m[2]}" if m else "")


def fetch_crous():
    tool, year = crous_tool()
    items, page = [], 1
    while True:
        payload = {
            "idTool": tool, "need_aggregation": False, "page": page, "pageSize": 100,
            "sector": None, "occupationModes": [], "location": IDF_BOUNDS,
            "residence": None, "precision": 4, "equipment": [], "price": {"max": 10000000},
        }
        res = json.loads(http(f"{CROUS_BASE}/api/fr/search/{tool}", data=payload,
                              headers={"Origin": CROUS_BASE, "Referer": f"{CROUS_BASE}/tools/{tool}/search"}))
        batch = res["results"]["items"]
        items += batch
        total = res["results"]["total"]["value"]
        if not batch or len(items) >= total or page > 20:
            break
        page += 1

    out = {}
    for it in items:
        if not it.get("available", True):
            continue
        modes = it.get("occupationModes") or []
        if CROUS_MODES and not any(m["type"] in CROUS_MODES for m in modes):
            continue
        rents = [m["rent"]["min"] / 100 for m in modes if m.get("rent")]
        rent_min = min(rents) if rents else None
        rent_max = max(m["rent"]["max"] / 100 for m in modes if m.get("rent")) if rents else None
        if MAX_RENT and rent_min and rent_min > MAX_RENT:
            continue
        res_ = it.get("residence") or {}
        zips = re.findall(r"\b(\d{2})\s?\d{3}\b", res_.get("address", ""))
        if zips:
            if zips[-1] not in DEPTS:  # ex. Chartres (28) : dans le rectangle IDF mais hors zone
                continue
        else:
            loc = res_.get("location") or {}
            lon, lat = loc.get("lon"), loc.get("lat")
            if lon is None or not (INNER_BOX[0] <= lon <= INNER_BOX[2] and INNER_BOX[1] <= lat <= INNER_BOX[3]):
                continue
        area = (it.get("area") or {}).get("min")
        key = f"crous:{it['id']}"
        details = ", ".join(filter(None, [
            f"{area:.0f} m²" if area else None,
            "/".join(MODE_LABELS.get(m["type"], m["type"]) for m in modes),
        ]))
        out[key] = {
            "source": "Crous",
            "title": f"{it.get('label', 'Logement')} — {res_.get('label', '')}",
            "region": "Crous " + ((res_.get("entity") or {}).get("name") or "").strip(),
            "residence": res_.get("label", ""),
            "kind": " · ".join(filter(None, [it.get("label"), details])),
            "year": year,
            "address": res_.get("address", ""),
            "price": (f"{rent_min:.0f} €" if rent_min == rent_max else f"{rent_min:.0f}–{rent_max:.0f} €") if rent_min else "?",
            "details": details,
            "url": f"{CROUS_BASE}/tools/{tool}/accommodations/{it['id']}",
            "count": 1,
        }
    return out


# ---------------------------------------------------------------- ARPEJ
def fetch_arpej():
    out, page, pages = {}, 1, 1
    while page <= pages and page <= 20:
        res = json.loads(http(f"{ARPEJ_BASE}/wp-json/sn/residences?page={page}"))
        pages = int(res.get("pages") or 1)
        for r in res.get("residences", []):
            e = r.get("extra_data") or {}
            zip_code = str(e.get("zip_code", "")).strip()
            if not zip_code.startswith(DEPTS):
                continue
            rooms = int(e.get("available_rooms") or 0)
            if rooms <= 0 or e.get("is_bookable") is False:
                continue
            restricted = str(e.get("is_restricted")).lower() == "true"
            if restricted and ARPEJ_SKIP_RESTRICTED:
                continue
            try:
                price = float(e.get("price_from") or 0)
            except ValueError:
                price = 0
            if MAX_RENT and price and price > MAX_RENT:
                continue
            types = ", ".join(t.get("name", "") for t in (e.get("rooms") or []) if isinstance(t, dict))
            city = str(e.get("city", "")).strip()
            out[f"arpej:{r['ID']}"] = {
                "source": "ARPEJ",
                "title": f"Résidence {r.get('title', '')}",
                "region": "ARPEJ" + (f" {city}" if city else ""),
                "residence": f"Résidence {r.get('title', '')}",
                "kind": " · ".join(filter(None, [f"{rooms} chambre(s) dispo", types or None])),
                "warning": "Réservée aux écoles partenaires (code d'accès)" if restricted else None,
                "address": f"{e.get('address', '')} {zip_code} {e.get('city', '')}".strip(),
                "price": f"dès {price:.0f} €" if price else "?",
                "details": ", ".join(filter(None, [
                    f"{rooms} chambre(s) dispo",
                    types or None,
                    "⚠️ réservée écoles partenaires (code d'accès)" if restricted else None,
                ])),
                "url": r.get("link") or ARPEJ_BASE,
                "count": rooms,
            }
        page += 1
    return out


# ---------------------------------------------------------------- Notifications
def _telegram(token, chat, html_text, plain_text):
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    params = {"chat_id": chat, "text": html_text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
    try:
        urllib.request.urlopen(api, data=urllib.parse.urlencode(params).encode(), timeout=20, context=SSL_CTX).read()
    except urllib.error.HTTPError as e:
        if e.code != 400:  # 400 = HTML refusé par Telegram -> renvoi en texte brut
            raise
        params.pop("parse_mode")
        params["text"] = plain_text
        urllib.request.urlopen(api, data=urllib.parse.urlencode(params).encode(), timeout=20, context=SSL_CTX).read()


def notify(title, message, url=None, tg_html=None):
    """Envoie sur ntfy et/ou Telegram. tg_html : version mise en forme pour Telegram (facultative)."""
    sent = False
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        # ntfy accepte les en-têtes UTF-8 encodés en RFC 2047 (accents, emojis)
        headers = {"Title": "=?UTF-8?B?" + base64.b64encode(title.encode()).decode() + "?=",
                   "Tags": "house", "Priority": "high"}
        if url:
            headers["Click"] = url
        req = urllib.request.Request(f"{server}/{topic}", data=message.encode(), headers=headers, method="POST")
        urllib.request.urlopen(req, timeout=20, context=SSL_CTX).read()
        sent = True
    tg_token, tg_chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat:
        esc = htmlmod.escape
        plain = f"{title}\n{message}" + (f"\n{url}" if url else "")
        html_text = tg_html or (f"<b>{esc(title)}</b>\n{esc(message)}" + (f"\n{esc(url)}" if url else ""))
        _telegram(tg_token, tg_chat, html_text, plain)
        sent = True
    if not sent:
        print("[aucun canal configuré]", title, "|", message, "|", url or "")


def _price_month(item):
    return item["price"] if item["price"] == "?" else f"{item['price']}/mois"


def fmt(item):
    """Version texte (ntfy, console)."""
    lines = [item.get("kind") or item["details"]]
    if item.get("year"):
        lines.append(f"📅 {item['year']}")
    lines += [f"📍 {item['address']}", f"💶 {_price_month(item)}"]
    if item.get("warning"):
        lines.append(f"⚠️ {item['warning']}")
    return "\n".join(lines)


def fmt_telegram(item):
    """Fiche façon « alerte logement » : 🏠 région — résidence, 📅, 📍, 💶, lien."""
    esc = htmlmod.escape
    region = item.get("region") or item["source"]
    lines = [f"🏠 <b>{esc(region)}</b> — {esc(item.get('residence') or item['title'])}"]
    if item.get("kind"):
        lines.append(f"🛏 {esc(item['kind'])}")
    if item.get("year"):
        lines.append(f"📅 {esc(item['year'])}")
    lines += [f"📍 {esc(item['address'])}", f"💶 {esc(_price_month(item))}"]
    if item.get("warning"):
        lines.append(f"⚠️ {esc(item['warning'])}")
    where = "le Crous" if item["source"] == "Crous" else "ARPEJ"
    lines += ["", f'🔗 <a href="{esc(item["url"], quote=True)}">Voir l\'offre sur {where}</a>']
    return "\n".join(lines)


def notify_item(item):
    notify(f"🏠 Nouveau {item['source']} : {item.get('residence') or item['title']}", fmt(item),
           item["url"], tg_html=fmt_telegram(item))


# ---------------------------------------------------------------- Main
def main():
    dry = "--dry-run" in sys.argv
    if "--test" in sys.argv:
        notify("🏠 Test notificateur logement", "Si tu lis ça, les notifications marchent.", CROUS_BASE)
        return
    if "--demo" in sys.argv:  # envoie une vraie offre de chaque source, au format des alertes
        for fn in (fetch_crous, fetch_arpej):
            got = list(fn().values())
            if got:
                notify_item(got[0])
        return

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else None
    first_run = state is None
    seen = (state or {}).get("seen", {})

    current, errors = {}, []
    for name, fn in (("Crous", fetch_crous), ("ARPEJ", fetch_arpej)):
        try:
            got = fn()
            current.update(got)
            print(f"{name}: {len(got)} offre(s) en IDF")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
            print(f"ERREUR {name}: {e}", file=sys.stderr)
            # on garde l'ancien état de cette source pour ne pas re-notifier au retour
            prefix = name.lower() + ":"
            current.update({k: v for k, v in seen.items() if k.startswith(prefix)})

    new = [(k, v) for k, v in current.items() if k not in seen or v["count"] > seen[k].get("count", 0)]

    if dry:
        for k, v in current.items():
            flag = "NOUVEAU " if (k, v) in new else ""
            print(f"- {flag}[{v['source']}] {v['title']} | {fmt(v).replace(chr(10), ' | ')} | {v['url']}")
        return

    if first_run:
        if NOTIFY_ON_FIRST_RUN:
            n_c = sum(1 for v in current.values() if v["source"] == "Crous")
            n_a = sum(1 for v in current.values() if v["source"] == "ARPEJ")
            notify("🏠 Notificateur logement activé",
                   f"Actuellement en Île-de-France : {n_c} logement(s) Crous et {n_a} résidence(s) ARPEJ avec des chambres libres. "
                   "Tu seras prévenu à chaque nouveauté.", CROUS_BASE)
    else:
        for _, v in new[:15]:
            notify_item(v)
            time.sleep(1)  # limite d'envoi Telegram
        if len(new) > 15:
            notify("🏠 Encore plus de logements", f"+{len(new) - 15} autres nouveautés, va voir les sites !", CROUS_BASE)

    STATE_FILE.write_text(json.dumps({"updated": time.strftime("%Y-%m-%d %H:%M:%S"), "seen": current},
                                     ensure_ascii=False, indent=1, sort_keys=True))
    print(f"{len(new)} nouveauté(s)" + (f" — erreurs: {errors}" if errors else ""))


if __name__ == "__main__":
    main()
