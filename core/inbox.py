"""Host-decoupled clip inbox.

Any other Wan2GP tab can hand a clip to Reel2Reel without importing the plugin
class (which would couple it to the host and risk circular imports). The plugins
directory is on ``sys.path``, so a sender just does::

    from reel2reel.inbox import enqueue_clips
    enqueue_clips(state, "/abs/path/to/clip.mp4")
    return gr.Tabs(selected="plugin_Reel2Reel")   # navigate; on outputs=[main_tabs]

IMPORT CONTRACT: ``from reel2reel.inbox import enqueue_clips`` requires the host to
expose this plugin dir importable as ``reel2reel`` — the on-disk hyphenated folder
name ('Reel2Reel-Wan2GP') is NOT a valid module name, so senders depend on the
host's sys.path setup aliasing it to ``reel2reel``.

The clips are stashed in the shared per-session ``state`` dict. Reel2Reel's
``on_tab_select(state)`` calls :func:`drain` on every tab entry and drops the
queued clips onto the live timeline — no button press required.

This module imports nothing but the standard library on purpose.
"""
from __future__ import annotations

import threading
from typing import Any

INBOX_KEY = "reel2reel_inbox"
_MAX_INBOX = 500                 # cap the queue so it can't grow unbounded
_lock = threading.Lock()         # guard the read-modify-write under Gradio's threaded queue


def _as_list(paths) -> list[str]:
    if paths is None:
        return []
    if isinstance(paths, (str, bytes)):
        return [str(paths)]
    try:
        return [str(p) for p in paths if p]
    except TypeError:
        return [str(paths)]


def enqueue_clips(state: Any, paths) -> list[str]:
    """Append one path or many to the inbox. ``state`` is the Wan2GP per-session
    state dict. Returns the current queue. Safe to call from any tab."""
    if not isinstance(state, dict):
        return []
    with _lock:
        box = list(state.get(INBOX_KEY) or [])
        box.extend(_as_list(paths))
        if len(box) > _MAX_INBOX:               # drop oldest entries when over the cap
            box = box[-_MAX_INBOX:]
        state[INBOX_KEY] = box
        return list(box)


def peek(state: Any) -> list[str]:
    if not isinstance(state, dict):
        return []
    with _lock:
        return list(state.get(INBOX_KEY, []))


def drain(state: Any) -> list[str]:
    """Return and clear the queued clips (atomic swap under the lock so a concurrent
    enqueue can't lose clips)."""
    if not isinstance(state, dict):
        return []
    with _lock:
        box = state.get(INBOX_KEY) or []
        state[INBOX_KEY] = []
        return list(box)
