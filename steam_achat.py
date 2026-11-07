#!/usr/bin/env python3
"""
steam_achat.py — Tire au hasard un jeu de TOUT le store Steam, dans une fourchette
de prix, en excluant les jeux que tu possèdes déjà et les DLC.

Prérequis : STEAM_API_KEY dans l'environnement (comme pour steam_random.py).
Les jeux possédés sont lus dans userdata.json et steam_games_cache.json (même dossier).

Exemples :
  python3 steam_achat.py 5 15              # un jeu entre 5 € et 15 €
  python3 steam_achat.py 0 10 --top 5      # 5 jeux entre 0 € et 10 € (gratuits inclus)
  python3 steam_achat.py 0.01 10           # pareil, sans les gratuits
  python3 steam_achat.py 10 30 --promo     # uniquement les jeux en promo
  python3 steam_achat.py 5 20 --avis 80    # au moins 80 % d'avis positifs
  python3 steam_achat.py 5 20 --ouvrir     # ouvre la page store du jeu tiré
  python3 steam_achat.py 0 0               # uniquement les jeux gratuits

Le prix est le prix actuel (promo comprise) en euros, pour la France.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

API = "https://api.steampowered.com"
HERE = Path(__file__).parent
CATALOG_FILE = HERE / "steam_catalog.json"
CATALOG_MAX_AGE = 7 * 24 * 3600   # le catalogue est retéléchargé après 7 jours
BATCH = 100                        # jeux interrogés par requête de prix


def api_get(endpoint: str, params: dict) -> dict:
    url = f"{API}/{endpoint}/?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "steam-random-picker"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                sys.exit("Erreur : clé API invalide ou refusée.")
            if e.code == 429 and attempt < 2:   # trop de requêtes : on patiente
                time.sleep(5)
                continue
            sys.exit(f"Erreur HTTP {e.code} sur {endpoint}")
        except urllib.error.URLError as e:
            sys.exit(f"Erreur réseau : {e.reason}")
    sys.exit(f"Steam refuse les requêtes sur {endpoint}, réessaie plus tard.")


# ---------------------------------------------------------------- Catalogue
def load_catalog(key: str, refresh: bool) -> dict[int, str]:
    """Tous les jeux du store (sans DLC, logiciels, vidéos) : {appid: nom}."""
    if CATALOG_FILE.exists() and not refresh:
        if time.time() - CATALOG_FILE.stat().st_mtime < CATALOG_MAX_AGE:
            return {int(k): v for k, v in
                    json.loads(CATALOG_FILE.read_text(encoding="utf-8")).items()}
    if not key:
        sys.exit("Il manque la clé API (STEAM_API_KEY) pour télécharger le catalogue.")
    print("Téléchargement du catalogue Steam...", flush=True)
    catalog, last = {}, 0
    while True:
        r = api_get("IStoreService/GetAppList/v1", {
            "key": key, "include_games": 1, "include_dlc": 0, "include_software": 0,
            "include_videos": 0, "include_hardware": 0,
            "max_results": 50000, "last_appid": last,
        }).get("response", {})
        for a in r.get("apps", []):
            catalog[a["appid"]] = a["name"]
        if not r.get("have_more_results"):
            break
        last = r["last_appid"]
    CATALOG_FILE.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    return catalog


def load_owned() -> set[int]:
    owned = set()
    ud = HERE / "userdata.json"
    if ud.exists():
        try:
            owned |= set(json.loads(ud.read_text(encoding="utf-8")).get("rgOwnedApps", []))
        except json.JSONDecodeError:
            print("Attention : userdata.json illisible, ignoré.")
    cache = HERE / "steam_games_cache.json"
    if cache.exists():
        owned |= {g["appid"] for g in json.loads(cache.read_text(encoding="utf-8"))}
    if not owned:
        print("Attention : aucun jeu possédé trouvé (userdata.json / steam_games_cache.json "
              "absents), rien ne sera exclu.")
    return owned


# ---------------------------------------------------------------- Prix
def fetch_store_items(appids: list[int], country: str) -> list[dict]:
    req = {
        "ids": [{"appid": a} for a in appids],
        "context": {"language": "french", "country_code": country},
        "data_request": {"include_basic_info": True, "include_reviews": True},
    }
    data = api_get("IStoreBrowseService/GetItems/v1", {"input_json": json.dumps(req)})
    return data.get("response", {}).get("store_items", [])


def parse_item(it: dict) -> dict | None:
    """Infos utiles d'un jeu, ou None s'il n'est pas achetable (pas sorti, retiré...)."""
    if not it.get("visible") or it.get("type", 0) != 0:   # type 0 = jeu
        return None
    opt = it.get("best_purchase_option") or {}
    if it.get("is_free"):
        price, label, discount = 0, "Gratuit", 0
    elif "final_price_in_cents" in opt:
        price = int(opt["final_price_in_cents"])
        label = opt.get("formatted_final_price", f"{price / 100:.2f} €")
        discount = int(opt.get("discount_pct", 0))
        if discount:
            label += f" (-{discount} %, au lieu de {opt.get('formatted_original_price', '?')})"
    else:
        return None
    rev = (it.get("reviews") or {}).get("summary_filtered") or {}
    return {
        "appid": it["appid"],
        "name": it.get("name", f"App {it['appid']}"),
        "price": price,
        "label": label,
        "discount": discount,
        "pct": rev.get("percent_positive"),
        "nb_avis": rev.get("review_count", 0),
    }


# ---------------------------------------------------------------- Main
def main():
    p = argparse.ArgumentParser(description="Jeu Steam aléatoire à acheter, dans une fourchette de prix.")
    p.add_argument("prix_min", type=float, help="Prix minimum en euros")
    p.add_argument("prix_max", type=float, help="Prix maximum en euros")
    p.add_argument("--top", type=int, default=1, help="Nombre de jeux à proposer")
    p.add_argument("--promo", action="store_true", help="Uniquement les jeux en promotion")
    p.add_argument("--avis", type=int, metavar="POURCENT",
                   help="Pourcentage minimum d'avis positifs (ex : 80)")
    p.add_argument("--min-avis", type=int, default=0, metavar="N",
                   help="Nombre minimum d'avis (évite les jeux quasi inconnus, ex : 100)")
    p.add_argument("--pays", default="FR", help="Code pays pour les prix (défaut FR)")
    p.add_argument("--ouvrir", action="store_true", help="Ouvre la page store du premier jeu tiré")
    p.add_argument("--maj-catalogue", action="store_true", help="Force le retéléchargement du catalogue")
    p.add_argument("--essais", type=int, default=50,
                   help=f"Requêtes de prix max avant d'abandonner (×{BATCH} jeux, défaut 50)")
    p.add_argument("--seed", type=int, help="Graine aléatoire")
    args = p.parse_args()

    if args.prix_min > args.prix_max:
        args.prix_min, args.prix_max = args.prix_max, args.prix_min
    cmin, cmax = round(args.prix_min * 100), round(args.prix_max * 100)

    key = os.environ.get("STEAM_API_KEY")
    catalog = load_catalog(key, args.maj_catalogue)
    owned = load_owned()
    candidates = [a for a in catalog if a not in owned]
    print(f"{len(catalog)} jeux sur Steam, {len(candidates)} que tu ne possèdes pas.")
    print(f"Recherche entre {args.prix_min:g} € et {args.prix_max:g} €...", flush=True)

    rng = random.Random(args.seed)
    rng.shuffle(candidates)
    want = max(1, args.top)
    found = []
    for i in range(0, min(len(candidates), args.essais * BATCH), BATCH):
        for it in fetch_store_items(candidates[i:i + BATCH], args.pays):
            g = parse_item(it)
            if not g or not (cmin <= g["price"] <= cmax):
                continue
            if args.promo and not g["discount"]:
                continue
            if args.avis is not None and (g["pct"] is None or g["pct"] < args.avis):
                continue
            if g["nb_avis"] < args.min_avis:
                continue
            found.append(g)
        if len(found) >= want:
            break

    if not found:
        sys.exit("Aucun jeu trouvé avec ces critères. Élargis la fourchette ou augmente --essais.")
    rng.shuffle(found)
    print()
    for n, g in enumerate(found[:want], 1):
        avis = (f"{g['pct']} % positifs sur {g['nb_avis']} avis" if g["nb_avis"]
                else "pas encore d'avis")
        print(f"  {n}. {g['name']}  —  {g['label']}  —  {avis}")
        print(f"     https://store.steampowered.com/app/{g['appid']}/")
    print()
    if args.ouvrir:
        webbrowser.open(f"https://store.steampowered.com/app/{found[0]['appid']}/")


if __name__ == "__main__":
    main()
