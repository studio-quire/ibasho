# Hearing

Hearing turns what the avatar hears into text the model can read: the viewer's sound is captured, speech in it
is transcribed on this machine by [Whisper](https://github.com/openai/whisper) (through
[faster-whisper](https://github.com/SYSTRAN/faster-whisper), on the CPU), and each utterance becomes a `heard`
event in the event log, where `ibasho watch` and `ibasho events` see it like any other.

It turns speech into words, and that's all it gives the model. Music doesn't come through as music: no tune, no
mood, no sense of what's playing (lyrics sometimes come through, as words), and nor do other sounds, apart from
claps and knocks counted as rhythms (below). The keeper's wake prompt and `ibasho hear on` say so to the model, so
it doesn't expect to hear a concert.

It's off by default. It needs extra packages, a small native helper, a model download and a steady share of the
CPU, and what it hears is other people talking, so turning it on is the person's decision, made in the config
file. Read [Privacy and consent](#privacy-and-consent) first.

## What it hears

- **The viewer's own sound, and nothing else on the machine.** On Windows, `proccap.exe` uses Windows' process
  loopback capture (Windows 10 version 2004 or later) on the viewer's process tree: the world's sounds, gestures,
  media on prims, the viewer's music. Not your browser, your calls, or anything else playing. On Linux (the
  container, [linux.md](linux.md)) the viewer plays on a sound-server sink of its own, `ibasho_world`, and hearing
  records only that sink.
- **The parcel's music stream, if you turn it on** (`follow_music`, off by default): Ibasho fetches the stream
  itself and transcribes the talk in it in overlapping 12-second windows, as `heard_stream` events. DJs talk over
  their sets, sometimes to you; lyrics come through too, so these wake the model only when its name comes up.
  It's off by default because most clubs, shops and homes play a stream, so following it keeps Whisper busy the
  whole time the avatar is there, mostly transcribing songs. Only `http`/`https` stream URLs are followed (a
  parcel's music URL is set by whoever owns the land; local files are never read). Needs the Ibasho viewer (it
  asks the viewer for the parcel's music URL). With `mute_viewer_music` (on by default) the viewer's own music
  volume goes to 0 while hearing runs, so songs don't keep Whisper busy through the viewer's sound either.
- **Claps and knocks** (`claps`, on by default): short percussive sounds that die away at once, grouped into
  rhythms as `heard_rhythm` events. In some places a clap is a way of talking (one for "are you there?", fast
  ones for cheering, rhythms traded back and forth). `ibasho clap PATTERN` claps back in the same notation.
- **Voice chat, only if you set it up** (`voice_device`, off by default): see [Voice chat](#voice-chat).

Hearing doesn't know who spoke; the text is what was said near the avatar, nothing more. Speech recognition
mishears, especially names, and sometimes "hears" words in music or noise; the events say how loud the sound was
so the model can weigh a faint line.

## Events

| kind | data | wakes `ibasho watch` |
|---|---|---|
| `heard` | `text`, `via` (`viewer` or `voice`), `dur` (s), `db` (loudness, dBFS), `took` (s to transcribe), `me` (my name came up) | when `me`, or when it's loud (over -42 dB) and three words or more |
| `heard_stream` | `text`, `url`, `me`, `took` | only when `me` |
| `heard_rhythm` | `count`, `pattern` (`x` a clap, `.` a 125 ms rest: `x...x.x.x...x`), `intervals_ms`, `tempo` (fast, steady, slow), `db`, `ago_s`, `mine` (my own `ibasho clap`), `text` | within 5 minutes of my own clapping, or with a friend within 30 m; never my own claps |

Every event carries `wake`, decided when it's heard. In `ibasho watch` they show as `heard: ...`,
`heard_stream: ...` and `heard_rhythm: 3 claps or knocks (fast) x.x.x, ended 1.2 s ago`.

"My name" means the words of `agent.name`, `agent.nicknames`, the avatar's names in world, and
`hearing.heard_as`: the ways Whisper tends to mishear the name (it will; listen for a while and add them). No
prompt primes Whisper with the name, since that makes it hear the name in music and silence.

## Turning it on

For the person running the agent, on Windows (Linux: [below](#on-linux)):

1. Install the extra packages: `pip install ".[hearing]"` in the Ibasho folder (faster-whisper, which brings PyAV and
   onnxruntime, and numpy). PyAV stays below 19, which broke faster-whisper's own audio decoding.
2. Get `proccap.exe`. Build it with `audio\build.bat`, which needs Visual Studio 2022 or its Build Tools with the
   C++ workload (the same install the viewer build uses); it finds Visual Studio itself and puts the exe in
   `%IBASHO_HOME%\bin` (default `%USERPROFILE%\Ibasho\bin`), where hearing looks for it. `audio\build.bat DIR` builds
   elsewhere; then set `hearing.proccap` to the exe. The exe is statically linked, so a copy runs on machines
   without Visual Studio.
3. In the config's `[hearing]` table set `enabled = true`, and look over the rest (below).
4. Run `ibasho hear setup`: it checks Windows, proccap and the packages, downloads the model into
   `%IBASHO_HOME%\models` (small.en, the model on most machines, is about 480 MB), and checks the voice device if one is set. Hearing never
   needs the network after that, except to follow music streams.
5. `ibasho up` starts hearing once the avatar is in world; `ibasho down` stops it.

The model can also run `ibasho hear off` and `ibasho hear on` (when the config enables hearing), and
`ibasho hear status` to see what each ear is doing. In the agent's sessions, `ibasho hear on --model` takes only
faster-whisper's own models up to medium (`tiny`, `base`, `small`, `medium`, their `.en` versions,
`distil-small.en`, `distil-medium.en`): a folder, another repository's model or the large ones are the person's
to choose, in `hearing.model`.

To check the install without the viewer: `ibasho hear run --file some-recording.wav` prints what it hears.

### On Linux

The container ([linux.md](linux.md)) has everything hearing needs: the packages, and a sound server of its own
(PulseAudio) whose outputs are all silent sinks that nothing outside the container hears. Then:

1. In the config's `[hearing]` table set `enabled = true`, and look over the rest (the model: below).
2. `docker exec ibasho ibasho hear setup` downloads the model into the volume (the container needs the internet
   for this, once) and checks the sound server.
3. `ibasho up` starts hearing, as on Windows.

Outside the container, Linux needs PulseAudio (or PipeWire's pulse server) with `pactl` and `parec`, and a null
sink named `ibasho_world` (`pactl load-module module-null-sink sink_name=ibasho_world`) that the viewer plays on
(as its default sink, or `PULSE_SINK=ibasho_world` in `ibasho up`'s environment); `docker/pulse.pa` shows the
container's sinks. Only the container has been tested.

### How much CPU it takes on a small server

Listening costs almost nothing: the sound server, the capture and the cutter take about 0.05 of a core together,
and the viewer keeps its idle frame rate while nothing is heard. Transcribing is the cost, and only while people
talk. Measured in the container on a private test grid, 2 CPUs, utterances of 3-6 seconds, Whisper at reduced
priority with one thread:

| | tiny.en | base.en | small.en |
|---|---|---|---|
| a 5-second utterance, the viewer idle | 0.7 s | 1.3 s | 3.2 s |
| the same, the viewer drawing flat out | 0.8 s | 1.3 s | 3.4 s |
| the same on the slow-VPS stand-in (below), viewer idle | | 2.4 s (3.5 at most) | 6.4 s (7.5) |
| CPU seconds per second of speech | 0.11 | 0.18 | 0.43 |
| memory | 0.2 GB | 0.25 GB | 0.5 GB |

The test machine's cores (a Ryzen 9600X) are roughly 1.5-2 times a typical VPS's vCPUs, so the slow-VPS stand-in
gave the container one CPU's worth of time over two. While Whisper works, it takes about a third of the viewer's
frame rate when the viewer is drawing flat out (7.5 to 5 frames a second); it draws flat out only while the
agent is acting. A second thread on 2 CPUs gained nothing while the viewer was busy and cost it more frames. With 4 CPUs, small.en took 2.7 s (two threads, the viewer idle) and
about 5 s while the viewer drew flat out.

So `model` and `threads` follow the machine: on fewer than 4 CPUs base.en with one thread, since small.en there
takes longer than the speech itself and would fall behind in a lively conversation (hearing then skips
utterances rather than lag); with 4 or more, small.en, which mishears less. A container's CPU quota counts, not
the host's cores. The clean test speech came through nearly word for word with every model, so it says nothing
about how much more the smaller ones mishear real voices.

## Settings

All in the `[hearing]` table of `config.toml`:

| setting | default | |
|---|---|---|
| `enabled` | `false` | `ibasho up` starts hearing; `ibasho hear on` works at all |
| `model` | `""` | empty: `small.en` (~480 MB) with 4 CPUs or more, `base.en` (~145 MB) with fewer, where small.en can't keep up beside the viewer ([below](#how-much-cpu-it-takes-on-a-small-server)). `tiny.en` (~75 MB) is lighter and misses more; `medium.en` (~1.5 GB) is better and much slower. Without `.en`, multilingual |
| `language` | `""` | multilingual models: a language code such as `"de"`, or empty to detect |
| `threads` | `0` | CPU threads for Whisper; 0 = half the CPUs (a container's CPU quota where it has one), at most 4 |
| `retention_days` | `2` | heard events are deleted after this many days (fractions work: `0.5` is 12 hours) |
| `follow_music` | `false` | transcribe the parcel's music stream (a steady CPU load while one plays) |
| `mute_viewer_music` | `true` | the viewer's music volume to 0 while hearing (back up in the viewer's Preferences > Sound & Media if you use the viewer yourself); on a server (`[server]`), the viewer doesn't play the parcel's music stream at all |
| `claps` | `true` | rhythm events |
| `heard_as` | `[]` | mishearings of the avatar's name that should count as the name |
| `gate_db` | `-45.0` | how loud (dBFS) a sound must be to count as an utterance |
| `voice_device` | `""` | voice chat capture, opt-in: below |
| `proccap` | `""` | Windows: the exe's path, if it isn't in `%IBASHO_HOME%\bin` or on PATH |

The hearing process runs at below-normal priority and transcribes one thing at a time, speech before music. If
it falls behind (a slow CPU, a long set of music), it skips new utterances rather than lag further. Whisper
`small.en` on 4 threads takes roughly a second per short utterance on a recent desktop CPU; following a music
stream keeps it partly busy all the time.

## Voice chat

Second Life voice chat (WebRTC, inside the viewer) doesn't show up in the viewer's process sound, so hearing it
takes a different route: the viewer is told to play voice on an output device that nothing else uses, and
hearing records everything on that device. That's why it's opt-in, and why the choice of device matters.

1. Have a spare output device. A virtual audio cable driver is the usual way (VB-Audio's VB-CABLE is a free
   one); an output nobody listens to (a monitor's HDMI audio without speakers) works too. Nothing else may play
   on it.
2. `ibasho hear devices` lists output devices with their ids. Windows default outputs are marked: hearing
   refuses them, for any role (console, multimedia, communications), because recording one would capture
   everything this machine plays, your own calls included. If the device you chose later becomes a default,
   capture stops.
3. Set `voice_device` to the device's name (or a unique part of it) or its id, then `ibasho hear setup` to check.
4. Voice must be enabled in the viewer (Preferences > Sound & Media > Voice) and allowed on the parcel; many
   places turn it off.

While voice capture is on, hearing sets the viewer's voice output (`VoiceOutputAudioDevice`) to that device and
keeps it there, so you won't hear voice chat on your speakers. To undo it: empty `voice_device`, then pick your
speakers again in the viewer's voice device settings.

**On Linux** (the container), the spare device is already there: voice plays on a sink of its own,
`ibasho_voice`, which nothing else uses.

1. In the config's `[server]` table set `voice = true`: the viewer then joins voice chat. It's off on servers by
   default, since without step 2 nothing is heard (in a quiet voice channel it took no CPU we could measure; a
   channel full of talkers is untested). `ibasho up` points the viewer's voice output at `ibasho_voice`, so
   voice never mixes into the sound the viewer ear records, whatever hearing's settings.
2. In `[hearing]` set `voice_device = "ibasho_voice"`, then `ibasho hear setup` to check.

While hearing runs, it also moves any voice stream it finds on `ibasho_world` back to `ibasho_voice` (should the
viewer's voice output be changed), so voice is heard only through this opt-in. Tested on a private OpenSim grid
with WebRTC voice (OpenSim's Janus voice module): voice chat came through as `heard` events with `via: voice`.
On Second Life's own voice servers it worked the same (2026-11-03): real voices nearby came through as `heard`
lines in about 1.3 s each with small.en on 4 CPUs.

The agent hears voice the way a person in the room would; it doesn't speak in voice (Ibasho has no voice output).

## Privacy and consent

What hearing produces is the most sensitive data Ibasho handles: other people's speech, as text.

- **Where it goes.** Transcripts exist in exactly one place on this machine: the event log, for
  `hearing.retention_days` (2 days by default, shorter than the rest of the log; the daemon prunes them hourly
  while it runs). Audio is never saved. The hearing process's own files (`state/hear.log`, `state/hear.json`) say
  what it's doing, never what it heard. The model's own notes are another matter: what it chooses to write down
  in its memory stays until it deletes it.
- **Forgetting.** `ibasho hear forget` deletes everything heard from the event log; `ibasho hear forget --last 30`
  just the last 30 minutes. `ibasho forget WHO` can't find heard speech, since it has no speaker: use
  `ibasho hear forget` when someone asks.
- **The model's provider.** What the model reads is sent to whoever runs the model. Heard speech is no
  exception. The person running the agent is responsible for that.
- **Linden Lab's Community Standards** (Disclosure): "Except for the purpose of reporting abuse or any violation
  of policies to Linden Lab, the remote monitoring, posting or sharing of conversations without a participant's
  consent are prohibited." The agent is present in world as itself, hearing what anyone standing there would
  hear; it must not post or share what it heard elsewhere (logs, sites, streams, other people) without the
  speakers' consent. Saying so in the avatar's profile ("I'm an AI; I turn speech near me into text to
  understand it, and keep it two days") is a good habit.
- **The law.** In many places recording a conversation needs the consent of everyone in it, and transcribing
  voice may count as recording. Rules differ by country and state, and this isn't legal advice: if you turn on
  voice capture, find out what applies where you are.

## How it fits together

- `audio/proccap.cpp`, `audio/build.bat`: the capture helper on Windows. `proccap <pid>` captures one process
  tree; `proccap --device <id>` one output device (refusing defaults, exit code 3); `proccap --list` lists output
  devices. Raw 16-bit PCM on stdout, real-time silence included. On Linux, `parec` records a sink's monitor in the
  same format and `pactl` lists the sinks; `docker/pulse.pa` is the container's sound server.
- `src/ibasho/hearing/`: `settings.py` (the config table), `capture.py` (runs proccap or parec), `audio.py` (the utterance
  cutter and the clap ear), `music.py` (stream following), `ears.py` (the process: `ibasho hear run`).
- `src/ibasho/cli/ears.py`: `ibasho hear on|off|status|setup|devices|forget|run` and `ibasho clap`.
- Events go to the daemon's `POST /event`; forgetting uses `POST /forget {kinds, after}`.
- Tests: `tests/test_hearing.py`. The capture tests find proccap through `IBASHO_TEST_PROCCAP` or
  `%USERPROFILE%\Ibasho\bin`, play test sound on a spare device when the `soundcard` package is installed (else
  quietly on the speakers), and the Whisper tests download `tiny.en` once into pytest's cache.
