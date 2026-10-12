"""The lookups every pipeline tool shares.

A tool puts scripts/ on sys.path from its own real path, then `from lib import cli, …`.
Stdlib only, Python 3.9 (the Macs' /usr/bin/python3), plus PyYAML where a YAML file is read.

Importing it also makes sure ffmpeg and ffprobe are findable. A process started by launchd, a
desktop app, a remote-control daemon or `ssh host cmd` inherits a bare PATH
(/usr/bin:/bin:/usr/sbin:/sbin) that no shell profile touched, so a bare `ffmpeg` fails although
Homebrew installed it. Every tool bin/tool runs imports this package before its first
subprocess, so the package-manager directories below that exist are added here, once, for the
tool and everything it starts. Appended, never prepended, and the caller's string is kept as it
was (empty entries included): a PATH the caller chose still wins. A script outside bin/tool
that calls ffmpeg gets this by importing lib, or from its own launcher's PATH.
"""
import os

# Homebrew on Apple silicon, Homebrew on Intel (and most hand installs), Linuxbrew system-wide
# and per user, then per-user installs (a static ffmpeg dropped in ~/.local/bin).
TOOL_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/home/linuxbrew/.linuxbrew/bin",
             "~/.linuxbrew/bin", "~/.local/bin")


def tool_path(path):
    """`path` with every directory of TOOL_DIRS that exists here and is not in it appended."""
    have = path.split(os.pathsep)
    add = [d for d in (os.path.expanduser(x) for x in TOOL_DIRS) if d not in have and os.path.isdir(d)]
    return os.pathsep.join([path] + add) if add else path


os.environ["PATH"] = tool_path(os.environ.get("PATH") or os.defpath)   # unset: what exec searched anyway
