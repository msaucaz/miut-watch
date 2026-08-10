#!/usr/bin/env python3
"""
Veille ouverture inscriptions MIUT 2027 — version 2.

Corrections v2 :
  - diagnostic Telegram explicite (secrets manquants, réponse de l'API)
  - suppression du capteur "URL 404 -> 200" (faux positifs : /registrations/,
    /inscricoes/ et /pt/inscricoes/ sont des pages permanentes)
  - surveillance par empreinte de contenu des pages règlement + inscriptions,
    qui portent le calendrier daté d'ouverture
  - détection ciblée d'une date d'ouverture mentionnant 2026/2027
  - alerte sur tout nouvel article du blog (volume faible : ~1-2 par mois)
  - premier run = baseline silencieuse, pas de déluge d'alertes
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

# Pages porteuses du calendrier d'ouverture : le règlement annonce les dates
# et heures d'ouverture distance par distance, plusieurs semaines à l'avance.
WATCH_PAGES = [
    "https://miutmadeira.com/regulations/",
    "https://miutmadeira.com/fr/reglements/",
    "https://miutmadeira.com/pt/regulamentos/",
    "https://miutmadeira.com/registrations/",
    "https://miutmadeira.com/fr/inscriptions-2/",
    "https://miutmadeira.com/pt/inscricoes/",
    "https://miutmadeira.com/fr/home-fr/",
    "https://miutmadeira.com/",
]

FEEDS = [
    "https://miutmadeira.com/feed/",
    "https://miutmadeira.com/fr/category/fr/feed/",
]

# Une date d'ouverture du type "October 30, 2026 - 15:00 - Opening of
# registrations" ou "30 octobre 2026 - 15h00 - ouverture des inscriptions".
OPENING_RE = re.compile(
    r"(?is)(20(?:26|27))[^.]{0,120}?(ouverture|opening|abertura)"
    r"[^.]{0,60}?(inscription|registration|inscri[çc])"
    r"|(ouverture|opening|abertura)[^.]{0,60}?"
    r"(inscription|registration|inscri[çc])[^.]{0,120}?(20(?:26|27))"
)

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
    """Envoi Telegram avec diagnostic complet dans les logs."""
    print("=== MESSAGE ===")
    print(text)
    print("===============")

    if not TG_TOKEN:
        print("DIAG: TELEGRAM_TOKEN absent ou vide cote runner.", file=sys.stderr)
    if not TG_CHAT:
        print("DIAG: TELEGRAM_CHAT_ID absent ou vide cote runner.", file=sys.stderr)
    if not (TG_TOKEN and TG_CHAT):
        print("DIAG: envoi abandonne. Verifier Settings > Secrets and "
              "variables > Actions, et le bloc env: du workflow.", file=sys.stderr)
        return

    print(f"DIAG: token len={len(TG_TOKEN)}, chat_id={TG_CHAT!r}")
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text,
                  "disable_web_page_preview": False},
            timeout=20,
        )
        print(f"DIAG: HTTP {resp.status_code} — {resp.text[:300]}")
        if resp.status_code != 200:
            print("DIAG: echec. 401/404 = token faux ; "
                  "'chat not found' = chat_id faux ou /start non fait.",
                  file=sys.stderr)
    except requests.RequestException as exc:
        print("DIAG: exception reseau:", exc, file=sys.stderr)


def get(url: str):
    return requests.get(url, headers=HEADERS, timeout=30, allow_redirects=True)


def visible_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;?|&#8217;|&#8211;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def check_pages(state: dict, alerts: list, first_run: bool) -> None:
    seen = state.setdefault("pages", {})
    for url in WATCH_PAGES:
        try:
            resp = get(url)
            if resp.status_code != 200:
                print(f"[page] {url} -> HTTP {resp.status_code}, ignoree")
                continue
            text = visible_text(resp.text)
        except requests.RequestException as exc:
            print(f"[page] {url}: {exc}", file=sys.stderr)
            continue

        digest = hashlib.sha256(text.encode()).hexdigest()
        opening = bool(OPENING_RE.search(text))
        prev = seen.get(url, {})
        seen[url] = {"hash": digest, "opening": opening, "len": len(text)}

        if first_run:
            print(f"[page] baseline {url} (opening={opening})")
            continue

        if opening and not prev.get("opening"):
            snippet = ""
            m = OPENING_RE.search(text)
            if m:
                s = max(0, m.start() - 120)
                snippet = "\n\n… " + text[s:m.end() + 200] + " …"
            alerts.append(f"🚨 DATE D'OUVERTURE DETECTEE\n{url}{snippet}")
        elif prev.get("hash") and prev["hash"] != digest:
            delta = len(text) - prev.get("len", len(text))
            alerts.append(f"✏️ Page modifiee ({delta:+d} caracteres)\n{url}")


def check_feeds(state: dict, alerts: list, first_run: bool) -> None:
    seen_ids = set(state.setdefault("feed_items", []))
    for feed in FEEDS:
        try:
            resp = get(feed)
            if resp.status_code != 200:
                print(f"[feed] {feed} -> HTTP {resp.status_code}, ignore")
                continue
            xml = resp.text
        except requests.RequestException as exc:
            print(f"[feed] {feed}: {exc}", file=sys.stderr)
            continue

        items = re.findall(r"(?s)<item>(.*?)</item>", xml)
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
            if not first_run:
                alerts.append(f"📰 Nouvel article MIUT\n{title}\n{link}")
    state["feed_items"] = sorted(seen_ids)


def heartbeat(state: dict) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    last = state.get("heartbeat")
    if last and (now - dt.datetime.fromisoformat(last)).days < 7:
        return
    state["heartbeat"] = now.isoformat()
    notify("✅ Veille MIUT active. Rien de neuf sur les inscriptions 2027.")


def main() -> int:
    state = load_state()
    first_run = not state
    alerts: list = []

    check_pages(state, alerts, first_run)
    check_feeds(state, alerts, first_run)

    if first_run:
        state["heartbeat"] = dt.datetime.now(dt.timezone.utc).isoformat()
        notify("✅ Veille MIUT initialisee. Reference etablie, "
               "surveillance du reglement et du blog active.")
    elif alerts:
        notify("MIUT 2027 — mouvement detecte\n\n" + "\n\n".join(alerts))
    else:
        heartbeat(state)

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
