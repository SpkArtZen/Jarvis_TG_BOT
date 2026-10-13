import os
import re
import sys
import time
import json
import uuid
import html
import shutil
import asyncio
import subprocess
from urllib.parse import urlparse
import dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Filter
from aiogram.types import Message, FSInputFile, InputMediaPhoto, InputMediaVideo
import yt_dlp

dotenv.load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
YOUTUBE_COOKIES = os.getenv("YOUTUBE_COOKIES")
if YOUTUBE_COOKIES and not os.path.exists("cookies.txt"):
    with open("cookies.txt", "w", encoding="utf-8") as f:
        f.write(YOUTUBE_COOKIES)

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB (ліміт Telegram Bot API)
MAX_ALBUM_ITEMS = 20              # Максимум фото з однієї каруселі
DOMAINS_FILE = "supported_domains.json"

BOT_START_TIME = time.time()       # Час старту бота для відсікання старих повідомлень
download_lock = asyncio.Lock()     # Черга: одночасно обробляється лише одне завантаження

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
    """Фільтр aiogram, що перевіряє наявність підтримуваного посилання у повідомленні
    та ігнорує повідомлення, надіслані під час AFK (до запуску бота)."""
    async def __call__(self, message: Message) -> bool | dict:
        if not message.text:
            return False

        # Ігноруємо повідомлення, які були відправлені до запуску бота
        if message.date.timestamp() < BOT_START_TIME:
            return False

        urls = URL_REGEX.findall(message.text)
        if not urls:
            return False

        domains = get_supported_domains()
        for url in urls:
            clean_url = url.rstrip('.,!?;:)')
            if is_supported_url(clean_url, domains):
                return {"url": clean_url}
        return False


def download_with_gallery_dl(url: str, download_path: str) -> list[str]:
    """Завантажує фотографії або галереї через gallery-dl"""
    cmd = [sys.executable, "-m", "gallery_dl"]
    if os.path.exists("cookies.txt"):
        cmd.extend(["--cookies", "cookies.txt"])
    cmd.extend(["--destination", download_path, url])

    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    collected = []
    for root, _, files in os.walk(download_path):
        for f in files:
            if not f.endswith(('.part', '.tmp', '.ytdl')):
                collected.append(os.path.join(root, f))
    return sorted(collected)


def download_media(url: str, download_path: str) -> dict:
    """Синхронна функція для завантаження медіа:
    відео через yt-dlp, фото та альбоми через gallery-dl."""
    base_opts = {
        # Пріоритет: намагаємось вміститися в 50 МБ (якщо 1080p завелике, yt-dlp обере 720p/480p)
        'format': 'bestvideo[filesize<45M][ext=mp4]+bestaudio[filesize<10M][ext=m4a]/bestvideo[filesize<45M]+bestaudio/best[filesize<50M]/bestvideo+bestaudio/best',
        'outtmpl': f'{download_path}/%(id)s.%(ext)s',
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
    }

    is_youtube = 'youtube.com' in url or 'youtu.be' in url
    ydl_opts = dict(base_opts)

    if os.path.exists('cookies.txt') and not is_youtube:
        ydl_opts['cookiefile'] = 'cookies.txt'

    def _extract(opts):
        with yt_dlp.YoutubeDL(opts) as ydl:
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
                "filepaths": [final_file]
            }

    try:
        return _extract(ydl_opts)
    except Exception as e:
        # Для YouTube: пробуємо з cookies як fallback (для 18+ відео)
        if is_youtube and os.path.exists('cookies.txt') and 'cookiefile' not in ydl_opts:
            try:
                retry_opts = dict(base_opts)
                retry_opts['cookiefile'] = 'cookies.txt'
                return _extract(retry_opts)
            except Exception:
                pass

        # Якщо це фото (Instagram, Pinterest, Twitter тощо) — завантажуємо через gallery-dl
        photos = download_with_gallery_dl(url, download_path)
        if photos:
            return {
                "title": "Медіа",
                "filepaths": photos
            }

        raise e


@dp.message(SupportedLinkFilter())
async def handle_video_link(message: Message, url: str):
    # Якщо черга зайнята — одразу інформуємо користувача
    if download_lock.locked():
        status_msg = await message.reply("⏳ Додано в чергу. Зачекайте завершення попереднього запиту...")
    else:
        status_msg = await message.reply("⏳ Завантажую медіа, зачекайте...")

    # Входимо в чергу (одночасно обробляється лише один запит)
    async with download_lock:
        await status_msg.edit_text("⏳ Завантажую медіа, зачекайте...")

        temp_dir = f"downloads/{uuid.uuid4()}"
        os.makedirs(temp_dir, exist_ok=True)

        try:
            result = await asyncio.to_thread(download_media, url, temp_dir)
            filepaths = result.get("filepaths", [])

            if not filepaths:
                raise FileNotFoundError("Медіа не знайдено після завантаження.")

            # Відбираємо файли, які не перевищують ліміт Telegram
            valid_files = [f for f in filepaths if os.path.exists(f) and os.path.getsize(f) <= MAX_FILE_SIZE]
            if not valid_files:
                await status_msg.edit_text(
                    f"❌ Файл перевищує ліміт {MAX_FILE_SIZE / (1024 * 1024):.0f} МБ Telegram Bot API."
                )
                return

            # Обмежуємо максимум фото для запобігання спаму
            valid_files = valid_files[:MAX_ALBUM_ITEMS]

            raw_title = result.get('title', 'Медіа') or 'Медіа'
            safe_title = html.escape(raw_title)[:500]
            caption = f"🎬 <b>{safe_title}</b>"

            await status_msg.edit_text("📤 Надсилаю...")

            if len(valid_files) == 1:
                filepath = valid_files[0]
                media_file = FSInputFile(filepath)
                _, ext = os.path.splitext(filepath)
                ext = ext.lower()

                if ext in ['.jpg', '.jpeg', '.png', '.webp']:
                    await message.reply_photo(photo=media_file, caption=caption, parse_mode="HTML")
                elif ext in ['.gif']:
                    await message.reply_animation(animation=media_file, caption=caption, parse_mode="HTML")
                else:
                    try:
                        await message.reply_video(video=media_file, caption=caption, parse_mode="HTML")
                    except Exception:
                        await message.reply_document(document=media_file, caption=caption, parse_mode="HTML")
            else:
                # Якщо це карусель / альбом (кілька фото чи відео) — надсилаємо пачками по 10
                for i in range(0, len(valid_files), 10):
                    chunk = valid_files[i:i + 10]
                    media_group = []
                    for idx, fpath in enumerate(chunk):
                        _, ext = os.path.splitext(fpath)
                        ext = ext.lower()
                        c = caption if (i == 0 and idx == 0) else None
                        if ext in ['.jpg', '.jpeg', '.png', '.webp']:
                            media_group.append(InputMediaPhoto(media=FSInputFile(fpath), caption=c, parse_mode="HTML"))
                        else:
                            media_group.append(InputMediaVideo(media=FSInputFile(fpath), caption=c, parse_mode="HTML"))
                    await message.reply_media_group(media=media_group)

            await status_msg.delete()

        except Exception as e:
            await status_msg.edit_text(
                f"❌ Не вдалося завантажити медіа:\n<code>{str(e)[:300]}</code>",
                parse_mode="HTML"
            )

        finally:
            if os.path.exists(temp_dir):
                for root, _, files in os.walk(temp_dir, topdown=False):
                    for f in files:
                        os.remove(os.path.join(root, f))
                    for d in os.listdir(root):
                        d_path = os.path.join(root, d)
                        if os.path.isdir(d_path):
                            os.rmdir(d_path)
                if os.path.exists(temp_dir):
                    os.rmdir(temp_dir)


@dp.message(F.text == "/start")
async def start_cmd(message: Message):
    if message.date.timestamp() < BOT_START_TIME:
        return

    domains = get_supported_domains()
    domains_str = ", ".join(sorted(domains)) if domains else "усі"
    await message.answer(
        "👋 Привіт! Надішли мені посилання, і я завантажу відео чи фото.\n\n"
        f"<b>Підтримувані домени:</b>\n<code>{domains_str}</code>",
        parse_mode="HTML"
    )


async def main():
    global BOT_START_TIME
    BOT_START_TIME = time.time()

    # Очищуємо залишки тимчасових файлів після можливих попередніх збоїв
    if os.path.exists("downloads"):
        shutil.rmtree("downloads", ignore_errors=True)
    os.makedirs("downloads", exist_ok=True)

    print("Бот запущений (черга активна, скидання старих повідомлень увімкнено)...")

    # Скидаємо накопичені повідомлення, надіслані під час перебування бота в офлайні
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, drop_pending_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
