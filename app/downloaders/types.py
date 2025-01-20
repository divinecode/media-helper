from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

class MediaType(Enum):
    VIDEO = auto()
    PHOTO = auto()

@dataclass
class DownloadResult:
    data: bytes
    media_type: MediaType
    caption: Optional[str] = None