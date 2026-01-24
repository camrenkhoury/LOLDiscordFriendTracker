# backfill_encrypted_ids.py
from storage import load_data, save_data
from riot import get_account_by_riot_id, get_summoner_by_puuid

def split_riot_id(key: str):
    if "#" not in key:
        raise ValueError(f"Bad Riot ID key (expected Name#TAG): {key}")
    return key.split("#", 1)

def main():
    data = load_data()
    players = data.get("players", {})
    if not players:
        print("No players in league.json")
        return

    updated = 0
    skipped = 0
    errors = 0

    # Ensure these exist (older files sometimes miss them)
    data.setdefault("player_match_index", {})
    data.setdefault("matches", {})

    # Iterate over a copy because we may rename keys in-place
    for old_key, p in list(players.items()):
        try:
            # If encrypted_id already present, still validate PUUID if you want.
            # For now, skip only if encrypted_id exists AND puuid looks present.
            if p.get("encrypted_id") and p.get("puuid"):
                skipped += 1
                continue

            # Resolve Riot ID -> fresh PUUID for CURRENT API key/app
            game_name = p.get("game_name")
            tag_line = p.get("tag_line")
            if not game_name or not tag_line:
                game_name, tag_line = split_riot_id(old_key)

            acct = get_account_by_riot_id(game_name, tag_line)
            new_game = acct.get("gameName", game_name)
            new_tag = acct.get("tagLine", tag_line)
            new_key = f"{new_game}#{new_tag}"
            new_puuid = acct["puuid"]

            # PUUID -> Summoner (gives encryptedSummonerId "id" for spectator v4)
            summ = get_summoner_by_puuid(new_puuid)
            enc = summ.get("id")
            if not enc:
                raise RuntimeError("summoner-by-puuid returned no 'id' (encryptedSummonerId)")

            # Update player object
            p["game_name"] = new_game
            p["tag_line"] = new_tag
            p["puuid"] = new_puuid
            p["encrypted_id"] = enc

            # If the key changed (case/canonicalization), rename in players + move match index
            if new_key != old_key:
                players[new_key] = p
                del players[old_key]

                pm = data.get("player_match_index", {})
                if old_key in pm:
                    pm[new_key] = pm.pop(old_key)

            updated += 1
            print("OK", new_key)

        except Exception as e:
            errors += 1
            print("ERROR", old_key, e)

    save_data(data)
    print(f"done. updated={updated} skipped={skipped} errors={errors}")

if __name__ == "__main__":
    main()
