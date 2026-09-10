import json
import sqlite3
import threading
from pathlib import Path


class RecordStore:
    """
    V1.11 SQLite store.

    Old pre-lifecycle-fix rows are preserved but NOT selected for autoplay.

    New rows use:
        life_timer_version = 3
    """

    LIFE_TIMER_VERSION = 3

    def __init__(
        self,
        db_path="robot_records.db",
    ):
        self.db_path = Path(
            db_path
        )

        self.lock = threading.RLock()

        self.db = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
        )

        self.db.row_factory = sqlite3.Row

        with self.lock:
            self.db.execute(
                "PRAGMA journal_mode=WAL"
            )

            self.db.execute(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    map_code INTEGER NOT NULL,
                    mirrored INTEGER NOT NULL,
                    map_hash TEXT NOT NULL,
                    finish_ms REAL NOT NULL,
                    event_count INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                        DEFAULT CURRENT_TIMESTAMP,
                    life_timer_version INTEGER NOT NULL
                        DEFAULT 0
                )
                """
            )

            self.db.execute(
                """
                CREATE TABLE IF NOT EXISTS player_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_name TEXT NOT NULL,
                    target_session_id INTEGER,
                    map_code INTEGER NOT NULL,
                    mirrored INTEGER NOT NULL,
                    map_hash TEXT NOT NULL,
                    round_id INTEGER,
                    end_reason TEXT NOT NULL,
                    victory_seconds REAL,
                    event_count INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                        DEFAULT CURRENT_TIMESTAMP,
                    life_timer_version INTEGER NOT NULL
                        DEFAULT 0
                )
                """
            )

            self._ensure_column(
                "records",
                "life_timer_version",
                "INTEGER NOT NULL DEFAULT 0",
            )

            self._ensure_column(
                "player_records",
                "life_timer_version",
                "INTEGER NOT NULL DEFAULT 0",
            )

            self.db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_records_best_v2
                ON records(
                    map_code,
                    mirrored,
                    map_hash,
                    life_timer_version,
                    finish_ms
                )
                """
            )

            self.db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_player_records_best_v2
                ON player_records(
                    map_code,
                    mirrored,
                    map_hash,
                    life_timer_version,
                    end_reason,
                    victory_seconds
                )
                """
            )

            self.db.commit()

        # V1.11 normalizes existing clean records:
        # one BEST row per exact route key
        # (mapCode + mirrored + mapHash), across self + learned players.
        self.compact_best_records()

        print(
            f"[DB] ready "
            f"path={self.db_path.resolve()} "
            f"lifeTimerVersion={self.LIFE_TIMER_VERSION}"
        )

    def _ensure_column(
        self,
        table,
        column,
        declaration,
    ):
        columns = {
            row["name"]
            for row in self.db.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()
        }

        if column not in columns:
            self.db.execute(
                f"ALTER TABLE {table} "
                f"ADD COLUMN {column} {declaration}"
            )

    def _best_exact_locked(
        self,
        *,
        map_code,
        mirrored,
        map_hash,
    ):
        """
        Return the fastest clean lifecycle-v3 row across BOTH tables
        for one exact replay-compatible route key.
        """
        own = self.db.execute(
            """
            SELECT
                id,
                finish_ms / 1000.0 AS seconds,
                event_count,
                payload_json
            FROM records
            WHERE map_code = ?
              AND mirrored = ?
              AND map_hash = ?
              AND life_timer_version = ?
            ORDER BY finish_ms ASC
            LIMIT 1
            """,
            (
                int(map_code),
                int(bool(mirrored)),
                str(map_hash),
                self.LIFE_TIMER_VERSION,
            ),
        ).fetchone()

        remote = self.db.execute(
            """
            SELECT
                id,
                target_name,
                victory_seconds AS seconds,
                event_count,
                payload_json
            FROM player_records
            WHERE map_code = ?
              AND mirrored = ?
              AND map_hash = ?
              AND end_reason = 'victory'
              AND life_timer_version = ?
              AND victory_seconds IS NOT NULL
            ORDER BY victory_seconds ASC
            LIMIT 1
            """,
            (
                int(map_code),
                int(bool(mirrored)),
                str(map_hash),
                self.LIFE_TIMER_VERSION,
            ),
        ).fetchone()

        candidates = []

        if own is not None:
            candidates.append(
                {
                    "source": "SELF",
                    "id": int(own["id"]),
                    "name": "SELF",
                    "seconds": float(own["seconds"]),
                    "points": int(own["event_count"]),
                    "payload_json": own["payload_json"],
                }
            )

        if remote is not None:
            candidates.append(
                {
                    "source": "PLAYER",
                    "id": int(remote["id"]),
                    "name": str(remote["target_name"]),
                    "seconds": float(remote["seconds"]),
                    "points": int(remote["event_count"]),
                    "payload_json": remote["payload_json"],
                }
            )

        if not candidates:
            return None

        candidates.sort(
            key=lambda item: (
                item["seconds"],
                0 if item["source"] == "SELF" else 1,
                item["id"],
            )
        )

        return candidates[0]

    def _delete_exact_locked(
        self,
        *,
        map_code,
        mirrored,
        map_hash,
    ):
        own_count = self.db.execute(
            """
            DELETE FROM records
            WHERE map_code = ?
              AND mirrored = ?
              AND map_hash = ?
            """,
            (
                int(map_code),
                int(bool(mirrored)),
                str(map_hash),
            ),
        ).rowcount

        player_count = self.db.execute(
            """
            DELETE FROM player_records
            WHERE map_code = ?
              AND mirrored = ?
              AND map_hash = ?
            """,
            (
                int(map_code),
                int(bool(mirrored)),
                str(map_hash),
            ),
        ).rowcount

        return (
            int(own_count or 0)
            + int(player_count or 0)
        )

    def save(self, record):
        """
        BEST-ONLY SELF SAVE.

        If there is no saved route:
            insert

        If this run is faster:
            delete old self/player route(s) for the exact map key
            insert this run

        If this run is slower or equal:
            do not save
        """
        map_code = int(record["mapCode"])
        mirrored = bool(record["mirrored"])
        map_hash = str(record["mapHash"])
        seconds = float(record["finishMs"]) / 1000.0

        payload = json.dumps(
            record,
            separators=(",", ":"),
        )

        life_timer_version = int(
            record.get(
                "lifeTimerVersion",
                self.LIFE_TIMER_VERSION,
            )
        )

        with self.lock:
            best = self._best_exact_locked(
                map_code=map_code,
                mirrored=mirrored,
                map_hash=map_hash,
            )

            if (
                best is not None
                and seconds >= best["seconds"]
            ):
                print(
                    f"[DB] BEST KEEP "
                    f"map={map_code} "
                    f"best={best['seconds']:.3f}s "
                    f"new={seconds:.3f}s "
                    f"source={best['name']}"
                )

                return None

            replaced = self._delete_exact_locked(
                map_code=map_code,
                mirrored=mirrored,
                map_hash=map_hash,
            )

            cursor = self.db.execute(
                """
                INSERT INTO records (
                    map_code,
                    mirrored,
                    map_hash,
                    finish_ms,
                    event_count,
                    payload_json,
                    life_timer_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    map_code,
                    int(mirrored),
                    map_hash,
                    float(record["finishMs"]),
                    len(record["events"]),
                    payload,
                    life_timer_version,
                ),
            )

            self.db.commit()
            record_id = int(cursor.lastrowid)

        print(
            f"[DB] BEST OVERWRITE "
            f"id={record_id} "
            f"map={map_code} "
            f"time={seconds:.3f}s "
            f"source=SELF "
            f"replacedRows={replaced}"
        )

        return record_id

    def get_best(
        self,
        *,
        map_code,
        mirrored,
        map_hash,
    ):
        with self.lock:
            row = self.db.execute(
                """
                SELECT
                    id,
                    payload_json
                FROM records
                WHERE map_code = ?
                  AND mirrored = ?
                  AND map_hash = ?
                  AND life_timer_version = ?
                ORDER BY finish_ms ASC
                LIMIT 1
                """,
                (
                    int(map_code),
                    int(bool(mirrored)),
                    str(map_hash),
                    self.LIFE_TIMER_VERSION,
                ),
            ).fetchone()

        if row is None:
            return None

        record = json.loads(
            row["payload_json"]
        )

        record["id"] = int(
            row["id"]
        )

        return record

    def save_player_record(
        self,
        record,
    ):
        """
        BEST-ONLY LEARNED PLAYER SAVE.

        Only successful victory lives are eligible.
        Competes directly against the saved SELF best.
        """
        if (
            str(record.get("reason", ""))
            != "victory"
        ):
            print(
                "[DB] PLAYER SKIP "
                f"reason={record.get('reason')}"
            )
            return None

        map_code = int(record["mapCode"])
        mirrored = bool(record["mirrored"])
        map_hash = str(record["mapHash"])
        seconds = float(record["victorySeconds"])

        payload = json.dumps(
            record,
            separators=(",", ":"),
        )

        life_timer_version = int(
            record.get(
                "lifeTimerVersion",
                self.LIFE_TIMER_VERSION,
            )
        )

        with self.lock:
            best = self._best_exact_locked(
                map_code=map_code,
                mirrored=mirrored,
                map_hash=map_hash,
            )

            if (
                best is not None
                and seconds >= best["seconds"]
            ):
                print(
                    f"[DB] BEST KEEP "
                    f"map={map_code} "
                    f"best={best['seconds']:.3f}s "
                    f"new={seconds:.3f}s "
                    f"candidate={record['targetName']}"
                )

                return None

            replaced = self._delete_exact_locked(
                map_code=map_code,
                mirrored=mirrored,
                map_hash=map_hash,
            )

            cursor = self.db.execute(
                """
                INSERT INTO player_records (
                    target_name,
                    target_session_id,
                    map_code,
                    mirrored,
                    map_hash,
                    round_id,
                    end_reason,
                    victory_seconds,
                    event_count,
                    payload_json,
                    life_timer_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(record["targetName"]),
                    (
                        None
                        if record.get("targetSessionId") is None
                        else int(record["targetSessionId"])
                    ),
                    map_code,
                    int(mirrored),
                    map_hash,
                    (
                        None
                        if record.get("roundId") is None
                        else int(record["roundId"])
                    ),
                    "victory",
                    seconds,
                    len(record.get("events", [])),
                    payload,
                    life_timer_version,
                ),
            )

            self.db.commit()
            record_id = int(cursor.lastrowid)

        print(
            f"[DB] BEST OVERWRITE "
            f"id={record_id} "
            f"map={map_code} "
            f"time={seconds:.3f}s "
            f"source={record['targetName']} "
            f"replacedRows={replaced}"
        )

        return record_id

    def get_best_player_record(
        self,
        *,
        target_name,
        map_code,
        mirrored,
        map_hash,
    ):
        """
        Used only by explicit /playplayer Nick.

        With BEST-only global storage this succeeds only if that player's
        route is currently the global best for this exact map key.
        """
        with self.lock:
            row = self.db.execute(
                """
                SELECT
                    id,
                    payload_json
                FROM player_records
                WHERE lower(target_name) = lower(?)
                  AND map_code = ?
                  AND mirrored = ?
                  AND map_hash = ?
                  AND end_reason = 'victory'
                  AND life_timer_version = ?
                  AND victory_seconds IS NOT NULL
                ORDER BY victory_seconds ASC
                LIMIT 1
                """,
                (
                    str(target_name),
                    int(map_code),
                    int(bool(mirrored)),
                    str(map_hash),
                    self.LIFE_TIMER_VERSION,
                ),
            ).fetchone()

        if row is None:
            return None

        record = json.loads(
            row["payload_json"]
        )
        record["id"] = int(row["id"])
        return record

    def get_best_any_route(
        self,
        *,
        map_code,
        mirrored,
        map_hash,
    ):
        with self.lock:
            best = self._best_exact_locked(
                map_code=map_code,
                mirrored=mirrored,
                map_hash=map_hash,
            )

        if best is None:
            print(
                f"[DB] BEST MISS map={map_code}"
            )
            return None

        record = json.loads(
            best["payload_json"]
        )
        record["id"] = best["id"]

        print(
            f"[DB] BEST HIT "
            f"map={map_code} "
            f"time={best['seconds']:.3f}s "
            f"source={best['name']} "
            f"points={best['points']}"
        )

        return record

    def get_time_best(
        self,
        *,
        map_code,
    ):
        """
        User-facing lookup by map code.

        There can technically be separate exact route variants
        (different mapHash/mirrored). Return only the single fastest one.
        """
        items = []

        with self.lock:
            own_rows = self.db.execute(
                """
                SELECT
                    id,
                    finish_ms / 1000.0 AS seconds,
                    event_count,
                    payload_json
                FROM records
                WHERE map_code = ?
                  AND life_timer_version = ?
                """,
                (
                    int(map_code),
                    self.LIFE_TIMER_VERSION,
                ),
            ).fetchall()

            player_rows = self.db.execute(
                """
                SELECT
                    id,
                    target_name,
                    victory_seconds AS seconds,
                    event_count,
                    payload_json
                FROM player_records
                WHERE map_code = ?
                  AND end_reason = 'victory'
                  AND life_timer_version = ?
                  AND victory_seconds IS NOT NULL
                """,
                (
                    int(map_code),
                    self.LIFE_TIMER_VERSION,
                ),
            ).fetchall()

        for row in own_rows:
            payload = json.loads(row["payload_json"])

            items.append(
                {
                    "source": "SELF",
                    "id": int(row["id"]),
                    "name": "SELF",
                    "seconds": float(row["seconds"]),
                    "points": int(row["event_count"]),
                    "mirrored": bool(payload.get("mirrored", False)),
                    "mapHash": payload.get("mapHash"),
                }
            )

        for row in player_rows:
            payload = json.loads(row["payload_json"])

            items.append(
                {
                    "source": "PLAYER",
                    "id": int(row["id"]),
                    "name": str(row["target_name"]),
                    "seconds": float(row["seconds"]),
                    "points": int(row["event_count"]),
                    "mirrored": bool(payload.get("mirrored", False)),
                    "mapHash": payload.get("mapHash"),
                }
            )

        if not items:
            return None

        items.sort(
            key=lambda item: (
                item["seconds"],
                0 if item["source"] == "SELF" else 1,
                item["id"],
            )
        )

        return items[0]

    def delete_map(
        self,
        *,
        map_code,
    ):
        """
        Delete every saved self/player route for one map code,
        including old lifecycle versions and alternate variants.
        """
        with self.lock:
            own = self.db.execute(
                """
                DELETE FROM records
                WHERE map_code = ?
                """,
                (int(map_code),),
            ).rowcount

            player = self.db.execute(
                """
                DELETE FROM player_records
                WHERE map_code = ?
                """,
                (int(map_code),),
            ).rowcount

            self.db.commit()

        deleted = (
            int(own or 0)
            + int(player or 0)
        )

        print(
            f"[DB] DELETE MAP "
            f"@{int(map_code)} "
            f"rows={deleted}"
        )

        return deleted

    def delete_all(self):
        """
        Delete ALL route records from both tables.
        """
        with self.lock:
            own = self.db.execute(
                "DELETE FROM records"
            ).rowcount

            player = self.db.execute(
                "DELETE FROM player_records"
            ).rowcount

            self.db.commit()

        deleted = (
            int(own or 0)
            + int(player or 0)
        )

        print(
            f"[DB] DELETE ALL rows={deleted}"
        )

        return deleted

    def compact_best_records(self):
        """
        Migrate an existing V1.10 DB to V1.11 BEST-only semantics.

        For each exact clean lifecycle-v3 route key:
            retain only the fastest row across BOTH tables.

        Old lifecycle rows remain untouched until /timedelete is used,
        because autoplay already ignores them.
        """
        with self.lock:
            keys = self.db.execute(
                """
                SELECT map_code, mirrored, map_hash
                FROM records
                WHERE life_timer_version = ?
                UNION
                SELECT map_code, mirrored, map_hash
                FROM player_records
                WHERE life_timer_version = ?
                  AND end_reason = 'victory'
                """,
                (
                    self.LIFE_TIMER_VERSION,
                    self.LIFE_TIMER_VERSION,
                ),
            ).fetchall()

            removed = 0

            for key in keys:
                map_code = int(key["map_code"])
                mirrored = int(key["mirrored"])
                map_hash = str(key["map_hash"])

                best = self._best_exact_locked(
                    map_code=map_code,
                    mirrored=mirrored,
                    map_hash=map_hash,
                )

                if best is None:
                    continue

                if best["source"] == "SELF":
                    removed += int(
                        self.db.execute(
                            """
                            DELETE FROM records
                            WHERE map_code = ?
                              AND mirrored = ?
                              AND map_hash = ?
                              AND life_timer_version = ?
                              AND id != ?
                            """,
                            (
                                map_code,
                                mirrored,
                                map_hash,
                                self.LIFE_TIMER_VERSION,
                                best["id"],
                            ),
                        ).rowcount
                        or 0
                    )

                    removed += int(
                        self.db.execute(
                            """
                            DELETE FROM player_records
                            WHERE map_code = ?
                              AND mirrored = ?
                              AND map_hash = ?
                              AND life_timer_version = ?
                            """,
                            (
                                map_code,
                                mirrored,
                                map_hash,
                                self.LIFE_TIMER_VERSION,
                            ),
                        ).rowcount
                        or 0
                    )

                else:
                    removed += int(
                        self.db.execute(
                            """
                            DELETE FROM player_records
                            WHERE map_code = ?
                              AND mirrored = ?
                              AND map_hash = ?
                              AND life_timer_version = ?
                              AND id != ?
                            """,
                            (
                                map_code,
                                mirrored,
                                map_hash,
                                self.LIFE_TIMER_VERSION,
                                best["id"],
                            ),
                        ).rowcount
                        or 0
                    )

                    removed += int(
                        self.db.execute(
                            """
                            DELETE FROM records
                            WHERE map_code = ?
                              AND mirrored = ?
                              AND map_hash = ?
                              AND life_timer_version = ?
                            """,
                            (
                                map_code,
                                mirrored,
                                map_hash,
                                self.LIFE_TIMER_VERSION,
                            ),
                        ).rowcount
                        or 0
                    )

            self.db.commit()

        if removed:
            print(
                f"[DB] BEST COMPACT removedRows={removed}"
            )

        return removed
