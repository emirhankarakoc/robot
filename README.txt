emirhankarakoc v1.15
===================

ROUND / MAP RECORD GUARD
------------------------

Changing rooms invalidates the previous map and round context. A victory is
eligible for recording only after this proxy has observed a NewRoundPacket for
the hand. For SELF records, the mapCode and roundId from the real outgoing
EnterHolePacket must exactly match the current NewRoundPacket.

Valid:

    [ENTER HOLE CONTEXT] newRoundMap=@100 victoryMap=@100 ... match=YES

Invalid runs never reach SQLite:

    [NON-NORMAL RUN SKIP] ... newRoundMap=@100 victoryMap=@999 ...
    reason=map-code-mismatch

This prevents stale/non-normal runs received around room changes from being
stored under the wrong map.

MIRRORED ROUTE FALLBACK
-----------------------

Every SELF, selected-player and first-winner record keeps the mirrored value
of the round where it was captured. Playback first looks for a record with the
same mapCode + mapHash + mirrored value.

If the matching orientation has no record but the opposite orientation does,
the opposite route is mirrored for playback automatically. The conversion
uses the current map width from <P L="..."> (default 800) and transforms:

    x
    velocityX
    movingLeft / movingRight
    facingRight
    rotation / angularVelocity

The stored record is never modified. A converted copy is used only for the
current playback. Terminal diagnostics show:

    [MIRROR FALLBACK] ...
    [MIRROR ROUTE] sourceMirrored=... targetMirrored=...

MIRRORED RECORD INVENTORY
-------------------------

Every NewRound prints two independent rows for the current exact map hash:

    [RECORD STATUS] @1351308 | hash=d00aa3857824 | MIRRORED=NO | KAYITLI | ...
    [RECORD STATUS] @1351308 | hash=d00aa3857824 | MIRRORED=YES | KAYITSIZ CURRENT

/timelist and /timelist @mapCode no longer collapse both orientations into a
single map row. For every exact mapCode+mapHash variant they print separate
MIRRORED=NO and MIRRORED=YES rows, including an explicit KAYITSIZ row when the
opposite orientation has no saved route.

Every KAYITLI row ends with an unambiguous record ID:

    ID=SELF:12
    ID=PLAYER:7

Delete only that exact record with:

    /timedelete SELF:12
    /timedelete PLAYER:7

The short form /timedelete 12 also works when that numeric ID exists in only
one table. If SELF:12 and PLAYER:12 both exist, the command refuses the
ambiguous number and tells you which full ID to use.

TRAMPOLINE + LAVA SIZE OVERRIDE (CLIENT ONLY)
---------------------------------------------

    /sismanlattrambolin on
    /sismanlattrambolin on 0.5
    /sismanlattrambolin on 0.05 0.10
    /sismanlattrambolin on 0.05 0.10 gorunmezacik
    /sismanlattrambolin gorunmezkapali
    /sismanlattrambolin off

    /sismanlatlav on
    /sismanlatlav on 0.5
    /sismanlatlav on 0.05 0.10
    /sismanlatlav on 0.05 0.10 gorunmezacik
    /sismanlatlav gorunmezkapali
    /sismanlatlav off

Syntax:

    /sismanlattrambolin on [width_px] [height_px] [gorunmezacik|gorunmezkapali]
    /sismanlatlav on [width_px] [height_px] [gorunmezacik|gorunmezkapali]

The commands increase the matching ground's L and H values in the XML sent to
the local client. Trampoline (T=2) and lava (T=3) have independent on/off and
size settings. Default width and height additions are 0.5px for each.

With one value, the same addition is used for both L and H. With two values,
the first is added to L (width) and the second to H (height). These are total
dimension additions: L=20 with width_px=0.5 becomes L=20.5. Decimal values
of any positive finite size are accepted, although very small changes may be
rounded by Flash.

gorunmezacik preserves the original visible ground and adds the enlarged
client-side physics layer as a separate m="" invisible overlay. Therefore the
original trampoline/lava artwork stays visible and only the added enlargement
is hidden. gorunmezkapali directly enlarges the original ground instead.
Visibility mode can also be changed without changing the saved size values:

    /sismanlatlav gorunmezacik
    /sismanlatlav gorunmezkapali

The setting is applied when the next map loads. It changes only the NewRound
XML sent to the local client. The original XML is still used for map hashing,
record lookup and the backend. Existing grounds are edited directly, so static
and dynamic trampoline/lava grounds are both supported.

CHAT PREFIX
-----------

All proxy-generated in-game messages use:

    [emirhankarakoc v1.15]

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

Delete one listed record by ID:

    /timedelete SELF:12
    /timedelete PLAYER:7

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

V1.6 hid raw database row IDs. RECORD-ID-DELETE-V7 brings them back as
unambiguous SELF:id / PLAYER:id references so one exact row can be deleted.


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


V1.11 - PAKETYOLLA TEST
=======================

Command:

    /paketyolla Nick#0000

Leading dot is also accepted by the existing command parser:

    ./paketyolla Nick#0000

Behavior:

    - sends ONE serverbound CommandPacket
    - command format: w Nick#0000 <payload>
    - private-message payload BODY = exactly 1024 ASCII bytes
    - no repeat loop
    - 1 second cooldown between manual invocations

Terminal:

    [PAKETYOLLA] target=Nick#0000 payloadBytes=1024 count=1

Important:
    This relies on the configured backend supporting the normal "w" private
    message command and routing it to the named connected client.

    The 1024-byte BODY is exact. The serialized network packet is a little
    larger because command text, nickname, string length/framing and packet
    headers are also present.


V1.12 - SAFE SERVER PACKET LOAD TEST
====================================

Command:

    /svpaketyolla 25

Also accepted by the existing parser:

    ./svpaketyolla 25

Guardrails:

    minimum: 1 packet
    maximum: 100 packets per run
    fixed rate: 10 packets / second
    only one test can run at a time
    no burst mode
    no infinite loop

Each packet is a small serverbound CommandPacket:

    svloadtest 1/25
    svloadtest 2/25
    ...

The backend may ignore the unknown command, but it still exercises
packet framing, decoding, and command dispatch.

Examples:

    /svpaketyolla 10
        ~1 second bounded test

    /svpaketyolla 50
        ~5 second bounded test

    /svpaketyolla 999
        automatically capped to 100

Terminal:

    [SVPAKETYOLLA] START count=50 rate=10/s
    [SVPAKETYOLLA] DONE sent=50 rateLimit=10/s


V1.13 - HOLE/SPAWN GEOMETRY REPLAY
==================================

NewRoundPacket itself carries map XML. This version parses that XML and uses:

    <T X="..." Y="..." />    = mouse hole
    <DS X="..." Y="..." />   = mouse spawn
    <P DS="m;x,y,..." />      = multiple exact mouse spawns
    <P L="..." H="..." />    = map width / height

Important:

    PlayerVictoryPacket does NOT expose x/y in the current Caseus definition.
    Victory provides the finishing session/time/place. For learned remote runs,
    the final real PlayerMovementPacket is matched to the nearest parsed <T>
    so the route knows which exact hole it was approaching.

Mirrored maps:

    effectiveX = mapWidth - rawXmlX

Route metadata added automatically:

    mapGeometry
    recordedSpawn
    targetHole
    routeFirstPoint
    routeLastRealPoint

Existing database routes are compatible. When an old route is loaded, v1.13
recomputes geometry metadata from the active NewRound XML in memory.

REPLAY START
------------

The first current self movement is compared with exact XML spawn points.
If the current and recorded route resolve to different concrete spawns, the
route is translated by the exact spawn delta. Arbitrary mid-map positions are
not used as an offset.

REPLAY FINISH
-------------

The old blind LEFT/RIGHT finish-drive is removed.

After the final REAL recorded checkpoint:

  1. The exact route target hole is already known from <T X,Y>.
  2. Natural server physics gets a 120 ms grace window.
  3. If victory has not arrived and the final checkpoint is within 180 px of
     the hole, a short 3-8 checkpoint X+Y guided segment approaches the exact
     hole coordinate.
  4. If the route end is farther than 180 px, no synthetic finish is sent.

This is intentionally distance-bounded so a bad/mismatched route cannot make
an uncontrolled cross-map correction.

NEW COMMAND
-----------

    /geometry

Shows current parsed map size, mirrored state, hole coordinates and exact
spawn coordinates in local proxy chat.

Useful terminal lines:

    [MAP GEOMETRY] width=800 height=400 holes=1 spawns=1 ...
    [HOLE] index=0 x=... y=...
    [SPAWN] index=0 x=... y=...
    [VICTORY GEOMETRY] session=... last=(x,y) hole=(x,y) distance=...
    [ROUTE SPAWN] ...
    [ROUTE HOLE] ...
    [SPAWN ALIGN] ...
    [HOLE GUIDE] last=(x,y) target=(x,y) delta=(x,y) distance=...


V1.14 - NO TELEPORT HOLE FINISH + VICTORY LATCH
=================================================

Changes from v1.13:

1) Removed geometry-driven synthetic movement.

   v1.13 could emit 3-8 interpolated absolute x/y packets from the last
   recorded point to <T X,Y>. That looked like a teleport and bypassed
   natural map physics.

   v1.14 sends ZERO hole-assist movement packets.
   Hole coordinates are diagnostics only.

   Expected end log:

       [PLAY END] last REAL checkpoint sent | ...
       [HOLE OBSERVE] last=(...) hole=(...) delta=(...) distance=... syntheticPackets=0
       [HOLE OBSERVE] natural coast only; hole geometry will not modify x/y
       [PLAY] trajectory complete; waiting for victory/death

2) Server victory is latched for the rest of the round.

   Some rooms emit a player-update Alive after victory. v1.13 interpreted
   that as a new life and replayed the same route repeatedly in one round.
   v1.14 blocks replay restart until the next NewRound.

       [PLAY] victory latched map=@... round=...; restart blocked until NewRound
       [PLAY] restart suppressed after victory ...

3) NewRound clears the victory latch normally.

The parsed <T> hole and spawn geometry remains available through /geometry.


V1.15 - CLIENT PHYSICS TAIL HANDOFF
===================================

Root cause fixed:
V1.14 kept Replayer.active=True after the final saved checkpoint.
That caused proxy_core to block the local client's own movement packets,
so "natural coast" never reached the backend.

V1.15 behavior:
- recorded trajectory packets are injected exactly as before
- at the final REAL checkpoint, injection ends
- Replayer.active becomes False
- waitingForFinish stays True to prevent accidental route restart
- real local PlayerMovementPacket packets are allowed through normally
- victory/death/new-round clears the waiting latch
- hole XML remains diagnostic only; no hole coordinate is injected

Expected end log:
[PLAY] trajectory complete; CLIENT PHYSICS HANDOFF active=False waitingForFinish=True
[TAIL PASSTHROUGH] CLIENT -> SERVER physics resumed | x=... y=...
[VICTORY] SERVER time=...
