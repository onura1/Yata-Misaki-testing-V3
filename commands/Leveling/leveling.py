import asyncio
import json
import logging
import os
import random
from typing import Tuple
import discord
from discord.ext import commands
import asyncpg

# Loglama ayarları
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s:%(levelname)s:%(name)s: %(message)s',
    handlers=[
        logging.FileHandler('leveling.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

CONFIG_FILE = "leveling_config.json"
DEFAULT_CONFIG = {
    "xp_range": {"min": 5, "max": 10},  # XP aralığı düşürüldü
    "xp_cooldown_seconds": 60,
    "level_roles": {},
    "blacklisted_channels": [],
    "xp_boosts": {},
    "congratulations_channel_id": None  # Tebrik mesajı için kanal ID'si
}

class LevelingCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.logger = logging.getLogger(__name__)
        self.config = DEFAULT_CONFIG.copy()
        self._load_config()
        self.cooldowns = {}
        self.db_pool = None
        self.bot.loop.create_task(self._init_db())

    async def _init_db(self):
        """PostgreSQL veritabanına bağlan ve tabloyu oluştur."""
        try:
            database_url = os.getenv("DATABASE_URL")
            if not database_url:
                self.logger.error("DATABASE_URL çevresel değişkeni tanımlı değil.")
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
                await conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_total_xp ON users (total_xp DESC)
                """)
                self.logger.info("PostgreSQL veritabanı başlatıldı.")
        except asyncpg.exceptions.PostgresSyntaxError as e:
            self.logger.error(f"SQL sentaks hatası: {e}")
            raise
        except asyncpg.exceptions.InvalidCatalogNameError:
            self.logger.error("Geçersiz veritabanı adı. DATABASE_URL kontrol edin.")
            raise
        except asyncpg.exceptions.ConnectionDoesNotExistError:
            self.logger.error("Veritabanına bağlantı kurulamadı. DATABASE_URL veya ağ ayarlarını kontrol edin.")
            raise
        except Exception as e:
            self.logger.error(f"Veritabanı başlatma hatası: {type(e).__name__}: {e}")
            raise

    def _load_config(self):
        """Yapılandırma dosyasını yükle."""
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    loaded_config = json.load(f)
                    if not isinstance(loaded_config, dict):
                        raise ValueError("Yapılandırma dosyası bir JSON nesnesi olmalı.")
                    for key in DEFAULT_CONFIG:
                        if key not in loaded_config:
                            self.logger.warning(f"'{key}' yapılandırmada eksik, varsayılan değer kullanılıyor.")
                            loaded_config[key] = DEFAULT_CONFIG[key]
                    if not isinstance(loaded_config["xp_range"], dict) or \
                       not all(k in loaded_config["xp_range"] for k in ["min", "max"]) or \
                       not all(isinstance(v, int) and v > 0 for v in loaded_config["xp_range"].values()):
                        self.logger.warning("Geçersiz 'xp_range', varsayılan değer kullanılıyor.")
                        loaded_config["xp_range"] = DEFAULT_CONFIG["xp_range"]
                    self.config.update(loaded_config)
                    self.logger.info(f"Yapılandırma yüklendi: {CONFIG_FILE}")
            else:
                self.config = DEFAULT_CONFIG.copy()
                self._save_config()
                self.logger.info(f"Yapılandırma oluşturuldu: {CONFIG_FILE}")
        except Exception as e:
            self.logger.error(f"Yapılandırma yüklenirken hata: {e}. Varsayılan yapılandırma kullanılıyor.")
            self.config = DEFAULT_CONFIG.copy()
            self._save_config()

    def _save_config(self):
        """Yapılandırmayı dosyaya kaydet."""
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
            self.logger.info(f"Yapılandırma kaydedildi: {CONFIG_FILE}")
        except Exception as e:
            self.logger.error(f"Yapılandırma kaydedilemedi: {e}")

    async def _get_user_data(self, guild_id: int, user_id: int) -> Tuple[int, int, int]:
        """Kullanıcı verilerini PostgreSQL'den al."""
        try:
            async with self.db_pool.acquire() as conn:
                result = await conn.fetchrow(
                    "SELECT level, xp, total_xp FROM users WHERE user_id = $1 AND guild_id = $2",
                    user_id, guild_id
                )
                if result and None not in result:
                    return (result['level'], result['xp'], result['total_xp'])
                else:
                    self.logger.info(f"Kullanıcı DB'de bulunamadı/eksik, sıfırlanıyor (K:{user_id}, S:{guild_id})")
                    await conn.execute(
                        "INSERT INTO users (user_id, guild_id, level, xp, total_xp) VALUES ($1, $2, 0, 0, 0)",
                        user_id, guild_id
                    )
                    return (0, 0, 0)
        except Exception as e:
            self.logger.error(f"Veri alma hatası (K:{user_id}, S:{guild_id}): {e}")
            return (0, 0, 0)

    async def _update_user_xp(self, guild_id: int, user_id: int, xp_to_add: int, boost: float = 1.0, message: discord.Message = None):
        """Kullanıcı XP'sini güncelle ve seviye atlamasını kontrol et."""
        boosted_xp = int(xp_to_add * boost)
        level, current_xp, total_xp = await self._get_user_data(guild_id, user_id)
        total_xp += boosted_xp
        current_xp += boosted_xp
        next_level_xp = self._calculate_xp_for_level(level + 1)
        
        level_up = False
        if current_xp >= next_level_xp:
            level += 1
            current_xp -= next_level_xp
            level_up = True
            self.logger.info(f"Kullanıcı seviye atladı: {user_id} (S:{guild_id}, Seviye: {level})")
            try:
                member = self.bot.get_guild(guild_id).get_member(user_id)
                if member:
                    await self._update_level_roles(member, guild_id, level)
                    # Embed ile tebrik mesajı
                    channel = message.channel if message else discord.utils.get(member.guild.text_channels, name="genel")
                    if channel and channel.permissions_for(member.guild.me).send_messages:
                        embed = discord.Embed(
                            title="🎉 Tebrikler!",
                            description=f"{member.mention}, seviye {level}'e ulaştın!",
                            color=discord.Color.gold()
                        )
                        embed.add_field(name="Önceki Seviye", value=str(level - 1), inline=True)
                        embed.add_field(name="Yeni Seviye", value=str(level), inline=True)
                        embed.set_thumbnail(url=member.avatar.url if member.avatar else None)
                        embed.set_footer(text=f"Toplam XP: {total_xp}")
                        await channel.send(embed=embed)
            except Exception as e:
                self.logger.error(f"Seviye rolü güncelleme hatası (K:{user_id}, S:{guild_id}): {e}")
        
        try:
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO users (user_id, guild_id, level, xp, total_xp) "
                    "VALUES ($1, $2, $3, $4, $5) "
                    "ON CONFLICT (user_id, guild_id) DO UPDATE "
                    "SET level = $3, xp = $4, total_xp = $5",
                    user_id, guild_id, level, current_xp, total_xp
                )
                self.logger.info(f"Kullanıcı XP güncellendi: {user_id} (S:{guild_id}, XP: {current_xp}, Total XP: {total_xp})")
        except Exception as e:
            self.logger.error(f"XP güncelleme hatası (K:{user_id}, S:{guild_id}): {e}")

    def _calculate_xp_for_level(self, level: int) -> int:
        """Bir sonraki seviye için gereken XP'yi hesapla."""
        if level < 0:
            return 0
        return 50 * (level ** 2) + (100 * level) + 200  # Daha zor bir seviye sistemi

    async def _update_level_roles(self, member: discord.Member, guild_id: int, level: int):
        """Seviye rollerini güncelle."""
        if "level_roles" not in self.config:
            self.logger.warning("Yapılandırmada 'level_roles' bulunamadı.")
            return
        
        level_role_map = self.config["level_roles"]
        roles_to_add = []
        roles_to_remove = []
        member_role_ids = {role.id for role in member.roles}
        bot_member = member.guild.me

        for level_threshold, role_id_str in level_role_map.items():
            try:
                level_threshold = int(level_threshold)
                role_id = int(role_id_str)
                role = member.guild.get_role(role_id)
                if not role:
                    self.logger.warning(f"Rol bulunamadı: {role_id} (S:{guild_id})")
                    continue
                if level >= level_threshold and role_id not in member_role_ids:
                    if role.position < bot_member.top_role.position:
                        roles_to_add.append(role)
                    else:
                        self.logger.warning(f"Rol {role.name} botun en yüksek rolünden yüksek, eklenemez!")
                elif level < level_threshold and role_id in member_role_ids:
                    if role.position < bot_member.top_role.position:
                        roles_to_remove.append(role)
                    else:
                        self.logger.warning(f"Rol {role.name} botun en yüksek rolünden yüksek, kaldırılamaz!")
            except ValueError:
                self.logger.error(f"Geçersiz seviye/rol ID: {level_threshold}/{role_id_str}")
        
        try:
            if roles_to_remove:
                await member.remove_roles(*roles_to_remove, reason="Seviye düşürüldü veya sıfırlandı")
                self.logger.info(f"{member.display_name}'dan roller kaldırıldı: {[r.name for r in roles_to_remove]}")
            if roles_to_add:
                await member.add_roles(*roles_to_add, reason=f"Seviye {level} ulaşıldı")
                self.logger.info(f"{member.display_name}'a roller eklendi: {[r.name for r in roles_to_add]}")
        except discord.Forbidden:
            self.logger.error(f"{member.display_name} için rol güncelleme izni yok.")
        except Exception as e:
            self.logger.error(f"Rol güncelleme hatası: {e}")

    async def _correct_member_level_roles(self, member: discord.Member, guild: discord.Guild):
        """Üyenin seviye rollerini düzelt."""
        level, _, _ = await self._get_user_data(guild.id, member.id)
        await self._update_level_roles(member, guild.id, level)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Mesaj gönderildiğinde XP kazandır."""
        if message.author.bot or not message.guild or message.channel.id in self.config["blacklisted_channels"]:
            return
        
        prefix = await self.bot.get_prefix(message)
        if isinstance(prefix, str):
            if message.content.startswith(prefix):
                return
        else:
            if any(message.content.startswith(p) for p in prefix):
                return
        
        user_id = message.author.id
        guild_id = message.guild.id
        current_time = discord.utils.utcnow().timestamp()
        
        if user_id not in self.cooldowns:
            self.cooldowns[user_id] = 0
        
        if current_time - self.cooldowns[user_id] >= self.config["xp_cooldown_seconds"]:
            xp_to_add = random.randint(self.config["xp_range"]["min"], self.config["xp_range"]["max"])
            boost = self._get_xp_boost(message.author)
            await self._update_user_xp(guild_id, user_id, xp_to_add, boost, message)
            self.cooldowns[user_id] = current_time

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        """Üye sunucudan çıktığında seviye ve XP verilerini sil."""
        try:
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM users WHERE user_id = $1 AND guild_id = $2",
                    member.id, member.guild.id
                )
                self.logger.info(f"Üye sunucudan ayrıldı, verileri silindi: {member.display_name} (ID: {member.id}, S: {member.guild.id})")
        except Exception as e:
            self.logger.error(f"Üye verileri silinirken hata (K:{member.id}, S:{member.guild.id}): {e}")

    def _get_xp_boost(self, member: discord.Member) -> float:
        """Kullanıcı veya rol için XP çarpanını al."""
        if "xp_boosts" not in self.config:
            return 1.0
        boost = 1.0
        boosts = self.config["xp_boosts"]
        user_boost = boosts.get(str(member.id))
        if user_boost:
            try:
                boost = max(boost, float(user_boost))
                if boost <= 0:
                    self.logger.warning(f"Geçersiz XP çarpanı: {user_boost} (K:{member.id})")
                    boost = 1.0
            except (ValueError, TypeError):
                self.logger.error(f"Geçersiz XP çarpanı: {user_boost} (K:{member.id})")
        for role in member.roles:
            role_boost = boosts.get(str(role.id))
            if role_boost:
                try:
                    boost = max(boost, float(role_boost))
                    if boost <= 0:
                        self.logger.warning(f"Geçersiz rol XP çarpanı: {role_boost} (R:{role.id})")
                        boost = 1.0
                except (ValueError, TypeError):
                    self.logger.error(f"Geçersiz rol XP çarpanı: {role_boost} (R:{role.id})")
        return boost

    @commands.command(name="seviye")
    async def level_command(self, ctx: commands.Context, member: discord.Member = None):
        """Kullanıcının seviyesini gösterir."""
        member = member or ctx.author
        level, xp, total_xp = await self._get_user_data(ctx.guild.id, member.id)
        xp_needed = self._calculate_xp_for_level(level + 1)
        progress = (xp / xp_needed) * 100 if xp_needed > 0 else 0
        progress_bar = f"[{'█' * int(progress // 5)}{'─' * (20 - int(progress // 5))}] ({progress:.1f}%)"
        
        async with self.db_pool.acquire() as conn:
            rank = await conn.fetchval(
                "SELECT COUNT(*) + 1 FROM users WHERE guild_id = $1 AND total_xp > $2",
                ctx.guild.id, total_xp
            ) or "N/A"

        level_role = None
        level_role_name = "Yok"
        level_role_map = self.config.get("level_roles", {})
        member_role_ids = {role.id for role in member.roles}
        
        highest_level = -1
        for level_threshold, role_id_str in level_role_map.items():
            try:
                level_threshold = int(level_threshold)
                if level >= level_threshold and level_threshold > highest_level:
                    role_id = int(role_id_str)
                    if role_id in member_role_ids:
                        role = ctx.guild.get_role(role_id)
                        if role:
                            level_role = role
                            highest_level = level_threshold
            except ValueError:
                self.logger.error(f"Geçersiz seviye/rol ID: {level_threshold}/{role_id_str}")
                continue
        
        if level_role:
            level_role_name = level_role.name
            embed_color = level_role.color if level_role.color.value != 0 else discord.Color.blue()
        else:
            embed_color = discord.Color.blue()
            self.logger.info(f"{member.display_name} (ID: {member.id}) için seviye rolü bulunamadı (Seviye: {level})")

        embed = discord.Embed(
            title=f"{member.display_name} Seviye Bilgisi",
            color=embed_color
        )
        embed.add_field(name="Seviye", value=str(level), inline=True)
        embed.add_field(name="XP", value=f"{xp}/{xp_needed}", inline=True)
        embed.add_field(name="Toplam XP", value=str(total_xp), inline=True)
        embed.add_field(name="Sıralama", value=str(rank), inline=True)
        embed.add_field(name="Seviye Rolü", value=level_role_name, inline=True)
        embed.add_field(name="İlerleme", value=progress_bar, inline=False)
        embed.set_thumbnail(url=member.avatar.url if member.avatar else None)
        await ctx.send(embed=embed)

    @commands.command(name="lider")
    async def leaderboard(self, ctx: commands.Context):
        """Sunucudaki kullanıcıların XP sıralamasını gösterir."""
        try:
            async with self.db_pool.acquire() as conn:
                # Top 10 kullanıcıyı toplam XP'ye göre sırala
                rows = await conn.fetch(
                    "SELECT user_id, total_xp, level FROM users WHERE guild_id = $1 ORDER BY total_xp DESC LIMIT 10",
                    ctx.guild.id
                )
                if not rows:
                    await ctx.send("⚠️ Bu sunucuda henüz sıralama yok!")
                    return

                embed = discord.Embed(
                    title="🏆 Liderlik Tablosu",
                    color=discord.Color.gold(),
                    timestamp=discord.utils.utcnow()
                )
                for index, row in enumerate(rows, 1):
                    user_id = row['user_id']
                    total_xp = row['total_xp']
                    level = row['level']
                    member = ctx.guild.get_member(user_id)
                    username = member.display_name if member else f"ID: {user_id}"
                    embed.add_field(
                        name=f"{index}. {username}",
                        value=f"Seviye: {level} | Toplam XP: {total_xp}",
                        inline=False
                    )
                embed.set_footer(text=f"Sunucu: {ctx.guild.name}")
                await ctx.send(embed=embed)
                self.logger.info(f"Liderlik tablosu görüntülendi (S:{ctx.guild.id})")
        except Exception as e:
            self.logger.error(f"Liderlik tablosu hatası (S:{ctx.guild.id}): {e}")
            await ctx.send("⚠️ Liderlik tablosu yüklenirken bir hata oluştu!")

    @commands.command(name="kanalac")
    @commands.has_permissions(manage_channels=True)
    async def remove_blacklist_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Bir kanalı XP kara listesinden kaldırır."""
        if channel.id in self.config["blacklisted_channels"]:
            self.config["blacklisted_channels"].remove(channel.id)
            self._save_config()
            await ctx.send(f"✅ {channel.mention} artık XP kara listesinden çıkarıldı!")
            self.logger.info(f"Kanal XP kara listesinden çıkarıldı: {channel.id} (S:{ctx.guild.id})")
        else:
            await ctx.send(f"❌ {channel.mention} zaten XP kara listesinde değil!")

    @commands.command(name="kanalengelle")
    @commands.has_permissions(manage_channels=True)
    async def blacklist_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Bir kanalı XP kazanımından kara listeye alır."""
        if channel.id not in self.config["blacklisted_channels"]:
            self.config["blacklisted_channels"].append(channel.id)
            self._save_config()
            await ctx.send(f"✅ {channel.mention} artık XP kazanımından kara listeye alındı!")
            self.logger.info(f"Kanal XP kara listesine eklendi: {channel.id} (S:{ctx.guild.id})")
        else:
            await ctx.send(f"❌ {channel.mention} zaten XP kara listesinde!")

    @commands.command(name="kapat")
    @commands.is_owner()
    async def shutdown(self, ctx: commands.Context):
        """Botu güvenli bir şekilde kapatır (Sadece sahip kullanabilir)."""
        await ctx.send("🔴 Bot kapatılıyor...")
        self.logger.info("Bot sahibi tarafından kapatıldı.")
        await self.bot.close()

    @commands.command(name="restart")
    @commands.is_owner()
    async def restart(self, ctx: commands.Context):
        """Botu yeniden başlatır (Sadece sahip kullanabilir)."""
        await ctx.send("🔄 Bot yeniden başlatılıyor...")
        self.logger.info("Bot sahibi tarafından yeniden başlatıldı.")
        await self.bot.close()  # Botu kapat, ana script yeniden başlatabilir

    @commands.command(name="seviyesifirla")
    @commands.has_permissions(manage_guild=True)
    async def reset_level(self, ctx: commands.Context, member: discord.Member):
        """Bir üyenin XP ve seviyesini sıfırlar."""
        try:
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE users SET level = 0, xp = 0, total_xp = 0 WHERE user_id = $1 AND guild_id = $2",
                    member.id, ctx.guild.id
                )
                await self._correct_member_level_roles(member, ctx.guild)
                await ctx.send(f"✅ {member.mention} için XP ve seviye sıfırlandı!")
                self.logger.info(f"{member.display_name} (ID: {member.id}) için seviye sıfırlandı (S:{ctx.guild.id})")
        except Exception as e:
            self.logger.error(f"Seviye sıfırlama hatası (K:{member.id}, S:{ctx.guild.id}): {e}")
            await ctx.send("⚠️ Seviye sıfırlama başarısız oldu!")

    @commands.command(name="uptime")
    @commands.is_owner()
    async def uptime(self, ctx: commands.Context):
        """Botun ne kadar süredir aktif olduğunu gösterir (Sadece Sahip)."""
        uptime_seconds = discord.utils.utcnow().timestamp() - self.bot.start_time
        days, remainder = divmod(int(uptime_seconds), 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = f"{days} gün, {hours} saat, {minutes} dakika, {seconds} saniye"
        await ctx.send(f"🕒 Bot {uptime_str} süredir aktif!")
        self.logger.info(f"Uptime komutu kullanıldı: {uptime_str}")

    @commands.command(name="xpayar")
    @commands.has_permissions(manage_guild=True)
    async def set_xp_range(self, ctx: commands.Context, min_xp: int, max_xp: int):
        """Mesajlar için XP aralığını ayarlar."""
        if min_xp > 0 and max_xp > min_xp:
            self.config["xp_range"]["min"] = min_xp
            self.config["xp_range"]["max"] = max_xp
            self._save_config()
            await ctx.send(f"✅ XP aralığı {min_xp}-{max_xp} olarak ayarlandı!")
            self.logger.info(f"XP aralığı ayarlandı: {min_xp}-{max_xp} (S:{ctx.guild.id})")
        else:
            await ctx.send("❌ Minimum XP 0'dan büyük olmalı ve maksimum XP minimumdan büyük olmalı!")

    @commands.command(name="xpboost")
    @commands.has_permissions(manage_guild=True)
    async def set_xp_boost(self, ctx: commands.Context, target: discord.Member, multiplier: float):
        """Bir kullanıcıya veya role XP çarpanı ayarlar."""
        if multiplier > 0:
            self.config["xp_boosts"][str(target.id)] = multiplier
            self._save_config()
            await ctx.send(f"✅ {target.mention} için XP çarpanı {multiplier}x olarak ayarlandı!")
            self.logger.info(f"XP çarpanı ayarlandı: {target.id} -> {multiplier}x (S:{ctx.guild.id})")
        else:
            await ctx.send("❌ XP çarpanı 0'dan büyük olmalı!")

    @commands.command(name="xpboostkaldir")
    @commands.has_permissions(manage_guild=True)
    async def remove_xp_boost(self, ctx: commands.Context, target: discord.Member):
        """Bir kullanıcıdan XP çarpanını kaldırır."""
        if str(target.id) in self.config["xp_boosts"]:
            del self.config["xp_boosts"][str(target.id)]
            self._save_config()
            await ctx.send(f"✅ {target.mention} için XP çarpanı kaldırıldı!")
            self.logger.info(f"XP çarpanı kaldırıldı: {target.id} (S:{ctx.guild.id})")
        else:
            await ctx.send(f"❌ {target.mention} için XP çarpanı zaten yok!")

    @commands.command(name="xpekle")
    @commands.has_permissions(manage_guild=True)
    async def add_xp(self, ctx: commands.Context, member: discord.Member, amount: int):
        """Belirli bir üyeye XP ekler."""
        if amount > 0:
            await self._update_user_xp(ctx.guild.id, member.id, amount, 1.0)
            await ctx.send(f"✅ {member.mention} için {amount} XP eklendi!")
            self.logger.info(f"{amount} XP eklendi: {member.id} (S:{ctx.guild.id})")
        else:
            await ctx.send("❌ XP miktarı 0'dan büyük olmalı!")

    async def cog_unload(self):
        """Cog kaldırıldığında veritabanı bağlantısını kapat."""
        if self.db_pool:
            await self.db_pool.close()
            self.logger.info("PostgreSQL veritabanı bağlantısı kapatıldı.")

async def setup(bot: commands.Bot):
    await bot.add_cog(LevelingCog(bot))