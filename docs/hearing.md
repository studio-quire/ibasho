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

- **The viewer's own sound, and nothing else on the machine.** `proccap.exe` uses Windows' process loopback
  capture (Windows 10 version 2004 or later) on the viewer's process tree: the world's sounds, gestures, media
  on prims, the viewer's music. Not your browser, your calls, or anything else playing.
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

For the person running the agent:

1. Install the extra packages: `pip install ".[hearing]"` in the Ibasho folder (faster-whisper, which brings PyAV and
   onnxruntime, and numpy). PyAV stays below 19, which broke faster-whisper's own audio decoding.
2. Get `proccap.exe`. Build it with `audio\build.bat`, which needs Visual Studio 2022 or its Build Tools with the
   C++ workload (the same install the viewer build uses); it finds Visual Studio itself and puts the exe in
   `%IBASHO_HOME%\bin` (default `%USERPROFILE%\Ibasho\bin`), where hearing looks for it. `audio\build.bat DIR` builds
   elsewhere; then set `hearing.proccap` to the exe. The exe is statically linked, so a copy runs on machines
   without Visual Studio.
3. In the config's `[hearing]` table set `enabled = true`, and look over the rest (below).
4. Run `ibasho hear setup`: it checks Windows, proccap and the packages, downloads the model into
   `%IBASHO_HOME%\models` (small.en is about 480 MB), and checks the voice device if one is set. Hearing never
   needs the network after that, except to follow music streams.
5. `ibasho up` starts hearing once the avatar is in world; `ibasho down` stops it.

The model can also run `ibasho hear off` and `ibasho hear on` (when the config enables hearing), and
`ibasho hear status` to see what each ear is doing. In the agent's sessions, `ibasho hear on --model` takes only
faster-whisper's own models up to medium (`tiny`, `base`, `small`, `medium`, their `.en` versions,
`distil-small.en`, `distil-medium.en`): a folder, another repository's model or the large ones are the person's
to choose, in `hearing.model`.

To check the install without the viewer: `ibasho hear run --file some-recording.wav` prints what it hears.

## Settings

All in the `[hearing]` table of `config.toml`:

| setting | default | |
|---|---|---|
| `enabled` | `false` | `ibasho up` starts hearing; `ibasho hear on` works at all |
| `model` | `"small.en"` | `tiny.en` (~75 MB) and `base.en` (~145 MB) are lighter and miss more; `medium.en` (~1.5 GB) is better and much slower. Without `.en`, multilingual |
| `language` | `""` | multilingual models: a language code such as `"de"`, or empty to detect |
| `threads` | `0` | CPU threads for Whisper; 0 = half the cores, at most 4 |
| `retention_days` | `2` | heard events are deleted after this many days (fractions work: `0.5` is 12 hours) |
| `follow_music` | `false` | transcribe the parcel's music stream (a steady CPU load while one plays) |
| `mute_viewer_music` | `true` | the viewer's music volume to 0 while hearing (back up in the viewer's Preferences > Sound & Media if you use the viewer yourself) |
| `claps` | `true` | rhythm events |
| `heard_as` | `[]` | mishearings of the avatar's name that should count as the name |
| `gate_db` | `-45.0` | how loud (dBFS) a sound must be to count as an utterance |
| `voice_device` | `""` | voice chat capture, opt-in: below |
| `proccap` | `""` | the exe's path, if it isn't in `%IBASHO_HOME%\bin` or on PATH |

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

- `audio/proccap.cpp`, `audio/build.bat`: the capture helper. `proccap <pid>` captures one process tree;
  `proccap --device <id>` one output device (refusing defaults, exit code 3); `proccap --list` lists output
  devices. Raw 16-bit PCM on stdout, real-time silence included.
- `src/ibasho/hearing/`: `settings.py` (the config table), `capture.py` (runs proccap), `audio.py` (the utterance
  cutter and the clap ear), `music.py` (stream following), `ears.py` (the process: `ibasho hear run`).
- `src/ibasho/cli/ears.py`: `ibasho hear on|off|status|setup|devices|forget|run` and `ibasho clap`.
- Events go to the daemon's `POST /event`; forgetting uses `POST /forget {kinds, after}`.
- Tests: `tests/test_hearing.py`. The capture tests find proccap through `IBASHO_TEST_PROCCAP` or
  `%USERPROFILE%\Ibasho\bin`, play test sound on a spare device when the `soundcard` package is installed (else
  quietly on the speakers), and the Whisper tests download `tiny.en` once into pytest's cache.
