"""Daily music copy from tracks already bundled in the mobile app."""

import hashlib
from datetime import date

# Keep IDs and display titles aligned with the mobile BgmTrack catalog.
MUSIC_TRACKS = (
    ("felt-piano-memories", "Soft Piano"),
    ("night-rain-on-tokyo", "Tokyo Nights"),
    ("midnight-tokyo-rain", "Midnight Drizzle"),
    ("midnight-tokyo-rain-2", "Before Dawn"),
    ("neon-rain", "Neon Streets"),
    ("fading-static", "Old Radio"),
)


def daily_music_title(local_date: date) -> str:
    """Stable across requests, languages and restarts for the same local date."""
    digest = hashlib.sha256(f"banner-music-v1:{local_date.isoformat()}".encode()).digest()
    return MUSIC_TRACKS[int.from_bytes(digest[:8], "big") % len(MUSIC_TRACKS)][1]


def pick_music_title(local_date: date, tracks) -> str:
    """Stable for a date and catalogue; never select inactive/unsupported rows."""
    if not tracks:
        raise ValueError("no available music")
    ordered = sorted(tracks, key=lambda track: track.id)
    digest = hashlib.sha256(f"banner-music-v1:{local_date.isoformat()}".encode()).digest()
    return ordered[int.from_bytes(digest[:8], "big") % len(ordered)].title
