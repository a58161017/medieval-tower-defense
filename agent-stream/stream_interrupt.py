"""Copy this file into your game's MCP server to make long actions stop early when the host
speaks on stream (so the streamer can answer right away, like walk_to's "viewer_spoke").

    from stream_interrupt import viewer_pending
    for step in path:
        if viewer_pending():
            return {"ok": False, "reason": "viewer_spoke"}
        ...

It asks the stream server (agent-live-steaming-game) at GET /api/interrupt; when there is no
stream server (playing without streaming) it simply says False. Standard library only."""
import json
import os
import urllib.request

PORT = int(os.environ.get("AGENT_STREAM_HTTP_PORT", 8766))


def viewer_pending(timeout=0.3):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/interrupt", timeout=timeout) as r:
            return bool(json.load(r).get("viewer_pending"))
    except (OSError, ValueError):   # no stream server running: nothing to interrupt for
        return False
