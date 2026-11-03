# Privacy policy

Ibasho is software you run on your own machine: the Ibasho viewer, a modified Second Life® viewer, and the `ibasho`
tooling. This page describes what it collects, stores, uses and shares, as Linden Lab's Third Party Viewer Policy
asks of every viewer. It also describes what the person running an agent takes on, because most of the data
here is other people's.

Last updated 2026-11-03. Changes are recorded in this repository's history.

## What Ibasho's authors collect: nothing

There's no Ibasho server. Neither the viewer nor the tooling sends anything to Ibasho's authors: no telemetry, no
crash reports, no accounts, no update checks, no analytics. The viewer's Help > Report Bug opens the public issue
tracker in your browser, and only what you choose to post there reaches anyone. Ibasho's authors hold no data
about you or about anyone your agent meets, so there's nothing for them to hand over or delete.

## What leaves your machine, and to whom

- **Linden Lab, or whoever runs the grid.** Everything any viewer sends: the login, the avatar's movements, chat
  and IMs, purchases, uploads, and the viewer statistics every viewer reports. Linden Lab's own
  [privacy policy](https://lindenlab.com/privacy) covers that. Ibasho doesn't change what the viewer tells the
  grid, with one exception on Linux: where the machine has no disk id (a container), the machine id sent at login
  is one made at random for the install (`install_id` in the viewer's settings folder), not the value every
  container would otherwise share. On Windows it's what any viewer on that PC sends.
- **Your agent's model provider.** This is the big one. Everything the agent perceives and does in its sessions
  goes to whoever runs the agent's model, as part of the conversation. With the keeper, that's Anthropic, through
  Claude Code, or another provider if you've set Claude Code up for one. With another harness, it's whichever
  provider that harness uses, and any other service it passes the conversation to. A model running on your own
  machine keeps it there. It includes:
  - nearby chat, IMs, group chat and conference messages, including other people's words;
  - names, usernames and avatar ids of the people around;
  - object names, descriptions, hover text and owners, notecards, and notifications (offers, group notices,
    payments);
  - pictures of what the viewer shows, which can include other people's avatars. Pictures leave out the
    viewer's interface (where chat and IM toasts show) unless the agent asks for it with `--ui`;
  - transcripts of speech, if hearing is on;
  - what the agent's own tools print, if you turned tools on ([tools.md](tools.md));
  - whatever the agent reads from its own memory folder.

  How long the provider keeps it, and what it may do with it, is set by your agreement with the provider: for
  Anthropic, its consumer or commercial terms and privacy policy, depending on how the agent is signed in. Read
  your provider's before you start.
- **Web servers.** The viewer's built-in browser loads web pages as any viewer does (profiles, search, media on
  prims and parcels), and plays the parcel's music stream from wherever the land's owner points it. With
  `hearing.follow_music` on, Ibasho fetches that stream too. `ibasho hear setup` downloads the speech model from
  Hugging Face once. After that, hearing works offline.
- **The person's browser, on Linux.** While `ibasho login` lasts, the viewer's screen is served to the person's
  own browser (through their SSH tunnel to the server) so they can type the password in. It goes nowhere else,
  and the view closes when that viewer does.

## What stays on your machine

All paths are under the install's home (`%USERPROFILE%\Ibasho` by default) unless shown otherwise. On Linux, in
the container, the install's home is `/home/ibasho/Ibasho` and the viewer's folder (`%APPDATA%\Ibasho` below) is
`/home/ibasho/.ibasho`, both in the container's volume; the saved login there is encrypted with the container's
MAC address.

| What | Where | Kept | What's in it |
|---|---|---|---|
| Event log | `data\events\` | 7 days (`data.retention_days`) | everything the daemon hears from the viewer: chat, IMs, notifications, payments, teleports, heard speech |
| Heard speech | in the event log | 2 days (`hearing.retention_days`) | text of what hearing transcribed. Audio is never saved |
| Pictures | `data\shots\` | 7 days | what the viewer showed when the agent looked |
| The viewer's own chat and IM transcripts | `%APPDATA%\Ibasho\` (the avatar's account folder) | off by default; anything written anyway ages out with the event log | the viewer's usual conversation logs. With `viewer.chat_logs = true` they're kept until you delete them. With Linden Lab's own viewer instead of Ibasho's, they stay on (the daemon reads IMs from them), in `%APPDATA%\SecondLife\` under the avatar's account name, and age out with the event log unless `chat_logs = true`. Ibasho touches only the avatar's own account folder there, never another account's, nor transcripts the viewer was told to keep elsewhere (its `InstantMessageLogPath`) |
| Keeper session logs (with the keeper) | `data\keeper\sessions\` | 7 days | the agent's words and its tool calls (which include what it said to people), not what the tools returned |
| Keeper log (with the keeper) | `data\keeper\keeper.log` | 7 days | what the keeper did (sessions, caps, restarts), and the agent's notes to you |
| Claude Code transcripts (with the keeper) | `claude\` (the agent's own Claude Code folder) | 7 days (Claude Code's `cleanupPeriodDays`, set by the keeper) | the whole session, including everything the tools returned, i.e. everything the agent perceived |
| Another harness's transcripts and logs | wherever that harness, or a keeper you wrote, keeps them | as long as it keeps them: Ibasho can't prune them, so set them to match | usually the whole session, i.e. everything the agent perceived |
| Notes between you and the agent | `data\keeper\notes.jsonl` | 7 days once delivered or read, pruned by the keeper (without it, until you delete them); a note still waiting stays until it's read | your notes and its replies |
| The agent's memory | `life\` | until the agent or you delete it | whatever the agent chose to write down: journal, places, people |
| The agent's tools, if you turned tools on | `life\tools\` | until the agent or you delete them | the programs it wrote and whatever they saved. Their temporary files over a day old are deleted before each run |
| The tools' Python | `tools-runtime\` | until `ibasho tool setup --remove` | Python and the packages you installed: nothing personal |
| Technical logs | `data\state\`, `%APPDATA%\Ibasho\logs\` | until you delete them (the daemon's and hearing's logs rotate at 1 MB) | what the software did. They're meant to hold no conversation, but can name people, places and ids |
| The saved login | `%APPDATA%\Ibasho\user_settings\bin_conf.dat` | until you delete it | the password's MD5 hash, readable by anything running as your Windows user |
| Counters | `data\state\` | rolling | today's messages and L$ spent |
| Uploads' copies | `data\state\uploads\` | about an hour | copies of the files the agent uploaded, which the viewer reads instead of the originals |
| Sessions' configs | `%LOCALAPPDATA%\Ibasho\sessions\` | 30 days | which config.toml each agent session ran with (a hash of the session id, and the path) |

The daemon prunes hourly while it runs, and the keeper prunes its own records when it starts and hourly while
it runs. A machine that's off doesn't prune, and it catches up when Ibasho next runs.

If you set `keeper.claude_config = "shared"` (meant for developing Ibasho), Claude Code's transcripts go to your
own Claude Code folder, and the keeper doesn't shorten their retention there (that would shorten your own
history too). That's one reason the separate folder is the default.

## Deleting

- **`ibasho forget WHO`** (an avatar id, or a name they've used) takes one person out of the event log: their
  words, and the agent's words to them. It also deletes the viewer's transcript of conversations with them,
  removes their lines from its nearby-chat transcript (both in the avatar's own account folder), and closes any
  open conversation with them in the viewer.
  It doesn't reach:
  - some system notices that name them, which stay until they age out;
  - pictures;
  - heard speech, which has no speaker attached (use `ibasho hear forget`);
  - the keeper's logs and notes, and Claude Code transcripts, which age out with retention, and another
    harness's records, which last as long as it keeps them;
  - the agent's own memory and tools. The agent deletes what it wrote about someone, and you can too;
  - anything already sent to the agent's model provider.
- **`ibasho hear forget`** deletes everything heard from the event log. `ibasho hear forget --last 30` deletes only
  the last 30 minutes.
- **Shorter retention:** `data.retention_days` (at least 1) and `hearing.retention_days` (fractions work: `0.5` is
  12 hours).
- **Everything:** see Uninstalling in [setup.md](setup.md). `ibasho uninstall` lists every place an install keeps
  data, the tools' sandbox among them.

## Your part, as the person running an agent

When you run an agent, you decide that other people's words, names and voices get collected on your machine and,
unless the model runs there too, sent to a model provider. Linden Lab's Scripted Agent Policy is clear that leaving its service doesn't take
personal data outside the law. Data transferred out of the Second Life service "is not exempt from protection
under applicable data privacy laws", and it names the GDPR and the CCPA. Under laws like these you're likely the
data controller, the one responsible. This isn't legal advice, and whether any exemption for purely personal
activity applies to you is a question for your own adviser. Linden Lab itself recommends asking a privacy
attorney. In practice, the duties look like this:

- **Notice.** People should be able to find out what happens to their words: that what's said near the avatar goes
  to an AI provider, how long things are kept, and how to ask to be forgotten (asking the agent works). The
  avatar's profile is where people look. The agent writes it; if you want the notice there, tell it why and leave
  the words to it.
- **A lawful basis.** You need a reason the law recognises for processing their data. Which one fits depends on
  where you are and what you do.
- **Retention.** Keep things no longer than needed. The defaults (7 days, 2 for speech) are a starting point;
  shorter is fine. With another harness, set its transcripts to age out on the same terms.
- **Deletion and access.** When someone asks to be forgotten, `ibasho forget`, `ibasho hear forget`, and the
  agent's own notes are the path. Someone may also ask what you hold about them, and the files above are where to
  look.
- **Security.** Keep the machine yours. The event log, transcripts and saved login are only as private as the
  Windows account they live in, or on Linux the server and the container's volume.
- **Transfers.** The provider may process data in another country. Its terms say where.

Hearing adds its own weight. Transcribing voice may count as recording, and in many places recording a
conversation needs everyone's consent. [hearing.md](hearing.md) has more.

## If you're a resident

If an agent running Ibasho has talked with you and you'd like to be forgotten, ask the agent, or the person
running it (the profile may say how to reach them). Ibasho's authors can't help directly, since they hold nothing
and can't reach anyone's install.
