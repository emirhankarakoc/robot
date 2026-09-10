TFM V1 - SIMPLE RECORD / PLAY
=============================

THIS IS A CLEAN REWRITE.

No V25/V28/V31 feature stack.
No keyboard recorder.
No local mirror.
No FastAPI.
No HTTP.
No 8787.
No player tracker.
No command manager.
No robot controller.

FILES
-----

main.py
proxy_core.py
record_store.py
recorder.py
replayer.py
requirements.txt
START_V1.bat


PORTS
-----

MAIN:
    11801

SATELLITE:
    12801

Backend:
    supplied dynamically by the normal Caseus Proxy Loader


COMMANDS
--------

/record on
/record off

/play on
/play off


RECORD FLOW
-----------

/record on

The current map is armed.

The next SERVER Alive state starts t=0.

Every REAL outgoing:

    serverbound.PlayerMovementPacket

is copied into memory.

For every checkpoint V1 stores:

    tUs
    x
    y
    velocityX
    velocityY
    movingLeft
    movingRight
    facingRight

plus only the packet fields necessary to replay the same movement packet.

The ORIGINAL client movement packet is still forwarded normally to the
backend while recording.


SUCCESS
-------

Only our own:

    clientbound.PlayerVictoryPacket

saves the attempt.

The record is written directly to:

    robot_records.db

There is no API server.


DEATH WHILE RECORDING
---------------------

Client death:

    [DEATH] CLIENT -> SERVER
    [RECORD] DEATH ...

The current attempt is discarded.

Record mode stays ON.

The next SERVER Alive starts a fresh attempt at t=0.


PLAY FLOW
---------

/play on

V1 loads the fastest successful record from robot_records.db for the exact:

    mapCode
    mirrored
    mapHash

The next SERVER Alive arms playback.

The first normal client movement after Alive provides the live backend
ClientConnection and starts playback at t=0.

During playback:

    live client PlayerMovementPacket = BLOCKED

Saved coordinate packets are sent to:

    source_conn.destination

at the same recorded timestamps.


DEATH WHILE PLAYING
-------------------

Because live client movement is blocked during PLAY, local client physics can
be stale.

Therefore a locally generated PlayerDiedPacket is ignored while replay is
active.

Real replay death is taken from the SERVER player activity state:

    player-list Dead
    or
    player-update Dead

Then replay stops.


VICTORY WHILE PLAYING
---------------------

Our own SERVER victory packet stops playback.


DATABASE
--------

robot_records.db is created automatically beside main.py.

Table:

    records

Fields:
    id
    map_code
    mirrored
    map_hash
    finish_ms
    event_count
    payload_json
    created_at


START
-----

Double click:

    START_V1.bat

or:

    py -3.11 main.py


IMPORTANT
---------

V1 intentionally does NOT add:
    interpolation
    dense fake checkpoints
    keyboard replay
    local teleport/mirror
    FastAPI
    HTTP
    old /auto command
    old /robot command
    old /izle command

The only goal is:

    RECORD client's real movement coordinates
    SAVE successful run
    PLAY the same movement coordinates back to backend


V1.1 LOCAL MIRROR
-----------------

Some backends broadcast a player's movement to OTHER players but do not echo
that same movement back to the originating player.

In that case playback can be reaching the backend correctly while your own
client appears stationary.

V1.1 fixes only the local display:

    saved checkpoint
        -> serverbound PlayerMovementPacket to backend
        -> clientbound MovePlayerPacket to our own client

The local mirror uses the exact same:
    x
    y
    velocityX
    velocityY

Facing direction is also mirrored with SetFacingPacket when the login
session id is available.

This does not add interpolation and does not create extra backend checkpoints.


V1.2 FIX
--------

V1.1 local mirror passed floating-point x/y/vx/vy values to:

    clientbound.MovePlayerPacket

Caseus expects integer fields there. That caused:

    TypeError: unsupported operand type(s) for &: 'float' and 'int'

V1.2 converts local mirror values with:

    int(round(...))

before constructing MovePlayerPacket.

The backend PlayerMovementPacket replay is unchanged.

V1.2 also isolates local display errors:
    backend write succeeds first
    local mirror is attempted second
    a local mirror failure no longer stops the replay loop

Useful logs:

    [BACKEND WRITE OK]
    [LOCAL MIRROR]
    [PLAY POS]

If local display still fails:

    [LOCAL MIRROR ERROR] ...

but backend replay continues.
