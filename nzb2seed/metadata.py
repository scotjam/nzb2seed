"""Outside data for a release: TVmaze (episode lists, IMDb/TVDB ids), srrDB (the files a
scene release shipped with, CRCs of what was inside its RARs), predb.net (pre time,
section, nukes) - and local MediaInfo / ffmpeg screenshots."""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import zlib

UA = "nzb2seed (+https://github.com/scotjam/nzb2seed)"
TVMAZE = "https://api.tvmaze.com"
SRRDB_API = "https://api.srrdb.com/v1"
SRRDB_DL = "https://www.srrdb.com/download"
SRRDB_WEB = "https://www.srrdb.com/release/details"
PREDB = "https://api.predb.net/"
PREDB_ME = "https://predb.me/"
XREL = "https://api.xrel.to/v2"

# FlareSolverr (https://github.com/FlareSolverr/FlareSolverr): used only when a site answers
# with a Cloudflare challenge. One browser session is reused per run, as its docs recommend.
FLARESOLVERR: str | None = None
_FS_SESSION: str | None = None

# Every lookup on the internet goes through this proxy (the VPN container's HTTP proxy) -
# never out from this machine's own address. No proxy: no lookups (they fail, visibly).
PROXY: str | None = None
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # FlareSolverr on the LAN


class NoProxy(urllib.error.URLError):
    def __init__(self):
        super().__init__("no outbound proxy set (Settings) - lookups only go out through the VPN")


def configure(flaresolverr_url: str | None, proxy: str | None = None):
    global FLARESOLVERR, PROXY
    FLARESOLVERR = (flaresolverr_url or "").rstrip("/") or None
    PROXY = (proxy or "").strip() or None


def _urlopen(req, timeout: float):
    """urlopen for internet sites: only ever through PROXY."""
    if not PROXY:
        raise NoProxy()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    return opener.open(req, timeout=timeout)


def _is_challenge(status: int, body: bytes) -> bool:
    head = body[:4000]
    return status in (403, 429, 503) and (b"Just a moment" in head or b"cf-chl" in head or b"challenge-platform" in head)


def _flaresolverr(cmd: dict, timeout: float = 130):
    req = urllib.request.Request(FLARESOLVERR + "/v1", data=json.dumps(cmd).encode(),
                                 headers={"Content-Type": "application/json"})
    with _LOCAL.open(req, timeout=timeout) as r:
        return json.loads(r.read())


def close_session():
    """Destroy the FlareSolverr browser session (call when a run is done)."""
    global _FS_SESSION
    if FLARESOLVERR and _FS_SESSION:
        try:
            _flaresolverr({"cmd": "sessions.destroy", "session": _FS_SESSION}, timeout=30)
        except Exception:
            pass
    _FS_SESSION = None


def _via_flaresolverr(url: str) -> tuple[int, str]:
    global _FS_SESSION
    if _FS_SESSION is None:
        _FS_SESSION = _flaresolverr({"cmd": "sessions.create"}, timeout=60).get("session")
    d = _flaresolverr({"cmd": "request.get", "url": url, "session": _FS_SESSION, "maxTimeout": 90000})
    if d.get("status") != "ok":
        raise RuntimeError(f"FlareSolverr: {d.get('message') or d.get('status')}")
    sol = d.get("solution") or {}
    return int(sol.get("status") or 0), sol.get("response") or ""


def _json_from_page(text: str):
    """FlareSolverr returns what the browser rendered; a JSON document shows up inside <pre>."""
    import html as _html
    t = text.strip()
    if t.startswith("<"):
        m = re.search(r"<pre[^>]*>(.*?)</pre>", t, re.S | re.I) or re.search(r"<body[^>]*>(.*?)</body>", t, re.S | re.I)
        t = _html.unescape(re.sub(r"<[^>]+>", "", m.group(1))) if m else t
    return json.loads(t)


def get_text(url: str, accept: str = "text/html") -> tuple[str | None, bool, str | None]:
    """(text, via_flaresolverr, None) - or (None, False, why). Cloudflare challenges go through
    FlareSolverr when one is configured."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    try:
        with _urlopen(req, timeout=30) as r:
            status, body = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read() or b""
    except (urllib.error.URLError, OSError) as e:
        return None, False, str(e)
    if _is_challenge(status, body):
        if not FLARESOLVERR:
            return None, False, "Cloudflare bot check (no FlareSolverr configured)"
        try:
            status, text = _via_flaresolverr(url)
        except Exception as e:
            return None, False, f"Cloudflare bot check; {e}"
        if status >= 400:
            return None, False, f"HTTP {status} through FlareSolverr"
        return text, True, None
    if status == 404:
        return None, False, None
    if status >= 400:
        return None, False, f"HTTP {status}"
    return body.decode("utf-8", errors="replace"), False, None


def get_json(url: str) -> tuple[object | None, str | None]:
    """(data, None) - or (None, why) when the site cannot be read. Cloudflare challenges go
    through FlareSolverr when one is configured."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with _urlopen(req, timeout=30) as r:
            status, body = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read() or b""
    except (urllib.error.URLError, OSError) as e:
        return None, str(e)
    if _is_challenge(status, body):
        if not FLARESOLVERR:
            return None, "Cloudflare bot check (no FlareSolverr configured)"
        try:
            status, text = _via_flaresolverr(url)
        except Exception as e:
            return None, f"Cloudflare bot check; {e}"
        if status >= 400:
            return None, f"HTTP {status} through FlareSolverr"
        try:
            return _json_from_page(text), None
        except ValueError:
            return None, "no JSON even through FlareSolverr"
    if status == 404:
        return None, None
    if status >= 400:
        return None, f"HTTP {status}"
    try:
        return json.loads(body), None
    except ValueError:
        return None, "the answer was not JSON"
_ARCHIVE_VOLUME = re.compile(r"\.(rar|r\d{2,3}|\d{3})$", re.I)


def _get(url: str, timeout: float = 30) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with _urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _json(url: str):
    data = _get(url)
    return json.loads(data) if data else None


# ---------------------------------------------------------------- TVmaze

def tvmaze_search(query: str) -> list[dict]:
    out = []
    for r in _json(f"{TVMAZE}/search/shows?q={urllib.parse.quote(query)}") or []:
        s = r["show"]
        ext = s.get("externals") or {}
        out.append({"id": s["id"], "name": s["name"], "premiered": s.get("premiered") or "",
                    "network": ((s.get("network") or s.get("webChannel") or {}).get("name") or ""),
                    "imdb": ext.get("imdb"), "tvdb": ext.get("thetvdb"), "url": s.get("url")})
    return out


def tvmaze_show(show_id: int) -> dict:
    s = _json(f"{TVMAZE}/shows/{show_id}")
    ext = s.get("externals") or {}
    return {"id": s["id"], "name": s["name"], "premiered": s.get("premiered") or "",
            "imdb": ext.get("imdb"), "tvdb": ext.get("thetvdb"), "url": s.get("url")}


def tvmaze_seasons(show_id: int) -> list[dict]:
    return [{"number": s["number"], "episodes": s.get("episodeOrder"), "premiere": s.get("premiereDate")}
            for s in _json(f"{TVMAZE}/shows/{show_id}/seasons") or [] if s.get("number") is not None]


def tvmaze_episodes(show_id: int, season: int) -> list[dict]:
    """Regular episodes of one season (specials left out)."""
    eps = _json(f"{TVMAZE}/shows/{show_id}/episodes") or []
    return [{"number": e["number"], "name": e.get("name") or "", "airdate": e.get("airdate") or ""}
            for e in eps if e.get("season") == season and e.get("number") is not None
            and (e.get("type") or "regular") == "regular"]


def links(show: dict) -> dict:
    out = {"TVmaze": show.get("url")}
    if show.get("imdb"):
        out["IMDb"] = f"https://www.imdb.com/title/{show['imdb']}/"
    if show.get("tvdb"):
        out["TVDB"] = f"https://thetvdb.com/?tab=series&id={show['tvdb']}"
    return {k: v for k, v in out.items() if v}


# ---------------------------------------------------------------- srrDB

def srrdb_details(release: str) -> dict | None:
    d = _json(f"{SRRDB_API}/details/{urllib.parse.quote(release)}")
    return d if d and d.get("files") is not None else None


def srrdb_file(release: str, path: str) -> bytes | None:
    """A file stored with the release on srrDB (.nfo, .sfv, proof images...)."""
    return _get(f"{SRRDB_DL}/file/{urllib.parse.quote(release)}/{urllib.parse.quote(path)}", timeout=60)


def release_files(details: dict) -> list[dict]:
    """Files of the original release other than its RAR volumes."""
    return [f for f in details.get("files", []) if not _ARCHIVE_VOLUME.search(f["name"])]


# ---------------------------------------------------------------- predb

def predb(release: str) -> dict | None:
    d = _json(f"{PREDB}?type=search&q={urllib.parse.quote(release)}")
    for r in (d or {}).get("data", []):
        if r.get("release", "").lower() == release.lower():
            return {"pretime": r.get("pretime"), "section": r.get("section"), "group": r.get("group"),
                    "nuked": bool(r.get("status")), "reason": r.get("reason") or "",
                    "url": "https://predb.net" + r["url"] if r.get("url") else None}
    return None


_PREDB_ME_POST = re.compile(
    r'<div class="post([^"]*)" id="(\d+)">(.*?)class="p-title"[^>]*>([^<]+)</a>', re.S)


def predb_me(release: str) -> dict:
    """predb.me's search page (its old JSON API is gone). It sits behind Cloudflare, so this
    goes through FlareSolverr when one is configured; a failure is reported, not hidden."""
    import html as _html
    text, _, why = get_text(f"{PREDB_ME}?search={urllib.parse.quote(release)}")
    if why:
        return {"available": False, "why": f"predb.me: {why}"}
    if text is None:
        return {"available": True, "found": False}
    for classes, pid, head, name in _PREDB_ME_POST.findall(text):
        if _html.unescape(name).strip().lower() != release.lower():
            continue
        t = re.search(r'class="p-time" data="(\d+)"', head)
        cats = re.findall(r'class="c-(?:adult|child)"[^>]*>([^<]+)</a>', head)
        nuke = re.search(r'class="[^"]*nuke[^"]*"[^>]*title="([^"]*)"', head)
        return {"available": True, "found": True, "pretime": int(t.group(1)) if t else None,
                "section": " / ".join(cats) or None,
                "nuked": "nuke" in classes.lower() or bool(nuke),
                "reason": _html.unescape(nuke.group(1)) if nuke else "",
                "url": f"{PREDB_ME}?post={pid}"}
    return {"available": True, "found": False}


def xrel(release: str) -> dict:
    """xrel.to's release lookup (behind Cloudflare too)."""
    data, why = get_json(f"{XREL}/release/info.json?dirname={urllib.parse.quote(release)}")
    if why:
        return {"available": False, "why": f"xrel.to: {why}"}
    if not isinstance(data, dict) or not data.get("dirname"):
        return {"available": True, "found": False}
    ext = data.get("ext_info") or {}
    return {"available": True, "found": True, "pretime": data.get("time"), "group": data.get("group_name"),
            "title": ext.get("title"), "url": data.get("link_href"),
            "nuked": bool((data.get("flags") or {}).get("nuke") or data.get("nuke"))}


# ---------------------------------------------------------------- local

def crc32(path: str) -> str:
    c = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8 << 20), b""):
            c = zlib.crc32(chunk, c)
    return f"{c & 0xFFFFFFFF:08X}"


def mediainfo(path: str) -> str:
    r = subprocess.run(["mediainfo", path], capture_output=True, text=True, errors="replace", timeout=300)
    text = r.stdout
    # MediaInfo prints the full local path; keep only the file name
    return re.sub(r"^(Complete name\s*:\s*).*$", lambda m: m.group(1) + os.path.basename(path), text, flags=re.M)


def duration(path: str) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                        "default=nw=1:nk=1", path], capture_output=True, text=True, timeout=120)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def screenshots(path: str, outdir: str, count: int = 4) -> list[str]:
    """``count`` PNG frames spread over the video (skipping the very start and end)."""
    os.makedirs(outdir, exist_ok=True)
    length = duration(path)
    if length <= 0:
        return []
    out = []
    for i in range(count):
        at = length * (0.15 + 0.7 * i / max(count - 1, 1))
        dst = os.path.join(outdir, f"screen{i + 1:02d}.png")
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{at:.2f}", "-i", path,
                            "-frames:v", "1", dst], capture_output=True, timeout=300)
        if r.returncode == 0 and os.path.exists(dst):
            out.append(dst)
    return out


def nfo_text(data: bytes) -> str:
    """NFOs are usually CP437 (DOS box drawing); fall back to UTF-8."""
    try:
        return data.decode("cp437")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")
