import hashlib
import time

import pak

from caseus.proxies.proxy import Proxy
from caseus.packets import (
    clientbound,
    serverbound,
)

from recorder import Recorder
from record_store import RecordStore
from replayer import Replayer


class TfmProxy(Proxy):
    """
    TFM V1.2

    ONLY:
        /record on
        /record off
        /play on
        /play off

    RECORD:
        client -> proxy -> backend
        outgoing movement packets are copied into memory
        own victory -> saved directly to SQLite
        death -> current attempt discarded

    PLAY:
        best SQLite record for current map is loaded
        next Alive + first client movement starts replay
        live client movement is blocked
        saved movement packets are sent to backend at recorded timestamps
    """

    def __init__(
        self,
        *args,
        **kwargs,
    ):
        super().__init__(
            *args,
            **kwargs,
        )

        self.store = RecordStore(
            "robot_records.db"
        )

        self.recorder = Recorder()
        self.replayer = Replayer()

        self.record_mode = False
        self.play_mode = False

        self.self_session_id = None
        self.self_name = None

        self.current_map = None
        self.current_round_id = None
        self.current_mirrored = False
        self.current_map_hash = None

        self.self_alive = False

        # Captured from a real client->server connection.
        self.serverbound_source = None

        # PLAY waits until the first outgoing movement after Alive.
        self.play_pending = False

        print(
            "[PROXY] V1.2 listeners ready"
        )

    # ==============================================================
    # HELPERS
    # ==============================================================

    def _bind_source(
        self,
        source,
    ):
        self.serverbound_source = source

    def _map_context_ready(self):
        return (
            self.current_map is not None
            and self.current_round_id is not None
            and self.current_map_hash is not None
        )

    def _arm_record_for_current_map(self):
        if not (
            self.record_mode
            and self._map_context_ready()
        ):
            return

        self.recorder.arm(
            map_code=
                self.current_map,

            mirrored=
                self.current_mirrored,

            map_hash=
                self.current_map_hash,

            round_id=
                self.current_round_id,
        )

    def _load_play_for_current_map(self):
        if not (
            self.play_mode
            and self._map_context_ready()
        ):
            self.replayer.arm(
                None
            )
            return

        record = self.store.get_best(
            map_code=
                self.current_map,

            mirrored=
                self.current_mirrored,

            map_hash=
                self.current_map_hash,
        )

        self.replayer.arm(
            record
        )

    def _on_alive(
        self,
        source_name,
    ):
        # Duplicate Alive packets are common.
        if self.self_alive:
            return

        self.self_alive = True

        print(
            f"[LIFE] ALIVE "
            f"map={self.current_map} "
            f"round={self.current_round_id} "
            f"source={source_name}"
        )

        if self.record_mode:
            self.recorder.on_alive(
                time.perf_counter_ns()
            )

        if self.play_mode:
            # We intentionally start when the first outgoing movement
            # gives us a live ClientConnection to the backend.
            self.play_pending = True

    def _on_server_dead(
        self,
        source_name,
    ):
        if not self.self_alive:
            return

        self.self_alive = False

        print(
            f"[LIFE] DEAD "
            f"map={self.current_map} "
            f"source={source_name}"
        )

        if self.record_mode:
            self.recorder.on_death(
                source_name
            )

        if self.play_mode:
            self.replayer.stop(
                "server-death"
            )

            self.play_pending = False

    # ==============================================================
    # LOGIN
    # ==============================================================

    @pak.packet_listener(
        clientbound.LoginSuccessPacket
    )
    async def on_login_success(
        self,
        source,
        packet,
    ):
        self.self_session_id = (
            packet.session_id
        )

        self.self_name = (
            packet.username
        )

        print(
            f"[LOGIN] "
            f"{self.self_name} "
            f"session={self.self_session_id}"
        )

    # ==============================================================
    # NEW ROUND
    # ==============================================================

    @pak.packet_listener(
        clientbound.NewRoundPacket
    )
    async def on_new_round(
        self,
        source,
        packet,
    ):
        self.replayer.stop(
            "new-round"
        )

        self.play_pending = False
        self.self_alive = False

        self.current_map = int(
            packet.map_code
        )

        self.current_round_id = int(
            packet.round_id
        )

        self.current_mirrored = bool(
            packet.mirrored
        )

        xml = str(
            getattr(
                packet,
                "xml",
                "",
            )
        )

        self.current_map_hash = (
            hashlib.sha256(
                xml.encode(
                    "utf-8"
                )
            )
            .hexdigest()
        )

        print(
            f"[MAP] "
            f"map={self.current_map} "
            f"round={self.current_round_id} "
            f"mirrored={self.current_mirrored} "
            f"hash={self.current_map_hash[:12]}"
        )

        if self.record_mode:
            self._arm_record_for_current_map()

        if self.play_mode:
            self._load_play_for_current_map()

    # ==============================================================
    # ALIVE / DEAD FROM SERVER
    # ==============================================================

    @pak.packet_listener(
        clientbound.SetPlayerListPacket
    )
    async def on_player_list(
        self,
        source,
        packet,
    ):
        if self.self_session_id is None:
            return

        for player in packet.players:
            if (
                player.session_id
                != self.self_session_id
            ):
                continue

            activity = getattr(
                player.activity,
                "name",
                str(player.activity),
            ).lower()

            if activity == "alive":
                self._on_alive(
                    "player-list"
                )

            elif activity == "dead":
                self._on_server_dead(
                    "player-list"
                )

            break

    @pak.packet_listener(
        clientbound.UpdatePlayerListPacket
    )
    async def on_player_update(
        self,
        source,
        packet,
    ):
        if self.self_session_id is None:
            return

        player = packet.player

        if (
            player.session_id
            != self.self_session_id
        ):
            return

        activity = getattr(
            player.activity,
            "name",
            str(player.activity),
        ).lower()

        if activity == "alive":
            self._on_alive(
                "player-update"
            )

        elif activity == "dead":
            self._on_server_dead(
                "player-update"
            )

    # ==============================================================
    # COMMANDS
    # ==============================================================

    @pak.packet_listener(
        serverbound.CommandPacket
    )
    async def on_command(
        self,
        source,
        packet,
    ):
        self._bind_source(
            source
        )

        text = (
            packet.command
            .strip()
        )

        if not text:
            return

        parts = text.split(
            maxsplit=1
        )

        command = (
            parts[0]
            .strip()
            .lower()
        )

        argument = (
            parts[1]
            .strip()
            .lower()
            if len(parts) > 1
            else None
        )

        # --------------------------
        # /record
        # --------------------------

        if command == "record":
            if argument is None:
                print(
                    f"[MODE] RECORD="
                    f"{'ON' if self.record_mode else 'OFF'}"
                )
                return self.DO_NOTHING

            if argument in (
                "on",
                "start",
            ):
                self.play_mode = False
                self.play_pending = False
                self.replayer.stop(
                    "record-on"
                )

                self.record_mode = True

                self._arm_record_for_current_map()

                print(
                    "[MODE] RECORD=ON "
                    "PLAY=OFF "
                    "waitingForNextAlive=True"
                )

                return self.DO_NOTHING

            if argument in (
                "off",
                "stop",
            ):
                self.record_mode = False

                self.recorder.disarm(
                    "record-off"
                )

                print(
                    "[MODE] RECORD=OFF"
                )

                return self.DO_NOTHING

            print(
                "[COMMAND] use: "
                "/record on | /record off"
            )

            return self.DO_NOTHING

        # --------------------------
        # /play
        # --------------------------

        if command == "play":
            if argument is None:
                print(
                    f"[MODE] PLAY="
                    f"{'ON' if self.play_mode else 'OFF'}"
                )
                return self.DO_NOTHING

            if argument in (
                "on",
                "start",
            ):
                self.record_mode = False
                self.recorder.disarm(
                    "play-on"
                )

                self.play_mode = True
                self.play_pending = False

                self._load_play_for_current_map()

                print(
                    "[MODE] PLAY=ON "
                    "RECORD=OFF "
                    "waitingForNextAlive=True"
                )

                return self.DO_NOTHING

            if argument in (
                "off",
                "stop",
            ):
                self.play_mode = False
                self.play_pending = False

                self.replayer.stop(
                    "play-off"
                )

                print(
                    "[MODE] PLAY=OFF"
                )

                return self.DO_NOTHING

            print(
                "[COMMAND] use: "
                "/play on | /play off"
            )

            return self.DO_NOTHING

        # Any other slash command belongs to the normal backend.
        return

    # ==============================================================
    # CLIENT -> SERVER MOVEMENT
    # ==============================================================

    @pak.packet_listener(
        serverbound.PlayerMovementPacket
    )
    async def on_self_movement(
        self,
        source,
        packet,
    ):
        self._bind_source(
            source
        )

        # RECORD:
        # save the client's real outgoing coordinate state,
        # then let the original packet continue to the backend.
        if (
            self.record_mode
            and self.recorder.active
        ):
            self.recorder.record_movement(
                packet
            )

            return

        # PLAY:
        # first real movement after Alive starts our saved trajectory,
        # and this live packet is blocked.
        if self.play_mode:
            if (
                self.play_pending
                and self.self_alive
                and not self.replayer.is_active()
            ):
                started = (
                    self.replayer.start(
                        round_id=
                            self.current_round_id,

                        source_conn=
                            source,

                        self_session_id=
                            self.self_session_id,
                    )
                )

                self.play_pending = (
                    not started
                )

            if self.replayer.is_active():
                return self.DO_NOTHING

    # ==============================================================
    # CLIENT -> SERVER DEATH
    # ==============================================================

    @pak.packet_listener(
        serverbound.PlayerDiedPacket
    )
    async def on_self_death(
        self,
        source,
        packet,
    ):
        self._bind_source(
            source
        )

        # In RECORD, death invalidates the current attempt.
        if self.record_mode:
            print(
                "[DEATH] CLIENT -> SERVER"
            )

            self.self_alive = False

            self.recorder.on_death(
                "client-death"
            )

            # Forward the real death packet normally.
            return

        # In PLAY, local client physics is not trusted because its live
        # movement packets are blocked. Wait for the backend's Dead state.
        if (
            self.play_mode
            and self.replayer.is_active()
        ):
            print(
                "[DEATH] LOCAL PLAY DEATH IGNORED; "
                "waitingForServerDead=True"
            )

            return self.DO_NOTHING

        print(
            "[DEATH] CLIENT -> SERVER"
        )

    # ==============================================================
    # SERVER VICTORY
    # ==============================================================

    @pak.packet_listener(
        clientbound.PlayerVictoryPacket
    )
    async def on_player_victory(
        self,
        source,
        packet,
    ):
        if (
            self.self_session_id is None
            or packet.session_id
            != self.self_session_id
        ):
            return

        finish_seconds = float(
            packet.seconds
        )

        print(
            f"[VICTORY] SERVER "
            f"time={finish_seconds:.3f}s"
        )

        if self.record_mode:
            record = (
                self.recorder.finish(
                    finish_seconds=
                        finish_seconds
                )
            )

            if record is not None:
                self.store.save(
                    record
                )

        if self.play_mode:
            self.replayer.stop(
                "server-victory"
            )

            self.play_pending = False

        self.self_alive = False
