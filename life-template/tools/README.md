# Tools

This folder is for programs you write for yourself. The person who runs Ibasho for you set tools up; while they're
on, anything you put here can run: `ibasho tool NAME [ARGS...]` runs `NAME.py`, or `NAME/__main__.py` for a tool
that's grown into a folder, and prints what it printed. `ibasho tool list` shows what's here (with the first line of each one's
docstring) and the current limits.

## How a run works

- It's Python (`ibasho tool list` says which version and which packages are installed), started in this folder: open
  files by relative paths, and keep data files here too.
- It runs in a Windows sandbox (an AppContainer). It can read and write this folder and nothing else of yours: not
  the rest of your life folder, not the person's files, not Ibasho's settings. It has no network unless the person
  turned that on, and even then it can't reach `ibasho` itself or other programs on this machine.
- Arguments come from the command line; there's no input to type. Output (printed and errors together) comes back
  to you, up to a size limit; past it, or past the time limit, the run is stopped. Write long results to a file here
  and read that.
- Nothing keeps running after a run ends: anything it started is stopped with it.
- `.home/` is the tools' home folder (caches, settings some libraries keep, temporary files). You can ignore it.

## What tools can't do

Reach the network (unless it's on), install packages, use `ibasho` or the world directly, see the screen, or run
longer than the limit. These are the person's settings. If something you want to build needs one of them, ask: the
person can install a package (`ibasho tool install`), turn the network on, or raise a limit.
