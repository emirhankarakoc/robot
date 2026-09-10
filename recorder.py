import copy
import threading
import time


class Recorder:
    """
    V1 recorder.

    Saves the CLIENT'S REAL outgoing PlayerMovementPacket states.

    Per checkpoint:
      - tUs
      - x / y
      - vx / vy
      - movingLeft / movingRight
      - facingRight
      - packet fields required to replay the same movement packet

    No keyboard hook.
    No interpolation.
    No fake checkpoints.
    """

    VERSION = 1

    def __init__(self):
        self.lock = threading.RLock()

        self.armed = False
        self.active = False

        self.context = None
        self.anchor_ns = None
        self.events = []

        # Preserve facing while both movement flags are false.
        self.facing_right = True

        print(
            "[RECORDER] V1 ready "
            "(real client coordinates only)"
        )

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

        if was_armed:
            print(
                f"[RECORD] OFF reason={reason}"
            )

    def on_alive(self, anchor_ns=None):
        if anchor_ns is None:
            anchor_ns = time.perf_counter_ns()

        with self.lock:
            if not self.armed:
                return False

            self.active = True
            self.anchor_ns = int(anchor_ns)
            self.events = []
            self.facing_right = True

        print(
            "[RECORD] ALIVE -> t=0 "
            "attempt started"
        )

        return True

    def on_death(self, source):
        """
        A dead attempt is NOT saved.
        Record mode stays armed so the next Alive starts a fresh attempt.
        """
        with self.lock:
            if not self.armed:
                return

            discarded = len(
                self.events
            )

            self.active = False
            self.anchor_ns = None
            self.events = []

        print(
            f"[RECORD] DEATH "
            f"source={source} "
            f"discardedPoints={discarded} "
            "waitingForNextAlive=True"
        )

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

        print(
            f"[REC POS] "
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
        finish_seconds,
    ):
        """
        Called only on our own SERVER victory packet.
        """
        with self.lock:
            if (
                not self.armed
                or not self.active
                or self.context is None
                or not self.events
            ):
                return None

            record = {
                "version":
                    self.VERSION,

                "mapCode":
                    self.context["mapCode"],

                "mirrored":
                    self.context["mirrored"],

                "mapHash":
                    self.context["mapHash"],

                "finishMs":
                    float(
                        finish_seconds
                    )
                    * 1000.0,

                "events":
                    copy.deepcopy(
                        self.events
                    ),
            }

            point_count = len(
                self.events
            )

            # Mode remains armed, but this attempt is complete.
            self.active = False
            self.anchor_ns = None
            self.events = []

        print(
            f"[RECORD] VICTORY "
            f"time={finish_seconds:.3f}s "
            f"points={point_count}"
        )

        return record
