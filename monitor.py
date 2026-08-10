#!/usr/bin/env python3
"""
Surveillance de l'ouverture des inscriptions MIUT (Madeira Island Ultra Trail).

Trois capteurs indépendants, tout déclenchement envoie une alerte Telegram :
  1. Flux RSS du blog  -> nouvel article contenant un mot-clé d'inscription
  2. Pages du site     -> apparition d'un mot-clé d'inscription dans le texte
  3. URLs candidates   -> une page d'inscription qui répond 200 alors qu'elle
                          était absente (404) au run précédent

Etat persisté dans state.json (recommité par le workflow GitHub Actions).
Un heartbeat hebdomadaire confirme que la surveillance tourne toujours.
"""

import hashlib
import json
import os
import re
import sys
import datetime as dt
from urllib.parse import urljoin

import requests

STATE_FILE = os.environ.get("MIUT_STATE", "state.json")
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

BASE = "https://miutmadeira.com"

WATCH_PAGES = [
    "https://miutmadeira.com/fr/home-fr/",
    "https://miutmadeira.com/",
    "https://miutmadeira.com/fr/levenement/",
]

FEEDS = [
    "https://miutmadeira.com/fr/category/fr/feed/",
    "https://miutmadeira.com/feed/",
]

# Pages qui n'existent pas encore mais apparaîtront à l'ouverture.
CANDIDATE_URLS = [
    "https://miutmadeira.com/fr/inscriptions/",
    "https://miutmadeira.com/fr/inscription/",
    "https://miutmadeira.com/registration/",
    "https://miutmadeira.com/registrations/",
    "https://miutmadeira.com/pt/inscricoes/",
    "https://miutmadeira.com/inscricoes/",
    "https://miutmadeira.com/fr/sinscrire/",
]

KEYWORDS = [
    r"inscription[s]?\s+(ouvert|open)",
    r"les inscriptions",
    r"s['’]inscrire",
    r"registration[s]?\s+(are\s+)?open",
    r"register\s+now",
    r"sign\s*up",
    r"inscri[çc][õo]es",
    r"registo",
    r"buy\s+your\s+bib",
    r"dossard",
]

KEY_RE = re.compile("|".join(KEYWORDS), re.IGNORECASE)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "fr,en;q=0.8",
}


# --------------------------------------------------------------------------- #
# Utilitaires
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)


def notify(text: str) -> None:
    print("ALERTE:", text)
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={
                "chat_id": TG_CHAT,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=20,
        )
    except requests.RequestException as exc:  # ne jamais faire échouer le run
        print("Echec envoi Telegram:", exc, file=sys.stderr)


def get(url: str, **kw):
    return requests.get(url, headers=HEADERS, timeout=30, **kw)


def visible_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;?", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------- #
# Capteurs
# --------------------------------------------------------------------------- #
def check_pages(state: dict, alerts: list) -> None:
    seen = state.setdefault("pages", {})
    for url in WATCH_PAGES:
        try:
            resp = get(url)
            if resp.status_code != 200:
                continue
            text = visible_text(resp.text)
        except requests.RequestException as exc:
            print(f"[pages] {url}: {exc}", file=sys.stderr)
            continue

        hits = sorted({m.group(0).lower() for m in KEY_RE.finditer(text)})
        prev = seen.get(url, {})
        new_hits = [h for h in hits if h not in prev.get("hits", [])]
        digest = hashlib.sha256(text.encode()).hexdigest()

        if new_hits:
            alerts.append(
                f"🔔 Mot-clé inscription détecté sur {url}\n"
                f"→ {', '.join(new_hits)}"
            )
        elif prev.get("hash") and prev["hash"] != digest:
            print(f"[pages] contenu modifié (sans mot-clé) : {url}")

        seen[url] = {"hash": digest, "hits": hits}


def check_feeds(state: dict, alerts: list) -> None:
    seen_ids = set(state.setdefault("feed_items", []))
    for feed in FEEDS:
        try:
            resp = get(feed)
            if resp.status_code != 200:
                continue
            xml = resp.text
        except requests.RequestException as exc:
            print(f"[feed] {feed}: {exc}", file=sys.stderr)
            continue

        items = re.findall(r"(?s)<item>(.*?)</item>", xml)
        for item in items:
            title = re.search(r"(?s)<title>(.*?)</title>", item)
            link = re.search(r"(?s)<link>(.*?)</link>", item)
            title = visible_text(title.group(1)) if title else ""
            link = link.group(1).strip() if link else BASE
            uid = hashlib.sha1((title + link).encode()).hexdigest()[:16]
            if uid in seen_ids:
                continue
            seen_ids.add(uid)
            if KEY_RE.search(title) or KEY_RE.search(visible_text(item)):
                alerts.append(f"📰 Nouvel article MIUT : {title}\n{link}")
            else:
                print(f"[feed] nouvel article ignoré : {title}")
    state["feed_items"] = sorted(seen_ids)


def check_candidates(state: dict, alerts: list) -> None:
    seen = state.setdefault("candidates", {})
    for url in CANDIDATE_URLS:
        try:
            code = get(url, allow_redirects=True).status_code
        except requests.RequestException as exc:
            print(f"[url] {url}: {exc}", file=sys.stderr)
            continue
        prev = seen.get(url)
        if code == 200 and prev != 200:
            alerts.append(f"🚨 Page d'inscription en ligne : {url}")
        seen[url] = code


def heartbeat(state: dict) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    last = state.get("heartbeat")
    if last:
        last = dt.datetime.fromisoformat(last)
        if (now - last).days < 7:
            return
    state["heartbeat"] = now.isoformat()
    notify(
        "✅ Veille MIUT active. Aucune ouverture d'inscription détectée à ce jour."
    )


# --------------------------------------------------------------------------- #
def main() -> int:
    state = load_state()
    alerts: list = []

    check_pages(state, alerts)
    check_feeds(state, alerts)
    check_candidates(state, alerts)

    if alerts:
        notify(
            "<b>MIUT 2027 — mouvement détecté</b>\n\n"
            + "\n\n".join(alerts)
            + "\n\nhttps://miutmadeira.com/fr/home-fr/"
        )
    else:
        heartbeat(state)

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
