"""
AdminManager Discord Bot
Хостинг:    Render.com (Web Service или Worker)
Туннель:    Cloudflare Tunnel → localhost:8080 → плагин
Зависимости: pip install discord.py aiohttp
"""

import os
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

# ── Переменные окружения (задаются в Render Dashboard → Environment) ──────────
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]          # токен бота
API_URL       = os.environ["API_URL"]                # https://your-tunnel.trycloudflare.com
API_TOKEN     = os.environ["API_TOKEN"]              # совпадает с api_token в Config.yml плагина
GUILD_ID      = int(os.environ["GUILD_ID"])          # ID Discord-сервера

# ID ролей Discord, которым разрешены команды (через запятую в переменной ALLOWED_ROLES)
# Пример: ALLOWED_ROLES=123456789,987654321   Пусто = все
_raw = os.getenv("ALLOWED_ROLES", "")
ALLOWED_ROLE_IDS: list[int] = [int(x) for x in _raw.split(",") if x.strip()]
# ─────────────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
bot     = commands.Bot(command_prefix="!", intents=intents)
guild   = discord.Object(id=GUILD_ID)


# ── HTTP-клиент к плагину ─────────────────────────────────────────────────────
async def api_get(path: str) -> dict:
    async with aiohttp.ClientSession() as s:
        async with s.get(
            f"{API_URL}{path}",
            headers={"Authorization": f"Bearer {API_TOKEN}"},
            timeout=aiohttp.ClientTimeout(total=8)
        ) as r:
            return await r.json()

async def api_post(path: str, payload: dict) -> dict:
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{API_URL}{path}",
            json=payload,
            headers={"Authorization": f"Bearer {API_TOKEN}"},
            timeout=aiohttp.ClientTimeout(total=8)
        ) as r:
            return await r.json()


# ── Проверка прав ─────────────────────────────────────────────────────────────
def has_permission(interaction: discord.Interaction) -> bool:
    if not ALLOWED_ROLE_IDS:
        return True
    member_roles = {r.id for r in interaction.user.roles}
    return bool(member_roles & set(ALLOWED_ROLE_IDS))


# ── /adminadd ─────────────────────────────────────────────────────────────────
@bot.tree.command(name="adminadd", description="Выдать роль игроку SCP сервера", guild=guild)
@app_commands.describe(
    steamid="Steam ID игрока (76561198000000000)",
    role="Роль: Admin / Moderator / Helper"
)
async def adminadd(interaction: discord.Interaction, steamid: str, role: str):
    await interaction.response.defer(ephemeral=True)
    if not has_permission(interaction):
        await interaction.followup.send("❌ Нет прав.", ephemeral=True)
        return
    try:
        data = await api_post("/adminadd", {"steamid": steamid, "role": role})
    except Exception as e:
        await interaction.followup.send(f"❌ Нет связи с сервером: `{e}`", ephemeral=True)
        return

    emoji = "✅" if data.get("ok") else "❌"
    await interaction.followup.send(f"{emoji} {data.get('message')}", ephemeral=True)

    if data.get("ok"):
        await _log(interaction, f"🛡 **{interaction.user}** выдал роль `{role}` → `{steamid}`")


# ── /adminremove ──────────────────────────────────────────────────────────────
@bot.tree.command(name="adminremove", description="Забрать роль у игрока SCP сервера", guild=guild)
@app_commands.describe(steamid="Steam ID игрока")
async def adminremove(interaction: discord.Interaction, steamid: str):
    await interaction.response.defer(ephemeral=True)
    if not has_permission(interaction):
        await interaction.followup.send("❌ Нет прав.", ephemeral=True)
        return
    try:
        data = await api_post("/adminremove", {"steamid": steamid})
    except Exception as e:
        await interaction.followup.send(f"❌ Нет связи с сервером: `{e}`", ephemeral=True)
        return

    emoji = "✅" if data.get("ok") else "❌"
    await interaction.followup.send(f"{emoji} {data.get('message')}", ephemeral=True)

    if data.get("ok"):
        await _log(interaction, f"🚫 **{interaction.user}** убрал роль у `{steamid}`")


# ── /adminlist ────────────────────────────────────────────────────────────────
@bot.tree.command(name="adminlist", description="Список администраторов SCP сервера", guild=guild)
async def adminlist(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not has_permission(interaction):
        await interaction.followup.send("❌ Нет прав.", ephemeral=True)
        return
    try:
        data = await api_get("/adminlist")
    except Exception as e:
        await interaction.followup.send(f"❌ Нет связи с сервером: `{e}`", ephemeral=True)
        return

    if not data.get("ok"):
        await interaction.followup.send(f"❌ {data.get('message')}", ephemeral=True)
        return

    admins: list[dict] = data.get("admins", [])
    if not admins:
        await interaction.followup.send("Список админов пуст.", ephemeral=True)
        return

    lines = [f"`{a['steamid']}` — **{a['role']}**" for a in admins]
    embed = discord.Embed(
        title=f"🛡 Список администраторов ({len(admins)})",
        description="\n".join(lines),
        color=discord.Color.blue()
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


# ── Лог в #admin-log ─────────────────────────────────────────────────────────
async def _log(interaction: discord.Interaction, text: str):
    log_ch = discord.utils.get(interaction.guild.text_channels, name="admin-log")
    if log_ch:
        await log_ch.send(text)


# ── Старт ─────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)
    print(f"[Bot] Запущен как {bot.user}")
    print(f"[Bot] API: {API_URL}")


bot.run(DISCORD_TOKEN)
