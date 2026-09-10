import asyncio

from caseus.packets import clientbound


class LocalPlayerReplayer:
    """
    Plays a PASSIVELY RECORDED remote-player route on OUR OWN CLIENT ONLY.

    IMPORTANT:
      - no serverbound movement packets
      - no backend spoof
      - no keyboard injection
      - visual/local playback only
    """

    def __init__(self):
        self.record = None
        self.task = None
        self.active = False
        self.generation = 0
        self.debug_logs = False

        print(
            "[PLAYPLAYER] local-only replayer ready"
        )

    def set_debug_logs(self, enabled):
        self.debug_logs = bool(enabled)

    def arm(self, record):
        self.record = record

        if record is None:
            print(
                "[PLAYPLAYER] no player record armed"
            )
            return False

        print(
            f"[PLAYPLAYER] ARMED "
            f"id={record.get('id')} "
            f"target={record.get('targetName')} "
            f"map={record.get('mapCode')} "
            f"points={len(record.get('events', []))}"
        )

        return True

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
            f"[PLAYPLAYER] STOP reason={reason}"
        )

    def is_active(self):
        return bool(self.active)

    async def _mirror(
        self,
        source_conn,
        event,
        self_session_id,
    ):
        x = int(round(float(
            event.get("x", 0.0)
        )))
        y = int(round(float(
            event.get("y", 0.0)
        )))
        vx = int(round(float(
            event.get("velocityX", 0.0)
        )))
        vy = int(round(float(
            event.get("velocityY", 0.0)
        )))

        await source_conn.write_packet_instance(
            clientbound.MovePlayerPacket(
                x=x,
                y=y,
                position_relative=False,
                velocity_x=vx,
                velocity_y=vy,
                velocity_relative=False,
            )
        )

        if self_session_id is not None:
            try:
                await source_conn.write_packet_instance(
                    clientbound.SetFacingPacket(
                        session_id=int(self_session_id),
                        facing_right=bool(
                            event.get(
                                "facingRight",
                                True,
                            )
                        ),
                    )
                )
            except Exception as exc:
                print(
                    "[PLAYPLAYER FACE ERROR] "
                    f"{type(exc).__name__}: {exc}"
                )

        if self.debug_logs:
            print(
                f"[PLAYPLAYER POS] "
                f"{event.get('tUs', 0) / 1_000_000:.6f}s "
                f"x={x} y={y} "
                f"face="
                f"{'R' if event.get('facingRight', True) else 'L'}"
            )

    async def _run(
        self,
        *,
        generation,
        source_conn,
        self_session_id,
    ):
        loop = asyncio.get_running_loop()
        anchor = loop.time()

        try:
            for event in self.record.get(
                "events",
                [],
            ):
                if (
                    not self.active
                    or generation != self.generation
                ):
                    return

                target = (
                    anchor
                    + int(
                        event.get(
                            "tUs",
                            0,
                        )
                    )
                    / 1_000_000.0
                )

                delay = target - loop.time()

                if delay > 0:
                    await asyncio.sleep(delay)

                if (
                    not self.active
                    or generation != self.generation
                ):
                    return

                await self._mirror(
                    source_conn,
                    event,
                    self_session_id,
                )

            self.active = False

            print(
                "[PLAYPLAYER] COMPLETE"
            )

        except asyncio.CancelledError:
            return

        except Exception as exc:
            self.active = False

            print(
                "[PLAYPLAYER ERROR] "
                f"{type(exc).__name__}: {exc}"
            )

    def start(
        self,
        *,
        source_conn,
        self_session_id,
    ):
        if self.record is None:
            print(
                "[PLAYPLAYER] START FAILED: no record"
            )
            return False

        if source_conn is None:
            print(
                "[PLAYPLAYER] START FAILED: no client connection"
            )
            return False

        events = self.record.get(
            "events",
            [],
        )

        if not events:
            print(
                "[PLAYPLAYER] START FAILED: empty record"
            )
            return False

        loop = asyncio.get_running_loop()

        self.generation += 1
        generation = self.generation

        self.active = True

        self.task = loop.create_task(
            self._run(
                generation=generation,
                source_conn=source_conn,
                self_session_id=self_session_id,
            )
        )

        print(
            f"[PLAYPLAYER] START "
            f"target={self.record.get('targetName')} "
            f"points={len(events)} "
            "mode=LOCAL_ONLY"
        )

        return True
