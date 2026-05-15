import unittest
from datetime import datetime, timezone

from analytics import compute_top_champions, compute_top_duos
from records import LOCAL_TZ, _game_start_local, queue_name
from storage import repair_cache_indexes, resolve_player_key, upsert_player


def make_match(match_id, queue_id, team_100, team_200, winning_team=100):
    participants = []
    for puuid in team_100:
        participants.append({"puuid": puuid, "teamId": 100, "win": winning_team == 100})
    for puuid in team_200:
        participants.append({"puuid": puuid, "teamId": 200, "win": winning_team == 200})

    return {
        "metadata": {"matchId": match_id},
        "info": {
            "queueId": queue_id,
            "gameStartTimestamp": 1767225600000,
            "participants": participants,
        },
    }


class CoreLogicTests(unittest.TestCase):
    def test_compute_top_duos_returns_named_records(self):
        data = {
            "players": {
                "Alpha#NA1": {"puuid": "a"},
                "Bravo#NA1": {"puuid": "b"},
                "Charlie#NA1": {"puuid": "c"},
            },
            "matches": {
                "m1": make_match("m1", 420, ["a", "b"], ["c"], winning_team=100),
                "m2": make_match("m2", 420, ["a", "b"], ["c"], winning_team=200),
                "m3": make_match("m3", 440, ["a", "b"], ["c"], winning_team=100),
            },
        }

        results = compute_top_duos(data, queue_id=420, min_games=2)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["players"], ("Alpha#NA1", "Bravo#NA1"))
        self.assertEqual(results[0]["wins"], 1)
        self.assertEqual(results[0]["losses"], 1)
        self.assertEqual(results[0]["games"], 2)
        self.assertEqual(results[0]["wr"], 50.0)

    def test_resolve_player_key_is_case_insensitive(self):
        data = {
            "players": {
                "Some Name#NA1": {
                    "game_name": "Some Name",
                    "tag_line": "NA1",
                    "puuid": "abc",
                }
            }
        }

        self.assertEqual(resolve_player_key(data, "some name#na1"), "Some Name#NA1")

    def test_game_start_local_uses_project_timezone(self):
        ts = int(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
        match = {"info": {"gameStartTimestamp": ts}}

        local = _game_start_local(match)

        self.assertEqual(local.tzinfo, LOCAL_TZ)
        self.assertEqual(local.hour, 7)

    def test_queue_name_has_readable_fallback(self):
        self.assertEqual(queue_name(420), "Solo/Duo")
        self.assertEqual(queue_name(9999), "Queue 9999")
        self.assertEqual(queue_name("unknown"), "unknown")

    def test_upsert_player_preserves_existing_fields(self):
        data = {
            "players": {
                "OldName#NA1": {
                    "game_name": "OldName",
                    "tag_line": "NA1",
                    "puuid": "abc",
                    "mmr": {"solo": {"current": 1234, "history": [["t", 1234]]}},
                }
            },
            "player_match_index": {"OldName#NA1": ["m1"]},
        }

        upsert_player(data, "NewName#NA1", "NewName", "NA1", "abc")

        self.assertNotIn("OldName#NA1", data["players"])
        self.assertEqual(data["players"]["NewName#NA1"]["mmr"]["solo"]["current"], 1234)
        self.assertEqual(data["player_match_index"]["NewName#NA1"], ["m1"])

    def test_repair_cache_indexes_dedupes_and_prunes(self):
        data = {
            "players": {"Alpha#NA1": {"puuid": "a"}},
            "matches": {"m1": {"info": {"queueId": 420}}},
            "player_match_index": {
                "Alpha#NA1": ["m1", "m1", "missing"],
                "Deleted#NA1": ["m2"],
            },
        }

        result = repair_cache_indexes(data, prune_missing_details=True)

        self.assertEqual(result["removed_player_indexes"], 1)
        self.assertEqual(result["removed_duplicates"], 1)
        self.assertEqual(result["removed_missing_details"], 1)
        self.assertEqual(data["player_match_index"], {"Alpha#NA1": ["m1"]})

    def test_compute_top_champions_tracks_group_performance(self):
        data = {
            "players": {
                "Alpha#NA1": {"puuid": "a"},
                "Bravo#NA1": {"puuid": "b"},
            },
            "matches": {
                "m1": {
                    "info": {
                        "queueId": 420,
                        "gameStartTimestamp": 1767225600000,
                        "participants": [
                            {
                                "puuid": "a",
                                "championName": "Ahri",
                                "win": True,
                                "kills": 5,
                                "deaths": 1,
                                "assists": 5,
                            },
                            {
                                "puuid": "b",
                                "championName": "Garen",
                                "win": False,
                                "kills": 2,
                                "deaths": 4,
                                "assists": 1,
                            },
                        ],
                    }
                },
                "m2": {
                    "info": {
                        "queueId": 420,
                        "gameStartTimestamp": 1767225600000,
                        "participants": [
                            {
                                "puuid": "a",
                                "championName": "Ahri",
                                "win": True,
                                "kills": 3,
                                "deaths": 2,
                                "assists": 7,
                            }
                        ],
                    }
                },
            },
        }

        results = compute_top_champions(data, queue_ids={420}, min_games=2)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["champion"], "Ahri")
        self.assertEqual(results[0]["wins"], 2)
        self.assertEqual(results[0]["games"], 2)
        self.assertAlmostEqual(results[0]["kda"], 20 / 3)


if __name__ == "__main__":
    unittest.main()
