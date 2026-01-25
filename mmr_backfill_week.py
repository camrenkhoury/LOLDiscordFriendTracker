# mmr_backfill_week.py
from datetime import datetime, timedelta, timezone
import asyncio

from storage import load_data, save_data
from riot import get_player_profile
from mmrupdate import estimate_mmr_from_rank, _ensure_mmr_struct

BACKFILL_DAYS = 7

def iso_at(dt):
    return dt.astimezone(timezone.utc).isoformat()

async def main():
    data = load_data()
    backfill_time = datetime.now(timezone.utc) - timedelta(days=BACKFILL_DAYS)

    for riot_id, player in data["players"].items():
        game_name = player["game_name"]
        tag_line = player["tag_line"]

        try:
            profile = get_player_profile(game_name, tag_line)
        except Exception as e:
            print("Failed:", riot_id, e)
            continue

        mmr = _ensure_mmr_struct(player)

        for entry in profile.get("ranked_entries", []):
            tier = entry.get("tier")
            div = entry.get("rank")
            lp = entry.get("leaguePoints", 0)
            queue = entry.get("queueType")

            if queue == "RANKED_SOLO_5x5":
                q = "solo"
            elif queue == "RANKED_FLEX_SR":
                q = "flex"
            else:
                continue

            value = estimate_mmr_from_rank(entry)
            if value is None:
                continue

            # backfill snapshot
            mmr[q]["history"].append([iso_at(backfill_time), value])

            # current snapshot
            mmr[q]["current"] = value

    save_data(data)
    print("MMR backfill complete.")

if __name__ == "__main__":
    asyncio.run(main())
