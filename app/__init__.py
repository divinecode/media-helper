import logging
from .config import Config
from .bot import VideoDownloadBot
from .downloaders import TikTokDownloader, YouTubeShortsDownloader, CoubDownloader

# Set up package-level logging
logging.getLogger(__name__).addHandler(logging.NullHandler())

__version__ = "1.0.0"
__author__ = "DivineCode Team"

# Define what gets imported with "from video_downloader import *"
__all__ = [
    'Config',
    'VideoDownloadBot',
    'TikTokDownloader',
    'YouTubeShortsDownloader',
    'CoubDownloader',
]