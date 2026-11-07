#!/usr/bin/env python3
"""
steam_random.py — Choisit un jeu aléatoire dans ta bibliothèque Steam,
pondéré selon ton temps de jeu.

Prérequis :
  - Une clé API Steam : https://steamcommunity.com/dev/apikey
  - Ton SteamID64 (ou ton pseudo d'URL perso, ex: steamcommunity.com/id/<pseudo>)
  - Profil Steam > Confidentialité > "Détails des jeux" en Public
    (sinon l'API renvoie une liste vide)

Exemples :
  export STEAM_API_KEY=XXXX   (à mettre dans ~/.zshrc)

  # Mise à jour complète de la liste (API + jeux absents de l'API via userdata.json) :
  python3 steam_random.py --steamid 7656119XXXXXXXXXX --free --userdata userdata.json

  # Tirages ensuite, sans réseau :
  python3 steam_random.py --offline
  python3 steam_random.py --offline --mode jamais --top 5
  python3 steam_random.py --offline --mode favoris --min-h 2 --launch

userdata.json : copie de https://store.steampowered.com/dynamicstore/userdata/
(connecté au store), à refaire quand tu achètes ou ajoutes des jeux.

Modes :
  backlog  (défaut) : favorise les jeux PEU joués   -> poids = 1 / (1 + heures)
  favoris           : favorise les jeux BEAUCOUP joués -> poids = sqrt(heures)
  jamais            : uniquement les jeux jamais lancés (0 min)
  uniforme          : pur hasard, temps de jeu ignoré (filtres quand même appliqués)
"""

from __future__ import annotations

import argparse
import json
import math
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
CACHE_FILE = Path(__file__).with_name("steam_games_cache.json")


# ---------------------------------------------------------------- API Steam
def api_get(endpoint: str, params: dict) -> dict:
    url = f"{API}/{endpoint}/?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "steam-random-picker"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            sys.exit("Erreur : clé API invalide ou refusée.")
        sys.exit(f"Erreur HTTP {e.code} sur {endpoint}")
    except urllib.error.URLError as e:
        sys.exit(f"Erreur réseau : {e.reason}")


def resolve_steamid(key: str, steamid: str) -> str:
    """Accepte un SteamID64 ou un pseudo d'URL perso."""
    if steamid.isdigit() and len(steamid) == 17:
        return steamid
    if len(steamid) == 32 and all(c in "0123456789abcdefABCDEF" for c in steamid):
        sys.exit("Ce que tu as mis dans --steamid ressemble à une clé API (32 caractères hexa).\n"
                 "--steamid attend ton SteamID64 (17 chiffres, commence par 7656119) "
                 "ou ton pseudo d'URL perso.")
    data = api_get("ISteamUser/ResolveVanityURL/v1", {"key": key, "vanityurl": steamid})
    resp = data.get("response", {})
    if resp.get("success") != 1:
        sys.exit(f"Impossible de résoudre le pseudo '{steamid}'.")
    return resp["steamid"]


def fetch_games(key: str, steamid: str, include_free: bool = False) -> list[dict]:
    params = {
        "key": key,
        "steamid": steamid,
        "include_appinfo": 1,
        "include_played_free_games": 1,   # F2P auxquels tu as déjà joué
        "skip_unvetted_apps": 0,          # jeux "non vérifiés" / mal configurés par l'éditeur
        "language": "french",
    }
    if include_free:
        params["include_free_sub"] = 1    # gratuits jamais lancés (peut ajouter du bruit)
    data = api_get("IPlayerService/GetOwnedGames/v1", params)
    games = data.get("response", {}).get("games", [])
    if not games:
        sys.exit("Aucun jeu renvoyé. Vérifie que 'Détails des jeux' est en Public "
                 "dans la confidentialité de ton profil.")
    return [
        {"appid": g["appid"],
         "name": g.get("name", f"App {g['appid']}"),
         "minutes": g.get("playtime_forever", 0)}
        for g in games
    ]


# ---------------------------------------------------------------- Jeux hors API
def fetch_game_catalog(key: str) -> dict[int, str]:
    """Tous les jeux (type 'game') du store : {appid: nom}. Quelques secondes."""
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
            return catalog
        last = r["last_appid"]


def merge_userdata(games: list[dict], key: str, path: str) -> list[dict]:
    """Ajoute les jeux possédés (selon le store) que GetOwnedGames ne renvoie pas
    (F2P jamais lancés, etc.). DLC, bandes-son et logiciels sont ignorés."""
    try:
        owned = set(json.loads(Path(path).read_text(encoding="utf-8")).get("rgOwnedApps", []))
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"Impossible de lire {path} : {e}")
    if not owned:
        sys.exit("rgOwnedApps est vide : connecte-toi au store avant d'ouvrir la page userdata.")
    catalog = fetch_game_catalog(key)
    known = {g["appid"] for g in games}
    extra = [{"appid": a, "name": catalog[a], "minutes": 0}
             for a in sorted(owned - known) if a in catalog]
    print(f"+ {len(extra)} jeux ajoutés depuis {Path(path).name} (temps de jeu inconnu -> 0)")
    return games + extra


# ---------------------------------------------------------------- Diagnostic
TYPE_FR = {"game": "Jeux", "dlc": "DLC", "demo": "Démos", "music": "Bandes-son",
           "application": "Logiciels", "tool": "Outils", "video": "Vidéos",
           "mod": "Mods", "beta": "Playtests/bêtas", "advertising": "Pub/promo",
           "?": "Inconnu / retiré du store"}


def app_info(appid: int) -> tuple[str, str]:
    """(type, nom) via l'API du store. ('?', ...) si l'app n'existe plus sur le store."""
    url = (f"https://store.steampowered.com/api/appdetails?appids={appid}"
           f"&filters=basic&l=french")
    req = urllib.request.Request(url, headers={"User-Agent": "steam-random-picker"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.load(r).get(str(appid), {})
    except (urllib.error.URLError, json.JSONDecodeError):
        return "?", f"App {appid} (erreur réseau)"
    if not d.get("success"):
        return "?", f"App {appid}"
    data = d.get("data", {})
    return data.get("type", "?"), data.get("name", f"App {appid}")


def compare(games: list[dict], path: str, limit: int) -> None:
    try:
        raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
        store = json.loads(raw)
    except OSError as e:
        sys.exit(f"Impossible de lire {path} : {e}")
    except json.JSONDecodeError:
        sys.exit("Le contenu n'est pas du JSON valide. Copie bien depuis l'onglet "
                 "« Données brutes » de Firefox (Ctrl/Cmd+A puis Ctrl/Cmd+C).")
    owned = set(store.get("rgOwnedApps", []))
    if not owned:
        sys.exit("rgOwnedApps est vide : tu n'étais sans doute pas connecté au store "
                 "dans le navigateur quand tu as ouvert la page.")
    api_ids = {g["appid"] for g in games}
    missing = sorted(owned - api_ids)
    print(f"\nStore : {len(owned)} apps possédées (jeux + DLC + bandes-son, etc.)")
    print(f"API   : {len(api_ids)} jeux")
    print(f"Présentes sur le store mais absentes de l'API : {len(missing)}")
    if not missing:
        return
    todo = missing[:limit]
    print(f"Identification de {len(todo)} apps (~{len(todo) * 1.6 / 60:.0f} min, "
          f"le store limite le nombre de requêtes)...\n")
    by_type: dict[str, list[str]] = {}
    for i, appid in enumerate(todo, 1):
        t, name = app_info(appid)
        by_type.setdefault(t, []).append(f"{name} [{appid}]")
        print(f"\r  {i}/{len(todo)}", end="", flush=True)
        time.sleep(1.6)
    print("\n")
    for t, names in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
        print(f"== {TYPE_FR.get(t, t)} : {len(names)}")
        for n in names:
            print(f"   - {n}")
    if len(missing) > limit:
        print(f"\n({len(missing) - limit} apps non identifiées, augmente --compare-limit)")


# ---------------------------------------------------------------- Pondération
def weight(minutes: int, mode: str) -> float:
    hours = minutes / 60
    if mode == "backlog":
        return 1 / (1 + hours)
    if mode == "favoris":
        # sqrt pour qu'un jeu à 2000 h n'écrase pas tout le reste
        return math.sqrt(hours)
    return 1.0  # uniforme / jamais


def filter_games(games, mode, min_h, max_h, exclude):
    out = []
    excl = [e.lower() for e in exclude]
    for g in games:
        h = g["minutes"] / 60
        if mode == "jamais" and g["minutes"] > 0:
            continue
        if min_h is not None and h < min_h:
            continue
        if max_h is not None and h > max_h:
            continue
        if str(g["appid"]) in excl or any(e in g["name"].lower() for e in excl):
            continue
        out.append(g)
    return out


def weighted_sample(games, k, mode, rng):
    """Tirage pondéré SANS remise (algo Efraimidis–Spirakis)."""
    keyed = []
    for g in games:
        w = weight(g["minutes"], mode)
        if w <= 0:
            continue
        keyed.append((rng.random() ** (1 / w), g))
    keyed.sort(key=lambda t: t[0], reverse=True)
    return [g for _, g in keyed[:k]]


def fmt_time(minutes: int) -> str:
    if minutes == 0:
        return "jamais lancé"
    h, m = divmod(minutes, 60)
    return f"{h} h {m:02d}" if h else f"{m} min"


# ---------------------------------------------------------------- Main
def main():
    p = argparse.ArgumentParser(description="Jeu Steam aléatoire pondéré par le temps de jeu.")
    p.add_argument("--key", default=os.environ.get("STEAM_API_KEY"),
                   help="Clé API Steam (ou variable d'env STEAM_API_KEY)")
    p.add_argument("--steamid", default=os.environ.get("STEAM_ID"),
                   help="SteamID64 ou pseudo d'URL perso (ou variable d'env STEAM_ID)")
    p.add_argument("--mode", choices=["backlog", "favoris", "jamais", "uniforme"],
                   default="backlog")
    p.add_argument("--top", type=int, default=1, help="Nombre de jeux à proposer")
    p.add_argument("--min-h", type=float, help="Temps de jeu minimum (heures)")
    p.add_argument("--max-h", type=float, help="Temps de jeu maximum (heures)")
    p.add_argument("--exclude", nargs="*", default=[],
                   help="AppIDs ou morceaux de nom à exclure (ex: --exclude wallpaper 431960)")
    p.add_argument("--offline", action="store_true",
                   help="Utilise le cache local au lieu d'appeler l'API")
    p.add_argument("--launch", action="store_true",
                   help="Lance directement le jeu tiré (via steam://)")
    p.add_argument("--seed", type=int, help="Graine aléatoire (pour reproduire un tirage)")
    p.add_argument("--free", action="store_true",
                   help="Inclut aussi les jeux gratuits jamais lancés (include_free_sub)")
    p.add_argument("--compare", metavar="FICHIER",
                   help="Diagnostic : compare avec le JSON de "
                        "store.steampowered.com/dynamicstore/userdata/")
    p.add_argument("--userdata", metavar="FICHIER",
                   help="Ajoute les jeux possédés absents de l'API, d'après le JSON de "
                        "store.steampowered.com/dynamicstore/userdata/")
    p.add_argument("--compare-limit", type=int, default=200,
                   help="Nombre max d'apps manquantes à identifier (défaut 200)")
    args = p.parse_args()

    # Récupération des jeux
    if args.offline:
        if not CACHE_FILE.exists():
            sys.exit(f"Pas de cache ({CACHE_FILE.name}). Lance une fois sans --offline.")
        games = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    else:
        if not args.key or not args.steamid:
            manquant = " et ".join(n for n, v in (("la clé API (--key ou STEAM_API_KEY)", args.key),
                                                  ("le SteamID (--steamid ou STEAM_ID)", args.steamid))
                                   if not v)
            sys.exit(f"Il manque {manquant}.\n"
                     "Sous zsh/bash : export STEAM_API_KEY=... (et non 'set', qui est la syntaxe de cmd Windows).")
        sid = resolve_steamid(args.key, args.steamid)
        games = fetch_games(args.key, sid, args.free)
        if args.userdata:
            games = merge_userdata(games, args.key, args.userdata)
        CACHE_FILE.write_text(json.dumps(games, ensure_ascii=False, indent=1), encoding="utf-8")

    if args.compare:
        compare(games, args.compare, args.compare_limit)
        return

    pool = filter_games(games, args.mode, args.min_h, args.max_h, args.exclude)
    if not pool:
        sys.exit("Aucun jeu ne correspond aux filtres.")

    rng = random.Random(args.seed)
    picks = weighted_sample(pool, max(1, args.top), args.mode, rng)
    if not picks:  # ex: mode favoris avec que des jeux à 0 h
        sys.exit("Aucun jeu tirable (tous les poids sont nuls pour ce mode).")

    total_w = sum(weight(g["minutes"], args.mode) for g in pool)
    print(f"\n🎮 {len(games)} jeux dans la bibliothèque, {len(pool)} éligibles "
          f"(mode : {args.mode})\n")
    for i, g in enumerate(picks, 1):
        chance = weight(g["minutes"], args.mode) / total_w * 100
        print(f"  {i}. {g['name']}  —  {fmt_time(g['minutes'])}  "
              f"(chance de tirage : {chance:.2f} %)  [appid {g['appid']}]")
    print()

    if args.launch:
        g = picks[0]
        print(f"Lancement de {g['name']}...")
        webbrowser.open(f"steam://rungameid/{g['appid']}")


if __name__ == "__main__":
    main()
