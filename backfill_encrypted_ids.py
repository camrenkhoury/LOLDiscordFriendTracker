# backfill_encrypted_ids.py
from storage import load_data, save_data
from riot import get_summoner_by_puuid

def main():
    data = load_data()
    players = data.get("players", {})
    updated = skipped = errors = 0

    for riot_id, p in players.items():
        if p.get("encrypted_id"):
            skipped += 1
            continue
        puuid = p.get("puuid")
        if not puuid:
            errors += 1
            continue
        try:
            summ = get_summoner_by_puuid(puuid)
            enc = summ.get("id")
            if not enc:
                errors += 1
                continue
            p["encrypted_id"] = enc
            updated += 1
        except Exception as e:
            print("ERROR", riot_id, e)
            errors += 1

    save_data(data)
    print(f"done. updated={updated} skipped={skipped} errors={errors}")

if __name__ == "__main__":
    main()
