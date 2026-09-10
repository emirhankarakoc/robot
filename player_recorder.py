import copy
import threading
import time


class PlayerRecorder:
    """
    V1.8 passive recorder for one selected REMOTE player.

    Every life has its own clock.

    Alive:
        t = 0
        events cleared

    Dead:
        failed attempt discarded
        timer cleared

    Victory:
        only the current life is returned for saving
    """

    LIFE_TIMER_VERSION = 2

    def __init__(self):
        self.lock = threading.RLock()

        self.target_name = None
        self.target_session_id = None

        self.map_context = None

        self.alive = False
        self.anchor_ns = None
        self.events = []
        self.life_index = 0

        self.facing_right = True

        print(
            "[PLAYER RECORDER] V1.8 passive life recorder ready"
        )

    @property
    def enabled(self):
        with self.lock:
            return self.target_name is not None

    @property
    def active(self):
        with self.lock:
            return (
                self.target_name is not None
                and self.target_session_id is not None
                and self.map_context is not None
                and self.alive
                and self.anchor_ns is not None
            )

    def set_target(self, nickname):
        nickname = str(nickname).strip()

        with self.lock:
            self.target_name = nickname
            self.target_session_id = None

            self.alive = False
            self.anchor_ns = None
            self.events = []
            self.life_index = 0
            self.facing_right = True

        print(
            f"[PLAYER RECORDER] target={nickname}"
        )

    def clear_target(self):
        with self.lock:
            old = self.target_name

            self.target_name = None
            self.target_session_id = None
            self.map_context = None

            self.alive = False
            self.anchor_ns = None
            self.events = []
            self.facing_right = True

        if old:
            print(
                f"[PLAYER RECORDER] OFF target={old}"
            )

    def set_session(self, session_id):
        with self.lock:
            if self.target_name is None:
                return False

            changed = (
                self.target_session_id is None
                or int(self.target_session_id)
                != int(session_id)
            )

            self.target_session_id = int(
                session_id
            )

        if changed:
            print(
                f"[PLAYER RECORDER] resolved "
                f"{self.target_name} -> session={session_id}"
            )

        return changed

    def start_map(
        self,
        *,
        map_code,
        mirrored,
        map_hash,
        round_id,
    ):
        with self.lock:
            if self.target_name is None:
                return

            self.map_context = {
                "mapCode": int(map_code),
                "mirrored": bool(mirrored),
                "mapHash": str(map_hash),
                "roundId": int(round_id),
            }

            self.alive = False
            self.anchor_ns = None
            self.events = []
            self.life_index = 0
            self.facing_right = True

        print(
            f"[PLAYER RECORDER] map armed "
            f"target={self.target_name} "
            f"map={map_code} round={round_id}"
        )

    def on_alive(
        self,
        observed_ns=None,
        source="activity",
    ):
        if observed_ns is None:
            observed_ns = time.perf_counter_ns()

        with self.lock:
            if (
                self.target_name is None
                or self.target_session_id is None
                or self.map_context is None
            ):
                return False

            if (
                self.alive
                and self.anchor_ns is not None
            ):
                return False

            self.alive = True
            self.anchor_ns = int(
                observed_ns
            )

            self.events = []
            self.facing_right = True
            self.life_index += 1

            target = self.target_name
            life = self.life_index

        print(
            f"[PLAYER LIFE] {target} "
            f"ALIVE -> t=0 "
            f"life={life} source={source}"
        )

        return True

    def on_death(self, source="activity"):
        with self.lock:
            if self.target_name is None:
                return False

            discarded = len(
                self.events
            )

            target = self.target_name
            life = self.life_index

            self.alive = False
            self.anchor_ns = None
            self.events = []
            self.facing_right = True

        print(
            f"[PLAYER LIFE] {target} "
            f"DEAD -> RESET t=0 "
            f"life={life} "
            f"source={source} "
            f"discardedPoints={discarded}"
        )

        return True

    def observe(
        self,
        packet,
        observed_ns=None,
    ):
        if observed_ns is None:
            observed_ns = time.perf_counter_ns()

        with self.lock:
            if (
                self.target_name is None
                or self.target_session_id is None
                or self.map_context is None
                or int(packet.session_id)
                != int(self.target_session_id)
            ):
                return None

            needs_fallback = (
                not self.alive
                or self.anchor_ns is None
            )

        if needs_fallback:
            self.on_alive(
                observed_ns,
                source="movement-fallback",
            )

        with self.lock:
            if (
                not self.alive
                or self.anchor_ns is None
            ):
                return None

            t_us = max(
                0,
                (
                    int(observed_ns)
                    - self.anchor_ns
                ) // 1000,
            )

            moving_left = bool(
                packet.moving_left
            )

            moving_right = bool(
                packet.moving_right
            )

            if (
                moving_right
                and not moving_left
            ):
                self.facing_right = True

            elif (
                moving_left
                and not moving_right
            ):
                self.facing_right = False

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
                    bool(self.facing_right),

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

            self.events.append(
                event
            )

            target = self.target_name
            life = self.life_index

        print(
            f"[WATCH POS] {target} "
            f"life={life} "
            f"{t_us / 1_000_000:.6f}s "
            f"x={event['x']:.2f} "
            f"y={event['y']:.2f} "
            f"vx={event['velocityX']:.2f} "
            f"vy={event['velocityY']:.2f} "
            f"face={'R' if event['facingRight'] else 'L'}"
        )

        return event

    def finish(
        self,
        reason,
        *,
        victory_seconds=None,
        observed_ns=None,
    ):
        if observed_ns is None:
            observed_ns = time.perf_counter_ns()

        with self.lock:
            if (
                self.target_name is None
                or self.map_context is None
                or not self.alive
                or self.anchor_ns is None
                or not self.events
            ):
                return None

            life_elapsed_seconds = max(
                0.0,
                (
                    int(observed_ns)
                    - self.anchor_ns
                )
                / 1_000_000_000.0,
            )

            record = {
                "version": 2,
                "lifeTimerVersion":
                    self.LIFE_TIMER_VERSION,

                "kind":
                    "passive-player-record",

                "targetName":
                    self.target_name,

                "targetSessionId":
                    self.target_session_id,

                "mapCode":
                    self.map_context["mapCode"],

                "mirrored":
                    self.map_context["mirrored"],

                "mapHash":
                    self.map_context["mapHash"],

                "roundId":
                    self.map_context["roundId"],

                "lifeIndex":
                    self.life_index,

                "reason":
                    str(reason),

                # This is OUR life duration and is the value used for
                # ranking/replay selection.
                "victorySeconds":
                    life_elapsed_seconds,

                "reportedVictorySeconds":
                    (
                        None
                        if victory_seconds is None
                        else float(
                            victory_seconds
                        )
                    ),

                "events":
                    copy.deepcopy(
                        self.events
                    ),
            }

            self.alive = False
            self.anchor_ns = None
            self.events = []
            self.facing_right = True

        print(
            f"[PLAYER RECORDER] SUCCESS "
            f"target={record['targetName']} "
            f"life={record['lifeIndex']} "
            f"lifeTime={record['victorySeconds']:.3f}s "
            f"serverReported="
            f"{'n/a' if victory_seconds is None else f'{float(victory_seconds):.3f}s'} "
            f"points={len(record['events'])}"
        )

        return record
