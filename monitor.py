#!/usr/bin/env python3
"""
Veille ouverture inscriptions MIUT 2027 — version 3 (ciblage strict).

Ne declenche QUE sur :
  1. apparition d'un motif "ouverture des inscriptions + 2026/2027"
     sur les pages reglement / inscriptions
  2. modification du contenu de la page REGLEMENT (elle ne bouge qu'une a
     deux fois par an ; son edition precede l'ouverture)
  3. article de blog associant inscription et millesime 2027

Tout le reste (page d'accueil, pages permanentes inchangees, articles
sans rapport) est journalise mais n'envoie rien.
Heartbeat hebdomadaire = temoin de bon fonctionnement.
"""

import hashlib
import json
import os
import re
import sys
import datetime as dt

import requests

STATE_FILE = os.environ.get("MIUT_STATE", "state.json")
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# Pages dont toute modification est un signal fort (rarement editees).
REGULATION_PAGES = [
    "https://miutmadeira.com/regulations/",
    "https://miutmadeira.com/fr/reglements/",
    "https://miutmadeira.com/fr/reglement/",
    "https://miutmadeira.com/pt/regulamentos/",
]

# Pages surveillees uniquement pour le motif "date d'ouverture".
REGISTRATION_PAGES = [
    "https://miutmadeira.com/registrations/",
    "https://miutmadeira.com/inscricoes/",
    "https://miutmadeira.com/pt/inscricoes/",
    "https://miutmadeira.com/fr/inscriptions/",
]

FEEDS = [
    "https://miutmadeira.com/feed/",
    "https://miutmadeira.com/fr/category/fr/feed/",
]

YEAR = r"20(?:26|27)"
OPEN_WORD = r"ouvertur|opening|abertura|ouvrent|open\b|abrem"
REG_WORD = r"inscription|registration|inscri[çc][õo]|inscri[çc][ãa]o"

# "October 30, 2026 - 15:00 - Opening of registrations for MIUT MARATHON"
# "30 octobre 2026 - 15h00 - ouverture des inscriptions"
OPENING_RE = re.compile(
    rf"(?is)(?:{YEAR}[^.\n]{{0,150}}?(?:{OPEN_WORD})[^.\n]{{0,80}}?(?:{REG_WORD})"
    rf"|(?:{OPEN_WORD})[^.\n]{{0,80}}?(?:{REG_WORD})[^.\n]{{0,150}}?{YEAR})"
)

# Article de blog : inscription + 2027 explicitement.
FEED_RE = re.compile(rf"(?is)(?:{REG_WORD})[^.\n]{{0,200}}?2027"
                     rf"|2027[^.\n]{{0,200}}?(?:{REG_WORD})")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "fr,en;q=0.8",
}


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
    print("=== MESSAGE ===\n" + text + "\n===============")
    if not (TG_TOKEN and TG_CHAT):
        print("DIAG: secrets Telegram manquants, envoi abandonne.", file=sys.stderr)
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text[:3900],
                  "disable_web_page_preview": False},
            timeout=20,
        )
        print(f"DIAG: HTTP {r.status_code} — {r.text[:200]}")
    except requests.RequestException as exc:
        print("DIAG: exception reseau:", exc, file=sys.stderr)


def get(url: str):
    return requests.get(url, headers=HEADERS, timeout=30, allow_redirects=True)


def visible_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;?|&#\d+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_text(url: str):
    try:
        r = get(url)
        if r.status_code != 200:
            print(f"[page] {url} -> HTTP {r.status_code}, ignoree")
            return None
        return visible_text(r.text)
    except requests.RequestException as exc:
        print(f"[page] {url}: {exc}", file=sys.stderr)
        return None


def excerpt(text: str, match) -> str:
    start = max(0, match.start() - 150)
    return "… " + text[start:match.end() + 250].strip() + " …"


def check_page_set(state, alerts, first_run, urls, alert_on_change):
    seen = state.setdefault("pages", {})
    for url in urls:
        text = fetch_text(url)
        if text is None:
            continue
        digest = hashlib.sha256(text.encode()).hexdigest()
        match = OPENING_RE.search(text)
        prev = seen.get(url, {})
        seen[url] = {"hash": digest, "opening": bool(match), "len": len(text)}

        if first_run:
            print(f"[page] baseline {url} (opening={bool(match)}, "
                  f"{len(text)} car.)")
            continue

        if match and not prev.get("opening"):
            alerts.append("DATE D'OUVERTURE DETECTEE\n" + url + "\n\n"
                          + excerpt(text, match))
        elif alert_on_change and prev.get("hash") and prev["hash"] != digest:
            alerts.append(f"Reglement modifie ({len(text) - prev.get('len', 0):+d} "
                          f"caracteres) — verifier le calendrier\n{url}")
        else:
            print(f"[page] {url} : inchangee")


def check_feeds(state, alerts, first_run):
    seen_ids = set(state.setdefault("feed_items", []))
    for feed in FEEDS:
        try:
            r = get(feed)
            if r.status_code != 200:
                print(f"[feed] {feed} -> HTTP {r.status_code}, ignore")
                continue
        except requests.RequestException as exc:
            print(f"[feed] {feed}: {exc}", file=sys.stderr)
            continue

        items = re.findall(r"(?s)<item>(.*?)</item>", r.text)
        print(f"[feed] {feed}: {len(items)} articles")
        for item in items:
            t = re.search(r"(?s)<title>(.*?)</title>", item)
            l = re.search(r"(?s)<link>(.*?)</link>", item)
            title = visible_text(t.group(1)) if t else "(sans titre)"
            link = l.group(1).strip() if l else ""
            uid = hashlib.sha1((title + link).encode()).hexdigest()[:16]
            if uid in seen_ids:
                continue
            seen_ids.add(uid)
            if first_run:
                continue
            body = visible_text(item)
            if FEED_RE.search(title) or FEED_RE.search(body):
                alerts.append(f"Article inscriptions 2027\n{title}\n{link}")
            else:
                print(f"[feed] article hors sujet, ignore : {title}")
    state["feed_items"] = sorted(seen_ids)


def heartbeat(state):
    now = dt.datetime.now(dt.timezone.utc)
    last = state.get("heartbeat")
    if last and (now - dt.datetime.fromisoformat(last)).days < 7:
        return
    state["heartbeat"] = now.isoformat()
    notify("Veille MIUT active. Aucune information sur les inscriptions 2027.")


def main() -> int:
    state = load_state()
    first_run = "pages" not in state
    alerts: list = []

    check_page_set(state, alerts, first_run, REGULATION_PAGES, True)
    check_page_set(state, alerts, first_run, REGISTRATION_PAGES, False)
    check_feeds(state, alerts, first_run)

    if first_run:
        state["heartbeat"] = dt.datetime.now(dt.timezone.utc).isoformat()
        notify("Veille MIUT initialisee (v3, ciblage strict 2027). "
               "Reference etablie sur le reglement et le blog.")
    elif alerts:
        notify("MIUT 2027 — INSCRIPTIONS\n\n" + "\n\n".join(alerts)
               + "\n\nhttps://miutmadeira.com/regulations/")
    else:
        heartbeat(state)

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
