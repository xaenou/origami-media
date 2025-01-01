from dataclasses import dataclass
from io import BytesIO
from typing import Optional


@dataclass
class YTDLPMetadata:
    url: str
    id: str
    title: Optional[str] = None
    uploader: Optional[str] = None
    extractor: Optional[str] = None
    ext: Optional[str] = None


@dataclass
class StreamMetadata:
    duration: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    size: Optional[int] = None


@dataclass
class MediaMetadata:
    # Non-default arguments (from YTDLPMetadata)
    url: str
    id: str

    # Optional arguments (from YTDLPMetadata)
    title: Optional[str] = None
    uploader: Optional[str] = None
    extractor: Optional[str] = None
    ext: Optional[str] = None

    # Optional arguments (from StreamMetadata)
    duration: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    size: Optional[int] = None


@dataclass
class Media:
    filename: str
    stream: BytesIO
    metadata: MediaMetadata
