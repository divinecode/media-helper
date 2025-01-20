import os
from dataclasses import dataclass
from typing import List
from pathlib import Path

@dataclass
class Config:
    # Bot settings
    bot_token: str
    bot_username: str
    allowed_usernames: List[str]
    
    # Download settings
    yt_proxy: str
    cookies_file: Path
    temp_dir: Path
    download_timeout: int = 90  # seconds
    
    # Telegram API timeouts
    read_timeout: int = 120
    write_timeout: int = 120
    connect_timeout: int = 120
    pool_timeout: int = 120
    
    # Connection settings
    connection_pool_size: int = 8
    
    @classmethod
    def from_env(cls) -> 'Config':
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
            
            # Telegram API timeouts
            read_timeout=int(os.getenv("READ_TIMEOUT", "120")),
            write_timeout=int(os.getenv("WRITE_TIMEOUT", "120")),
            connect_timeout=int(os.getenv("CONNECT_TIMEOUT", "120")),
            pool_timeout=int(os.getenv("POOL_TIMEOUT", "120")),
            
            # Connection settings
            connection_pool_size=int(os.getenv("CONNECTION_POOL_SIZE", "8")),
        )