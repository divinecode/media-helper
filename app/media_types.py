from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

class MediaType(Enum):
    VIDEO = auto()
    PHOTO = auto()
    AUDIO = auto()

@dataclass
class DownloadResult:
    data: bytes
    media_type: MediaType
    caption: Optional[str] = None

@dataclass
class MediaItem:
    data: bytes
    media_type: MediaType
    caption: Optional[str] = None
    size_mb: float = 0.0

    @classmethod
    def from_bytes(cls, data: bytes, media_type: MediaType, caption: Optional[str] = None) -> 'MediaItem':
        return cls(
            data=data,
            media_type=media_type,
            caption=caption,
            size_mb=len(data) / (1024 * 1024)
        )