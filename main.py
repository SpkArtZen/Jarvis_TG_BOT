import os
import re
import json
import uuid
import html
import asyncio
from urllib.parse import urlparse
import dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Filter
from aiogram.types import Message, FSInputFile
import yt_dlp

dotenv.load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB (ліміт Telegram Bot API)
DOMAINS_FILE = "supported_domains.json"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

URL_REGEX = re.compile(r'https?://[^\s]+')

_domains_cache = set()
_domains_mtime = 0


def get_supported_domains() -> set:
    """Завантажує та кешує список дозволених доменів з JSON файлу.
    Автоматично оновлюється при зміні файлу."""
    global _domains_cache, _domains_mtime
    if not os.path.exists(DOMAINS_FILE):
        return set()
    try:
        mtime = os.path.getmtime(DOMAINS_FILE)
        if mtime != _domains_mtime:
            with open(DOMAINS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                _domains_cache = {d.lower().strip() for d in data.get("domains", [])}
                _domains_mtime = mtime
    except Exception as e:
        print(f"Помилка читання {DOMAINS_FILE}: {e}")
    return _domains_cache


def is_supported_url(url: str, supported_domains: set) -> bool:
    """Перевіряє, чи належить URL до одного з підтримуваних доменів."""
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower().rstrip('.')
        for domain in supported_domains:
            domain = domain.lower()
            if hostname == domain or hostname.endswith('.' + domain):
                return True
    except Exception:
        pass
    return False


class SupportedLinkFilter(Filter):
    """Фільтр aiogram, що перевіряє наявність підтримуваного посилання у повідомленні."""
    async def __call__(self, message: Message) -> bool | dict:
        if not message.text:
            return False
        urls = URL_REGEX.findall(message.text)
        if not urls:
            return False

        domains = get_supported_domains()
        for url in urls:
            # Видаляємо кінцеві розділові знаки, якщо посилання було в кінці речення
            clean_url = url.rstrip('.,!?;:)')
            if is_supported_url(clean_url, domains):
                return {"url": clean_url}
        return False


def download_media(url: str, download_path: str) -> dict:
    """Синхронна функція для завантаження через yt-dlp."""
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': f'{download_path}/%(id)s.%(ext)s',
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info and 'entries' in info and info['entries']:
            info = info['entries'][0]

        filename = ydl.prepare_filename(info)
        base, _ = os.path.splitext(filename)
        mp4_filename = f"{base}.mp4"

        if os.path.exists(mp4_filename):
            final_file = mp4_filename
        elif os.path.exists(filename):
            final_file = filename
        else:
            files = [os.path.join(download_path, f) for f in os.listdir(download_path)]
            final_file = files[0] if files else filename

        return {
            "title": info.get("title", "Медіа") if info else "Медіа",
            "filepath": final_file
        }


@dp.message(SupportedLinkFilter())
async def handle_video_link(message: Message, url: str):
    status_msg = await message.reply("⏳ Завантажую медіа, зачекайте...")

    temp_dir = f"downloads/{uuid.uuid4()}"
    os.makedirs(temp_dir, exist_ok=True)

    try:
        result = await asyncio.to_thread(download_media, url, temp_dir)
        filepath = result["filepath"]

        if not os.path.exists(filepath):
            raise FileNotFoundError("Файл не знайдено після завантаження.")

        filesize = os.path.getsize(filepath)
        if filesize > MAX_FILE_SIZE:
            await status_msg.edit_text(
                f"❌ Файл важить {filesize / (1024 * 1024):.1f} МБ.\n"
                f"Telegram Bot API підтримує файли лише до 50 МБ."
            )
            return

        await status_msg.edit_text("📤 Надсилаю...")
        media_file = FSInputFile(filepath)
        raw_title = result.get('title', 'Медіа') or 'Медіа'
        safe_title = html.escape(raw_title)[:500]
        caption = f"🎬 <b>{safe_title}</b>"

        _, ext = os.path.splitext(filepath)
        ext = ext.lower()

        # Підтримка фото, gif та відео (наприклад, Pinterest часто містить статичні фото)
        if ext in ['.jpg', '.jpeg', '.png', '.webp']:
            await message.reply_photo(photo=media_file, caption=caption, parse_mode="HTML")
        elif ext in ['.gif']:
            await message.reply_animation(animation=media_file, caption=caption, parse_mode="HTML")
        else:
            try:
                await message.reply_video(video=media_file, caption=caption, parse_mode="HTML")
            except Exception:
                await message.reply_document(document=media_file, caption=caption, parse_mode="HTML")

        await status_msg.delete()

    except Exception as e:
        await status_msg.edit_text(
            f"❌ Не вдалося завантажити медіа:\n<code>{str(e)[:300]}</code>",
            parse_mode="HTML"
        )

    finally:
        if os.path.exists(temp_dir):
            for f in os.listdir(temp_dir):
                os.remove(os.path.join(temp_dir, f))
            os.rmdir(temp_dir)


@dp.message(F.text == "/start")
async def start_cmd(message: Message):
    domains = get_supported_domains()
    domains_str = ", ".join(sorted(domains)) if domains else "усі"
    await message.answer(
        "👋 Привіт! Надішли мені посилання, і я завантажу відео чи фото.\n\n"
        f"<b>Підтримувані домени:</b>\n<code>{domains_str}</code>",
        parse_mode="HTML"
    )


async def main():
    print("Бот запущений з фільтром доменів...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
