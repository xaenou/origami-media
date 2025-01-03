from dataclasses import dataclass
from io import BytesIO
from typing import Optional


@dataclass
class MediaInfo:
    url: str
    id: Optional[str] = None
    thumbnail_url: Optional[str] = None
    title: Optional[str] = None
    uploader: Optional[str] = None
    extractor: Optional[str] = None
    ext: Optional[str] = None
    duration: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    size: Optional[int] = None
    has_video: bool = False
    has_audio: bool = False
    is_image: bool = False


@dataclass
class MediaFile:
    filename: str
    metadata: MediaInfo
    stream: BytesIO = BytesIO()

    def __del__(self):
        if not self.stream.closed:
            self.stream.close()


@dataclass
class Media:
    content: MediaFile
    thumbnail: Optional[MediaFile] = None


@dataclass
class ProcessedMedia:
    filename: str
    content_info: MediaInfo
    content_uri: str
    thumbnail_info: Optional[MediaInfo]
    thumbnail_uri: Optional[str]

    def __str__(self):
        return (
            f"ProcessedMedia(content_uri='{self.content_uri}', "
            f"thumbnail_uri='{self.thumbnail_uri or 'None'}')"
        )
