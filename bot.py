"""
AdminManager Discord Bot — WebSocket + HTTP на одном порту через aiohttp
Хостинг: Render.com (Web Service)
"""

import os
import sys
import json
import asyncio
import uuid
import logging
from typing import Optional

sys.stdout.reconfigure(line_buffering=True)

import discord
from discord import app_commands
from discord.ext import commands
from aiohttp import web
import aiohttp

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("AdminBot")

# ── Настройки ─────────────────────────────────────────────────────────────────
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID      = int(os.environ["GUILD_ID"])
API_TOKEN     = os.environ["API_TOKEN"]
WS_PORT       = int(os.getenv("PORT", "8080"))

_raw = os.getenv("ALLOWED_ROLES", "")
ALLOWED_ROLE_IDS: list[int] = [int(x) for x in _raw.split(",") if x.strip()]

RESPONSE_TIMEOUT = 15
# ─────────────────────────────────────────────────────────────────────────────

plugin_ws: Optional[web.WebSocketResponse] = None
pending: dict[str, asyncio.Future] = {}


# ── aiohttp WebSocket handler ─────────────────────────────────────────────────
async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    global plugin_ws

    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {API_TOKEN}":
        raise web.HTTPUnauthorized(text="Unauthorized")

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    plugin_ws = ws
    log.info(f"WS: плагин подключился с {request.remote}")

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data   = json.loads(msg.data)
                    req_id = data.get("id")
                    if req_id and req_id in pending:
                        pending[req_id].set_result(data)
                except Exception as e:
                    log.error(f"WS: ошибка разбора ответа: {e}")
            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break
    finally:
        if plugin_ws is ws:
            plugin_ws = None
        log.info("WS: плагин отключился")

    return ws


async def send_to_plugin(action: str, **kwargs) -> dict:
    if plugin_ws is None or plugin_ws.closed:
        return {"ok": False, "message": "⚠️ Плагин не подключён (сервер SCP выключен?)"}

    req_id = uuid.uuid4().hex[:12]
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    pending[req_id] = future

    payload = {"id": req_id, "action": action, **kwargs}
    try:
        await plugin_ws.send_str(json.dumps(payload))
        result = await asyncio.wait_for(future, timeout=RESPONSE_TIMEOUT)
        return result
    except asyncio.TimeoutError:
        return {"ok": False, "message": "⚠️ Плагин не ответил (таймаут)"}
    except Exception as e:
        return {"ok": False, "message": f"Ошибка: {e}"}
    finally:
        pending.pop(req_id, None)


# ── Health check ──────────────────────────────────────────────────────────────
async def health(request: web.Request) -> web.Response:
    return web.Response(text="OK")


# ── Discord бот ───────────────────────────────────────────────────────────────
intents = discord.Intents.default()
bot     = commands.Bot(command_prefix="!", intents=intents)
guild   = discord.Object(id=GUILD_ID)


def has_permission(interaction: discord.Interaction) -> bool:
    if not ALLOWED_ROLE_IDS:
        return True
    return bool({r.id for r in interaction.user.roles} & set(ALLOWED_ROLE_IDS))


@bot.tree.command(name="adminadd", description="Выдать роль игроку SCP сервера", guild=guild)
@app_commands.describe(steamid="Steam ID (76561198000000000)", role="Admin / Moderator / Helper")
async def adminadd(interaction: discord.Interaction, steamid: str, role: str):
    await interaction.response.defer(ephemeral=True)
    if not has_permission(interaction):
        await interaction.followup.send("❌ Нет прав.", ephemeral=True); return
    data = await send_to_plugin("add", steamid=steamid, role=role)
    await interaction.followup.send(f"{'✅' if data['ok'] else '❌'} {data['message']}", ephemeral=True)
    if data["ok"]:
        await _log(interaction, f"🛡 **{interaction.user}** выдал роль `{role}` → `{steamid}`")


@bot.tree.command(name="adminremove", description="Забрать роль у игрока SCP сервера", guild=guild)
@app_commands.describe(steamid="Steam ID игрока")
async def adminremove(interaction: discord.Interaction, steamid: str):
    await interaction.response.defer(ephemeral=True)
    if not has_permission(interaction):
        await interaction.followup.send("❌ Нет прав.", ephemeral=True); return
    data = await send_to_plugin("remove", steamid=steamid)
    await interaction.followup.send(f"{'✅' if data['ok'] else '❌'} {data['message']}", ephemeral=True)
    if data["ok"]:
        await _log(interaction, f"🚫 **{interaction.user}** убрал роль у `{steamid}`")


@bot.tree.command(name="adminlist", description="Список администраторов SCP сервера", guild=guild)
async def adminlist(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not has_permission(interaction):
        await interaction.followup.send("❌ Нет прав.", ephemeral=True); return
    data = await send_to_plugin("list")
    if not data["ok"]:
        await interaction.followup.send(f"❌ {data['message']}", ephemeral=True); return
    try:
        admins = json.loads(data["message"])
    except Exception:
        admins = []
    if not admins:
        await interaction.followup.send("Список пуст.", ephemeral=True); return
    lines = []
    for entry in admins:
        if ":" in entry:
            sid, role = entry.split(":", 1)
            lines.append(f"`{sid.strip()}` — **{role.strip()}**")
    embed = discord.Embed(
        title=f"🛡 Администраторы ({len(lines)})",
        description="\n".join(lines) or "—",
        color=discord.Color.blue()
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


async def _log(interaction: discord.Interaction, text: str):
    ch = discord.utils.get(interaction.guild.text_channels, name="admin-log")
    if ch:
        await ch.send(text)


@bot.event
async def on_ready():
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)
    log.info(f"Discord: запущен как {bot.user}")


# ── Запуск ────────────────────────────────────────────────────────────────────
async def main():
    # aiohttp приложение: и WS и health check на одном порту
    app = web.Application()
    app.router.add_get("/ws", ws_handler)       # WebSocket endpoint
    app.router.add_get("/", health)             # health check GET

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WS_PORT)
    await site.start()
    log.info(f"HTTP+WS сервер слушает порт {WS_PORT}")

    async with bot:
        await bot.start(DISCORD_TOKEN)

asyncio.run(main())
