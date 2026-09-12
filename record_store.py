import json
import sqlite3
import threading
from pathlib import Path


class RecordStore:
    """
    emirhankarakoc v1.1 storage

    Rules:
      - lifecycle v3 only
      - minimum replayable time: 7.100 seconds
      - one BEST record per exact map identity
      - SELF and learned PLAYER routes compete for that one BEST
      - blacklist is persistent
      - records have an editable owner
    """

    LIFE_TIMER_VERSION = 3
    MIN_RECORD_SECONDS = 7.1

    def __init__(self, db_path="robot_records.db"):
        self.db_path = Path(db_path)
        self.lock = threading.RLock()

        self.db = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
        )
        self.db.row_factory = sqlite3.Row

        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")

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
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    life_timer_version INTEGER NOT NULL DEFAULT 0,
                    owner_name TEXT NOT NULL DEFAULT 'SELF'
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
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    life_timer_version INTEGER NOT NULL DEFAULT 0
                )
                """
            )

            self.db.execute(
                """
                CREATE TABLE IF NOT EXISTS blacklist (
                    name_key TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            self._ensure_column(
                "records",
                "life_timer_version",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                "records",
                "owner_name",
                "TEXT NOT NULL DEFAULT 'SELF'",
            )
            self._ensure_column(
                "player_records",
                "life_timer_version",
                "INTEGER NOT NULL DEFAULT 0",
            )

            self.db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_records_best_v3
                ON records(
                    map_code, mirrored, map_hash,
                    life_timer_version, finish_ms
                )
                """
            )
            self.db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_player_records_best_v3
                ON player_records(
                    map_code, mirrored, map_hash,
                    life_timer_version, end_reason, victory_seconds
                )
                """
            )
            self.db.commit()

        self.compact_best_records()

        print(
            f"[DB] ready path={self.db_path.resolve()} "
            f"lifeTimerVersion={self.LIFE_TIMER_VERSION} "
            f"minTime={self.MIN_RECORD_SECONDS:.3f}s"
        )

    def _ensure_column(self, table, column, declaration):
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

    @staticmethod
    def _key(name):
        return str(name).strip().casefold()

    def is_blacklisted(self, name):
        if not name:
            return False

        with self.lock:
            row = self.db.execute(
                "SELECT 1 FROM blacklist WHERE name_key = ?",
                (self._key(name),),
            ).fetchone()

        return row is not None

    def blacklist_add(self, name):
        name = str(name).strip()
        if not name:
            return 0

        key = self._key(name)

        with self.lock:
            self.db.execute(
                """
                INSERT INTO blacklist(name_key, display_name)
                VALUES(?, ?)
                ON CONFLICT(name_key)
                DO UPDATE SET
                    display_name=excluded.display_name,
                    created_at=CURRENT_TIMESTAMP
                """,
                (key, name),
            )

            deleted_self = self.db.execute(
                "DELETE FROM records WHERE lower(owner_name)=lower(?)",
                (name,),
            ).rowcount

            deleted_player = self.db.execute(
                "DELETE FROM player_records WHERE lower(target_name)=lower(?)",
                (name,),
            ).rowcount

            self.db.commit()

        deleted = int(deleted_self or 0) + int(deleted_player or 0)

        print(
            f"[BLACKLIST] ADD {name} "
            f"deletedOwnedRecords={deleted}"
        )
        return deleted

    def blacklist_remove(self, name):
        name = str(name).strip()
        with self.lock:
            count = self.db.execute(
                "DELETE FROM blacklist WHERE name_key = ?",
                (self._key(name),),
            ).rowcount
            self.db.commit()

        print(f"[BLACKLIST] REMOVE {name} rows={int(count or 0)}")
        return int(count or 0)

    def blacklist_clear(self):
        with self.lock:
            count = self.db.execute(
                "DELETE FROM blacklist"
            ).rowcount
            self.db.commit()

        print(f"[BLACKLIST] CLEAR rows={int(count or 0)}")
        return int(count or 0)

    def blacklist_list(self):
        with self.lock:
            rows = self.db.execute(
                """
                SELECT display_name
                FROM blacklist
                ORDER BY lower(display_name)
                """
            ).fetchall()

        return [str(row["display_name"]) for row in rows]

    def _owner_blacklisted_locked(self, owner):
        if not owner:
            return False

        row = self.db.execute(
            "SELECT 1 FROM blacklist WHERE name_key = ?",
            (self._key(owner),),
        ).fetchone()

        return row is not None

    def _best_exact_locked(self, *, map_code, mirrored, map_hash):
        own = self.db.execute(
            """
            SELECT
                id,
                owner_name,
                finish_ms / 1000.0 AS seconds,
                event_count,
                payload_json
            FROM records
            WHERE map_code = ?
              AND mirrored = ?
              AND map_hash = ?
              AND life_timer_version = ?
              AND finish_ms >= ?
              AND NOT EXISTS (
                    SELECT 1
                    FROM blacklist b
                    WHERE b.name_key = lower(records.owner_name)
              )
            ORDER BY finish_ms ASC
            LIMIT 1
            """,
            (
                int(map_code),
                int(bool(mirrored)),
                str(map_hash),
                self.LIFE_TIMER_VERSION,
                self.MIN_RECORD_SECONDS * 1000.0,
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
              AND victory_seconds >= ?
              AND NOT EXISTS (
                    SELECT 1
                    FROM blacklist b
                    WHERE b.name_key = lower(player_records.target_name)
              )
            ORDER BY victory_seconds ASC
            LIMIT 1
            """,
            (
                int(map_code),
                int(bool(mirrored)),
                str(map_hash),
                self.LIFE_TIMER_VERSION,
                self.MIN_RECORD_SECONDS,
            ),
        ).fetchone()

        candidates = []

        if own is not None:
            candidates.append(
                {
                    "source": "SELF",
                    "id": int(own["id"]),
                    "name": str(own["owner_name"] or "SELF"),
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

    def _delete_exact_locked(self, *, map_code, mirrored, map_hash):
        a = self.db.execute(
            """
            DELETE FROM records
            WHERE map_code=? AND mirrored=? AND map_hash=?
            """,
            (int(map_code), int(bool(mirrored)), str(map_hash)),
        ).rowcount

        b = self.db.execute(
            """
            DELETE FROM player_records
            WHERE map_code=? AND mirrored=? AND map_hash=?
            """,
            (int(map_code), int(bool(mirrored)), str(map_hash)),
        ).rowcount

        return int(a or 0) + int(b or 0)

    def save(self, record):
        record = dict(record)

        map_code = int(record["mapCode"])
        mirrored = bool(record["mirrored"])
        map_hash = str(record["mapHash"])
        seconds = float(record["finishMs"]) / 1000.0

        owner = str(
            record.get("ownerName")
            or record.get("targetName")
            or "SELF"
        )

        record["ownerName"] = owner
        record["targetName"] = owner

        if seconds < self.MIN_RECORD_SECONDS:
            print(
                f"[DB] SKIP TOO SHORT "
                f"map=@{map_code} owner={owner} "
                f"time={seconds:.3f}s "
                f"minimum={self.MIN_RECORD_SECONDS:.3f}s"
            )
            return None

        if self.is_blacklisted(owner):
            print(
                f"[DB] SKIP BLACKLISTED "
                f"map=@{map_code} owner={owner}"
            )
            return None

        payload = json.dumps(record, separators=(",", ":"))

        with self.lock:
            best = self._best_exact_locked(
                map_code=map_code,
                mirrored=mirrored,
                map_hash=map_hash,
            )

            if best is not None and seconds >= best["seconds"]:
                print(
                    f"[DB] BEST KEEP map=@{map_code} "
                    f"best={best['seconds']:.3f}s "
                    f"owner={best['name']} "
                    f"candidate={owner} {seconds:.3f}s"
                )
                return None

            replaced = self._delete_exact_locked(
                map_code=map_code,
                mirrored=mirrored,
                map_hash=map_hash,
            )

            cursor = self.db.execute(
                """
                INSERT INTO records(
                    map_code, mirrored, map_hash,
                    finish_ms, event_count, payload_json,
                    life_timer_version, owner_name
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    map_code,
                    int(mirrored),
                    map_hash,
                    float(record["finishMs"]),
                    len(record.get("events", [])),
                    payload,
                    self.LIFE_TIMER_VERSION,
                    owner,
                ),
            )
            self.db.commit()
            record_id = int(cursor.lastrowid)

        print(
            f"[DB] BEST OVERWRITE id={record_id} "
            f"map=@{map_code} time={seconds:.3f}s "
            f"owner={owner} replacedRows={replaced}"
        )
        return record_id

    def save_player_record(self, record):
        record = dict(record)

        if str(record.get("reason", "")) != "victory":
            print(
                f"[DB] PLAYER SKIP reason={record.get('reason')}"
            )
            return None

        map_code = int(record["mapCode"])
        mirrored = bool(record["mirrored"])
        map_hash = str(record["mapHash"])
        seconds = float(record["victorySeconds"])
        owner = str(record["targetName"])

        if seconds < self.MIN_RECORD_SECONDS:
            print(
                f"[DB] PLAYER SKIP TOO SHORT "
                f"map=@{map_code} owner={owner} "
                f"time={seconds:.3f}s "
                f"minimum={self.MIN_RECORD_SECONDS:.3f}s"
            )
            return None

        if self.is_blacklisted(owner):
            print(
                f"[DB] PLAYER SKIP BLACKLISTED "
                f"map=@{map_code} owner={owner}"
            )
            return None

        payload = json.dumps(record, separators=(",", ":"))

        with self.lock:
            best = self._best_exact_locked(
                map_code=map_code,
                mirrored=mirrored,
                map_hash=map_hash,
            )

            if best is not None and seconds >= best["seconds"]:
                print(
                    f"[DB] BEST KEEP map=@{map_code} "
                    f"best={best['seconds']:.3f}s "
                    f"owner={best['name']} "
                    f"candidate={owner} {seconds:.3f}s"
                )
                return None

            replaced = self._delete_exact_locked(
                map_code=map_code,
                mirrored=mirrored,
                map_hash=map_hash,
            )

            cursor = self.db.execute(
                """
                INSERT INTO player_records(
                    target_name, target_session_id,
                    map_code, mirrored, map_hash, round_id,
                    end_reason, victory_seconds, event_count,
                    payload_json, life_timer_version
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    owner,
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
                    self.LIFE_TIMER_VERSION,
                ),
            )
            self.db.commit()
            record_id = int(cursor.lastrowid)

        print(
            f"[DB] BEST OVERWRITE id={record_id} "
            f"map=@{map_code} time={seconds:.3f}s "
            f"owner={owner} replacedRows={replaced}"
        )
        return record_id

    def get_best(self, *, map_code, mirrored, map_hash):
        with self.lock:
            row = self.db.execute(
                """
                SELECT id, payload_json, owner_name
                FROM records
                WHERE map_code=?
                  AND mirrored=?
                  AND map_hash=?
                  AND life_timer_version=?
                  AND finish_ms >= ?
                  AND NOT EXISTS(
                    SELECT 1 FROM blacklist b
                    WHERE b.name_key=lower(records.owner_name)
                  )
                ORDER BY finish_ms ASC
                LIMIT 1
                """,
                (
                    int(map_code),
                    int(bool(mirrored)),
                    str(map_hash),
                    self.LIFE_TIMER_VERSION,
                    self.MIN_RECORD_SECONDS * 1000.0,
                ),
            ).fetchone()

        if row is None:
            return None

        record = json.loads(row["payload_json"])
        record["id"] = int(row["id"])
        record.setdefault("ownerName", str(row["owner_name"] or "SELF"))
        record.setdefault("targetName", record["ownerName"])
        return record

    def get_best_player_record(
        self,
        *,
        target_name,
        map_code,
        mirrored,
        map_hash,
    ):
        if self.is_blacklisted(target_name):
            return None

        with self.lock:
            row = self.db.execute(
                """
                SELECT id, payload_json
                FROM player_records
                WHERE lower(target_name)=lower(?)
                  AND map_code=?
                  AND mirrored=?
                  AND map_hash=?
                  AND end_reason='victory'
                  AND life_timer_version=?
                  AND victory_seconds >= ?
                ORDER BY victory_seconds ASC
                LIMIT 1
                """,
                (
                    str(target_name),
                    int(map_code),
                    int(bool(mirrored)),
                    str(map_hash),
                    self.LIFE_TIMER_VERSION,
                    self.MIN_RECORD_SECONDS,
                ),
            ).fetchone()

        if row is None:
            return None

        record = json.loads(row["payload_json"])
        record["id"] = int(row["id"])
        return record

    def get_best_any_route(self, *, map_code, mirrored, map_hash):
        with self.lock:
            best = self._best_exact_locked(
                map_code=map_code,
                mirrored=mirrored,
                map_hash=map_hash,
            )

        if best is None:
            print(f"[DB] BEST MISS map=@{int(map_code)}")
            return None

        record = json.loads(best["payload_json"])
        record["id"] = best["id"]
        record.setdefault("targetName", best["name"])
        record.setdefault("ownerName", best["name"])

        print(
            f"[DB] BEST HIT map=@{int(map_code)} "
            f"time={best['seconds']:.3f}s "
            f"owner={best['name']} points={best['points']}"
        )
        return record

    def get_time_best(self, *, map_code):
        items = []

        with self.lock:
            own_rows = self.db.execute(
                """
                SELECT
                    id, owner_name,
                    finish_ms / 1000.0 AS seconds,
                    event_count, payload_json
                FROM records
                WHERE map_code=?
                  AND life_timer_version=?
                  AND finish_ms >= ?
                  AND NOT EXISTS(
                    SELECT 1 FROM blacklist b
                    WHERE b.name_key=lower(records.owner_name)
                  )
                """,
                (
                    int(map_code),
                    self.LIFE_TIMER_VERSION,
                    self.MIN_RECORD_SECONDS * 1000.0,
                ),
            ).fetchall()

            player_rows = self.db.execute(
                """
                SELECT
                    id, target_name,
                    victory_seconds AS seconds,
                    event_count, payload_json
                FROM player_records
                WHERE map_code=?
                  AND end_reason='victory'
                  AND life_timer_version=?
                  AND victory_seconds >= ?
                  AND NOT EXISTS(
                    SELECT 1 FROM blacklist b
                    WHERE b.name_key=lower(player_records.target_name)
                  )
                """,
                (
                    int(map_code),
                    self.LIFE_TIMER_VERSION,
                    self.MIN_RECORD_SECONDS,
                ),
            ).fetchall()

        for row in own_rows:
            payload = json.loads(row["payload_json"])
            items.append(
                {
                    "source": "SELF",
                    "id": int(row["id"]),
                    "name": str(row["owner_name"] or "SELF"),
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

    def get_all_time_bests(self):
        """
        Return one fastest usable BEST row per mapCode.
        Exact replay compatibility remains mapCode+mirrored+mapHash.
        """
        with self.lock:
            own_rows = self.db.execute(
                """
                SELECT id, map_code, owner_name,
                       finish_ms / 1000.0 AS seconds,
                       event_count, payload_json
                FROM records
                WHERE life_timer_version=?
                  AND finish_ms >= ?
                  AND NOT EXISTS(
                    SELECT 1 FROM blacklist b
                    WHERE b.name_key=lower(records.owner_name)
                  )
                """,
                (
                    self.LIFE_TIMER_VERSION,
                    self.MIN_RECORD_SECONDS * 1000.0,
                ),
            ).fetchall()

            player_rows = self.db.execute(
                """
                SELECT id, map_code, target_name,
                       victory_seconds AS seconds,
                       event_count, payload_json
                FROM player_records
                WHERE end_reason='victory'
                  AND life_timer_version=?
                  AND victory_seconds >= ?
                  AND NOT EXISTS(
                    SELECT 1 FROM blacklist b
                    WHERE b.name_key=lower(player_records.target_name)
                  )
                """,
                (
                    self.LIFE_TIMER_VERSION,
                    self.MIN_RECORD_SECONDS,
                ),
            ).fetchall()

        by_map = {}

        def consider(item):
            code = int(item["mapCode"])
            cur = by_map.get(code)

            if cur is None or float(item["seconds"]) < float(cur["seconds"]):
                by_map[code] = item

        for row in own_rows:
            payload = json.loads(row["payload_json"])
            consider(
                {
                    "source": "SELF",
                    "id": int(row["id"]),
                    "mapCode": int(row["map_code"]),
                    "name": str(row["owner_name"] or "SELF"),
                    "seconds": float(row["seconds"]),
                    "points": int(row["event_count"]),
                    "mirrored": bool(payload.get("mirrored", False)),
                    "mapHash": payload.get("mapHash"),
                }
            )

        for row in player_rows:
            payload = json.loads(row["payload_json"])
            consider(
                {
                    "source": "PLAYER",
                    "id": int(row["id"]),
                    "mapCode": int(row["map_code"]),
                    "name": str(row["target_name"]),
                    "seconds": float(row["seconds"]),
                    "points": int(row["event_count"]),
                    "mirrored": bool(payload.get("mirrored", False)),
                    "mapHash": payload.get("mapHash"),
                }
            )

        return [by_map[k] for k in sorted(by_map)]

    def set_best_owner(self, *, map_code, new_owner):
        new_owner = str(new_owner).strip()

        if not new_owner:
            return {"ok": False, "reason": "empty-owner"}

        if self.is_blacklisted(new_owner):
            return {"ok": False, "reason": "owner-blacklisted"}

        best = self.get_time_best(map_code=map_code)
        if best is None:
            return {"ok": False, "reason": "no-record"}

        with self.lock:
            if best["source"] == "SELF":
                row = self.db.execute(
                    "SELECT payload_json FROM records WHERE id=?",
                    (best["id"],),
                ).fetchone()

                payload = json.loads(row["payload_json"])
                payload["ownerName"] = new_owner
                payload["targetName"] = new_owner

                self.db.execute(
                    """
                    UPDATE records
                    SET owner_name=?, payload_json=?
                    WHERE id=?
                    """,
                    (
                        new_owner,
                        json.dumps(payload, separators=(",", ":")),
                        best["id"],
                    ),
                )
            else:
                row = self.db.execute(
                    "SELECT payload_json FROM player_records WHERE id=?",
                    (best["id"],),
                ).fetchone()

                payload = json.loads(row["payload_json"])
                payload["targetName"] = new_owner

                self.db.execute(
                    """
                    UPDATE player_records
                    SET target_name=?, payload_json=?
                    WHERE id=?
                    """,
                    (
                        new_owner,
                        json.dumps(payload, separators=(",", ":")),
                        best["id"],
                    ),
                )

            self.db.commit()

        print(
            f"[DB] OWNER CHANGE map=@{int(map_code)} "
            f"{best['name']} -> {new_owner}"
        )

        return {
            "ok": True,
            "oldOwner": best["name"],
            "newOwner": new_owner,
            "seconds": best["seconds"],
        }

    def delete_map(self, *, map_code):
        with self.lock:
            a = self.db.execute(
                "DELETE FROM records WHERE map_code=?",
                (int(map_code),),
            ).rowcount
            b = self.db.execute(
                "DELETE FROM player_records WHERE map_code=?",
                (int(map_code),),
            ).rowcount
            self.db.commit()

        deleted = int(a or 0) + int(b or 0)
        print(f"[DB] DELETE MAP @{int(map_code)} rows={deleted}")
        return deleted

    def delete_all(self):
        with self.lock:
            a = self.db.execute("DELETE FROM records").rowcount
            b = self.db.execute("DELETE FROM player_records").rowcount
            self.db.commit()

        deleted = int(a or 0) + int(b or 0)
        print(f"[DB] DELETE ALL rows={deleted}")
        return deleted

    def compact_best_records(self):
        """
        Convert the existing DB to the current rules:
          - drop old lifecycle generations
          - drop <11s records
          - drop blacklisted owners
          - one best row per exact map identity
        """
        with self.lock:
            removed = 0

            removed += int(
                self.db.execute(
                    "DELETE FROM records WHERE life_timer_version != ?",
                    (self.LIFE_TIMER_VERSION,),
                ).rowcount or 0
            )
            removed += int(
                self.db.execute(
                    "DELETE FROM player_records WHERE life_timer_version != ?",
                    (self.LIFE_TIMER_VERSION,),
                ).rowcount or 0
            )

            removed += int(
                self.db.execute(
                    "DELETE FROM records WHERE finish_ms < ?",
                    (self.MIN_RECORD_SECONDS * 1000.0,),
                ).rowcount or 0
            )
            removed += int(
                self.db.execute(
                    """
                    DELETE FROM player_records
                    WHERE victory_seconds IS NULL
                       OR victory_seconds < ?
                       OR end_reason != 'victory'
                    """,
                    (self.MIN_RECORD_SECONDS,),
                ).rowcount or 0
            )

            removed += int(
                self.db.execute(
                    """
                    DELETE FROM records
                    WHERE EXISTS(
                        SELECT 1 FROM blacklist b
                        WHERE b.name_key=lower(records.owner_name)
                    )
                    """
                ).rowcount or 0
            )
            removed += int(
                self.db.execute(
                    """
                    DELETE FROM player_records
                    WHERE EXISTS(
                        SELECT 1 FROM blacklist b
                        WHERE b.name_key=lower(player_records.target_name)
                    )
                    """
                ).rowcount or 0
            )

            keys = self.db.execute(
                """
                SELECT map_code, mirrored, map_hash FROM records
                UNION
                SELECT map_code, mirrored, map_hash FROM player_records
                """
            ).fetchall()

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
                            WHERE map_code=? AND mirrored=? AND map_hash=? AND id != ?
                            """,
                            (map_code, mirrored, map_hash, best["id"]),
                        ).rowcount or 0
                    )
                    removed += int(
                        self.db.execute(
                            """
                            DELETE FROM player_records
                            WHERE map_code=? AND mirrored=? AND map_hash=?
                            """,
                            (map_code, mirrored, map_hash),
                        ).rowcount or 0
                    )
                else:
                    removed += int(
                        self.db.execute(
                            """
                            DELETE FROM player_records
                            WHERE map_code=? AND mirrored=? AND map_hash=? AND id != ?
                            """,
                            (map_code, mirrored, map_hash, best["id"]),
                        ).rowcount or 0
                    )
                    removed += int(
                        self.db.execute(
                            """
                            DELETE FROM records
                            WHERE map_code=? AND mirrored=? AND map_hash=?
                            """,
                            (map_code, mirrored, map_hash),
                        ).rowcount or 0
                    )

            self.db.commit()

        if removed:
            print(f"[DB] COMPACT removedRows={removed}")

        return removed
