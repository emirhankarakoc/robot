import copy
import threading
import time


class Recorder:
    """
    V1.10 SELF recorder.

    The important rule is:
        EVERY LIFE HAS ITS OWN CLOCK.

    Dead:
        discard current attempt
        anchor = None
        events = []

    Alive:
        t = 0
        fresh attempt

    If a room does not emit an Alive activity packet reliably,
    the first real movement can start a movement-fallback life.

    finishMs is NOT trusted from the server packet anymore.
    It is measured locally from our own monotonic Alive -> Victory clock.
    """

    VERSION = 3
    LIFE_TIMER_VERSION = 3

    def __init__(self):
        self.lock = threading.RLock()

        self.armed = False
        self.active = False

        self.context = None
        self.anchor_ns = None
        self.events = []
        self.life_index = 0

        self.facing_right = True
        self.debug_logs = False

        print(
            "[RECORDER] emirhankarakoc v1.2 ready "
            "(life timer resets on every death)"
        )

    def set_debug_logs(self, enabled):
        self.debug_logs = bool(enabled)

    @staticmethod
    def _enum_value(value):
        return int(
            getattr(
                value,
                "value",
                value,
            )
        )

    def arm(
        self,
        *,
        map_code,
        mirrored,
        map_hash,
        round_id,
    ):
        with self.lock:
            self.armed = True
            self.active = False
            self.anchor_ns = None
            self.events = []
            self.life_index = 0
            self.facing_right = True

            self.context = {
                "mapCode": int(map_code),
                "mirrored": bool(mirrored),
                "mapHash": str(map_hash),
                "roundId": int(round_id),
            }

        print(
            f"[RECORD] ARMED "
            f"map={map_code} "
            f"round={round_id} "
            "waitingForAlive=True"
        )

    def disarm(self, reason="off"):
        with self.lock:
            was_armed = self.armed

            self.armed = False
            self.active = False
            self.anchor_ns = None
            self.context = None
            self.events = []
            self.facing_right = True

        if was_armed:
            print(
                f"[RECORD] OFF reason={reason}"
            )

    def on_alive(
        self,
        anchor_ns=None,
        source="activity",
    ):
        if anchor_ns is None:
            anchor_ns = time.perf_counter_ns()

        with self.lock:
            if not self.armed:
                return False

            # Duplicate Alive should not restart a currently active life.
            if self.active and self.anchor_ns is not None:
                return False

            self.active = True
            self.anchor_ns = int(anchor_ns)
            self.events = []
            self.facing_right = True
            self.life_index += 1

            life_index = self.life_index

        print(
            f"[RECORD LIFE] ALIVE -> t=0 "
            f"life={life_index} source={source}"
        )

        return True

    def ensure_alive_from_movement(
        self,
        observed_ns=None,
    ):
        """
        Fallback for room modes where Alive update is late/missing.
        """
        if observed_ns is None:
            observed_ns = time.perf_counter_ns()

        with self.lock:
            if not self.armed:
                return False

            if self.active and self.anchor_ns is not None:
                return False

        return self.on_alive(
            observed_ns,
            source="movement-fallback",
        )

    def on_death(self, source):
        """
        Failed life is never kept as replay data.
        """
        with self.lock:
            if not self.armed:
                return False

            discarded = len(
                self.events
            )

            was_active = (
                self.active
                or self.anchor_ns is not None
                or discarded > 0
            )

            self.active = False
            self.anchor_ns = None
            self.events = []
            self.facing_right = True

        print(
            f"[RECORD LIFE] DEAD -> RESET t=0 "
            f"source={source} "
            f"discardedPoints={discarded} "
            "waitingForNextAlive=True"
        )

        return was_active

    def record_movement(
        self,
        packet,
        observed_ns=None,
    ):
        if observed_ns is None:
            observed_ns = time.perf_counter_ns()

        with self.lock:
            if (
                not self.armed
                or not self.active
                or self.anchor_ns is None
            ):
                return False

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

            t_us = max(
                0,
                (
                    int(observed_ns)
                    - self.anchor_ns
                ) // 1000,
            )

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
                    self._enum_value(
                        packet.entered_portal
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

            life_index = self.life_index

        if self.debug_logs:
            print(
                f"[REC POS] "
                f"life={life_index} "
                f"{t_us / 1_000_000:.6f}s "
                f"x={event['x']:.2f} "
                f"y={event['y']:.2f} "
                f"vx={event['velocityX']:.2f} "
                f"vy={event['velocityY']:.2f} "
                f"face={'R' if event['facingRight'] else 'L'}"
            )

        return True

    def finish(
        self,
        *,
        finish_seconds=None,
        observed_ns=None,
    ):
        """
        Success time is measured from OUR life anchor.

        finish_seconds from server is retained only as debug/reference data.
        """
        if observed_ns is None:
            observed_ns = time.perf_counter_ns()

        with self.lock:
            if (
                not self.armed
                or not self.active
                or self.context is None
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

            finish_us = int(
                round(
                    life_elapsed_seconds
                    * 1_000_000.0
                )
            )

            all_events = copy.deepcopy(
                self.events
            )

            # Keep trajectory timestamps only up to victory.
            # If a REAL movement packet races in after victory, use its
            # state as the final snapshot but clamp it to exact victory time.
            saved_events = [
                event
                for event in all_events
                if int(event.get("tUs", 0)) < finish_us
            ]

            trailing_events = [
                event
                for event in all_events
                if int(event.get("tUs", 0)) >= finish_us
            ]

            final_source = (
                all_events[-1]
                if all_events
                else None
            )

            if final_source is not None:
                terminal = copy.deepcopy(
                    final_source
                )
                terminal["tUs"] = finish_us
                terminal["terminalHold"] = True
                terminal["postVictoryCaptured"] = bool(
                    trailing_events
                )
                saved_events.append(
                    terminal
                )

            print(
                f"[RECORD FINAL SNAPSHOT] "
                f"finish={finish_us / 1_000_000:.6f}s "
                f"rawEvents={len(all_events)} "
                f"postVictoryPackets={len(trailing_events)} "
                f"savedEvents={len(saved_events)}"
            )

            record = {
                "version":
                    self.VERSION,

                "lifeTimerVersion":
                    self.LIFE_TIMER_VERSION,

                "mapCode":
                    self.context["mapCode"],

                "mirrored":
                    self.context["mirrored"],

                "mapHash":
                    self.context["mapHash"],

                "roundId":
                    self.context["roundId"],

                "lifeIndex":
                    self.life_index,

                "finishMs":
                    life_elapsed_seconds
                    * 1000.0,

                "reportedVictorySeconds":
                    (
                        None
                        if finish_seconds is None
                        else float(
                            finish_seconds
                        )
                    ),

                "events":
                    saved_events,
            }

            point_count = len(
                saved_events
            )

            life_index = self.life_index

            self.active = False
            self.anchor_ns = None
            self.events = []
            self.facing_right = True

        print(
            f"[RECORD] VICTORY "
            f"life={life_index} "
            f"lifeTime={life_elapsed_seconds:.3f}s "
            f"serverReported="
            f"{'n/a' if finish_seconds is None else f'{float(finish_seconds):.3f}s'} "
            f"points={point_count}"
        )

        return record
