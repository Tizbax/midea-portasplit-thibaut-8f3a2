#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Midea PortaSplit 12000 BTU — Surveillance de stock + notification téléphone.

Surveille plusieurs pages produit de revendeurs français, détecte quand le
climatiseur repasse "disponible", et t'envoie une notification sur ton
téléphone (push ntfy par défaut, ou SMS / Telegram).

Usage :
    python3 midea_stock_monitor.py            # boucle en continu
    python3 midea_stock_monitor.py --once     # une seule vérification (idéal cron / tâche planifiée)
    python3 midea_stock_monitor.py --test-notif   # envoie une notif de test puis quitte

La configuration se fait dans config.json (voir README.md).
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")
LOG_PATH = os.path.join(HERE, "monitor.log")

# En-têtes pour ressembler à un vrai navigateur (réduit les blocages basiques).
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5",
}

# --- Signaux de disponibilité (français) -----------------------------------
# Si un de ces textes est présent -> RUPTURE (priorité la plus forte).
DEFAULT_OUT_OF_STOCK = [
    "actuellement indisponible",
    "temporairement indisponible",
    "temporairement en rupture",
    "produit indisponible",
    "produit épuisé",
    "épuisé",
    "rupture de stock",
    "en rupture",
    "victime de son succès",
    "me prévenir",
    "prévenez-moi",
    "recevoir une alerte",
    "être alerté",
    "bientôt disponible",
    "réapprovisionnement",
    "non disponible",
    "indisponible en ligne",
    "stock épuisé",
]
# Sinon, si un de ces textes est présent -> DISPONIBLE.
DEFAULT_IN_STOCK = [
    "ajouter au panier",
    "ajouter au panier",  # variantes d'accents gérées par la normalisation
    "acheter cet article",
    "acheter maintenant",
    "en stock",
    "disponible en ligne",
    "livraison à domicile",
    "retrait en magasin",
    "commander",
]


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def normalize(text):
    """Minuscule + suppression des accents pour une comparaison robuste."""
    text = text.lower()
    repl = {
        "à": "a", "â": "a", "ä": "a",
        "é": "e", "è": "e", "ê": "e", "ë": "e",
        "î": "i", "ï": "i",
        "ô": "o", "ö": "o",
        "ù": "u", "û": "u", "ü": "u",
        "ç": "c",
    }
    for k, v in repl.items():
        text = text.replace(k, v)
    return text


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log(f"Impossible de lire {os.path.basename(path)} ({e}). Valeur par défaut utilisée.")
        return default


def save_state(state):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except OSError as e:
        log(f"Impossible d'écrire l'état : {e}")


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")


def strip_html(html):
    html = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<style\b[^>]*>.*?</style>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    return text


def check_availability(retailer):
    """Renvoie ('available' | 'unavailable' | 'unknown' | 'error', detail)."""
    url = retailer["url"]
    out_signals = retailer.get("out_of_stock_signals", DEFAULT_OUT_OF_STOCK)
    in_signals = retailer.get("in_stock_signals", DEFAULT_IN_STOCK)
    try:
        html = fetch(url)
    except Exception as e:  # noqa: BLE001 - on veut attraper tout problème réseau
        return "error", str(e)

    text = normalize(strip_html(html))

    hit_out = next((s for s in out_signals if normalize(s) in text), None)
    if hit_out:
        return "unavailable", f"signal rupture: « {hit_out} »"

    hit_in = next((s for s in in_signals if normalize(s) in text), None)
    if hit_in:
        return "available", f"signal dispo: « {hit_in} »"

    return "unknown", "aucun signal reconnu (page à vérifier / rendu JavaScript ?)"


# --- Notifications ----------------------------------------------------------

def notify_ntfy(cfg, title, message, url=None):
    topic = cfg.get("topic")
    server = cfg.get("server", "https://ntfy.sh").rstrip("/")
    if not topic:
        log("ntfy: 'topic' manquant dans la config, notification ignorée.")
        return False
    headers = {
        "Title": title.encode("utf-8"),
        "Priority": "urgent",
        "Tags": "rotating_light",
    }
    if url:
        headers["Click"] = url
    data = message.encode("utf-8")
    req = urllib.request.Request(f"{server}/{topic}", data=data, headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=15)
        return True
    except Exception as e:  # noqa: BLE001
        log(f"ntfy: échec envoi ({e})")
        return False


def notify_free_mobile(cfg, title, message, url=None):
    """SMS gratuit pour les abonnés Free Mobile (option à activer sur mobile.free.fr)."""
    user = cfg.get("user")
    key = cfg.get("pass")
    if not user or not key:
        log("Free Mobile: 'user'/'pass' manquants, SMS ignoré.")
        return False
    full = f"{title}\n{message}"
    if url:
        full += f"\n{url}"
    params = urllib.parse.urlencode({"user": user, "pass": key, "msg": full})
    endpoint = f"https://smsapi.free-mobile.fr/sendmsg?{params}"
    try:
        urllib.request.urlopen(endpoint, timeout=15)
        return True
    except Exception as e:  # noqa: BLE001
        log(f"Free Mobile SMS: échec ({e})")
        return False


def notify_twilio(cfg, title, message, url=None):
    """SMS via Twilio (payant). Nécessite account_sid, auth_token, from, to."""
    sid = cfg.get("account_sid")
    token = cfg.get("auth_token")
    frm = cfg.get("from")
    to = cfg.get("to")
    if not all([sid, token, frm, to]):
        log("Twilio: paramètres manquants, SMS ignoré.")
        return False
    body = f"{title} — {message}"
    if url:
        body += f" {url}"
    data = urllib.parse.urlencode({"From": frm, "To": to, "Body": body}).encode("utf-8")
    endpoint = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    req = urllib.request.Request(endpoint, data=data, method="POST")
    auth = urllib.request.HTTPBasicAuthHandler()
    import base64
    creds = base64.b64encode(f"{sid}:{token}".encode()).decode()
    req.add_header("Authorization", f"Basic {creds}")
    try:
        urllib.request.urlopen(req, timeout=20)
        return True
    except Exception as e:  # noqa: BLE001
        log(f"Twilio SMS: échec ({e})")
        return False


def notify_telegram(cfg, title, message, url=None):
    token = cfg.get("bot_token")
    chat_id = cfg.get("chat_id")
    if not token or not chat_id:
        log("Telegram: 'bot_token'/'chat_id' manquants, notification ignorée.")
        return False
    text = f"*{title}*\n{message}"
    if url:
        text += f"\n{url}"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode()
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    req = urllib.request.Request(endpoint, data=data, method="POST")
    try:
        urllib.request.urlopen(req, timeout=15)
        return True
    except Exception as e:  # noqa: BLE001
        log(f"Telegram: échec ({e})")
        return False


NOTIFIERS = {
    "ntfy": notify_ntfy,
    "free_mobile": notify_free_mobile,
    "twilio": notify_twilio,
    "telegram": notify_telegram,
}


def send_notifications(config, title, message, url=None):
    notif_cfg = config.get("notifications", {})
    sent_any = False
    for name, handler in NOTIFIERS.items():
        chan = notif_cfg.get(name)
        if chan and chan.get("enabled"):
            ok = handler(chan, title, message, url)
            log(f"Notification {name}: {'envoyée' if ok else 'échec'}")
            sent_any = sent_any or ok
    if not sent_any:
        log("Aucune notification envoyée (aucun canal activé/valide). Vérifie config.json.")
    return sent_any


# --- Boucle principale ------------------------------------------------------

def run_once(config, state):
    changed = False
    for retailer in config.get("retailers", []):
        name = retailer.get("name", retailer.get("url", "?"))
        if not retailer.get("enabled", True):
            continue
        status, detail = check_availability(retailer)
        prev = state.get(name, {}).get("status")
        log(f"{name}: {status} — {detail}")

        # Alerte quand on PASSE à 'available' depuis autre chose.
        if status == "available" and prev != "available":
            title = "🟢 Midea PortaSplit DISPO !"
            message = f"{name} affiche le climatiseur comme disponible ({detail}). Fonce vérifier et commander."
            send_notifications(config, title, message, retailer.get("url"))

        state[name] = {
            "status": status,
            "detail": detail,
            "url": retailer.get("url"),
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        }
        changed = True
    if changed:
        save_state(state)
    return state


def main():
    parser = argparse.ArgumentParser(description="Surveillance de stock Midea PortaSplit 12000 BTU")
    parser.add_argument("--once", action="store_true", help="Une seule vérification puis quitte (pour cron)")
    parser.add_argument("--test-notif", action="store_true", help="Envoie une notification de test et quitte")
    args = parser.parse_args()

    config = load_json(CONFIG_PATH, None)
    if config is None:
        log(f"config.json introuvable à côté du script ({CONFIG_PATH}). Voir README.md.")
        sys.exit(1)

    if args.test_notif:
        ok = send_notifications(
            config,
            "🔔 Test PortaSplit",
            "Ceci est un test : les notifications fonctionnent. Tu seras prévenu dès un retour en stock.",
            "https://www.amazon.fr/dp/B0CY2YW8BT",
        )
        sys.exit(0 if ok else 1)

    state = load_json(STATE_PATH, {})

    if args.once:
        run_once(config, state)
        return

    interval = int(config.get("interval_seconds", 3600))
    log(f"Démarrage de la surveillance (intervalle {interval}s). Ctrl+C pour arrêter.")
    try:
        while True:
            run_once(config, state)
            time.sleep(interval)
    except KeyboardInterrupt:
        log("Arrêt demandé. À bientôt.")


if __name__ == "__main__":
    main()
