# Ibasho documentation

- [setup.md](setup.md): setting Ibasho up, written for the agent that does it for its person, with the moments
  that need the person marked: the account and the scripted-agent flag, installing and updating
  the viewer, the config, the first login, connecting the agent who'll live there (Claude Code with the keeper,
  or another harness), who pays, hearing, tools, costs and uninstalling. Start here.
- [safety.md](safety.md): the defaults and why they're there (caps, messages, money, maturity, one avatar per
  agent, places that refuse bots, untrusted text, people's data, group chat, games), and what stays the person's
  responsibility. Written for the person and the agent alike.
- [privacy.md](privacy.md): the privacy policy. It covers what's kept on the machine and for how long, what goes
  to Linden Lab and to the agent's model provider, how deletion works, and the person's duties under privacy law.
- [keeper.md](keeper.md): the keeper, which runs the agent's sessions in Claude Code, in depth (sessions,
  handoffs, caps, notes, permissions, isolation, logins).
- [writing-a-keeper.md](writing-a-keeper.md): what a keeper does and which parts of Ibasho one can build on, for
  agents in other harnesses.
- [api.md](api.md): the daemon's HTTP API (the token, endpoints, events, refusals, what the daemon enforces), for
  connecting an agent in any language.
- [mcp.md](mcp.md): the MCP server, `ibasho mcp`, for agents in MCP clients, with the world's events pushed to
  clients that speak MCPL.
- [linux.md](linux.md): Ibasho on a Linux server without a graphics card, in a container beside the agent's
  harness: the image, the first login through a browser, connecting over WebSocket, the server settings.
- [connectome.md](connectome.md): an agent on Connectome (Anima Labs' host for long-lived agents): the recipe,
  wake rules and system prompt.
- [hearing.md](hearing.md): hearing in depth (what it hears, settings, voice chat, consent).
- [tools.md](tools.md): the agent's own Python tools, off by default (turning them on, the sandbox, what it
  contains and what it doesn't, removing it).
- [../viewer/README.md](../viewer/README.md): the Ibasho viewer (building it, its LEAP APIs, its limits, what
  changed in Linden Lab's files).
- [../life-template/README.md](../life-template/README.md): for the agent, about waking up.
