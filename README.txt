emirhankarakoc v1.2
===================

CHAT PREFIX
-----------

All proxy-generated in-game messages use:

    [emirhankarakoc v1.2]

The old [V1.xx] prefix is gone.


ROUND-START RECORD DISPLAY
--------------------------

Every NewRound checks the current exact map identity.

If a usable BEST exists:

    ROUND @7680000 | BEST 17.486s | owner=Armagan#1300 | 57 points

If none exists:

    ROUND @7680000 | NO SAVED RUN


FIRST PLACE LOGGING
-------------------

Example:

    FIRST PLACE learned | @7680000 | Popmie#1793 | 32.270s | 86 points

Map code is included in terminal and in-game logs.


BEST-ONLY STORAGE
-----------------

Only one BEST route exists per exact:

    mapCode + mirrored + mapHash

A faster SELF or learned PLAYER route overwrites the old route.
A slower/equal route is ignored.


MINIMUM TIME
------------

Any route shorter than:

    11.000 seconds

is rejected and never becomes replay data.


OWNER
-----

New SELF records are tagged with the logged-in player name.

Change the owner label of a map's current BEST:

    /timeowner @7680000 Nick#0000


BLACKLIST
---------

Add:

    /blacklist add Nick#0000

Shortcut:

    /blacklist Nick#0000

Remove:

    /blacklist remove Nick#0000

Show:

    /blacklist list

Clear:

    /blacklist clear

Adding somebody to the blacklist immediately deletes records currently
owned by that name. Future runs from the blacklisted owner are ignored.


REPLAY TERMINAL LOGGING
-----------------------

Every movement sent by the replay prints its packet class and state:

    [TX->SERVER] packet=PlayerMovementPacket ...

Local mirror packets are also logged:

    [TX->CLIENT] packet=MovePlayerPacket ...
    [TX->CLIENT] packet=SetFacingPacket ...


VICTORY / LAST HELD MOVEMENT FIX
--------------------------------

If victory happens while LEFT/RIGHT/JUMP state is still held, there may be
no final release packet.

Successful recordings now append a terminalHold checkpoint at the exact
victory timestamp using the last movement state.

Replay also sends four short 50ms terminal-grace repeats while waiting for
server victory.

This prevents sparse recordings from ending one movement packet before the
hole recognition.


AFK FARMING
-----------

Enable:

    /afkfarming on

Disable:

    /afkfarming off

When ON:

1. If a saved route already exists at round start:
       PLAY is enabled and the route is used.

2. If no route exists:
       system waits for the first eligible winner route
       and schedules two jump pulses from the first usable self movement
       packet so the client does not remain completely idle.

3. When an eligible first-place route is learned:
       PLAY is enabled automatically
       the learned route is armed
       if we are still alive it starts immediately in the SAME hand.

Blacklisted winners and records shorter than 11 seconds are not used.


TIME COMMANDS
-------------

Show current map BEST:

    /timelist

Show another map:

    /timelist @7680000

Delete current map:

    /timedelete

Delete map:

    /timedelete @7680000

Delete all route data:

    /timedelete all


MAIN MODES
----------

    /record on
    /record off

    /play on
    /play off

    /recordplayer Nick#0000
    /recordplayer off

    /playplayer Nick#0000
    /playplayer off

    /afkfarming on
    /afkfarming off

    /help


V1.2 FIXES
==========

AFKFARMING JUMPS:
    first jump  = 5.00s after LIFE/Alive start
    second jump = 5.75s after LIFE/Alive start

FINAL MOVEMENT:
    victory opens a 150ms post-victory movement capture window
    final real serverbound movement packets are still accepted
    newest real movement state is clamped to exact victory timestamp
    saved run duration does NOT include the extra 150ms

Expected terminal diagnostics:
    [VICTORY CAPTURE] ... 150ms final-movement window OPEN
    [REC POST-VICTORY MOVEMENT] ...
    [RECORD FINAL SNAPSHOT] ... postVictoryPackets=N
    [RECORD FINALIZED] ...

Replay terminal grace:
    6 repeats x 40ms
