from dataclasses import dataclass
from typing import Optional, Union

from mautrix.types import AudioInfo, FileInfo, ImageInfo, ThumbnailInfo, VideoInfo


@dataclass
class DisplayModel:
    msgtype: str
    mimetype: str
    url: str
    filename: str
    size: int
    thumbnail_url: Optional[str] = None
    thumbnail_info: Optional[ThumbnailInfo] = None
    info: Optional[Union[ImageInfo, VideoInfo, AudioInfo, FileInfo]] = None
