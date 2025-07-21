import asyncio
import json
import logging
import os
import random
from typing import Dict, Tuple

import asyncpg
import discord
from discord.ext import commands, tasks

# --- Loglama Ayarları ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s:%(levelname)s:%(name)s: %(message)s',
    handlers=[
        logging.FileHandler('leveling.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# --- Yapılandırma ---
CONFIG_FILE = "leveling_config.json"
DEFAULT_CONFIG = {
    "xp_range": {"min": 5, "max": 10},
    "xp_cooldown_seconds": 60,
    "level_roles": {},
    "blacklisted_channels": [],
    "xp_boosts": {},
    "congratulations_channel_id": None,
    "stack_roles": False # Yeni ayar: Rollerin yığılıp yığılmayacağı
}

class LevelingCog(commands.Cog):
    """Veritabanı ve komut yapısı geliştirilmiş seviye sistemi."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.logger = logging.getLogger(__name__)
        self.config = DEFAULT_CONFIG.copy()
        self._load_config()
        
        self.cooldowns = {}
        self.db_pool = None
        
        # --- PERFORMANS GELİŞTİRMESİ: XP Önbelleği ---
        # Her mesajda DB'ye yazmak yerine XP'yi burada biriktiririz.
        self.xp_cache: Dict[int, Dict[int, int]] = {} # guild_id -> {user_id: xp_to_add}
        
        self.bot.loop.create_task(self._init_db())
        self.flush_xp_cache_to_db.start() # Arka plan görevini başlat

    async def _init_db(self):
        """PostgreSQL veritabanına bağlanır ve gerekli tabloları oluşturur."""
        try:
            database_url = os.getenv("DATABASE_URL")
            if not database_url:
                self.logger.critical("DATABASE_URL çevresel değişkeni tanımlı değil. Bot başlatılamıyor.")
                raise ValueError("DATABASE_URL çevresel değişkeni eksik.")
            
            self.db_pool = await asyncpg.create_pool(database_url)
            async with self.db_pool.acquire() as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        user_id BIGINT,
                        guild_id BIGINT,
                        level INTEGER DEFAULT 0,
                        xp INTEGER DEFAULT 0,
                        total_xp INTEGER DEFAULT 0,
                        PRIMARY KEY (user_id, guild_id)
                    )
                """)
            self.logger.info("PostgreSQL veritabanı bağlantısı başarılı.")
        except Exception as e:
            self.logger.critical(f"Veritabanı başlatılamadı: {e}")
            raise

    def _load_config(self):
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    loaded_config = json.load(f)
                    for key, value in DEFAULT_CONFIG.items():
                        loaded_config.setdefault(key, value)
                    self.config = loaded_config
                self.logger.info(f"Yapılandırma dosyası ({CONFIG_FILE}) başarıyla yüklendi.")
            else:
                self._save_config()
                self.logger.info(f"Varsayılan yapılandırma dosyası ({CONFIG_FILE}) oluşturuldu.")
        except Exception as e:
            self.logger.error(f"Yapılandırma yüklenirken hata: {e}. Varsayılan ayarlar kullanılıyor.")
            self.config = DEFAULT_CONFIG.copy()

    def _save_config(self):
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
            self.logger.info(f"Yapılandırma {CONFIG_FILE} dosyasına kaydedildi.")
        except Exception as e:
            self.logger.error(f"Yapılandırma kaydedilemedi: {e}")
            
    def _calculate_xp_for_level(self, level: int) -> int:
        """Belirtilen seviyeye ulaşmak için gereken toplam XP miktarını hesaplar."""
        return 50 * (level ** 2) + (100 * level) + 200

    @tasks.loop(seconds=60.0)
    async def flush_xp_cache_to_db(self):
        """Önbellekte biriken XP'leri periyodik olarak veritabanına yazar."""
        if not self.xp_cache:
            return

        local_cache = self.xp_cache.copy()
        self.xp_cache.clear()
        
        self.logger.info(f"{len(local_cache)} sunucudan XP verileri veritabanına yazılıyor...")

        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                for guild_id, users in local_cache.items():
                    guild = self.bot.get_guild(guild_id)
                    if not guild:
                        continue
                        
                    for user_id, xp_to_add in users.items():
                        user_data = await conn.fetchrow(
                            "SELECT level, xp, total_xp FROM users WHERE user_id = $1 AND guild_id = $2",
                            user_id, guild_id
                        )

                        if user_data:
                            level, xp, total_xp = user_data['level'], user_data['xp'], user_data['total_xp']
                        else:
                            level, xp, total_xp = 0, 0, 0

                        xp += xp_to_add
                        total_xp += xp_to_add
                        
                        xp_for_next = self._calculate_xp_for_level(level + 1)
                        level_up = False
                        while xp >= xp_for_next:
                            level += 1
                            xp -= xp_for_next
                            xp_for_next = self._calculate_xp_for_level(level + 1)
                            level_up = True
                        
                        await conn.execute(
                            """
                            INSERT INTO users (user_id, guild_id, level, xp, total_xp)
                            VALUES ($1, $2, $3, $4, $5)
                            ON CONFLICT (user_id, guild_id) DO UPDATE
                            SET level = $3, xp = $4, total_xp = $5
                            """,
                            user_id, guild_id, level, xp, total_xp
                        )

                        if level_up:
                            member = guild.get_member(user_id)
                            if member:
                                self.bot.loop.create_task(self._handle_level_up(member, level, total_xp))

    async def _handle_level_up(self, member: discord.Member, new_level: int, total_xp: int):
        """Seviye atlama durumunda tebrik mesajı gönderir ve rolleri günceller."""
        await self._update_level_roles(member, new_level)
        
        channel_id = self.config.get("congratulations_channel_id")
        channel = self.bot.get_channel(channel_id) if channel_id else member.guild.system_channel
        
        if channel and channel.permissions_for(member.guild.me).send_messages:
            embed = discord.Embed(
                title="🎉 Seviye Atladın!",
                description=f"Tebrikler {member.mention}, **{new_level}** seviyesine ulaştın!",
                color=discord.Color.gold()
            ).set_thumbnail(url=member.display_avatar.url).set_footer(text=f"Yeni Toplam XP: {total_xp}")
            await channel.send(embed=embed)

    async def _update_level_roles(self, member: discord.Member, level: int):
        """Kullanıcının seviyesine göre rollerini ekler veya kaldırır."""
        guild = member.guild
        level_roles = self.config.get("level_roles", {})
        stack_roles = self.config.get("stack_roles", False)
        
        roles_to_add = []
        roles_to_remove = []
        highest_role_reached = None

        for level_str, role_id_str in sorted(level_roles.items(), key=lambda x: int(x[0])):
            level_threshold = int(level_str)
            role_id = int(role_id_str)
            role = guild.get_role(role_id)

            if not role or role.position >= guild.me.top_role.position:
                continue

            has_role = role in member.roles
            
            if level >= level_threshold:
                highest_role_reached = role
                if not has_role:
                    roles_to_add.append(role)
            elif has_role:
                roles_to_remove.append(role)
        
        if not stack_roles and highest_role_reached:
            for level_str, role_id_str in level_roles.items():
                role_id = int(role_id_str)
                role = guild.get_role(role_id)
                if role and role != highest_role_reached and role in member.roles:
                    roles_to_remove.append(role)

        if roles_to_add:
            await member.add_roles(*roles_to_add, reason=f"{level}. seviyeye ulaşıldı.")
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove, reason="Seviye rolü güncellendi.")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Her mesajda XP'yi veritabanı yerine önbelleğe ekler."""
        if not message.guild or message.author.bot or message.channel.id in self.config.get("blacklisted_channels", []):
            return
            
        prefix = await self.bot.get_prefix(message)
        if message.content.startswith(tuple(prefix) if isinstance(prefix, list) else prefix):
            return

        user_id = message.author.id
        guild_id = message.guild.id
        current_time = asyncio.get_event_loop().time()
        cooldown = self.config.get("xp_cooldown_seconds", 60)
        
        if current_time - self.cooldowns.get(user_id, 0) > cooldown:
            self.cooldowns[user_id] = current_time
            xp_range = self.config.get("xp_range", {"min": 5, "max": 10})
            xp_to_add = random.randint(xp_range['min'], xp_range['max'])
            
            if guild_id not in self.xp_cache:
                self.xp_cache[guild_id] = {}
            if user_id not in self.xp_cache[guild_id]:
                self.xp_cache[guild_id][user_id] = 0
            self.xp_cache[guild_id][user_id] += xp_to_add

    # --- KULLANICI KOMUTLARI ---
    @commands.command(name="seviye", aliases=["level", "rank"])
    async def level_command(self, ctx: commands.Context, member: discord.Member = None):
        """Bir üyenin seviye, XP ve sunucu sıralamasını gösterir."""
        target = member or ctx.author
        
        async with self.db_pool.acquire() as conn:
            user_data = await conn.fetchrow("SELECT level, xp, total_xp FROM users WHERE user_id = $1 AND guild_id = $2", target.id, ctx.guild.id)
            if not user_data:
                await ctx.send(f"{target.display_name} kullanıcısının henüz bir seviye verisi yok.")
                return
            
            level, xp, total_xp = user_data['level'], user_data['xp'], user_data['total_xp']
            rank = await conn.fetchval(
                "SELECT COUNT(*) + 1 FROM users WHERE guild_id = $1 AND total_xp > $2",
                ctx.guild.id, total_xp
            ) or 1

        xp_needed = self._calculate_xp_for_level(level + 1)
        progress = (xp / xp_needed) * 100
        progress_bar = f"[{'█' * int(progress / 5)}{'─' * (20 - int(progress / 5))}]"

        embed = discord.Embed(
            title=f"{target.display_name} Seviye Bilgisi",
            color=discord.Color.gold()
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="Sıralama", value=f"#{rank}", inline=True)
        embed.add_field(name="Seviye", value=str(level), inline=True)
        embed.add_field(name="Toplam XP", value=str(total_xp), inline=True)
        embed.add_field(
            name="İlerleme",
            value=f"{xp} / {xp_needed} XP\n{progress_bar} {progress:.1f}%",
            inline=False
        )
        await ctx.send(embed=embed)

    @commands.command(name="lider", aliases=["leaderboard", "top"])
    async def leaderboard(self, ctx: commands.Context):
        """Sunucudaki en yüksek XP'ye sahip kullanıcıları listeler."""
        async with self.db_pool.acquire() as conn:
            top_users = await conn.fetch(
                "SELECT user_id, level, total_xp FROM users WHERE guild_id = $1 ORDER BY total_xp DESC LIMIT 10",
                ctx.guild.id
            )

        if not top_users:
            await ctx.send("Bu sunucuda henüz kimse sıralamaya girmemiş.")
            return

        embed = discord.Embed(
            title=f"🏆 {ctx.guild.name} Liderlik Tablosu",
            color=discord.Color.gold()
        )
        
        description = []
        for i, user_data in enumerate(top_users, 1):
            member = ctx.guild.get_member(user_data['user_id'])
            display_name = member.display_name if member else f"Bilinmeyen Üye (ID: {user_data['user_id']})"
            description.append(
                f"**{i}.** {display_name} - **Seviye {user_data['level']}** ({user_data['total_xp']} XP)"
            )
        
        embed.description = "\n".join(description)
        await ctx.send(embed=embed)

    @commands.command(name="seviyesifirla", aliases=["levelreset"])
    @commands.has_permissions(manage_guild=True)
    async def reset_level(self, ctx: commands.Context, member: discord.Member):
        """Bir üyenin seviyesini ve XP'sini tamamen sıfırlar."""
        async with self.db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET level = 0, xp = 0, total_xp = 0 WHERE user_id = $1 AND guild_id = $2",
                member.id, ctx.guild.id
            )
        
        await self._update_level_roles(member, 0)
        await ctx.send(f"✅ {member.mention} kullanıcısının tüm seviye verileri sıfırlandı.")
        self.logger.info(f"{ctx.author.display_name}, {member.display_name} kullanıcısının seviyesini sıfırladı.")

    # --- YÖNETİCİ KOMUT GRUBU ---
    @commands.group(name="seviyeayar", aliases=["levelsettings"], invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    async def level_settings(self, ctx: commands.Context):
        """Seviye sistemi ayarlarını yönetmek için ana komut."""
        embed = discord.Embed(
            title="Seviye Sistemi Ayarları",
            description="Aşağıdaki alt komutları kullanarak seviye sistemini yapılandırabilirsiniz:",
            color=discord.Color.gold()
        )
        embed.add_field(name=f"`{ctx.prefix}seviyeayar rolver <seviye> <@rol>`", value="Seviye ödül rolü belirler.", inline=False)
        embed.add_field(name=f"`{ctx.prefix}seviyeayar rolkaldir <seviye>`", value="Bir seviye ödül rolünü kaldırır.", inline=False)
        embed.add_field(name=f"`{ctx.prefix}seviyeayar rolyiginla <ac/kapat>`", value="Seviye rolleri yığılsın mı yoksa sadece en yükseği mi kalsın.", inline=False)
        embed.add_field(name=f"`{ctx.prefix}seviyeayar kanalkapat <#kanal>`", value="Bir kanalda XP kazanımını kapatır.", inline=False)
        embed.add_field(name=f"`{ctx.prefix}seviyeayar kanalac <#kanal>`", value="Bir kanalda XP kazanımını açar.", inline=False)
        await ctx.send(embed=embed)

    @level_settings.command(name="rolver")
    @commands.has_permissions(manage_guild=True)
    async def set_level_role(self, ctx: commands.Context, level: int, role: discord.Role):
        if role.position >= ctx.guild.me.top_role.position:
            await ctx.send(f"❌ '{role.name}' rolünü yönetemem. Lütfen botun rolünü bu rolün üzerine taşıyın.")
            return
        self.config["level_roles"][str(level)] = role.id
        self._save_config()
        await ctx.send(f"✅ Seviye **{level}** için ödül rolü **{role.name}** olarak ayarlandı.")

    @level_settings.command(name="rolkaldir")
    @commands.has_permissions(manage_guild=True)
    async def remove_level_role(self, ctx: commands.Context, level: int):
        if str(level) in self.config["level_roles"]:
            del self.config["level_roles"][str(level)]
            self._save_config()
            await ctx.send(f"✅ Seviye **{level}** için ayarlanmış ödül rolü kaldırıldı.")
        else:
            await ctx.send(f"❌ Bu seviye için zaten bir ödül rolü ayarlanmamış.")

    @level_settings.command(name="rolyiginla")
    @commands.has_permissions(manage_guild=True)
    async def set_role_stacking(self, ctx: commands.Context, durum: str):
        durum = durum.lower()
        if durum in ["aç", "ac", "on", "true", "evet"]:
            self.config["stack_roles"] = True
            self._save_config()
            await ctx.send("✅ Rol yığınlama **aktif**. Kullanıcılar kazandıkları tüm seviye rollerini koruyacak.")
        elif durum in ["kapat", "off", "false", "hayir"]:
            self.config["stack_roles"] = False
            self._save_config()
            await ctx.send("✅ Rol yığınlama **devre dışı**. Kullanıcılar sadece ulaştıkları en yüksek seviye rolünü taşıyacak.")
        else:
            await ctx.send("❌ Geçersiz durum. Lütfen `ac` veya `kapat` kullanın.")

    async def cog_unload(self):
        """Cog kapatıldığında önbelleği veritabanına yaz ve bağlantıyı kapat."""
        self.flush_xp_cache_to_db.cancel()
        await self.flush_xp_cache_to_db()
        if self.db_pool:
            await self.db_pool.close()
            self.logger.info("LevelingCog kaldırıldı, PostgreSQL bağlantısı kapatıldı.")

async def setup(bot: commands.Bot):
    """Bot'a LevelingCog'u ekler."""
    await bot.add_cog(LevelingCog(bot))
