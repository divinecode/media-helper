import os
import re
import requests
from telegram import Update
from telegram import constants
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters
)
import yt_dlp
import aiohttp
import asyncio
from telebot.types import ReactionTypeEmoji

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "")
YTPROXY = os.environ.get("YT_PROXY", "socks5://5.183.70.46:1080")

ALLOWED_USERNAMES = os.environ.get("ALLOWED_USERNAMES", "")
ALLOWED_USERNAMES = [u.strip() for u in ALLOWED_USERNAMES.split(",") if u.strip()]

TIKTOK_LINK_REGEX = re.compile(r"https?://(?:vt\.)?(?:www\.)?tiktok\.com/[\w\-/.@]+")
TIKTOK_VIDEO_ID_REGEX = re.compile(r"/video/(\d+)")
TIKTOK_SHORT_LINK_REGEX = re.compile(r"https?://(?!www\.)[a-zA-Z0-9_-]+\.(?:tiktok|douyin)\.com")

COUB_LINK_REGEX = re.compile(r"https?://coub\.com/view/(\w+)")
COUB_API_URL = "https://coub.com/api/v2/coubs/{coub_id}"

cookies_file = 'cookies.txt'

def resolve_tiktok_url(short_url: str) -> str:
    """
    Разрешает короткую ссылку TikTok и возвращает полный URL.
    """
    print(f"Обнаружена короткая ссылка: {short_url}")
    try:
        response = requests.head(short_url, allow_redirects=True, timeout=10)    
        return response.url
    except Exception as e:
        print(f"Ошибка при разрешении короткой ссылки: {e}")
        return ""

def extract_video_id(tiktok_url: str) -> str | None:
    """
    Извлекает ID видео из полной ссылки TikTok.
    Возвращает ID видео или None, если ID не найден.
    """
    print(f"Пытаюсь получить ID из: {tiktok_url}")
    match = TIKTOK_VIDEO_ID_REGEX.search(tiktok_url)
    if match:
        print(f"Получил: {match.group(1)}")
        return match.group(1)
    print(f"Не получилось извлечь ID")
    return None

async def download_tiktok_video(tiktok_url: str) -> bytes | None:
    """
    Скачивает видео с TikTok через tikcdn.io, поддерживая укороченные и полные ссылки.
    Возвращает байты видео или None в случае ошибки.
    """
    match = TIKTOK_SHORT_LINK_REGEX.search(tiktok_url)
    try:
        if match:
            tiktok_url = resolve_tiktok_url(tiktok_url)
            if not tiktok_url:
                print("Не удалось разрешить короткую ссылку.")
                return None

        video_id = extract_video_id(tiktok_url)
        if not video_id:
            print("Не удалось извлечь ID видео из ссылки.")
            return None

        print(f"ID видео: {video_id}")
        download_url = f"https://tikcdn.io/ssstik/{video_id}"

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                " AppleWebKit/537.36 (KHTML, like Gecko)"
                " Chrome/108.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.tiktok.com/",
        }
        response = requests.get(download_url, headers=headers, timeout=10)
        response.raise_for_status()

        return response.content
    except Exception as e:
        print(f"Ошибка при загрузке видео: {e}")
        return None

async def download_youtube_shorts(url: str) -> bytes | None:
    """
    Скачивает короткое (не более 1 минуты) видео с YouTube (Shorts) через yt-dlp.
    Возвращает байты видео или None, если произошла ошибка или видео слишком длинное.
    """
    try:
        # Сначала получаем информацию о видео без скачивания
        ydl_opts_info = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'simulate': True,  
            'cookies': cookies_file,
            'proxy': YTPROXY
        }
        with yt_dlp.YoutubeDL(ydl_opts_info) as ydl:
            info = ydl.extract_info(url, download=False)
            duration = info.get('duration', 0)
            if duration > 60:
                print("Видео длиннее 1 минуты, скачивание запрещено.")
                return None

        # Теперь скачиваем в файл
        ydl_opts_download = {
            'quiet': True,
            'no_warnings': True,            
            'outtmpl': 'temp_video.%(ext)s',          
            'cookies': cookies_file,
            'proxy': YTPROXY,
            'format': 'bestvideo+bestaudio/best',  # Максимальное качество
            'merge_output_format': 'mp4',          # Объединить в mp4            
            'postprocessors': [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4'  # Конвертировать в mp4 (если требуется)
            }],
        }
        with yt_dlp.YoutubeDL(ydl_opts_download) as ydl:
            ydl.download([url])

        # Ищем скачанный файл
        downloaded_file = None
        for ext in ['mp4', 'mkv', 'webm']:
            potential_path = f"temp_video.{ext}"
            if os.path.exists(potential_path):
                downloaded_file = potential_path
                break

        if not downloaded_file:
            print("Не найден скачанный файл youtube.")
            return None

        with open(downloaded_file, 'rb') as f:
            video_data = f.read()

        os.remove(downloaded_file)
        return video_data

    except Exception as e:
        print(f"Ошибка при скачивании видео с Youtube: {e}")
        return None


def download_file(url: str, filename: str) -> str:
    """
    Download a file from a URL and save it locally.
    """
    try:
        response = requests.get(url, stream=True, timeout=10)
        response.raise_for_status()
        with open(filename, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        return filename
    except Exception as e:
        print(f"Failed to download file: {e}")
        return ""

async def fetch_coub_data(coub_url: str) -> dict | None:
    """
    Получает данные о видео Coub через API.
    """
    try:
        # Извлекаем ID из ссылки
        coub_id_match = re.search(r"coub\.com/view/(\w+)", coub_url)
        if not coub_id_match:
            print("Неверный формат ссылки Coub.")
            return None

        coub_id = coub_id_match.group(1)
        api_url = f"https://coub.com/api/v2/coubs/{coub_id}"

        async with aiohttp.ClientSession() as session:
            async with session.get(api_url) as response:
                response.raise_for_status()
                return await response.json()
    except Exception as e:
        print(f"Ошибка при получении данных Coub: {e}")
        return None

async def download_coub_video(coub_url: str) -> bytes | None:
    """
    Downloads and merges Coub video and audio, looping the video until the audio ends.
    Uses H.265 for better compression.
    """
    
    try:
        coub_data = await fetch_coub_data(coub_url)
        if not coub_data or not coub_data.get("file_versions"):
            print("Failed to fetch video data.")
            return None

        video_url = coub_data["file_versions"]["html5"]["video"]["high"]["url"]
        audio_url = coub_data["file_versions"]["html5"]["audio"]["high"]["url"]

        async with aiohttp.ClientSession() as session:
            async with session.get(video_url) as video_response:
                video_response.raise_for_status()
                video_data = await video_response.read()

            async with session.get(audio_url) as audio_response:
                audio_response.raise_for_status()
                audio_data = await audio_response.read()

        # Save temporary files
        video_path = "temp_coub_video.mp4"
        audio_path = "temp_coub_audio.mp3"
        output_path = "temp_coub_output.mp4"

        with open(video_path, "wb") as f:
            f.write(video_data)
        with open(audio_path, "wb") as f:
            f.write(audio_data)

        # Use ffmpeg to loop video until audio ends, with H.265 compression
        ffmpeg_command = (
            f"ffmpeg -y -stream_loop -1 -i {video_path} -i {audio_path} "
            f"-c:v copy -crf 28 -c:a aac -shortest {output_path}"
        )
        process = await asyncio.create_subprocess_shell(
            ffmpeg_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            print(f"FFmpeg error: {stderr.decode()}")
            return None

        # Read merged video
        with open(output_path, "rb") as f:
            result = f.read()

        # Clean up temporary files
        os.remove(video_path)
        os.remove(audio_path)
        os.remove(output_path)

        return result
    except Exception as e:
        print(f"Error downloading Coub video: {e}")
        return None


def bot_was_mentioned(update: Update) -> bool:
    """
    Проверяет, упомянут ли бот в сообщении (через @BOT_USERNAME).
    """
    message = update.effective_message
    if not message or not message.entities:
        return False

    for entity in message.entities:
        if entity.type == "mention":
            mention_text = message.parse_entity(entity)
            if mention_text.lower() == f"@{BOT_USERNAME}".lower():
                return True
    return False

def extract_links(message: str) -> list[str]:
    """
    Извлекает все ссылки из сообщения.
    """
    urls = re.findall(r'(https?://\S+)', message)
    return urls

async def handle_mentions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обработчик команды /download. 
    Ждём ссылку в аргументах и скачиваем либо TikTok, либо YouTube Shorts.
    """
    message = update.effective_message
    user = update.effective_user

    # Проверяем пользователя в списке разрешённых
    #if ALLOWED_USERNAMES and (not user or user.username not in ALLOWED_USERNAMES):
    #    await message.reply_text("Ты не можешь скачивать видео.")
    #    return

    #args = context.args
    #if not args:
    #    await message.reply_text("Пожалуйста, укажи ссылку: /download <URL>")
    #    return
    if not bot_was_mentioned(update):
        return  # Игнорируем сообщения без упоминания бота

    text = message.text or ""
    url = extract_links(text)[0]
    #await context.bot.set_message_reaction(
    #    message.chat.id,
    #    message.message_id,
    #    [ReactionTypeEmoji('👍')],
    #    is_big=False
    #)
    try:
        video_data = None
        print(f"Скачиваем видео {url}")
        
        if "coub.com" in url.lower():
            video_data = await download_coub_video(url)
        elif "tiktok.com" in url.lower():
            video_data = await download_tiktok_video(url)
        elif "youtube.com" in url.lower() or "youtu.be" in url.lower():  
            video_data = await download_youtube_shorts(url)
        else:
            await message.reply_text("Я умею скачивать только Coub, TikTok и YouTube Shorts!")
            return

        if not video_data:
            await message.reply_text("Анлак, не получилось скачать видео")
            return
            
        print(f"Отправляем видео {url}")
        await message.reply_video(video=video_data)
    except Exception as e:
        print(f"Ошибка в процессе /download: {e}")
        await message.reply_text("Произошла ошибка. Попробуйте ещё раз позже.")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /start — приветственное сообщение.
    """
    await update.message.reply_text(
        "Привет! Я бот для скачивания коротких видео (TikTok и YouTube Shorts).\n"
        "Используй /download <ссылка>, чтобы скачать видео до 1 минуты."
    )

async def handle_other_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обработчик любых сообщений, не являющихся командами.
    Бот реагирует только если упомянут. Иначе молчит.
    """
    if bot_was_mentioned(update):
        # Даём короткую подсказку, если бот явно упомянут
        await update.effective_message.reply_text(
            "Чтобы скачать видео, используй команду /download <ссылка>.\n"
            "Поддерживаются TikTok и короткие видео YouTube (до 1 минуты)."
        )
    else:
        # Если не упомянули — бот молчит (ничего не отвечает)
        return

def main():
    app = Application.builder().token(BOT_TOKEN).read_timeout(60).write_timeout(60).build()
    
    # Команды
    #app.add_handler(CommandHandler("start", start_command))
    #app.add_handler(CommandHandler("download", handle_download))

    # Все остальные сообщения
    app.add_handler(MessageHandler(filters.ALL, handle_mentions))

    print("Бот запущен. Ожидаем сообщения...")
    app.run_polling()

if __name__ == "__main__":
    main()
