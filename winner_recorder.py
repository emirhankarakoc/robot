import copy
import time


class WinnerRecorder:
    """
    V1.9 passive recorder for ALL remote players.

    Critical rule:
        each session_id has an independent LIFE clock.

    Alive:
        clear failed old life
        t=0

    Dead:
        discard current life completely

    Movement without a prior Alive:
        start a movement-fallback life at t=0

    Victory:
        return ONLY the current life for saving
    """

    LIFE_TIMER_VERSION = 3

    def __init__(self):
        self.enabled = False

        self.map_context = None

        self.players_by_session = {}

        self.alive_by_session = {}
        self.anchor_by_session = {}
        self.events_by_session = {}
        self.facing_by_session = {}
        self.life_index_by_session = {}

        self.last_alive_signal_by_session = {}
        self.alive_signal_debounce_ns = 750_000_000  # 0.75 s

        print(
            "[WINNER RECORDER] V1.9 lifecycle recorder ready"
        )

    def set_enabled(self, enabled):
        self.enabled = bool(
            enabled
        )

        print(
            f"[WINNER RECORDER] "
            f"{'ON' if self.enabled else 'OFF'}"
        )

    def new_round(
        self,
        *,
        map_code,
        mirrored,
        map_hash,
        round_id,
    ):
        self.map_context = {
            "mapCode": int(map_code),
            "mirrored": bool(mirrored),
            "mapHash": str(map_hash),
            "roundId": int(round_id),
        }

        self.alive_by_session = {}
        self.anchor_by_session = {}
        self.events_by_session = {}
        self.facing_by_session = {}
        self.life_index_by_session = {}
        self.last_alive_signal_by_session = {}

        print(
            f"[WINNER RECORDER] NEW ROUND "
            f"map={map_code} round={round_id}"
        )

    def register_player(
        self,
        session_id,
        name,
    ):
        if (
            session_id is None
            or not name
        ):
            return

        self.players_by_session[
            int(session_id)
        ] = str(name)

    def _name(self, session_id):
        return self.players_by_session.get(
            int(session_id),
            f"session-{int(session_id)}",
        )

    def on_alive(
        self,
        session_id,
        *,
        observed_ns=None,
        source="activity",
        allow_respawn_pulse=False,
    ):
        if (
            not self.enabled
            or self.map_context is None
        ):
            return False

        if observed_ns is None:
            observed_ns = time.perf_counter_ns()

        observed_ns = int(
            observed_ns
        )

        session_id = int(
            session_id
        )

        already_alive = (
            self.alive_by_session.get(
                session_id,
                False,
            )
            and self.anchor_by_session.get(
                session_id
            )
            is not None
        )

        last_signal = (
            self.last_alive_signal_by_session.get(
                session_id
            )
        )

        separated = (
            last_signal is None
            or (
                observed_ns
                - int(last_signal)
            )
            >= self.alive_signal_debounce_ns
        )

        self.last_alive_signal_by_session[
            session_id
        ] = observed_ns

        if already_alive:
            if not (
                allow_respawn_pulse
                and separated
            ):
                return False

            discarded = len(
                self.events_by_session.get(
                    session_id,
                    [],
                )
            )

            print(
                f"[REMOTE LIFE] "
                f"{self._name(session_id)} "
                f"session={session_id} "
                f"RESPAWN ALIVE pulse -> RESET "
                f"discardedPoints={discarded}"
            )

        life_index = (
            self.life_index_by_session.get(
                session_id,
                0,
            )
            + 1
        )

        self.life_index_by_session[
            session_id
        ] = life_index

        self.alive_by_session[
            session_id
        ] = True

        self.anchor_by_session[
            session_id
        ] = observed_ns

        self.events_by_session[
            session_id
        ] = []

        self.facing_by_session[
            session_id
        ] = True

        print(
            f"[REMOTE LIFE] "
            f"{self._name(session_id)} "
            f"session={session_id} "
            f"ALIVE -> t=0 "
            f"life={life_index} "
            f"source={source}"
        )

        return True

    def on_dead(
        self,
        session_id,
        *,
        source="activity",
    ):
        if (
            not self.enabled
            or self.map_context is None
        ):
            return False

        session_id = int(
            session_id
        )

        discarded = len(
            self.events_by_session.get(
                session_id,
                [],
            )
        )

        life_index = (
            self.life_index_by_session.get(
                session_id,
                0,
            )
        )

        self.alive_by_session[
            session_id
        ] = False

        self.anchor_by_session.pop(
            session_id,
            None,
        )

        self.events_by_session[
            session_id
        ] = []

        self.facing_by_session[
            session_id
        ] = True

        self.last_alive_signal_by_session.pop(
            session_id,
            None,
        )

        print(
            f"[REMOTE LIFE] "
            f"{self._name(session_id)} "
            f"session={session_id} "
            f"DEAD -> RESET t=0 "
            f"life={life_index} "
            f"source={source} "
            f"discardedPoints={discarded}"
        )

        return True

    def observe(
        self,
        packet,
        *,
        self_session_id=None,
        observed_ns=None,
    ):
        if (
            not self.enabled
            or self.map_context is None
        ):
            return

        session_id = int(
            packet.session_id
        )

        if (
            self_session_id is not None
            and session_id
            == int(self_session_id)
        ):
            return

        if observed_ns is None:
            observed_ns = time.perf_counter_ns()

        if (
            not self.alive_by_session.get(
                session_id,
                False,
            )
            or self.anchor_by_session.get(
                session_id
            )
            is None
        ):
            self.on_alive(
                session_id,
                observed_ns=observed_ns,
                source="movement-fallback",
                allow_respawn_pulse=False,
            )

        anchor = self.anchor_by_session.get(
            session_id
        )

        if anchor is None:
            return

        t_us = max(
            0,
            (
                int(observed_ns)
                - anchor
            ) // 1000,
        )

        moving_left = bool(
            packet.moving_left
        )

        moving_right = bool(
            packet.moving_right
        )

        facing = self.facing_by_session.get(
            session_id,
            True,
        )

        if (
            moving_right
            and not moving_left
        ):
            facing = True

        elif (
            moving_left
            and not moving_right
        ):
            facing = False

        self.facing_by_session[
            session_id
        ] = facing

        friction = (
            packet.friction_info
        )

        rotation = (
            packet.rotation_info
        )

        event = {
            "tUs": int(t_us),

            "x": float(packet.x),
            "y": float(packet.y),

            "velocityX":
                float(packet.velocity_x),

            "velocityY":
                float(packet.velocity_y),

            "movingLeft":
                moving_left,

            "movingRight":
                moving_right,

            "facingRight":
                bool(facing),

            "jumping":
                bool(packet.jumping),

            "jumpingFrameIndex":
                int(
                    packet.jumping_frame_index
                ),

            "frictionCharge":
                float(
                    friction.charge
                ),

            "frictionLossRate":
                float(
                    friction.loss_rate
                ),

            "enteredPortal":
                int(
                    getattr(
                        packet.entered_portal,
                        "value",
                        packet.entered_portal,
                    )
                ),

            "rotationInfo":
                (
                    None
                    if rotation is None
                    else {
                        "rotation":
                            float(
                                rotation.rotation
                            ),

                        "angularVelocity":
                            float(
                                rotation.angular_velocity
                            ),

                        "fixedRotation":
                            bool(
                                rotation.fixed_rotation
                            ),
                    }
                ),
        }

        self.events_by_session.setdefault(
            session_id,
            [],
        ).append(
            event
        )

    def winner_record(
        self,
        session_id,
        reported_victory_seconds=None,
        *,
        observed_ns=None,
    ):
        if (
            not self.enabled
            or self.map_context is None
        ):
            return None

        if observed_ns is None:
            observed_ns = time.perf_counter_ns()

        session_id = int(
            session_id
        )

        anchor = self.anchor_by_session.get(
            session_id
        )

        events = self.events_by_session.get(
            session_id,
            [],
        )

        if (
            not self.alive_by_session.get(
                session_id,
                False,
            )
            or anchor is None
            or not events
        ):
            return None

        life_elapsed_seconds = max(
            0.0,
            (
                int(observed_ns)
                - int(anchor)
            )
            / 1_000_000_000.0,
        )

        name = self._name(
            session_id
        )

        life_index = (
            self.life_index_by_session.get(
                session_id,
                0,
            )
        )

        record = {
            "version": 3,

            "lifeTimerVersion":
                self.LIFE_TIMER_VERSION,

            "kind":
                "passive-winner-record",

            "targetName":
                name,

            "targetSessionId":
                session_id,

            "mapCode":
                self.map_context["mapCode"],

            "mirrored":
                self.map_context["mirrored"],

            "mapHash":
                self.map_context["mapHash"],

            "roundId":
                self.map_context["roundId"],

            "lifeIndex":
                life_index,

            "reason":
                "victory",

            "victorySeconds":
                life_elapsed_seconds,

            "reportedVictorySeconds":
                (
                    None
                    if reported_victory_seconds
                    is None
                    else float(
                        reported_victory_seconds
                    )
                ),

            "events":
                copy.deepcopy(
                    events
                ),
        }

        print(
            f"[WINNER LIFE] "
            f"{name} "
            f"life={life_index} "
            f"SUCCESS "
            f"lifeTime={life_elapsed_seconds:.3f}s "
            f"serverReported="
            f"{'n/a' if reported_victory_seconds is None else f'{float(reported_victory_seconds):.3f}s'} "
            f"points={len(events)}"
        )

        # Finished life must not continue collecting.
        self.alive_by_session[
            session_id
        ] = False

        self.anchor_by_session.pop(
            session_id,
            None,
        )

        self.events_by_session[
            session_id
        ] = []

        self.facing_by_session[
            session_id
        ] = True

        return record
