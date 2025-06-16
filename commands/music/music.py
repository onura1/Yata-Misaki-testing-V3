import discord
from discord.ext import commands
from discord import FFmpegPCMAudio, PCMVolumeTransformer
import logging
import os
import yt_dlp
import asyncio
from collections import deque
from typing import Optional, Dict, List
import re

# --- Logger Ayarları ---
log = logging.getLogger(__name__)

# --- Sabitler ve Ayarlar ---
# YENİ EKLENDİ: İşletim sistemine göre .exe uzantısını otomatik ekler.
FFMPEG_PATH_BASE = "./bin/ffmpeg"
FFMPEG_PATH = FFMPEG_PATH_BASE + ".exe" if os.name == 'nt' else FFMPEG_PATH_BASE

YTDL_FORMAT_OPTIONS = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0'
}
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}
ytdl = yt_dlp.YoutubeDL(YTDL_FORMAT_OPTIONS)

class Song:
    """Çalınacak bir şarkıyı temsil eden sınıf."""
    def __init__(self, data: Dict, requester: discord.Member):
        self.source_url = data.get('url')
        self.title = data.get('title', 'Bilinmeyen Başlık')
        self.duration = data.get('duration', 0)
        self.thumbnail = data.get('thumbnail')
        self.webpage_url = data.get('webpage_url')
        self.requester = requester

    def format_duration(self) -> str:
        """Süreyi MM:SS formatına çevirir."""
        if not self.duration:
            return "??:??"
        minutes, seconds = divmod(self.duration, 60)
        return f"{int(minutes):02d}:{int(seconds):02d}"

class MusicQueue:
    """Her sunucuya özel müzik kuyruğunu yöneten sınıf."""
    def __init__(self):
        self._queue = deque()
        self.current_song: Optional[Song] = None
        self.loop = False
        self.volume = 0.5  # Varsayılan ses seviyesi

    def add(self, song: Song):
        self._queue.append(song)

    def get_next(self) -> Optional[Song]:
        if not self._queue:
            return None
        return self._queue.popleft()

    def clear(self):
        self._queue.clear()

    @property
    def is_empty(self) -> bool:
        return not self._queue

    @property
    def queue_list(self) -> List[Song]:
        return list(self._queue)

class PlayerControls(discord.ui.View):
    """'Şimdi Çalıyor' mesajı için butonlar."""
    def __init__(self, music_cog, ctx):
        super().__init__(timeout=None)
        self.music_cog = music_cog
        self.ctx = ctx

    @discord.ui.button(label="Durdur", style=discord.ButtonStyle.red, emoji="🛑")
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.music_cog.stop.callback(self.music_cog, self.ctx)
        await interaction.response.defer()

    @discord.ui.button(label="Atla", style=discord.ButtonStyle.blurple, emoji="⏭️")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.music_cog.skip.callback(self.music_cog, self.ctx)
        await interaction.response.defer()

    @discord.ui.button(label="Döngü", style=discord.ButtonStyle.grey, emoji="🔁")
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.music_cog.loop.callback(self.music_cog, self.ctx)
        await interaction.response.defer()

    @discord.ui.button(label="Sırayı Göster", style=discord.ButtonStyle.grey, emoji="📜")
    async def queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.music_cog.queue.callback(self.music_cog, self.ctx)
        await interaction.response.defer() # Sadece mesajı göstermesi için defer yeterli


class MusicCog(commands.Cog, name="Müzik"):
    """Profesyonel Müzik Botu Eklentisi"""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.queues: Dict[int, MusicQueue] = {}
        self.play_locks: Dict[int, asyncio.Lock] = {}

        # --- YENİ EKLENDİ: Gelişmiş Hata Ayıklama Logları ---
        log.info("--- Müzik Cog Başlatılıyor: Hata Ayıklama Bilgileri ---")
        log.info(f"Botun çalıştığı ana dizin (CWD): {os.getcwd()}")
        log.info(f"Kontrol edilen FFmpeg yolu: {FFMPEG_PATH}")
        abs_path = os.path.abspath(FFMPEG_PATH)
        log.info(f"Aranan FFmpeg dosyasının tam (mutlak) yolu: {abs_path}")

        if not os.path.exists(FFMPEG_PATH):
            log.critical(f"KRİTİK HATA: FFmpeg dosyası belirtilen mutlak yolda bulunamadı!")
            log.critical("Lütfen yukarıdaki 'ana dizin' yolunun projenizin ana klasörü olduğundan emin olun.")
            log.critical("Eğer değilse, botu projenin ana klasöründeyken 'python main.py' komutuyla başlatın.")
        else:
            log.info(f"FFmpeg dosyası '{abs_path}' yolunda başarıyla bulundu.")
        log.info("----------------------------------------------------")


    def get_queue(self, guild_id: int) -> MusicQueue:
        if guild_id not in self.queues:
            self.queues[guild_id] = MusicQueue()
        return self.queues[guild_id]

    def get_lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self.play_locks:
            self.play_locks[guild_id] = asyncio.Lock()
        return self.play_locks[guild_id]
    
    async def _cleanup(self, guild_id: int):
        """Sunucudan ayrılırken kaynakları temizler."""
        if guild_id in self.queues:
            del self.queues[guild_id]
        if guild_id in self.play_locks:
            del self.play_locks[guild_id]
        log.info(f"{guild_id} ID'li sunucu için temizlik yapıldı.")

    async def _play_next(self, ctx: commands.Context):
        """Kuyruktaki bir sonraki şarkıyı çalar. Bu fonksiyon, sistemin kalbidir."""
        guild_id = ctx.guild.id
        queue = self.get_queue(guild_id)
        
        async with self.get_lock(guild_id):
            if ctx.voice_client is None or not ctx.voice_client.is_connected():
                return await self._cleanup(guild_id)

            next_song: Optional[Song] = None
            if queue.loop and queue.current_song:
                next_song = queue.current_song
            else:
                next_song = queue.get_next()

            if not next_song:
                log.info(f"{guild_id}: Kuyruk boş. 1 dakika içinde ayrılacak.")
                await ctx.send("📜 Kuyruk bitti. 1 dakika içinde kanaldan ayrılacağım.", delete_after=30)
                await asyncio.sleep(60)
                if ctx.voice_client and not ctx.voice_client.is_playing():
                     await ctx.voice_client.disconnect()
                return

            queue.current_song = next_song
            source = PCMVolumeTransformer(FFmpegPCMAudio(next_song.source_url, executable=FFMPEG_PATH, **FFMPEG_OPTIONS), volume=queue.volume)
            
            def after_playing(error):
                if error:
                    log.error(f"Şarkı çalınırken hata: {error}", exc_info=True)
                # Yarış durumu oluşturmamak için bot'un event loop'unda güvenli bir şekilde çalıştır.
                self.bot.loop.create_task(self._play_next(ctx))

            ctx.voice_client.play(source, after=after_playing)

            embed = discord.Embed(
                title="🎶 Şimdi Çalıyor",
                description=f"**[{next_song.title}]({next_song.webpage_url})**",
                color=discord.Color.blue()
            )
            embed.add_field(name="Süre", value=next_song.format_duration())
            embed.add_field(name="İsteyen", value=next_song.requester.mention)
            if next_song.thumbnail:
                embed.set_thumbnail(url=next_song.thumbnail)
            
            await ctx.send(embed=embed, view=PlayerControls(self, ctx))

    @commands.command(name='çal', aliases=['p', 'play'], help="Bir şarkıyı çalar veya sıraya ekler.")
    async def play(self, ctx: commands.Context, *, query: str):
        """YouTube veya Spotify linkinden/arama teriminden şarkı çalar."""
        if not ctx.author.voice:
            return await ctx.send("🎧 Bu komutu kullanmak için bir ses kanalında olmalısın!")

        voice_channel = ctx.author.voice.channel
        if ctx.voice_client and ctx.voice_client.channel != voice_channel:
            await ctx.voice_client.move_to(voice_channel)
        elif not ctx.voice_client:
            await voice_channel.connect()

        try:
            loop = self.bot.loop or asyncio.get_event_loop()
            data = await loop.run_in_executor(None, lambda: ytdl.extract_info(query, download=False))
            
            if 'entries' in data: # Bu bir çalma listesi
                song_list = data['entries']
                await ctx.send(f"✅ **{len(song_list)}** şarkılık bir çalma listesi bulundu ve sıraya ekleniyor...")
            else: # Tek bir şarkı
                song_list = [data]

            queue = self.get_queue(ctx.guild.id)
            for entry in song_list:
                song = Song(entry, ctx.author)
                queue.add(song)
            
            if len(song_list) == 1:
                await ctx.send(embed=discord.Embed(
                    description=f"✅ **Sıraya Eklendi:** [{song_list[0]['title']}]({song_list[0]['webpage_url']})",
                    color=discord.Color.green()
                ))

        except Exception as e:
            log.error(f"Şarkı alınırken hata: {e}", exc_info=True)
            return await ctx.send(f"⚠️ `{query}` aranırken bir hata oluştu. Lütfen tekrar deneyin.")

        if not ctx.voice_client.is_playing():
            await self._play_next(ctx)
            
    @commands.command(name='atla', aliases=['s', 'skip'], help="Mevcut şarkıyı atlar.")
    async def skip(self, ctx: commands.Context):
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.stop() # Bu, after_playing fonksiyonunu tetikler ve sıradakini başlatır.
            await ctx.message.add_reaction("⏭️")
        else:
            await ctx.send("❓ Atlanacak bir şarkı çalmıyor.")

    @commands.command(name='durdur', aliases=['stop', 'çık'], help="Müziği durdurur ve kanaldan ayrılır.")
    async def stop(self, ctx: commands.Context):
        if ctx.voice_client:
            queue = self.get_queue(ctx.guild.id)
            queue.clear()
            queue.loop = False
            ctx.voice_client.stop()
            await ctx.voice_client.disconnect()
            await self._cleanup(ctx.guild.id)
            await ctx.message.add_reaction("🛑")

    @commands.command(name='kuyruk', aliases=['q', 'queue'], help="Şarkı sırasını gösterir.")
    async def queue(self, ctx: commands.Context):
        queue = self.get_queue(ctx.guild.id)
        if not queue.current_song and queue.is_empty:
            return await ctx.send("📜 Kuyruk boş.")

        embed = discord.Embed(title="📜 Şarkı Sırası", color=discord.Color.purple())
        if queue.current_song:
            embed.add_field(
                name="🎶 Şimdi Çalıyor",
                value=f"**[{queue.current_song.title}]({queue.current_song.webpage_url})** | `{queue.current_song.format_duration()}` | İsteyen: {queue.current_song.requester.mention}",
                inline=False
            )
        
        if not queue.is_empty:
            song_list_str = ""
            for i, song in enumerate(queue.queue_list[:10]):
                song_list_str += f"`{i+1}.` **{song.title}** | `{song.format_duration()}`\n"
            
            if len(queue.queue_list) > 10:
                song_list_str += f"\n...ve **{len(queue.queue_list) - 10}** şarkı daha."

            embed.add_field(name="Sırada Bekleyenler", value=song_list_str, inline=False)
        
        await ctx.send(embed=embed)

    @commands.command(name='döngü', aliases=['loop'], help="Mevcut şarkıyı döngüye alır.")
    async def loop(self, ctx: commands.Context):
        queue = self.get_queue(ctx.guild.id)
        queue.loop = not queue.loop
        status = "açıldı" if queue.loop else "kapatıldı"
        await ctx.send(f"🔁 Döngü **{status}**.")
    
    @commands.command(name='ses', aliases=['v', 'volume'], help="Botun ses seviyesini ayarlar (1-150).")
    async def volume(self, ctx: commands.Context, level: int):
        if not ctx.voice_client or not ctx.voice_client.is_connected():
            return await ctx.send(" Bot bir ses kanalında değil.")
        
        if not 0 <= level <= 150:
            return await ctx.send("Ses seviyesi 0 ile 150 arasında olmalı.")
            
        queue = self.get_queue(ctx.guild.id)
        queue.volume = level / 100
        if ctx.voice_client.source:
            ctx.voice_client.source.volume = queue.volume
        await ctx.send(f"🔊 Ses seviyesi **%{level}** olarak ayarlandı.")

    @commands.command(name='çalan', aliases=['np', 'nowplaying'], help="Şu anda çalan şarkıyı gösterir.")
    async def nowplaying(self, ctx: commands.Context):
        queue = self.get_queue(ctx.guild.id)
        if not queue.current_song:
            return await ctx.send("Şu anda hiçbir şey çalmıyor.")
        
        embed = discord.Embed(
            title="🎶 Şimdi Çalıyor",
            description=f"**[{queue.current_song.title}]({queue.current_song.webpage_url})**",
            color=discord.Color.blue()
        )
        embed.add_field(name="Süre", value=queue.current_song.format_duration())
        embed.add_field(name="İsteyen", value=queue.current_song.requester.mention)
        if queue.current_song.thumbnail:
            embed.set_thumbnail(url=queue.current_song.thumbnail)
        await ctx.send(embed=embed)
        
    @commands.command(name='temizle', aliases=['clear'], help="Şarkı sırasını temizler.")
    async def clear(self, ctx: commands.Context):
        queue = self.get_queue(ctx.guild.id)
        queue.clear()
        await ctx.send("🗑️ Kuyruk temizlendi.")


async def setup(bot: commands.Bot):
    # Cog'u eklemeden önce FFmpeg dosyasının varlığını kontrol edelim
    if not os.path.exists(FFMPEG_PATH):
        # Hata ayıklama logları __init__ içine taşındığı için bu kontrol sade kalabilir.
        log.critical(f"Müzik Cog'u yüklenemedi çünkü FFmpeg '{FFMPEG_PATH}' yolunda bulunamadı.")
        return
    await bot.add_cog(MusicCog(bot))
