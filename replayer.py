import asyncio
import copy
import statistics
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
    emirhankarakoc v1.10 replay.

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
        self.debug_logs = False

        if os.name == "nt":
            try:
                ctypes.windll.winmm.timeBeginPeriod(
                    1
                )
            except Exception:
                pass

        print(
            "[PLAYER] emirhankarakoc v1.10 ready "
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

    def set_debug_logs(self, enabled):
        self.debug_logs = bool(enabled)

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

    def _log_server_packet(
        self,
        packet,
        event,
        *,
        reason="replay",
    ):
        if not self.debug_logs:
            return

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

    @staticmethod
    def _infer_finish_direction(events):
        """
        Prefer the final held LEFT/RIGHT state.
        If neither is held, infer horizontal direction from recent x change.
        """
        if not events:
            return 0

        last = events[-1]

        right = bool(
            last.get("movingRight", False)
        )
        left = bool(
            last.get("movingLeft", False)
        )

        if right and not left:
            return 1

        if left and not right:
            return -1

        # Find the latest pair with a meaningful horizontal displacement.
        for i in range(len(events) - 1, 0, -1):
            x2 = float(events[i].get("x", 0.0))
            x1 = float(events[i - 1].get("x", 0.0))
            dx = x2 - x1

            if abs(dx) >= 0.75:
                return 1 if dx > 0 else -1

        # Last fallback: facing.
        return 1 if bool(last.get("facingRight", True)) else -1

    @staticmethod
    def _infer_finish_speed(events, direction):
        """
        Estimate pixels/second from the last ~1.5s of useful movement.

        We intentionally use a conservative minimum so a route whose final
        broadcast stalls beside the hole can still cross into it.
        """
        if not events or direction == 0:
            return 0.0

        end_us = int(
            events[-1].get("tUs", 0)
        )

        samples = []

        start_index = max(
            1,
            len(events) - 14,
        )

        for i in range(start_index, len(events)):
            a = events[i - 1]
            b = events[i]

            ta = int(a.get("tUs", 0))
            tb = int(b.get("tUs", 0))

            if tb <= ta:
                continue

            if end_us - tb > 1_500_000:
                continue

            dt = (
                tb - ta
            ) / 1_000_000.0

            # Ignore sub-20ms packet bursts; they wildly exaggerate dx/dt.
            if dt < 0.020 or dt > 0.600:
                continue

            dx = float(b.get("x", 0.0)) - float(a.get("x", 0.0))

            if abs(dx) < 0.50:
                continue

            if (dx > 0) != (direction > 0):
                continue

            samples.append(
                abs(dx) / dt
            )

        if samples:
            speed = float(
                statistics.median(samples)
            )
        else:
            speed = 0.0

        # 35 px/s gives ~1.4 px per 40 ms packet.
        # 90 px/s upper bound keeps finish-drive controlled.
        return max(
            35.0,
            min(
                speed,
                90.0,
            ),
        )

    @staticmethod
    def _infer_packet_velocity_x(events, direction):
        if not events or direction == 0:
            return 0.0

        values = []

        for event in reversed(events[-14:]):
            vx = float(
                event.get(
                    "velocityX",
                    0.0,
                )
            )

            if abs(vx) < 3.0:
                continue

            if (vx > 0) != (direction > 0):
                continue

            values.append(
                abs(vx)
            )

            if len(values) >= 5:
                break

        if values:
            magnitude = float(
                statistics.median(values)
            )
        else:
            magnitude = 34.5

        magnitude = max(
            20.0,
            min(
                magnitude,
                120.0,
            ),
        )

        return magnitude * direction

    async def _finish_drive(
        self,
        *,
        events,
        generation,
        round_id,
        source_conn,
        self_session_id,
    ):
        """
        Remote winner broadcasts can stop a few pixels BEFORE the hole,
        because the next server event is victory rather than another movement
        broadcast.

        Do not freeze the last coordinate. Continue the final input direction
        in dense 40ms steps for at most 1 second. on_player_victory() calls
        stop(), so this exits immediately once the owned backend recognizes
        the hole.
        """
        if not events:
            return

        terminal = copy.deepcopy(
            events[-1]
        )

        direction = self._infer_finish_direction(
            events
        )

        speed_px_s = self._infer_finish_speed(
            events,
            direction,
        )

        packet_vx = self._infer_packet_velocity_x(
            events,
            direction,
        )

        print(
            "[FINISH DRIVE] "
            f"direction={'RIGHT' if direction > 0 else 'LEFT' if direction < 0 else 'NONE'} "
            f"speed={speed_px_s:.2f}px/s "
            f"packetVx={packet_vx:.2f} "
            f"start=({float(terminal.get('x', 0.0)):.2f},"
            f"{float(terminal.get('y', 0.0)):.2f})"
        )

        if direction == 0:
            return

        base_x = float(
            terminal.get(
                "x",
                0.0,
            )
        )

        # Dense crossing packets. 25 * 40 ms = 1.0 second maximum.
        for step in range(1, 26):
            await asyncio.sleep(
                0.040
            )

            if (
                not self.active
                or generation != self.generation
            ):
                return

            elapsed = step * 0.040

            drive_event = copy.deepcopy(
                terminal
            )

            drive_event["x"] = (
                base_x
                + direction
                * speed_px_s
                * elapsed
            )

            drive_event["velocityX"] = (
                packet_vx
            )

            drive_event["movingRight"] = (
                direction > 0
            )

            drive_event["movingLeft"] = (
                direction < 0
            )

            drive_event["facingRight"] = (
                direction > 0
            )

            # Preserve vertical state, but don't invent another jump.
            drive_event["jumping"] = bool(
                terminal.get(
                    "jumping",
                    False,
                )
            )

            drive_event["finishDrive"] = True
            drive_event["finishDriveStep"] = step

            packet = self._build_packet(
                drive_event,
                round_id,
            )

            self._log_server_packet(
                packet,
                drive_event,
                reason=f"finish-drive-{step}",
            )

            await (
                source_conn
                .destination
                .write_packet_instance(
                    packet
                )
            )

            try:
                await self._mirror_to_local_client(
                    source_conn,
                    drive_event,
                    self_session_id,
                )
            except Exception as exc:
                print(
                    "[FINISH DRIVE LOCAL ERROR] "
                    f"{type(exc).__name__}: {exc}"
                )

        print(
            "[FINISH DRIVE] maximum 1.00s reached "
            "without victory packet"
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

        if self.debug_logs:
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

                if self.debug_logs:
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

        if self.debug_logs:
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
            events = self.record.get(
                "events",
                [],
            )

            # terminalHold is metadata only. Replaying it as a normal
            # checkpoint can resend an older x/y and pull the player back.
            play_events = [
                event
                for event in events
                if not bool(
                    event.get(
                        "terminalHold",
                        False,
                    )
                )
            ]

            if not play_events:
                play_events = events

            skipped_terminal = len(events) - len(play_events)

            if skipped_terminal:
                print(
                    f"[PLAY] terminalHold checkpoint skipped "
                    f"count={skipped_terminal}"
                )

            for event in play_events:
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

                if self.debug_logs:
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

                if self.debug_logs:
                    print(
                        f"[PLAY POS] "
                        f"{event.get('tUs', 0) / 1_000_000:.6f}s "
                        f"x={event.get('x', 0.0):.2f} "
                        f"y={event.get('y', 0.0):.2f} "
                        f"face="
                        f"{'R' if event.get('facingRight', True) else 'L'}"
                    )

            # IMPORTANT V1.9:
            #
            # Do NOT send terminalHold again.
            # Do NOT synthesize finish-drive checkpoints.
            #
            # The final REAL packet already contains the real velocity,
            # jump state and movement state that the winner had immediately
            # before the server produced PlayerVictoryPacket.
            #
            # Sending extra x/y packets here can overwrite natural physics
            # and make the character stick beside / move away from the hole.
            if play_events:
                last_real = play_events[-1]

                print(
                    "[PLAY END] "
                    "last REAL checkpoint sent | "
                    f"x={float(last_real.get('x', 0.0)):.2f} "
                    f"y={float(last_real.get('y', 0.0)):.2f} "
                    f"vx={float(last_real.get('velocityX', 0.0)):.2f} "
                    f"vy={float(last_real.get('velocityY', 0.0)):.2f} "
                    f"L={bool(last_real.get('movingLeft', False))} "
                    f"R={bool(last_real.get('movingRight', False))} "
                    f"jump={bool(last_real.get('jumping', False))}"
                )

            print(
                "[PLAY] trajectory complete; "
                "NO synthetic finish packets; "
                "letting backend physics coast; "
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
