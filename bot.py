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

RESPONSE_TIMEOUT = 20
# ─────────────────────────────────────────────────────────────────────────────

# Очередь команд для плагина
_queue: asyncio.Queue = asyncio.Queue()
_pending: dict[str, asyncio.Future] = {}

# ── Привязки SCP роль → список Discord role ID ────────────────────────────────
# Формат: { "Admin": [123456789, 987654321], "Moderator": [111222333] }

def _load_rolemap() -> dict[str, list[int]]:
    raw = os.getenv("ROLEMAP_JSON", "{}")
    try:
        return json.loads(raw)
    except Exception:
        return {}

def _save_rolemap(data: dict[str, list[int]]):
    import urllib.request
    render_token  = os.getenv("RENDER_API_KEY", "")
    render_svc_id = os.getenv("RENDER_SERVICE_ID", "")
    if not render_token or not render_svc_id:
        log.warning("RENDER_API_KEY или RENDER_SERVICE_ID не заданы — rolemap сбросится при перезапуске!")
        return
    payload = json.dumps({"value": json.dumps(data)}).encode()
    url = f"https://api.render.com/v1/services/{render_svc_id}/env-vars/ROLEMAP_JSON"
    req = urllib.request.Request(url, data=payload, method="PUT")
    req.add_header("Authorization", f"Bearer {render_token}")
    req.add_header("Content-Type", "application/json")
    try:
        urllib.request.urlopen(req, timeout=10)
        log.info("rolemap сохранён в Render env.")
    except Exception as e:
        log.error(f"Не удалось сохранить rolemap в Render: {e}")
      
rolemap: dict[str, list[int]] = _load_rolemap()


# ── Проверка токена ───────────────────────────────────────────────────────────
def _check_auth(request: web.Request):
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {API_TOKEN}":
        raise web.HTTPUnauthorized(text="Unauthorized")


# ── GET /poll ─────────────────────────────────────────────────────────────────
async def handle_poll(request: web.Request) -> web.Response:
    _check_auth(request)
    try:
        cmd = _queue.get_nowait()
        log.info(f"POLL → отдал команду: {cmd['action']} {cmd.get('steamid','')}")
        return web.json_response(cmd)
    except asyncio.QueueEmpty:
        return web.Response(status=204)


# ── POST /result ──────────────────────────────────────────────────────────────
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
    return web.Response(text=f"OK | queued={_queue.qsize()} waiting={len(_pending)}")


# ── Отправить команду плагину ─────────────────────────────────────────────────
async def send_command(action: str, steamid: str = "", role: str = "") -> dict:
    req_id = uuid.uuid4().hex[:12]
    fut    = asyncio.get_running_loop().create_future()
    _pending[req_id] = fut
    await _queue.put({"id": req_id, "action": action, "steamid": steamid, "role": role})
    log.info(f"CMD queued: {action} {steamid} [{req_id}]")
    try:
        return await asyncio.wait_for(fut, timeout=RESPONSE_TIMEOUT)
    except asyncio.TimeoutError:
        _pending.pop(req_id, None)
        return {"ok": False, "message": "⚠️ Плагин не ответил (таймаут). Сервер SCP:SL запущен?"}


# ── Discord бот ───────────────────────────────────────────────────────────────
intents         = discord.Intents.default()
intents.members = True   # нужно для выдачи ролей участникам
bot             = commands.Bot(command_prefix="!", intents=intents)
guild_obj       = discord.Object(id=GUILD_ID)


def has_permission(interaction: discord.Interaction) -> bool:
    if not ALLOWED_ROLE_IDS:
        return True
    return bool({r.id for r in interaction.user.roles} & set(ALLOWED_ROLE_IDS))


# ── /adminadd ─────────────────────────────────────────────────────────────────
@bot.tree.command(name="adminadd", description="Выдать роль игроку", guild=guild_obj)
@app_commands.describe(steamid="Steam ID (76561198000000000)", role="admin")
async def adminadd(interaction: discord.Interaction, steamid: str, role: str):
    await interaction.response.defer(ephemeral=True)
    if not has_permission(interaction):
        await interaction.followup.send("❌ Нет прав.", ephemeral=True)
        return
    data = await send_command("add", steamid=steamid, role=role)
    await interaction.followup.send(f"{'✅' if data['ok'] else '❌'} {data['message']}", ephemeral=True)
    if data["ok"]:
        await _log(interaction, f"🛡 **{interaction.user}** выдал роль `{role}` → `{steamid}`")


# ── /adminremove ──────────────────────────────────────────────────────────────
@bot.tree.command(name="adminremove", description="Забрать роль у игрока", guild=guild_obj)
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


# ── /ban, /unban ──────────────────────────────────────────────────────────────
@bot.tree.command(name="ban", description="Забанить игрока", guild=guild_obj)
@app_commands.describe(
    steamid="Steam ID игрока (76561198000000000)",
    days="Количество дней (0 = перманентный бан)",
    reason="Причина бана"
)
async def ban(interaction: discord.Interaction, steamid: str, days: int = 7, reason: str = "Ban"):
    await interaction.response.defer(ephemeral=True)
    if not has_permission(interaction):
        await interaction.followup.send("❌ Нет прав.", ephemeral=True)
        return
    
    data = await send_command("ban", steamid=steamid, role=f"{days}:{reason}")
    await interaction.followup.send(f"{'✅' if data['ok'] else '❌'} {data['message']}", ephemeral=True)
    if data["ok"]:
        duration = "перманентно" if days == 0 else f"на {days} дней"
        await _log(interaction, f"🚫 **{interaction.user}** забанил `{steamid}` {duration}\n**Причина:** {reason}")


@bot.tree.command(name="unban", description="Разбанить игрока", guild=guild_obj)
@app_commands.describe(steamid="Steam ID игрока (76561198000000000)")
async def unban(interaction: discord.Interaction, steamid: str):
    await interaction.response.defer(ephemeral=True)
    if not has_permission(interaction):
        await interaction.followup.send("❌ Нет прав.", ephemeral=True)
        return
    data = await send_command("unban", steamid=steamid)
    await interaction.followup.send(f"{'✅' if data['ok'] else '❌'} {data['message']}", ephemeral=True)
    if data["ok"]:
        await _log(interaction, f"✅ **{interaction.user}** разбанил `{steamid}`")

@bot.tree.command(name="unban", description="Разбанить игрока по Steam ID", guild=guild_obj)
@app_commands.describe(steamid="Steam ID игрока (76561198000000000)")
async def unban(interaction: discord.Interaction, steamid: str):
    await interaction.response.defer(ephemeral=True)
    if not has_permission(interaction):
        await interaction.followup.send("❌ Нет прав.", ephemeral=True)
        return
    data = await send_command("unban", steamid=steamid)
    await interaction.followup.send(f"{'✅' if data['ok'] else '❌'} {data['message']}", ephemeral=True)
    if data["ok"]:
        await _log(interaction, f"✅ **{interaction.user}** разбанил `{steamid}`")
      
# ── /adminlist ────────────────────────────────────────────────────────────────
@bot.tree.command(name="adminlist", description="Список администраторов", guild=guild_obj)
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


# ── /roleslist ────────────────────────────────────────────────────────────────
@bot.tree.command(name="roleslist", description="Показать все доступные роли", guild=guild_obj)
async def roleslist(interaction: discord.Interaction):
    if not has_permission(interaction):
        await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
        return
    raw = os.getenv("AVAILABLE_ROLES", "")
    if not raw:
        await interaction.response.send_message("⚠️ AVAILABLE_ROLES не задан.", ephemeral=True)
        return
    lines = []
    for entry in raw.split(","):
        entry = entry.strip()
        if ":" in entry:
            role, badge = entry.split(":", 1)
            lines.append(f"• `{role.strip()}` — {badge.strip()}")
        else:
            lines.append(f"• `{entry}`")
    embed = discord.Embed(
        title="📋 Доступные SCP роли",
        description="\n".join(lines) or "—",
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ── /rolesadd ─────────────────────────────────────────────────────────────────
@bot.tree.command(name="rolesadd", description="Выдать Discord роли по SCP:SL роли игрока", guild=guild_obj)
@app_commands.describe(
    steamid="Steam ID игрока (76561198000000000)",
    member="Discord пользователь которому выдать роли"
)
async def rolesadd(interaction: discord.Interaction, steamid: str, member: discord.Member):
    await interaction.response.defer(ephemeral=True)
    if not has_permission(interaction):
        await interaction.followup.send("❌ Нет прав.", ephemeral=True)
        return

    # Получаем SCP роль игрока из плагина
    data = await send_command("list")
    if not data["ok"]:
        await interaction.followup.send(f"❌ Не удалось получить список: {data['message']}", ephemeral=True)
        return

    try:
        admins = json.loads(data["message"])
    except Exception:
        await interaction.followup.send("❌ Ошибка разбора ответа от плагина.", ephemeral=True)
        return

    # Ищем steamid в списке и определяем SCP роль
    clean_steam = steamid.replace("@steam", "")
    scp_role: Optional[str] = None
    for entry in admins:
        if clean_steam in entry and ":" in entry:
            _, scp_role = entry.split(":", 1)
            scp_role = scp_role.strip()
            break

    if scp_role is None:
        await interaction.followup.send(
            f"❌ Игрок `{clean_steam}` не найден в списке админов сервера.",
            ephemeral=True
        )
        return

    # Ищем привязанные Discord роли
    discord_role_ids = rolemap.get(scp_role, [])
    if not discord_role_ids:
        await interaction.followup.send(
            f"⚠️ Для SCP роли `{scp_role}` нет привязанных Discord ролей.\n"
            f"Используй `/rolemap add {scp_role} @роль` чтобы добавить.",
            ephemeral=True
        )
        return

    # Выдаём Discord роли
    given  = []
    failed = []
    guild_instance = interaction.guild

    for role_id in discord_role_ids:
        d_role = guild_instance.get_role(role_id)
        if d_role is None:
            failed.append(f"ID:{role_id} (не найдена)")
            continue
        try:
            await member.add_roles(d_role, reason=f"AdminManager: SCP:SL роль {scp_role} / {interaction.user}")
            given.append(d_role.mention)
        except discord.Forbidden:
            failed.append(f"{d_role.name} (нет прав)")
        except Exception as e:
            failed.append(f"{d_role.name} ({e})")

    lines = [f"🎭 SCP роль игрока: **{scp_role}**", f"👤 Пользователь: {member.mention}", ""]
    if given:
        lines.append(f"✅ Выданы роли: {', '.join(given)}")
    if failed:
        lines.append(f"❌ Не удалось выдать: {', '.join(failed)}")

    await interaction.followup.send("\n".join(lines), ephemeral=True)

    if given:
        await _log(
            interaction,
            f"🎭 **{interaction.user}** выдал Discord роли {', '.join(given)} "
            f"→ {member.mention} (SCP: `{scp_role}` / `{clean_steam}`)"
        )


# ── /rolemap ──────────────────────────────────────────────────────────────────
rolemap_group = app_commands.Group(
    name="rolemap",
    description="Управление привязкой SCP ролей к Discord ролям",
    guild_ids=[GUILD_ID]
)


@rolemap_group.command(name="add", description="Привязать SCP:SL роль к Discord роли")
@app_commands.describe(
    scp_role="Роль в SCP (Admin, Moderator, Helper...)",
    discord_role="Discord роль которую выдавать"
)
async def rolemap_add(interaction: discord.Interaction, scp_role: str, discord_role: discord.Role):
    if not has_permission(interaction):
        await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
        return

    if scp_role not in rolemap:
        rolemap[scp_role] = []

    if discord_role.id in rolemap[scp_role]:
        await interaction.response.send_message(
            f"⚠️ `{scp_role}` уже привязана к {discord_role.mention}.", ephemeral=True
        )
        return

    rolemap[scp_role].append(discord_role.id)
    _save_rolemap(rolemap)

    await interaction.response.send_message(
        f"✅ `{scp_role}` → {discord_role.mention} привязано.", ephemeral=True
    )
    log.info(f"rolemap add: {scp_role} → {discord_role.name} ({discord_role.id})")


@rolemap_group.command(name="remove", description="Убрать привязку SCP:SL роли к Discord роли")
@app_commands.describe(
    scp_role="Роль в SCP",
    discord_role="Discord роль которую убрать из привязки"
)
async def rolemap_remove(interaction: discord.Interaction, scp_role: str, discord_role: discord.Role):
    if not has_permission(interaction):
        await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
        return

    if scp_role not in rolemap or discord_role.id not in rolemap[scp_role]:
        await interaction.response.send_message(
            f"⚠️ Привязка `{scp_role}` → {discord_role.mention} не найдена.", ephemeral=True
        )
        return

    rolemap[scp_role].remove(discord_role.id)
    if not rolemap[scp_role]:
        del rolemap[scp_role]
    _save_rolemap(rolemap)

    await interaction.response.send_message(
        f"✅ Привязка `{scp_role}` → {discord_role.mention} удалена.", ephemeral=True
    )


@rolemap_group.command(name="list", description="Показать все привязки ролей")
async def rolemap_list(interaction: discord.Interaction):
    if not rolemap:
        await interaction.response.send_message("Привязок нет. Используй `/rolemap add`.", ephemeral=True)
        return

    lines = []
    for scp_role, role_ids in rolemap.items():
        mentions = []
        for rid in role_ids:
            r = interaction.guild.get_role(rid)
            mentions.append(r.mention if r else f"ID:{rid}")
        lines.append(f"**{scp_role}** → {', '.join(mentions)}")

    embed = discord.Embed(
        title="🗺 Привязки SCP:SL → Discord ролей",
        description="\n".join(lines),
        color=discord.Color.gold()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


bot.tree.add_command(rolemap_group)


# ── Helpers ───────────────────────────────────────────────────────────────────
async def _log(interaction: discord.Interaction, text: str):
    ch = discord.utils.get(interaction.guild.text_channels, name="admin-log")
    if ch:
        await ch.send(text)


@bot.event
async def on_ready():
    bot.tree.copy_global_to(guild=guild_obj)
    await bot.tree.sync(guild=guild_obj)
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
