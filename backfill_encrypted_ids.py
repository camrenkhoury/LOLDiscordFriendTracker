# backfill_encrypted_ids.py  (repurposed to REBUILD PUUIDS)
from storage import load_data, save_data
from riot import get_account_by_riot_id

def main():
    data = load_data()
    players = data.get("players", {})
    updated = skipped = errors = 0

    for riot_id, p in players.items():
        if "#" not in riot_id:
            print("ERROR", riot_id, "invalid riot_id key (expected Name#TAG)")
            errors += 1
            continue

        game_name, tag_line = riot_id.split("#", 1)

        try:
            acct = get_account_by_riot_id(game_name, tag_line)
            puuid = acct.get("puuid")
            if not puuid:
                print("ERROR", riot_id, "account lookup returned no puuid")
                errors += 1
                continue

            # update canonical casing too (optional but good)
            p["game_name"] = acct.get("gameName", game_name)
            p["tag_line"]  = acct.get("tagLine", tag_line)

            if p.get("puuid") == puuid:
                skipped += 1
                continue

            p["puuid"] = puuid

            # LIVE no longer needs encryptedSummonerId; remove if present to avoid confusion
            if "encrypted_id" in p:
                p.pop("encrypted_id", None)

            updated += 1

        except Exception as e:
            print("ERROR", riot_id, e)
            errors += 1

    save_data(data)
    print(f"done. updated={updated} skipped={skipped} errors={errors}")

if __name__ == "__main__":
    main()
