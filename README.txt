TFM V1.11 - BEST ONLY
=====================

CORE RULE
---------

For one exact replay-compatible route key:

    mapCode + mirrored + mapHash

V1.11 keeps ONLY ONE record total.

SELF and learned PLAYER/WINNER records compete against each other.


NEW RESULT
----------

No record:
    new successful run is saved

New run is faster:
    previous SELF/PLAYER record is deleted
    new run becomes the only BEST

New run is slower or equal:
    it is NOT saved
    existing BEST stays untouched


EXAMPLES
--------

Existing:
    Pedro#4565 17.486s

New SELF:
    16.750s

Result:
    Pedro row deleted
    SELF 16.750s is the only stored clean route


Existing:
    SELF 16.750s

New winner:
    Pedro#4565 17.200s

Result:
    new Pedro run is ignored
    SELF 16.750s remains


OLD V1.10 DUPLICATES
--------------------

On startup V1.11 automatically compacts existing lifecycle-v3 data.

For every exact map route key it retains only the fastest row across:

    records
    player_records


TIMELIST
--------

Current map:

    /timelist

Specific map:

    /timelist @7680000

Output is ONE line only:

    [V1.11] BEST @7680000 | 16.750s | SELF | R#12 | 57 pts

or:

    [V1.11] BEST @7680000 | 14.921s | Pedro#4565 | P#8 | 63 pts


DELETE
------

Delete current map:

    /timedelete

Delete a specific map code:

    /timedelete @7680000

This removes both:
    SELF records
    PLAYER/winner records

for that map code, including alternate hash/mirrored variants.


Delete EVERYTHING:

    /timedelete all


PERSISTENT PLAY AFTER DELETE
----------------------------

If PLAY is ON and you delete the current map's BEST:

    active replay is stopped
    route is removed
    current map becomes "no route"

Then normal autolearn behavior applies:
    play manually
    record new successful run
    learn a winner if another player is faster


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

/timelist
/timelist @mapCode

/timedelete
/timedelete @mapCode
/timedelete all
