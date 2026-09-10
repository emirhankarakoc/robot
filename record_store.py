import json
import sqlite3
import threading
from pathlib import Path


class RecordStore:
    """
    V1 local storage.

    One Python process.
    No FastAPI.
    No port 8787.
    No HTTP.

    Successful recordings are written directly to robot_records.db.
    """

    def __init__(self, db_path="robot_records.db"):
        self.db_path = Path(db_path)
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
                        DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            self.db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_records_best
                ON records(
                    map_code,
                    mirrored,
                    map_hash,
                    finish_ms
                )
                """
            )

            self.db.commit()

        print(
            f"[DB] ready path={self.db_path.resolve()}"
        )

    def save(self, record):
        payload = json.dumps(
            record,
            separators=(",", ":"),
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
                    payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    int(record["mapCode"]),
                    int(bool(record["mirrored"])),
                    str(record["mapHash"]),
                    float(record["finishMs"]),
                    len(record["events"]),
                    payload,
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
            f"time={record['finishMs'] / 1000.0:.3f}s "
            f"points={len(record['events'])}"
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
                ORDER BY finish_ms ASC
                LIMIT 1
                """,
                (
                    int(map_code),
                    int(bool(mirrored)),
                    str(map_hash),
                ),
            ).fetchone()

        if row is None:
            print(
                f"[DB] MISS map={map_code}"
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
            f"time={record['finishMs'] / 1000.0:.3f}s "
            f"points={len(record.get('events', []))}"
        )

        return record
