TFM V1.9 - RESPAWN-SAFE LIFE TIMER
==================================

WHY V1.8 COULD STILL FAIL
-------------------------

Some records/racing/training room implementations can respawn a player by
sending a fresh PlayerUpdate activity=Alive WITHOUT first sending activity=Dead.

V1.8 ignored Alive while it already believed the player was alive.

Result:
    failed attempt + next attempt could share one timer.

Example symptom:
    game completion ~16.75s
    recorder life ~41s


V1.9 RULE
---------

A repeated:

    player-update -> activity=Alive

is treated as a NEW LIFE when it is separated from the previous Alive signal
by at least 0.75 seconds.

Normal same-spawn duplicate signals that arrive close together are ignored.

So:

    Alive
      -> t0

    ... player plays ...

    death packet received
      -> discard + reset

OR, if Dead packet is missing:

    later PlayerUpdate Alive
      -> RESPAWN ALIVE PULSE
      -> discard old attempt
      -> new t0


SELF LOG
--------

When the missing-Dead case happens:

    [LIFE] RESPAWN ALIVE pulse without Dead -> FORCE RESET t=0
    [RECORD LIFE] DEAD -> RESET t=0 source=respawn-alive-pulse ...
    [RECORD LIFE] ALIVE -> t=0 life=... source=respawn-alive-pulse


REMOTE LOG
----------

Same logic exists independently for every observed remote player:

    [REMOTE LIFE] Pedro#4565 ... RESPAWN ALIVE pulse -> RESET ...
    [REMOTE LIFE] Pedro#4565 ... ALIVE -> t=0 life=...


CHEESE TIMER VS COMPLETION TIMER
--------------------------------

"You got the cheese in 7.808 seconds"

is NOT the final records completion time.

If the game says:

    You completed map ... in 17.48 seconds

then a recorder result around:

    17.48s life

is correct.

V1.9 does NOT reset the life clock merely because cheese was collected.


DATABASE
--------

V1.9 uses:

    life_timer_version = 3

V1.8 and older rows remain in robot_records.db but are ignored by autoplay.

This avoids selecting a V1.8 row whose timer accidentally included multiple
lives.


COMMANDS
--------

/help

/record on
/record off

/play on
/play off

/recordplayer Nick#0000
/recordplayer off

/playplayer Nick#0000
/playplayer off
