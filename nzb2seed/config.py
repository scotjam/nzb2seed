from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib


DEFAULT_QBIT_CATEGORY = "nzb2seed"
DEFAULT_GUI_USER = "admin"
DEFAULT_GUI_PASSWORD = "nzb2seed"


@dataclass
class Config:
    path: Path
    prowlarr_url: str = ""
    prowlarr_key: str = ""
    indexer_ids: list[int] = field(default_factory=list)
    categories: list[int] = field(default_factory=list)
    sab_url: str = ""
    sab_key: str = ""
    sab_category: str = "nzb2seed"
    sab_priority: int = 0
    sab_delete_history: bool = False
    qbit_url: str = ""
    qbit_user: str = ""
    qbit_pass: str = ""
    qbit_category: str = DEFAULT_QBIT_CATEGORY   # what nzb2seed's torrents are filed under
    qbit_tags: list[str] = field(default_factory=list)
    start_when_complete: bool = False
    sab_to_local: list = field(default_factory=list)
    local_to_qbit: list = field(default_factory=list)
    output_dir: str = ""
    torrent_dir: str = ""          # resolved (absolute)
    torrent_dir_raw: str = "torrents"
    post_processing: str = "auto"
    cleanup: bool = True
    local_verify: bool = False
    retry_bad_pieces: bool = True     # replace files that fail the piece check with other posts
    demand_rules: list = field(default_factory=list)   # priority rules, best first
    demand_block: list = field(default_factory=list)   # anything matching is not built
    demand_only_rules: bool = False   # build only what a priority rule matches
    demand_min_sample: int = 8        # a rule is never offered on fewer torrents than this
    demand_age_days: float = 7.0      # a torrent younger than this has not had its chance
    space_min_percent: float = 0      # pause downloading below this much free space (0 = off)
    space_min_gb: float = 0           # ... or below this many GB free (0 = off)
    space_pause_torrents: bool = False  # also stop downloading torrents (can cost H&R grace)
    retention_enabled: bool = False   # remove builds again after retention_days (off by default)
    retention_days: int = 30          # how long a build stays in qBittorrent before it goes
    peek_archives: bool = True        # fetch a RAR post's first volume and read its headers
                                      # before downloading the rest of it
    outbound_proxy: str = ""          # e.g. http://127.0.0.1:8888 (the VPN container's HTTP proxy): srrDB/predb/xrel/TVmaze lookups
    flaresolverr_url: str = ""        # e.g. http://nas:8191 - only used when a site answers with a Cloudflare challenge
    # automatic builds from autobrr (the Automatic tab)
    auto_enabled: bool = False
    auto_folder: str = ""              # the inbox as this machine sees it
    auto_autobrr_folder: str = ""      # the same folder as autobrr sees it (its watch-folder action)
    auto_wait_hours: float = 14 / 60   # how long to keep trying for the Usenet post
                                      # (measured: a release that is posted at all is there
                                      #  within minutes of the torrent, often before it)
    auto_retry_first_minutes: float = 2.0   # the first gap; it doubles up to the cap below
    auto_retry_minutes: float = 2.0    # the longest gap between tries of one torrent
    auto_start: bool = True            # start seeding - only ever at a 100.0% recheck
    auto_parallel: int = 1             # builds at once
    auto_searches_per_hour: int = 40   # Prowlarr searches automatic builds may make per hour
    autobrr_url: str = ""
    autobrr_key: str = ""
    match_episode_names: bool = False  # place posts/files named only by episode title (more searches)
    episode_source: str = "all"       # where a show's episode list comes from: all | tvmaze | tmdb | tvdb | imdb
    season_packing: str = "scene"     # "scene" = keep scene RARs (rebuild them from srrDB) | "unpack"
    gui_username: str = DEFAULT_GUI_USER
    gui_password: str = DEFAULT_GUI_PASSWORD   # empty = no login, LAN-only


# (section, key, attribute) - the on-disk layout of nzb2seed.toml
LAYOUT = [
    ("prowlarr", "url", "prowlarr_url"),
    ("prowlarr", "api_key", "prowlarr_key"),
    ("prowlarr", "indexer_ids", "indexer_ids"),
    ("prowlarr", "categories", "categories"),
    ("sabnzbd", "url", "sab_url"),
    ("sabnzbd", "api_key", "sab_key"),
    ("sabnzbd", "category", "sab_category"),
    ("sabnzbd", "priority", "sab_priority"),
    ("sabnzbd", "delete_history", "sab_delete_history"),
    ("qbittorrent", "url", "qbit_url"),
    ("qbittorrent", "username", "qbit_user"),
    ("qbittorrent", "password", "qbit_pass"),
    ("qbittorrent", "category", "qbit_category"),
    ("qbittorrent", "tags", "qbit_tags"),
    ("qbittorrent", "start_when_complete", "start_when_complete"),
    ("paths", "sab_to_local", "sab_to_local"),
    ("paths", "local_to_qbit", "local_to_qbit"),
    ("paths", "output_dir", "output_dir"),
    ("paths", "torrent_dir", "torrent_dir_raw"),
    ("behaviour", "post_processing", "post_processing"),
    ("behaviour", "cleanup", "cleanup"),
    ("behaviour", "local_verify", "local_verify"),
    ("behaviour", "retry_bad_pieces", "retry_bad_pieces"),
    ("behaviour", "peek_archives", "peek_archives"),
    ("demand", "rules", "demand_rules"),
    ("demand", "block", "demand_block"),
    ("demand", "only_rules", "demand_only_rules"),
    ("demand", "min_sample", "demand_min_sample"),
    ("demand", "age_days", "demand_age_days"),
    ("space", "min_percent", "space_min_percent"),
    ("space", "min_gb", "space_min_gb"),
    ("space", "pause_torrents", "space_pause_torrents"),
    ("retention", "enabled", "retention_enabled"),
    ("retention", "days", "retention_days"),
    ("behaviour", "season_packing", "season_packing"),
    ("behaviour", "episode_source", "episode_source"),
    ("behaviour", "match_episode_names", "match_episode_names"),
    ("flaresolverr", "url", "flaresolverr_url"),
    ("network", "proxy", "outbound_proxy"),
    ("automatic", "enabled", "auto_enabled"),
    ("automatic", "folder", "auto_folder"),
    ("automatic", "autobrr_folder", "auto_autobrr_folder"),
    ("automatic", "wait_hours", "auto_wait_hours"),
    ("automatic", "retry_minutes", "auto_retry_minutes"),
    ("automatic", "retry_first_minutes", "auto_retry_first_minutes"),
    ("automatic", "start", "auto_start"),
    ("automatic", "parallel", "auto_parallel"),
    ("automatic", "searches_per_hour", "auto_searches_per_hour"),
    ("autobrr", "url", "autobrr_url"),
    ("autobrr", "api_key", "autobrr_key"),
    ("gui", "username", "gui_username"),
    ("gui", "password", "gui_password"),
]


def candidates(explicit: str | None) -> list[Path]:
    if explicit:
        return [Path(explicit)]
    out = []
    if os.environ.get("NZB2SEED_CONFIG"):
        out.append(Path(os.environ["NZB2SEED_CONFIG"]))
    out.append(Path.cwd() / "nzb2seed.toml")
    out.append(Path(__file__).resolve().parent.parent / "nzb2seed.toml")
    return out


def find(explicit: str | None) -> Path | None:
    return next((p for p in candidates(explicit) if p.is_file()), None)


def from_sections(d: dict, path: Path) -> Config:
    cfg = Config(path=path)
    for section, key, attr in LAYOUT:
        if key in d.get(section, {}):
            setattr(cfg, attr, d[section][key])
    return finalize(cfg)


def finalize(cfg: Config) -> Config:
    # torrents nzb2seed adds are filed under their own category, so they are easy to tell
    # from the rest of qBittorrent. Set it to "-" to add them without any category.
    if not cfg.qbit_category:
        cfg.qbit_category = DEFAULT_QBIT_CATEGORY
    elif cfg.qbit_category == "-":
        cfg.qbit_category = ""
    if cfg.post_processing not in ("auto", "repair", "unpack"):
        raise ValueError("post_processing must be 'auto', 'repair' or 'unpack' (+Delete is never allowed)")
    raw = cfg.torrent_dir_raw or "torrents"
    cfg.torrent_dir = raw if os.path.isabs(raw) else str(Path(cfg.path).parent / raw)
    return cfg


def to_sections(cfg: Config) -> dict:
    out: dict = {}
    for section, key, attr in LAYOUT:
        out.setdefault(section, {})[key] = getattr(cfg, attr)
    return out


def load(explicit: str | None = None) -> Config:
    p = find(explicit)
    if p is None:
        raise SystemExit("no config found; copy nzb2seed.example.toml to nzb2seed.toml and fill it in "
                         f"(looked in: {', '.join(str(p) for p in candidates(explicit))})")
    with open(p, "rb") as fh:
        try:
            return from_sections(tomllib.load(fh), p)
        except ValueError as e:
            raise SystemExit(f"{p}: {e}")


def load_or_default(explicit: str | None = None) -> Config:
    """For the GUI: an existing config, or an empty one that Settings will create."""
    p = find(explicit)
    if p is None:
        return finalize(Config(path=candidates(explicit)[0] if explicit else Path.cwd() / "nzb2seed.toml"))
    with open(p, "rb") as fh:
        return from_sections(tomllib.load(fh), p)


def _toml(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v)  # JSON string escapes are valid TOML basic-string escapes
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k} = {_toml(x)}" for k, x in v.items()) + "}"
    if isinstance(v, list):
        return "[" + ", ".join(_toml(x) for x in v) + "]"
    raise TypeError(f"cannot write {type(v).__name__} to TOML")


def save(cfg: Config):
    lines = ["# nzb2seed configuration (written by the GUI; comments are not preserved)"]
    for section, values in to_sections(cfg).items():
        lines.append(f"\n[{section}]")
        lines += [f"{k} = {_toml(v)}" for k, v in values.items()]
    tmp = str(cfg.path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    try:
        os.chmod(tmp, 0o600)          # it holds API keys and passwords: owner only
    except OSError:
        pass
    os.replace(tmp, cfg.path)
