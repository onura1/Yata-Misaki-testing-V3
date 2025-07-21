import asyncio
import json
import logging
import os
import time
import traceback
from pathlib import Path

import asyncpg
import discord
from discord.ext import commands
from dotenv import load_dotenv

# --- Temel Ayarlar ---
load_dotenv()
BASE_DIR = Path(__file__).parent

# --- Yapılandırma Yükleme ---
CONFIG_FILE_PATH = BASE_DIR / "config.json"
try:
    with open(CONFIG_FILE_PATH, 'r', encoding='utf-8') as f:
        config = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    config = {"PREFIX": "!"}
    print("UYARI: config.json bulunamadı veya bozuk. Varsayılan prefix '!' kullanılıyor.")

BOT_TOKEN = os.getenv("DISCORD_TOKEN")
DEFAULT_PREFIX = config.get("PREFIX", "!")
BOT_LOG_CHANNEL_ID = config.get("BOT_LOG_CHANNEL_ID")

# --- Loglama Sistemi ---
class DiscordLogHandler(logging.Handler):
    """Logları belirli bir Discord kanalına gönderen özel handler."""
    def __init__(self, bot_instance, log_channel_id):
        super().__init__()
        self.bot = bot_instance
        self.log_channel_id = log_channel_id
        self.queue = asyncio.Queue()
        self.task = None
        self.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(name)s - %(message)s'))

    async def _log_sender(self):
        await self.bot.wait_until_ready()
        channel = self.bot.get_channel(self.log_channel_id)
        if not channel:
            print(f"HATA: Log kanalı (ID: {self.log_channel_id}) bulunamadı. Kanal loglama devre dışı.")
            return

        while not self.bot.is_closed():
            try:
                record = await self.queue.get()
                if record is None:  # Kapatma sinyali
                    break
                msg = self.format(record)
                # Mesajları 2000 karakter limitine göre böl
                for chunk in [msg[i:i+1990] for i in range(0, len(msg), 1990)]:
                    await channel.send(f"```{chunk}```")
            except Exception as e:
                print(f"Log gönderme hatası: {e}")

    def start(self):
        self.task = self.bot.loop.create_task(self._log_sender())

    def emit(self, record):
        if self.bot.loop.is_running():
            self.queue.put_nowait(record)

    def close(self):
        super().close()
        if self.task:
            self.queue.put_nowait(None) # Sinyal göndererek döngüyü sonlandır

# Logger'ı kur
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Konsol handler'ı
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(name)s - %(message)s'))
logger.addHandler(console_handler)

# --- Dinamik Prefix Fonksiyonu ---
async def get_prefix(bot, message):
    if not message.guild:
        return commands.when_mentioned_or(DEFAULT_PREFIX)(bot, message)
    if not bot.db:
        return commands.when_mentioned_or(DEFAULT_PREFIX)(bot, message)
    prefix = await bot.db.fetchval("SELECT prefix FROM guild_settings WHERE guild_id = $1", message.guild.id)
    return commands.when_mentioned_or(prefix or DEFAULT_PREFIX)(bot, message)

# --- Ana Bot Sınıfı ---
class YataMisakiBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.config = config
        self.start_time = time.time()
        self.db = None
        self.discord_log_handler = None

    async def setup_hook(self):
        # Discord log handler'ını kur
        if BOT_LOG_CHANNEL_ID:
            self.discord_log_handler = DiscordLogHandler(self, int(BOT_LOG_CHANNEL_ID))
            logger.addHandler(self.discord_log_handler)
            self.discord_log_handler.start()
            logger.info(f"Discord log handler'ı {BOT_LOG_CHANNEL_ID} kanalı için ayarlandı.")

        # Merkezi veritabanı havuzunu oluştur
        try:
            self.db = await asyncpg.create_pool(dsn=os.getenv("DATABASE_URL"))
            logger.info("Merkezi veritabanı havuzu başarıyla oluşturuldu.")
        except Exception as e:
            logger.critical(f"Merkezi veritabanı havuzu oluşturulamadı: {e}")
            await self.close()
            return

        # Cog'ları yükle
        await self.load_all_extensions()

    async def close(self):
        logger.info("Bot kapatılıyor...")
        if self.discord_log_handler:
            self.discord_log_handler.close()
        if self.db:
            await self.db.close()
            logger.info("Merkezi veritabanı havuzu kapatıldı.")
        await super().close()

    async def on_ready(self):
        logger.info("-" * 30)
        logger.info(f"Giriş yapıldı: {self.user.name} (ID: {self.user.id})")
        logger.info(f"Discord.py versiyonu: {discord.__version__}")
        logger.info(f"{len(self.guilds)} sunucuda aktif.")
        logger.info("-" * 30)
        await self.change_presence(activity=discord.Game(name=f"Yata Misaki"))

    async def on_command_error(self, ctx: commands.Context, error):
        if hasattr(ctx.command, 'on_error'):
            return
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ Lütfen bu komutu tekrar kullanmadan önce {error.retry_after:.1f} saniye bekleyin.", delete_after=5)
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"❌ Eksik argüman: `{error.param.name}`. Yardım için `{DEFAULT_PREFIX}yardim {ctx.command.name}` yazın.")
        elif isinstance(error, commands.CheckFailure):
            await ctx.send("❌ Bu komutu kullanma yetkiniz yok!")
        elif isinstance(error, commands.CommandInvokeError):
            logger.error(f"'{ctx.command.name}' komutunda hata oluştu:", exc_info=error.original)
            await ctx.send("⚠️ Komut işlenirken bir hata oluştu. Geliştirici bilgilendirildi.")
        else:
            logger.error(f"Beklenmedik bir komut hatası: {error}", exc_info=True)

    async def load_all_extensions(self):
        logger.info("-" * 30)
        logger.info("Cog'lar yükleniyor...")
        cogs_dir = BASE_DIR / "commands"
        for path in cogs_dir.rglob("*.py"):
            if path.name != "__init__.py":
                extension_path = ".".join(path.relative_to(BASE_DIR).parts).replace(".py", "")
                try:
                    await self.load_extension(extension_path)
                    logger.info(f"'{extension_path}' başarıyla yüklendi.")
                except Exception:
                    logger.error(f"'{extension_path}' yüklenemedi.", exc_info=True)
        logger.info("-" * 30)

# --- Bot Başlatma ---
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = YataMisakiBot(
    command_prefix=get_prefix,
    intents=intents,
    help_command=None,
    case_insensitive=True
)

async def main():
    if not BOT_TOKEN:
        logger.critical("DISCORD_TOKEN bulunamadı!")
        return
    try:
        await bot.start(BOT_TOKEN)
    except Exception:
        logger.critical("Bot başlatılırken kritik bir hata oluştu!", exc_info=True)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot manuel olarak durduruldu.")
