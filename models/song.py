from typing import Dict


class Song:
    def __init__(self, data: Dict):
        self.url = data.get("url", "")
        self.title = data.get("title", "Unknown Title")
        self.duration = data.get("duration", 0)
        self.thumbnail = data.get("thumbnail", "")
        self.uploader = data.get("uploader", "Unknown")
        self.webpage_url = data.get("webpage_url", "")
        self.requested_by = data.get("requested_by", "Unknown")

    def __str__(self):
        return f"**{self.title}** by {self.uploader}"

    def to_dict(self):
        return {
            "url": self.url,
            "title": self.title,
            "duration": self.duration,
            "thumbnail": self.thumbnail,
            "uploader": self.uploader,
            "webpage_url": self.webpage_url,
            "requested_by": self.requested_by,
        }

    @classmethod
    def from_dict(cls, data: Dict):
        return cls(data)