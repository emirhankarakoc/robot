import json
import sqlite3
import threading
from pathlib import Path


class RecordStore:
    """
    V1.8 SQLite store.

    Old pre-lifecycle-fix rows are preserved but NOT selected for autoplay.

    New rows use:
        life_timer_version = 2
    """

    LIFE_TIMER_VERSION = 2

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

    def save(self, record):
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
                    int(record["mapCode"]),
                    int(bool(record["mirrored"])),
                    str(record["mapHash"]),
                    float(record["finishMs"]),
                    len(record["events"]),
                    payload,
                    life_timer_version,
                ),
            )

            self.db.commit()

            record_id = int(
                cursor.lastrowid
            )

        print(
            f"[DB] SAVED "
            f"id={record_id} "
            f"map={record['mapCode']} "
            f"lifeTime={record['finishMs'] / 1000.0:.3f}s "
            f"points={len(record['events'])} "
            f"lifeTimerV={life_timer_version}"
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
            print(
                f"[DB] MISS map={map_code} "
                "(no clean lifecycle-v2 self record)"
            )
            return None

        record = json.loads(
            row["payload_json"]
        )

        record["id"] = int(
            row["id"]
        )

        print(
            f"[DB] HIT "
            f"id={record['id']} "
            f"map={map_code} "
            f"lifeTime={record['finishMs'] / 1000.0:.3f}s "
            f"points={len(record.get('events', []))}"
        )

        return record

    def save_player_record(
        self,
        record,
    ):
        if (
            str(
                record.get(
                    "reason",
                    "",
                )
            )
            != "victory"
        ):
            print(
                "[DB] PLAYER SKIP "
                f"reason={record.get('reason')} "
                "(only successful life is stored)"
            )
            return None

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
                        if record.get(
                            "targetSessionId"
                        )
                        is None
                        else int(
                            record[
                                "targetSessionId"
                            ]
                        )
                    ),

                    int(record["mapCode"]),
                    int(bool(record["mirrored"])),
                    str(record["mapHash"]),

                    (
                        None
                        if record.get(
                            "roundId"
                        )
                        is None
                        else int(
                            record["roundId"]
                        )
                    ),

                    "victory",

                    float(
                        record[
                            "victorySeconds"
                        ]
                    ),

                    len(
                        record.get(
                            "events",
                            [],
                        )
                    ),

                    payload,
                    life_timer_version,
                ),
            )

            self.db.commit()

            record_id = int(
                cursor.lastrowid
            )

        print(
            f"[DB] PLAYER SAVED "
            f"id={record_id} "
            f"target={record['targetName']} "
            f"map={record['mapCode']} "
            f"lifeTime={record['victorySeconds']:.3f}s "
            f"points={len(record.get('events', []))} "
            f"lifeTimerV={life_timer_version}"
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
        ONLY successful lifecycle-v2 records are replayable.
        Failed/dead/round-change rows from old versions are ignored.
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
                ORDER BY
                    victory_seconds ASC,
                    event_count DESC
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
            print(
                f"[DB] PLAYER MISS "
                f"target={target_name} "
                f"map={map_code} "
                "(no clean lifecycle-v2 victory)"
            )

            return None

        record = json.loads(
            row["payload_json"]
        )

        record["id"] = int(
            row["id"]
        )

        print(
            f"[DB] PLAYER HIT "
            f"id={record['id']} "
            f"target={record.get('targetName')} "
            f"map={map_code} "
            f"lifeTime={record.get('victorySeconds', 0.0):.3f}s "
            f"points={len(record.get('events', []))}"
        )

        return record

    def get_best_any_route(
        self,
        *,
        map_code,
        mirrored,
        map_hash,
    ):
        """
        Fastest SUCCESSFUL lifecycle-v2 route from:
          1) self records
          2) learned remote winner records
        """
        own = self.get_best(
            map_code=map_code,
            mirrored=mirrored,
            map_hash=map_hash,
        )

        with self.lock:
            row = self.db.execute(
                """
                SELECT
                    id,
                    payload_json,
                    victory_seconds
                FROM player_records
                WHERE map_code = ?
                  AND mirrored = ?
                  AND map_hash = ?
                  AND end_reason = 'victory'
                  AND life_timer_version = ?
                  AND victory_seconds IS NOT NULL
                ORDER BY
                    victory_seconds ASC,
                    event_count DESC
                LIMIT 1
                """,
                (
                    int(map_code),
                    int(bool(mirrored)),
                    str(map_hash),
                    self.LIFE_TIMER_VERSION,
                ),
            ).fetchone()

        remote = None

        if row is not None:
            remote = json.loads(
                row["payload_json"]
            )

            remote["id"] = int(
                row["id"]
            )

        if own is None:
            return remote

        if remote is None:
            return own

        own_seconds = float(
            own["finishMs"]
        ) / 1000.0

        remote_seconds = float(
            remote["victorySeconds"]
        )

        if remote_seconds < own_seconds:
            return remote

        return own
