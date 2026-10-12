"""中世紀守塔 (Kingdom Defense) as an MCP server, so an AI streamer can play game.html.

The game runs unmodified in a real Microsoft Edge window (app mode, so OBS can capture it by
title) driven through Playwright. The page already exposes everything needed: the game's own
globals (state, towers, LEVELS, placeTower, startWave, useSkill ...) and the helpers of its
built-in OllamaAI player (summarizeEnemies, getBuildableSpotsNearPath).

Tools: observe, choose_level, build, upgrade, sell, start_wave, use_skill, set_speed, watch,
retry, back_to_levels. Progress (stars, gems, upgrades) lives in the game's localStorage, kept
in a persistent browser profile (profile/), so it carries over between streams.

env: TD_HEADLESS=1 (tests), TD_PROFILE=<dir>, TD_GAME=<path to game.html>,
     AGENT_STREAM_HTTP_PORT (where stream_interrupt asks whether the host is speaking)."""
import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from mcp.server.mcpserver import Image, MCPServer
from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stream_interrupt import viewer_pending  # noqa: E402

HERE = Path(__file__).resolve().parent
GAME = Path(os.environ.get("TD_GAME", HERE.parent / "game.html")).resolve()
PROFILE = Path(os.environ.get("TD_PROFILE", HERE / "profile"))
HEADLESS = os.environ.get("TD_HEADLESS", "0") == "1"
WINDOW = (1480, 1000)
WATCH_MAX_S = 20
TOWER_LETTER = {"archer": "A", "ice": "I", "cannon": "C", "magic": "M", "poison": "P", "lightning": "L", "support": "S"}

S = {"page": None, "ctx": None}

SNAPSHOT_JS = """(letters) => {
  const cls = (id, c) => document.getElementById(id).classList.contains(c);
  const screen = cls('gameOverlay', 'active') ? 'result' : cls('gameScreen', 'active') ? 'game'
    : cls('levelScreen', 'active') ? 'levels' : !cls('menuScreen', 'hidden') ? 'menu' : 'other';
  const out = { screen, gems: saveData.gems,
    levels: LEVELS.map(l => ({ id: l.id, name: l.name, difficulty: l.diffName, waves: l.waves,
                               stars: saveData.levelStars[l.id] || 0, two_paths: !!l.path2 })) };
  if ((screen === 'game' || screen === 'result') && state) {
    Object.assign(out, { level: { id: currentLevel.id, name: currentLevel.name },
      wave: state.wave, max_wave: state.maxWave, wave_active: state.waveActive, gold: state.gold,
      hp: state.hp, max_hp: state.maxHp, kills: state.kills, score: state.score, speed: gameSpeed,
      game_over: state.gameOver, enemies: OllamaAI.summarizeEnemies(),
      enemies_still_coming: state.spawnQueue.length });
    out.towers = towers.map(t => ({ type: t.type, level: t.level, x: t.x, y: t.y,
      upgrade_cost: t.level < 4 ? TOWERS[t.type].upgCost[t.level - 1] : null }));
    out.build_spots = OllamaAI.getBuildableSpotsNearPath(16).map(s => [s.x, s.y]);
    out.affordable = Object.entries(TOWERS).filter(([k, c]) => state.gold >= c.cost).map(([k]) => k);
    const now = Date.now();
    out.skill_cooldown_s = Object.fromEntries(Object.keys(SKILLS).map(k =>
      [k, Math.max(0, Math.ceil((skillCooldowns[k] - now) / 1000))]));
    out.map = grid.map(row => row.map(c => c.tower ? letters[c.tower.type]
      : c.type === 'path' ? '=' : c.terrain === 'swamp' ? '~' : c.terrain === 'lava' ? '^' : '.').join(''));
    out.entrances = paths.map(p => p[0]);
    out.castle = paths[0][paths[0].length - 1];
  }
  if (screen === 'result') out.result = { title: document.getElementById('modalTitle').textContent,
    stars: document.getElementById('starRating').textContent, gems: document.getElementById('finalGems').textContent };
  return out;
}"""

ACTION_JS = """([action, a]) => {
  const inLevel = () => document.getElementById('gameScreen').classList.contains('active') && state && !state.gameOver;
  const towerAt = (x, y) => towers.find(t => t.x === x && t.y === y);
  switch (action) {
    case 'choose_level': {
      const level = LEVELS.find(l => l.id === a.level_id);
      if (!level) return { ok: false, error: `no level ${a.level_id}; levels are 1-${LEVELS.length}` };
      if (inLevel() && !a.force) return { ok: false, error: 'a level is being played; finish it or use force' };
      document.getElementById('gameOverlay').classList.remove('active');
      showLevelSelect(); startLevel(level);
      return { ok: true };
    }
    case 'build': {
      if (!inLevel()) return { ok: false, error: 'not in a level' };
      const cfg = TOWERS[a.tower_type];
      if (!cfg) return { ok: false, error: `unknown tower; one of ${Object.keys(TOWERS).join(', ')}` };
      const cell = grid[a.y] && grid[a.y][a.x];
      if (!cell) return { ok: false, error: 'outside the map (x 0-19, y 0-12)' };
      if (cell.type === 'path') return { ok: false, error: 'that square is the road' };
      if (cell.tower) return { ok: false, error: 'a tower is already there' };
      if (state.gold < cfg.cost) return { ok: false, error: `need ${cfg.cost} gold, have ${state.gold}` };
      state.selectedType = a.tower_type; placeTower(a.x, a.y);
      return towerAt(a.x, a.y) ? { ok: true, built: cfg.name, gold_left: state.gold } : { ok: false, error: 'not built' };
    }
    case 'upgrade': {
      if (!inLevel()) return { ok: false, error: 'not in a level' };
      const t = towerAt(a.x, a.y);
      if (!t) return { ok: false, error: 'no tower there' };
      if (t.level >= 4) return { ok: false, error: 'already max level 4' };
      const cost = TOWERS[t.type].upgCost[t.level - 1];
      if (state.gold < cost) return { ok: false, error: `need ${cost} gold, have ${state.gold}` };
      selectTower(t); upgradeTower();
      return { ok: true, level: t.level, gold_left: state.gold };
    }
    case 'sell': {
      if (!inLevel()) return { ok: false, error: 'not in a level' };
      const t = towerAt(a.x, a.y);
      if (!t) return { ok: false, error: 'no tower there' };
      const before = state.gold; selectTower(t); sellTower();
      return { ok: true, refund: state.gold - before };
    }
    case 'start_wave': {
      if (!inLevel()) return { ok: false, error: 'not in a level' };
      if (state.waveActive) return { ok: false, error: 'a wave is already running' };
      startWave(); return { ok: true, wave: state.wave };
    }
    case 'use_skill': {
      if (!inLevel()) return { ok: false, error: 'not in a level' };
      if (!SKILLS[a.skill]) return { ok: false, error: 'skill: meteor, freeze or gold' };
      const left = skillCooldowns[a.skill] - Date.now();
      if (left > 0) return { ok: false, error: `cooling down, ${Math.ceil(left / 1000)} s left` };
      if (!state.waveActive && a.skill !== 'gold') return { ok: false, error: 'only during a wave' };
      useSkill(a.skill); return { ok: true, used: SKILLS[a.skill].name };
    }
    case 'set_speed': {
      if (![1, 2, 3].includes(a.speed)) return { ok: false, error: 'speed: 1, 2 or 3' };
      gameSpeed = a.speed; document.getElementById('speedBtn').textContent = gameSpeed + 'x';
      return { ok: true, speed: gameSpeed };
    }
    case 'retry': retryLevel(); return { ok: true };
    case 'back_to_levels': backToLevels(); return { ok: true };
  }
  return { ok: false, error: 'unknown action' };
}"""


async def _open_page():
    pw = await async_playwright().start()
    url = GAME.as_uri()
    args = [f"--window-size={WINDOW[0]},{WINDOW[1]}", "--autoplay-policy=no-user-gesture-required",
            # the game loop runs on requestAnimationFrame: keep it running when the window is covered
            "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
            "--disable-background-timer-throttling"]
    if not HEADLESS:
        args.append(f"--app={url}")   # no tabs/address bar; window title = the page title (OBS matches it)
    PROFILE.mkdir(parents=True, exist_ok=True)
    ctx = await pw.chromium.launch_persistent_context(str(PROFILE), channel="msedge", headless=HEADLESS,
                                                      no_viewport=True, args=args)
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    if not page.url.startswith("file:"):
        await page.goto(url)
    await page.wait_for_load_state("load")
    await page.wait_for_function("typeof LEVELS !== 'undefined' && typeof OllamaAI !== 'undefined'")
    S.update(page=page, ctx=ctx, pw=pw)
    return page


@asynccontextmanager
async def lifespan(_server):
    await _open_page()     # the window is up before the first tool call, for OBS
    try:
        yield {}
    finally:
        try:
            await S["ctx"].close()
            await S["pw"].stop()
        except Exception as e:   # the window may already be gone
            print(f"[td] close: {e!r}", file=sys.stderr)


mcp = MCPServer("kingdom-defense", lifespan=lifespan,
                instructions="中世紀守塔：塔防遊戲。observe 看局面，build/upgrade 蓋塔，start_wave 開打，watch 看戰況。")


async def page():
    p = S["page"]
    if p is None or p.is_closed():
        p = await _open_page()
    return p


async def snapshot():
    return await (await page()).evaluate(SNAPSHOT_JS, TOWER_LETTER)


async def act(action, **a):
    return await (await page()).evaluate(ACTION_JS, [action, a])


def _text(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


async def _with_view(result, screenshot):
    snap = await snapshot()
    out = [_text({**result, "state": snap})]
    if screenshot:
        p = await page()
        target = p.locator("#gameCanvas") if snap["screen"] == "game" else p
        out.append(Image(data=await target.screenshot(type="png"), format="png"))
    return out


@mcp.tool()
async def observe(screenshot: bool = True):
    """The current screen and game state. In a level: wave/max_wave, wave_active, gold, hp/max_hp,
    kills, towers (type, level, x, y, upgrade_cost), enemies, build_spots (empty squares next to
    the road, best first), affordable towers, skill_cooldown_s, and map: 13 rows x 20 columns
    ('=' road, '.' grass, '~' swamp slows enemies, '^' lava burns them, letters = towers:
    A archer I ice C cannon M magic P poison L lightning S support), entrances, castle.
    Outside a level: levels (id, name, difficulty, waves, stars) and gems. screenshot: the battlefield."""
    return await _with_view({}, screenshot)


@mcp.tool()
async def choose_level(level_id: int, force: bool = False):
    """Start level 1-8 (1 草原之路 is the easiest). force=true abandons a level in progress."""
    return await _with_view(await act("choose_level", level_id=level_id, force=force), True)


@mcp.tool()
async def build(tower_type: str, x: int, y: int):
    """Build a tower on an empty grass square (x 0-19, y 0-12; build_spots lists good ones).
    archer 50G fast, hits flyers | ice 80G slows, ground only | poison 100G damage over time |
    cannon 120G splash, ground only | magic 150G chain lightning | lightning 180G big hit + stun |
    support 200G no attack, boosts nearby towers."""
    return await _with_view(await act("build", tower_type=tower_type, x=x, y=y), False)


@mcp.tool()
async def upgrade(x: int, y: int):
    """Upgrade the tower at (x, y) (max level 4; cost in towers[].upgrade_cost)."""
    return await _with_view(await act("upgrade", x=x, y=y), False)


@mcp.tool()
async def sell(x: int, y: int):
    """Sell the tower at (x, y) for 60% of its cost x level."""
    return await _with_view(await act("sell", x=x, y=y), False)


@mcp.tool()
async def start_wave():
    """Send the next wave. Then use watch() to follow the battle."""
    return await _with_view(await act("start_wave"), False)


@mcp.tool()
async def use_skill(skill: str):
    """meteor (隕石術: hits the 5 toughest enemies, 30 s cooldown) | freeze (全場冰凍: slows every
    enemy 5 s, 45 s) | gold (金幣加倍: double kill gold 10 s, 60 s). meteor/freeze only during a wave."""
    return await _with_view(await act("use_skill", skill=skill), False)


@mcp.tool()
async def set_speed(speed: int):
    """Game speed 1, 2 or 3. Keep 1 while streaming unless a wave is clearly under control."""
    return await _with_view(await act("set_speed", speed=speed), False)


@mcp.tool()
async def watch(seconds: int = 10, screenshot: bool = True):
    """Let the battle run for up to `seconds` (max 20) and report what happened: hp_lost, kills,
    gold_gained, and why it stopped: wave_cleared, game_over, castle_hit (lost HP), viewer_spoke
    (the host is talking: answer first), or time_up."""
    seconds = max(1, min(seconds, WATCH_MAX_S))
    before = await snapshot()
    if before["screen"] != "game":
        return [_text({"ok": False, "error": "not in a level", "state": before})]
    stopped, last = "time_up", before
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while loop.time() < deadline:
        await asyncio.sleep(0.5)
        if await asyncio.to_thread(viewer_pending):
            stopped = "viewer_spoke"
            break
        last = await snapshot()
        if last.get("game_over") or last["screen"] == "result":
            stopped = "game_over"
            break
        if before.get("wave_active") and not last.get("wave_active"):
            stopped = "wave_cleared"
            break
        if before.get("hp", 0) - last.get("hp", 0) >= max(3, before.get("max_hp", 0) * 0.15):
            stopped = "castle_hit"     # a real dent, not every single leak
            break
    report = {"ok": True, "stopped": stopped, "hp_lost": before.get("hp", 0) - last.get("hp", 0),
              "kills": last.get("kills", 0) - before.get("kills", 0),
              "gold_gained": last.get("gold", 0) - before.get("gold", 0)}
    return await _with_view(report, screenshot)


@mcp.tool()
async def retry():
    """After a defeat (or victory): play the same level again."""
    return await _with_view(await act("retry"), True)


@mcp.tool()
async def back_to_levels():
    """After a level ends: back to the level list."""
    return await _with_view(await act("back_to_levels"), False)


if __name__ == "__main__":
    mcp.run("stdio")
