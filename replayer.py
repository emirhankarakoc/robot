import asyncio
import os
import ctypes

from caseus import enums
from caseus.packets import serverbound, clientbound
from caseus.packets.common import (
    PlayerFrictionInfo,
    PlayerRotationInfo,
)


class Replayer:
    """
    emirhankarakoc v1.2 replay.

    Sends ONLY saved PlayerMovementPacket states to the backend,
    at the SAME recorded timestamps.

    No physical keyboard injection.
    Same saved coordinate state is mirrored to our own client.
    No interpolation.
    """

    def __init__(self):
        self.record = None
        self.task = None
        self.active = False
        self.generation = 0

        if os.name == "nt":
            try:
                ctypes.windll.winmm.timeBeginPeriod(
                    1
                )
            except Exception:
                pass

        print(
            "[PLAYER] emirhankarakoc v1.2 ready "
            "(recorded coordinates -> backend + local mirror)"
        )

    def arm(self, record):
        self.record = record

        if record is None:
            print(
                "[PLAY] no record armed"
            )
            return False

        print(
            f"[PLAY] ARMED "
            f"id={record.get('id')} "
            f"map={record.get('mapCode')} "
            f"points={len(record.get('events', []))}"
        )

        return True

    def is_active(self):
        return bool(
            self.active
        )

    def stop(self, reason="stop"):
        self.generation += 1
        self.active = False

        task = self.task
        self.task = None

        if (
            task is not None
            and not task.done()
        ):
            task.cancel()

        print(
            f"[PLAY] STOP reason={reason}"
        )

    @staticmethod
    def _build_packet(
        event,
        round_id,
    ):
        rotation_data = event.get(
            "rotationInfo"
        )

        rotation = None

        if isinstance(
            rotation_data,
            dict,
        ):
            rotation = PlayerRotationInfo(
                rotation=float(
                    rotation_data.get(
                        "rotation",
                        0.0,
                    )
                ),

                angular_velocity=float(
                    rotation_data.get(
                        "angularVelocity",
                        0.0,
                    )
                ),

                fixed_rotation=bool(
                    rotation_data.get(
                        "fixedRotation",
                        False,
                    )
                ),
            )

        return serverbound.PlayerMovementPacket(
            round_id=int(
                round_id
            ),

            moving_right=bool(
                event.get(
                    "movingRight",
                    False,
                )
            ),

            moving_left=bool(
                event.get(
                    "movingLeft",
                    False,
                )
            ),

            x=float(
                event.get(
                    "x",
                    0.0,
                )
            ),

            y=float(
                event.get(
                    "y",
                    0.0,
                )
            ),

            velocity_x=float(
                event.get(
                    "velocityX",
                    0.0,
                )
            ),

            velocity_y=float(
                event.get(
                    "velocityY",
                    0.0,
                )
            ),

            friction_info=
                PlayerFrictionInfo(
                    charge=float(
                        event.get(
                            "frictionCharge",
                            0.0,
                        )
                    ),

                    loss_rate=float(
                        event.get(
                            "frictionLossRate",
                            0.0,
                        )
                    ),
                ),

            jumping=bool(
                event.get(
                    "jumping",
                    False,
                )
            ),

            jumping_frame_index=int(
                event.get(
                    "jumpingFrameIndex",
                    0,
                )
            ),

            entered_portal=
                enums.Portal(
                    int(
                        event.get(
                            "enteredPortal",
                            0,
                        )
                    )
                ),

            rotation_info=rotation,
        )

    @staticmethod
    def _log_server_packet(
        packet,
        event,
        *,
        reason="replay",
    ):
        print(
            "[TX->SERVER] "
            f"packet={type(packet).__name__} "
            f"reason={reason} "
            f"t={int(event.get('tUs', 0)) / 1_000_000:.6f}s "
            f"round={getattr(packet, 'round_id', '?')} "
            f"x={float(event.get('x', 0.0)):.2f} "
            f"y={float(event.get('y', 0.0)):.2f} "
            f"vx={float(event.get('velocityX', 0.0)):.2f} "
            f"vy={float(event.get('velocityY', 0.0)):.2f} "
            f"L={bool(event.get('movingLeft', False))} "
            f"R={bool(event.get('movingRight', False))} "
            f"jump={bool(event.get('jumping', False))} "
            f"terminalHold={bool(event.get('terminalHold', False))}"
        )

    async def _mirror_to_local_client(
        self,
        source_conn,
        event,
        self_session_id,
    ):
        """
        LOCAL DISPLAY ONLY.

        Backend still receives the saved serverbound movement packet.

        This sends the SAME recorded x/y/vx/vy to our own connected client so
        we can actually see the replay even when the backend does not echo the
        sender's movement back to the sender.
        """
        # Caseus clientbound.MovePlayerPacket serializes these as integer
        # fields. V1.1 passed floats here, causing:
        #   TypeError: unsupported operand type(s) for &: 'float' and 'int'
        x = int(round(float(
            event.get(
                "x",
                0.0,
            )
        )))

        y = int(round(float(
            event.get(
                "y",
                0.0,
            )
        )))

        vx = int(round(float(
            event.get(
                "velocityX",
                0.0,
            )
        )))

        vy = int(round(float(
            event.get(
                "velocityY",
                0.0,
            )
        )))

        local_move = clientbound.MovePlayerPacket(
            x=x,
            y=y,
            position_relative=False,
            velocity_x=vx,
            velocity_y=vy,
            velocity_relative=False,
        )

        print(
            "[TX->CLIENT] "
            f"packet={type(local_move).__name__} "
            f"x={x} y={y} vx={vx} vy={vy}"
        )

        await source_conn.write_packet_instance(
            local_move
        )

        if self_session_id is not None:
            try:
                face_packet = clientbound.SetFacingPacket(
                    session_id=int(
                        self_session_id
                    ),
                    facing_right=bool(
                        event.get(
                            "facingRight",
                            True,
                        )
                    ),
                )

                print(
                    "[TX->CLIENT] "
                    f"packet={type(face_packet).__name__} "
                    f"session={self_session_id} "
                    f"facingRight={bool(event.get('facingRight', True))}"
                )

                await source_conn.write_packet_instance(
                    face_packet
                )
            except Exception as exc:
                print(
                    "[LOCAL FACE ERROR] "
                    f"{type(exc).__name__}: {exc}"
                )

        print(
            f"[LOCAL MIRROR] "
            f"{event.get('tUs', 0) / 1_000_000:.6f}s "
            f"x={x:.2f} "
            f"y={y:.2f} "
            f"face="
            f"{'R' if event.get('facingRight', True) else 'L'}"
        )

    async def _run(
        self,
        *,
        generation,
        anchor_time,
        round_id,
        source_conn,
        self_session_id,
    ):
        loop = asyncio.get_running_loop()

        try:
            for event in self.record.get(
                "events",
                [],
            ):
                if (
                    not self.active
                    or generation
                    != self.generation
                ):
                    return

                target = (
                    float(anchor_time)
                    + int(
                        event.get(
                            "tUs",
                            0,
                        )
                    )
                    / 1_000_000.0
                )

                delay = (
                    target
                    - loop.time()
                )

                if delay > 0:
                    await asyncio.sleep(
                        delay
                    )

                if (
                    not self.active
                    or generation
                    != self.generation
                ):
                    return

                packet = self._build_packet(
                    event,
                    round_id,
                )

                self._log_server_packet(
                    packet,
                    event,
                    reason="replay",
                )

                await (
                    source_conn
                    .destination
                    .write_packet_instance(
                        packet
                    )
                )

                print(
                    f"[BACKEND WRITE OK] "
                    f"{event.get('tUs', 0) / 1_000_000:.6f}s "
                    f"x={event.get('x', 0.0):.2f} "
                    f"y={event.get('y', 0.0):.2f}"
                )

                # Local display must never kill backend replay.
                try:
                    await self._mirror_to_local_client(
                        source_conn,
                        event,
                        self_session_id,
                    )
                except Exception as exc:
                    print(
                        "[LOCAL MIRROR ERROR] "
                        f"{type(exc).__name__}: {exc}"
                    )

                print(
                    f"[PLAY POS] "
                    f"{event.get('tUs', 0) / 1_000_000:.6f}s "
                    f"x={event.get('x', 0.0):.2f} "
                    f"y={event.get('y', 0.0):.2f} "
                    f"face="
                    f"{'R' if event.get('facingRight', True) else 'L'}"
                )

            # Victory can occur while the final movement key is still held.
            # Repeat the terminal state briefly so a sparse trajectory does
            # not stop one packet before the server recognizes the hole.
            events = self.record.get(
                "events",
                [],
            )

            if (
                events
                and bool(events[-1].get("terminalHold", False))
            ):
                terminal_event = events[-1]

                for repeat_index in range(6):
                    await asyncio.sleep(0.04)

                    if (
                        not self.active
                        or generation != self.generation
                    ):
                        return

                    packet = self._build_packet(
                        terminal_event,
                        round_id,
                    )

                    self._log_server_packet(
                        packet,
                        terminal_event,
                        reason=f"terminal-grace-{repeat_index + 1}",
                    )

                    await (
                        source_conn
                        .destination
                        .write_packet_instance(
                            packet
                        )
                    )

            print(
                "[PLAY] trajectory complete; "
                "waiting for victory/death"
            )

        except asyncio.CancelledError:
            return

        except Exception as exc:
            print(
                f"[PLAY ERROR] "
                f"{type(exc).__name__}: {exc}"
            )

            self.active = False

    def start(
        self,
        *,
        round_id,
        source_conn,
        self_session_id=None,
    ):
        if self.record is None:
            print(
                "[PLAY] START FAILED: no record"
            )
            return False

        if (
            source_conn is None
            or source_conn.destination is None
        ):
            print(
                "[PLAY] START FAILED: no backend connection"
            )
            return False

        events = self.record.get(
            "events",
            []
        )

        if not events:
            print(
                "[PLAY] START FAILED: empty record"
            )
            return False

        loop = asyncio.get_running_loop()

        self.generation += 1
        generation = self.generation

        self.active = True

        anchor_time = (
            loop.time()
        )

        self.task = loop.create_task(
            self._run(
                generation=generation,
                anchor_time=anchor_time,
                round_id=int(round_id),
                source_conn=source_conn,
                self_session_id=self_session_id,
            )
        )

        print(
            f"[PLAY] START "
            f"id={self.record.get('id')} "
            f"round={round_id} "
            f"points={len(events)}"
        )

        return True
