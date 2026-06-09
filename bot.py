"""
AdminManager Discord Bot — HTTP polling на aiohttp
Хостинг: Render.com (Web Service)

Схема:
  Плагин каждые 2 сек  →  GET  /poll    — забирает команду (или 204)
  Плагин               →  POST /result  — возвращает результат
  Discord slash cmd    →  send_command() → кладёт в очередь, ждёт Future
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

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("AdminBot")

# ── Настройки ─────────────────────────────────────────────────────────────────
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID      = int(os.environ["GUILD_ID"])
API_TOKEN     = os.environ["API_TOKEN"]
HTTP_PORT     = int(os.getenv("PORT", "8080"))

_raw = os.getenv("ALLOWED_ROLES", "")
ALLOWED_ROLE_IDS: list[int] = [int(x) for x in _raw.split(",") if x.strip()]

RESPONSE_TIMEOUT = 20   # сек — плагин опрашивает каждые 2 сек, 20 хватит
# ─────────────────────────────────────────────────────────────────────────────

# Очередь команд для плагина: каждый элемент — dict {id, action, steamid, role}
_queue: asyncio.Queue = asyncio.Queue()

# reqId -> Future[dict] — ждём ответ от плагина
_pending: dict[str, asyncio.Future] = {}


# ── Проверка токена ───────────────────────────────────────────────────────────
def _check_auth(request: web.Request):
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {API_TOKEN}":
        raise web.HTTPUnauthorized(text="Unauthorized")


# ── GET /poll  (плагин забирает команду) ─────────────────────────────────────
async def handle_poll(request: web.Request) -> web.Response:
    _check_auth(request)
    try:
        cmd = _queue.get_nowait()
        log.info(f"POLL → отдал команду: {cmd['action']} {cmd.get('steamid','')}")
        return web.json_response(cmd)
    except asyncio.QueueEmpty:
        return web.Response(status=204)  # нет команд — плагин ждёт 2 сек и снова


# ── POST /result  (плагин возвращает результат) ──────────────────────────────
async def handle_result(request: web.Request) -> web.Response:
    _check_auth(request)
    data   = await request.json()
    req_id = data.get("id")
    ok_str = "OK" if data.get("ok") else "ERR"
    log.info(f"RESULT ← {req_id}: {ok_str}: {data.get('message','')}")

    if req_id and req_id in _pending:
        fut = _pending.pop(req_id)
        if not fut.done():
            fut.set_result(data)

    return web.json_response({"ok": True})


# ── Health check ──────────────────────────────────────────────────────────────
async def handle_health(request: web.Request) -> web.Response:
    queued = _queue.qsize()
    waiting = len(_pending)
    return web.Response(text=f"OK | queued={queued} waiting={waiting}")


# ── Отправить команду плагину и дождаться ответа ─────────────────────────────
async def send_command(action: str, steamid: str = "", role: str = "") -> dict:
    req_id = uuid.uuid4().hex[:12]
    loop   = asyncio.get_running_loop()
    fut    = loop.create_future()
    _pending[req_id] = fut

    await _queue.put({"id": req_id, "action": action, "steamid": steamid, "role": role})
    log.info(f"CMD queued: {action} {steamid} [{req_id}]")

    try:
        return await asyncio.wait_for(fut, timeout=RESPONSE_TIMEOUT)
    except asyncio.TimeoutError:
        _pending.pop(req_id, None)
        return {"ok": False, "message": "⚠️ Плагин не ответил (таймаут). Сервер SCP запущен?"}


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
        await interaction.followup.send("❌ Нет прав.", ephemeral=True)
        return
    data = await send_command("add", steamid=steamid, role=role)
    await interaction.followup.send(f"{'✅' if data['ok'] else '❌'} {data['message']}", ephemeral=True)
    if data["ok"]:
        await _log(interaction, f"🛡 **{interaction.user}** выдал роль `{role}` → `{steamid}`")


@bot.tree.command(name="adminremove", description="Забрать роль у игрока SCP сервера", guild=guild)
@app_commands.describe(steamid="Steam ID игрока")
async def adminremove(interaction: discord.Interaction, steamid: str):
    await interaction.response.defer(ephemeral=True)
    if not has_permission(interaction):
        await interaction.followup.send("❌ Нет прав.", ephemeral=True)
        return
    data = await send_command("remove", steamid=steamid)
    await interaction.followup.send(f"{'✅' if data['ok'] else '❌'} {data['message']}", ephemeral=True)
    if data["ok"]:
        await _log(interaction, f"🚫 **{interaction.user}** убрал роль у `{steamid}`")


@bot.tree.command(name="adminlist", description="Список администраторов SCP сервера", guild=guild)
async def adminlist(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not has_permission(interaction):
        await interaction.followup.send("❌ Нет прав.", ephemeral=True)
        return
    data = await send_command("list")
    if not data["ok"]:
        await interaction.followup.send(f"❌ {data['message']}", ephemeral=True)
        return
    try:
        admins = json.loads(data["message"])
    except Exception:
        admins = []
    if not admins:
        await interaction.followup.send("Список пуст.", ephemeral=True)
        return
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
    app = web.Application()
    app.router.add_get("/poll",    handle_poll)
    app.router.add_post("/result", handle_result)
    app.router.add_get("/",        handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HTTP_PORT)
    await site.start()
    log.info(f"HTTP сервер слушает порт {HTTP_PORT}")

    async with bot:
        await bot.start(DISCORD_TOKEN)


asyncio.run(main())
