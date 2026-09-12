import asyncio
import copy
import hashlib
import time
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation

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
    TFM emirhankarakoc v1.10

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
        NewRound is the replay clock anchor
        saved timestamps preserve the original 3-2-1 timing
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
        self.afk_jumps_done_for_life = False

        # Separate from jump state:
        # True means THIS ROUND started with no route and AFKFARMING is
        # waiting for the first eligible learned winner so it can replay
        # immediately in the same round.
        self.afk_waiting_for_route = False

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
        self.current_map_width = 800.0

        # The records room emits StartRoundCountdownPacket as a real edge:
        # True before NewRound starts the 3-2-1 state, then False releases
        # controls. Playback waits for that falling edge instead of estimating
        # a fixed duration from NewRound/Alive/client heartbeat packets.
        self.round_countdown_active = False
        self.round_countdown_packet_seen = False
        self.current_round_anchor_time = None
        self.awaiting_countdown_end = False

        # A victory is recordable only when it belongs to a NewRound that
        # this proxy actually observed. SELF victories are additionally
        # matched against the mapCode+roundId in EnterHolePacket.
        self.current_round_verified = False
        self.self_enter_hole_context = None

        # The first NewRound received after an explicit JoinRoom is a snapshot
        # of the hand already in progress, not proof that this proxy observed
        # its real start. It is quarantined until the following NewRound.
        self.pending_join_snapshot = False
        self.current_round_join_snapshot = False

        # Client-only ground size overrides applied once per NewRound.
        # Trampoline and lava keep independent on/off and L/H values.
        self.ground_resize_settings = {
            "trambolin": {
                "ground_type": "2",
                "enabled": False,
                "width_px": Decimal("0.5"),
                "height_px": Decimal("0.5"),
                "force_invisible": False,
            },
            "lav": {
                "ground_type": "3",
                "enabled": False,
                "width_px": Decimal("0.5"),
                "height_px": Decimal("0.5"),
                "force_invisible": False,
            },
        }

        self.self_alive = False

        # Records/racing rooms can emit a fresh PlayerUpdate Alive on
        # respawn without a preceding Dead. A short debounce prevents the
        # normal player-list + player-update spawn pair from double-resetting.
        self.last_self_alive_signal_ns = None
        self.self_alive_signal_debounce_ns = 750_000_000  # 0.75 s

        # Captured from a real client->server connection.
        self.serverbound_source = None

        # Normally consumed immediately by NewRound synchronization. It stays
        # True only when the backend connection was not available at NewRound,
        # or when a same-round respawn needs the movement fallback.
        self.play_pending = False

        # /chatafterfirst
        #
        # Only fires when a SAVED replay actually started during this round
        # and our session is the first finisher.
        self.chat_after_first_enabled = False
        self.chat_after_first_message = None
        self.chat_after_first_sent_this_round = False
        self.replay_started_this_round = False

        # Controls only local proxy/status messages injected into game chat.
        # Real room chat such as /chatafterfirst remains independent.
        self.chat_logging = True

        # High-frequency terminal diagnostics are OFF by default.
        # This avoids console I/O in movement hot paths.
        self.debug_logs = False
        self.recorder.set_debug_logs(False)
        self.replayer.set_debug_logs(False)
        self.player_recorder.set_debug_logs(False)
        self.local_player_replayer.set_debug_logs(False)

        print(
            "[PROXY] emirhankarakoc v1.10 listeners ready"
        )
        print(
            "[ROBOT] JOIN-SNAPSHOT-GUARD-V16 | BACKEND-FIRST-V15 | "
            "COUNTDOWN-EDGE-SYNC-V14 | "
            "NEWROUND-CLOCK-SYNC-V13 | "
            "COUNTDOWN-SYNC-V12 | "
            "MAP-START-SYNC-V11 | "
            "MAP-START-FAST-V10 | "
            "CHATLOGGING-ROUND-ID-V9 | "
            "ROUND-MATCH-GUARD-V8 | "
            "RECORD-ID-DELETE-V7 | "
            "MIRROR-INVENTORY-V6 | "
            "MIRROR-FALLBACK-V5 | "
            "GROUND-OVERLAY-V4 | NO LIMIT | "
            "/sismanlattrambolin + /sismanlatlav"
        )

    # ==============================================================
    # HELPERS
    # ==============================================================

    @staticmethod
    def _format_xml_number(value):
        decimal_value = (
            value
            if isinstance(value, Decimal)
            else Decimal(str(value))
        )
        text = format(decimal_value, "f")

        if "." in text:
            text = text.rstrip("0").rstrip(".")

        return "0" if text in ("", "-0") else text

    def _expand_configured_grounds(self, xml):
        """
        Increase L and H for enabled ground types in the client XML.

        The original server XML is retained for map hashing and route lookup.
        Editing the existing ground avoids invalid sub-10px helper grounds and
        also works for dynamic grounds.
        """
        try:
            root = ET.fromstring(xml)
        except (ET.ParseError, TypeError, ValueError):
            return xml, {}

        enabled_by_type = {
            setting["ground_type"]: setting
            for setting in self.ground_resize_settings.values()
            if setting["enabled"]
        }
        changed_counts = {
            ground_type: 0
            for ground_type in enabled_by_type
        }

        # Work from a snapshot of each ground container. Invisible mode adds
        # a new ground, so newly appended overlays must not be processed again.
        for container in root.iter("S"):
            grounds = [
                child
                for child in list(container)
                if child.tag == "S"
            ]

            for ground in grounds:
                ground_type = ground.attrib.get("T")
                setting = enabled_by_type.get(ground_type)

                if setting is None:
                    continue

                try:
                    length = Decimal(ground.attrib["L"])
                    height = Decimal(ground.attrib["H"])
                except (InvalidOperation, KeyError, TypeError, ValueError):
                    continue

                expanded_length = self._format_xml_number(
                    length + setting["width_px"]
                )
                expanded_height = self._format_xml_number(
                    height + setting["height_px"]
                )

                if setting["force_invisible"]:
                    # Preserve the original visible ground. The enlarged
                    # collision layer is a separate invisible overlay.
                    attributes = dict(ground.attrib)
                    attributes.pop("lua", None)
                    attributes.pop("i", None)
                    attributes["L"] = expanded_length
                    attributes["H"] = expanded_height
                    attributes["m"] = ""
                    container.append(
                        ET.Element("S", attributes)
                    )
                else:
                    ground.attrib["L"] = expanded_length
                    ground.attrib["H"] = expanded_height

                changed_counts[ground_type] += 1

        if not any(changed_counts.values()):
            return xml, changed_counts

        return (
            ET.tostring(
                root,
                encoding="unicode",
                short_empty_elements=True,
            ),
            changed_counts,
        )

    async def _configure_ground_resize(
        self,
        setting_key,
        command_name,
        argument_raw,
        source,
    ):
        setting = self.ground_resize_settings[setting_key]

        if argument_raw is None:
            state = "ON" if setting["enabled"] else "OFF"
            await self._chat(
                f"{command_name.upper()}={state} | "
                f"width=+{self._format_xml_number(setting['width_px'])}px | "
                f"height=+{self._format_xml_number(setting['height_px'])}px | "
                f"invisible={'ON' if setting['force_invisible'] else 'OFF'} | "
                "range=>0, no upper limit | applies on next map",
                source,
            )
            return

        tokens = argument_raw.split()
        action = tokens[0].lower()
        visibility_values = {
            "gorunmezacik": True,
            "gorunmezkapali": False,
        }

        if action in visibility_values and len(tokens) == 1:
            setting["force_invisible"] = visibility_values[action]
            visibility_state = (
                "ON"
                if setting["force_invisible"]
                else "OFF"
            )
            await self._chat(
                f"{command_name.upper()} INVISIBLE={visibility_state} | "
                "applies on next map",
                source,
            )
            print(
                f"[{command_name.upper()}] "
                f"INVISIBLE={visibility_state}"
            )
            return

        if action in ("off", "stop") and len(tokens) == 1:
            setting["enabled"] = False
            await self._chat(
                f"{command_name.upper()} OFF | "
                "current map unchanged; next map normal",
                source,
            )
            print(f"[{command_name.upper()}] OFF")
            return

        if action not in ("on", "start"):
            await self._chat(
                f"usage: /{command_name} on [width_px] [height_px] "
                "[gorunmezacik|gorunmezkapali] | off",
                source,
            )
            return

        value_tokens = tokens[1:]
        requested_visibility = None

        if value_tokens and value_tokens[-1].lower() in visibility_values:
            visibility_token = value_tokens.pop().lower()
            requested_visibility = visibility_values[visibility_token]

        if len(value_tokens) > 2:
            await self._chat(
                f"usage: /{command_name} on [width_px] [height_px] "
                "[gorunmezacik|gorunmezkapali] | off",
                source,
            )
            return

        requested_values = []

        for value_text in value_tokens:
            try:
                requested_value = Decimal(value_text)
            except InvalidOperation:
                requested_value = None

            if (
                requested_value is None
                or not requested_value.is_finite()
                or requested_value <= 0
            ):
                await self._chat(
                    f"{command_name.upper()} values must be "
                    "positive finite numbers",
                    source,
                )
                return

            requested_values.append(requested_value.normalize())

        if requested_values:
            setting["width_px"] = requested_values[0]
            setting["height_px"] = requested_values[0]

        if len(requested_values) == 2:
            setting["height_px"] = requested_values[1]

        if requested_visibility is not None:
            setting["force_invisible"] = requested_visibility

        setting["enabled"] = True
        await self._chat(
            f"{command_name.upper()} ON | "
            f"L+{self._format_xml_number(setting['width_px'])}px "
            f"H+{self._format_xml_number(setting['height_px'])}px | "
            f"INVISIBLE={'ON' if setting['force_invisible'] else 'OFF'} | "
            "OVERLAY V4 | NO LIMIT | applies on next map",
            source,
        )
        print(
            f"[{command_name.upper()}] ON "
            f"widthGrowth={self._format_xml_number(setting['width_px'])}px "
            f"heightGrowth={self._format_xml_number(setting['height_px'])}px "
            f"invisible={setting['force_invisible']}"
        )

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

    def _start_selected_route(
        self,
        *,
        trigger,
        anchor_time=None,
        first_control_target_seconds=None,
        start_at_first_control=False,
        source_conn=None,
    ):
        """Start the armed route once and keep fallback state consistent."""
        if not (
            self.play_mode
            and not self.auto_record_fallback
            and self.selected_route is not None
            and not self.replayer.is_active()
        ):
            return False

        connection = (
            source_conn
            if source_conn is not None
            else self.serverbound_source
        )

        started = self.replayer.start(
            round_id=self.current_round_id,
            source_conn=connection,
            self_session_id=self.self_session_id,
            anchor_time=anchor_time,
            trim_initial_delay=(
                first_control_target_seconds is None
            ),
            countdown_target_seconds=first_control_target_seconds,
            start_at_first_control=start_at_first_control,
            trigger=trigger,
        )

        self.play_pending = not started

        if started:
            self.replay_started_this_round = True
            print(
                f"[REPLAY ROUND FLAG] "
                f"map=@{self.current_map} "
                "replayStartedThisRound=True "
                f"source={trigger}"
            )

        return started

    async def _start_selected_route_at_countdown_release(
        self,
        *,
        release_time,
    ):
        """Send the first control directly; do not put a task hop before it."""
        if not (
            self.play_mode
            and not self.auto_record_fallback
            and self.selected_route is not None
            and not self.replayer.is_active()
        ):
            return False

        started = await self.replayer.start_at_countdown_release(
            round_id=self.current_round_id,
            source_conn=self.serverbound_source,
            self_session_id=self.self_session_id,
            release_time=release_time,
        )

        self.play_pending = not started

        if started:
            self.replay_started_this_round = True

        return started

    @staticmethod
    def _map_width_from_xml(xml):
        try:
            root = ET.fromstring(xml)
            map_settings = root.find("./P")

            if map_settings is None:
                return 800.0

            width = Decimal(
                map_settings.attrib.get("L", "800")
            )

            if not width.is_finite() or width <= 0:
                return 800.0

            return float(width)
        except (ET.ParseError, InvalidOperation, TypeError, ValueError):
            return 800.0

    @staticmethod
    def _mirror_route_for_orientation(route, *, map_width, target_mirrored):
        if route is None:
            return None

        source_mirrored = bool(
            route.get("mirrored", False)
        )
        target_mirrored = bool(target_mirrored)

        if source_mirrored == target_mirrored:
            return route

        mirrored_route = copy.deepcopy(route)

        for event in mirrored_route.get("events", []):
            if event.get("x") is not None:
                event["x"] = (
                    float(map_width)
                    - float(event["x"])
                )

            if event.get("velocityX") is not None:
                event["velocityX"] = -float(
                    event["velocityX"]
                )

            moving_left = bool(
                event.get("movingLeft", False)
            )
            moving_right = bool(
                event.get("movingRight", False)
            )
            event["movingLeft"] = moving_right
            event["movingRight"] = moving_left

            if "facingRight" in event:
                event["facingRight"] = not bool(
                    event["facingRight"]
                )

            rotation = event.get("rotationInfo")

            if rotation is not None:
                if rotation.get("rotation") is not None:
                    rotation["rotation"] = -float(
                        rotation["rotation"]
                    )

                if rotation.get("angularVelocity") is not None:
                    rotation["angularVelocity"] = -float(
                        rotation["angularVelocity"]
                    )

        mirrored_route["sourceMirrored"] = source_mirrored
        mirrored_route["mirrored"] = target_mirrored
        mirrored_route["mirroredForPlayback"] = True
        mirrored_route["mirrorWidth"] = float(map_width)

        print(
            "[MIRROR ROUTE] "
            f"sourceMirrored={source_mirrored} "
            f"targetMirrored={target_mirrored} "
            f"width={float(map_width):.3f} "
            f"points={len(mirrored_route.get('events', []))}"
        )

        return mirrored_route

    def _get_best_route_for_current_map(self):
        route = self.store.get_best_any_route(
            map_code=self.current_map,
            mirrored=self.current_mirrored,
            map_hash=self.current_map_hash,
        )

        if route is not None:
            return route

        opposite_route = self.store.get_best_any_route(
            map_code=self.current_map,
            mirrored=not self.current_mirrored,
            map_hash=self.current_map_hash,
        )

        if opposite_route is None:
            return None

        print(
            "[MIRROR FALLBACK] "
            f"map=@{self.current_map} "
            f"savedMirrored={bool(opposite_route.get('mirrored', False))} "
            f"currentMirrored={self.current_mirrored}"
        )

        return self._mirror_route_for_orientation(
            opposite_route,
            map_width=self.current_map_width,
            target_mirrored=self.current_mirrored,
        )

    def _get_player_route_for_current_map(self, target_name):
        route = self.store.get_best_player_record(
            target_name=target_name,
            map_code=self.current_map,
            mirrored=self.current_mirrored,
            map_hash=self.current_map_hash,
        )

        if route is not None:
            return route

        opposite_route = self.store.get_best_player_record(
            target_name=target_name,
            map_code=self.current_map,
            mirrored=not self.current_mirrored,
            map_hash=self.current_map_hash,
        )

        return self._mirror_route_for_orientation(
            opposite_route,
            map_width=self.current_map_width,
            target_mirrored=self.current_mirrored,
        )

    @staticmethod
    def _orientation_status_line(status, mirrored):
        orientation = "YES" if mirrored else "NO"
        key = "mirrored" if mirrored else "normal"
        item = status.get(key)
        map_code = int(status["mapCode"])
        map_hash = str(status.get("mapHash") or "UNKNOWN")

        if item is None:
            return (
                f"@{map_code} | hash={map_hash[:12]} | "
                f"MIRRORED={orientation} | KAYITSIZ"
            )

        return (
            f"@{map_code} | hash={map_hash[:12]} | "
            f"MIRRORED={orientation} | KAYITLI | "
            f"{item['seconds']:.3f}s | "
            f"{item['name']} | source={item['source']} | "
            f"{item['points']} pts | "
            f"ID={item['source']}:{item['id']}"
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

    @staticmethod
    def _route_record_reference(route):
        if not route or route.get("id") is None:
            return "UNKNOWN"

        source = str(route.get("source") or "").upper()

        if source not in ("SELF", "PLAYER"):
            kind = str(route.get("kind") or "").lower()
            source = "PLAYER" if "passive" in kind else "SELF"

        return f"{source}:{int(route['id'])}"

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
            # ONE jump only, exactly life +5.00s.
            target_offset_seconds = 5.00

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
            ) + 1

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
                "reason=afkfarming-jump-1 "
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

            try:
                await self.replayer._mirror_to_local_client(
                    source_conn,
                    jump_event,
                    self.self_session_id,
                )
            except Exception as exc:
                print(
                    "[AFKFARMING LOCAL ERROR] "
                    f"{type(exc).__name__}: {exc}"
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
                "reason=afkfarming-jump-1-release "
                f"map=@{self.current_map} "
                f"round={round_id} "
                "jump=False"
            )

            await (
                source_conn.destination
                .write_packet_instance(release_packet)
            )

            try:
                await self.replayer._mirror_to_local_client(
                    source_conn,
                    release_event,
                    self.self_session_id,
                )
            except Exception as exc:
                print(
                    "[AFKFARMING LOCAL ERROR] "
                    f"{type(exc).__name__}: {exc}"
                )

            # Jump is done, but IMPORTANT:
            # afk_waiting_for_route remains True.
            self.afk_jumps_done_for_life = True
            self.afk_jump_pending = False

            print(
                f"[AFKFARMING] jump DONE "
                f"map=@{self.current_map} "
                "count=1 | still waiting for first eligible route"
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
            or self.afk_jumps_done_for_life
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
            "-> one jump scheduled for 5.00s"
        )

    async def _activate_learned_route_now(self, route):
        if not route or not self._map_context_ready():
            return False

        owner = self._route_owner(route)
        seconds = self._route_seconds(route)

        self.afk_waiting_for_route = False

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

            if started:
                self.replay_started_this_round = True
                print(
                    f"[REPLAY ROUND FLAG] "
                    f"map=@{self.current_map} "
                    "replayStartedThisRound=True "
                    "source=afkfarming"
                )

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
            f"started={started} "
            f"playMode={self.play_mode} "
            f"playPending={self.play_pending} "
            f"waitingForRoute={self.afk_waiting_for_route}"
        )

        return started

    def _arm_record_for_current_map(self):
        if not (
            self.record_mode
            and self._map_context_ready()
            and self.current_round_verified
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

    def _load_play_for_current_map(
        self,
        route=None,
        *,
        route_checked=False,
    ):
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

        # NewRound already loads this route for its status line. Reusing it
        # avoids a duplicate SQLite lookup at the latency-sensitive start of
        # every map. Other callers can keep the original lookup behavior.
        if not route_checked:
            route = self._get_best_route_for_current_map()

        if route is None:
            self.replayer.arm(None)

            self.auto_record_fallback = True

            if self.current_round_verified:
                # Use the same plain Recorder used by /record.
                self.recorder.arm(
                    map_code=self.current_map,
                    mirrored=self.current_mirrored,
                    map_hash=self.current_map_hash,
                    round_id=self.current_round_id,
                )
            else:
                self.recorder.disarm(
                    "joined-mid-round-snapshot"
                )

            print(
                "[AUTOLEARN] NO ROUTE -> "
                "manual movement allowed; "
                + (
                    "recording this attempt"
                    if self.current_round_verified
                    else "recording disabled for join snapshot"
                )
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

        # A fresh life needs its own EnterHole proof before it can be saved.
        self.self_enter_hole_context = None

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
        self.afk_jumps_done_for_life = False

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
                    and not self.replayer.is_active()
                    and not self.awaiting_countdown_end
                )

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
        self.self_enter_hole_context = None
        self.last_self_alive_signal_ns = None
        self.afk_life_start_ns = None
        self.afk_jumps_done_for_life = False

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

    async def _send_room_message_to_backend(
        self,
        message,
    ):
        """
        Send a REAL room chat message as the connected player.

        This is different from _chat(), which only shows a local proxy/status
        line in the game client.
        """
        text = str(message).strip()

        if not text:
            print(
                "[CHATAFTERFIRST] SEND SKIP empty message"
            )
            return False

        source_conn = self.serverbound_source

        if (
            source_conn is None
            or source_conn.destination is None
        ):
            print(
                "[CHATAFTERFIRST] SEND FAILED no backend connection"
            )
            return False

        try:
            packet = serverbound.RoomMessagePacket(
                message=text,
            )

            print(
                "[TX->SERVER] "
                f"packet={type(packet).__name__} "
                f"reason=chatafterfirst "
                f"message={text!r}"
            )

            await (
                source_conn.destination
                .write_packet_instance(
                    packet
                )
            )

            print(
                "[CHATAFTERFIRST] SENT "
                f"map=@{self.current_map} "
                f"message={text!r}"
            )

            return True

        except Exception as exc:
            print(
                "[CHATAFTERFIRST ERROR] "
                f"{type(exc).__name__}: {exc}"
            )
            return False

    async def _maybe_chat_after_first(
        self,
        *,
        winner_session_id,
    ):
        """
        Fire once per round only when:
          - feature ON
          - configured message exists
          - saved replay actually STARTED this round
          - first victory belongs to our session
        """
        if not self.chat_after_first_enabled:
            return False

        if not self.chat_after_first_message:
            return False

        if self.chat_after_first_sent_this_round:
            return False

        if not self.replay_started_this_round:
            print(
                "[CHATAFTERFIRST] SKIP "
                "first place was not from a started saved replay"
            )
            return False

        if self.self_session_id is None:
            return False

        if int(winner_session_id) != int(self.self_session_id):
            return False

        sent = await self._send_room_message_to_backend(
            self.chat_after_first_message
        )

        if sent:
            self.chat_after_first_sent_this_round = True

        return sent

    async def _chat(self, message, source=None, *, force=False):
        """
        Show proxy command/status messages inside the game client.
        """
        if not force and not getattr(self, "chat_logging", True):
            return False

        conn = source or self.serverbound_source

        if conn is None:
            print(f"[CHAT FALLBACK] {message}")
            return False

        try:
            await conn.write_packet(
                clientbound.GeneralMessagePacket,
                message=f"<J>[emirhankarakoc v1.10]</J> {message}",
            )
            return True
        except Exception as exc:
            print(
                f"[CHAT ERROR] "
                f"{type(exc).__name__}: {exc}"
            )
            return False

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

    @pak.packet_listener(
        clientbound.StartRoundCountdownPacket
    )
    async def on_start_round_countdown(
        self,
        source,
        packet,
    ):
        previous_active = self.round_countdown_active
        current_active = bool(
            packet.activate_countdown
        )
        self.round_countdown_active = current_active
        self.round_countdown_packet_seen = True

        print(
            "[COUNTDOWN EDGE] "
            f"previous={previous_active} "
            f"current={current_active} "
            f"map=@{self.current_map} "
            f"round={self.current_round_id}"
        )

        # In the records room the authoritative release sequence is
        # True -> NewRound/Alive -> False. Start at that False edge and map the
        # first saved left/right/jump checkpoint to the edge itself.
        if (
            previous_active
            and not current_active
            and self.awaiting_countdown_end
            and self.selected_route is not None
            and not self.replayer.is_active()
        ):
            release_time = asyncio.get_running_loop().time()
            started = await self._start_selected_route_at_countdown_release(
                release_time=release_time,
            )

            # The authoritative edge has already passed. If backend binding
            # was unavailable, release the movement fallback instead of
            # waiting forever for another False packet in the same round.
            self.awaiting_countdown_end = False

            print(
                "[COUNTDOWN RELEASE] "
                f"map=@{self.current_map} "
                f"round={self.current_round_id} "
                "firstControl=DIRECT-BACKEND-WRITE "
                f"started={started}"
            )

    @pak.packet_listener(
        serverbound.JoinRoomPacket
    )
    async def on_join_room(
        self,
        source,
        packet,
    ):
        """Invalidate the previous hand before entering another room."""
        self._bind_source(source)

        old_map = self.current_map
        old_round = self.current_round_id

        self.current_round_verified = False
        self.pending_join_snapshot = True
        self.current_round_join_snapshot = False
        self.self_enter_hole_context = None
        self.first_victory_session_id = None

        self.self_alive = False
        self.last_self_alive_signal_ns = None
        self.afk_life_start_ns = None
        self.afk_jumps_done_for_life = False
        self.play_pending = False
        self.selected_route = None

        self.replayer.stop("join-room")
        self.local_player_replayer.stop("join-room")
        self._cancel_afk_jump_task("join-room")

        # Preserve a legitimately completed run during its short final
        # movement window; every other stale recorder context is discarded.
        if not self.self_victory_capture_pending:
            self.recorder.disarm("join-room")

        self.player_recorder.invalidate_round("join-room")
        self.winner_recorder.invalidate_round("join-room")
        self.players_by_session = {}
        self.sessions_by_name = {}

        self.current_map = None
        self.current_round_id = None
        self.current_map_hash = None
        self.current_map_width = 800.0
        self.current_round_anchor_time = None
        self.round_countdown_active = False
        self.round_countdown_packet_seen = False
        self.awaiting_countdown_end = False

        print(
            f"[ROUND CONTEXT] INVALIDATED room-change "
            f"oldMap=@{old_map} oldRound={old_round} "
            f"targetRoom={getattr(packet, 'name', 'UNKNOWN')}"
        )

        # Forward JoinRoomPacket normally.
        return

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
        # Capture this before SQLite, XML and terminal work. All saved event
        # timestamps are scheduled against this one deterministic clock.
        round_anchor_time = asyncio.get_running_loop().time()
        self.current_round_anchor_time = round_anchor_time
        self.awaiting_countdown_end = False

        is_join_snapshot = bool(
            self.pending_join_snapshot
        )
        self.pending_join_snapshot = False
        self.current_round_join_snapshot = is_join_snapshot

        self.current_round_verified = False
        self.self_enter_hole_context = None

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
        self.afk_jumps_done_for_life = False

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

        self.current_map_width = self._map_width_from_xml(
            xml
        )

        self.current_map_hash = (
            hashlib.sha256(
                xml.encode(
                    "utf-8"
                )
            )
            .hexdigest()
        )

        self.current_round_verified = not is_join_snapshot

        print(
            "[ROUND VALIDATION] "
            f"map=@{self.current_map} "
            f"round={self.current_round_id} "
            f"source={'JOIN-SNAPSHOT' if is_join_snapshot else 'FRESH-NEWROUND'} "
            f"recordable={self.current_round_verified}"
        )

        replacement_packet = None

        ground_resize_enabled = any(
            setting["enabled"]
            for setting in self.ground_resize_settings.values()
        )

        if ground_resize_enabled and xml:
            (
                client_xml,
                changed_counts,
            ) = self._expand_configured_grounds(xml)

            if any(changed_counts.values()):
                replacement_packet = packet.copy(
                    xml=client_xml
                )

            for setting_key, setting in self.ground_resize_settings.items():
                if not setting["enabled"]:
                    continue

                print(
                    f"[SISMANLAT{setting_key.upper()}] "
                    f"map=@{self.current_map} "
                    f"grounds={changed_counts.get(setting['ground_type'], 0)} "
                    f"widthGrowth={self._format_xml_number(setting['width_px'])}px "
                    f"heightGrowth={self._format_xml_number(setting['height_px'])}px "
                    f"invisible={setting['force_invisible']} "
                    f"mode={'overlay' if setting['force_invisible'] else 'direct'}"
                )

        print(
            f"[MAP] "
            f"map={self.current_map} "
            f"round={self.current_round_id} "
            f"mirrored={self.current_mirrored} "
            f"hash={self.current_map_hash[:12]}"
        )

        orientation_status = self.store.get_orientation_status(
            map_code=self.current_map,
            map_hash=self.current_map_hash,
        )

        for mirrored_value in (False, True):
            current_marker = (
                " CURRENT"
                if mirrored_value == self.current_mirrored
                else ""
            )
            print(
                "[RECORD STATUS] "
                + self._orientation_status_line(
                    orientation_status,
                    mirrored_value,
                )
                + current_marker
            )

        if self.player_recorder.enabled:
            if self.current_round_verified:
                self.player_recorder.start_map(
                    map_code=self.current_map,
                    mirrored=self.current_mirrored,
                    map_hash=self.current_map_hash,
                    round_id=self.current_round_id,
                )

                self._resolve_recordplayer_target()
            else:
                self.player_recorder.invalidate_round(
                    "joined-mid-round-snapshot"
                )

        # Both persistent modes can learn from other players.
        self.winner_recorder.set_enabled(
            self.record_mode
            or self.play_mode
            or self.afk_farming
        )

        if self.current_round_verified:
            self.winner_recorder.new_round(
                map_code=self.current_map,
                mirrored=self.current_mirrored,
                map_hash=self.current_map_hash,
                round_id=self.current_round_id,
            )
        else:
            self.winner_recorder.invalidate_round(
                "joined-mid-round-snapshot"
            )

        self.first_victory_session_id = None
        self.selected_route = None
        self.auto_record_fallback = False
        self.play_pending = False
        self.afk_waiting_for_route = False

        self.chat_after_first_sent_this_round = False
        self.replay_started_this_round = False

        # ALWAYS show the currently usable record at hand start.
        round_best = self._get_best_route_for_current_map()

        # AFK farming owns autoplay when a route already exists.
        if self.afk_farming and round_best is not None:
            self.play_mode = True

        # Prepare replay before any optional in-game status write. This also
        # reuses round_best instead of querying SQLite a second time.
        if self.play_mode:
            self._load_play_for_current_map(
                round_best,
                route_checked=True,
            )

            if self.selected_route is not None:
                if self.round_countdown_active:
                    self.awaiting_countdown_end = True
                    self.play_pending = False
                    started = False
                else:
                    # Rooms that explicitly have no countdown release their
                    # first real control directly from NewRound.
                    started = self._start_selected_route(
                        trigger="new-round-no-countdown",
                        anchor_time=round_anchor_time,
                        first_control_target_seconds=0.0,
                        start_at_first_control=True,
                    )

                print(
                    "[MAP START CLOCK] "
                    f"map=@{self.current_map} "
                    f"round={self.current_round_id} "
                    f"countdownActive={self.round_countdown_active} "
                    f"countdownPacketSeen={self.round_countdown_packet_seen} "
                    f"waitingForFalseEdge={self.awaiting_countdown_end} "
                    f"started={started}"
                )

        elif self.record_mode:
            self._arm_record_for_current_map()

        if round_best is not None:
            owner = self._route_owner(round_best)
            seconds = self._route_seconds(round_best)
            record_reference = self._route_record_reference(round_best)

            asyncio.create_task(
                self._chat(
                    f"ROUND @{self.current_map} | "
                    f"BEST {seconds:.3f}s | "
                    f"owner={owner} | "
                    f"{len(round_best.get('events', []))} points | "
                    f"ID={record_reference}"
                )
            )

            print(
                f"[ROUND BEST] map=@{self.current_map} "
                f"owner={owner} "
                f"time={seconds:.3f}s "
                f"points={len(round_best.get('events', []))} "
                f"ID={record_reference}"
            )
        else:
            print(
                f"[ROUND BEST] map=@{self.current_map} NONE"
            )

            asyncio.create_task(
                self._chat(
                    f"ROUND @{self.current_map} | NO SAVED RUN"
                )
            )

        self.afk_waiting_for_route = (
            self.afk_farming
            and round_best is None
        )

        self.afk_jump_pending = (
            self.afk_waiting_for_route
        )

        print(
            f"[AFKFARMING STATE] "
            f"map=@{self.current_map} "
            f"waitingForRoute={self.afk_waiting_for_route} "
            f"jumpPending={self.afk_jump_pending}"
        )

        if self.afk_farming and round_best is None:
            asyncio.create_task(
                self._chat(
                    f"AFKFARMING @{self.current_map} | "
                    "no run: 1 jump at 5.00s; "
                    "first eligible winner -> instant replay"
                )
            )

        # Incoming packets are immutable in Caseus. Send a copied NewRound
        # packet to the local client and suppress only the original packet.
        # On failure, return normally so the untouched server packet is still
        # forwarded instead of breaking the round.
        if replacement_packet is not None:
            try:
                await source.destination.write_packet_instance(
                    replacement_packet
                )
                return self.DO_NOTHING
            except Exception as exc:
                print(
                    "[SISMANLAT ERROR] "
                    f"{type(exc).__name__}: {exc} | "
                    "original map forwarded"
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
        # /chatlogging on|off
        # --------------------------

        if command == "chatlogging":
            if argument is None:
                state = "ON" if self.chat_logging else "OFF"
                print(f"[CHATLOGGING] {state}")
                await self._chat(
                    f"CHATLOGGING {state}",
                    source,
                    force=True,
                )
                return self.DO_NOTHING

            if argument in ("on", "start"):
                self.chat_logging = True
                state = "ON"
            elif argument in ("off", "stop"):
                self.chat_logging = False
                state = "OFF"
            else:
                await self._chat(
                    "usage: /chatlogging on | off",
                    source,
                    force=True,
                )
                return self.DO_NOTHING

            print(
                f"[CHATLOGGING] {state} | "
                "terminal logs remain enabled"
            )
            await self._chat(
                f"CHATLOGGING {state} | terminal logs remain enabled",
                source,
                force=True,
            )
            return self.DO_NOTHING

        # --------------------------
        # /sismanlattrambolin on [width_px] [height_px] | off
        # /sismanlatlav on [width_px] [height_px] | off
        # --------------------------

        ground_resize_commands = {
            "sismanlattrambolin": "trambolin",
            "sismanlatlav": "lav",
        }

        if command in ground_resize_commands:
            await self._configure_ground_resize(
                ground_resize_commands[command],
                command,
                argument_raw,
                source,
            )
            return self.DO_NOTHING

        # --------------------------
        # /debuglogs on|off
        # --------------------------

        if command == "debuglogs":
            if argument is None:
                await self._chat(
                    f"DEBUGLOGS={'ON' if self.debug_logs else 'OFF'}",
                    source,
                )
                return self.DO_NOTHING

            if argument in ("on", "start"):
                enabled = True
            elif argument in ("off", "stop"):
                enabled = False
            else:
                await self._chat(
                    "usage: /debuglogs on | off",
                    source,
                )
                return self.DO_NOTHING

            self.debug_logs = enabled
            self.recorder.set_debug_logs(enabled)
            self.replayer.set_debug_logs(enabled)
            self.player_recorder.set_debug_logs(enabled)
            self.local_player_replayer.set_debug_logs(enabled)

            state = "ON" if enabled else "OFF"

            print(
                f"[DEBUGLOGS] {state} | "
                "movement packet/position terminal logs "
                f"{'enabled' if enabled else 'suppressed'}"
            )

            await self._chat(
                f"DEBUGLOGS {state}",
                source,
            )

            return self.DO_NOTHING

        # --------------------------
        # /chatafterfirst
        # /chatafterfirst on
        # /chatafterfirst off
        # /chatafterfirst [message]
        # --------------------------

        if command == "chatafterfirst":
            if argument_raw is None:
                status = (
                    "ON"
                    if self.chat_after_first_enabled
                    else "OFF"
                )

                configured = (
                    self.chat_after_first_message
                    if self.chat_after_first_message
                    else "<not set>"
                )

                await self._chat(
                    f"CHATAFTERFIRST={status} | "
                    f"message={configured}",
                    source,
                )

                print(
                    f"[CHATAFTERFIRST] "
                    f"status={status} "
                    f"message={configured!r}"
                )

                return self.DO_NOTHING

            if argument == "off":
                self.chat_after_first_enabled = False

                await self._chat(
                    "CHATAFTERFIRST OFF",
                    source,
                )

                print(
                    "[CHATAFTERFIRST] OFF"
                )

                return self.DO_NOTHING

            if argument == "on":
                if not self.chat_after_first_message:
                    await self._chat(
                        "CHATAFTERFIRST has no message. "
                        "Use /chatafterfirst your message first.",
                        source,
                    )

                    print(
                        "[CHATAFTERFIRST] ON FAILED no message configured"
                    )

                    return self.DO_NOTHING

                self.chat_after_first_enabled = True

                await self._chat(
                    f"CHATAFTERFIRST ON | "
                    f"message={self.chat_after_first_message}",
                    source,
                )

                print(
                    "[CHATAFTERFIRST] ON "
                    f"message={self.chat_after_first_message!r}"
                )

                return self.DO_NOTHING

            # Any other argument is the message itself.
            message = argument_raw.strip()

            if not message:
                await self._chat(
                    "usage: /chatafterfirst on | off | [message]",
                    source,
                )
                return self.DO_NOTHING

            self.chat_after_first_message = message
            self.chat_after_first_enabled = True

            await self._chat(
                f"CHATAFTERFIRST ON | message={message}",
                source,
            )

            print(
                "[CHATAFTERFIRST] MESSAGE SET + ON "
                f"message={message!r}"
            )

            return self.DO_NOTHING

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
                    route = self._get_best_route_for_current_map()

                if route is not None:
                    self.afk_waiting_for_route = False
                    self.afk_jump_pending = False

                    self.play_mode = True
                    self.selected_route = None
                    self._load_play_for_current_map()

                    await self._chat(
                        f"AFKFARMING ON | existing run found "
                        f"@{self.current_map}; autoplay enabled",
                        source,
                    )
                else:
                    self.afk_waiting_for_route = self._map_context_ready()
                    self.afk_jump_pending = self.afk_waiting_for_route

                    await self._chat(
                        "AFKFARMING ON | no run => 1 jump at 5s, "
                        "then first eligible winner is replayed immediately",
                        source,
                    )

                print("[MODE] AFKFARMING=ON")
                return self.DO_NOTHING

            if argument in ("off", "stop"):
                self.afk_farming = False
                self.afk_waiting_for_route = False
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
                        "usage: /timelist | /timelist @7680000",
                        source,
                    )
                    return self.DO_NOTHING

            statuses = self.store.get_all_orientation_statuses(
                map_code=requested_map
            )

            # The current exact map can be reported as two KAYITSIZ rows even
            # before either orientation has ever been stored in SQLite.
            if (
                not statuses
                and requested_map is not None
                and self._map_context_ready()
                and requested_map == self.current_map
            ):
                statuses = [
                    self.store.get_orientation_status(
                        map_code=self.current_map,
                        map_hash=self.current_map_hash,
                    )
                ]

            if not statuses:
                label = (
                    f"@{requested_map}"
                    if requested_map is not None
                    else "ALL"
                )
                print(f"[TIMELIST] {label} EMPTY")
                await self._chat(
                    f"TIMELIST {label} | no saved variants",
                    source,
                )
                return self.DO_NOTHING

            unique_maps = {
                int(status["mapCode"])
                for status in statuses
            }

            print()
            print("=" * 96)
            print(
                f" TIMELIST | {len(unique_maps)} MAP(S) | "
                f"{len(statuses)} EXACT HASH VARIANT(S) | "
                f"{len(statuses) * 2} ORIENTATION ROW(S)"
            )
            print("=" * 96)

            await self._chat(
                f"TIMELIST | {len(unique_maps)} map(s) | "
                f"{len(statuses)} hash variant(s) | YES/NO separated",
                source,
            )

            for status in statuses:
                for mirrored_value in (False, True):
                    line = self._orientation_status_line(
                        status,
                        mirrored_value,
                    )
                    print(f"[TIMELIST] {line}")
                    await self._chat(
                        line,
                        source,
                    )

            print("=" * 96)
            print()
            return self.DO_NOTHING

        # --------------------------
        # /timedelete
        # /timedelete @mapCode
        # /timedelete SELF:id | PLAYER:id | id
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
                    try:
                        requested_map = int(value[1:])
                    except ValueError:
                        await self._chat(
                            "usage: /timedelete | /timedelete @map | "
                            "/timedelete ID | /timedelete all",
                            source,
                        )
                        return self.DO_NOTHING
                else:
                    reference = value.upper()
                    record_source = None
                    record_id_text = reference

                    if ":" in reference:
                        source_text, record_id_text = reference.split(":", 1)
                        source_aliases = {
                            "S": "SELF",
                            "SELF": "SELF",
                            "P": "PLAYER",
                            "PLAYER": "PLAYER",
                        }
                        record_source = source_aliases.get(source_text)

                        if record_source is None:
                            record_id_text = ""
                    elif len(reference) > 1 and reference[0] in ("S", "P"):
                        record_source = (
                            "SELF" if reference[0] == "S" else "PLAYER"
                        )
                        record_id_text = reference[1:]

                    try:
                        record_id = int(record_id_text)
                    except ValueError:
                        await self._chat(
                            "usage: /timedelete SELF:12 | "
                            "/timedelete PLAYER:7 | /timedelete 12",
                            source,
                        )
                        return self.DO_NOTHING

                    result = self.store.delete_record(
                        record_id=record_id,
                        source=record_source,
                    )

                    if not result["ok"]:
                        if result["reason"] == "ambiguous":
                            choices = " or ".join(
                                match["reference"]
                                for match in result["matches"]
                            )
                            message = (
                                f"TIMEDELETE ID={record_id} ambiguous | "
                                f"use /timedelete {choices}"
                            )
                        else:
                            message = (
                                f"TIMEDELETE ID={reference} failed | "
                                f"{result['reason']}"
                            )

                        await self._chat(message, source)
                        return self.DO_NOTHING

                    if (
                        self.current_map is not None
                        and int(result["mapCode"]) == int(self.current_map)
                    ):
                        self.replayer.stop("timedelete-record-id")
                        self.selected_route = None
                        self.play_pending = False

                        if self.play_mode and self._map_context_ready():
                            self._load_play_for_current_map()

                    await self._chat(
                        f"TIMEDELETE ID={result['reference']} | "
                        f"@{result['mapCode']} | "
                        f"MIRRORED={'YES' if result['mirrored'] else 'NO'} | "
                        f"deleted {result['deleted']} row(s)",
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

            chat_first_status = (
                "ON"
                if self.chat_after_first_enabled
                else "OFF"
            )

            debug_status = (
                "ON"
                if self.debug_logs
                else "OFF"
            )

            chat_logging_status = (
                "ON"
                if self.chat_logging
                else "OFF"
            )

            sismanlat_trambolin_status = (
                "ON"
                if self.ground_resize_settings["trambolin"]["enabled"]
                else "OFF"
            )

            sismanlat_lav_status = (
                "ON"
                if self.ground_resize_settings["lav"]["enabled"]
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
                    f"| AFKFARMING={afk_status} "
                    f"| CHATAFTERFIRST={chat_first_status} "
                    f"| CHATLOGGING={chat_logging_status} "
                    f"| TRAMBOLIN={sismanlat_trambolin_status} "
                    f"| LAV={sismanlat_lav_status} "
                    f"| DEBUGLOGS={debug_status}"
                ),

                (
                    "/chatlogging on/off | proxy durum mesajlarinin "
                    "oyun chatine yazilmasini acar/kapatir; terminal acik kalir."
                ),

                (
                    "/sismanlattrambolin on [width_px] [height_px] "
                    "[gorunmezacik|gorunmezkapali] | "
                    "trambolinleri client'ta buyutur; off ile kapanir."
                ),

                (
                    "/sismanlatlav on [width_px] [height_px] "
                    "[gorunmezacik|gorunmezkapali] | "
                    "lavlari client'ta buyutur; off ile kapanir."
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
                    "/debuglogs on/off | movement packet ve position "
                    "terminal spamini acar/kapatir. Default=OFF."
                ),

                (
                    "/chatafterfirst on/off/[message] | saved replay ile "
                    "1. olursan room chate otomatik mesaj yollar."
                ),

                (
                    "/afkfarming on/off | run yoksa 1 jump at 5s; "
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
                    "/timelist | tum map/hash kayitlarini MIRRORED YES/NO ayirir. "
                    "/timelist @map | tek mapin iki yonunu gosterir."
                ),

                (
                    "/timedelete [@map] | map kaydini siler. "
                    "/timedelete ID | tek kaydi siler. "
                    "/timedelete all | tum kayitlari siler."
                ),

                (
                    "/help | bu listeyi gosterir."
                ),
            ]

            print()
            print("=" * 54)
            print(" TFM emirhankarakoc v1.10 HELP")
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

            record = self._get_player_route_for_current_map(
                argument_raw
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
                self.replay_started_this_round = True

                print(
                    f"[REPLAY ROUND FLAG] "
                    f"map=@{self.current_map} "
                    "replayStartedThisRound=True "
                    "source=playplayer"
                )

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

    def _ensure_self_alive_from_movement(self, observed_ns):
        """Rare fallback path for rooms that omit/delay Alive activity."""
        if self.self_alive:
            return

        self.self_alive = True

        if self.afk_life_start_ns is None:
            self.afk_life_start_ns = int(observed_ns)

        self.afk_jumps_done_for_life = False

        print(
            "[LIFE] SELF movement-fallback "
            "ALIVE -> t=0"
        )

        if self.play_mode and self.auto_record_fallback:
            self.recorder.on_alive(
                observed_ns,
                source="movement-fallback",
            )

        elif self.record_mode and not self.play_mode:
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

        # Rare post-victory capture path. Normal recording and playback never
        # enter this branch.
        if self.self_victory_capture_pending:
            if self.recorder.armed and self.recorder.active:
                self.recorder.record_movement(
                    packet,
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

        # Idle/heartbeat movement packets can arrive during 3-2-1. They are
        # neither a release signal nor a valid fallback while the authoritative
        # True -> False countdown edge is pending.
        if (
            self.play_mode
            and self.awaiting_countdown_end
            and self.selected_route is not None
        ):
            return self.DO_NOTHING

        # Most rooms already delivered Alive. This slow setup runs at most
        # once per life when that signal is missing or late.
        if not self.self_alive:
            self._ensure_self_alive_from_movement(
                time.perf_counter_ns()
            )

        # AFK scheduling is evaluated only until its one task is created.
        if self.afk_jump_pending and self.afk_jump_task is None:
            self._maybe_start_afk_jumps(
                source,
                packet,
            )

        # RECORD hot path: identical shape to the smooth baseline.
        if (
            self.record_mode
            and not self.play_mode
            and self.recorder.active
        ):
            self.recorder.record_movement(
                packet
            )

            return

        # PLAY hot path: NewRound normally started the route already. Movement
        # is only a fallback when NewRound had no bound backend connection, or
        # for a same-round respawn without a new map packet.
        if (
            self.play_mode
            and not self.auto_record_fallback
            and self.selected_route is not None
        ):
            if (
                self.play_pending
                and not self.replayer.is_active()
            ):
                started = self._start_selected_route(
                    trigger="movement-fallback",
                    source_conn=source,
                )

            if self.replayer.is_active():
                return self.DO_NOTHING

        # PLAY with no route keeps the current autolearn behavior. Once the
        # recorder is active this is the same direct record call as above.
        if (
            self.play_mode
            and self.auto_record_fallback
        ):
            self.recorder.ensure_alive_from_movement(
                time.perf_counter_ns()
            )

            self.recorder.record_movement(
                packet
            )

            return

        # A late /record on can reach here before the first Alive packet.
        if self.record_mode:
            self.recorder.ensure_alive_from_movement(
                time.perf_counter_ns()
            )

            self.recorder.record_movement(
                packet
            )

            return

    # ==============================================================
    # CLIENT -> SERVER HOLE / DEATH
    # ==============================================================

    @pak.packet_listener(
        serverbound.EnterHolePacket
    )
    async def on_self_enter_hole(
        self,
        source,
        packet,
    ):
        self._bind_source(source)

        victory_map_code = int(packet.map_code)
        victory_round_id = int(packet.round_id)
        valid = (
            self.current_round_verified
            and self.current_map is not None
            and self.current_round_id is not None
            and victory_map_code == int(self.current_map)
            and victory_round_id == int(self.current_round_id)
        )

        self.self_enter_hole_context = {
            "mapCode": victory_map_code,
            "roundId": victory_round_id,
            "valid": bool(valid),
            "observedNs": time.perf_counter_ns(),
        }

        print(
            f"[ENTER HOLE CONTEXT] "
            f"newRoundMap=@{self.current_map} "
            f"victoryMap=@{victory_map_code} "
            f"newRoundRound={self.current_round_id} "
            f"victoryRound={victory_round_id} "
            f"match={'YES' if valid else 'NO'}"
        )

        # Forward the real EnterHolePacket normally.
        return

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
        self.self_enter_hole_context = None
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
        # Critical path: passive winner learning stays synchronous so the
        # first-place trajectory is complete before PlayerVictoryPacket.
        self.winner_recorder.observe(
            packet,
            self_session_id=self.self_session_id,
        )

        # Secondary /recordplayer work is a single unlocked attribute check
        # while disabled (the normal case).
        session_id = int(packet.session_id)
        target_session_id = self.player_recorder.target_session_id

        if (
            target_session_id is None
            or session_id != int(target_session_id)
        ):
            return

        if (
            self.self_session_id is not None
            and session_id == int(self.self_session_id)
        ):
            return

        self.player_recorder.observe(packet)

    async def _finalize_self_victory_after_capture(
        self,
        *,
        victory_ns,
        finish_seconds,
        victory_map_code,
        victory_round_id,
        save_mode,
    ):
        try:
            await asyncio.sleep(0.150)

            context = self.recorder.context

            if (
                context is None
                or int(context.get("mapCode", -1))
                != int(victory_map_code)
                or int(context.get("roundId", -1))
                != int(victory_round_id)
            ):
                print(
                    "[RECORD FINALIZE SKIP] "
                    f"victoryMap=@{victory_map_code} "
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
        victory_ns = time.perf_counter_ns()

        winner_session_id = int(packet.session_id)
        is_self_victory = (
            self.self_session_id is not None
            and winner_session_id == int(self.self_session_id)
        )

        winner_name = self.players_by_session.get(
            winner_session_id,
            (
                self.self_name
                if is_self_victory
                else f"session-{winner_session_id}"
            ),
        )

        # PlayerVictoryPacket itself has no mapCode in the standard protocol.
        # For SELF, EnterHolePacket is the authoritative victory context.
        victory_map_code = getattr(packet, "map_code", None)
        victory_round_id = getattr(packet, "round_id", None)
        hole_context = (
            self.self_enter_hole_context
            if is_self_victory
            else None
        )

        if is_self_victory and hole_context is not None:
            if victory_map_code is None:
                victory_map_code = hole_context.get("mapCode")

            if victory_round_id is None:
                victory_round_id = hole_context.get("roundId")

        invalid_reasons = []

        if (
            not self.current_round_verified
            or self.current_map is None
            or self.current_round_id is None
        ):
            invalid_reasons.append(
                "joined-mid-round-snapshot"
                if self.current_round_join_snapshot
                else "new-round-not-observed"
            )

        if is_self_victory and victory_map_code is None:
            invalid_reasons.append("missing-enter-hole-map")

        if is_self_victory and victory_round_id is None:
            invalid_reasons.append("missing-enter-hole-round")

        if (
            victory_map_code is not None
            and self.current_map is not None
            and int(victory_map_code) != int(self.current_map)
        ):
            invalid_reasons.append("map-code-mismatch")

        if (
            victory_round_id is not None
            and self.current_round_id is not None
            and int(victory_round_id) != int(self.current_round_id)
        ):
            invalid_reasons.append("round-id-mismatch")

        victory_context_valid = not invalid_reasons

        if not victory_context_valid:
            reason = ",".join(invalid_reasons)
            print(
                f"[NON-NORMAL RUN SKIP] "
                f"owner={winner_name} "
                f"newRoundMap=@{self.current_map} "
                f"victoryMap=@{victory_map_code} "
                f"newRoundRound={self.current_round_id} "
                f"victoryRound={victory_round_id} "
                f"reason={reason}"
            )

            await self._chat(
                f"RECORD SKIP | NON-NORMAL RUN | "
                f"NewRound=@{self.current_map}/{self.current_round_id} | "
                f"Victory=@{victory_map_code}/{victory_round_id} | "
                f"{reason}"
            )

            self.winner_recorder.on_dead(
                winner_session_id,
                source="invalid-victory-context",
            )

            # Preserve unrelated first-place/chat behavior, but mark this
            # invalid hand consumed so no route from it can be learned.
            if self.first_victory_session_id is None:
                self.first_victory_session_id = winner_session_id

                if is_self_victory:
                    await self._maybe_chat_after_first(
                        winner_session_id=winner_session_id,
                    )

        # Learn the first ELIGIBLE finisher.
        # Blacklisted owners are ignored so they cannot poison training data.
        if (
            victory_context_valid
            and self.first_victory_session_id is None
        ):
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
                    observed_ns=victory_ns,
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

                    await self._maybe_chat_after_first(
                        winner_session_id=winner_session_id,
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
                            and self.afk_waiting_for_route
                            and record_id is not None
                        ):
                            print(
                                f"[AFKFARMING] FIRST ROUTE TRIGGER "
                                f"map=@{self.current_map} "
                                f"owner={winner_record['targetName']} "
                                f"time={learned_seconds:.3f}s "
                                "-> PLAY NOW"
                            )

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
            if victory_context_valid:
                await self._finish_player_record(
                    "victory",
                    victory_seconds=float(packet.seconds),
                )
            else:
                self.player_recorder.on_death(
                    "invalid-victory-context"
                )

        if not is_self_victory:
            return

        finish_seconds = float(
            packet.seconds
        )

        print(
            f"[VICTORY] SERVER "
            f"time={finish_seconds:.3f}s"
        )

        capture_mode = None

        if (
            victory_context_valid
            and
            self.play_mode
            and self.auto_record_fallback
            and self.recorder.active
        ):
            capture_mode = "autolearn"

        elif (
            victory_context_valid
            and self.record_mode
            and self.recorder.active
        ):
            capture_mode = "record"

        elif not victory_context_valid and self.recorder.armed:
            self.recorder.on_death(
                "invalid-victory-context"
            )

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
                    victory_map_code=int(victory_map_code),
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
        self.self_enter_hole_context = None
        self.last_self_alive_signal_ns = None
        self.afk_life_start_ns = None
