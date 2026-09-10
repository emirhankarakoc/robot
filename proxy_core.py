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
from player_recorder import PlayerRecorder
from local_player_replayer import LocalPlayerReplayer
from winner_recorder import WinnerRecorder


class TfmProxy(Proxy):
    """
    TFM V1.9

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
        self.player_recorder = PlayerRecorder()
        self.local_player_replayer = LocalPlayerReplayer()
        self.winner_recorder = WinnerRecorder()

        # V1.6 persistent autoplay/autolearn state.
        #
        # When PLAY is ON:
        #   route exists -> replay route to owned backend + local mirror
        #   no route     -> play normally and record our attempt
        self.auto_record_fallback = False
        self.selected_route = None

        # First PlayerVictoryPacket of a round is the winner we learn.
        self.first_victory_session_id = None

        # Lightweight player directory used by /recordplayer Nick#0000.
        self.players_by_session = {}
        self.sessions_by_name = {}

        self.record_mode = False
        self.play_mode = False

        self.self_session_id = None
        self.self_name = None

        self.current_map = None
        self.current_round_id = None
        self.current_mirrored = False
        self.current_map_hash = None

        self.self_alive = False

        # Records/racing rooms can emit a fresh PlayerUpdate Alive on
        # respawn without a preceding Dead. A short debounce prevents the
        # normal player-list + player-update spawn pair from double-resetting.
        self.last_self_alive_signal_ns = None
        self.self_alive_signal_debounce_ns = 750_000_000  # 0.75 s

        # Captured from a real client->server connection.
        self.serverbound_source = None

        # PLAY waits until the first outgoing movement after Alive.
        self.play_pending = False

        print(
            "[PROXY] V1.9 listeners ready"
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
        """
        Select the best route for the current map from BOTH sources:

          records        -> our own successful runs
          player_records -> passively learned winner runs

        The returned event schema is intentionally compatible with Replayer.
        """
        self.selected_route = None
        self.auto_record_fallback = False
        self.play_pending = False

        if not (
            self.play_mode
            and self._map_context_ready()
        ):
            self.replayer.arm(None)
            return None

        route = self.store.get_best_any_route(
            map_code=self.current_map,
            mirrored=self.current_mirrored,
            map_hash=self.current_map_hash,
        )

        if route is None:
            self.replayer.arm(None)

            self.auto_record_fallback = True

            # Use the same plain Recorder used by /record.
            self.recorder.arm(
                map_code=self.current_map,
                mirrored=self.current_mirrored,
                map_hash=self.current_map_hash,
                round_id=self.current_round_id,
            )

            print(
                "[AUTOLEARN] NO ROUTE -> "
                "manual movement allowed; recording this attempt"
            )

            return None

        self.selected_route = route
        self.replayer.arm(route)
        self.play_pending = True

        source_name = (
            route.get("targetName")
            or "SELF"
        )

        print(
            f"[AUTOLEARN] ROUTE READY "
            f"id={route.get('id')} "
            f"source={source_name} "
            f"points={len(route.get('events', []))}"
        )

        return route

    def _on_alive(
        self,
        source_name,
    ):
        observed_ns = time.perf_counter_ns()

        already_alive = bool(
            self.self_alive
        )

        last_signal = (
            self.last_self_alive_signal_ns
        )

        separated = (
            last_signal is None
            or (
                observed_ns
                - int(last_signal)
            )
            >= self.self_alive_signal_debounce_ns
        )

        self.last_self_alive_signal_ns = (
            observed_ns
        )

        # Same spawn can generate player-list and player-update Alive very
        # close together. Ignore that pair.
        #
        # BUT: in records/racing/training a respawn can arrive as a NEW
        # player-update Alive without a preceding Dead. If sufficiently
        # separated, that Alive pulse is authoritative evidence of a new life.
        respawn_pulse = (
            already_alive
            and source_name == "player-update"
            and separated
        )

        if already_alive and not respawn_pulse:
            return

        if respawn_pulse:
            print(
                "[LIFE] RESPAWN ALIVE pulse "
                "without Dead -> FORCE RESET t=0"
            )

            if self.recorder.armed:
                self.recorder.on_death(
                    "respawn-alive-pulse"
                )

            if self.replayer.is_active():
                self.replayer.stop(
                    "respawn-alive-pulse"
                )

        self.self_alive = True

        print(
            f"[LIFE] ALIVE "
            f"map={self.current_map} "
            f"round={self.current_round_id} "
            f"source={source_name} "
            f"respawnPulse={respawn_pulse}"
        )

        if self.play_mode:
            if self.auto_record_fallback:
                self.recorder.on_alive(
                    observed_ns,
                    source=(
                        "respawn-alive-pulse"
                        if respawn_pulse
                        else source_name
                    ),
                )
            else:
                self.play_pending = (
                    self.selected_route is not None
                )

                if (
                    self.play_pending
                    and self.serverbound_source is not None
                    and not self.replayer.is_active()
                ):
                    started = self.replayer.start(
                        round_id=self.current_round_id,
                        source_conn=self.serverbound_source,
                        self_session_id=self.self_session_id,
                    )

                    self.play_pending = not started

        elif self.record_mode:
            self.recorder.on_alive(
                observed_ns,
                source=(
                    "respawn-alive-pulse"
                    if respawn_pulse
                    else source_name
                ),
            )

    def _on_server_dead(
        self,
        source_name,
    ):
        was_alive = self.self_alive
        self.self_alive = False
        self.last_self_alive_signal_ns = None

        print(
            f"[LIFE] DEAD -> RESET t=0 "
            f"map={self.current_map} "
            f"source={source_name} "
            f"wasAlive={was_alive}"
        )

        # Always clear Recorder if it owns a life.
        if self.recorder.armed:
            self.recorder.on_death(
                source_name
            )

        if self.play_mode:
            if not self.auto_record_fallback:
                self.replayer.stop(
                    "server-death"
                )

                self.play_pending = (
                    self.selected_route is not None
                )

    async def _chat(self, message, source=None):
        """
        Show proxy command/status messages inside the game client.
        """
        conn = source or self.serverbound_source

        if conn is None:
            print(f"[CHAT FALLBACK] {message}")
            return

        try:
            await conn.write_packet(
                clientbound.GeneralMessagePacket,
                message=f"<J>[V1.9]</J> {message}",
            )
        except Exception as exc:
            print(
                f"[CHAT ERROR] "
                f"{type(exc).__name__}: {exc}"
            )

    @staticmethod
    def _player_name(player):
        name = getattr(player, "username", None)

        if not name:
            name = getattr(player, "name", None)

        if name is None:
            return None

        return str(name)

    @staticmethod
    def _activity_name(player):
        activity = getattr(
            player,
            "activity",
            None,
        )

        return getattr(
            activity,
            "name",
            str(activity),
        ).lower()

    def _feed_remote_lifecycle(
        self,
        player,
        *,
        source_name,
        observed_ns=None,
    ):
        """
        Feed Alive/Dead to BOTH passive recorder systems.

        This is the key V1.8 fix for records/training/racing rooms where
        multiple lives can happen inside the same NewRound.
        """
        if observed_ns is None:
            observed_ns = time.perf_counter_ns()

        session_id = getattr(
            player,
            "session_id",
            None,
        )

        if session_id is None:
            return

        session_id = int(
            session_id
        )

        # Self lifecycle is handled by _on_alive/_on_server_dead below.
        if (
            self.self_session_id is not None
            and session_id
            == int(self.self_session_id)
        ):
            return

        activity = self._activity_name(
            player
        )

        if activity == "alive":
            allow_respawn_pulse = (
                source_name == "player-update"
            )

            self.winner_recorder.on_alive(
                session_id,
                observed_ns=observed_ns,
                source=source_name,
                allow_respawn_pulse=allow_respawn_pulse,
            )

            if (
                self.player_recorder.target_session_id
                is not None
                and session_id
                == int(
                    self.player_recorder.target_session_id
                )
            ):
                self.player_recorder.on_alive(
                    observed_ns,
                    source=source_name,
                    allow_respawn_pulse=allow_respawn_pulse,
                )

        elif activity == "dead":
            self.winner_recorder.on_dead(
                session_id,
                source=source_name,
            )

            if (
                self.player_recorder.target_session_id
                is not None
                and session_id
                == int(
                    self.player_recorder.target_session_id
                )
            ):
                self.player_recorder.on_death(
                    source_name
                )

    def _register_player(self, player):
        session_id = getattr(
            player,
            "session_id",
            None,
        )

        name = self._player_name(player)

        if session_id is None or not name:
            return

        session_id = int(session_id)

        self.players_by_session[
            session_id
        ] = name

        self.winner_recorder.register_player(
            session_id,
            name,
        )

        self.sessions_by_name[
            name.casefold()
        ] = session_id

        target = self.player_recorder.target_name

        if (
            target is not None
            and target.casefold() == name.casefold()
        ):
            self.player_recorder.set_session(
                session_id
            )

    def _resolve_recordplayer_target(self):
        target = self.player_recorder.target_name

        if not target:
            return None

        session_id = self.sessions_by_name.get(
            target.casefold()
        )

        if session_id is not None:
            self.player_recorder.set_session(
                session_id
            )

        return session_id

    async def _finish_player_record(
        self,
        reason,
        *,
        victory_seconds=None,
    ):
        record = self.player_recorder.finish(
            reason,
            victory_seconds=victory_seconds,
        )

        if record is None:
            return None

        record_id = self.store.save_player_record(
            record
        )

        await self._chat(
            f"recordplayer saved #{record_id}: "
            f"{record['targetName']} | "
            f"{len(record['events'])} points | "
            f"{record['reason']}"
        )

        return record_id

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
        # Incomplete/failed passive target life is never replay data.
        if self.player_recorder.enabled:
            self.player_recorder.on_death(
                "round-change"
            )

        self.replayer.stop(
            "new-round"
        )

        self.local_player_replayer.stop(
            "new-round"
        )

        self.play_pending = False
        self.self_alive = False
        self.last_self_alive_signal_ns = None

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

        if self.player_recorder.enabled:
            self.player_recorder.start_map(
                map_code=self.current_map,
                mirrored=self.current_mirrored,
                map_hash=self.current_map_hash,
                round_id=self.current_round_id,
            )

            self._resolve_recordplayer_target()

        # Both persistent modes can learn from other players.
        self.winner_recorder.set_enabled(
            self.record_mode
            or self.play_mode
        )

        self.winner_recorder.new_round(
            map_code=self.current_map,
            mirrored=self.current_mirrored,
            map_hash=self.current_map_hash,
            round_id=self.current_round_id,
        )

        self.first_victory_session_id = None
        self.selected_route = None
        self.auto_record_fallback = False
        self.play_pending = False

        if self.play_mode:
            # V1.6:
            # choose best self/learned-winner route and replay it to
            # the owned backend. If nothing exists, record our normal run.
            self._load_play_for_current_map()

        elif self.record_mode:
            self._arm_record_for_current_map()

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
        observed_ns = time.perf_counter_ns()

        for player in packet.players:
            self._register_player(
                player
            )

            self._feed_remote_lifecycle(
                player,
                source_name="player-list",
                observed_ns=observed_ns,
            )

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
        player = packet.player

        self._register_player(
            player
        )

        observed_ns = time.perf_counter_ns()

        self._feed_remote_lifecycle(
            player,
            source_name="player-update",
            observed_ns=observed_ns,
        )

        if self.self_session_id is None:
            return

        if (
            int(player.session_id)
            != int(self.self_session_id)
        ):
            return

        activity = self._activity_name(
            player
        )

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
            .lstrip("./")
        )

        argument_raw = (
            parts[1].strip()
            if len(parts) > 1
            else None
        )

        argument = (
            argument_raw.lower()
            if argument_raw is not None
            else None
        )

        # --------------------------
        # /help
        # --------------------------

        if command == "help":
            record_status = (
                "ON"
                if self.record_mode
                else "OFF"
            )

            play_status = (
                "ON"
                if self.play_mode
                else "OFF"
            )

            recordplayer_target = (
                self.player_recorder.target_name
            )

            playplayer_target = (
                (
                    self.replayer.record
                    or {}
                ).get(
                    "targetName"
                )
            )

            help_lines = [
                (
                    f"STATUS | RECORD={record_status} "
                    f"| PLAY={play_status}"
                ),

                (
                    "/record on | kalici kayit modunu acar. "
                    "Round degisse de acik kalir."
                ),

                (
                    "/record off | kayit modunu kapatir."
                ),

                (
                    "/play on | kalici autoplay + autolearn acar. "
                    "Kayit varsa en hizli rotayi servera oynatir; "
                    "yoksa normal oynayisini kaydeder."
                ),

                (
                    "/play off | autoplay/autolearn modunu kapatir."
                ),

                (
                    "/recordplayer Nick#0000 | secilen oyuncunun "
                    "serverdan gelen hareketlerini pasif kaydeder."
                ),

                (
                    "/recordplayer off | secili oyuncu kaydini kapatir. "
                    f"Current={recordplayer_target or 'OFF'}"
                ),

                (
                    "/playplayer Nick#0000 | secilen oyuncunun "
                    "kaydedilmis rotasini OWNED server + local client'a oynatir."
                ),

                (
                    "/playplayer off | manuel player replay'i durdurur. "
                    f"Current={playplayer_target or 'OFF'}"
                ),

                (
                    "LIFE TIMER | death=discard+reset; ayrica records/racing "
                    "respawn Alive pulse da yeni life=t0."
                ),

                (
                    "/help | bu listeyi gosterir."
                ),
            ]

            print()
            print("=" * 54)
            print(" TFM V1.9 HELP")
            print("=" * 54)

            for line in help_lines:
                print(line)

            print("=" * 54)
            print()

            for line in help_lines:
                await self._chat(
                    line,
                    source,
                )

            return self.DO_NOTHING

        # --------------------------
        # /recordplayer Nick#0000
        # Passive observation only.
        # --------------------------

        if command == "recordplayer":
            if argument_raw is None:
                target = self.player_recorder.target_name

                if target is None:
                    await self._chat(
                        "recordplayer: OFF",
                        source,
                    )
                else:
                    session_id = (
                        self.player_recorder.target_session_id
                    )

                    await self._chat(
                        f"recordplayer: {target} | "
                        f"session="
                        f"{session_id if session_id is not None else 'waiting'}",
                        source,
                    )

                return self.DO_NOTHING

            if argument in (
                "off",
                "stop",
            ):
                self.player_recorder.on_death(
                    "manual-stop"
                )

                self.player_recorder.clear_target()

                await self._chat(
                    "recordplayer OFF",
                    source,
                )

                return self.DO_NOTHING

            # New target.
            if self.player_recorder.enabled:
                self.player_recorder.on_death(
                    "target-change"
                )

            self.player_recorder.set_target(
                argument_raw
            )

            if self._map_context_ready():
                self.player_recorder.start_map(
                    map_code=self.current_map,
                    mirrored=self.current_mirrored,
                    map_hash=self.current_map_hash,
                    round_id=self.current_round_id,
                )

            session_id = self._resolve_recordplayer_target()

            if session_id is None:
                await self._chat(
                    f"recordplayer ON: {argument_raw} | "
                    "player not seen yet, waiting for player list",
                    source,
                )
            else:
                await self._chat(
                    f"recordplayer ON: {argument_raw} | "
                    f"session={session_id}",
                    source,
                )

            return self.DO_NOTHING

        # --------------------------
        # /playplayer Nick#0000
        # Plays passive target route on OUR CLIENT ONLY.
        # --------------------------

        if command == "playplayer":
            if argument_raw is None:
                current = self.replayer.record or {}

                await self._chat(
                    (
                        f"playplayer: "
                        f"{current.get('targetName', 'OFF')}"
                    ),
                    source,
                )

                return self.DO_NOTHING

            if argument in (
                "off",
                "stop",
            ):
                self.replayer.stop(
                    "playplayer-off"
                )

                await self._chat(
                    "playplayer OFF",
                    source,
                )

                return self.DO_NOTHING

            if not self._map_context_ready():
                await self._chat(
                    "playplayer failed: map context not ready",
                    source,
                )

                return self.DO_NOTHING

            record = self.store.get_best_player_record(
                target_name=argument_raw,
                map_code=self.current_map,
                mirrored=self.current_mirrored,
                map_hash=self.current_map_hash,
            )

            if record is None:
                await self._chat(
                    f"playplayer: no saved route for "
                    f"{argument_raw} on this map",
                    source,
                )

                return self.DO_NOTHING

            self.replayer.stop(
                "new-playplayer"
            )

            self.replayer.arm(
                record
            )

            self.selected_route = record
            self.auto_record_fallback = False
            self.play_pending = True

            started = self.replayer.start(
                round_id=self.current_round_id,
                source_conn=source,
                self_session_id=self.self_session_id,
            )

            self.play_pending = not started

            if started:
                await self._chat(
                    f"playplayer ON: {argument_raw} | "
                    f"{len(record.get('events', []))} points | "
                    "OWNED SERVER + LOCAL",
                    source,
                )
            else:
                await self._chat(
                    f"playplayer armed: {argument_raw} | "
                    "waiting for movement/Alive",
                    source,
                )

            return self.DO_NOTHING

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
                self.record_mode = True

                # RECORD and PLAY are independent persistent switches.
                # If PLAY is currently handling this map, it owns Recorder.
                if not self.play_mode:
                    self._arm_record_for_current_map()

                self.winner_recorder.set_enabled(
                    True
                )

                print(
                    "[MODE] RECORD=ON "
                    f"PLAY={'ON' if self.play_mode else 'OFF'} "
                    "persistent=True"
                )

                await self._chat(
                    "RECORD ON | persistent across rounds",
                    source,
                )

                return self.DO_NOTHING

            if argument in (
                "off",
                "stop",
            ):
                self.record_mode = False

                # Do not kill an autolearn attempt owned by PLAY.
                if not (
                    self.play_mode
                    and self.auto_record_fallback
                ):
                    self.recorder.disarm(
                        "record-off"
                    )

                self.winner_recorder.set_enabled(
                    self.play_mode
                )

                print(
                    "[MODE] RECORD=OFF"
                )

                await self._chat(
                    "RECORD OFF",
                    source,
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
                self.play_mode = True
                self.play_pending = False
                self.auto_record_fallback = False
                self.selected_route = None

                self.winner_recorder.set_enabled(
                    True
                )

                # Apply immediately to current map too.
                route = self._load_play_for_current_map()

                print(
                    "[MODE] PLAY=ON "
                    "mode=SERVER-AUTOLEARN "
                    f"RECORD={'ON' if self.record_mode else 'OFF'} "
                    "persistent=True"
                )

                if route is None:
                    await self._chat(
                        "PLAY ON | no route: normal play + autolearn",
                        source,
                    )
                else:
                    await self._chat(
                        f"PLAY ON | route #{route.get('id')} "
                        f"source={route.get('targetName', 'SELF')} | "
                        "server replay armed",
                        source,
                    )

                return self.DO_NOTHING

            if argument in (
                "off",
                "stop",
            ):
                self.play_mode = False
                self.play_pending = False
                self.auto_record_fallback = False
                self.selected_route = None

                self.replayer.stop(
                    "play-off"
                )

                self.local_player_replayer.stop(
                    "play-off"
                )

                # If RECORD remains ON, it becomes the owner of Recorder.
                if self.record_mode:
                    self._arm_record_for_current_map()

                self.winner_recorder.set_enabled(
                    self.record_mode
                )

                print(
                    "[MODE] PLAY=OFF"
                )

                await self._chat(
                    "PLAY OFF",
                    source,
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

        observed_ns = time.perf_counter_ns()

        # Movement itself proves the player is alive.
        # This fallback is important in training/racing rooms where an
        # Alive activity update may arrive late or not at all.
        if not self.self_alive:
            self.self_alive = True

            print(
                "[LIFE] SELF movement-fallback "
                "ALIVE -> t=0"
            )

            if (
                self.play_mode
                and self.auto_record_fallback
            ):
                self.recorder.on_alive(
                    observed_ns,
                    source="movement-fallback",
                )

            elif (
                self.record_mode
                and not self.play_mode
            ):
                self.recorder.on_alive(
                    observed_ns,
                    source="movement-fallback",
                )

            if (
                self.play_mode
                and not self.auto_record_fallback
                and self.selected_route is not None
            ):
                self.play_pending = True

        # PLAY route exists:
        # start saved trajectory and block physical movement while active.
        if (
            self.play_mode
            and not self.auto_record_fallback
            and self.selected_route is not None
        ):
            if (
                self.play_pending
                and not self.replayer.is_active()
            ):
                started = self.replayer.start(
                    round_id=self.current_round_id,
                    source_conn=source,
                    self_session_id=self.self_session_id,
                )

                self.play_pending = (
                    not started
                )

            if self.replayer.is_active():
                return self.DO_NOTHING

        # PLAY has no route -> normal play + automatic recording.
        if (
            self.play_mode
            and self.auto_record_fallback
        ):
            self.recorder.ensure_alive_from_movement(
                observed_ns
            )

            self.recorder.record_movement(
                packet,
                observed_ns=observed_ns,
            )

            return

        # Plain persistent RECORD.
        if self.record_mode:
            self.recorder.ensure_alive_from_movement(
                observed_ns
            )

            self.recorder.record_movement(
                packet,
                observed_ns=observed_ns,
            )

            return

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

        print(
            "[DEATH] CLIENT -> SERVER | "
            "RESET SELF LIFE TIMER"
        )

        self.self_alive = False
        self.last_self_alive_signal_ns = None

        # Any real client death ends the current attempt.
        if self.recorder.armed:
            self.recorder.on_death(
                "client-death"
            )

        if self.replayer.is_active():
            self.replayer.stop(
                "client-death"
            )

            self.play_pending = (
                self.selected_route is not None
            )

        # Forward the real death packet normally to owned backend.
        return

    # ==============================================================
    # SERVER -> CLIENT REMOTE PLAYER MOVEMENT
    # Passive /recordplayer observer
    # ==============================================================

    @pak.packet_listener(
        clientbound.PlayerMovementPacket
    )
    async def on_remote_player_movement(
        self,
        source,
        packet,
    ):
        # Passive winner-learning while RECORD or PLAY is enabled.
        self.winner_recorder.observe(
            packet,
            self_session_id=self.self_session_id,
        )

        if not self.player_recorder.enabled:
            return

        session_id = int(
            packet.session_id
        )

        # We only record the selected remote target.
        if (
            self.player_recorder.target_session_id is None
            or session_id
            != int(self.player_recorder.target_session_id)
        ):
            return

        # Never reinterpret our own movement as a remote target.
        if (
            self.self_session_id is not None
            and session_id == int(self.self_session_id)
        ):
            return

        self.player_recorder.observe(
            packet
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
        # Learn only the FIRST finisher of the round.
        #
        # We deliberately record remote movement passively from server
        # broadcasts, then make that route eligible for future autoplay.
        is_first_victory = (
            self.first_victory_session_id is None
        )

        if is_first_victory:
            self.first_victory_session_id = int(
                packet.session_id
            )

            winner_record = self.winner_recorder.winner_record(
                packet.session_id,
                float(packet.seconds),
            )

            if winner_record is not None:
                self.store.save_player_record(
                    winner_record
                )

                await self._chat(
                    f"FIRST PLACE learned: "
                    f"{winner_record['targetName']} | "
                    f"{float(packet.seconds):.3f}s | "
                    f"{len(winner_record['events'])} points"
                )

            elif (
                self.self_session_id is not None
                and int(packet.session_id)
                == int(self.self_session_id)
            ):
                print(
                    "[AUTOLEARN] we were first; "
                    "no remote winner route needed"
                )

        # Passive target victory.
        if (
            self.player_recorder.target_session_id is not None
            and int(packet.session_id)
            == int(self.player_recorder.target_session_id)
        ):
            await self._finish_player_record(
                "victory",
                victory_seconds=float(packet.seconds),
            )

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

        # Recorder has one owner per round:
        #   PLAY fallback when no route exists
        #   otherwise plain RECORD mode
        if (
            self.play_mode
            and self.auto_record_fallback
        ):
            record = self.recorder.finish(
                finish_seconds=finish_seconds
            )

            if record is not None:
                self.store.save(
                    record
                )

                await self._chat(
                    f"autolearn saved OUR run | "
                    f"{record['finishMs'] / 1000.0:.3f}s life | "
                    f"{len(record['events'])} points"
                )

        elif self.record_mode:
            record = self.recorder.finish(
                finish_seconds=finish_seconds
            )

            if record is not None:
                self.store.save(
                    record
                )

                await self._chat(
                    f"record saved | "
                    f"{record['finishMs'] / 1000.0:.3f}s life | "
                    f"{len(record['events'])} points"
                )

        if self.play_mode:
            self.replayer.stop(
                "server-victory"
            )

            self.play_pending = False

        self.self_alive = False
        self.last_self_alive_signal_ns = None
