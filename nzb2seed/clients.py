"""Thin clients for the Prowlarr, SABnzbd and qBittorrent Web APIs."""
from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import requests

TIMEOUT = 30


class ApiError(RuntimeError):
    pass


def short_reason(r) -> str:
    """One readable line from an error answer (sites often send a whole HTML page)."""
    text = r.text or ""
    if "<" in text:
        m = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
        body = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
        text = (m.group(1) + ": " if m else "") + re.sub(r"<[^>]+>", " ", body)
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return f"HTTP {r.status_code}" + (f" - {text[:160]}" if text else "")


# ================================================================ Prowlarr

@dataclass
class Release:
    title: str
    protocol: str          # "usenet" | "torrent"
    indexer: str
    indexer_id: int
    size: int
    guid: str
    download_url: str
    info_url: str
    publish_date: str
    grabs: int | None
    seeders: int | None
    files: int | None

    @classmethod
    def from_api(cls, r: dict) -> "Release":
        return cls(
            title=r.get("title") or r.get("fileName") or "",
            protocol=(r.get("protocol") or "").lower(),
            indexer=r.get("indexer") or "",
            indexer_id=r.get("indexerId") or 0,
            size=r.get("size") or 0,
            guid=r.get("guid") or "",
            download_url=r.get("downloadUrl") or "",
            info_url=r.get("infoUrl") or r.get("commentUrl") or "",
            publish_date=r.get("publishDate") or "",
            grabs=r.get("grabs"),
            seeders=r.get("seeders"),
            files=r.get("files"),
        )


class Prowlarr:
    def __init__(self, url: str, api_key: str, proxy: str = ""):
        self.url = url.rstrip("/")
        self.s = requests.Session()
        self.s.trust_env = False            # no environment proxies: Prowlarr is on the LAN
        self.s.headers["X-Api-Key"] = api_key
        self.proxy = (proxy or "").strip()

    def status(self) -> str:
        r = self.s.get(f"{self.url}/api/v1/system/status", timeout=TIMEOUT)
        if r.status_code != 200:
            raise ApiError(f"Prowlarr: HTTP {r.status_code} {r.text[:200]}")
        n = len(self.s.get(f"{self.url}/api/v1/indexer", timeout=TIMEOUT).json())
        return f"Prowlarr {r.json().get('version', '?')}, {n} indexer(s)"

    def search(self, query: str, indexer_ids=None, categories=None, limit: int = 1000) -> list[Release]:
        params = [("query", query), ("type", "search"), ("limit", str(limit)), ("offset", "0")]
        params += [("indexerIds", str(i)) for i in indexer_ids or []]
        params += [("categories", str(c)) for c in categories or []]
        r = self.s.get(f"{self.url}/api/v1/search", params=params, timeout=300)
        if r.status_code != 200:
            raise ApiError(f"Prowlarr search failed: HTTP {r.status_code} {r.text[:300]}")
        return [Release.from_api(x) for x in r.json()]

    def fetch(self, release: Release) -> bytes:
        """Download the .nzb / .torrent through Prowlarr's link.

        Prowlarr fetches torrent files itself, inside whatever network it sits in (a VPN
        container). For Usenet indexers Prowlarr always answers with the indexer's own
        link (a redirect it cannot switch off) - that link is only ever followed through
        the outbound proxy (the VPN container's HTTP proxy), never from this machine's own
        address, which would show the indexer a second IP."""
        url = release.download_url
        home = urlsplit(self.url).netloc.lower()
        for _ in range(5):
            off_host = urlsplit(url).netloc.lower() != home
            if off_host and not self.proxy:
                raise ApiError(
                    f"{release.indexer} sends its downloads straight from the indexer (Prowlarr's "
                    f"\"Redirect\"), and no outbound proxy is set - nzb2seed will not reach an indexer "
                    f"from this machine's own address. Set the VPN container's HTTP proxy in Settings")
            if off_host:
                # the indexer itself: without Prowlarr's key, and only through the proxy
                r = requests.get(url, timeout=120, allow_redirects=False,
                                 proxies={"http": self.proxy, "https": self.proxy})
            else:
                r = self.s.get(url, timeout=120, allow_redirects=False)
            if r.is_redirect or r.status_code in (301, 302, 303, 307, 308):
                url = requests.compat.urljoin(url, r.headers.get("Location", ""))
                if url.startswith("magnet:"):
                    raise ApiError(f"{release.indexer} only offers a magnet link for this release; "
                                   "a .torrent file is required")
                continue
            if r.status_code != 200:
                raise ApiError(f"download from {release.indexer} failed: {short_reason(r)}")
            return r.content
        raise ApiError("too many redirects while downloading release")


# ================================================================ SABnzbd

PP_REPAIR = 1          # +Repair
PP_UNPACK = 2          # +Repair/Unpack      (archives are kept)
PP_DELETE = 3          # +Repair/Unpack/Delete — never used here

SAB_DONE = {"Completed"}
SAB_FAILED = {"Failed"}


class SABnzbd:
    def __init__(self, url: str, api_key: str):
        self.url = url.rstrip("/") + "/api"
        self.key = api_key
        self.s = requests.Session()

    def _call(self, mode: str, method="GET", files=None, **params) -> dict:
        params = {"mode": mode, "apikey": self.key, "output": "json", **params}
        if method == "POST":
            r = self.s.post(self.url, params=params, files=files, timeout=TIMEOUT)
        else:
            r = self.s.get(self.url, params=params, timeout=TIMEOUT)
        if r.status_code != 200:
            raise ApiError(f"SABnzbd {mode} failed: HTTP {r.status_code} {r.text[:300]}")
        data = r.json()
        if isinstance(data, dict) and data.get("status") is False:
            raise ApiError(f"SABnzbd {mode} failed: {data.get('error')}")
        return data

    def version(self) -> str:
        return self._call("version").get("version", "?")

    def config(self) -> dict:
        return self._call("get_config").get("config", {})

    def add_nzb(self, nzb: bytes, name: str, category: str, pp: int, priority: int = 0) -> str:
        if pp not in (PP_REPAIR, PP_UNPACK):
            raise ValueError("refusing to add an NZB with a post-processing level that deletes archives")
        data = self._call("addfile", method="POST",
                          files={"name": (name + ".nzb", nzb, "application/x-nzb")},
                          nzbname=name, cat=category or "*", pp=str(pp),
                          script="None", priority=str(priority))
        ids = data.get("nzo_ids") or []
        if not ids:
            raise ApiError(f"SABnzbd did not accept the NZB: {data}")
        return ids[0]

    def queue_slot(self, nzo_id: str) -> dict | None:
        q = self._call("queue", nzo_ids=nzo_id).get("queue", {})
        for s in q.get("slots", []):
            if s.get("nzo_id") == nzo_id:
                return s
        return None

    def history_slot(self, nzo_id: str) -> dict | None:
        h = self._call("history", nzo_ids=nzo_id).get("history", {})
        for s in h.get("slots", []):
            if s.get("nzo_id") == nzo_id:
                return s
        return None

    def ensure_pp(self, nzo_id: str, pp: int) -> str:
        """Make sure a queued job is +Repair (or +Unpack) and never +Delete."""
        slot = self.queue_slot(nzo_id)
        if slot is None:
            return "not in queue (already finished?)"
        label = {PP_REPAIR: "+Repair", PP_UNPACK: "+Repair/Unpack"}[pp]
        if "unpackopts" not in slot:
            return label + " (requested; this SABnzbd does not report it)"
        current = str(slot.get("unpackopts", ""))
        if current != str(pp):
            self._call("change_opts", value=nzo_id, value2=str(pp))
            slot = self.queue_slot(nzo_id)
            current = str((slot or {}).get("unpackopts", ""))
            if current != str(pp):
                raise ApiError(f"SABnzbd kept post-processing at {current!r} for {nzo_id}")
        return label

    def status(self, nzo_id: str) -> tuple[str, dict]:
        slot = self.queue_slot(nzo_id)
        if slot is not None:
            return "Queued:" + slot.get("status", ""), slot
        slot = self.history_slot(nzo_id)
        if slot is not None:
            return slot.get("status", ""), slot
        return "Unknown", {}

    def delete_history(self, nzo_id: str):
        self._call("history", name="delete", value=nzo_id, del_files="0")


# ================================================================ qBittorrent

CHECKING = {"checkingUP", "checkingDL", "checkingResumeData", "queuedForChecking",
            "moving", "allocating", "metaDL"}


class QBittorrent:
    def __init__(self, url: str, username: str = "", password: str = ""):
        self.url = url.rstrip("/")
        self.s = requests.Session()
        self.s.headers["Referer"] = self.url
        self.username, self.password = username, password
        self._api = None

    def login(self):
        if not self.username:
            return  # relying on qBittorrent's "bypass authentication for clients in whitelisted subnets"
        r = self.s.post(f"{self.url}/api/v2/auth/login", timeout=TIMEOUT,
                        data={"username": self.username, "password": self.password})
        if r.status_code != 200 or r.text.strip() not in ("Ok.", ""):
            raise ApiError(f"qBittorrent login failed: HTTP {r.status_code} {r.text[:200]}")

    def _req(self, method: str, path: str, **kw):
        kw.setdefault("timeout", TIMEOUT)
        r = self.s.request(method, f"{self.url}/api/v2/{path}", **kw)
        if r.status_code == 403:
            self.login()
            r = self.s.request(method, f"{self.url}/api/v2/{path}", **kw)
        return r

    def _ok(self, r, what):
        if r.status_code != 200:
            raise ApiError(f"qBittorrent {what} failed: HTTP {r.status_code} {r.text[:200]}")
        return r

    def api_version(self) -> tuple[int, ...]:
        if self._api is None:
            v = self._ok(self._req("GET", "app/webapiVersion"), "webapiVersion").text.strip()
            self._api = tuple(int(x) for x in v.split(".") if x.isdigit())
        return self._api

    def app_version(self) -> str:
        return self._ok(self._req("GET", "app/version"), "version").text.strip()

    def preferences(self) -> dict:
        return self._ok(self._req("GET", "app/preferences"), "preferences").json()

    def info(self, infohash: str) -> dict | None:
        r = self._ok(self._req("GET", "torrents/info", params={"hashes": infohash}), "info")
        lst = r.json()
        return lst[0] if lst else None

    def files(self, infohash: str) -> list[dict]:
        return self._ok(self._req("GET", "torrents/files", params={"hash": infohash}), "files").json()

    def piece_states(self, infohash: str) -> list[int]:
        """0 = not downloaded, 1 = downloading, 2 = downloaded (verified)."""
        return self._ok(self._req("GET", "torrents/pieceStates", params={"hash": infohash}),
                        "pieceStates").json()

    def add_stopped(self, torrent_bytes: bytes, filename: str, save_path: str,
                    category: str = "", tags: str = ""):
        data = {
            "savepath": save_path,
            "paused": "true",            # WebAPI < 2.11 (qBittorrent 4.x)
            "stopped": "true",           # WebAPI >= 2.11 (qBittorrent 5.x)
            "skip_checking": "false",
            "autoTMM": "false",
            "useDownloadPath": "false",  # never look in the "incomplete" folder
            "contentLayout": "Original",
            "root_folder": "true",       # pre-4.3.2 spelling of contentLayout=Original
        }
        if category:
            data["category"] = category
        if tags:
            data["tags"] = tags
        r = self._req("POST", "torrents/add", data=data,
                      files={"torrents": (filename, torrent_bytes, "application/x-bittorrent")})
        self._ok(r, "add")
        if r.text.strip() == "Fails.":
            raise ApiError("qBittorrent rejected the torrent (Fails.)")

    def stop(self, infohash: str):
        path = "torrents/stop" if self.api_version() >= (2, 11) else "torrents/pause"
        self._ok(self._req("POST", path, data={"hashes": infohash}), "stop")

    def start(self, infohash: str):
        path = "torrents/start" if self.api_version() >= (2, 11) else "torrents/resume"
        self._ok(self._req("POST", path, data={"hashes": infohash}), "start")

    def set_location(self, infohash: str, location: str):
        self._ok(self._req("POST", "torrents/setLocation",
                           data={"hashes": infohash, "location": location}), "setLocation")

    def recheck(self, infohash: str):
        self._ok(self._req("POST", "torrents/recheck", data={"hashes": infohash}), "recheck")

    def wait_idle(self, infohash: str, timeout: float = 24 * 3600, on_tick=None,
                  min_wait: float = 3.0) -> dict:
        """Wait until the torrent is no longer checking/moving; return its final info."""
        start = time.monotonic()
        settled = 0
        while True:
            t = self.info(infohash)
            if t is None:
                raise ApiError("torrent disappeared from qBittorrent")
            if on_tick:
                on_tick(t)
            busy = t.get("state") in CHECKING
            settled = 0 if busy else settled + 1
            if settled >= 2 and time.monotonic() - start >= min_wait:
                return t
            if time.monotonic() - start > timeout:
                raise ApiError("timed out waiting for qBittorrent")
            time.sleep(1.5)
