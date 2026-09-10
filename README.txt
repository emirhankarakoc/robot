emirhankarakoc v1.10
===================

CHAT PREFIX
-----------

All proxy-generated in-game messages use:

    [emirhankarakoc v1.10]

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

    8.000 seconds

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

Blacklisted winners and records shorter than 8 seconds are not used.


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


V1.3 FIXES
==========

AFKFARMING:
    - exactly TWO jumps per life
    - jump #1 at life +5.00s
    - jump #2 at life +5.75s
    - sequence cannot re-arm itself after it finishes
    - synthetic jump/release is mirrored to local client, so it is visible

FINISH / HOLE:
    Remote winner movement broadcasts can end beside the hole and then the
    next packet is PlayerVictoryPacket. Repeating that final x/y freezes the
    replay next to the hole.

    V1.3 replaces frozen terminal spam with FINISH DRIVE:

      - infer LEFT/RIGHT from final held movement
      - infer recent horizontal speed
      - after nominal route end, send dense 40ms movement checkpoints
      - advance x continuously in final direction
      - mirror the same checkpoints to local client
      - maximum 1.00s
      - server PlayerVictoryPacket immediately stops the replay

Expected terminal:
    [FINISH DRIVE] direction=RIGHT speed=...
    [TX->SERVER] ... reason=finish-drive-1 ...
    [TX->CLIENT] ...
    [PLAY] STOP reason=server-victory


V1.4 FIXES
==========

AFK JUMP
--------
Exactly ONE jump per life:

    life +5.00s -> jump
    +0.10s      -> release
    DONE

The jump cannot schedule again during the same life.


FIRST RUN -> INSTANT PLAY
-------------------------
V1.3 used afk_jump_pending for two different jobs:

    1) should the AFK jump still run?
    2) are we waiting for the first learned route?

After the jump finished, afk_jump_pending became False.
Therefore a winner arriving later was saved but NOT replayed immediately.

V1.4 separates those states:

    afk_jump_pending
        only controls the one 5-second jump

    afk_waiting_for_route
        remains True for the entire recordless round
        until an eligible winner is saved

Flow:

    round starts
    no route
    afk_waiting_for_route=True

    5.00s
    one jump
    jumpPending=False
    waitingForRoute STILL True

    first eligible winner
    record saved
    [AFKFARMING] FIRST ROUTE TRIGGER ... -> PLAY NOW
    play_mode=True
    route armed
    if self is alive + server connection exists:
        replay starts immediately in the SAME round

    route activation
    afk_waiting_for_route=False


DIAGNOSTIC LOGS
----------------
At round start:

    [AFKFARMING STATE] ... waitingForRoute=True jumpPending=True

After one jump:

    [AFKFARMING] jump DONE ... count=1 | still waiting for first eligible route

When first route arrives:

    [AFKFARMING] FIRST ROUTE TRIGGER ... -> PLAY NOW
    [AFKFARMING] route activated ... started=True ...


V1.5
====

Minimum record time:
    8.000 seconds

/timelist
    Shows ALL saved map codes and their fastest usable BEST.

Example:
    @7288887 | 12.538s | Sstryss#0000 | P#211 | 43 pts
    @7680000 | 14.921s | Pedro#4565 | P#8 | 63 pts

/timelist @7680000
    Shows only that map's BEST.

Internally replay still matches:
    mapCode + mirrored + mapHash


V1.6
====

/timelist output cleaned up.

Old:
    @7680000 | 14.921s | Pedro#4565 | P#8 | 63 pts

New:
    @7680000 | 14.921s | Pedro#4565 | 63 pts

Single map:

    BEST @7680000 | 14.921s | Pedro#4565 | 63 pts

Database row IDs are no longer shown in chat/terminal timelist output.


V1.7 - CHATAFTERFIRST
=====================

Commands:

    /chatafterfirst GG
        sets message to "GG" and turns feature ON

    /chatafterfirst nice run :)
        sets the full message and turns feature ON

    /chatafterfirst off
        disables automatic message

    /chatafterfirst on
        enables the previously configured message

    /chatafterfirst
        shows current status + configured message


WHEN IT FIRES
-------------

It sends a REAL RoomMessagePacket to the backend only when ALL are true:

    feature is ON
    a message is configured
    a SAVED replay actually STARTED during this round
    our session is the FIRST finisher
    message was not already sent this round

It does NOT fire for:

    manual run
    recording/autolearn fallback without a replay start
    second / third / later place
    replay that was only armed but never started


TERMINAL LOG
------------

When a saved replay starts:

    [REPLAY ROUND FLAG] ... replayStartedThisRound=True

When first place triggers chat:

    [TX->SERVER] packet=RoomMessagePacket reason=chatafterfirst message='GG'
    [CHATAFTERFIRST] SENT map=@1234567 message='GG'


V1.8 FINAL CHECKPOINT
=====================
terminalHold is metadata only and is not sent as a normal replay packet.
Finish-drive begins from the last REAL checkpoint.
Also imports statistics to fix the runtime NameError.


V1.9 - NATURAL FINISH
=====================

The old FINISH DRIVE is disabled.

Why:
    The final REAL recorded PlayerMovementPacket already contains the
    winner's real position, velocity, jump state and movement flags.

    After that packet, the original winner's backend physics naturally
    continued until PlayerVictoryPacket.

    Synthetic finish-drive packets were repeatedly overwriting x/y and
    velocity after the real route ended. This could make the replay stick
    beside the hole or move away from it.

New flow:

    REAL checkpoint 1
    REAL checkpoint 2
    ...
    LAST REAL checkpoint
    terminalHold metadata -> NOT transmitted
    finish-drive -> NOT transmitted
    no more movement packets
    backend/client physics naturally coast
    wait for PlayerVictoryPacket or death

Expected log:

    [PLAY] terminalHold checkpoint skipped count=1
    [PLAY END] last REAL checkpoint sent | x=... y=... vx=... vy=...
    [PLAY] trajectory complete; NO synthetic finish packets; letting backend physics coast; waiting for victory/death

There should be NO:
    reason=finish-drive-1
    reason=finish-drive-2
    ...
after the recorded route ends.


V1.10 - DEBUGLOGS / LOW-CONSOLE-OVERHEAD MODE
==============================================

Default:
    DEBUGLOGS=OFF

Commands:
    /debuglogs
    /debuglogs on
    /debuglogs off

OFF suppresses high-frequency terminal output such as:
    [REC POS]
    [WATCH POS]
    [TX->SERVER] replay movement packet details
    [BACKEND WRITE OK]
    [TX->CLIENT] local movement/facing details
    [LOCAL MIRROR]
    [PLAY POS]
    [PLAYPLAYER POS]

Important lifecycle / result logs stay visible:
    NEW ROUND / MAP
    PLAY START / STOP
    RECORD armed / saved
    BEST hit / miss / overwrite
    FIRST PLACE
    VICTORY / DEATH
    AFKFARMING state
    errors

This changes logging only. Packet timing, replay data, recording,
backend forwarding and local mirroring are unchanged.
