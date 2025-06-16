# commands/partner.py

import discord
from discord.ext import commands
import asyncpg # PostgreSQL için
import logging
import datetime
import re
import os
import pytz 
import asyncio # <-- BU SATIR BURADA OLMALI!
from typing import Optional, List, Tuple, Union, Dict

# --- Configuration & Constants ---
LOG_FILE = "partner_system.log"

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# Türkiye zaman dilimini tanımla (UTC+3)
TURKEY_TZ = pytz.timezone("Europe/Istanbul")

class PartnershipCog(commands.Cog):
    """Partnerlik ile ilgili komutları ve olayları yönetir."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.logger = logging.getLogger("PartnershipCog")
        self.db_pool: Optional[asyncpg.Pool] = None
        self.bot.loop.create_task(self._async_init_db())

    async def _async_init_db(self):
        """Asenkron PostgreSQL veritabanını başlat ve tabloyu oluştur."""
        await self.bot.wait_until_ready() # Bot hazır olana kadar bekle
        try:
            database_url = os.getenv("DATABASE_URL")
            if not database_url:
                self.logger.critical("DATABASE_URL çevresel değişkeni tanımlı değil. Veritabanı bağlantısı kurulamadı.")
                raise ValueError("DATABASE_URL çevresel değişkeni eksik.")
            
            self.db_pool = await asyncpg.create_pool(database_url)

            async with self.db_pool.acquire() as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS partners (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT NOT NULL,
                        guild_id BIGINT NOT NULL,
                        invite_link TEXT NOT NULL,
                        timestamp TIMESTAMP WITH TIME ZONE NOT NULL
                    )
                """)
                await conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_partner_user_id ON partners (user_id);
                """)
                await conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_partner_timestamp ON partners (timestamp);
                """)
                self.logger.info(f"PostgreSQL veritabanına bağlandı ve 'partners' tablosu kontrol edildi.")
        except asyncpg.exceptions.InvalidCatalogNameError:
            self.logger.critical("Geçersiz veritabanı adı. DATABASE_URL çevresel değişkenini kontrol edin.")
            raise
        except asyncpg.exceptions.ConnectionDoesNotExistError:
            self.logger.critical("Veritabanına bağlantı kurulamadı. DATABASE_URL veya ağ ayarlarını kontrol edin.")
            raise
        except Exception as e:
            self.logger.critical(f"Kritik veritabanı başlatma hatası: {type(e).__name__}: {e}")
            self.db_pool = None
            raise

    async def _add_partner_record(self, user_id: int, guild_id: int, invite_link: str, timestamp_utc: datetime.datetime):
        """Veritabanına yeni bir partner kaydı ekle (UTC zaman damgası)."""
        if not self.db_pool:
            self.logger.error("Veritabanı bağlantı havuzu hazır değil, partner kaydı eklenemiyor.")
            return

        try:
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO partners (user_id, guild_id, invite_link, timestamp) VALUES ($1, $2, $3, $4)",
                    user_id, guild_id, invite_link, timestamp_utc
                )
            self.logger.info(f"Partner kaydı eklendi: Kullanıcı {user_id}, Sunucu {guild_id}, Link {invite_link}, UTC Zaman {timestamp_utc}")
        except Exception as e:
            self.logger.error(f"Partner kaydı eklenirken hata: {type(e).__name__}: {e}")

    async def _get_partner_details(self, period: str) -> List[asyncpg.Record]:
        """Belirli bir dönem için partner ayrıntılarını al (UTC'ye göre filtrele)."""
        if not self.db_pool:
            self.logger.error("Veritabanı bağlantı havuzu hazır değil, partner detayları alınamıyor.")
            return []

        query = ""
        if period == "daily":
            query = """
                SELECT user_id, invite_link, timestamp FROM partners
                WHERE date_trunc('day', timestamp) = date_trunc('day', NOW() AT TIME ZONE 'UTC')
                ORDER BY timestamp DESC
            """
        elif period == "monthly":
            query = """
                SELECT user_id, invite_link, timestamp FROM partners
                WHERE date_trunc('month', timestamp) = date_trunc('month', NOW() AT TIME ZONE 'UTC')
                ORDER BY timestamp DESC
            """
        elif period == "yearly":
            query = """
                SELECT user_id, invite_link, timestamp FROM partners
                WHERE date_trunc('year', timestamp) = date_trunc('year', NOW() AT TIME ZONE 'UTC')
                ORDER BY timestamp DESC
            """
        else:
            self.logger.warning(f"Geçersiz dönem istendi: {period}")
            return []

        try:
            async with self.db_pool.acquire() as conn:
                rows = await conn.fetch(query)
            return rows
        except Exception as e:
            self.logger.error(f"Partner detayları alınırken hata: {type(e).__name__}: {e}")
            return []

    async def _get_partner_details_in_range(self, start_dt_utc: datetime.datetime, end_dt_utc: datetime.datetime) -> List[asyncpg.Record]:
        """Belirli bir tarih aralığındaki tüm partnerlik ayrıntılarını al."""
        if not self.db_pool:
            self.logger.error("Veritabanı bağlantı havuzu hazır değil, partner detayları alınamıyor.")
            return []
        
        query = """
            SELECT user_id, invite_link, timestamp FROM partners
            WHERE timestamp >= $1 AND timestamp <= $2
            ORDER BY timestamp DESC
        """
        try:
            async with self.db_pool.acquire() as conn:
                rows = await conn.fetch(query, start_dt_utc, end_dt_utc)
            return rows
        except Exception as e:
            self.logger.error(f"Tarih aralığında partner detayları alınırken hata: {type(e).__name__}: {e}")
            return []

    async def _get_top_partners_by_period(self, period: str, limit: int) -> List[asyncpg.Record]:
        """Belirli bir dönemde en çok partnerlik yapan kullanıcıları döndürür."""
        if not self.db_pool:
            self.logger.error("Veritabanı bağlantı havuzu hazır değil, en iyi partnerler alınamıyor.")
            return []
        
        query = ""
        if period == "daily":
            query = """
                SELECT user_id, COUNT(*) as count FROM partners
                WHERE date_trunc('day', timestamp) = date_trunc('day', NOW() AT TIME ZONE 'UTC')
                GROUP BY user_id ORDER BY count DESC LIMIT $1
            """
        elif period == "monthly":
            query = """
                SELECT user_id, COUNT(*) as count FROM partners
                WHERE date_trunc('month', timestamp) = date_trunc('month', NOW() AT TIME ZONE 'UTC')
                GROUP BY user_id ORDER BY count DESC LIMIT $1
            """
        elif period == "yearly":
            query = """
                SELECT user_id, COUNT(*) as count FROM partners
                WHERE date_trunc('year', timestamp) = date_trunc('year', NOW() AT TIME ZONE 'UTC')
                GROUP BY user_id ORDER BY count DESC LIMIT $1
            """
        else:
            self.logger.warning(f"Geçersiz dönem istendi: {period}")
            return []

        try:
            async with self.db_pool.acquire() as conn:
                rows = await conn.fetch(query, limit)
            return rows
        except Exception as e:
            self.logger.error(f"Dönemlik en iyi partnerler alınırken hata: {type(e).__name__}: {e}")
            return []

    async def _get_user_partner_counts(self, user_id: int) -> Dict[str, Union[int, str]]:
        """Belirli bir kullanıcının günlük, aylık, yıllık ve toplam partnerlik sayılarını ve genel sıralamasını döndürür."""
        if not self.db_pool:
            self.logger.error("Veritabanı bağlantı havuzu hazır değil, kullanıcı partnerlik sayıları alınamıyor.")
            return {
                "daily": 0, "monthly": 0, "yearly": 0, "total": 0, "rank": "N/A"
            }

        try:
            async with self.db_pool.acquire() as conn:
                daily_count = await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM partners
                    WHERE user_id = $1 AND date_trunc('day', timestamp) = date_trunc('day', NOW() AT TIME ZONE 'UTC')
                    """,
                    user_id
                ) or 0

                monthly_count = await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM partners
                    WHERE user_id = $1 AND date_trunc('month', timestamp) = date_trunc('month', NOW() AT TIME ZONE 'UTC')
                    """,
                    user_id
                ) or 0

                yearly_count = await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM partners
                    WHERE user_id = $1 AND date_trunc('year', timestamp) = date_trunc('year', NOW() AT TIME ZONE 'UTC')
                    """,
                    user_id
                ) or 0

                total_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM partners WHERE user_id = $1",
                    user_id
                ) or 0

                rank_query = """
                    WITH UserPartners AS (
                        SELECT user_id, COUNT(*) AS total_partners_count
                        FROM partners
                        GROUP BY user_id
                    )
                    SELECT rank_alias.rank_num FROM (
                        SELECT user_id, DENSE_RANK() OVER (ORDER BY total_partners_count DESC) as rank_num
                        FROM UserPartners
                    ) AS rank_alias
                    WHERE rank_alias.user_id = $1;
                """
                rank = await conn.fetchval(rank_query, user_id)
                rank_str = str(rank) if rank is not None else "Yok"

                return {
                    "daily": daily_count,
                    "monthly": monthly_count,
                    "yearly": yearly_count,
                    "total": total_count,
                    "rank": rank_str
                }
        except Exception as e:
            self.logger.error(f"Kullanıcı partnerlik sayıları alınırken hata (K:{user_id}): {type(e).__name__}: {e}")
            return {
                "daily": 0, "monthly": 0, "yearly": 0, "total": 0, "rank": "Hata"
            }


    async def _get_server_name_from_invite(self, invite_link: str) -> Optional[str]:
        """Bir Discord davet linkinden sunucu adını al."""
        try:
            invite = await self.bot.fetch_invite(invite_link)
            return invite.guild.name if invite.guild else "Bilinmeyen Sunucu"
        except discord.errors.NotFound:
            self.logger.warning(f"Geçersiz davet linki veya süresi dolmuş: {invite_link}")
            return "Geçersiz/Süresi Dolmuş Link"
        except discord.errors.Forbidden:
            self.logger.error(f"Botun davet linkine erişim izni yok: {invite_link}")
            return "Erişim Yok"
        except ValueError as ve: # Yakaladığınız özel hata
            self.logger.error(f"Davet linki kontrol edilirken ValueError: {ve} (Link: {invite_link})")
            return "Geçersiz Karakter Hatası"
        except Exception as e:
            self.logger.error(f"Davet linkinden sunucu adı alınırken beklenmeyen hata: {type(e).__name__}: {e}")
            return "Hata"

    # --- Event Listener ---
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Partner kanalındaki davet linklerini tespit eder ve partnerlik kaydını tutar."""

        if message.author.bot or message.guild is None:
            return

        partner_channel_id = self.bot.config.get("PARTNER_CHANNEL_ID")
        if not partner_channel_id:
            self.logger.error("[Partner Sistemi] Yapılandırmada 'PARTNER_CHANNEL_ID' bulunamadı. Lütfen ana bot yapılandırmasına ekleyin.")
            return

        try:
            partner_channel_id = int(partner_channel_id)
        except ValueError:
            self.logger.error(f"[Partner Sistemi] 'PARTNER_CHANNEL_ID' değeri geçersiz bir sayı değil: {partner_channel_id}")
            return

        if message.channel.id != partner_channel_id:
            return

        # Daha katı regex: Sadece geçerli davet kodu karakterlerini yakalar
        invite_pattern = r"(?:https?://)?discord\.gg/([a-zA-Z0-9-]{6,})" 
        # re.finditer ile tüm eşleşen kodları al
        found_codes = [match.group(1) for match in re.finditer(invite_pattern, message.content)]

        if not found_codes:
            return

        for invite_code in found_codes: # Yakalanan her davet kodu için
            invite_guild_name = "Bilinmiyor"
            guild_id_from_invite = None
            try:
                # fetch_invite'a sadece davet kodunu gönder (daha güvenilir)
                invite_obj = await self.bot.fetch_invite(invite_code)
                if not invite_obj.guild:
                    self.logger.warning(f"Davet linki bir sunucuya ait değil (Grup sohbeti vb.): {invite_code}")
                    continue
                invite_guild_name = invite_obj.guild.name
                guild_id_from_invite = invite_obj.guild.id
            except ValueError as ve: # Davet linki içinde izin verilmeyen karakterler hatası
                self.logger.error(f"Davet linki kontrol edilirken ValueError: {ve} (Kullanıcının gönderdiği kod: {invite_code})")
                continue
            except discord.errors.NotFound:
                self.logger.warning(f"Davet linki geçersiz veya süresi dolmuş: {invite_code}")
                continue
            except discord.errors.Forbidden:
                self.logger.warning(f"Botun davet linkine erişim izni yok: {invite_code}")
                continue
            except Exception as e:
                self.logger.error(f"Davet linki kontrol edilirken beklenmeyen hata: {type(e).__name__}: {e} (Kod: {invite_code})")
                continue

            # Veritabanına kaydederken tam URL'yi veya sadece kodu kaydedebilirsiniz.
            # Şu anki haliyle `invite_link` değişkeni tam `discord.gg/kod` formatında değil,
            # `invite_code` sadece kodu içeriyor. Veritabanına tutarlı bir format kaydetmek önemli.
            # `f"https://discord.gg/{invite_code}"` kaydetmek daha iyi olur.
            await self._add_partner_record(message.author.id, message.guild.id, f"https://discord.gg/{invite_code}", message.created_at)

            embed = discord.Embed(
                title="🎯 Yeni bir partnerlik bildirimi!",
                color=discord.Color.red(),
                timestamp=message.created_at.astimezone(TURKEY_TZ)
            )
            if message.guild.icon:
                embed.set_thumbnail(url=message.guild.icon.url)

            partner_image_url = self.bot.config.get("PARTNER_IMAGE_URL", "")
            if partner_image_url:
                embed.set_image(url=partner_image_url)

            embed.add_field(
                name=f"👋 Partnerliği yapan: {message.author.display_name}",
                value=(
                    f"🔥 Partnerlik yapılan sunucu: **{invite_guild_name}**\n"
                    f"🆔 Sunucu ID: {guild_id_from_invite}\n"
                    f"⏰ Partnerlik Zamanı: {message.created_at.astimezone(TURKEY_TZ).strftime('%Y-%m-%d %H:%M:%S')}"
                ),
                inline=False
            )
            embed.set_footer(text=f"ID: {message.author.id}")

            try:
                await message.channel.send(embed=embed)
                await message.add_reaction("🤝")
            except discord.Forbidden:
                self.logger.error(f"[Hata] {message.channel.name} kanalına mesaj gönderme veya tepki ekleme izni yok.")
            except discord.HTTPException as e:
                self.logger.error(f"Partnerlik bildirimi gönderilirken bir HTTP hatası oluştu: {e}")

    # --- Commands ---
    @commands.command(name="partnerstats")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def partner_stats_command(self, ctx: commands.Context):
        """
        Sunucudaki tüm partnerliklerin detaylı listesini (günlük, aylık, yıllık) gösterir.
        Hangi kullanıcının hangi sunucu ile ne zaman partnerlik yaptığını listeler.
        """
        if not ctx.guild:
            await ctx.send("Bu komut sadece sunucularda kullanılabilir.")
            return
        if not self.db_pool:
            await ctx.send("Veritabanı bağlantısı hazır değil, istatistikler alınamıyor. Lütfen bot sahibine bildirin.")
            return

        daily_partners = await self._get_partner_details("daily")
        monthly_partners = await self._get_partner_details("monthly")
        yearly_partners = await self._get_partner_details("yearly")

        embed = discord.Embed(
            title=f"{ctx.guild.name} Partner İstatistikleri",
            color=discord.Color.red()
        )
        if ctx.guild.icon:
            embed.set_thumbnail(url=ctx.guild.icon.url)

        async def format_partner_records(records: List[asyncpg.Record]):
            formatted_list = []
            for record in records:
                user_id = record['user_id']
                invite_link = record['invite_link']
                timestamp_utc = record['timestamp'] 
                
                member = ctx.guild.get_member(user_id)
                user_name = member.display_name if member else f"Ayrılmış Üye (ID: {user_id})"
                
                timestamp_tr = timestamp_utc.astimezone(TURKEY_TZ)
                
                server_name = await self._get_server_name_from_invite(invite_link)
                formatted_list.append(f"**{server_name}** - {user_name} - {timestamp_tr.strftime('%Y-%m-%d %H:%M:%S')}")
            return "\n".join(formatted_list)

        embed.add_field(
            name=f"Günlük Partnerlikler ({len(daily_partners)})",
            value=await format_partner_records(daily_partners) if daily_partners else "Bugün partnerlik yapılmamış.",
            inline=False
        )

        embed.add_field(
            name=f"Aylık Partnerlikler ({len(monthly_partners)})",
            value=await format_partner_records(monthly_partners) if monthly_partners else "Bu ay partnerlik yapılmamış.",
            inline=False
        )

        embed.add_field(
            name=f"Yıllık Partnerlikler ({len(yearly_partners)})",
            value=await format_partner_records(yearly_partners) if yearly_partners else "Bu yıl partnerlik yapılmamış.",
            inline=False
        )

        embed.set_footer(text=f"Rapor Tarihi (TR): {datetime.datetime.now(TURKEY_TZ).strftime('%Y-%m-%d %H:%M:%S')}")
        await ctx.send(embed=embed)

    @commands.command(name="partnerleaderboard")
    @commands.cooldown(1, 10, commands.BucketType.guild)
    async def partner_leaderboard_command(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """
        Belirtilen bir kullanıcının (veya sizin) partnerlik istatistiklerini gösterir.
        Günlük, aylık, yıllık ve toplam partnerlik sayılarının yanı sıra, genel sıralamasını da içerir.
        Örn: !partnerleaderboard @kullanıcı
        """
        member = member or ctx.author
        
        if not ctx.guild:
            await ctx.send("Bu komut sadece sunucularda kullanılabilir.")
            return
        if not self.db_pool:
            await ctx.send("Veritabanı bağlantısı hazır değil, istatistikler alınamıyor. Lütfen bot sahibine bildirin.")
            return
        if member.bot:
            await ctx.send("Botların partnerlik istatistikleri tutulmaz.")
            return

        user_stats = await self._get_user_partner_counts(member.id)

        embed = discord.Embed(
            title=f"🏆 {member.display_name} Partnerlik İstatistikleri",
            color=discord.Color.red()
        )
        embed.set_thumbnail(url=member.avatar.url if member.avatar else None)

        embed.add_field(name="Günlük Partnerlik", value=str(user_stats['daily']), inline=True)
        embed.add_field(name="Aylık Partnerlik", value=str(user_stats['monthly']), inline=True)
        embed.add_field(name="Yıllık Partnerlik", value=str(user_stats['yearly']), inline=True)
        embed.add_field(name="Toplam Partnerlik", value=str(user_stats['total']), inline=True)
        embed.add_field(name="Genel Sıralama", value=str(user_stats['rank']), inline=True)
        
        embed.set_footer(text=f"Rapor Tarihi (TR): {datetime.datetime.now(TURKEY_TZ).strftime('%Y-%m-%d %H:%M:%S')}")
        await ctx.send(embed=embed)

    @commands.command(name="partnertop")
    @commands.cooldown(1, 15, commands.BucketType.guild)
    async def partner_top_command(self, ctx: commands.Context, period: str, limit: Optional[int] = 10):
        """
        Belirli bir döneme göre en çok partnerlik yapan kullanıcıların liderlik tablosunu gösterir.
        Dönemler: 'günlük', 'aylık', 'yıllık'. Limit isteğe bağlıdır (varsayılan: 10, max: 50).
        Örn: !partnertop aylık 5
        """
        if not ctx.guild:
            await ctx.send("Bu komut sadece sunucularda kullanılabilir.")
            return
        if not self.db_pool:
            await ctx.send("Veritabanı bağlantısı hazır değil, lider tablosu alınamıyor. Lütfen bot sahibine bildirin.")
            return
        if period.lower() not in ["günlük", "aylık", "yıllık"]:
            return await ctx.send("❌ Geçersiz dönem! Lütfen 'günlük', 'aylık' veya 'yıllık' kullanın.")
        if limit < 1 or limit > 50:
            return await ctx.send("❌ Limit 1 ile 50 arasında olmalı.")

        top_partners = await self._get_top_partners_by_period(period.lower(), limit)

        embed = discord.Embed(
            title=f"🏆 {ctx.guild.name} {period.capitalize()} Partner Lider Tablosu ({limit} Kişi)",
            color=discord.Color.red()
        )
        if ctx.guild.icon:
            embed.set_thumbnail(url=ctx.guild.icon.url)

        if not top_partners:
            embed.description = f"Bu {period} döneminde henüz kimse partnerlik yapmamış."
        else:
            description = ""
            for rank, record in enumerate(top_partners, start=1):
                user_id = record['user_id']
                count = record['count']
                member = ctx.guild.get_member(user_id)
                member_name = member.display_name if member else f"Ayrılmış Üye (ID: {user_id})"
                description += f"**{rank}.** {member_name} - **{count}** partnerlik\n"
            embed.description = description

        embed.set_footer(text=f"Rapor Tarihi (TR): {datetime.datetime.now(TURKEY_TZ).strftime('%Y-%m-%d %H:%M:%S')}")
        await ctx.send(embed=embed)

    @commands.command(name="partnerstats_aralık")
    @commands.cooldown(1, 15, commands.BucketType.guild)
    async def partner_stats_range_command(self, ctx: commands.Context, start_date_str: str, end_date_str: Optional[str] = None):
        """
        Belirtilen tarih aralığındaki tüm partnerlikleri listeler.
        Tarih formatı: YYYY-MM-DD
        Örn: !partnerstats_aralık 2024-01-01 2024-01-31
        Örn: !partnerstats_aralık 2024-05-01 (belirtilen tarihten bugüne kadar)
        """
        if not ctx.guild:
            await ctx.send("Bu komut sadece sunucularda kullanılabilir.")
            return
        if not self.db_pool:
            await ctx.send("Veritabanı bağlantısı hazır değil, istatistikler alınamıyor. Lütfen bot sahibine bildirin.")
            return

        try:
            start_date = datetime.datetime.strptime(start_date_str, "%Y-%m-%d").date()
            if end_date_str:
                end_date = datetime.datetime.strptime(end_date_str, "%Y-%m-%d").date()
            else:
                end_date = datetime.datetime.now(TURKEY_TZ).date()

            start_dt_utc = datetime.datetime.combine(start_date, datetime.time.min, tzinfo=TURKEY_TZ).astimezone(datetime.timezone.utc)
            end_dt_utc = datetime.datetime.combine(end_date, datetime.time.max, tzinfo=TURKEY_TZ).astimezone(datetime.timezone.utc)
            
            if start_dt_utc > end_dt_utc:
                return await ctx.send("❌ Başlangıç tarihi bitiş tarihinden sonra olamaz.")

        except ValueError:
            return await ctx.send("❌ Geçersiz tarih formatı! Lütfen 'YYYY-MM-DD' formatını kullanın.")

        partners_in_range = await self._get_partner_details_in_range(start_dt_utc, end_dt_utc)

        embed = discord.Embed(
            title=f"{ctx.guild.name} Partner İstatistikleri ({start_date_str} - {end_date.strftime('%Y-%m-%d')})",
            color=discord.Color.red()
        )
        if ctx.guild.icon:
            embed.set_thumbnail(url=ctx.guild.icon.url)

        async def format_partner_records(records: List[asyncpg.Record]):
            formatted_list = []
            for record in records:
                user_id = record['user_id']
                invite_link = record['invite_link']
                timestamp_utc = record['timestamp']
                
                member = ctx.guild.get_member(user_id)
                user_name = member.display_name if member else f"Ayrılmış Üye (ID: {user_id})"
                
                timestamp_tr = timestamp_utc.astimezone(TURKEY_TZ)
                
                server_name = await self._get_server_name_from_invite(invite_link)
                formatted_list.append(f"**{server_name}** - {user_name} - {timestamp_tr.strftime('%Y-%m-%d %H:%M:%S')}")
            return "\n".join(formatted_list)

        # Mesaj limiti nedeniyle uzun listeleri ayırma (Discord'un 2000 karakter limiti vardır)
        if len(partners_in_range) > 20: # Örneğin 20'den fazla kayıt varsa uyarı ver
            # Discord field value limiti 1024 karakterdir. Bu yüzden çok uzun listelerde sorun olabilir.
            # Daha fazla kayıt varsa, pagination (sayfalama) veya bir dosya olarak gönderme düşünülebilir.
            await ctx.send(f"⚠️ Belirtilen aralıkta çok fazla partnerlik ({len(partners_in_range)} adet) bulundu. Sadece ilk 20 tanesi gösterilecektir. Daha fazla detayı loglardan inceleyebilirsiniz.")
            partners_to_display = partners_in_range[:20]
        else:
            partners_to_display = partners_in_range

        embed.add_field(
            name=f"Partnerlikler ({len(partners_in_range)} toplam)",
            value=await format_partner_records(partners_to_display) if partners_to_display else "Bu tarih aralığında partnerlik yapılmamış.",
            inline=False
        )
        
        embed.set_footer(text=f"Rapor Tarihi (TR): {datetime.datetime.now(TURKEY_TZ).strftime('%Y-%m-%d %H:%M:%S')}")
        await ctx.send(embed=embed)

    @commands.command(name="partnerreset")
    @commands.has_permissions(manage_guild=True)
    @commands.cooldown(1, 30, commands.BucketType.guild)
    async def partner_reset_command(self, ctx: commands.Context, target: Optional[Union[discord.Member, str]] = None):
        """
        Partnerlik verilerini sıfırlar. Yönetici komutudur.
        - !partnerreset hepsi: Tüm sunucudaki partnerlik verilerini sıfırlar.
        - !partnerreset @kullanıcı: Belirli bir kullanıcının partnerlik verilerini sıfırlar.
        """
        if not ctx.guild:
            await ctx.send("Bu komut sadece sunucularda kullanılabilir.")
            return
        if not self.db_pool:
            await ctx.send("Veritabanı bağlantısı hazır değil. Lütfen bot sahibine bildirin.")
            return

        if target is None:
            return await ctx.send("❌ Lütfen sıfırlanacak hedefi belirtin: `hepsi` veya bir `@kullanıcı` etiketi.")
        
        confirmation_message = ""
        success_message = ""
        log_message = ""
        
        try:
            async with self.db_pool.acquire() as conn:
                if isinstance(target, str) and target.lower() == "hepsi":
                    confirmation_message = "⚠️ **UYARI:** Bu işlem tüm sunucudaki partnerlik verilerini kalıcı olarak silecektir. Emin misiniz? Onaylamak için `evet` yazın."
                    await ctx.send(confirmation_message)
                    
                    def check(m):
                        return m.author == ctx.author and m.channel == ctx.channel and m.content.lower() == 'evet'
                    
                    try:
                        await self.bot.wait_for('message', check=check, timeout=30.0)
                        await conn.execute("DELETE FROM partners WHERE guild_id = $1", ctx.guild.id)
                        success_message = "✅ Tüm partnerlik verileri başarıyla sıfırlandı!"
                        log_message = f"Tüm partnerlik verileri sıfırlandı (Sunucu: {ctx.guild.id})"
                    except asyncio.TimeoutError:
                        return await ctx.send("İşlem zaman aşımına uğradı, sıfırlama iptal edildi.")
                
                elif isinstance(target, discord.Member):
                    if target.bot:
                        return await ctx.send("Botların partnerlik verileri sıfırlanamaz.")
                    confirmation_message = f"⚠️ **UYARI:** {target.mention} kullanıcısının tüm partnerlik verilerini kalıcı olarak silecektir. Emin misiniz? Onaylamak için `evet` yazın."
                    await ctx.send(confirmation_message)

                    def check(m):
                        return m.author == ctx.author and m.channel == ctx.channel and m.content.lower() == 'evet'
                    
                    try:
                        await self.bot.wait_for('message', check=check, timeout=30.0)
                        await conn.execute(
                            "DELETE FROM partners WHERE user_id = $1 AND guild_id = $2",
                            target.id, ctx.guild.id
                        )
                        success_message = f"✅ {target.mention} kullanıcısının partnerlik verileri başarıyla sıfırlandı!"
                        log_message = f"{target.display_name} (ID: {target.id}) kullanıcısının partnerlik verileri sıfırlandı (Sunucu: {ctx.guild.id})"
                    except asyncio.TimeoutError:
                        return await ctx.send("İşlem zaman aşımına uğradı, sıfırlama iptal edildi.")
                else:
                    return await ctx.send("❌ Geçersiz hedef! Lütfen 'hepsi' yazın veya bir kullanıcıyı etiketleyin.")
            
            if success_message:
                await ctx.send(success_message)
                self.logger.info(log_message)

        except Exception as e:
            self.logger.error(f"Partnerlik sıfırlama hatası: {type(e).__name__}: {e} (Kullanıcı: {ctx.author.id}, Hedef: {target})")
            await ctx.send("❓ Verileri sıfırlarken bir hata oluştu.")


    # --- Error Handling ---
    @partner_stats_command.error
    async def partner_stats_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ Bu komutu tekrar kullanmak için **{error.retry_after:.1f}** saniye beklemeniz gerekiyor.", ephemeral=True)
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("Bu komut sadece sunucularda kullanılabilir.")
        elif isinstance(error, commands.MissingPermissions):
            await ctx.send("Bu komutu kullanmak için gerekli izinlere sahip değilsiniz.")
        else:
            self.logger.error(f"partnerstats komut hatası: {type(error).__name__}: {error} (Kullanıcı: {ctx.author.id}, Sunucu: {ctx.guild.id if ctx.guild else 'DM'})")
            await ctx.send("❓ Komut yürütülürken bir hata oluştu. Lütfen daha sonra tekrar deneyin veya bot sahibine bildirin.")

    @partner_leaderboard_command.error
    async def partner_leaderboard_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ Bu komutu tekrar kullanmak için **{error.retry_after:.1f}** saniye beklemeniz gerekiyor.", ephemeral=True)
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("Bu komut sadece sunucularda kullanılabilir.")
        elif isinstance(error, commands.MissingPermissions):
            await ctx.send("Bu komutu kullanmak için gerekli izinlere sahip değilsiniz.")
        elif isinstance(error, commands.MemberNotFound):
            await ctx.send("Belirtilen üye bulunamadı.")
        else:
            self.logger.error(f"partnerleaderboard komut hatası: {type(error).__name__}: {error} (Kullanıcı: {ctx.author.id}, Sunucu: {ctx.guild.id if ctx.guild else 'DM'})")
            await ctx.send("❓ Komut yürütülürken bir hata oluştu. Lütfen daha sonra tekrar deneyin veya bot sahibine bildirin.")
            
    @partner_top_command.error
    async def partner_top_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ Bu komutu tekrar kullanmak için **{error.retry_after:.1f}** saniye beklemeniz gerekiyor.", ephemeral=True)
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("Bu komut sadece sunucularda kullanılabilir.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"❌ Eksik argüman: `{error.param.name}`. Kullanım: `!partnertop <günlük|aylık|yıllık> [limit]`")
        else:
            self.logger.error(f"partnertop komut hatası: {type(error).__name__}: {error} (Kullanıcı: {ctx.author.id}, Sunucu: {ctx.guild.id if ctx.guild else 'DM'})")
            await ctx.send("❓ Komut yürütülürken bir hata oluştu. Lütfen daha sonra tekrar deneyin veya bot sahibine bildirin.")

    @partner_stats_range_command.error
    async def partner_stats_range_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ Bu komutu tekrar kullanmak için **{error.retry_after:.1f}** saniye beklemeniz gerekiyor.", ephemeral=True)
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("Bu komut sadece sunucularda kullanılabilir.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"❌ Eksik argüman: `{error.param.name}`. Kullanım: `!partnerstats_aralık <başlangıç_tarihi> [bitiş_tarihi]`")
        else:
            self.logger.error(f"partnerstats_aralık komut hatası: {type(error).__name__}: {error} (Kullanıcı: {ctx.author.id}, Sunucu: {ctx.guild.id if ctx.guild else 'DM'})")
            await ctx.send("❓ Komut yürütülürken bir hata oluştu. Lütfen daha sonra tekrar deneyin veya bot sahibine bildirin.")

    @partner_reset_command.error
    async def partner_reset_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ Bu komutu kullanmak için `Sunucuyu Yönet` yetkisine sahip olmalısınız.")
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("Bu komut sadece sunucularda kullanılabilir.")
        elif isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ Bu komutu tekrar kullanmak için **{error.retry_after:.1f}** saniye beklemeniz gerekiyor.", ephemeral=True)
        elif isinstance(error, commands.MemberNotFound):
            await ctx.send("Belirtilen üye bulunamadı.")
        else:
            self.logger.error(f"partnerreset komut hatası: {type(error).__name__}: {error} (Kullanıcı: {ctx.author.id}, Sunucu: {ctx.guild.id if ctx.guild else 'DM'})")
            await ctx.send("❓ Komut yürütülürken bir hata oluştu. Lütfen daha sonra tekrar deneyin veya bot sahibine bildirin.")


    # --- Cog Lifecycle ---
    async def cog_unload(self):
        """Clean up when the cog is unloaded."""
        if self.db_pool:
            await self.db_pool.close()
            self.logger.info("Cog kaldırıldı, DB bağlantı havuzu kapatıldı.")

async def setup(bot: commands.Bot):
    """Setup function to load the cog."""
    try:
        import asyncpg
        import pytz
        import asyncio # Bu satırın burada olduğundan emin olun!
    except ImportError as e:
        logging.error(f"Gerekli modüllerden biri bulunamadı: {e}. Partner sistemi ÇALIŞMAYACAK. Lütfen 'pip install asyncpg pytz' komutunu çalıştırın.")
        return
    
    if not hasattr(bot, 'config') or not isinstance(bot.config, dict) or "PARTNER_CHANNEL_ID" not in bot.config:
        logging.critical("Botun ana yapılandırmasında 'PARTNER_CHANNEL_ID' bulunamadı. Partner sistemi düzgün çalışmayabilir.")

    await bot.add_cog(PartnershipCog(bot))
    print("✅ Partnership Cog yüklendi!")