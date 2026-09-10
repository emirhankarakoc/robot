import asyncio
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
    TFM emirhankarakoc v1.2

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

        # First eligible finisher of a round is the route we learn.
        self.first_victory_session_id = None

        # AFK farming:
        # no route -> two synthetic jump pulses
        # first eligible learned winner -> immediately arm/play that route
        self.afk_farming = False
        self.afk_jump_pending = False
        self.afk_jump_task = None
        self.afk_life_start_ns = None

        self.self_victory_capture_pending = False
        self.self_victory_capture_task = None
        self.self_victory_capture_ns = None

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
            "[PROXY] emirhankarakoc v1.2 listeners ready"
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

    @staticmethod
    def _route_owner(route):
        if not route:
            return "UNKNOWN"

        return str(
            route.get("ownerName")
            or route.get("targetName")
            or "SELF"
        )

    @staticmethod
    def _route_seconds(route):
        if not route:
            return None

        if route.get("victorySeconds") is not None:
            return float(route["victorySeconds"])

        if route.get("finishMs") is not None:
            return float(route["finishMs"]) / 1000.0

        return None

    def _cancel_afk_jump_task(self, reason):
        task = self.afk_jump_task
        self.afk_jump_task = None
        self.afk_jump_pending = False

        if task is not None and not task.done():
            task.cancel()
            print(f"[AFKFARMING] jump task cancelled reason={reason}")

    @staticmethod
    def _event_from_self_packet(packet):
        rotation = packet.rotation_info

        return {
            "tUs": 0,
            "x": float(packet.x),
            "y": float(packet.y),
            "velocityX": float(packet.velocity_x),
            "velocityY": float(packet.velocity_y),
            "movingLeft": bool(packet.moving_left),
            "movingRight": bool(packet.moving_right),
            "facingRight": (
                True
                if bool(packet.moving_right)
                else False
                if bool(packet.moving_left)
                else True
            ),
            "jumping": bool(packet.jumping),
            "jumpingFrameIndex": int(packet.jumping_frame_index),
            "frictionCharge": float(packet.friction_info.charge),
            "frictionLossRate": float(packet.friction_info.loss_rate),
            "enteredPortal": int(
                getattr(
                    packet.entered_portal,
                    "value",
                    packet.entered_portal,
                )
            ),
            "rotationInfo": (
                None
                if rotation is None
                else {
                    "rotation": float(rotation.rotation),
                    "angularVelocity": float(rotation.angular_velocity),
                    "fixedRotation": bool(rotation.fixed_rotation),
                }
            ),
        }

    async def _afk_jump_sequence(
        self,
        *,
        source_conn,
        base_event,
        round_id,
        life_start_ns,
    ):
        try:
            # First jump at life+5.00s, second at life+5.75s.
            for jump_number, target_offset_seconds in (
                (1, 5.00),
                (2, 5.75),
            ):
                elapsed = max(
                    0.0,
                    (
                        time.perf_counter_ns()
                        - int(life_start_ns)
                    )
                    / 1_000_000_000.0,
                )

                delay = max(
                    0.0,
                    target_offset_seconds - elapsed,
                )

                if delay > 0:
                    await asyncio.sleep(delay)

                if (
                    not self.afk_farming
                    or not self.afk_jump_pending
                    or self.current_round_id != round_id
                    or self.selected_route is not None
                ):
                    return

                jump_event = dict(base_event)
                jump_event["jumping"] = True
                jump_event["velocityY"] = -50.0
                jump_event["jumpingFrameIndex"] = int(
                    jump_event.get("jumpingFrameIndex", 0)
                ) + jump_number

                packet = self.replayer._build_packet(
                    jump_event,
                    round_id,
                )

                actual_elapsed = (
                    time.perf_counter_ns()
                    - int(life_start_ns)
                ) / 1_000_000_000.0

                print(
                    "[TX->SERVER] "
                    f"packet={type(packet).__name__} "
                    f"reason=afkfarming-jump-{jump_number} "
                    f"lifeT={actual_elapsed:.3f}s "
                    f"map=@{self.current_map} "
                    f"round={round_id} "
                    f"x={jump_event['x']:.2f} "
                    f"y={jump_event['y']:.2f} "
                    f"vy={jump_event['velocityY']:.2f} "
                    "jump=True"
                )

                await (
                    source_conn.destination
                    .write_packet_instance(packet)
                )

                await asyncio.sleep(0.10)

                if (
                    not self.afk_farming
                    or self.current_round_id != round_id
                    or self.selected_route is not None
                ):
                    return

                release_event = dict(base_event)
                release_event["jumping"] = False
                release_event["velocityY"] = 0.0

                release_packet = self.replayer._build_packet(
                    release_event,
                    round_id,
                )

                print(
                    "[TX->SERVER] "
                    f"packet={type(release_packet).__name__} "
                    f"reason=afkfarming-jump-{jump_number}-release "
                    f"map=@{self.current_map} "
                    f"round={round_id} "
                    "jump=False"
                )

                await (
                    source_conn.destination
                    .write_packet_instance(release_packet)
                )

        except asyncio.CancelledError:
            return

        except Exception as exc:
            print(
                f"[AFKFARMING ERROR] "
                f"{type(exc).__name__}: {exc}"
            )

        finally:
            self.afk_jump_task = None

    def _maybe_start_afk_jumps(self, source_conn, packet):
        if (
            not self.afk_farming
            or not self.afk_jump_pending
            or self.selected_route is not None
            or self.afk_jump_task is not None
            or source_conn is None
            or source_conn.destination is None
            or self.current_round_id is None
            or self.afk_life_start_ns is None
        ):
            return

        base_event = self._event_from_self_packet(packet)
        round_id = int(self.current_round_id)
        life_start_ns = int(self.afk_life_start_ns)

        self.afk_jump_task = asyncio.create_task(
            self._afk_jump_sequence(
                source_conn=source_conn,
                base_event=base_event,
                round_id=round_id,
                life_start_ns=life_start_ns,
            )
        )

        elapsed = (
            time.perf_counter_ns()
            - life_start_ns
        ) / 1_000_000_000.0

        print(
            f"[AFKFARMING] no route map=@{self.current_map} "
            f"lifeT={elapsed:.3f}s "
            "-> jumps scheduled for 5.00s / 5.75s"
        )

    async def _activate_learned_route_now(self, route):
        if not route or not self._map_context_ready():
            return False

        owner = self._route_owner(route)
        seconds = self._route_seconds(route)

        self._cancel_afk_jump_task(
            "winner-route-ready"
        )

        if self.recorder.armed:
            self.recorder.on_death(
                "afkfarming-winner-route"
            )

        self.play_mode = True
        self.auto_record_fallback = False
        self.selected_route = route
        self.play_pending = True

        self.replayer.stop(
            "afkfarming-new-winner"
        )
        self.replayer.arm(
            route
        )

        started = False

        if (
            self.self_alive
            and self.serverbound_source is not None
        ):
            started = self.replayer.start(
                round_id=self.current_round_id,
                source_conn=self.serverbound_source,
                self_session_id=self.self_session_id,
            )
            self.play_pending = not started

        await self._chat(
            f"AFKFARMING | winner route ready "
            f"@{self.current_map} | {owner} | "
            f"{seconds:.3f}s | "
            f"{'PLAYING NOW' if started else 'ARMED'}"
        )

        print(
            f"[AFKFARMING] route activated "
            f"map=@{self.current_map} "
            f"owner={owner} "
            f"time={seconds:.3f}s "
            f"started={started}"
        )

        return started

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

        route_seconds = self._route_seconds(route)

        print(
            f"[AUTOLEARN] ROUTE READY "
            f"map=@{self.current_map} "
            f"id={route.get('id')} "
            f"owner={source_name} "
            f"time={route_seconds:.3f}s "
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
        self.afk_life_start_ns = int(observed_ns)

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
        self.afk_life_start_ns = None

        print(
            f"[LIFE] DEAD -> RESET t=0 "
            f"map={self.current_map} "
            f"source={source_name} "
            f"wasAlive={was_alive}"
        )

        if (
            self.recorder.armed
            and not self.self_victory_capture_pending
        ):
            self.recorder.on_death(
                source_name
            )
        elif self.self_victory_capture_pending:
            print(
                "[LIFE] DEAD during victory capture "
                "-> keeping recorder until final snapshot"
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
                message=f"<J>[emirhankarakoc v1.2]</J> {message}",
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

        if record_id is not None:
            await self._chat(
                f"recordplayer BEST saved #{record_id} | "
                f"@{record['mapCode']} | "
                f"{record['targetName']} | "
                f"{record['victorySeconds']:.3f}s | "
                f"{len(record['events'])} points"
            )
        else:
            print(
                f"[RECORDPLAYER] not stored "
                f"map=@{record['mapCode']} "
                f"owner={record['targetName']} "
                f"time={record['victorySeconds']:.3f}s"
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

        self._cancel_afk_jump_task(
            "new-round"
        )

        self.play_pending = False
        self.self_alive = False
        self.last_self_alive_signal_ns = None
        self.afk_life_start_ns = None

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
            or self.afk_farming
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

        # ALWAYS show the currently usable record at hand start.
        round_best = self.store.get_best_any_route(
            map_code=self.current_map,
            mirrored=self.current_mirrored,
            map_hash=self.current_map_hash,
        )

        if round_best is not None:
            owner = self._route_owner(round_best)
            seconds = self._route_seconds(round_best)

            await self._chat(
                f"ROUND @{self.current_map} | "
                f"BEST {seconds:.3f}s | "
                f"owner={owner} | "
                f"{len(round_best.get('events', []))} points"
            )

            print(
                f"[ROUND BEST] map=@{self.current_map} "
                f"owner={owner} "
                f"time={seconds:.3f}s "
                f"points={len(round_best.get('events', []))}"
            )
        else:
            print(
                f"[ROUND BEST] map=@{self.current_map} NONE"
            )

            await self._chat(
                f"ROUND @{self.current_map} | NO SAVED RUN"
            )

        # AFK farming owns autoplay when a route already exists.
        if self.afk_farming and round_best is not None:
            self.play_mode = True

        self.afk_jump_pending = (
            self.afk_farming
            and round_best is None
        )

        if self.play_mode:
            self._load_play_for_current_map()

        elif self.record_mode:
            self._arm_record_for_current_map()

        if self.afk_farming and round_best is None:
            await self._chat(
                f"AFKFARMING @{self.current_map} | "
                "no run: 2 jumps armed; first eligible winner -> instant replay"
            )

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
        # /afkfarming on|off
        # --------------------------

        if command == "afkfarming":
            if argument is None:
                await self._chat(
                    f"AFKFARMING={'ON' if self.afk_farming else 'OFF'}",
                    source,
                )
                return self.DO_NOTHING

            if argument in ("on", "start"):
                self.afk_farming = True
                self.winner_recorder.set_enabled(True)

                route = None
                if self._map_context_ready():
                    route = self.store.get_best_any_route(
                        map_code=self.current_map,
                        mirrored=self.current_mirrored,
                        map_hash=self.current_map_hash,
                    )

                if route is not None:
                    self.play_mode = True
                    self.selected_route = None
                    self._load_play_for_current_map()

                    await self._chat(
                        f"AFKFARMING ON | existing run found "
                        f"@{self.current_map}; autoplay enabled",
                        source,
                    )
                else:
                    self.afk_jump_pending = self._map_context_ready()

                    await self._chat(
                        "AFKFARMING ON | no run => 2 jumps, "
                        "then first eligible winner is replayed immediately",
                        source,
                    )

                print("[MODE] AFKFARMING=ON")
                return self.DO_NOTHING

            if argument in ("off", "stop"):
                self.afk_farming = False
                self._cancel_afk_jump_task("afkfarming-off")

                self.winner_recorder.set_enabled(
                    self.record_mode or self.play_mode
                )

                await self._chat(
                    "AFKFARMING OFF",
                    source,
                )
                print("[MODE] AFKFARMING=OFF")
                return self.DO_NOTHING

            await self._chat(
                "usage: /afkfarming on | /afkfarming off",
                source,
            )
            return self.DO_NOTHING

        # --------------------------
        # /blacklist ...
        # --------------------------

        if command == "blacklist":
            if argument_raw is None or argument == "list":
                names = self.store.blacklist_list()

                if not names:
                    await self._chat(
                        "BLACKLIST | empty",
                        source,
                    )
                else:
                    await self._chat(
                        "BLACKLIST | " + ", ".join(names[:20]),
                        source,
                    )

                return self.DO_NOTHING

            tokens = argument_raw.split(
                maxsplit=1
            )

            action = tokens[0].lower()

            if action == "clear":
                count = self.store.blacklist_clear()
                await self._chat(
                    f"BLACKLIST CLEARED | {count}",
                    source,
                )
                return self.DO_NOTHING

            if action in ("remove", "del", "delete"):
                if len(tokens) < 2:
                    await self._chat(
                        "usage: /blacklist remove Nick#0000",
                        source,
                    )
                    return self.DO_NOTHING

                name = tokens[1].strip()
                count = self.store.blacklist_remove(name)

                await self._chat(
                    f"BLACKLIST REMOVE | {name} | rows={count}",
                    source,
                )
                return self.DO_NOTHING

            if action == "add":
                if len(tokens) < 2:
                    await self._chat(
                        "usage: /blacklist add Nick#0000",
                        source,
                    )
                    return self.DO_NOTHING
                name = tokens[1].strip()
            else:
                # Convenience:
                # /blacklist Nick#0000
                name = argument_raw.strip()

            deleted = self.store.blacklist_add(name)

            current_owner = self._route_owner(
                self.selected_route
            ) if self.selected_route else None

            if (
                current_owner is not None
                and current_owner.casefold()
                == name.casefold()
            ):
                self.replayer.stop(
                    "blacklisted-current-owner"
                )
                self.selected_route = None
                self.play_pending = False

                if self.play_mode and self._map_context_ready():
                    self._load_play_for_current_map()

            await self._chat(
                f"BLACKLIST ADD | {name} | "
                f"removedRecords={deleted}",
                source,
            )
            return self.DO_NOTHING

        # --------------------------
        # /timeowner @mapCode Nick#0000
        # --------------------------

        if command == "timeowner":
            if argument_raw is None:
                await self._chat(
                    "usage: /timeowner @7680000 Nick#0000",
                    source,
                )
                return self.DO_NOTHING

            tokens = argument_raw.split(
                maxsplit=1
            )

            if len(tokens) != 2:
                await self._chat(
                    "usage: /timeowner @7680000 Nick#0000",
                    source,
                )
                return self.DO_NOTHING

            map_text = tokens[0].strip()
            new_owner = tokens[1].strip()

            if map_text.startswith("@"):
                map_text = map_text[1:]

            try:
                map_code = int(map_text)
            except ValueError:
                await self._chat(
                    "usage: /timeowner @7680000 Nick#0000",
                    source,
                )
                return self.DO_NOTHING

            result = self.store.set_best_owner(
                map_code=map_code,
                new_owner=new_owner,
            )

            if result["ok"]:
                await self._chat(
                    f"OWNER @{map_code} | "
                    f"{result['oldOwner']} -> {result['newOwner']} | "
                    f"{result['seconds']:.3f}s",
                    source,
                )
            else:
                await self._chat(
                    f"OWNER @{map_code} failed | {result['reason']}",
                    source,
                )

            return self.DO_NOTHING

        # --------------------------
        # /timelist [@mapCode]
        # BEST only
        # --------------------------

        if command == "timelist":
            requested_map = None

            if argument_raw is not None:
                value = argument_raw.strip()

                if value.startswith("@"):
                    value = value[1:]

                try:
                    requested_map = int(value)
                except ValueError:
                    await self._chat(
                        "usage: /timelist @7680000",
                        source,
                    )
                    return self.DO_NOTHING

            map_code = (
                requested_map
                if requested_map is not None
                else self.current_map
            )

            if map_code is None:
                await self._chat(
                    "timelist failed: no current map; use /timelist @mapCode",
                    source,
                )
                return self.DO_NOTHING

            item = self.store.get_time_best(
                map_code=map_code,
            )

            if item is None:
                await self._chat(
                    f"BEST @{map_code} | no saved record",
                    source,
                )

                print(
                    f"[TIMELIST] @{map_code} EMPTY"
                )

                return self.DO_NOTHING

            db_ref = (
                f"R#{item['id']}"
                if item["source"] == "SELF"
                else f"P#{item['id']}"
            )

            line = (
                f"BEST @{map_code} | "
                f"{item['seconds']:.3f}s | "
                f"{item['name']} | "
                f"{db_ref} | "
                f"{item['points']} pts"
            )

            print(
                f"[TIMELIST] {line}"
            )

            await self._chat(
                line,
                source,
            )

            return self.DO_NOTHING

        # --------------------------
        # /timedelete
        # /timedelete @mapCode
        # /timedelete all
        # --------------------------

        if command == "timedelete":
            if (
                argument_raw is not None
                and argument_raw.strip().lower()
                == "all"
            ):
                deleted = self.store.delete_all()

                # Do not keep a deleted route armed in memory.
                self.replayer.stop(
                    "timedelete-all"
                )
                self.selected_route = None
                self.play_pending = False

                await self._chat(
                    f"TIMEDELETE ALL | deleted {deleted} row(s)",
                    source,
                )

                return self.DO_NOTHING

            requested_map = None

            if argument_raw is not None:
                value = argument_raw.strip()

                if value.startswith("@"):
                    value = value[1:]

                try:
                    requested_map = int(value)
                except ValueError:
                    await self._chat(
                        "usage: /timedelete | /timedelete @7680000 | /timedelete all",
                        source,
                    )

                    return self.DO_NOTHING

            map_code = (
                requested_map
                if requested_map is not None
                else self.current_map
            )

            if map_code is None:
                await self._chat(
                    "timedelete failed: no current map",
                    source,
                )
                return self.DO_NOTHING

            deleted = self.store.delete_map(
                map_code=map_code,
            )

            if (
                self.current_map is not None
                and int(map_code)
                == int(self.current_map)
            ):
                self.replayer.stop(
                    "timedelete-current-map"
                )
                self.selected_route = None
                self.play_pending = False

                # If persistent PLAY is ON, this map now has no record,
                # so switch back to normal/manual autolearn.
                if self.play_mode and self._map_context_ready():
                    self._load_play_for_current_map()

            await self._chat(
                f"TIMEDELETE @{map_code} | deleted {deleted} row(s)",
                source,
            )

            return self.DO_NOTHING

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

            afk_status = (
                "ON"
                if self.afk_farming
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
                    f"| PLAY={play_status} "
                    f"| AFKFARMING={afk_status}"
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
                    "/afkfarming on/off | run yoksa 2 jump; "
                    "ilk uygun winner gelince aninda replay."
                ),

                (
                    "/blacklist add/remove/list/clear Nick | "
                    "blacklisted owner recordlari kullanilmaz."
                ),

                (
                    "/timeowner @map Nick | BEST kaydin owner bilgisini degistirir."
                ),

                (
                    "/timelist [@map] | sadece mevcut BEST kaydi gosterir."
                ),

                (
                    "/timedelete [@map] | map kaydini siler. "
                    "/timedelete all | tum kayitlari siler."
                ),

                (
                    "/help | bu listeyi gosterir."
                ),
            ]

            print()
            print("=" * 54)
            print(" TFM emirhankarakoc v1.2 HELP")
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
                    or self.afk_farming
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

        if self.self_victory_capture_pending:
            if self.recorder.armed and self.recorder.active:
                self.recorder.record_movement(
                    packet,
                    observed_ns=observed_ns,
                )

                print(
                    "[REC POST-VICTORY MOVEMENT] "
                    f"x={float(packet.x):.2f} "
                    f"y={float(packet.y):.2f} "
                    f"vx={float(packet.velocity_x):.2f} "
                    f"vy={float(packet.velocity_y):.2f} "
                    f"L={bool(packet.moving_left)} "
                    f"R={bool(packet.moving_right)} "
                    f"jump={bool(packet.jumping)}"
                )

            return

        self._maybe_start_afk_jumps(
            source,
            packet,
        )

        # Movement itself proves the player is alive.
        # This fallback is important in training/racing rooms where an
        # Alive activity update may arrive late or not at all.
        if not self.self_alive:
            self.self_alive = True

            if self.afk_life_start_ns is None:
                self.afk_life_start_ns = int(observed_ns)

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

        if (
            self.recorder.armed
            and not self.self_victory_capture_pending
        ):
            self.recorder.on_death(
                "client-death"
            )
        elif self.self_victory_capture_pending:
            print(
                "[DEATH] death during victory capture "
                "-> keeping successful recorder state"
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

    async def _finalize_self_victory_after_capture(
        self,
        *,
        victory_ns,
        finish_seconds,
        victory_round_id,
        save_mode,
    ):
        try:
            await asyncio.sleep(0.150)

            context = self.recorder.context

            if (
                context is None
                or int(context.get("roundId", -1))
                != int(victory_round_id)
            ):
                print(
                    "[RECORD FINALIZE SKIP] "
                    f"victoryRound={victory_round_id} "
                    "recorder context changed"
                )
                return

            record = self.recorder.finish(
                finish_seconds=finish_seconds,
                observed_ns=victory_ns,
            )

            if record is None:
                print("[RECORD FINALIZE] no active record")
                return

            record["ownerName"] = self.self_name or "SELF"
            record["targetName"] = record["ownerName"]

            record_id = self.store.save(record)

            print(
                f"[RECORD FINALIZED] "
                f"mode={save_mode} "
                f"map=@{record['mapCode']} "
                f"owner={record['ownerName']} "
                f"time={record['finishMs'] / 1000.0:.3f}s "
                f"points={len(record['events'])} "
                f"stored={'yes' if record_id is not None else 'no'}"
            )

            await self._chat(
                f"{save_mode} saved | "
                f"@{record['mapCode']} | "
                f"{record['finishMs'] / 1000.0:.3f}s life | "
                f"{len(record['events'])} points"
            )

        except asyncio.CancelledError:
            return

        except Exception as exc:
            print(
                "[RECORD FINALIZE ERROR] "
                f"{type(exc).__name__}: {exc}"
            )

        finally:
            self.self_victory_capture_pending = False
            self.self_victory_capture_task = None
            self.self_victory_capture_ns = None

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
        # Learn the first ELIGIBLE finisher.
        # Blacklisted owners are ignored so they cannot poison training data.
        if self.first_victory_session_id is None:
            winner_session_id = int(
                packet.session_id
            )

            winner_name = self.players_by_session.get(
                winner_session_id,
                (
                    self.self_name
                    if (
                        self.self_session_id is not None
                        and winner_session_id == int(self.self_session_id)
                    )
                    else f"session-{winner_session_id}"
                ),
            )

            if self.store.is_blacklisted(
                winner_name
            ):
                print(
                    f"[FIRST PLACE SKIP] "
                    f"map=@{self.current_map} "
                    f"owner={winner_name} "
                    "reason=blacklisted"
                )

                await self._chat(
                    f"FIRST PLACE skipped | "
                    f"@{self.current_map} | "
                    f"{winner_name} | BLACKLISTED"
                )

            else:
                winner_record = self.winner_recorder.winner_record(
                    packet.session_id,
                    float(packet.seconds),
                )

                # Self winner is stored by the normal self recorder below.
                if (
                    winner_record is None
                    and self.self_session_id is not None
                    and winner_session_id == int(self.self_session_id)
                ):
                    self.first_victory_session_id = winner_session_id

                    print(
                        f"[FIRST PLACE] "
                        f"map=@{self.current_map} "
                        f"owner={self.self_name} "
                        f"serverTime={float(packet.seconds):.3f}s "
                        "source=self"
                    )

                elif winner_record is not None:
                    learned_seconds = float(
                        winner_record["victorySeconds"]
                    )

                    if learned_seconds < self.store.MIN_RECORD_SECONDS:
                        print(
                            f"[FIRST PLACE SKIP] "
                            f"map=@{self.current_map} "
                            f"owner={winner_record['targetName']} "
                            f"time={learned_seconds:.3f}s "
                            f"reason=under-{self.store.MIN_RECORD_SECONDS:.0f}s"
                        )

                        await self._chat(
                            f"FIRST PLACE skipped | "
                            f"@{self.current_map} | "
                            f"{winner_record['targetName']} | "
                            f"{learned_seconds:.3f}s < "
                            f"{self.store.MIN_RECORD_SECONDS:.0f}s"
                        )
                    else:
                        self.first_victory_session_id = winner_session_id

                        record_id = self.store.save_player_record(
                            winner_record
                        )

                        print(
                            f"[FIRST PLACE learned] "
                            f"map=@{self.current_map} | "
                            f"{winner_record['targetName']} | "
                            f"{learned_seconds:.3f}s | "
                            f"{len(winner_record['events'])} points | "
                            f"saved={'yes' if record_id is not None else 'no-faster-record'}"
                        )

                        await self._chat(
                            f"FIRST PLACE learned | "
                            f"@{self.current_map} | "
                            f"{winner_record['targetName']} | "
                            f"{learned_seconds:.3f}s | "
                            f"{len(winner_record['events'])} points"
                        )

                        # If this hand started with no route, AFKFARMING
                        # immediately turns on PLAY and starts the learned
                        # winner trajectory in the SAME hand.
                        if (
                            self.afk_farming
                            and self.afk_jump_pending
                            and record_id is not None
                        ):
                            winner_record["id"] = record_id

                            await self._activate_learned_route_now(
                                winner_record
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

        victory_ns = time.perf_counter_ns()

        capture_mode = None

        if (
            self.play_mode
            and self.auto_record_fallback
            and self.recorder.active
        ):
            capture_mode = "autolearn"

        elif self.record_mode and self.recorder.active:
            capture_mode = "record"

        if capture_mode is not None:
            self.self_victory_capture_pending = True
            self.self_victory_capture_ns = victory_ns

            if (
                self.self_victory_capture_task is not None
                and not self.self_victory_capture_task.done()
            ):
                self.self_victory_capture_task.cancel()

            self.self_victory_capture_task = asyncio.create_task(
                self._finalize_self_victory_after_capture(
                    victory_ns=victory_ns,
                    finish_seconds=finish_seconds,
                    victory_round_id=int(self.current_round_id),
                    save_mode=capture_mode,
                )
            )

            print(
                "[VICTORY CAPTURE] "
                f"map=@{self.current_map} "
                "150ms final-movement window OPEN"
            )

        if self.play_mode:
            self.replayer.stop(
                "server-victory"
            )

            self.play_pending = False

        self.self_alive = False
        self.last_self_alive_signal_ns = None
        self.afk_life_start_ns = None
