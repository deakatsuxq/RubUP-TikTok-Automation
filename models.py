"""Values exchanged by discovery, persistence and the application layer."""
from dataclasses import dataclass, field


@dataclass
class Video:
    video_id: str
    url: str
    author: str = ""
    description: str = ""
    hashtags: list[str] = field(default_factory=list)
    published_at: str | None = None
    query: str = ""
    category: str = ""
    is_test: bool = False
