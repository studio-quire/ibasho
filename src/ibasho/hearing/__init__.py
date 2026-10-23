"""Hearing: the viewer's sound transcribed on this machine into `heard` events (docs/hearing.md).

`settings` reads the config's [hearing] table, `capture` runs proccap.exe (audio/), `audio` cuts sound into
utterances and claps, `music` follows the parcel's stream, `ears` is the process that ties them together.
Imports of faster-whisper and PyAV stay inside the functions that need them, so the rest of Ibasho works without
the hearing extras installed.
"""
