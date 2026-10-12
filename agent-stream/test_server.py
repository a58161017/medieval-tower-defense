"""Drive the MCP server like Claude Code does (stdio client), with a headless Edge and a
throw-away browser profile. Run: uv run pytest -q"""
import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters

HERE = Path(__file__).resolve().parent


def fake_stream_server(pending):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"viewer_pending": pending["value"]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


async def call(c, name, args=None):
    r = await c.call_tool(name, args or {})
    assert not r.is_error, r.content
    return json.loads(r.content[0].text), [p for p in r.content[1:] if p.type == "image"]


def test_play_level_one(tmp_path):
    pending = {"value": False}
    srv = fake_stream_server(pending)
    env = dict(os.environ, TD_HEADLESS="1", TD_PROFILE=str(tmp_path / "profile"),
               AGENT_STREAM_HTTP_PORT=str(srv.server_address[1]), PYTHONIOENCODING="utf-8")

    async def run():
        params = StdioServerParameters(command=sys.executable, args=["server.py"], cwd=str(HERE), env=env)
        async with Client(params) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert {"observe", "choose_level", "build", "start_wave", "watch"} <= names

            menu, imgs = await call(c, "observe")
            assert menu["state"]["screen"] == "menu" and len(menu["state"]["levels"]) == 8 and imgs

            lvl, _ = await call(c, "choose_level", {"level_id": 1})
            st = lvl["state"]
            assert lvl["ok"] and st["screen"] == "game" and st["level"]["name"] == "草原之路"
            assert st["gold"] == 200 and len(st["map"]) == 13 and len(st["map"][0]) == 20
            assert "=" in "".join(st["map"]) and st["build_spots"]

            x, y = st["build_spots"][0]
            built, _ = await call(c, "build", {"tower_type": "archer", "x": x, "y": y})
            assert built["ok"] and built["state"]["gold"] == 150
            assert built["state"]["map"][y][x] == "A"
            road = next((cx, cy) for cy, row in enumerate(st["map"]) for cx, ch in enumerate(row) if ch == "=")
            bad, _ = await call(c, "build", {"tower_type": "archer", "x": road[0], "y": road[1]})
            assert bad["ok"] is False and "road" in bad["error"]
            nope, _ = await call(c, "build", {"tower_type": "dragon", "x": 0, "y": 0})
            assert nope["ok"] is False

            up, _ = await call(c, "upgrade", {"x": x, "y": y})
            assert up["ok"] and up["level"] == 2

            started, _ = await call(c, "start_wave")
            assert started["ok"] and started["state"]["wave_active"]
            await call(c, "set_speed", {"speed": 3})
            watched, imgs = await call(c, "watch", {"seconds": 20})
            assert watched["stopped"] in ("wave_cleared", "time_up", "castle_hit") and imgs
            assert watched["kills"] >= 1

            pending["value"] = True
            stopped, _ = await call(c, "watch", {"seconds": 5, "screenshot": False})
            assert stopped["stopped"] == "viewer_spoke"
            pending["value"] = False

            skill, _ = await call(c, "use_skill", {"skill": "gold"})
            assert skill["ok"]
            again, _ = await call(c, "use_skill", {"skill": "gold"})
            assert again["ok"] is False and "cooling" in again["error"]

    try:
        asyncio.run(run())
    finally:
        srv.shutdown()
