"""
AdminManager Discord Bot — WebSocket сервер
Хостинг: Render.com (Web Service)
Плагин сам подключается к боту при старте сервера SCP.

Зависимости: pip install discord.py websockets
"""

import os
import sys
import json
import asyncio
import uuid
import logging
from typing import Optional

# Отключаем буферизацию — логи видны в реальном времени на Render
sys.stdout.reconfigure(line_buffering=True)

import discord
from discord import app_commands
from discord.ext import commands
import websockets
from websockets.asyncio.server import ServerConnection, serve
from websockets.http11 import Request, Response
from websockets.datastructures import Headers

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("AdminBot")

# ── Настройки (Environment Variables на Render) ───────────────────────────────
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID      = int(os.environ["GUILD_ID"])
API_TOKEN     = os.environ["API_TOKEN"]           # совпадает с api_token в Config.yml плагина
WS_PORT       = int(os.getenv("PORT", "8080"))    # Render сам задаёт PORT

_raw = os.getenv("ALLOWED_ROLES", "")
ALLOWED_ROLE_IDS: list[int] = [int(x) for x in _raw.split(",") if x.strip()]

RESPONSE_TIMEOUT = 15  # секунд ждём ответ от плагина
# ─────────────────────────────────────────────────────────────────────────────

# Активное WS соединение с плагином (только одно)
plugin_ws: Optional[ServerConnection] = None

# Ожидающие ответа: {req_id: asyncio.Future}
pending: dict[str, asyncio.Future] = {}


# ── Health check — отвечает на HEAD/GET от Render до WS handshake ─────────────
async def health_check(connection: ServerConnection, request: Request) -> Optional[Response]:
    if request.method in ("HEAD", "GET"):
        body = b"OK"
        headers = Headers([
            ("Content-Type", "text/plain"),
            ("Content-Length", str(len(body))),
        ])
        return Response(200, "OK", headers, body)
    return None  # продолжить WS handshake


# ── WebSocket сервер ──────────────────────────────────────────────────────────
async def ws_handler(ws: ServerConnection):
    global plugin_ws

    # Проверка токена
    auth = ws.request.headers.get("Authorization", "")
    if auth != f"Bearer {API_TOKEN}":
        await ws.close(1008, "Unauthorized")
        log.warning("WS: отклонено соединение — неверный токен")
        return

    plugin_ws = ws
    log.info(f"WS: плагин подключился с {ws.remote_address}")

    try:
        async for message in ws:
            try:
                data   = json.loads(message)
                req_id = data.get("id")
                if req_id and req_id in pending:
                    pending[req_id].set_result(data)
            except Exception as e:
                log.error(f"WS: ошибка разбора ответа: {e}")
    finally:
        if plugin_ws is ws:
            plugin_ws = None
        log.info("WS: плагин отключился")


async def send_to_plugin(action: str, **kwargs) -> dict:
    """Отправляет команду плагину и ждёт ответа."""
    if plugin_ws is None:
        return {"ok": False, "message": "⚠️ Плагин не подключён (сервер SCP выключен?)"}

    req_id = uuid.uuid4().hex[:12]
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    pending[req_id] = future

    payload = {"id": req_id, "action": action, **kwargs}
    try:
        await plugin_ws.send(json.dumps(payload))
        result = await asyncio.wait_for(future, timeout=RESPONSE_TIMEOUT)
        return result
    except asyncio.TimeoutError:
        return {"ok": False, "message": "⚠️ Плагин не ответил (таймаут)"}
    except Exception as e:
        return {"ok": False, "message": f"Ошибка: {e}"}
    finally:
        pending.pop(req_id, None)


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


# ── Запуск обоих серверов вместе ──────────────────────────────────────────────
async def main():
    ws_server = await serve(ws_handler, "0.0.0.0", WS_PORT, process_request=health_check)
    log.info(f"WS сервер слушает порт {WS_PORT}")

    async with bot:
        await asyncio.gather(
            bot.start(DISCORD_TOKEN),
            ws_server.wait_closed(),
        )

asyncio.run(main())
