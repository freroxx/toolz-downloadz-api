"""
Platform registry — single source of truth for all supported sites.
Each platform has: id, display_name, domains, extractor_args defaults, color, enabled.
"""
from typing import Dict, Any, List

PLATFORMS: Dict[str, Dict[str, Any]] = {
    "youtube": {
        "id": "youtube",
        "name": "YouTube",
        "domains": ["youtube.com", "youtu.be", "music.youtube.com", "m.youtube.com"],
        "color": "#FF0000",
        "enabled": True,
        "supports": ["video", "shorts", "playlist", "channel", "music", "live"],
        "extractor_args": {},
    },
    "tiktok": {
        "id": "tiktok",
        "name": "TikTok",
        "domains": ["tiktok.com", "vm.tiktok.com", "vt.tiktok.com"],
        "color": "#000000",
        "enabled": True,
        "supports": ["video", "slideshow"],
        "extractor_args": {
            "api_hostname": "api16-normal-useast5.us.tiktok.com",
            "web_instance_url": "https://www.tiktok.com/",
        },
    },
    "instagram": {
        "id": "instagram",
        "name": "Instagram",
        "domains": ["instagram.com", "instagr.am"],
        "color": "gradient",
        "enabled": True,
        "supports": ["reel", "post", "story", "carousel"],
        "extractor_args": {"get_comments": False},
    },
    "twitter": {
        "id": "twitter",
        "name": "X (Twitter)",
        "domains": ["twitter.com", "x.com", "t.co"],
        "color": "#1DA1F2",
        "enabled": True,
        "supports": ["video", "gif"],
        "extractor_args": {"mobile_redirect": True},
    },
    "reddit": {
        "id": "reddit",
        "name": "Reddit",
        "domains": ["reddit.com", "redd.it", "redditmedia.com"],
        "color": "#FF4500",
        "enabled": True,
        "supports": ["video", "gif", "gallery"],
    },
    "facebook": {
        "id": "facebook",
        "name": "Facebook",
        "domains": ["facebook.com", "fb.com", "fb.watch"],
        "color": "#0866FF",
        "enabled": True,
        "supports": ["video", "reel", "story"],
    },
    "soundcloud": {
        "id": "soundcloud",
        "name": "SoundCloud",
        "domains": ["soundcloud.com"],
        "color": "#FF5500",
        "enabled": True,
        "supports": ["audio", "playlist"],
    },
    "twitch": {
        "id": "twitch",
        "name": "Twitch",
        "domains": ["twitch.tv", "clips.twitch.tv"],
        "color": "#9146FF",
        "enabled": True,
        "supports": ["clip", "vod", "live"],
    },
    "vimeo": {
        "id": "vimeo",
        "name": "Vimeo",
        "domains": ["vimeo.com", "player.vimeo.com"],
        "color": "#1AB7EA",
        "enabled": True,
        "supports": ["video"],
    },
    "dailymotion": {
        "id": "dailymotion",
        "name": "Dailymotion",
        "domains": ["dailymotion.com", "dai.ly"],
        "color": "#0066DC",
        "enabled": True,
        "supports": ["video"],
    },
    "pinterest": {
        "id": "pinterest",
        "name": "Pinterest",
        "domains": ["pinterest.com", "pin.it"],
        "color": "#E60023",
        "enabled": True,
        "supports": ["video", "idea_pin"],
    },
    "threads": {
        "id": "threads",
        "name": "Threads",
        "domains": ["threads.net"],
        "color": "#000000",
        "enabled": True,
        "supports": ["video", "post"],
    },
    "linkedin": {
        "id": "linkedin",
        "name": "LinkedIn",
        "domains": ["linkedin.com"],
        "color": "#0A66C2",
        "enabled": True,
        "supports": ["video"],
    },
    "snapchat": {
        "id": "snapchat",
        "name": "Snapchat",
        "domains": ["snapchat.com", "story.snapchat.com"],
        "color": "#FFFC00",
        "enabled": True,
        "supports": ["spotlight", "story"],
    },
    "bilibili": {
        "id": "bilibili",
        "name": "Bilibili",
        "domains": ["bilibili.com", "b23.tv"],
        "color": "#00A1D6",
        "enabled": True,
        "supports": ["video"],
    },
    "generic": {
        "id": "generic",
        "name": "Generic",
        "domains": [],
        "color": "#6750A4",
        "enabled": True,
        "supports": ["video", "audio"],
    },
}


def detect_platform(url: str) -> str:
    url_lower = url.lower()
    for pid, info in PLATFORMS.items():
        if pid == "generic":
            continue
        for domain in info.get("domains", []):
            if domain in url_lower:
                return pid
    return "generic"


def list_platforms(enabled_only: bool = True) -> List[Dict[str, Any]]:
    out = []
    for pid, info in PLATFORMS.items():
        if enabled_only and not info.get("enabled"):
            continue
        out.append(
            {
                "id": pid,
                "name": info["name"],
                "domains": info["domains"],
                "color": info["color"],
                "supports": info.get("supports", []),
                "enabled": info.get("enabled", True),
            }
        )
    return out


def is_supported(url: str) -> bool:
    return detect_platform(url) != "generic" or url.startswith(("http://", "https://"))
