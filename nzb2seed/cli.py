"""nzb2seed: build a 100% complete torrent entirely from Usenet downloads.

  gui      open the web GUI (search, pick, build, watch progress, settings)
  search   query Prowlarr and list Usenet + torrent results
  run      search -> NZB(s) to SABnzbd (+Repair) -> .torrent -> lay files out
           like the torrent -> fix .nfo line endings -> remove extras ->
           add to qBittorrent stopped -> set location -> recheck -> 100%
  assemble same as run from "lay files out" onward, for a .torrent and
           folder(s) you already have
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys

from . import matching
from .clients import ApiError, Prowlarr, Release
from .config import Config, load
from .pipeline import (Abort, Options, execute_assemble, execute_run, gb, group_selection,
                       local_release, pair, preview_plan, search)
from .torrent import parse
from .report import Cancelled, info, step


def age(date: str) -> str:
    try:
        d = dt.datetime.fromisoformat(date.replace("Z", "+00:00"))
        return f"{(dt.datetime.now(dt.timezone.utc) - d).days}d"
    except ValueError:
        return "?"


def table(rows: list[Release], start: int = 1):
    for i, r in enumerate(rows, start):
        extra = f"S:{r.seeders}" if r.protocol == "torrent" else f"G:{r.grabs}"
        files = f"{r.files}f" if r.files else ""
        print(f"  {i:>3}  {r.protocol[:3]:3}  {r.indexer[:16]:16}  {gb(r.size):>10}  "
              f"{files:>5}  {extra:>7}  {age(r.publish_date):>6}  {r.title}")


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        raise Abort("no input available; use --yes for non-interactive runs")


def pick_numbers(prompt: str, n: int, allow_many: bool) -> list[int]:
    while True:
        s = ask(prompt)
        if not s:
            return []
        try:
            nums = [int(x) for x in re.split(r"[,\s]+", s) if x]
        except ValueError:
            print("    enter number(s), e.g. 3 or 1,4,5")
            continue
        if all(1 <= x <= n for x in nums) and (allow_many or len(nums) == 1):
            return [x - 1 for x in nums]
        print(f"    choose between 1 and {n}")


def choose_for_file(cfg: Config, args, t, path) -> tuple[Release, list[list[Release]]]:
    """Pair a .torrent file the user already has with NZBs found through Prowlarr."""
    torrent = local_release(t, path)
    query = args.query or t.name
    step(f"Searching Prowlarr for NZBs of: {query}")
    _, nzbs = search(cfg, query)
    info(f"{len(nzbs)} usenet result(s)")
    return finish_choice(cfg, args, torrent, nzbs)


def choose(cfg: Config, args) -> tuple[Release, list[list[Release]]]:
    step(f"Searching Prowlarr for: {args.query}")
    torrents, nzbs = search(cfg, args.query)
    if args.indexer:
        torrents = [t for t in torrents if args.indexer.casefold() in t.indexer.casefold()]
    info(f"{len(torrents)} torrent and {len(nzbs)} usenet result(s)")
    if not torrents:
        raise Abort("no torrent results to build")

    key = matching.norm(args.query)
    exact = [t for t in torrents if matching.norm(t.title) == key]
    if args.yes:
        pool = exact or torrents
        if len(pool) != 1:
            table(pool)
            raise Abort("several torrents match; narrow the query or use --indexer")
        torrent = pool[0]
    else:
        torrents.sort(key=lambda t: (matching.norm(t.title) != key, t.title))
        print("\nTorrents:")
        table(torrents)
        idx = pick_numbers("\nTorrent to build [number, empty = quit]: ", len(torrents), False)
        if not idx:
            raise Abort("nothing chosen")
        torrent = torrents[idx[0]]
    return finish_choice(cfg, args, torrent, nzbs)


def finish_choice(cfg: Config, args, torrent: Release, nzbs: list[Release]):
    groups, nzbs, how = pair(cfg, Prowlarr(cfg.prowlarr_url, cfg.prowlarr_key), torrent, nzbs)
    info(f"usenet: {how}")

    if not args.yes:
        if groups:
            print("\nPlanned NZB(s):")
            table([g[0] for g in groups])
            a = ask("\nUse these? [Y/n/m = pick manually]: ").lower()
        else:
            a = "m"
        if a in ("n", "no"):
            raise Abort("cancelled")
        if a == "m":
            nzbs = matching.rank_nzbs(nzbs)
            print("\nUsenet results:")
            table(nzbs)
            idx = pick_numbers("\nNZB number(s), comma separated [empty = quit]: ", len(nzbs), True)
            if not idx:
                raise Abort("nothing chosen")
            groups = group_selection([nzbs[i] for i in idx])
    if not groups:
        info("nothing picked: nzb2seed will find the NZBs itself, closest match first "
             "(whole torrent, then season, then episode)")
    return torrent, groups


def options(args) -> Options:
    return Options(pp=getattr(args, "pp", None), output_dir=args.output_dir,
                   no_cleanup=args.no_cleanup, local_verify=args.local_verify,
                   no_qbit=args.no_qbit, start=args.start, dry_run=args.dry_run,
                   retry_bad=False if getattr(args, "no_retry", False) else None)


def cmd_search(cfg: Config, args) -> int:
    torrents, nzbs = search(cfg, args.query)
    for name, rows in (("torrent", torrents), ("usenet", nzbs)):
        rows = sorted(rows, key=lambda r: r.title)
        print(f"\n{name} ({len(rows)}):")
        table(rows)
    return 0


def cmd_run(cfg: Config, args) -> int:
    data = None
    if getattr(args, "torrent", None):
        with open(args.torrent, "rb") as fh:
            data = fh.read()
        t = parse(data)
        tor_rel, groups = choose_for_file(cfg, args, t, args.torrent)
    else:
        if not args.query:
            raise Abort("give a search query, or --torrent FILE")
        tor_rel, groups = choose(cfg, args)
    if args.dry_run:
        if data is not None:
            preview_plan(cfg, Prowlarr(cfg.prowlarr_url, cfg.prowlarr_key), t, t.name)
        info("dry run: stopping before anything is downloaded")
        return 0
    execute_run(cfg, options(args), tor_rel, groups, torrent_data=data)
    return 0


def cmd_assemble(cfg: Config, args) -> int:
    execute_assemble(cfg, options(args), args.torrent, args.source)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="nzb2seed", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="path to nzb2seed.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gui", help="open the web GUI")
    g.add_argument("--host", default="127.0.0.1",
                   help="address to listen on (default 127.0.0.1; 0.0.0.0 = the whole LAN)")
    g.add_argument("--port", type=int, default=9797)
    g.add_argument("--username", help="login user name (overrides the config; default admin)")
    g.add_argument("--password", help="login password (overrides the config; default nzb2seed, "
                   "change it in Settings). An empty password switches the login off, which "
                   "only lets in this machine and private LAN addresses")
    g.add_argument("--allowed-host", action="append", default=[],
                   help="extra host name the GUI is reached by (e.g. a DNS name or reverse proxy)")
    g.add_argument("--no-browser", action="store_true", help="do not open a browser window")

    s = sub.add_parser("search", help="list Prowlarr results")
    s.add_argument("query")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--output-dir", help="local folder the torrent's data goes in "
                        "(default: [paths] output_dir, else the SABnzbd job's parent folder)")
    common.add_argument("--no-cleanup", action="store_true", help="keep files that are not in the torrent")
    common.add_argument("--local-verify", action="store_true",
                        help="hash every piece locally before adding to qBittorrent")
    common.add_argument("--no-qbit", action="store_true", help="stop after laying out the files")
    common.add_argument("--start", action="store_true", help="start seeding once the recheck hits 100%%")
    common.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    common.add_argument("--no-retry", action="store_true",
                        help="if pieces fail, stop instead of downloading other posts of the release")

    r = sub.add_parser("run", parents=[common], help="search, download and build a torrent from Usenet")
    r.add_argument("query", nargs="?", help="release name (or search text) to look for in Prowlarr; "
                   "with --torrent it defaults to the torrent's name")
    r.add_argument("--torrent", help="build this .torrent file (skips the torrent search; "
                   "NZBs are still found through Prowlarr)")
    r.add_argument("--indexer", help="only consider torrents from indexers whose name contains this")
    r.add_argument("--pp", choices=["auto", "repair", "unpack"],
                   help="SABnzbd post-processing: auto (+Repair when the torrent holds RARs, "
                        "+Repair/Unpack when it holds unpacked files), repair, or unpack; never +Delete")
    r.add_argument("-y", "--yes", action="store_true", help="no prompts; requires an unambiguous match")

    a = sub.add_parser("assemble", parents=[common], help="build from a .torrent and existing folder(s)")
    a.add_argument("torrent", help=".torrent file")
    a.add_argument("source", nargs="+", help="folder(s) holding the NZB download(s)")

    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except AttributeError:
            pass

    if args.cmd == "gui":
        from .gui import serve
        return serve(args.config, args.host, args.port, args.password, not args.no_browser,
                     args.allowed_host, args.username)

    from . import report
    report.interactive = not getattr(args, "yes", False) and sys.stdin.isatty()
    cfg = load(args.config)
    try:
        return {"search": cmd_search, "run": cmd_run, "assemble": cmd_assemble}[args.cmd](cfg, args)
    except (Abort, Cancelled) as e:
        print(f"\n!! {e}", file=sys.stderr)
        return 2
    except ApiError as e:
        print(f"\n!! {e}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
