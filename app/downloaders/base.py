from abc import ABC, abstractmethod
from typing import Optional, List, Union
import aiohttp
import logging
from config import Config
from media_types import DownloadResult

logger = logging.getLogger(__name__)

class VideoDownloader(ABC):
    def __init__(self, config: Config):
        self.config = config
        
    @abstractmethod
    async def download(self, url: str) -> Optional[Union[bytes, List[DownloadResult]]]:
        pass
    
    @abstractmethod
    def can_handle(self, url: str) -> bool:
        pass
    
    async def _download_file(self, url: str, headers: Optional[dict] = None) -> Optional[bytes]:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as response:
                    response.raise_for_status()
                    return await response.read()
        except Exception as e:
            logger.error(f"Error downloading file: {e}")
            return None