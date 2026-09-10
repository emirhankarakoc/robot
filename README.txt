TFM V1.8 - LIFECYCLE SAFE
===========================

MAIN FIX
--------

Every player now has a LIFE clock, not only a round clock.

For SELF and every observed REMOTE player:

    DEAD
        -> current attempt discarded
        -> anchor cleared
        -> events cleared
        -> timer reset

    ALIVE
        -> new life
        -> t = 0

    first movement without Alive packet
        -> movement-fallback Alive
        -> t = 0

This is specifically meant to work in training / records / racing-style
rooms where a player can die and respawn multiple times inside one NewRound.


SUCCESS TIME
------------

Old versions trusted:

    PlayerVictoryPacket.seconds

for DB ranking.

V1.8 does NOT trust that as the primary timer.

V1.8 measures:

    local monotonic Alive -> Victory duration

and saves that as:

    finishMs            for self records
    victorySeconds      for remote winner records

The server's packet.seconds is retained only as:

    reportedVictorySeconds

for debugging.


FAILED ATTEMPTS
---------------

Failed/dead remote lives are NOT replay data.

They are discarded.

Round-change, target-change and manual stop do not create a replayable
remote route.

Only:

    reason = victory

is stored as a clean remote route.


OLD DATABASE ROWS
-----------------

Old rows are preserved.

V1.8 adds:

    life_timer_version = 2

to both tables.

Autoplay selects ONLY lifecycle-v2 rows.

This prevents old records with non-reset timers from contaminating
records/training data.


EXPECTED SELF LOG
-----------------

Death:

    [DEATH] CLIENT -> SERVER | RESET SELF LIFE TIMER
    [RECORD LIFE] DEAD -> RESET t=0 ...

Respawn:

    [LIFE] ALIVE ...
    [RECORD LIFE] ALIVE -> t=0 life=...

If Alive packet is absent:

    [LIFE] SELF movement-fallback ALIVE -> t=0
    [RECORD LIFE] ALIVE -> t=0 ... source=movement-fallback


EXPECTED REMOTE LOG
-------------------

    [REMOTE LIFE] Pedro#4565 ... ALIVE -> t=0 life=...
    ...
    [REMOTE LIFE] Pedro#4565 ... DEAD -> RESET t=0 ...
    ...
    [REMOTE LIFE] Pedro#4565 ... ALIVE -> t=0 life=...


WINNER
------

The first finisher is still learned.

But only the winner's CURRENT life is saved.

If the winner died three times before the successful attempt, those three
failed trajectories are already discarded and do not exist inside the saved
winner route.


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


PORTS
-----

MAIN       11801
SATELLITE  12801
