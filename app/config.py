import os
from dataclasses import dataclass, field
from typing import List
from pathlib import Path

@dataclass
class CompressionConfig:
    # Size thresholds
    default_compress_threshold_mb: int = 10  # Compress videos larger than this
    max_telegram_size_mb: int = 45
    max_compress_size_mb: int = 200
    
    # Default compression for videos > 5MB (balanced efficiency and quality)
    default_crf: int = 28        # Higher CRF = more compression
    default_scale: int = 1280    # Good balance for most videos
    default_preset: str = "veryfast"  # Fast compression
    default_audio_bitrate: int = 96  # Lower but still decent audio quality
    
    # First pass compression (high quality, for large videos)
    first_pass_crf: int = 28
    first_pass_scale: int = 1080
    first_pass_preset: str = "fast"
    first_pass_audio_bitrate: int = 128
    
    # Second pass compression (if first pass file is still too large)
    second_pass_crf: int = 32
    second_pass_scale: int = 720
    second_pass_preset: str = "faster"
    second_pass_audio_bitrate: int = 96

@dataclass
class Config:
    # Required parameters (no defaults)
    bot_token: str
    bot_username: str
    allowed_usernames: List[str]
    yt_proxy: str
    cookies_file: Path
    temp_dir: Path
    
    # Optional parameters (with defaults)
    # Multi-user settings
    max_concurrent_downloads: int = 20
    max_downloads_per_user: int = 3
    
    # Download settings
    download_timeout: int = 90
    
    # Telegram API timeouts
    read_timeout: int = 120
    write_timeout: int = 120
    connect_timeout: int = 120
    pool_timeout: int = 120
    
    # Connection settings
    connection_pool_size: int = 8
    
    # Compression settings
    compression: CompressionConfig = field(default_factory=CompressionConfig)
    
    @classmethod
    def from_env(cls) -> 'Config':
        compression = CompressionConfig(
            default_compress_threshold_mb=int(os.getenv("DEFAULT_COMPRESS_THRESHOLD_MB", "5")),
            max_telegram_size_mb=int(os.getenv("MAX_TELEGRAM_SIZE_MB", "45")),
            max_compress_size_mb=int(os.getenv("MAX_COMPRESS_SIZE_MB", "100")),
            default_crf=int(os.getenv("DEFAULT_CRF", "23")),
            default_scale=int(os.getenv("DEFAULT_SCALE", "1280")),
            default_preset=os.getenv("DEFAULT_PRESET", "veryfast"),
            default_audio_bitrate=int(os.getenv("DEFAULT_AUDIO_BITRATE", "128")),
            first_pass_crf=int(os.getenv("FIRST_PASS_CRF", "28")),
            first_pass_scale=int(os.getenv("FIRST_PASS_SCALE", "1080")),
            first_pass_preset=os.getenv("FIRST_PASS_PRESET", "fast"),
            first_pass_audio_bitrate=int(os.getenv("FIRST_PASS_AUDIO_BITRATE", "128")),
            second_pass_crf=int(os.getenv("SECOND_PASS_CRF", "32")),
            second_pass_scale=int(os.getenv("SECOND_PASS_SCALE", "720")),
            second_pass_preset=os.getenv("SECOND_PASS_PRESET", "faster"),
            second_pass_audio_bitrate=int(os.getenv("SECOND_PASS_AUDIO_BITRATE", "96")),
        )
        
        return cls(
            # Bot settings
            bot_token=os.getenv("BOT_TOKEN", ""),
            bot_username=os.getenv("BOT_USERNAME", ""),
            allowed_usernames=[u.strip() for u in os.getenv("ALLOWED_USERNAMES", "").split(",") if u.strip()],
            
            # Download settings
            yt_proxy=os.getenv("YT_PROXY", ""),
            cookies_file=Path(os.getenv("COOKIES_FILE", "cookies.txt")),
            temp_dir=Path(os.getenv("TEMP_DIR", "temp")),
            download_timeout=int(os.getenv("DOWNLOAD_TIMEOUT", "90")),
            
            # Multi-user settings
            max_concurrent_downloads=int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "20")),
            max_downloads_per_user=int(os.getenv("MAX_DOWNLOADS_PER_USER", "3")),
            
            # Telegram API timeouts
            read_timeout=int(os.getenv("READ_TIMEOUT", "120")),
            write_timeout=int(os.getenv("WRITE_TIMEOUT", "120")),
            connect_timeout=int(os.getenv("CONNECT_TIMEOUT", "120")),
            pool_timeout=int(os.getenv("POOL_TIMEOUT", "120")),
            
            # Connection settings
            connection_pool_size=int(os.getenv("CONNECTION_POOL_SIZE", "8")),
            
            # Compression settings
            compression=compression,
        )