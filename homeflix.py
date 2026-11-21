#!/usr/bin/env python3
"""
Homeflix - a self-hosted, Netflix-style media server for your LAN.

* Scans ~/Downloads/movies (video) and ~/Music (audio) by default
* Netflix-like web UI: profiles, hero banner, rows, Continue Watching, My List,
  TV-show grouping (S01E02 naming), albums, search
* Streams with HTTP Range (instant seeking) for browser-friendly files and
  transcodes on the fly with ffmpeg for everything else (HEVC, AVI, WMV, ...)
* Subtitles: sidecar .srt/.vtt and embedded text subs
* Optional password, files served by opaque ID only (no path traversal)

* Finds commercial breaks in TV recordings and skips them (detector from TabloView)

Python 3.8+ standard library only. ffmpeg/ffprobe are optional but strongly
recommended (thumbnails, durations, tags, transcoding, finding ad breaks).

Copyright (C) 2026 crash101g
Commercial-break detector: Copyright (C) JoeDV, from TabloView
(https://github.com/JoeDV/TabloView), used under the GPL-3.0.

This program is free software: you can redistribute it and/or modify it under the
terms of the GNU General Public License as published by the Free Software Foundation,
either version 3 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with this
program. If not, see <https://www.gnu.org/licenses/>.
"""
import argparse
import hashlib
import hmac
import http.server
import json
import mimetypes
import os
import random
import re
import secrets
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

VERSION = "1.4.1"

VIDEO_EXT = {".mp4", ".m4v", ".mkv", ".webm", ".mov", ".avi", ".wmv", ".flv", ".ts",
             ".m2ts", ".mts", ".mpg", ".mpeg", ".3gp", ".ogv", ".divx", ".vob"}
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".wav", ".wma",
             ".aiff", ".aif", ".ape", ".mka", ".alac", ".wv"}
IMG_NAMES = ("cover", "folder", "front", "album", "poster", "albumart")
TEXT_SUB_CODECS = {"subrip", "ass", "ssa", "mov_text", "webvtt", "text"}
WEB_VCODECS = {"h264", "vp8", "vp9", "av1"}
WEB_ACODECS = {"aac", "mp3", "opus", "vorbis", "flac"}
WEB_VCONTAINERS = {".mp4", ".m4v", ".webm", ".mov", ".mkv", ".ogv"}
WEB_AUDIO_EXT = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".wav"}
# Transcode quality: (x264 preset, CRF, max bitrate in Mbit/s). On a 6-core i5-8500T every level
# still encodes 720p at 2x+ realtime; "high" is about +2 dB SSIM over "balanced".
QUALITY = {"balanced": ("veryfast", 22, 8), "high": ("faster", 18, 20), "max": ("fast", 16, 40)}

WINDOWS = os.name == "nt"
MACOS = sys.platform == "darwin"


def app_dirs():
    """(config, data, cache) folders, in each OS's usual place."""
    home = Path.home()
    if WINDOWS:
        roaming = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming") / "Homeflix"
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / "Homeflix"
        return roaming, roaming, local / "Cache"
    if MACOS:
        support = home / "Library" / "Application Support" / "Homeflix"
        return support, support, home / "Library" / "Caches" / "Homeflix"
    env = os.environ.get
    return (Path(env("XDG_CONFIG_HOME") or home / ".config") / "homeflix",
            Path(env("XDG_DATA_HOME") or home / ".local" / "share") / "homeflix",
            Path(env("XDG_CACHE_HOME") or home / ".cache") / "homeflix")


CONF_DIR, XDG_DATA, XDG_CACHE = app_dirs()
CONF_FILE = CONF_DIR / "env"

def find_tool(name):
    """Find ffmpeg/ffprobe on PATH, or where common installers put them (a background
    task or launchd agent may not have the same PATH as your terminal)."""
    found = shutil.which(name)
    if found:
        return found
    if WINDOWS:
        local = os.environ.get("LOCALAPPDATA", "")
        dirs = [os.path.join(local, "Microsoft", "WinGet", "Links"),
                os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "chocolatey", "bin"),
                os.path.join(str(Path.home()), "scoop", "shims"),
                os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "ffmpeg", "bin"),
                r"C:\ffmpeg\bin"]
    else:
        dirs = ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", "/usr/bin", "/snap/bin"]
    return shutil.which(name, path=os.pathsep.join(dirs))


THUMB_RETRY_SECONDS = 24 * 3600  # a thumbnail that couldn't be made is tried again after this long

FFMPEG = find_tool("ffmpeg")
FFPROBE = find_tool("ffprobe")
NICE = shutil.which("nice")
# On Windows, keep ffmpeg from flashing a console window when running without one (pythonw).
SUBPROC = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if WINDOWS else {}
# Thumbnails are background work: run them at low priority.
LOW_PRIO = dict(SUBPROC, creationflags=SUBPROC["creationflags"] | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)) \
    if WINDOWS else {}


def load_settings(path: Path):
    """Read KEY="value" lines from the settings file into os.environ.

    Real environment variables and command-line flags still win. Values may be bare or
    double-quoted with \\ and \" escapes (the format the installers write, which systemd
    also reads)."""
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return
    for line in lines:
        k, sep, v = line.strip().partition("=")
        k = k.strip()
        if not sep or not k.startswith("HOMEFLIX_"):
            continue
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] == '"':
            out, i, v = [], 0, v[1:-1]
            while i < len(v):
                if v[i] == "\\" and i + 1 < len(v):
                    i += 1
                out.append(v[i])
                i += 1
            v = "".join(out)
        os.environ.setdefault(k, v)


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


def atomic_write(path: Path, data: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: antivirus or a reader briefly holding the file
            if attempt == 4:
                raise
            time.sleep(0.1)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


# ----------------------------------------------------------------------------
# Name parsing
# ----------------------------------------------------------------------------
EP_RE = re.compile(r"(?i)(?:^|[\s._\-\[(])s(\d{1,2})[\s._\-]?e(\d{1,3})(?:[\s._\-]?e\d{1,3})?|(?:^|[\s._\-])(\d{1,2})x(\d{2})(?=[\s._\-]|$)")
YEAR_RE = re.compile(r"[\s.(\[_\-]((?:19[2-9]|20[0-4])\d)(?=[\s.)\]_\-]|$)")
JUNK_RE = re.compile(
    r"(?i)[\s\[(\-]*\b(2160p|1080p|1080i|720p|576p|480p|4k|uhd|bluray|blu-ray|brrip|bdrip|"
    r"webrip|web-dl|webdl|web|hdtv|hdrip|dvdrip|dvdscr|xvid|x264|x265|h\.?264|h\.?265|hevc|"
    r"10bit|8bit|aac\d?(?:\.\d)?|ac3|dts|ddp?\d?(?:\.\d)?|atmos|hdr10?|dv|remux|proper|repack|"
    r"extended|unrated|imax|yts(?:\.\w+)?|yify|rarbg|internal|multi|dual)\b.*$")
TRACKNO_RE = re.compile(r"^\s*(?:(\d)-)?(\d{1,3})[\s.\-_]+(?=\D)")


def tidy(s: str) -> str:
    s = re.sub(r"[._]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip(" -[](){}")


def clean_title(stem: str):
    """Return (title, year) from a release-style file name."""
    s = " " + re.sub(r"[._]+", " ", stem) + " "
    year = None
    hits = [m for m in YEAR_RE.finditer(s) if m.start() > 1]
    if hits:
        m = hits[-1]
        year = int(m.group(1))
        s = s[:m.start()]
    s = JUNK_RE.sub("", s)
    t = tidy(s)
    return (t or tidy(stem)), year


def parse_video_name(path: Path, root: Path):
    stem = path.stem
    info = {}
    m = EP_RE.search(stem)
    if m:
        season = int(m.group(1) or m.group(3))
        ep = int(m.group(2) or m.group(4))
        show_part = stem[:m.start()]
        show, year = clean_title(show_part) if show_part.strip(" ._-") else ("", None)
        if not show:
            parent = path.parent
            if re.match(r"(?i)^(season|series|s)\s*\d+$", parent.name) and parent.parent != root:
                parent = parent.parent
            if parent != root:
                show, year = clean_title(parent.name)
        ep_title, _ = clean_title(stem[m.end():]) if stem[m.end():].strip(" ._-") else ("", None)
        info.update(show=show or "Unknown Show", season=season, episode=ep,
                    epTitle=ep_title, title=ep_title or f"Episode {ep}", year=year)
        return info
    title, year = clean_title(stem)
    if path.parent != root:
        ptitle, pyear = clean_title(path.parent.name)
        if (pyear and not year) or len(title) <= 3 or title.lower() in ("movie", "film", "video"):
            title, year = ptitle, pyear or year
    info.update(title=title, year=year)
    return info


# ----------------------------------------------------------------------------
# Library
# ----------------------------------------------------------------------------
class Library:
    def __init__(self, roots, make_thumbs=True):
        self.roots = [Path(r).expanduser().resolve() for r in roots]
        self.items = {}
        self.version = 0
        self.scanning = False
        self.lock = threading.Lock()
        self.scan_lock = threading.Lock()
        self.make_thumbs = make_thumbs and bool(FFMPEG)
        self.probe_path = XDG_CACHE / "probe.json"
        self.probe_cache = load_json(self.probe_path, {})
        self.thumb_dir = XDG_CACHE / "thumbs"
        self.sub_dir = XDG_CACHE / "subs"
        self.thumb_dir.mkdir(parents=True, exist_ok=True)
        self.sub_dir.mkdir(parents=True, exist_ok=True)
        self.thumb_sem = threading.Semaphore(2)
        self.thumb_locks = {}

    # -- scanning ------------------------------------------------------------
    def scan(self):
        if not self.scan_lock.acquire(blocking=False):
            return
        self.scanning = True
        t0 = time.time()
        try:
            found = {}
            for root in self.roots:
                if not root.is_dir():
                    log(f"WARNING: library folder not found: {root}")
                    continue
                for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
                    dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                    d = Path(dirpath)
                    lower = {f.lower(): f for f in filenames}
                    for fn in filenames:
                        if fn.startswith("."):
                            continue
                        ext = os.path.splitext(fn)[1].lower()
                        kind = "video" if ext in VIDEO_EXT else "audio" if ext in AUDIO_EXT else None
                        if not kind:
                            continue
                        if kind == "video" and re.search(r"(?i)\bsample\b", fn):
                            continue
                        p = d / fn
                        try:
                            it = self._base_item(p, root, kind, filenames, lower)
                        except OSError:
                            continue
                        found[it["id"]] = it
            with self.lock:
                # keep already-probed metadata for unchanged files
                for iid, it in found.items():
                    old = self.items.get(iid)
                    if old and old["_key"] == it["_key"] and old.get("_probed"):
                        found[iid] = old
                self.items = found
                self.version += 1
            todo = [it for it in found.values() if not it.get("_probed")]
            if todo and FFPROBE:
                log(f"Probing {len(todo)} file(s) with ffprobe...")
                done = 0
                with ThreadPoolExecutor(max_workers=4) as pool:
                    for _ in pool.map(self._probe_item, todo):
                        done += 1
                        if done % 50 == 0:
                            with self.lock:
                                self.version += 1
                            self._save_probe_cache()
                self._save_probe_cache()
            with self.lock:
                self.version += 1
            nv = sum(1 for i in found.values() if i["kind"] == "video")
            log(f"Library: {nv} videos, {len(found) - nv} audio files ({time.time() - t0:.1f}s)")
        finally:
            self.scanning = False
            self.scan_lock.release()
        self._prune_cache()
        if self.make_thumbs:
            threading.Thread(target=self._pregen_thumbs, daemon=True).start()

    def _base_item(self, p: Path, root: Path, kind, siblings, lower):
        st = p.stat()
        sp = str(p)
        iid = hashlib.sha1(sp.encode("utf-8", "surrogateescape")).hexdigest()[:14]
        rel = p.relative_to(root)
        it = {
            "id": iid, "kind": kind, "size": st.st_size, "added": int(st.st_mtime),
            "folder": rel.parent.as_posix() if str(rel.parent) != "." else root.name,
            "file": p.name, "duration": 0,
            "_path": sp, "_root": str(root), "_key": f"{st.st_size}|{int(st.st_mtime)}",
            "_ext": p.suffix.lower(), "_sidesubs": [], "_img": None,
        }
        if kind == "video":
            it.update(parse_video_name(p, root))
            # sidecar subtitles: Movie.srt, Movie.en.srt, Movie.English.vtt ...
            stem_l = p.stem.lower()
            for fn in siblings:
                fl = fn.lower()
                if fl.endswith((".srt", ".vtt")) and fl.startswith(stem_l):
                    mid = fn[len(p.stem):-4].strip(". _-")
                    it["_sidesubs"].append({"file": str(p.parent / fn), "label": mid or "Subtitles"})
            if not it["_sidesubs"]:
                subs_dir = p.parent / "Subs"
                if subs_dir.is_dir():
                    for f in sorted(subs_dir.iterdir()):
                        if f.suffix.lower() in (".srt", ".vtt"):
                            it["_sidesubs"].append({"file": str(f), "label": tidy(f.stem)})
            for ext in (".jpg", ".jpeg", ".png", ".webp"):
                cand = lower.get(p.stem.lower() + ext) or lower.get(p.stem.lower() + "-poster" + ext)
                if cand:
                    it["_img"] = str(p.parent / cand)
                    break
            else:
                if p.parent != root:
                    for n in ("poster", "cover", "folder"):
                        for ext in (".jpg", ".jpeg", ".png", ".webp"):
                            if n + ext in lower:
                                it["_img"] = str(p.parent / lower[n + ext])
                                break
                        if it["_img"]:
                            break
            it["direct"] = it["_ext"] in WEB_VCONTAINERS
        else:
            parts = rel.parts
            stem = p.stem
            m = TRACKNO_RE.match(stem)
            track = int(m.group(2)) if m else 0
            disc = int(m.group(1)) if m and m.group(1) else 1
            title = tidy(stem[m.end():]) if m else tidy(stem)
            artist = album = None
            if len(parts) >= 3:
                artist, album = parts[-3], parts[-2]
            elif len(parts) == 2:
                album = parts[0]
            if not artist and " - " in title:
                a, t = title.split(" - ", 1)
                artist, title = a.strip(), t.strip()
            it.update(title=title or stem, artist=artist or "Unknown Artist",
                      albumArtist=artist or "Unknown Artist", album=album or "Singles",
                      track=track, disc=disc, year=None, genre=None)
            for n in IMG_NAMES:
                for ext in (".jpg", ".jpeg", ".png", ".webp"):
                    if n + ext in lower:
                        it["_img"] = str(p.parent / lower[n + ext])
                        break
                if it["_img"]:
                    break
            else:
                imgs = sorted(f for f in siblings if f.lower().endswith((".jpg", ".jpeg", ".png")))
                if imgs:
                    it["_img"] = str(p.parent / imgs[0])
            it["direct"] = it["_ext"] in WEB_AUDIO_EXT
        return it

    def _probe_item(self, it):
        key = it["_path"] + "|" + it["_key"]
        data = self.probe_cache.get(key)
        if data is None:
            try:
                out = subprocess.run(
                    [FFPROBE, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", it["_path"]],
                    capture_output=True, timeout=30, stdin=subprocess.DEVNULL, **SUBPROC)
                raw = json.loads(out.stdout or b"{}")
            except Exception:
                raw = {}
            data = self._summarize_probe(raw)
            self.probe_cache[key] = data
        self._apply_probe(it, data)
        it["_probed"] = True

    @staticmethod
    def _summarize_probe(raw):
        fmt = raw.get("format", {}) or {}
        tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
        d = {"duration": float(fmt.get("duration") or 0), "tags": {}, "subs": [], "v": None, "a": None, "cover": False}
        for k in ("title", "artist", "album", "album_artist", "track", "disc", "date", "genre"):
            if tags.get(k):
                d["tags"][k] = str(tags[k])[:200]
        for s in raw.get("streams", []) or []:
            ct = s.get("codec_type")
            disp = s.get("disposition") or {}
            stags = {k.lower(): v for k, v in (s.get("tags") or {}).items()}
            if ct == "video" and disp.get("attached_pic"):
                d["cover"] = True
            elif ct == "video" and not d["v"]:
                d["v"] = {"codec": s.get("codec_name"), "w": s.get("width"), "h": s.get("height"),
                          "pix": s.get("pix_fmt")}
            elif ct == "audio" and not d["a"]:
                d["a"] = {"codec": s.get("codec_name"), "ch": s.get("channels")}
                for k in ("title", "artist", "album"):  # ogg/opus keep tags on the stream
                    if stags.get(k) and k not in d["tags"]:
                        d["tags"][k] = str(stags[k])[:200]
            elif ct == "subtitle":
                d["subs"].append({"codec": s.get("codec_name"), "lang": stags.get("language"),
                                  "title": stags.get("title"), "forced": bool(disp.get("forced"))})
            if not d["duration"] and s.get("duration"):
                try:
                    d["duration"] = float(s["duration"])
                except ValueError:
                    pass
        return d

    def _apply_probe(self, it, d):
        it["duration"] = round(d.get("duration") or 0, 1)
        v, a = d.get("v"), d.get("a")
        it["_v"], it["_a"] = v, a
        it["_cover"] = d.get("cover")
        it["_embsubs"] = [s for s in d.get("subs", [])]
        if it["kind"] == "video":
            if v:
                it["width"], it["height"] = v.get("w"), v.get("h")
            vc = (v or {}).get("codec")
            ac = (a or {}).get("codec")
            it["vcodec"] = vc
            it["direct"] = (it["_ext"] in WEB_VCONTAINERS and vc in WEB_VCODECS
                            and (ac is None or ac in WEB_ACODECS)
                            and (v or {}).get("pix") in (None, "yuv420p", "yuvj420p"))
        else:
            ac = (a or {}).get("codec") or ""
            it["direct"] = it["_ext"] in WEB_AUDIO_EXT and (ac in WEB_ACODECS or ac.startswith("pcm_") or not ac)
            t = d.get("tags", {})
            if t.get("title"):
                it["title"] = t["title"]
            if t.get("artist"):
                it["artist"] = t["artist"]
            if t.get("album"):
                it["album"] = t["album"]
            it["albumArtist"] = t.get("album_artist") or it["artist"]
            if t.get("track"):
                m = re.match(r"\d+", t["track"])
                if m:
                    it["track"] = int(m.group())
            if t.get("disc"):
                m = re.match(r"\d+", t["disc"])
                if m:
                    it["disc"] = int(m.group())
            if t.get("date"):
                m = re.search(r"\d{4}", t["date"])
                if m:
                    it["year"] = int(m.group())
            if t.get("genre"):
                it["genre"] = t["genre"]

    def _save_probe_cache(self):
        with self.lock:
            live = {i["_path"] + "|" + i["_key"] for i in self.items.values()}
            data = {k: v for k, v in self.probe_cache.items() if k in live}
        try:
            atomic_write(self.probe_path, json.dumps(data))
        except OSError as e:
            log("could not save probe cache:", e)

    # -- public views -----------------------------------------------------------
    def get(self, iid):
        with self.lock:
            return self.items.get(iid)

    def subs_for(self, it):
        out = []
        for i, s in enumerate(it.get("_sidesubs", [])):
            out.append({"label": s["label"], "src": ("side", i)})
        for i, s in enumerate(it.get("_embsubs", []) or []):
            if s.get("codec") in TEXT_SUB_CODECS:
                lab = s.get("title") or (s.get("lang") or "").upper() or f"Track {i + 1}"
                if s.get("lang") and s.get("title") and s["lang"].lower() not in s["title"].lower():
                    lab = f"{s['title']} ({s['lang']})"
                if s.get("forced"):
                    lab += " [forced]"
                out.append({"label": lab, "src": ("emb", i)})
        return out

    def public(self):
        with self.lock:
            items = list(self.items.values())
            ver = self.version
        pub = []
        for it in items:
            p = {k: v for k, v in it.items() if not k.startswith("_")}
            if it["kind"] == "video":
                p["subs"] = [s["label"] for s in self.subs_for(it)]
            pub.append(p)
        return {"version": ver, "scanning": self.scanning, "items": pub,
                "transcode": bool(FFMPEG), "roots": [r.name for r in self.roots]}

    # -- thumbnails -------------------------------------------------------------
    @staticmethod
    def _fresh(path: Path, it, max_age=None) -> bool:
        """A cache file made from this item that is still good: it exists, is newer than the file
        (a file replaced under the same name gets a new one) and, with max_age, not older than that."""
        try:
            m = path.stat().st_mtime
        except OSError:
            return False
        return m >= it["added"] and (max_age is None or time.time() - m < max_age)

    def thumb_cached(self, it) -> bool:
        """Nothing to do for this item's thumbnail: it has its own picture, a good cached one, or a
        recent "couldn't make one" marker."""
        if it.get("_img") and os.path.isfile(it["_img"]):
            return True
        return (self._fresh(self.thumb_dir / f"{it['id']}.jpg", it)
                or self._fresh(self.thumb_dir / f"{it['id']}.none", it, THUMB_RETRY_SECONDS))

    def thumb(self, it):
        if it.get("_img") and os.path.isfile(it["_img"]):
            return it["_img"]
        out = self.thumb_dir / f"{it['id']}.jpg"
        miss = self.thumb_dir / f"{it['id']}.none"
        if self._fresh(out, it):
            return str(out)
        # a thumbnail that couldn't be made is tried again after a day, or when the file changes
        if self._fresh(miss, it, THUMB_RETRY_SECONDS) or not self.make_thumbs:
            return str(out) if out.exists() else None
        lk = self.thumb_locks.setdefault(it["id"], threading.Lock())
        with lk:
            if self._fresh(out, it):
                return str(out)
            with self.thumb_sem:
                ok = self._gen_thumb(it, out)
        if not ok:
            miss.touch()
            try:
                out.unlink()  # an old picture of a file that has been replaced
            except OSError:
                pass
            return None
        try:
            miss.unlink()
        except OSError:
            pass
        return str(out)

    def _gen_thumb(self, it, out: Path):
        pre = [NICE, "-n", "15"] if NICE else []
        tmp = out.with_suffix(".tmp.jpg")
        if it["kind"] == "audio":
            if it.get("_probed") and not it.get("_cover"):
                return False
            cmds = [[FFMPEG, "-nostdin", "-v", "error", "-y", "-i", it["_path"], "-an", "-map", "0:v:0",
                     "-frames:v", "1", "-vf", "scale=500:-2", str(tmp)]]
        else:
            dur = it.get("duration") or 0
            ss = min(max(dur * 0.12, 3), 600) if dur else 20
            base = ["-i", it["_path"], "-map", "0:v:0", "-frames:v", "1",
                    "-vf", "thumbnail=40,scale=640:-2", "-q:v", "4", str(tmp)]
            cmds = [[FFMPEG, "-nostdin", "-v", "error", "-y", "-ss", f"{ss:.1f}"] + base,
                    [FFMPEG, "-nostdin", "-v", "error", "-y"] + base]
        for c in cmds:
            try:
                subprocess.run(pre + c, capture_output=True, timeout=60, stdin=subprocess.DEVNULL, **(LOW_PRIO or SUBPROC))
            except Exception:
                continue
            if tmp.exists() and tmp.stat().st_size > 0:
                os.replace(tmp, out)
                return True
        try:
            tmp.unlink()
        except OSError:
            pass
        return False

    def _prune_cache(self):
        """Delete cached thumbnails, extracted subtitles and ad breaks of files no longer in the
        library (deleted, renamed or moved). Skipped while a library folder is missing (an unplugged
        drive or a NAS that's offline), so its files' thumbnails aren't thrown away."""
        missing = [str(r) for r in self.roots if not r.is_dir()]
        if missing:
            return
        with self.lock:
            ids = set(self.items)
        if not ids:
            return
        removed = 0
        for folder in (self.thumb_dir, self.sub_dir, XDG_CACHE / "ads"):
            try:
                files = list(folder.iterdir())
            except OSError:
                continue
            for f in files:
                # cache files are named after the item: <id>.jpg, <id>.none, <id>.tmp.jpg, <id>_<n>.vtt, <id>.json
                iid = f.name.split(".")[0].split("_")[0]
                if re.fullmatch(r"[0-9a-f]{14}", iid) and iid not in ids:
                    try:
                        f.unlink()
                        removed += 1
                    except OSError:
                        pass
        if removed:
            log(f"Cache: removed {removed} file(s) left from videos and songs that are gone")

    def _pregen_thumbs(self):
        with self.lock:
            items = list(self.items.values())
        # albums only need one cover per folder
        seen_dirs = set()
        todo = []
        for it in sorted(items, key=lambda x: -x["added"]):
            if it["kind"] == "audio":
                d = os.path.dirname(it["_path"])
                if d in seen_dirs:
                    continue
                seen_dirs.add(d)
            todo.append(it)
        n = 0
        for it in todo:
            if not self.thumb_cached(it) and self.thumb(it):
                n += 1
        if n:
            log(f"Generated {n} thumbnail(s)")

    # -- subtitles -------------------------------------------------------------
    def subtitle_vtt(self, it, idx, offset=0.0):
        subs = self.subs_for(it)
        if idx < 0 or idx >= len(subs):
            return None
        kind, n = subs[idx]["src"]
        if kind == "side":
            raw = Path(it["_sidesubs"][n]["file"]).read_bytes()
            text = None
            for enc in ("utf-8-sig", "cp1252", "latin-1"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            vtt = srt_to_vtt(text) if not text.lstrip().startswith("WEBVTT") else text
        else:
            cache = self.sub_dir / f"{it['id']}_{n}.vtt"
            if not self._fresh(cache, it):
                if not FFMPEG:
                    return None
                r = subprocess.run([FFMPEG, "-nostdin", "-v", "error", "-i", it["_path"], "-map", f"0:s:{n}",
                                    "-f", "webvtt", "pipe:1"], capture_output=True, timeout=300,
                                   stdin=subprocess.DEVNULL, **SUBPROC)
                if r.returncode != 0 or not r.stdout:
                    return None
                cache.write_bytes(r.stdout)
            vtt = cache.read_text(encoding="utf-8", errors="replace")
        if offset:
            vtt = shift_vtt(vtt, offset)
        return vtt


TS_RE = re.compile(r"(\d{1,2}:)?(\d{1,2}):(\d{2})[.,](\d{3})")


def srt_to_vtt(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for line in text.split("\n"):
        if "-->" in line:
            line = re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", line)
        lines.append(line)
    return "WEBVTT\n\n" + "\n".join(lines)


def shift_vtt(vtt, offset):
    def to_s(m):
        h = int(m.group(1)[:-1]) if m.group(1) else 0
        return h * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 1000

    def fmt(s):
        s = max(0.0, s)
        h, rem = divmod(s, 3600)
        mnt, sec = divmod(rem, 60)
        return f"{int(h):02d}:{int(mnt):02d}:{sec:06.3f}"

    out = []
    for line in vtt.split("\n"):
        if "-->" in line:
            line = TS_RE.sub(lambda m: fmt(to_s(m) - offset), line)
        out.append(line)
    return "\n".join(out)


# ----------------------------------------------------------------------------
# Profiles / progress state
# ----------------------------------------------------------------------------
class State:
    COLORS = ["#e50914", "#2b7bd6", "#e5a00d", "#1fa463", "#8e44ad", "#e67e22", "#16a085", "#c0392b"]

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.data = load_json(path, {})
        self.data.setdefault("profiles", [])
        self.data.setdefault("progress", {})
        self.data.setdefault("mylist", {})
        if not self.data["profiles"]:
            self.data["profiles"].append({"id": "p1", "name": "Me", "color": self.COLORS[0]})
            self._save()

    def _save(self):
        atomic_write(self.path, json.dumps(self.data, indent=1))

    def profiles(self):
        with self.lock:
            return list(self.data["profiles"])

    def add_profile(self, name):
        name = str(name).strip()[:24]
        if not name:
            raise ValueError("name required")
        with self.lock:
            if len(self.data["profiles"]) >= 8:
                raise ValueError("max 8 profiles")
            pid = "p" + secrets.token_hex(4)
            col = self.COLORS[len(self.data["profiles"]) % len(self.COLORS)]
            p = {"id": pid, "name": name, "color": col}
            self.data["profiles"].append(p)
            self._save()
            return p

    def delete_profile(self, pid):
        with self.lock:
            self.data["profiles"] = [p for p in self.data["profiles"] if p["id"] != pid]
            self.data["progress"].pop(pid, None)
            self.data["mylist"].pop(pid, None)
            self._save()

    def _valid(self, pid):
        return any(p["id"] == pid for p in self.data["profiles"])

    def get(self, pid):
        with self.lock:
            return {"progress": self.data["progress"].get(pid, {}), "mylist": self.data["mylist"].get(pid, [])}

    def set_progress(self, pid, iid, pos, dur, done=None):
        with self.lock:
            if not self._valid(pid):
                raise ValueError("bad profile")
            pos, dur = float(pos), float(dur or 0)
            if done is None:
                done = bool(dur and pos >= dur * 0.93)
            prog = self.data["progress"].setdefault(pid, {})
            prog[iid] = {"pos": round(pos, 1), "dur": round(dur, 1), "done": done, "updated": int(time.time())}
            if len(prog) > 3000:  # trim oldest
                for k in sorted(prog, key=lambda k: prog[k]["updated"])[:500]:
                    prog.pop(k)
            self._save()

    def clear_progress(self, pid, iid):
        with self.lock:
            self.data["progress"].get(pid, {}).pop(iid, None)
            self._save()

    def set_mylist(self, pid, key, on):
        with self.lock:
            if not self._valid(pid):
                raise ValueError("bad profile")
            lst = self.data["mylist"].setdefault(pid, [])
            if on and key not in lst:
                lst.insert(0, str(key)[:300])
            elif not on and key in lst:
                lst.remove(key)
            self._save()


# ----------------------------------------------------------------------------
# Commercial breaks
#
# The detector below (from THUMB_W down to _not_over_rating) is taken from TabloView by JoeDV,
# https://github.com/JoeDV/TabloView (app/commercials.py, commit b65ee70, GPL-3.0), with these
# changes: int.bit_count() replaced for Python 3.8; TabloView's Tablo, database, settings and
# Claude code left out. Its comments describe TabloView's test recordings and are kept as written.
#
# How it works (from TabloView): one low-priority ffmpeg pass per recording measures black frames,
# silences, the four corners of every second (where networks keep their logo, which they take off
# for the ads) and a tiny colour thumbnail per second. A break is where the logo is gone for a
# minute or more, or, for channels without a logo, a long run that doesn't look like the show; its
# edges are snapped to the black, silent moments where the show stops and starts. It is tuned to
# skip too little rather than too much.
# ----------------------------------------------------------------------------
def _popcount(x: int) -> int:
    return bin(x).count("1")


THUMB_W, THUMB_H = 32, 18  # the per-second frame size (rgb24)
BINS = 64  # 4 levels each of red, green and blue
SMOOTH_SECONDS = 15  # majority vote over this window evens out single odd frames
# US commercial breaks run a minute or more; shorter odd-looking stretches are the show's own graphics
# (e.g. Jeopardy's red Final Jeopardy reveal, 25 s)
MIN_BREAK_SECONDS = 45
MERGE_GAP_SECONDS = 10  # a moment of show-like frames inside a break doesn't split it
SNAP_SECONDS = 8  # move a break edge to a black frame this close
# An edge without a black frame to anchor it moves this far into the break: better a few seconds of
# ad than skipping any of the show (what Tablo's own commercial skip got wrong)
UNANCHORED_TRIM = 3
MIN_SEPARATION = 0.15  # show and ad scores must be at least this far apart to trust the result
MAX_AD_PIECE_SECONDS = 300  # between two black-and-silent moments: longer than this is show
# A piece this short next to an ad piece is part of the same break, whatever it looks like: a dim
# one-minute drug ad opened a Tracker break and scored like the show (2026-10-25, 62 s). Raising this
# from 45 changed nothing on the five test recordings
SHORT_PIECE_SECONDS = 65
AD_PIECE_SHARE = 0.85  # a piece averaging below this share of the show's long pieces is ads
LEVEL_SECONDS = 0.05  # the sound's loudness is measured this often (speech dips between syllables)

# The detector, saved with each result: results from an older one are redone (1: colour only;
# 2: the channel logo, black frames and silences; 3: up to a minute of show-like ad next to a break;
# 4: a break ends where the show comes back, not a fixed time before the logo does). Not raised for
# _without_long_pieces (2026-10-30): every copy would re-read every unwatched recording, logo channels
# too; a show's recordings are redone on request instead (its page, or a recording's menu)
METHOD = 4

# The channel logo: the bottom-right corner of a 160x90 copy of each second's frame (the logo is a few
# pixels of light text there, still clear at that size)
FRAME_W, FRAME_H = 160, 90
CORNER_W, CORNER_H = 36, 22
LIGHTER = 12  # a logo pixel is this much lighter than the 5x5 around it, whatever the picture
LOGO_MIN_PIXELS = 12  # fewer pixels than this that are lighter most of the time: no logo to go by
LOGO_SEPARATION = 0.3  # seconds with and without the logo must score at least this far apart
LOGO_SMOOTH_SECONDS = 9
LOGO_MERGE_GAP = 35  # a network promo with the logo on (15-30 s), inside a break, doesn't split it
LOGO_SNAP_SECONDS = 12  # move a break edge to a black-and-silent moment (or a black frame) this close
# The logo comes back this long after the show does: Fox shows its TV rating badge instead for the
# first seconds back (Hell's Kitchen: 15-16 s after each of four breaks). If a channel brings it back
# at once, the cost is a few seconds of ad left in, not show skipped
LOGO_RETURN_LAG = 15
# ABC brings it back later still: 18-37 s after the show (Shark Tank, 2026-10-29; ending each break
# LOGO_RETURN_LAG before the logo skipped up to 22 s of the show). So a break ends at its last
# black-and-silent moment up to this long before the logo returns
LOGO_RETURN_MAX = 45
# A break has the logo gone for at least LOGO_SOLID_SECONDS in a row (ignoring blips of it of up to
# LOGO_BLIP_SECONDS). ABC's see-through logo dropped out for 5-10 s at a time on bright scenes, and
# 87 s of Shark Tank was taken for a break
LOGO_SOLID_SECONDS = 20
LOGO_BLIP_SECONDS = 1
# A faint logo: CBS's see-through eye is ~5 px wide in the 160x90 copy, too faint for the corners above
# (and lit in under half the seconds: it vanishes on bright scenes, and ads are a quarter of the time).
# In a 480x270 copy's corners (EYE_W x EYE_H) it's a clear ring. A pixel EYE_LIGHTER lighter than the
# 5x5 around it (worked out by FFmpeg) and lit in over EYE_PIXEL_SHARE of the seconds is the logo; at
# least EYE_MIN_PIXELS of them, or there's none; a second with EYE_ON of them lit has it on. On
# Survivor that's on in 91% of show seconds and 6% of ad seconds
EYE_SCALE_W, EYE_SCALE_H = 480, 270
EYE_W, EYE_H = 108, 66
EYE_LIGHTER = 8
EYE_PIXEL_SHARE = 0.35
EYE_MIN_PIXELS = 20
EYE_ON = 0.3
EYE_EDGE = 3
EYE_GONE_EARLY = 20  # a break starts at the first black frame this long after the eye goes
EYE_SAMPLE = 5  # which pixels are the logo: judged from every 5th second (the same answer, a fifth of the work)
# The TV rating (TV-14 V...) in the top-left third of the same 480x270 copy: networks show it when the
# show starts and as it comes back from breaks (ABC's 9-1-1 and NBC's SVU, 2026-10-30: after every
# break; CBS: at the top of each hour). ABC and CBS put it in a box, NBC just writes it, so it's
# learned rather than looked for: what stays put (lit in RATING_STEADY seconds running) in a compact
# patch of the corner during the recording's first RATING_OPENING seconds, the most of it; then every
# second where RATING_MATCH of those pixels are lit again. On five recordings the show's own rating
# matched at 0.87-1.0 and nothing else above 0.56: promos carry ratings too, but others or smaller
# (9-1-1: TV-14 LV and TV-PG DL), as do The Price Is Right's price tags and busy pictures. Trusted only
# when seen again after the opening (else it might be an ad's still graphic). A suggestion from the user
RATING_W, RATING_H = 160, 90
RATING_OPENING = 180
RATING_STEADY = 8
RATING_MIN_PIXELS = 60
RATING_MAX_W, RATING_MAX_H = 70, 66  # 9-1-1's box 36x43, SVU's lettering 39x40, CBS's box 35x33
RATING_MATCH = 0.75
RATING_MIN_SECONDS = 2  # seen in at least this many seconds of one showing
RATING_GAP = 3  # seconds it may go unseen within one showing
RATING_LOOKBACK = 60  # the show came back at the last black frame up to this long before its rating
# NBC lets its own promos run after the logo is back (SVU: 13 and 15 s): a rating this soon after a
# break's end moves the end to the last black-and-silent moment before the rating
RATING_AFTER = 30


def histogram(frame: bytes) -> List[float]:
    """64-bin colour histogram of one rgb24 frame, as fractions of the pixels."""
    h = [0] * BINS
    for p in range(0, len(frame) - 2, 3):
        h[(frame[p] >> 6) * 16 + (frame[p + 1] >> 6) * 4 + (frame[p + 2] >> 6)] += 1
    total = len(frame) // 3 or 1
    return [x / total for x in h]


def black_frames(ffmpeg_log: str) -> List[float]:
    """Midpoints of the black frames FFmpeg's blackdetect reported, in seconds."""
    return [(float(a) + float(b)) / 2
            for a, b in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", ffmpeg_log)]


def _mean(hs: Sequence[List[float]]) -> List[float]:
    n = len(hs) or 1
    return [sum(h[k] for h in hs) / n for k in range(BINS)]


def _similarity(a: List[float], b: List[float]) -> float:
    return sum(min(x, y) for x, y in zip(a, b))


def _threshold(values: List[float]) -> Tuple[float, float, float]:
    """Split values into two groups (1-D k-means): (threshold, low centre, high centre)."""
    lo, hi = min(values), max(values)
    for _ in range(20):
        t = (lo + hi) / 2
        low = [v for v in values if v < t] or [lo]
        high = [v for v in values if v >= t] or [hi]
        lo, hi = sum(low) / len(low), sum(high) / len(high)
    return (lo + hi) / 2, lo, hi


def _runs(flags: List[bool]) -> List[Tuple[int, int]]:
    """[start, end) index ranges where flags is True."""
    runs, start = [], None
    for i, f in enumerate(flags + [False]):
        if f and start is None:
            start = i
        elif not f and start is not None:
            runs.append((start, i))
            start = None
    return runs


def loudness(ffmpeg_log: str) -> List[float]:
    """The sound's level (dB) every LEVEL_SECONDS, in order, from astats/ametadata in the log."""
    return [float(v) if v not in ("-inf", "nan") else -120.0
            for v in re.findall(r"lavfi\.astats\.Overall\.RMS_level=(\S+)", ffmpeg_log)]


def silences(ffmpeg_log: str) -> List[Tuple[float, float]]:
    """(start, end) of the silences FFmpeg's silencedetect reported, in seconds."""
    return [(float(a), float(b)) for a, b in
            re.findall(r"silence_start: ([\d.]+)[\s\S]*?silence_end: ([\d.]+)", ffmpeg_log)]


def lighter_pixels(corner: bytes) -> List[bool]:
    """Which pixels of a grey CORNER_W x CORNER_H corner are clearly lighter than the 5x5 around them."""
    w, h = CORNER_W, CORNER_H
    # Sums of every rectangle from the top left, so each 5x5 average is four lookups
    sat = [[0] * (w + 1) for _ in range(h + 1)]
    for y in range(h):
        row = 0
        for x in range(w):
            row += corner[y * w + x]
            sat[y + 1][x + 1] = sat[y][x + 1] + row
    out = []
    for y in range(h):
        y0, y1 = max(0, y - 2), min(h, y + 3)
        for x in range(w):
            x0, x1 = max(0, x - 2), min(w, x + 3)
            total = sat[y1][x1] - sat[y0][x1] - sat[y1][x0] + sat[y0][x0]
            out.append(corner[y * w + x] - total / ((y1 - y0) * (x1 - x0)) > LIGHTER)
    return out


def logo_scores(corners: List[Union[bytes, Sequence[bool]]]) -> Optional[Tuple[List[float], float, float]]:
    """Per second, how much the channel's logo is in this corner, the score that separates "on"
    from "off", and how clearly they separate; None if there's no logo here to go by. A corner is
    its grey pixels, or already which of them are lit (lighter_pixels; the test recordings keep that)."""
    marks = [lighter_pixels(c) if isinstance(c, (bytes, bytearray)) else c for c in corners]
    n = len(marks)
    if n < 120:
        return None
    freq = [sum(m[i] for m in marks) / n for i in range(len(marks[0]))]
    logo = [i for i, f in enumerate(freq) if f > 0.5]  # light in most seconds: the logo
    rest = [i for i, f in enumerate(freq) if f < 0.2]  # the picture
    if len(logo) < LOGO_MIN_PIXELS or not rest:
        return None
    # Logo pixels lit, beyond what a busy picture lights everywhere anyway
    scores = [sum(m[i] for i in logo) / len(logo) - sum(m[i] for i in rest) / len(rest) for m in marks]
    t, lo, hi = _threshold(scores)
    # A QR code in an ad's corner can pass for the logo (about two thirds of its pixels lit); a
    # stricter cut-off would lose the logo over bright scenes too, so LOGO_MERGE_GAP bridges such ads
    return (scores, t, hi - lo) if hi - lo >= LOGO_SEPARATION else None


# A "logo" gone this long in the middle of a show isn't the channel's: no break runs that long
LOGO_GONE_MAX = 7 * 60


def _gone_too_long(scores: List[float], t: float) -> bool:
    """Whether this corner's mark is missing for longer than any break, away from the start and end.
    The Floor (Fox, 2026-11-16) had a graphic top left for its first 28 minutes only: it separated
    a little more clearly than Fox's own logo (bottom right), so the rest of the show, 29 minutes,
    was taken for three breaks of 8 to 11 minutes."""
    return any(b["end"] - b["start"] > LOGO_GONE_MAX for b in _logo_breaks(scores, t, [], []))


def best_logo(corners: List[Sequence[bytes]]) -> Optional[Tuple[List[float], float]]:
    """The corner with the channel's logo (most networks: bottom right; some channels use another):
    the clearest one whose mark is never gone for longer than a break. None: no logo to go by."""
    found = [(k, f) for k in range(len(corners[0])) if (f := logo_scores([c[k] for c in corners]))]
    if not found:
        return None
    # None that stays: not a logo at all, but something like a station's weather bar, there over show
    # and ads alike until it's taken down (Jeopardy on WTHR, 2026-11-16: gone for 10 minutes of the show,
    # which became a 9½-minute "break"). Colour, as for channels without a logo
    likely = [(k, f) for k, f in found if not _gone_too_long(f[0], f[1])]
    if not likely:
        return None
    k, (scores, t, _) = max(likely, key=lambda kf: kf[1][2])
    if not _logo_breaks(scores, t, [], []):
        # Never gone, so not the channel's logo but a station's box over show and ads alike (Scrubs,
        # ABC, 2026-11-12: "Storm Track", the time and temperature bottom right for all 31 minutes, and
        # the network's see-through logo just above it, lit in only half the seconds of the show). The
        # logo may still be in that corner, among the pixels that come and go
        inner = _logo_among_rest([c[k] for c in corners])
        return inner if inner and not _gone_too_long(*inner) and _logo_breaks(*inner, [], []) else None
    return scores, t


# Lit in nearly every second: part of something that stays (a station's box), not a logo that leaves
STEADY_SHARE = 0.9
# In _logo_among_rest, a logo pixel is lit this much more often with the logo than without it
TOGETHER = 0.3


def _logo_among_rest(corners: List[Union[bytes, Sequence[bool]]]) -> Optional[Tuple[List[float], float]]:
    """A corner's logo once what stays on screen is set aside: of the pixels lit often but not always,
    the ones that come and go together. From all of them, a first guess at the seconds with the logo;
    then, a few times over, the pixels lit much more in those seconds than in the others, and the
    seconds from those. None if they don't settle into a clear logo."""
    marks = [lighter_pixels(c) if isinstance(c, (bytes, bytearray)) else c for c in corners]
    n = len(marks)
    freq = [sum(m[i] for m in marks) / n for i in range(len(marks[0]))]
    logo = [i for i, f in enumerate(freq) if 0.2 < f <= STEADY_SHARE]
    rest = [i for i, f in enumerate(freq) if f < 0.2]
    for _ in range(5):
        if len(logo) < LOGO_MIN_PIXELS or not rest:
            return None
        scores = [sum(m[i] for i in logo) / len(logo) - sum(m[i] for i in rest) / len(rest) for m in marks]
        t, lo, hi = _threshold(scores)
        on = [s >= t for s in scores]
        with_logo = sum(on)
        if not 0 < with_logo < n:
            return None
        lit_on = [sum(m[i] for m, o in zip(marks, on) if o) / with_logo for i in range(len(freq))]
        lit_off = [sum(m[i] for m, o in zip(marks, on) if not o) / (n - with_logo) for i in range(len(freq))]
        new = [i for i in range(len(freq)) if lit_on[i] - lit_off[i] > TOGETHER]
        if new == logo:
            break
        logo = new
    return (scores, t) if hi - lo >= LOGO_SEPARATION else None


def _logo_breaks(scores: List[float], t: float, edges: List[float], blacks: List[float],
                 nearest_start: bool = False) -> List[Dict[str, float]]:
    """Breaks from the seconds without the logo; edges are the black-and-silent moments."""
    n = len(scores)
    on = [s >= t for s in scores]
    half = LOGO_SMOOTH_SECONDS // 2
    smooth = [sum(on[max(0, i - half):i + half + 1]) * 2 > len(on[max(0, i - half):i + half + 1]) for i in range(n)]
    merged: List[List[int]] = []
    for s, e in _runs([not x for x in smooth]):
        if merged and s - merged[-1][1] <= LOGO_MERGE_GAP:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    # Where the logo is gone for good (a second or two of it showing aside): a break has at least one
    # such stretch and its edges are at the first and last of them. A see-through logo (ABC) flickers
    # out on bright scenes in the show, which the smoothing alone took for breaks or joined onto them
    off = [not x for x in on]
    for s, e in _runs(on):
        if e - s <= LOGO_BLIP_SECONDS:
            off[s:e] = [True] * (e - s)
    solid = [r for r in _runs(off) if r[1] - r[0] >= LOGO_SOLID_SECONDS]
    trimmed = []
    for s, e in merged:
        inside = [r for r in solid if r[0] < e and r[1] > s]
        if inside:
            trimmed.append([max(s, inside[0][0]), min(e, inside[-1][1])])
    merged = trimmed

    def snap(t: float, marks: List[float]) -> Optional[float]:
        near = [b for b in marks if abs(b - t) <= LOGO_SNAP_SECONDS]
        return min(near, key=lambda b: abs(b - t)) if near else None

    def last_before(t: float, marks: List[float]) -> Optional[float]:
        near = [b for b in marks if t - LOGO_RETURN_MAX <= b <= t]
        return max(near) if near else None

    breaks = []
    for s, e in merged:
        if e - s < MIN_BREAK_SECONDS:
            continue
        if s == 0 or e == n:
            # At the start or end of the recording: the show's own opening and closing often run
            # without the logo too, so only up to (from) the last (first) black-and-silent moment
            inside = [b for b in edges if s <= b <= e]
            if not inside:
                continue
            start, end = (0.0, max(inside)) if s == 0 else (min(inside), float(n))
            if end - start >= 10:
                breaks.append({"start": round(start, 1), "end": round(end, 1)})
            continue
        # The faint logo (nearest_start): CBS takes the eye off up to ~15 s before it cuts away (The Price
        # Is Right, 2026-10-30: gone at 35:48, black at 36:01), so the first black frame after it goes
        if nearest_start:
            after = [b for b in blacks if s - 2 <= b <= s + EYE_GONE_EARLY]
            start = min(after) if after else None
        else:
            start = snap(float(s), edges) or snap(float(s), blacks)
        start = float(s) + 1 if start is None else start
        # The show comes back before the logo does, at the break's last black frame (silent on ABC,
        # not always on Fox); without one, LOGO_RETURN_LAG before the logo
        end = last_before(float(e), blacks)
        if end is None or end <= start:
            back = float(e) - LOGO_RETURN_LAG
            end = snap(back, blacks)
            end = back if end is None else end
        if end - start >= MIN_BREAK_SECONDS:
            breaks.append({"start": round(start, 1), "end": round(end, 1)})
    return breaks


def find_breaks(hists: List[List[float]], blacks: List[float], corners: Optional[List[Sequence[bytes]]] = None,
                quiet: Optional[List[Tuple[float, float]]] = None,
                program_end: Optional[float] = None, how: Optional[Dict[str, str]] = None,
                eye: Optional[List[float]] = None,
                rating: Optional[List[Sequence[int]]] = None) -> Optional[List[Dict[str, float]]]:
    """Commercial breaks as [{'start', 'end'}] in seconds, or None if this recording's show and ads
    can't be told apart reliably. corners and quiet (silences) are used when there are any: the
    channel logo, edges at black frames that are also silent. program_end: where the show is
    scheduled to end in the recording. eye: per second, how much of a faint logo is showing in a
    bigger corner (eye_scores), for channels without a clear one. rating: when the show's own rating
    box shows (rating_boxes), never inside a break."""
    found = _find_breaks(hists, blacks, corners, quiet, program_end, how, eye)
    return _not_over_rating(found, rating or [], sorted(blacks), _edges(blacks, quiet))


def _edges(blacks: List[float], quiet: Optional[List[Tuple[float, float]]]) -> List[float]:
    """Black frames that are also silent (all black frames when there's no sound measured)."""
    return sorted(b for b in blacks if any(a - 1 <= b <= z + 1 for a, z in quiet)) if quiet else sorted(blacks)


def _find_breaks(hists: List[List[float]], blacks: List[float], corners: Optional[List[Sequence[bytes]]],
                 quiet: Optional[List[Tuple[float, float]]], program_end: Optional[float],
                 how: Optional[Dict[str, str]], eye: Optional[List[float]]) -> Optional[List[Dict[str, float]]]:
    edges = _edges(blacks, quiet)
    if corners:
        found = best_logo([(c,) if isinstance(c, bytes) else c for c in corners])
        if found:
            if how is not None:
                how["by"] = "logo"
            return _logo_breaks(found[0], found[1], edges, sorted(blacks))
    if how is not None:
        how["by"] = "colour"  # which way it went, saved with the result: a later fix can redo just these
    breaks = _colour_breaks(hists, blacks, edges, quiet, program_end)
    if not eye:
        return breaks
    # A faint logo (the CBS eye) as well: a break found either way. On three CBS recordings the eye
    # alone never skipped any show and found what colour missed (Survivor 59% -> 96% of the ads),
    # while colour found the breaks full of CBS's own promos, which carry the eye (2026-10-30)
    if how is not None:
        how["by"] = "colour+eye"
    by_eye = _logo_breaks(eye, EYE_ON, edges, sorted(blacks), nearest_start=True)
    return _join((breaks or []) + by_eye) if breaks is not None or by_eye else breaks


def _colour_breaks(hists: List[List[float]], blacks: List[float], edges: List[float],
                   quiet: Optional[List[Tuple[float, float]]], program_end: Optional[float]) -> Optional[List[Dict[str, float]]]:
    """No clear logo: the seconds that don't look like the show, and the pieces between black-and-
    silent moments that look like ads on average. None if show and ads don't separate."""
    n = len(hists)
    if n < 120:
        return None
    # What the show looks like: start from the longest stretch between black frames (almost always
    # show), then refine from every second that matched it
    cuts = [0.0] + sorted(blacks) + [float(n)]
    a, b = max(zip(cuts, cuts[1:]), key=lambda s: s[1] - s[0])
    ref = _mean(hists[int(a):max(int(b), int(a) + 1)])
    for _ in range(2):
        scores = [_similarity(h, ref) for h in hists]
        t, lo, hi = _threshold(scores)
        ref = _mean([h for h, s in zip(hists, scores) if s >= t])
    if hi - lo < MIN_SEPARATION:
        return None

    show = [s >= t for s in scores]
    half = SMOOTH_SECONDS // 2
    smooth = [sum(show[max(0, i - half):i + half + 1]) * 2 > len(show[max(0, i - half):i + half + 1])
              for i in range(n)]
    ads = _runs([not s for s in smooth])
    merged: List[List[int]] = []
    for s, e in ads:
        if merged and s - merged[-1][1] <= MERGE_GAP_SECONDS:
            merged[-1][1] = e
        else:
            merged.append([s, e])

    def snap(t: float) -> Optional[float]:
        near = [b for b in blacks if abs(b - t) <= SNAP_SECONDS]
        return min(near, key=lambda b: abs(b - t)) if near else None

    breaks = []
    for s, e in merged:
        if e - s < MIN_BREAK_SECONDS:
            continue
        start, end = snap(float(s)), snap(float(e))
        # A real break has a black frame at one edge at least (or runs to the start or end of the recording)
        if start is None and end is None and 0 < s and e < n:
            continue
        start = float(s) + UNANCHORED_TRIM if start is None else start
        end = float(e) - UNANCHORED_TRIM if end is None else end
        breaks.append({"start": round(start if s > 0 else 0.0, 1), "end": round(end if e < n else float(n), 1)})
    if quiet:
        breaks = _join(breaks + _marked_pieces(scores, t, edges, n, program_end))
        breaks = _without_long_pieces(breaks, edges, n)
        breaks = _opening_to_edge(breaks, edges)
    return breaks


def _opening_to_edge(breaks: List[Dict[str, float]], edges: List[float]) -> List[Dict[str, float]]:
    """A break at the very start of the recording (the end of whatever aired before) that doesn't end
    at a black-and-silent moment ends at its last one instead: Survivor's ran on from 0:20 to a black
    frame at 0:54, skipping the show's first 31 s (2026-10-30). The other seven test recordings'
    openings end at such a moment already, or have none."""
    out = []
    for br in breaks:
        if br["start"] < 1 and not any(abs(e - br["end"]) <= 1 for e in edges):
            before = [e for e in edges if 10 <= e < br["end"]]
            if before:
                br = {"start": 0.0, "end": round(max(before), 1)}
        out.append(br)
    return out


def _without_long_pieces(breaks: List[Dict[str, float]], edges: List[float], n: int) -> List[Dict[str, float]]:
    """A break never holds a whole stretch of more than MAX_AD_PIECE_SECONDS between two black-and-
    silent moments: that stretch is show, and is cut out of it. Survivor's firelit Tribal Council
    (597 s between two of them) scored less like the show than the ads did, and colour alone took
    75:21-87:34 for one break, ten minutes of show (2026-10-30). Only a stretch wholly inside the
    break: Jeopardy's breaks start without such a moment, so the stretch before each one runs into it
    and is left alone."""
    cuts: List[float] = []
    for b in [0.0] + edges + [float(n)]:
        if not cuts or b - cuts[-1] > 5:
            cuts.append(b)
    long_pieces = [(a, b) for a, b in zip(cuts, cuts[1:]) if b - a > MAX_AD_PIECE_SECONDS]
    out = []
    for br in breaks:
        parts = [(br["start"], br["end"])]
        for a, b in long_pieces:
            kept = []
            for s, e in parts:
                if s - 1 <= a and b <= e + 1:
                    kept += [p for p in ((s, a), (b, e)) if p[1] - p[0] >= MIN_BREAK_SECONDS]
                else:
                    kept.append((s, e))
            parts = kept
        out += [{"start": round(s, 1), "end": round(e, 1)} for s, e in parts]
    return out


def _marked_pieces(scores: List[float], t: float, edges: List[float], n: int,
                   program_end: Optional[float] = None) -> List[Dict[str, float]]:
    """Without a logo: the black-and-silent moments mark nearly every break edge and the changes
    between ads (on a Price Is Right recording, 14 of 16 edges and almost nothing in the show), so
    the pieces between them that look like ads on average are ads. One second at a time, the show's
    colourful set and the ads overlap; a piece's average doesn't (ads 0.12-0.42, show 0.47-0.62)."""
    cuts: List[float] = []
    for b in [0.0] + edges + [float(n)]:
        if not cuts or b - cuts[-1] > 5:
            cuts.append(b)
    spans = list(zip(cuts, cuts[1:]))
    means = [sum(scores[int(a):max(int(b), int(a) + 1)]) / max(1, len(scores[int(a):max(int(b), int(a) + 1)])) for a, b in spans]
    # What the show's pieces look like: no break runs past MAX_AD_PIECE_SECONDS, so the long pieces
    # are show. A piece clearly below them is ads (Boston Blue: show 0.67-0.74, two whole breaks
    # 0.56-0.57, above the recording-wide cut-off). Without long pieces, that cut-off
    long_means = sorted(m for (a, b), m in zip(spans, means) if b - a > MAX_AD_PIECE_SECONDS)
    cut = long_means[len(long_means) // 2] * AD_PIECE_SHARE if long_means else t
    ad = [b - a <= MAX_AD_PIECE_SECONDS and m < cut for (a, b), m in zip(spans, means)]
    # The show's last segment often runs straight into the closing break with no black-and-silent
    # moment between them (Jeopardy: Final Jeopardy and the station's ads were one piece, and the
    # piece looked like ads), so a piece across the scheduled end is never taken whole
    if program_end:
        ad = [x and not (a + 5 < program_end < b - 5) for (a, b), x in zip(spans, ad)]
    # A short piece against an ad piece is the rest of that break (an ad brighter than most): the
    # show never runs that briefly between two of these moments
    for i, (a, b) in enumerate(spans):
        if not ad[i] and b - a <= SHORT_PIECE_SECONDS and ((i and ad[i - 1]) or (i + 1 < len(ad) and ad[i + 1])):
            ad[i] = True
    return [{"start": round(a, 1), "end": round(b, 1)} for (a, b), x in zip(spans, ad) if x]


def _join(breaks: List[Dict[str, float]]) -> List[Dict[str, float]]:
    """Overlapping or touching breaks as one, in order."""
    out: List[Dict[str, float]] = []
    for b in sorted(breaks, key=lambda x: x["start"]):
        if out and b["start"] <= out[-1]["end"] + 1:
            out[-1]["end"] = max(out[-1]["end"], b["end"])
        else:
            out.append(dict(b))
    return [b for b in out if b["end"] - b["start"] >= 10]



# Each second FFmpeg sends one OUT_W x OUT_H rgb24 frame: the colour thumbnail on top (padded to the
# corners' width) and the four grey corners below it, in CORNERS order
CORNERS = ("top left", "top right", "bottom left", "bottom right")
OUT_W, OUT_H = CORNER_W, THUMB_H + len(CORNERS) * CORNER_H


def analysis_graph() -> str:
    """FFmpeg's filter graph for the one pass: [bd] black frames, [o] the per-second frame, [sd]
    silences and [lv] loudness every LEVEL_SECONDS (the last three printed to the log)."""
    x, y = FRAME_W - CORNER_W, FRAME_H - CORNER_H
    crops = [(0, 0), (x, 0), (0, y), (x, y)]
    labels = "".join(f"[k{i}]" for i in range(len(crops)))
    return (f"[0:v]scale=320:-2,split[a][b];[a]blackdetect=d=0.05:pix_th=0.10[bd];"
            f"[b]fps=1,scale={FRAME_W}:{FRAME_H},format=rgb24,split={len(crops) + 1}[p]{labels};"
            f"[p]scale={THUMB_W}:{THUMB_H},pad={OUT_W}:{THUMB_H}[t];"
            + "".join(f"[k{i}]crop={CORNER_W}:{CORNER_H}:{cx}:{cy},format=gray,format=rgb24[c{i}];" for i, (cx, cy) in enumerate(crops))
            + "[t]" + "".join(f"[c{i}]" for i in range(len(crops))) + f"vstack=inputs={len(crops) + 1},format=rgb24[o];"
            f"[0:a]asplit[a1][a2];[a1]silencedetect=n=-50dB:d=0.15[sd];"
            f"[a2]aformat=sample_rates=8000:channel_layouts=mono,asetnsamples=n={int(8000 * LEVEL_SECONDS)}:p=0,"
            f"astats=metadata=1:reset=1:measure_overall=RMS_level:measure_perchannel=none,"
            f"ametadata=mode=print:key=lavfi.astats.Overall.RMS_level[lv];"
            # [big]: which pixels of each bigger corner are lighter than the 5x5 around them, 1 bit each
            f"[0:v]fps=1,scale={EYE_SCALE_W}:{EYE_SCALE_H},format=gray,split=2[e1][e2];[e2]boxblur=2:1[e3];"
            f"[e1][e3]blend=all_expr='if(gt(A\\,B+{EYE_LIGHTER})\\,255\\,0)',split=5" + "".join(f"[g{i}]" for i in range(5)) + ";"
            + "".join(f"[g{i}]crop={EYE_W}:{EYE_H}:{cx}:{cy}[h{i}];" for i, (cx, cy) in
                      enumerate([(0, 0), (EYE_SCALE_W - EYE_W, 0), (0, EYE_SCALE_H - EYE_H), (EYE_SCALE_W - EYE_W, EYE_SCALE_H - EYE_H)]))
            + "".join(f"[h{i}]" for i in range(4)) + "vstack=inputs=4,format=monob[big];"
            # [rt]: the same, for the top-left third, where the rating box is
            f"[g4]crop={RATING_W}:{RATING_H}:0:0,format=monob[rt]")


def _split_frame(frame: bytes) -> Tuple[bytes, Tuple[bytes, ...]]:
    """(the THUMB_W x THUMB_H rgb24 thumbnail, the grey corners) from one FFmpeg frame."""
    row = OUT_W * 3
    thumb = b"".join(frame[y * row:y * row + THUMB_W * 3] for y in range(THUMB_H))
    corners = tuple(bytes(frame[(THUMB_H + k * CORNER_H + y) * row + x * 3] for y in range(CORNER_H) for x in range(CORNER_W))
                    for k in range(len(CORNERS)))
    return thumb, corners


def eye_path(log_path) -> Path:
    return Path(str(log_path) + ".eye")


def eye_scores(path) -> Optional[List[float]]:
    """From the pass's [big] file: per second, the share of the faint logo's pixels lit, in the
    corner where it's clearest; None if no corner has one. Deletes the file."""
    row = (EYE_W + 7) // 8
    per_corner = row * EYE_H
    per = per_corner * 4
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    finally:
        Path(path).unlink(missing_ok=True)
    n = len(data) // per
    if n < 120:
        return None

    def bits(sec: int, k: int) -> bytes:
        return data[sec * per + k * per_corner:sec * per + (k + 1) * per_corner]

    best = None
    for k in range(4):
        # How often each pixel is lit, from every EYE_SAMPLE-th second
        counts = [0] * (per_corner * 8)
        samples = range(0, n, EYE_SAMPLE)
        for sec in samples:
            for j, byte in enumerate(bits(sec, k)):
                if byte:
                    for b in range(8):
                        if byte & (0x80 >> b):
                            counts[j * 8 + b] += 1
        cut = EYE_PIXEL_SHARE * len(samples)
        # Not the outer EYE_EDGE pixels: FFmpeg's blur makes the picture's own edge (often a thin line)
        # look lighter than its surroundings in every second, ads included; no logo sits there
        logo = [(i // 8, 0x80 >> (i % 8)) for i, cnt in enumerate(counts) if cnt > cut
                and EYE_EDGE <= i % (row * 8) < EYE_W - EYE_EDGE and EYE_EDGE <= i // (row * 8) < EYE_H - EYE_EDGE]
        if len(logo) >= EYE_MIN_PIXELS and (best is None or len(logo) > len(best[1])):
            best = (k, logo)
    if best is None:
        return None
    k, logo = best
    return [sum(1 for j, mask in logo if bits(sec, k)[j] & mask) / len(logo) for sec in range(n)]


def rating_path(log_path) -> Path:
    return Path(str(log_path) + ".rating")


_REVERSED = bytes(int(f"{b:08b}"[::-1], 2) for b in range(256))


def rating_rows(frame: bytes) -> List[int]:
    """One second of the pass's rating file (monob, RATING_W x RATING_H) as rows, bit x = pixel x;
    the picture's own edge (EYE_EDGE) left out, as for the eye."""
    row = (RATING_W + 7) // 8
    keep = ((1 << (RATING_W - EYE_EDGE)) - 1) ^ ((1 << EYE_EDGE) - 1)
    return [int.from_bytes(frame[y * row:(y + 1) * row].translate(_REVERSED), "little") & keep
            if EYE_EDGE <= y < RATING_H - EYE_EDGE else 0 for y in range(RATING_H)]


def _rating_pattern(seconds: List[List[int]]) -> Optional[Tuple[List[int], int, int]]:
    """(rows, first row, last row) of what stays put longest in a compact patch in the opening."""
    best = None
    for s in range(0, min(RATING_OPENING, len(seconds) - RATING_STEADY + 1)):
        rows = list(seconds[s])
        for k in range(1, RATING_STEADY):
            rows = [a & b for a, b in zip(rows, seconds[s + k])]
        n = sum(_popcount(r) for r in rows)
        if n < RATING_MIN_PIXELS or (best and n <= best[0]):
            continue
        ys = [y for y, r in enumerate(rows) if r]
        x0 = min((r & -r).bit_length() - 1 for r in rows if r)
        x1 = max(r.bit_length() - 1 for r in rows if r)
        if x1 - x0 <= RATING_MAX_W and ys[-1] - ys[0] <= RATING_MAX_H:
            best = (n, rows, ys[0], ys[-1])
    return best[1:] if best else None


def _rating_match(rows: List[int], pattern: Tuple[List[int], int, int]) -> float:
    """The share of the pattern's pixels lit in this second (shifted a pixel either way at most)."""
    want, top, bottom = pattern
    total = sum(_popcount(r) for r in want)
    best = 0
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            hit = 0
            for y in range(top, bottom + 1):
                if 0 <= y + dy < len(rows):
                    r = rows[y + dy]
                    hit += _popcount(want[y] & (r >> dx if dx >= 0 else r << -dx))
            best = max(best, hit)
    return best / total


def show_rating(seconds: List[List[int]]) -> List[Tuple[int, int]]:
    """[start, end) of each time the show's own rating shows, from each second's rows (rating_rows):
    learned in the opening, then matched; nothing unless it's seen again after the opening."""
    pattern = _rating_pattern(seconds)
    if pattern is None:
        return []  # the recording missed the show's start, or no rating: nothing to go by
    shows: List[List[int]] = []
    for sec, rows in enumerate(seconds):
        if _rating_match(rows, pattern) >= RATING_MATCH:
            if shows and sec - shows[-1][1] <= RATING_GAP:
                shows[-1][1] = sec + 1
                shows[-1][2] += 1
            else:
                shows.append([sec, sec + 1, 1])
    shows = [s for s in shows if s[2] >= RATING_MIN_SECONDS]
    if not any(s[0] >= RATING_OPENING for s in shows):
        return []
    return [(s[0], s[1]) for s in shows]


def rating_boxes(path) -> List[Tuple[int, int]]:
    """From the pass's rating file: when the show's own rating shows (show_rating). Deletes the file."""
    per = (RATING_W + 7) // 8 * RATING_H
    try:
        data = Path(path).read_bytes()
    except OSError:
        return []
    finally:
        Path(path).unlink(missing_ok=True)
    return show_rating([rating_rows(data[s * per:(s + 1) * per]) for s in range(len(data) // per)])



def _not_over_rating(breaks: Optional[List[Dict[str, float]]], rating: List[Sequence[int]],
                     blacks: List[float], edges: Sequence[float] = ()) -> Optional[List[Dict[str, float]]]:
    """Whatever shows the show's own rating is the show: a break that runs into it ends at the last
    black frame before it (the show came back there), one that starts while it shows starts after it,
    and what's left shorter than a break isn't one. A break ending up to RATING_AFTER seconds before
    the rating shows ends at the last black-and-silent moment (edges) before the rating instead."""
    if not breaks or not rating:
        return breaks
    out = []
    for b in breaks:
        start, end = b["start"], b["end"]
        for s, e in sorted(rating):
            if s <= start < e:
                start = float(e)
            elif start < s < end:
                back = [x for x in blacks if max(start, s - RATING_LOOKBACK) < x <= s]
                end = max(back) if back else s - 2.0
            elif end <= s <= end + RATING_AFTER:
                later = [x for x in edges if end < x <= s]
                end = max(later) if later else end
        if end - start >= MIN_BREAK_SECONDS:
            out.append({"start": round(start, 1), "end": round(end, 1)})
    return out

# ---- running the detector in Homeflix ---------------------------------------------------------
# Commercial skipping is off unless turned on. auto: files that are usually TV recordings (with
# ads); all: every video
RECORDING_EXT = {".ts", ".m2ts", ".mts", ".mpg", ".mpeg"}
ADS_MODES = ("off", "auto", "all")


def ads_pass(path: str, work: Path, stop: threading.Event, progress: Dict[str, float]):
    """One ffmpeg pass over a local file (adapted from TabloView's _analyze): the per-second
    thumbnails and corners from stdout, black frames, silences, the faint logo and the rating box
    from the log and two side files. None if stopped."""
    work.parent.mkdir(parents=True, exist_ok=True)
    log_path = work.with_suffix(".log")
    eye_p, rating_p = eye_path(log_path), rating_path(log_path)
    cmd = [FFMPEG, "-hide_banner", "-nostats", "-nostdin", "-i", path, "-filter_complex", analysis_graph(),
           "-map", "[bd]", "-f", "null", "-",
           "-map", "[sd]", "-f", "null", "-",
           "-map", "[lv]", "-f", "null", "-",
           "-map", "[big]", "-f", "rawvideo", "-y", str(eye_p),
           "-map", "[rt]", "-f", "rawvideo", "-y", str(rating_p),
           "-map", "[o]", "-f", "rawvideo", "-"]
    pre = [NICE, "-n", "15"] if NICE else []
    size = OUT_W * OUT_H * 3
    hists: List[List[float]] = []
    corners: List[Tuple[bytes, ...]] = []
    stopped = False
    try:
        with open(log_path, "w", encoding="utf-8") as lf:
            proc = subprocess.Popen(pre + cmd, stdout=subprocess.PIPE, stderr=lf, stdin=subprocess.DEVNULL,
                                    **(LOW_PRIO or SUBPROC))

            def watch():
                while proc.poll() is None:
                    if stop.wait(1):
                        proc.kill()
                        return
            threading.Thread(target=watch, daemon=True).start()
            try:
                while True:
                    frame = proc.stdout.read(size)
                    if len(frame) < size:
                        break
                    thumb, corner = _split_frame(frame)
                    hists.append(histogram(thumb))
                    corners.append(corner)
                    progress["done"] = len(hists)
            finally:
                stopped = stop.is_set()
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
        if stopped:
            return None
        if proc.returncode != 0 and len(hists) < 60:
            raise RuntimeError(f"ffmpeg exited with code {proc.returncode}")
        text = log_path.read_text(encoding="utf-8", errors="replace")
        eye = eye_scores(eye_p)  # these two read and delete their files
        rating = rating_boxes(rating_p)
        return {"hists": hists, "corners": corners, "blacks": black_frames(text), "quiet": silences(text),
                "eye": eye, "rating": rating}
    finally:
        for p in (log_path, eye_p, rating_p):
            try:
                p.unlink()
            except OSError:
                pass


class AdFinder:
    """Finds the ad breaks in recordings in the background, one at a time, at low priority, and
    waits while anything is being converted for playback. Results are cached per file (redone when
    the file or the detector changes); breaks marked "not an ad" are kept in the data folder."""

    def __init__(self, lib: "Library", mode: str = "off"):
        self.lib = lib
        self.mode = mode if mode in ADS_MODES else "off"
        self.dir = XDG_CACHE / "ads"
        self.not_ads_path = XDG_DATA / "not_ads.json"
        self.not_ads = load_json(self.not_ads_path, {})
        self.lock = threading.Lock()
        self.asked: List[str] = []
        self.current: Optional[Dict[str, object]] = None
        self.wake = threading.Event()
        self.busy = lambda: False  # set by main(): True while a video is being converted

    # -- results ---------------------------------------------------------------
    def _path(self, iid: str) -> Path:
        return self.dir / f"{iid}.json"

    def result(self, it) -> Optional[dict]:
        r = load_json(self._path(it["id"]), None)
        if r and r.get("key") == it["_key"] and r.get("method") == METHOD:
            return r
        return None

    def _save(self, it, status: str, breaks=None, by=None, error=None):
        atomic_write(self._path(it["id"]), json.dumps(
            {"key": it["_key"], "method": METHOD, "status": status, "breaks": breaks or [], "by": by,
             "error": error, "at": int(time.time())}))

    def breaks(self, it) -> List[Dict[str, float]]:
        r = self.result(it)
        if not r or r.get("status") != "done":
            return []
        with self.lock:
            skip = self.not_ads.get(it["id"], [])
        return [b for b in r["breaks"] if not any(s < b["end"] and b["start"] < e for s, e in skip)]

    def not_a_break(self, it, start: float, end: float):
        with self.lock:
            self.not_ads.setdefault(it["id"], []).append([round(float(start), 1), round(float(end), 1)])
            atomic_write(self.not_ads_path, json.dumps(self.not_ads))

    # -- what to look at ---------------------------------------------------------
    def _can(self, it) -> bool:
        return (it["kind"] == "video" and it.get("_probed") and bool(it.get("_a"))
                and (it.get("duration") or 0) >= 300)

    def _wanted(self, it) -> bool:
        if self.mode == "off" or not self._can(it):
            return False
        return self.mode == "all" or it["_ext"] in RECORDING_EXT

    def pending(self) -> list:
        with self.lib.lock:
            items = list(self.lib.items.values())
        with self.lock:
            asked = list(self.asked)
        by_id = {it["id"]: it for it in items}
        first = [by_id[i] for i in asked if i in by_id and self._can(by_id[i])]
        rest = sorted((it for it in items if it["id"] not in asked and self._wanted(it)), key=lambda x: -x["added"])
        return [it for it in first + rest if it["id"] in asked or not self.result(it)]

    def ask(self, it) -> bool:
        if self.mode == "off" or not FFMPEG or not self._can(it):
            return False
        with self.lock:
            if it["id"] not in self.asked:
                self.asked.append(it["id"])
        try:
            self._path(it["id"]).unlink()
        except OSError:
            pass
        self.wake.set()
        return True

    def info(self, it) -> dict:
        if self.mode == "off":
            return {"status": "off", "breaks": []}  # the player shows nothing about ads
        cur = self.current
        if cur and cur["id"] == it["id"]:
            total = it.get("duration") or 0
            pct = min(99, round(float(cur["done"]) / total * 100)) if total else None
            return {"status": "running", "percent": pct, "breaks": []}
        r = self.result(it)
        if r:
            return {"status": r["status"], "by": r.get("by"), "breaks": self.breaks(it)}
        with self.lock:
            queued = it["id"] in self.asked
        if queued or (FFMPEG and self._wanted(it)):
            return {"status": "queued", "breaks": []}
        return {"status": "none" if FFMPEG and self._can(it) else "unavailable", "breaks": []}

    # -- the worker ------------------------------------------------------------
    def run(self):
        if not FFMPEG or self.mode == "off":
            return
        while True:
            self.wake.wait(30)
            self.wake.clear()
            try:
                if self.busy():
                    continue
                todo = self.pending()
                if todo:
                    self.analyze(todo[0])
                    if self.pending():
                        self.wake.set()  # straight on to the next
            except Exception as e:
                log(f"[Ads] {e}")

    def analyze(self, it):
        stop = threading.Event()
        self.current = {"id": it["id"], "done": 0}
        name = it.get("show") and f"{it['show']} S{it.get('season')}E{it.get('episode')}" or it.get("title") or it["file"]

        def pause_while_busy():
            while not stop.is_set():
                if self.busy():
                    stop.set()
                time.sleep(2)
        threading.Thread(target=pause_while_busy, daemon=True).start()
        t0 = time.time()
        try:
            feats = ads_pass(it["_path"], self.dir / f"work_{it['id']}", stop, self.current)
            if feats is None:
                return  # something started playing; tried again later
            how: Dict[str, str] = {}
            found = find_breaks(feats["hists"], feats["blacks"], feats["corners"], feats["quiet"], None, how,
                                feats["eye"], feats["rating"])
            self._save(it, "unreliable" if found is None else "done", found, how.get("by"))
            log(f"[Ads] {name}: " + ("show and ads could not be told apart, nothing marked" if found is None
                                     else f"{len(found)} break(s), found by {how.get('by')}")
                + f" ({time.time() - t0:.0f}s)")
        except Exception as e:
            self._save(it, "failed", error=str(e)[:300])
            log(f"[Ads] couldn't look for ads in {name}: {e}")
        finally:
            stop.set()
            self.current = None
            with self.lock:
                if it["id"] in self.asked and self.result(it):
                    self.asked.remove(it["id"])


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second server silently share the port, so leave it off there.
    allow_reuse_address = not WINDOWS
    request_queue_size = 64


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"Homeflix/{VERSION}"
    lib: Library = None
    state: State = None
    password = None
    secret = b""
    quiet = False
    quality = "high"
    ads: "AdFinder" = None
    converting = 0  # videos being converted right now: ad finding waits for them
    conv_lock = threading.Lock()

    def log_message(self, fmt, *args):
        if not self.quiet:
            sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")

    # -- helpers ---------------------------------------------------------------
    def _token(self):
        return hmac.new(self.secret, (self.password or "").encode(), hashlib.sha256).hexdigest()

    def authed(self):
        if not self.password:
            return True
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "hf_auth" and hmac.compare_digest(v, self._token()):
                return True
        return False

    def send_bytes(self, body: bytes, ctype="application/json", status=200, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, obj, status=200):
        self.send_bytes(json.dumps(obj, separators=(",", ":")).encode(), status=status,
                        extra={"Cache-Control": "no-store"})

    def err(self, status, msg="error"):
        self.send_json({"error": msg}, status)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 100_000:
            raise ValueError("body too large")
        return json.loads(self.rfile.read(n) or b"{}")

    # -- routing ---------------------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self.route_get()
        except ConnectionError:  # includes ConnectionAbortedError, which Windows raises
            pass

    def do_POST(self):
        try:
            self.route_post()
        except ConnectionError:  # includes ConnectionAbortedError, which Windows raises
            pass
        except (ValueError, KeyError, TypeError) as e:
            self.err(400, str(e))

    def route_get(self):
        u = urllib.parse.urlsplit(self.path)
        path, qs = u.path, urllib.parse.parse_qs(u.query)
        if path == "/favicon.svg":
            return self.send_bytes(FAVICON.encode(), "image/svg+xml", extra={"Cache-Control": "max-age=86400"})
        if not self.authed():
            if path.startswith(("/api/", "/media/", "/transcode/", "/thumb/", "/subs/")):
                return self.err(401, "login required")
            return self.send_bytes(LOGIN_HTML.encode(), "text/html; charset=utf-8", extra={"Cache-Control": "no-store"})
        if path in ("/", "/index.html"):
            return self.send_bytes(APP_HTML.encode(), "text/html; charset=utf-8",
                                   extra={"Cache-Control": "no-cache",
                                          "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'"})
        if path == "/api/library":
            return self.send_json(self.lib.public())
        if path == "/api/profiles":
            return self.send_json({"profiles": self.state.profiles(), "auth": bool(self.password)})
        if path == "/api/state":
            return self.send_json(self.state.get((qs.get("profile") or [""])[0]))
        m = re.fullmatch(r"/api/ads/([0-9a-f]{14})", path)
        if m:
            it = self.lib.get(m.group(1))
            if not it:
                return self.err(404, "unknown item")
            return self.send_json(self.ads.info(it))
        m = re.fullmatch(r"/(media|transcode|thumb|subs)/([0-9a-f]{14})(?:/(\d+))?", path)
        if not m:
            return self.err(404, "not found")
        kind, iid, sub = m.groups()
        it = self.lib.get(iid)
        if not it:
            return self.err(404, "unknown item")
        if kind == "media":
            return self.send_file(it["_path"], guess_type(it))
        if kind == "thumb":
            p = self.lib.thumb(it)
            if not p:
                return self.send_bytes(b"", "image/jpeg", status=404, extra={"Cache-Control": "max-age=300"})
            return self.send_file(p, mimetypes.guess_type(p)[0] or "image/jpeg", cache="max-age=604800")
        if kind == "subs":
            off = float((qs.get("offset") or ["0"])[0] or 0)
            vtt = self.lib.subtitle_vtt(it, int(sub or 0), off)
            if vtt is None:
                return self.err(404, "no subtitle")
            return self.send_bytes(vtt.encode("utf-8"), "text/vtt; charset=utf-8", extra={"Cache-Control": "no-store"})
        if kind == "transcode":
            t = float((qs.get("t") or ["0"])[0] or 0)
            return self.transcode(it, max(0.0, t), (qs.get("hevc") or [""])[0])

    def route_post(self):
        u = urllib.parse.urlsplit(self.path)
        path = u.path
        if path == "/login":
            n = int(self.headers.get("Content-Length") or 0)
            form = urllib.parse.parse_qs(self.rfile.read(min(n, 4096)).decode(errors="replace"))
            pw = (form.get("password") or [""])[0]
            if self.password and hmac.compare_digest(pw.encode(), self.password.encode()):
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Set-Cookie", f"hf_auth={self._token()}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            time.sleep(1.5)
            self.send_response(303)
            self.send_header("Location", "/?bad=1")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not self.authed():
            return self.err(401, "login required")
        b = self.read_json()
        if path == "/api/profiles":
            if b.get("delete"):
                self.state.delete_profile(b["delete"])
                return self.send_json({"ok": True})
            return self.send_json(self.state.add_profile(b.get("name", "")))
        if path == "/api/progress":
            if b.get("clear"):
                self.state.clear_progress(b["profile"], b["id"])
            else:
                self.state.set_progress(b["profile"], b["id"], b.get("pos", 0), b.get("dur", 0), b.get("done"))
            return self.send_json({"ok": True})
        if path == "/api/mylist":
            self.state.set_mylist(b["profile"], b["key"], bool(b.get("on")))
            return self.send_json({"ok": True})
        if path == "/api/rescan":
            threading.Thread(target=self.lib.scan, daemon=True).start()
            return self.send_json({"ok": True})
        m = re.fullmatch(r"/api/ads/([0-9a-f]{14})/(find|not_ad)", path)
        if m:
            it = self.lib.get(m.group(1))
            if not it:
                return self.err(404, "unknown item")
            if m.group(2) == "find":
                return self.send_json({"ok": self.ads.ask(it)})
            self.ads.not_a_break(it, float(b["start"]), float(b["end"]))
            return self.send_json({"ok": True, "breaks": self.ads.breaks(it)})
        return self.err(404, "not found")

    # -- streaming -------------------------------------------------------------
    def send_file(self, fpath, ctype, cache="no-cache"):
        try:
            f = open(fpath, "rb")
        except OSError:
            return self.err(404, "file missing")
        with f:
            size = os.fstat(f.fileno()).st_size
            start, end, status = 0, size - 1, 200
            rng = self.headers.get("Range")
            if rng:
                m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
                if m and (m.group(1) or m.group(2)):
                    if m.group(1) == "":
                        start = max(0, size - int(m.group(2)))
                    else:
                        start = int(m.group(1))
                        if m.group(2):
                            end = min(int(m.group(2)), size - 1)
                    if start >= size or start > end:
                        return self.send_bytes(b"", ctype, 416, {"Content-Range": f"bytes */{size}"})
                    status = 206
            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1 << 18, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def transcode(self, it, t, hevc=""):
        """Stream it as fragmented MP4. hevc="1" (Main) or "10" (Main10) means the browser
        can decode HEVC itself, so HEVC video is passed through untouched."""
        if not FFMPEG:
            return self.err(501, "ffmpeg is not installed on the server")
        cmd = [FFMPEG, "-nostdin", "-v", "error", "-hide_banner"]
        if t > 0:
            cmd += ["-ss", f"{t:.2f}"]
        cmd += ["-i", it["_path"]]
        if it["kind"] == "video":
            v, a = it.get("_v") or {}, it.get("_a") or {}
            pix = v.get("pix")
            cmd += ["-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn"]
            hevc_ok = v.get("codec") == "hevc" and (pix in ("yuv420p", None) or (hevc == "10" and pix == "yuv420p10le"))
            if hevc in ("1", "10") and hevc_ok:
                cmd += ["-c:v", "copy", "-tag:v", "hvc1"]
            elif not VENC or (v.get("codec") == "h264" and pix in ("yuv420p", "yuvj420p", None)):
                cmd += ["-c:v", "copy"]
            else:
                preset, crf, mbit = QUALITY[self.quality]
                scale = ["-pix_fmt", "yuv420p", "-vf", "scale='min(1920,iw)':-2"]
                if VENC == "libx264":
                    cmd += ["-c:v", VENC, "-preset", preset, "-crf", str(crf), "-maxrate", f"{mbit}M",
                            "-bufsize", f"{mbit * 2}M", "-g", "48"] + scale
                else:  # libopenh264 has no CRF mode; its default bitrate is very low
                    cmd += ["-c:v", VENC, "-b:v", f"{mbit // 2}M", "-maxrate", f"{mbit}M"] + scale
            if a.get("codec") == "aac":
                # AAC from .ts recordings comes in ADTS packets, which MP4 can't hold as they are
                cmd += ["-c:a", "copy", "-bsf:a", "aac_adtstoasc"]
            else:
                cmd += ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]
            ctype = "video/mp4"
        else:
            cmd += ["-vn", "-sn", "-c:a", "aac", "-b:a", "256k"]
            ctype = "audio/mp4"
        cmd += ["-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"]
        if self.command == "HEAD":
            return self.send_bytes(b"", ctype)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                **SUBPROC)
        self.close_connection = True
        counted = it["kind"] == "video"
        if counted:
            with Handler.conv_lock:
                Handler.converting += 1
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                chunk = proc.stdout.read(1 << 16)
                if not chunk:
                    break
                self.wfile.write(chunk)
        finally:
            proc.kill()
            proc.wait()
            if counted:
                with Handler.conv_lock:
                    Handler.converting -= 1


def guess_type(it):
    ext = it["_ext"]
    table = {".mkv": "video/x-matroska", ".webm": "video/webm", ".mp4": "video/mp4", ".m4v": "video/mp4",
             ".mov": "video/mp4", ".ogv": "video/ogg", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
             ".aac": "audio/aac", ".flac": "audio/flac", ".ogg": "audio/ogg", ".oga": "audio/ogg",
             ".opus": "audio/ogg", ".wav": "audio/wav"}
    return table.get(ext) or mimetypes.guess_type(it["_path"])[0] or "application/octet-stream"


def pick_encoder():
    if not FFMPEG:
        return None
    try:
        out = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=10,
                             **SUBPROC).stdout
    except Exception:
        return None
    for enc in ("libx264", "libopenh264", "h264_vaapi"):
        if re.search(rf"\b{enc}\b", out):
            return enc if enc != "h264_vaapi" else None
    return None


VENC = None


def lan_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    if MACOS:  # resolving the hostname can stall for seconds on macOS; the UDP trick is enough
        return sorted(ips)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            if not info[4][0].startswith("127."):
                ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ips)


# ----------------------------------------------------------------------------
# Front-end
# ----------------------------------------------------------------------------
FAVICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="12" fill="#141414"/><path d="M18 10h10l18 44H36z" fill="#e50914"/><path d="M18 10h10v44H18zM36 10h10v44H36z" fill="#b20710"/><path d="M18 10h10l18 44H36z" fill="#e50914"/></svg>"""

LOGIN_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Homeflix - Sign in</title><link rel="icon" href="/favicon.svg">
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#000 radial-gradient(ellipse at top,#3a0508,#000 70%);font-family:Helvetica,Arial,sans-serif;color:#fff}
form{background:rgba(0,0,0,.75);padding:48px 56px;border-radius:6px;width:300px}h1{color:#e50914;letter-spacing:2px;margin:0 0 28px;font-size:34px}
input{width:100%;box-sizing:border-box;padding:14px;border-radius:4px;border:0;background:#333;color:#fff;font-size:16px;margin-bottom:16px}
button{width:100%;padding:14px;border:0;border-radius:4px;background:#e50914;color:#fff;font-size:16px;font-weight:700;cursor:pointer}
.bad{color:#e87c03;font-size:14px;margin-bottom:12px;display:none}.ver{color:#777;font-size:12px;text-align:center;margin-top:18px}</style></head><body>
<form method="post" action="/login"><h1>HOMEFLIX</h1><div class="bad" id="bad">Incorrect password.</div>
<input type="password" name="password" placeholder="Password" autofocus required><button>Sign In</button><div class="ver">Homeflix v__VERSION__</div></form>
<script>if(location.search.includes('bad'))document.getElementById('bad').style.display='block'</script></body></html>"""

APP_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#141414">
<title>Homeflix</title><link rel="icon" href="/favicon.svg">
<style>
:root{--red:#e50914;--bg:#141414;--card:#232323;--mut:#a3a3a3}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:#fff;font-family:"Netflix Sans","Helvetica Neue",Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased}
body.noscroll{overflow:hidden}
button{font:inherit;color:inherit;cursor:pointer}
a{color:inherit}
.hidden{display:none!important}
/* nav */
#nav{position:fixed;top:0;left:0;right:0;z-index:20;display:flex;align-items:center;gap:22px;padding:14px 4%;background:linear-gradient(#000c,#0000);transition:background .3s}
#nav.solid{background:#141414}
.logo{color:var(--red);font-weight:900;font-size:28px;letter-spacing:1px;cursor:pointer;user-select:none;text-shadow:0 2px 6px #0008}
.logo .ver{color:#aaa;font-size:11px;font-weight:600;letter-spacing:0;margin-left:6px;vertical-align:super;text-shadow:none}
#links{display:flex;gap:18px;font-size:14px}
#links a{text-decoration:none;color:#e5e5e5;cursor:pointer}
#links a.on{color:#fff;font-weight:700}
#links a:hover{color:#b3b3b3}
.navr{margin-left:auto;display:flex;align-items:center;gap:14px}
#search{background:#000a;border:1px solid #fff8;color:#fff;padding:7px 10px 7px 32px;width:220px;border-radius:2px;font-size:14px;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='16' height='16' fill='none' stroke='white' stroke-width='2'%3E%3Ccircle cx='7' cy='7' r='5'/%3E%3Cpath d='M11 11l4 4'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:9px center}
#search:focus{outline:none;border-color:#fff}
.iconbtn{background:none;border:0;font-size:20px;opacity:.85;padding:4px}
.iconbtn:hover{opacity:1}
.avatar{width:32px;height:32px;border-radius:4px;display:grid;place-items:center;font-weight:700;cursor:pointer}
/* hero */
.hero{position:relative;height:78vh;min-height:420px;background:#000;overflow:hidden}
.hero .bg{position:absolute;inset:0;background-size:cover;background-position:center 30%}
.hero:after{content:"";position:absolute;inset:0;background:linear-gradient(77deg,#000d 0,#0006 45%,#0000 70%),linear-gradient(#0000 60%,var(--bg) 100%)}
.hero .info{position:absolute;z-index:2;left:4%;bottom:26%;max-width:560px}
.hero h1{font-size:clamp(32px,5vw,64px);margin:0 0 12px;line-height:1;text-shadow:2px 2px 6px #0009}
.hero p{font-size:16px;color:#ddd;margin:0 0 20px;text-shadow:1px 1px 3px #000}
.btn{display:inline-flex;align-items:center;gap:8px;border:0;border-radius:4px;padding:10px 26px;font-weight:700;font-size:17px}
.btn.play{background:#fff;color:#000}.btn.play:hover{background:#ffffffbf}
.btn.gray{background:#6d6d6eb3;color:#fff}.btn.gray:hover{background:#6d6d6e66}
.btn.round{width:42px;height:42px;padding:0;justify-content:center;border-radius:50%;border:2px solid #fff8;background:#2a2a2a99;font-size:20px}
.btn.round:hover{border-color:#fff}
/* rows */
main{padding-bottom:120px}
.rows{position:relative;z-index:3;margin-top:-12vh}
.page{padding-top:90px}
.row{margin:0 0 3vw}
.row h2{font-size:clamp(16px,1.4vw,22px);margin:0 4% 10px;font-weight:700;color:#e5e5e5}
.rowwrap{position:relative}
.track{display:flex;gap:6px;overflow-x:auto;padding:14px 4%;scroll-behavior:smooth;scrollbar-width:none;scroll-padding:0 4%}
.track::-webkit-scrollbar{display:none}
.arr{position:absolute;top:14px;bottom:14px;width:4%;z-index:4;border:0;background:#000a;font-size:40px;opacity:0;transition:opacity .2s}
.rowwrap:hover .arr{opacity:1}.arr.l{left:0}.arr.r{right:0}
.card{flex:0 0 auto;width:clamp(150px,17vw,300px);cursor:pointer;transition:transform .25s;position:relative;border-radius:4px}
.card.sq{width:clamp(130px,13vw,220px)}
.card:hover{transform:scale(1.07);z-index:5}
.card .lbl{font-size:13px;margin-top:6px;color:#ddd;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card .sub{font-size:12px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.art{position:relative;aspect-ratio:16/9;border-radius:4px;overflow:hidden;display:grid;place-items:center;text-align:center}
.art.sq{aspect-ratio:1}
.art .ph{padding:10px;font-weight:800;font-size:clamp(13px,1.2vw,18px);text-shadow:0 2px 6px #0007;line-height:1.15}
.art img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;opacity:0;transition:opacity .3s}
.art.loaded img{opacity:1}
.art .badge{position:absolute;left:6px;top:6px;z-index:2;background:var(--red);font-size:10px;font-weight:800;padding:2px 5px;border-radius:2px}
.pbar{position:absolute;left:8px;right:8px;bottom:8px;height:3px;background:#4d4d4d;z-index:2;border-radius:2px}
.pbar i{display:block;height:100%;background:var(--red)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(clamp(150px,17vw,260px),1fr));gap:28px 8px;padding:0 4%}
.grid.sq{grid-template-columns:repeat(auto-fill,minmax(clamp(130px,13vw,200px),1fr))}
.grid .card{width:auto}
.ptitle{display:flex;align-items:center;gap:18px;padding:0 4% 20px;flex-wrap:wrap}
.ptitle h1{margin:0;font-size:clamp(24px,3vw,38px)}
select.sel{background:#000;color:#fff;border:1px solid #fff;padding:5px 8px;font-size:14px}
.empty{padding:20vh 4%;text-align:center;color:var(--mut)}
.empty h2{color:#fff}
.empty code{background:#222;padding:2px 6px;border-radius:3px}
.scanning{position:fixed;right:16px;bottom:16px;background:#222;padding:8px 14px;border-radius:4px;font-size:13px;z-index:30;box-shadow:0 4px 12px #0008}
body.mplay .scanning{bottom:96px}
/* modal */
#modal{position:fixed;inset:0;z-index:40;background:#000b;overflow-y:auto;padding:4vh 12px}
.mbox{max-width:860px;margin:0 auto;background:#181818;border-radius:8px;overflow:hidden;box-shadow:0 8px 40px #000;position:relative}
.mhead{position:relative;aspect-ratio:16/8;background:#000}
.mhead .art{position:absolute;inset:0;aspect-ratio:auto;border-radius:0}
.mhead:after{content:"";position:absolute;inset:0;background:linear-gradient(#0000 40%,#181818)}
.mhead .info{position:absolute;left:5%;bottom:8%;z-index:2;right:5%}
.mhead h1{margin:0 0 16px;font-size:clamp(24px,4vw,44px);text-shadow:2px 2px 6px #000a}
.mhead.album{aspect-ratio:auto;display:flex;gap:26px;padding:30px 5% 10px;align-items:flex-end;background:linear-gradient(#3a3a3a,#181818)}
.mhead.album:after{display:none}
.mhead.album .art{position:relative;width:200px;flex:0 0 200px;aspect-ratio:1;border-radius:4px;box-shadow:0 8px 24px #0009}
.mhead.album .info{position:static}
.close{position:absolute;top:14px;right:14px;z-index:5;width:36px;height:36px;border-radius:50%;border:0;background:#181818;font-size:20px}
.mbody{padding:10px 5% 36px}
.meta{display:flex;gap:10px;align-items:center;flex-wrap:wrap;color:#bcbcbc;font-size:15px;margin-bottom:14px}
.meta .hd{border:1px solid #888;padding:0 5px;font-size:11px;border-radius:3px}
.meta .new{color:#46d369;font-weight:700}
.small{font-size:13px;color:#777;word-break:break-all}
.btns{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.eps h3{display:flex;justify-content:space-between;align-items:center;font-size:22px}
.ep{display:flex;gap:16px;align-items:center;padding:16px 10px;border-bottom:1px solid #404040;cursor:pointer;border-radius:4px}
.ep:hover{background:#333}
.ep .n{font-size:22px;color:#d2d2d2;width:28px;text-align:center;flex:0 0 28px}
.ep .art{width:150px;flex:0 0 150px}
.ep .t{flex:1;min-width:0}.ep .t b{display:block;margin-bottom:4px}
.ep .d{color:var(--mut);font-size:14px}
.trk{display:grid;grid-template-columns:34px 1fr auto;gap:12px;padding:9px 10px;border-radius:4px;cursor:pointer;align-items:center}
.trk:hover{background:#2a2a2a}
.trk .n{color:var(--mut);text-align:right}
.trk.on{color:#46d369}
.trk .a{color:var(--mut);font-size:13px}
/* video player */
#player{position:fixed;inset:0;z-index:60;background:#000}
#vid{width:100%;height:100%;background:#000}
#vid::cue{background:#000a;font-size:clamp(16px,2.6vw,34px)}
.pov{position:absolute;left:0;right:0;transition:opacity .3s;z-index:2}
#player.idle .pov{opacity:0}#player.idle{cursor:none}
.ptop{top:0;padding:18px 24px;display:flex;align-items:center;gap:18px;background:linear-gradient(#000b,#0000)}
.ptop button{background:none;border:0;font-size:30px}
#ptitle{font-size:18px}#ptitle small{color:#bbb;margin-left:8px}
.pbot{bottom:0;padding:10px 24px 18px;background:linear-gradient(#0000,#000c)}
.seekrow{display:flex;align-items:center;gap:14px;font-size:14px}
input[type=range]{accent-color:var(--red);cursor:pointer}
#pseek{flex:1;width:100%}#pvol{width:90px}
.pvolw{display:flex;align-items:center;gap:18px}
.seekwrap{flex:1;position:relative;display:flex;align-items:center}
#padbar{position:absolute;left:0;right:0;top:calc(50% + 4px);height:4px;pointer-events:none}
#padbar i{position:absolute;top:0;bottom:0;background:#f5c518;border-radius:2px;opacity:.9}
#pskip{position:absolute;right:28px;bottom:120px;z-index:4;display:flex;gap:10px;align-items:center}
#pskip button{background:#000b;color:#fff;border:1px solid #fffa;border-radius:4px;padding:10px 18px;font-size:16px;font-weight:700;cursor:pointer}
#pskip button:hover{background:#fff;color:#000}
#pskip #pnotad{font-weight:400;font-size:14px;padding:8px 12px;border-color:#fff5;color:#ddd}
.ptop #pads{margin-left:auto;font-size:14px;color:#ddd;border:1px solid #fff5;border-radius:4px;padding:6px 10px;white-space:nowrap}
/* phones: volume is set with the phone's own buttons (iPhones ignore the slider), and the space keeps
   the full-screen button on screen */
@media (pointer:coarse){#pvol{display:none}}
.pctl{display:flex;align-items:center;gap:18px;margin-top:8px}
.pctl button{background:none;border:0;font-size:24px;min-width:32px}
.pctl .sp{flex:1}
.pctl select{background:#000a;color:#fff;border:1px solid #666;padding:4px}
#pcenter,.ptimes{display:none}
/* Phones and tablets: big controls. Back 30 s / play / forward 30 s in the middle of the picture, a
   slider you can tap anywhere above mute and full screen at the bottom */
#player.touch #pcenter{display:flex;top:50%;transform:translateY(-50%);justify-content:center;align-items:center;gap:44px;pointer-events:none}
#player.touch #pcenter button{pointer-events:auto}
#player.touch.idle .pov,#player.touch.idle #pcenter button{pointer-events:none}
#player.touch .pcbtn{display:grid;place-items:center;border:0;padding:0;border-radius:50%;color:#fff;cursor:pointer;-webkit-tap-highlight-color:transparent}
#player.touch .pcbtn svg{fill:currentColor}
#player.touch #pplay{width:96px;height:96px;background:#ffffff38;color:#fff;-webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px)}
#player.touch #pplay svg{width:52px;height:52px}
#player.touch #prew,#player.touch #pfwd{width:80px;height:80px;position:relative;background:#ffffff2e;-webkit-backdrop-filter:blur(4px);backdrop-filter:blur(4px)}
#player.touch #prew svg,#player.touch #pfwd svg{width:52px;height:52px}
#player.touch .pcbtn b{position:absolute;inset:0;display:grid;place-items:center;font-size:15px;font-weight:800;padding-top:6px;pointer-events:none}
#player.touch .pcbtn:active{filter:brightness(.85)}
#player.touch .pbot{padding:10px 20px max(18px,env(safe-area-inset-bottom));background:linear-gradient(#0000,#000d 40%)}
#player.touch .seekwrap{height:48px;touch-action:none;cursor:pointer}
#player.touch #pseek{pointer-events:none;-webkit-appearance:none;appearance:none;height:8px;border-radius:4px;background:linear-gradient(to right,var(--red) var(--pct,0%),#ffffff4d var(--pct,0%))}
#player.touch #pseek::-webkit-slider-thumb{-webkit-appearance:none;width:26px;height:26px;border-radius:50%;background:var(--red);border:0;box-shadow:0 2px 8px #000a}
#player.touch #pseek::-moz-range-thumb{width:26px;height:26px;border-radius:50%;background:var(--red);border:0}
#player.touch #ptime{display:none}
#player.touch .ptimes{display:flex;justify-content:space-between;font-size:16px;color:#ddd;margin-top:-6px;font-variant-numeric:tabular-nums}
#player.touch #padbar{top:calc(50% + 6px)}
#player.touch .pctl{margin-top:4px;gap:8px}
#player.touch .pctl button{width:56px;height:56px;min-width:56px;display:grid;place-items:center;border-radius:50%}
#player.touch .pctl button svg{width:32px;height:32px;fill:currentColor}
#player.touch #pskip{top:76px;bottom:auto;right:16px}
#psubbtn{display:none}
/* Computers: big TV-style controls. Title above a thick bar, then a row of large round buttons; the one
   under the mouse (or keyboard focus) turns white and shows its name, like the selected button on a TV */
#player.desk .ptop{padding:36px 48px;background:linear-gradient(#000c,#0000)}
#player.desk #pback{width:64px;height:64px;border-radius:50%;display:grid;place-items:center;color:#fff;cursor:pointer}
#player.desk #pback svg{width:40px;height:40px;fill:currentColor}
#player.desk #pads{font-size:16px;padding:8px 14px;cursor:pointer;color:#ddd;background:none}
#player.desk .pbot{padding:120px 72px 34px;background:linear-gradient(#0000,#000e 60%)}
#player.desk #ptitle{margin-bottom:22px}
#player.desk #ptitle b{display:block;font-size:40px;font-weight:800}
#player.desk #ptitle small{display:block;margin:4px 0 0;font-size:24px;color:#ccc}
#player.desk .seekrow{gap:24px;font-size:24px;font-variant-numeric:tabular-nums}
#player.desk .seekwrap{height:32px}
#player.desk #pseek{-webkit-appearance:none;appearance:none;height:8px;border-radius:4px;background:linear-gradient(to right,var(--red) var(--pct,0%),#ffffff4d var(--pct,0%))}
#player.desk #pseek::-webkit-slider-thumb{-webkit-appearance:none;width:24px;height:24px;border-radius:50%;background:var(--red);border:0;box-shadow:0 2px 8px #000a}
#player.desk #pseek::-moz-range-thumb{width:24px;height:24px;border-radius:50%;background:var(--red);border:0}
#player.desk #padbar{top:calc(50% + 8px);height:4px}
#player.desk .pctl{margin-top:24px;gap:20px;align-items:flex-start;padding-bottom:28px}
#player.desk .pctl .sp{align-self:center}
#player.desk .pctl button{position:relative;width:76px;height:76px;min-width:76px;padding:0;border-radius:50%;display:grid;place-items:center;color:#fff;cursor:pointer}
#player.desk .pctl button svg{width:50px;height:50px;fill:currentColor}
#player.desk .pctl button::after{content:attr(data-l);position:absolute;top:calc(100% + 8px);left:50%;transform:translateX(-50%);white-space:nowrap;font-size:15px;font-weight:700;color:#fff;opacity:0;pointer-events:none}
#player.desk .pctl button:hover,#player.desk .pctl button:focus-visible,#player.desk #psubw:hover #psubbtn,#player.desk #psubs:focus-visible+#psubbtn{background:#fff;color:#000;outline:0}
#player.desk .pctl button:hover::after,#player.desk .pctl button:focus-visible::after,#player.desk #psubw:hover #psubbtn::after,#player.desk #psubs:focus-visible+#psubbtn::after{opacity:1}
#player.desk .sk{position:relative;display:block;width:50px;height:50px}
#player.desk .sk b{position:absolute;inset:0;display:grid;place-items:center;font-size:15px;font-weight:800;padding-top:6px}
#player.desk .pvolw{display:flex;align-items:center;gap:14px;height:76px}
#player.desk #pvol{width:0;opacity:0;transition:width .2s,opacity .2s}
#player.desk .pvolw:hover #pvol,#player.desk #pvol:focus-visible{width:130px;opacity:1}
#player.desk #pmode{align-self:center;color:#bbb}
#player.desk #psubw{position:relative}
#player.desk #psubbtn{display:grid}
#player.desk #psubs{position:absolute;left:0;top:0;width:76px;height:76px;opacity:0;cursor:pointer;z-index:1}
#player.desk #pskip{bottom:320px;right:72px}
#player.desk #pskip button{font-size:20px;padding:14px 24px}#player.desk #pskip #pnotad{font-size:16px;padding:12px 16px}
#player.desk #pmsg{font-size:22px;padding:18px 28px}
@media (max-width:900px),(max-height:600px){#player.desk .pbot{padding:80px 28px 18px}#player.desk .ptop{padding:20px 24px}
  #player.desk #ptitle b{font-size:28px}#player.desk #ptitle small{font-size:18px}#player.desk .seekrow{font-size:18px}
  #player.desk .pctl{gap:8px;margin-top:14px}#player.desk .pctl button,#player.desk #psubs{width:56px;height:56px;min-width:56px}
  #player.desk .pctl button svg,#player.desk .sk{width:36px;height:36px}#player.desk .sk b{font-size:11px;padding-top:4px}
  #player.desk .pvolw{height:56px}#player.desk #pskip{bottom:240px;right:28px}}
#pmsg{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);background:#000c;padding:14px 22px;border-radius:6px;z-index:3;font-size:16px;pointer-events:none}
.spinner{width:52px;height:52px;border:5px solid #fff3;border-top-color:var(--red);border-radius:50%;animation:sp 1s linear infinite;position:absolute;left:50%;top:50%;margin:-26px;z-index:1;pointer-events:none}
@keyframes sp{to{transform:rotate(360deg)}}
/* music bar */
#mbar{position:fixed;left:0;right:0;bottom:0;z-index:30;background:#181818;border-top:1px solid #282828;display:grid;grid-template-columns:minmax(180px,1fr) 2fr minmax(120px,1fr);align-items:center;gap:16px;padding:10px 16px;height:84px}
#mbar .now{display:flex;gap:12px;align-items:center;min-width:0;cursor:pointer}
#mbar .now .art{width:56px;flex:0 0 56px}
#mbar .now .ph{font-size:9px}
#mbar .tt{min-width:0}#mbar .tt b,#mbar .tt span{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#mbar .tt span{color:var(--mut);font-size:13px}
.mctr{display:flex;flex-direction:column;align-items:center;gap:4px}
.mbtns{display:flex;gap:18px;align-items:center}
.mbtns button{background:none;border:0;font-size:18px;opacity:.85}.mbtns button:hover{opacity:1}
.mbtns button.on{color:#46d369}
.mbtns .pp{width:34px;height:34px;border-radius:50%;background:#fff;color:#000;font-size:15px;opacity:1}
.mseek{display:flex;gap:8px;align-items:center;width:100%;font-size:11px;color:var(--mut)}
.mseek input{flex:1}
.mright{display:flex;justify-content:flex-end;gap:10px;align-items:center}
.mright input{width:100px}
.mright button{background:none;border:0;font-size:18px}
#mexpand{font-size:0!important}#mexpand svg{width:22px;height:22px;fill:currentColor;vertical-align:middle}
#mprog,#mptimes{display:none}
/* Phones and tablets: the music bar in two rows. Song + "Big player" on top, big previous / play / next
   below, and a thin progress line along the top edge */
#mbar.touch{display:grid;grid-template-columns:minmax(0,1fr) auto;grid-template-areas:"prog prog" "times times" "now open" "ctl ctl";height:auto;gap:0;
  padding:0 0 max(14px,env(safe-area-inset-bottom));align-items:center}
/* the top edge is a slider: tap anywhere to jump there, or drag (the knob grows under your finger) */
#mbar.touch #mprog{display:flex;grid-area:prog;height:40px;margin-top:-20px;padding:0 12px;align-items:center;touch-action:none;cursor:pointer}
#mbar.touch #mprog .mptrk{position:relative;width:100%;height:6px;background:#ffffff38;border-radius:3px}
#mbar.touch #mprog i{position:absolute;left:0;top:0;bottom:0;width:0;background:#fff;border-radius:3px}
#mbar.touch #mprog em{position:absolute;top:50%;left:0;width:22px;height:22px;margin:-11px 0 0 -11px;background:#fff;border-radius:50%;box-shadow:0 2px 6px #000a;transition:width .1s,height .1s,margin .1s}
#mbar.touch #mprog.drag em{width:32px;height:32px;margin:-16px 0 0 -16px}
#mbar.touch #mptimes{display:flex;grid-area:times;justify-content:space-between;padding:0 14px;margin-top:-10px;font-size:13px;color:#bbb;font-variant-numeric:tabular-nums}
#mbar.touch .now{grid-area:now;padding:4px 10px 4px 14px;gap:12px}
#mbar.touch .now .art{width:52px;flex-basis:52px}
#mbar.touch .tt b{font-size:17px}#mbar.touch .tt span{font-size:14px}
#mbar.touch #mexpand{grid-area:open;font-size:15px!important;font-weight:700;height:52px;padding:0 16px;margin:12px 14px 4px 0;border-radius:26px;
  background:#ffffff1f;display:flex;align-items:center;gap:6px;color:#fff;border:0;white-space:nowrap;cursor:pointer;margin-top:4px}
#mbar.touch #mexpand svg{width:22px;height:22px}
#mbar.touch .mctr{grid-area:ctl;padding-top:6px}
#mbar.touch .mseek,#mbar.touch #mshuf,#mbar.touch #mrep,#mbar.touch .mright{display:none}
#mbar.touch .mbtns{gap:34px}
#mbar.touch .mbtns button{width:72px;height:72px;min-width:72px;min-height:72px;border-radius:50%;display:grid;place-items:center;opacity:1;color:#fff;padding:0;-webkit-tap-highlight-color:transparent}
#mbar.touch .mbtns button:active{background:#ffffff1f}
#mbar.touch .mbtns svg{width:46px;height:46px;fill:currentColor}
#mbar.touch .mbtns .pp{width:84px;height:84px;background:#fff;color:#000}
#mbar.touch .mbtns .pp:active{background:#ddd}
#mbar.touch .mbtns .pp svg{width:48px;height:48px}
body.mplay.touchbar main{padding-bottom:230px}
body.mplay.touchbar .scanning{bottom:200px}
/* Now Playing: big controls, made for phones (and the car) */
#mfull{position:fixed;inset:0;z-index:70;background:linear-gradient(#2b2b2b,#090909 70%);display:flex;flex-direction:column;align-items:center;gap:12px;
  padding:max(10px,env(safe-area-inset-top)) 20px max(18px,env(safe-area-inset-bottom));touch-action:manipulation;-webkit-user-select:none;user-select:none;overflow:hidden}
#mfull button{background:none;border:0;color:#fff;display:grid;place-items:center;min-width:56px;min-height:56px;border-radius:50%;cursor:pointer;padding:0;-webkit-tap-highlight-color:transparent}
#mfull button:active{background:#ffffff26}
#mfull button.on{color:#46d369}
#mfull svg{width:30px;height:30px;fill:currentColor}
.mfhead{grid-area:head;width:100%;max-width:640px;display:flex;align-items:center;justify-content:space-between;color:#bbb;font-size:15px;font-weight:600}
.mfart{grid-area:art;flex:1 1 auto;min-height:0;width:100%;max-width:min(80vw,34vh,460px);display:flex;align-items:center;justify-content:center}
.mfart .art{width:100%;aspect-ratio:1;max-height:100%;border-radius:8px;box-shadow:0 10px 30px #000a}
.mfart .art .ph{font-size:22px}
.mfinfo{grid-area:info;width:100%;max-width:640px;text-align:center;min-width:0}
#mftitle{font-size:26px;font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#mfsub{font-size:17px;color:#bbb;margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer}
.mfseek{grid-area:seek;width:100%;max-width:640px;height:56px;display:flex;align-items:center;cursor:pointer;touch-action:none}
.mfseek .mftrack{position:relative;width:100%;height:10px;background:#ffffff38;border-radius:5px}
.mfseek .fill{position:absolute;left:0;top:0;bottom:0;background:#fff;border-radius:5px}
.mfseek .knob{position:absolute;top:50%;width:28px;height:28px;margin:-14px 0 0 -14px;background:#fff;border-radius:50%;box-shadow:0 2px 8px #000a;transition:width .1s,height .1s,margin .1s}
.mfseek.drag .knob{width:40px;height:40px;margin:-20px 0 0 -20px}
.mftimes{grid-area:times;width:100%;max-width:640px;display:flex;justify-content:space-between;font-size:18px;color:#ddd;font-variant-numeric:tabular-nums;margin-top:-8px}
.mfhead .r{display:flex;gap:4px}
/* 1: previous / next song, above the slider and well away from the skip buttons */
.mfsong{grid-area:song;width:100%;max-width:520px;display:flex;justify-content:space-between;gap:20px;margin-top:4px}
#mfull .mfsong button{flex:1 1 0;height:64px;border-radius:32px;background:#ffffff1c;display:flex;align-items:center;justify-content:center;gap:8px;font-size:19px;font-weight:700}
#mfull .mfsong button:active{background:#ffffff33}
#mfull .mfsong svg{width:34px;height:34px}
/* 3: back 15 s / play / forward 15 s. Tap: 15 s, double-tap: 30 s */
.mfctl{grid-area:ctl;width:100%;max-width:420px;display:flex;align-items:center;justify-content:space-between;margin-top:4px}
#mfull .skip{width:92px;height:92px;position:relative;background:#ffffff1c;transition:background .15s}
#mfull .skip:active{background:#ffffff33}
#mfull .skip svg{width:58px;height:58px;transition:opacity .15s}
#mfull .skip b{position:absolute;inset:0;display:grid;place-items:center;font-size:17px;font-weight:800;padding-top:7px;pointer-events:none}
#mfull .skip.pend{background:var(--red)}
#mfull .skip.pend svg{opacity:.22}
#mfull .skip.pend b{font-size:26px;padding-top:0}
#mfull #mfplay{width:108px;height:108px;background:#fff;color:#000}
#mfull #mfplay:active{background:#ddd}
#mfull #mfplay svg{width:58px;height:58px}
.mfhint{grid-area:hint;font-size:13px;color:#8c8c8c}
/* a phone on its side (a car mount): picture on the left, the same three rows on the right */
@media (orientation:landscape) and (max-height:540px){
 #mfull{display:grid;grid-template-columns:minmax(0,min(46vh,32vw)) minmax(0,1fr);
   grid-template-areas:"head head" "art info" "art song" "art seek" "art times" "art ctl";grid-template-rows:auto auto auto auto auto 1fr;
   column-gap:28px;row-gap:0;align-items:center;justify-items:center;
   padding:max(4px,env(safe-area-inset-top)) max(24px,env(safe-area-inset-right)) max(10px,env(safe-area-inset-bottom)) max(24px,env(safe-area-inset-left))}
 .mfart{max-width:none;height:100%;align-self:center}
 #mftitle{font-size:20px}#mfsub{font-size:14px;margin-top:2px}
 .mfsong{margin-top:6px}#mfull .mfsong button{height:50px}
 .mfseek{height:44px;margin-top:2px}.mftimes{font-size:15px;margin-top:-6px}
 .mfctl{max-width:380px;margin-top:0}
 #mfull .skip{width:76px;height:76px}#mfull .skip svg{width:48px;height:48px}#mfull .skip b{font-size:15px;padding-top:6px}
 #mfull #mfplay{width:86px;height:86px}#mfull #mfplay svg{width:48px;height:48px}
 .mfhint{display:none}
}
/* profiles gate */
#gate{position:fixed;inset:0;z-index:80;background:var(--bg);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:30px}
#gate h1{font-weight:400;font-size:clamp(28px,4vw,52px);margin:0}
.profs{display:flex;gap:2vw;flex-wrap:wrap;justify-content:center}
.prof{display:flex;flex-direction:column;align-items:center;gap:10px;cursor:pointer;color:#808080;position:relative}
.prof .sq{width:clamp(84px,10vw,150px);aspect-ratio:1;border-radius:6px;display:grid;place-items:center;font-size:clamp(34px,4vw,64px);font-weight:800;color:#fff;border:3px solid transparent}
.prof:hover{color:#fff}.prof:hover .sq{border-color:#fff}
.prof .add{background:none;border:3px solid #808080!important;color:#808080;font-weight:300}
.prof .del{position:absolute;top:-8px;right:-8px;width:28px;height:28px;border-radius:50%;background:var(--red);border:0;color:#fff;font-size:14px}
.gbtn{background:none;border:1px solid #808080;color:#808080;padding:8px 22px;letter-spacing:2px;font-size:14px}
.gbtn:hover{color:#fff;border-color:#fff}
@media (max-width:800px){
 #links{display:none}#nav.menu #links{display:flex;position:absolute;top:60px;left:0;right:0;flex-direction:column;background:#141414;padding:14px 4%}
 #menuBtn{display:inline-block!important}
 #search{width:130px}
 .hero{height:60vh}.hero .info{bottom:16%}
 .rows{margin-top:-6vh}
 #mbar{grid-template-columns:1fr auto;height:76px;padding:8px 10px 8px 12px}
 .mright,.mseek,#mshuf,#mrep{display:none}
 .mbtns{gap:4px}.mbtns button{min-width:48px;min-height:48px;font-size:22px;opacity:1}
 .mbtns .pp{width:48px;height:48px;font-size:20px}
 .ep .art{width:100px;flex-basis:100px}
 .mhead.album{flex-direction:column;align-items:flex-start}.mhead.album .art{width:150px;flex-basis:auto}
}
</style></head>
<body>
<div id="gate" class="hidden"></div>
<header id="nav">
  <button id="menuBtn" class="iconbtn" style="display:none" aria-label="Menu">&#9776;</button>
  <div class="logo" data-go="home" title="Homeflix __VERSION__">HOMEFLIX<span class="ver">v__VERSION__</span></div>
  <nav id="links">
    <a data-go="home">Home</a><a data-go="shows">TV Shows</a><a data-go="movies">Movies</a>
    <a data-go="music">Music</a><a data-go="recent">New &amp; Popular</a><a data-go="mylist">My List</a>
  </nav>
  <div class="navr">
    <input id="search" type="search" placeholder="Titles, artists, albums" autocomplete="off">
    <button id="rescan" class="iconbtn" title="Rescan library">&#10227;</button>
    <div id="avatar" class="avatar" title="Switch profile"></div>
  </div>
</header>
<main id="main"></main>
<div id="modal" class="hidden"></div>
<div id="scan" class="scanning hidden">Scanning library&hellip;</div>

<div id="player" class="hidden">
  <video id="vid" playsinline preload="auto"></video>
  <div class="spinner hidden" id="pspin"></div>
  <div id="pmsg" class="hidden"></div>
  <div class="pov" id="pcenter"></div>
  <div class="pov ptop"><button id="pback" title="Back (Esc)">&#8592;</button><div id="ptitle"></div><button id="pads" class="hidden" title="Skip ad breaks automatically, or ask each time"></button></div>
  <div id="pskip" class="hidden"><button id="pnotad" title="Never skip this part">Not an ad</button><button id="pskipbtn"></button></div>
  <div class="pov pbot">
    <div class="seekrow"><div class="seekwrap"><input type="range" id="pseek" min="0" max="1000" value="0" step="1"><div id="padbar"></div></div><span id="ptime">0:00</span></div>
    <div class="ptimes"><span id="ptcur">0:00</span><span id="ptdur">0:00</span></div>
    <div class="pctl">
      <button id="pplay" title="Play/Pause (Space)">&#10074;&#10074;</button>
      <button id="prew" title="Back 10s (&larr;)">&#8634;</button>
      <button id="pfwd" title="Forward 10s (&rarr;)">&#8635;</button>
      <div class="pvolw"><button id="pmute" title="Mute (M)">&#128266;</button>
      <input type="range" id="pvol" min="0" max="1" step="0.05" value="1" aria-label="Volume"></div>
      <span class="sp"></span>
      <span id="pmode" class="small"></span>
      <div id="psubw"><select id="psubs" title="Subtitles"></select><button id="psubbtn" tabindex="-1" aria-hidden="true" data-l="Subtitles"></button></div>
      <button id="pnext" title="Next episode (N)" class="hidden">&#9197;</button>
      <button id="pfs" title="Full screen (F)">&#9974;</button>
    </div>
  </div>
</div>

<div id="mbar" class="hidden">
  <div id="mprog" role="slider" aria-label="Position in song"><div class="mptrk"><i></i><em></em></div></div>
  <div id="mptimes"><span id="mpcur">0:00</span><span id="mpdur">0:00</span></div>
  <div class="now" id="mnow"></div>
  <div class="mctr">
    <div class="mbtns">
      <button id="mshuf" title="Shuffle">&#8644;</button>
      <button id="mprev" title="Previous">&#9198;</button>
      <button id="mplay" class="pp" title="Play/Pause">&#9654;</button>
      <button id="mnext" title="Next">&#9197;</button>
      <button id="mrep" title="Repeat">&#8635;</button>
      <button id="mexpand" title="Big controls (Now Playing)" aria-label="Open Now Playing"><svg viewBox="0 0 24 24"><path d="M7.4 15.4 12 10.8l4.6 4.6L18 14l-6-6-6 6z"/></svg></button>
    </div>
    <div class="mseek"><span id="mcur">0:00</span><input type="range" id="mseek" min="0" max="1000" value="0"><span id="mdur">0:00</span></div>
  </div>
  <div class="mright"><span>&#128264;</span><input type="range" id="mvol" min="0" max="1" step="0.05" value="1"><button id="mclose" title="Close">&#10005;</button></div>
  <audio id="aud" preload="auto"></audio>
</div>

<div id="mfull" class="hidden" role="dialog" aria-label="Now Playing">
  <div class="mfhead"><button id="mfclose" aria-label="Close Now Playing"></button><span>Now Playing</span><span class="r"><button id="mfrep" aria-label="Repeat"></button><button id="mfshuf" aria-label="Shuffle"></button></span></div>
  <div class="mfart" id="mfart"></div>
  <div class="mfinfo"><div id="mftitle"></div><div id="mfsub" title="Open the album"></div></div>
  <div class="mfsong"><button id="mfprev" aria-label="Previous song"></button><button id="mfnext" aria-label="Next song"></button></div>
  <div class="mfseek" id="mfseek" role="slider" aria-label="Position in song"><div class="mftrack"><div class="fill" id="mffill"></div><div class="knob" id="mfknob"></div></div></div>
  <div class="mftimes"><span id="mfcur">0:00</span><span id="mfdur">0:00</span></div>
  <div class="mfctl">
    <button id="mfback" class="skip" aria-label="Back 15 seconds (double-tap: 30)"></button>
    <button id="mfplay" aria-label="Play or pause"></button>
    <button id="mffwd" class="skip" aria-label="Forward 15 seconds (double-tap: 30)"></button>
  </div>
  <div class="mfhint">Tap: 15 s &middot; Double-tap: 30 s</div>
</div>

<script>
"use strict";
const $=s=>document.querySelector(s);
const S={items:[],byId:{},movies:[],shows:[],albums:[],showByKey:{},albumByKey:{},profile:null,profiles:[],
         progress:{},mylist:[],view:'home',q:'',version:-1,scanning:false,transcode:true,auth:false};
// Can this browser decode HEVC itself? Then HEVC files are passed through untouched instead of re-encoded.
const HEVC=(()=>{const v=document.createElement('video'),ok=c=>v.canPlayType(`video/mp4; codecs="${c}"`)==='probably';return ok('hvc1.2.4.L120.90')?'10':ok('hvc1.1.6.L120.90')?'1':''})();
const hevcPass=it=>!!HEVC&&it.vcodec==='hevc';

function h(tag,attrs,...kids){const e=document.createElement(tag);
  for(const[k,v]of Object.entries(attrs||{})){if(v==null||v===false)continue;
    if(k.startsWith('on'))e.addEventListener(k.slice(2),v);else if(k==='class')e.className=v;else if(k==='style')e.style.cssText=v;else e.setAttribute(k,v===true?'':v)}
  for(const c of kids.flat(9)){if(c==null||c===false)continue;e.append(c.nodeType?c:document.createTextNode(String(c)))}return e}
async function api(p,opt){const r=await fetch(p,opt);if(r.status===401){location.reload();throw new Error('auth')}if(!r.ok)throw new Error(await r.text());return r.json()}
const post=(p,b)=>api(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
function fmtT(s){s=Math.max(0,Math.floor(s||0));const hh=Math.floor(s/3600),m=Math.floor(s%3600/60),ss=s%60;return(hh?hh+':'+String(m).padStart(2,'0'):m)+':'+String(ss).padStart(2,'0')}
function fmtDur(s){if(!s)return'';const m=Math.max(1,Math.round(s/60));return m>=60?Math.floor(m/60)+'h '+(m%60)+'m':m+'m'}
function hash(s){let x=0;for(const c of String(s))x=(x*31+c.charCodeAt(0))|0;return Math.abs(x)}
function grad(s){const a=hash(s)%360;return`linear-gradient(135deg,hsl(${a} 55% 32%),hsl(${(a+40)%360} 60% 14%))`}
function res(it){const w=it.width||0,hh=it.height||0;if(w>=3200||hh>=1900)return'4K';if(w>=1800||hh>=1000)return'HD';if(w>=1200||hh>=700)return'720p';return w?'SD':''}
const isNew=it=>Date.now()/1000-it.added<14*86400;

function meta(...k){return h('div',{class:'meta'},k.flat().filter(x=>x!=null&&x!==''&&x!==false).map(x=>x.nodeType?x:h('span',{},x)))}
function art(id,title,sq,badge){const w=h('div',{class:'art'+(sq?' sq':''),style:'background:'+grad(title)},h('span',{class:'ph'},title));
  if(badge)w.append(h('span',{class:'badge'},badge));
  if(id){const it=S.byId[id],i=h('img',{loading:'lazy',alt:'',src:'/thumb/'+id+(it?'?v='+it.added:'')});i.onload=()=>w.classList.add('loaded');i.onerror=()=>i.remove();w.append(i)}return w}

/* ---------- data ---------- */
function group(){
  S.byId={};for(const it of S.items)S.byId[it.id]=it;
  const vids=S.items.filter(i=>i.kind==='video');
  S.movies=vids.filter(v=>!v.show).sort((a,b)=>a.title.localeCompare(b.title));
  const sh={};for(const v of vids.filter(v=>v.show)){(sh[v.show]??={key:'show:'+v.show,name:v.show,eps:[],added:0,year:null}).eps.push(v)}
  S.shows=Object.values(sh);for(const s of S.shows){s.eps.sort((a,b)=>a.season-b.season||a.episode-b.episode);s.added=Math.max(...s.eps.map(e=>e.added));s.year=s.eps.find(e=>e.year)?.year||null;s.seasons=[...new Set(s.eps.map(e=>e.season))]}
  S.shows.sort((a,b)=>a.name.localeCompare(b.name));S.showByKey={};for(const s of S.shows)S.showByKey[s.key]=s;
  const al={};for(const t of S.items.filter(i=>i.kind==='audio')){const k='album:'+t.albumArtist+'\u0001'+t.album;(al[k]??={key:k,title:t.album,artist:t.albumArtist,tracks:[],year:null,added:0,genre:null}).tracks.push(t)}
  S.albums=Object.values(al);for(const a of S.albums){a.tracks.sort((x,y)=>(x.disc-y.disc)||(x.track-y.track)||x.title.localeCompare(y.title));a.year=a.tracks.find(t=>t.year)?.year||null;a.added=Math.max(...a.tracks.map(t=>t.added));a.cover=a.tracks[0].id;a.genre=a.tracks.find(t=>t.genre)?.genre;
    const arts=new Set(a.tracks.map(t=>t.artist));if(a.artist==='Unknown Artist'&&arts.size===1)a.artist=[...arts][0]}
  S.albums.sort((a,b)=>a.artist.localeCompare(b.artist)||(a.year||0)-(b.year||0)||a.title.localeCompare(b.title));S.albumByKey={};for(const a of S.albums)S.albumByKey[a.key]=a;
}
async function loadLibrary(){
  const d=await api('/api/library');S.scanning=d.scanning;S.transcode=d.transcode;S.roots=d.roots;
  $('#scan').classList.toggle('hidden',!d.scanning);
  if(d.version!==S.version){S.version=d.version;S.items=d.items;group();render()}
  if(d.scanning)setTimeout(loadLibrary,3000);
}
async function loadState(){const d=await api('/api/state?profile='+encodeURIComponent(S.profile.id));S.progress=d.progress||{};S.mylist=d.mylist||[]}
const prog=id=>S.progress[id];
function pct(id){const p=prog(id);if(!p||!p.dur||p.done)return 0;return Math.min(100,p.pos/p.dur*100)}

function continueList(){
  const out=[];const seen=new Set();
  const ents=Object.entries(S.progress).filter(([id])=>S.byId[id]&&S.byId[id].kind==='video').sort((a,b)=>b[1].updated-a[1].updated);
  for(const[id,p]of ents){const it=S.byId[id];
    if(it.show){if(seen.has(it.show))continue;seen.add(it.show);
      if(p.done){const s=S.showByKey['show:'+it.show];const i=s.eps.indexOf(it);const nx=s.eps[i+1];if(nx&&!(prog(nx.id)?.done))out.push(nx)}
      else if(p.pos>20)out.push(it)}
    else if(!p.done&&p.pos>20)out.push(it)}
  return out.slice(0,20)}

/* ---------- cards & rows ---------- */
function itemCard(it){const sub=it.show?`${it.show} · S${it.season}:E${it.episode}`:[it.year,fmtDur(it.duration)].filter(Boolean).join(' · ');
  const p=pct(it.id);const a=art(it.id,it.show||it.title,false,isNew(it)?'NEW':null);if(p)a.append(h('div',{class:'pbar'},h('i',{style:`width:${p}%`})));
  return h('div',{class:'card',onclick:()=>it.show?openShow(S.showByKey['show:'+it.show],it):openMovie(it)},a,h('div',{class:'lbl'},it.show?(it.epTitle||it.title):it.title),h('div',{class:'sub'},sub))}
function showCard(s){return h('div',{class:'card',onclick:()=>openShow(s)},art(s.eps[0].id,s.name,false,isNew(s)?'NEW EPISODES':null),h('div',{class:'lbl'},s.name),h('div',{class:'sub'},`${s.seasons.length} Season${s.seasons.length>1?'s':''} · ${s.eps.length} ep`))}
function albumCard(a){return h('div',{class:'card sq',onclick:()=>openAlbum(a)},art(a.cover,a.title,true),h('div',{class:'lbl'},a.title),h('div',{class:'sub'},[a.artist,a.year].filter(Boolean).join(' · ')))}
function keyCard(k){if(k.startsWith('show:'))return S.showByKey[k]&&showCard(S.showByKey[k]);if(k.startsWith('album:'))return S.albumByKey[k]&&albumCard(S.albumByKey[k]);return S.byId[k]&&itemCard(S.byId[k])}
function row(title,cards){cards=cards.filter(Boolean);if(!cards.length)return null;const tr=h('div',{class:'track'},cards);
  const sc=d=>tr.scrollBy({left:d*tr.clientWidth*.85});
  return h('section',{class:'row'},h('h2',{},title),h('div',{class:'rowwrap'},h('button',{class:'arr l',onclick:()=>sc(-1),'aria-label':'Scroll left'},'‹'),tr,h('button',{class:'arr r',onclick:()=>sc(1),'aria-label':'Scroll right'},'›')))}
function recentVideos(){const out=[];const seen=new Set();for(const v of S.items.filter(i=>i.kind==='video').sort((a,b)=>b.added-a.added)){if(v.show){if(seen.has(v.show))continue;seen.add(v.show);out.push(showCard(S.showByKey['show:'+v.show]))}else out.push(itemCard(v));if(out.length>=30)break}return out}

/* ---------- views ---------- */
function go(v){S.view=v;if(v!=='search'){S.q='';$('#search').value=''}$('#nav').classList.remove('menu');render();window.scrollTo(0,0)}
function render(){
  document.querySelectorAll('#links a').forEach(a=>a.classList.toggle('on',a.dataset.go===S.view));
  const m=$('#main');m.replaceChildren();
  if(!S.items.length){m.append(h('div',{class:'empty'},h('h2',{},S.scanning?'Scanning your library…':'Nothing here yet'),
     h('p',{},'Homeflix looks in: ',(S.roots||[]).map((r,i)=>[i?', ':'',h('code',{},r)])),h('p',{},'Add videos or music there, then press ⟳ to rescan.')));return}
  const V={home:viewHome,movies:viewMovies,shows:viewShows,music:viewMusic,mylist:viewMyList,recent:viewRecent,search:viewSearch}[S.view]||viewHome;V(m)}
function hero(m){const pool=[...S.movies,...S.shows.map(s=>s.eps[0])];if(!pool.length)return false;
  const pick=pool[hash(new Date().toDateString()+S.profile.id)%pool.length];const isShow=!!pick.show;const title=isShow?pick.show:pick.title;
  const bg=h('div',{class:'bg'});const img=new Image();bg.style.backgroundImage=grad(title);img.onload=()=>bg.style.backgroundImage=`url(/thumb/${pick.id})`;img.src='/thumb/'+pick.id;
  const meta=isShow?`${S.showByKey['show:'+pick.show].eps.length} episodes`:[pick.year,fmtDur(pick.duration),res(pick)].filter(Boolean).join('  ·  ');
  m.append(h('div',{class:'hero'},bg,h('div',{class:'info'},h('h1',{},title),h('p',{},meta),h('div',{class:'btns'},
    h('button',{class:'btn play',onclick:()=>isShow?playShow(S.showByKey['show:'+pick.show]):playVideo(pick)},'▶  Play'),
    h('button',{class:'btn gray',onclick:()=>isShow?openShow(S.showByKey['show:'+pick.show]):openMovie(pick)},'ⓘ  More Info')))));return true}
function viewHome(m){const hasHero=hero(m);const w=h('div',{class:hasHero?'rows':'rows page'});
  const genres={};for(const a of S.albums)if(a.genre)(genres[a.genre]??=[]).push(a);
  w.append(...[row(`Continue Watching for ${S.profile.name}`,continueList().map(itemCard)),row('My List',S.mylist.map(keyCard)),
    row('Recently Added',recentVideos()),row('TV Shows',S.shows.map(showCard)),row('Movies',S.movies.map(itemCard)),
    row('New Music',[...S.albums].sort((a,b)=>b.added-a.added).slice(0,30).map(albumCard)),
    ...Object.entries(genres).filter(([,l])=>l.length>2).slice(0,6).map(([g,l])=>row(g,l.map(albumCard))),
    S.albums.length>30?row('All Albums',S.albums.map(albumCard)):null].filter(Boolean));m.append(w)}
function sortSel(cur,opts,cb){const s=h('select',{class:'sel'},opts.map(([v,l])=>h('option',{value:v,selected:v===cur},l)));s.onchange=()=>cb(s.value);return s}
function viewMovies(m){S.msort??='az';const list=[...S.movies];if(S.msort==='new')list.sort((a,b)=>b.added-a.added);if(S.msort==='year')list.sort((a,b)=>(b.year||0)-(a.year||0));
  m.append(h('div',{class:'page'},h('div',{class:'ptitle'},h('h1',{},'Movies'),sortSel(S.msort,[['az','A–Z'],['new','Recently Added'],['year','Release Year']],v=>{S.msort=v;render()}),h('span',{class:'small'},`${list.length} titles`)),
   list.length?h('div',{class:'grid'},list.map(itemCard)):h('div',{class:'empty'},'No movies found.')))}
function viewShows(m){m.append(h('div',{class:'page'},h('div',{class:'ptitle'},h('h1',{},'TV Shows')),S.shows.length?h('div',{class:'grid'},S.shows.map(showCard)):h('div',{class:'empty'},'No TV shows found. Name episodes like ',h('code',{},'Show Name S01E02.mkv'),'.')))}
function viewRecent(m){m.append(h('div',{class:'page'},h('div',{class:'ptitle'},h('h1',{},'New & Popular')),row('Recently Added Videos',recentVideos()),row('Recently Added Music',[...S.albums].sort((a,b)=>b.added-a.added).slice(0,40).map(albumCard))))}
function viewMyList(m){const c=S.mylist.map(keyCard).filter(Boolean);m.append(h('div',{class:'page'},h('div',{class:'ptitle'},h('h1',{},'My List')),c.length?h('div',{class:'grid'},c):h('div',{class:'empty'},'Add titles with the + button.')))}
function viewMusic(m){S.asort??='artist';const list=[...S.albums];if(S.asort==='new')list.sort((a,b)=>b.added-a.added);if(S.asort==='title')list.sort((a,b)=>a.title.localeCompare(b.title));
  const all=S.items.filter(i=>i.kind==='audio');
  m.append(h('div',{class:'page'},h('div',{class:'ptitle'},h('h1',{},'Music'),sortSel(S.asort,[['artist','Artist'],['title','Album'],['new','Recently Added']],v=>{S.asort=v;render()}),
     h('button',{class:'btn play',onclick:()=>playQueue(all,0,true)},'⇄  Shuffle All'),h('span',{class:'small'},`${S.albums.length} albums · ${all.length} songs`)),
   list.length?h('div',{class:'grid sq'},list.map(albumCard)):h('div',{class:'empty'},'No music found.')))}
function viewSearch(m){const q=S.q.toLowerCase().trim();const has=(...f)=>f.some(x=>x&&String(x).toLowerCase().includes(q));
  const mv=S.movies.filter(v=>has(v.title,v.year,v.file)),sh=S.shows.filter(s=>has(s.name)),eps=S.items.filter(v=>v.show&&has(v.epTitle)&&!has(v.show)),
        al=S.albums.filter(a=>has(a.title,a.artist,a.genre)),tr=S.items.filter(t=>t.kind==='audio'&&has(t.title,t.artist));
  const w=h('div',{class:'page'},h('div',{class:'ptitle'},h('h1',{},`Results for “${S.q}”`)));
  if(!(mv.length+sh.length+eps.length+al.length+tr.length))w.append(h('div',{class:'empty'},'No matches.'));
  w.append(...[row('Movies',mv.map(itemCard)),row('TV Shows',sh.map(showCard)),row('Episodes',eps.slice(0,40).map(itemCard)),row('Albums',al.map(albumCard))].filter(Boolean));
  if(tr.length)w.append(h('section',{class:'row',style:'padding:0 4%'},h('h2',{style:'margin:0 0 10px'},'Songs'),tr.slice(0,50).map((t,i)=>trackRow(t,i+1,()=>playQueue(tr,i)))));m.append(w)}

/* ---------- detail modals ---------- */
function modal(content){const md=$('#modal');md.replaceChildren(h('div',{class:'mbox'},h('button',{class:'close',onclick:closeModal,'aria-label':'Close'},'✕'),content));md.classList.remove('hidden');document.body.classList.add('noscroll');md.scrollTop=0}
function closeModal(){$('#modal').classList.add('hidden');document.body.classList.remove('noscroll')}
$('#modal').addEventListener('click',e=>{if(e.target.id==='modal')closeModal()});
function listBtn(key){const on=S.mylist.includes(key);const b=h('button',{class:'btn round',title:on?'Remove from My List':'Add to My List'},on?'✓':'+');
  b.onclick=async()=>{const now=!S.mylist.includes(key);await post('/api/mylist',{profile:S.profile.id,key,on:now});if(now)S.mylist.unshift(key);else S.mylist=S.mylist.filter(k=>k!==key);b.textContent=now?'✓':'+';b.title=now?'Remove from My List':'Add to My List';if(S.view==='mylist'||S.view==='home')render()};return b}
function resumeLabel(it){const p=prog(it.id);return p&&!p.done&&p.pos>20?`▶  Resume ${fmtT(p.pos)}`:'▶  Play'}
function openMovie(it){const p=prog(it.id);const r=res(it);
  modal([h('div',{class:'mhead'},art(it.id,it.title),h('div',{class:'info'},h('h1',{},it.title),h('div',{class:'btns'},
      h('button',{class:'btn play',onclick:()=>{closeModal();playVideo(it)}},resumeLabel(it)),
      p&&p.pos>20?h('button',{class:'btn gray',onclick:()=>{closeModal();playVideo(it,0)}},'↺  Start Over'):null,listBtn(it.id)))),
   h('div',{class:'mbody'},meta(isNew(it)?h('span',{class:'new'},'New'):null,it.year,fmtDur(it.duration),r?h('span',{class:'hd'},r):null,it.subs?.length?h('span',{class:'hd'},'CC'):null,
       !it.direct&&S.transcode&&!hevcPass(it)?h('span',{class:'hd',title:'Converted on the fly for your browser'},'TRANSCODE'):null),
     p&&!p.done&&p.dur?h('div',{style:'max-width:300px;margin-bottom:14px'},h('div',{class:'pbar',style:'position:static'},h('i',{style:`width:${pct(it.id)}%`})),h('div',{class:'small',style:'margin-top:6px'},`${fmtT(p.dur-p.pos)} remaining`)):null,
     h('div',{class:'small'},it.folder+' / '+it.file),h('div',{class:'small'},(it.size/1e9).toFixed(2)+' GB'))])}
function nextUp(s){const ents=s.eps.map(e=>[e,prog(e.id)]).filter(x=>x[1]).sort((a,b)=>b[1].updated-a[1].updated);if(!ents.length)return s.eps[0];const[e,p]=ents[0];if(!p.done)return e;return s.eps[s.eps.indexOf(e)+1]||s.eps[0]}
function playShow(s){playVideo(nextUp(s))}
function openShow(s,focusEp){const nu=focusEp||nextUp(s);let season=nu.season;
  const eplist=h('div');const drawEps=()=>{eplist.replaceChildren(...s.eps.filter(e=>e.season===season).map(e=>{const p=pct(e.id);const a=art(e.id,`E${e.episode}`);if(p)a.append(h('div',{class:'pbar'},h('i',{style:`width:${p}%`})));if(prog(e.id)?.done)a.append(h('span',{class:'badge',style:'background:#46d369;color:#000'},'✓'));
     return h('div',{class:'ep',onclick:()=>{closeModal();playVideo(e)}},h('div',{class:'n'},e.episode),a,h('div',{class:'t'},h('b',{},e.epTitle||`Episode ${e.episode}`),h('div',{class:'d'},fmtDur(e.duration)+(e.subs?.length?'  · CC':''))))}))};
  const sel=s.seasons.length>1?sortSel(String(season),s.seasons.map(n=>[String(n),'Season '+n]),v=>{season=+v;drawEps()}):h('span',{class:'small'},'Season '+season);
  drawEps();
  modal([h('div',{class:'mhead'},art(s.eps[0].id,s.name),h('div',{class:'info'},h('h1',{},s.name),h('div',{class:'btns'},
     h('button',{class:'btn play',onclick:()=>{closeModal();playVideo(nu)}},prog(nu.id)&&!prog(nu.id).done?`▶  Resume S${nu.season}:E${nu.episode}`:`▶  Play S${nu.season}:E${nu.episode}`),listBtn(s.key)))),
   h('div',{class:'mbody'},meta(s.year,`${s.seasons.length} Season${s.seasons.length>1?'s':''}`,`${s.eps.length} Episodes`),
     h('div',{class:'eps'},h('h3',{},'Episodes',sel),eplist))])}
function trackRow(t,n,onplay){const on=M.q[M.i]&&M.q[M.i].id===t.id&&!$('#mbar').classList.contains('hidden');
  return h('div',{class:'trk'+(on?' on':''),onclick:onplay},h('div',{class:'n'},on?'♫':n),h('div',{},h('div',{},t.title),h('div',{class:'a'},t.artist)),h('div',{class:'a'},fmtT(t.duration)))}
function openAlbum(a){const dur=a.tracks.reduce((s,t)=>s+(t.duration||0),0);
  modal([h('div',{class:'mhead album'},art(a.cover,a.title,true),h('div',{class:'info'},h('div',{class:'small',style:'color:#ccc'},'ALBUM'),h('h1',{},a.title),
      meta(h('b',{},a.artist),a.year,`${a.tracks.length} songs`,fmtDur(dur),a.genre),
      h('div',{class:'btns'},h('button',{class:'btn play',onclick:()=>playQueue(a.tracks,0)},'▶  Play'),h('button',{class:'btn gray',onclick:()=>playQueue(a.tracks,0,true)},'⇄  Shuffle'),listBtn(a.key)))),
   h('div',{class:'mbody'},a.tracks.map((t,i)=>trackRow(t,t.track||i+1,()=>{playQueue(a.tracks,i);openAlbum(a)})))])}

/* ---------- video player ---------- */
const vid=$('#vid'),P={it:null,mode:'direct',off:0,dur:0,pending:0,sub:-1,lastSave:0,drag:false,idleT:0};
function playVideo(it,start){stopMusic(true);closeModal();P.it=it;P.dur=it.duration||0;P.mode=(it.direct||!S.transcode)?'direct':'tx';P.hevc=hevcPass(it);P.sub=-1;
  const p=prog(it.id);if(start==null)start=p&&!p.done&&p.pos>20?Math.max(0,p.pos-5):0;
  $('#ptitle').replaceChildren(it.show?h('span',{},h('b',{},it.show),h('small',{},`S${it.season}:E${it.episode} ${it.epTitle||''}`)):h('b',{},it.title));
  const subs=$('#psubs');subs.replaceChildren(h('option',{value:-1},'Subtitles: Off'),...(it.subs||[]).map((l,i)=>h('option',{value:i},l)));$('#psubw').classList.toggle('hidden',!(it.subs||[]).length);
  $('#pnext').classList.toggle('hidden',!nextEp(it));
  $('#player').classList.remove('hidden');document.body.classList.add('noscroll');
  if(document.fullscreenEnabled&&!document.fullscreenElement&&matchMedia('(pointer:coarse)').matches)$('#player').requestFullscreen().catch(()=>{});
  P.ads=[];P.adsState=null;P.allow=new Set();P.undo=null;P.adJump=null;clearTimeout(P.adsT);adsUI();loadAds(it.id);
  loadVid(start);poke()}
function nextEp(it){if(!it.show)return null;const s=S.showByKey['show:'+it.show];return s.eps[s.eps.indexOf(it)+1]||null}
function loadVid(t){spin(true);[...vid.querySelectorAll('track')].forEach(x=>x.remove());
  if(P.mode==='direct'){P.off=0;P.pending=t;vid.src='/media/'+P.it.id}else{P.off=t;P.pending=0;vid.src=`/transcode/${P.it.id}?t=${t.toFixed(1)}${P.hevc?'&hevc='+HEVC:''}`}
  $('#pmode').textContent=P.mode==='tx'&&!P.hevc?'Transcoding':'';
  if(P.sub>=0)addSub(P.sub);vid.play().catch(()=>{})}
function addSub(i){[...vid.querySelectorAll('track')].forEach(x=>x.remove());P.sub=i;if(i<0)return;
  const tr=h('track',{kind:'subtitles',default:true,src:`/subs/${P.it.id}/${i}?offset=${P.off.toFixed(2)}`,label:'sub'});vid.append(tr);
  tr.addEventListener('load',()=>{for(const t of vid.textTracks)t.mode='showing'});setTimeout(()=>{for(const t of vid.textTracks)t.mode='showing'},300)}
const cur=()=>P.off+(vid.currentTime||0);
function seekTo(t){t=Math.max(0,Math.min(t,(P.dur||1e9)-1));if(P.mode==='direct')vid.currentTime=t;else loadVid(t)}
function spin(on){$('#pspin').classList.toggle('hidden',!on)}
function msg(t,ms=2500){const m=$('#pmsg');m.textContent=t;m.classList.remove('hidden');clearTimeout(m._t);m._t=setTimeout(()=>m.classList.add('hidden'),ms)}
// HEVC passthrough failed (an error, or audio with no picture): convert it after all
function hevcFail(){const t=cur();P.hevc=false;msg('Converting this file for your browser…');loadVid(t)}
vid.addEventListener('loadedmetadata',()=>{if(P.mode==='tx'&&P.hevc&&!vid.videoWidth)return hevcFail();if(P.mode==='direct'){if(isFinite(vid.duration))P.dur=vid.duration;if(P.pending){vid.currentTime=P.pending;P.pending=0}}adsUI()});
vid.addEventListener('waiting',()=>spin(true));vid.addEventListener('playing',()=>{if(P.mode==='tx'&&P.hevc&&!vid.videoWidth)return hevcFail();spin(false);playIcon()});
vid.addEventListener('canplay',()=>spin(false));
vid.addEventListener('pause',()=>{playIcon();saveProg(true)});
vid.addEventListener('error',()=>{if(!P.it||!vid.getAttribute('src'))return;spin(false);
  if(P.mode==='direct'&&S.transcode){const t=cur();P.mode='tx';msg('Converting this file for your browser…');loadVid(t)}
  else if(P.mode==='tx'&&P.hevc)hevcFail()
  else msg(S.transcode?'Sorry, this file could not be played.':'Your browser cannot play this format and ffmpeg is not installed on the server.',6000)});
vid.addEventListener('timeupdate',()=>{const t=cur();adTick(t);if(!P.drag&&P.dur)seekShow(t);ptimeShow(t);$('#ptdur').textContent=fmtT(P.dur);if(Date.now()-P.lastSave>10000)saveProg()});
vid.addEventListener('ended',async()=>{if(P.mode==='tx'&&P.dur&&cur()<P.dur-30){return}await saveProg(true,true);const n=nextEp(P.it);if(n){msg('Next episode…');playVideo(n,0)}else closePlayer()});
async function saveProg(force,done){if(!P.it)return;P.lastSave=Date.now();const t=cur();if(!force&&t<5)return;const body={profile:S.profile.id,id:P.it.id,pos:t,dur:P.dur};if(done)body.done=true;
  S.progress[P.it.id]={pos:t,dur:P.dur,done:done||(P.dur&&t>=P.dur*.93),updated:Date.now()/1000};try{await post('/api/progress',body)}catch(e){}}
function closePlayer(){if(!P.it)return;saveProg(true);P.it=null;clearTimeout(P.adsT);$('#pskip').classList.add('hidden');vid.pause();vid.removeAttribute('src');vid.load();
  if(document.fullscreenElement)document.exitFullscreen().catch(()=>{});$('#player').classList.add('hidden');document.body.classList.remove('noscroll');render()}
const togglePlay=()=>vid.paused?vid.play():vid.pause();
/* Phones and tablets get the big-button layout (TOUCH); computers keep the bottom bar */
const TOUCH=matchMedia('(pointer:coarse)').matches,SKIP=TOUCH?30:10;
const VI={play:'<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>',pause:'<svg viewBox="0 0 24 24"><path d="M6 5h4v14H6zm8 0h4v14h-4z"/></svg>',
  rew:'<svg viewBox="0 0 24 24"><path d="M12 5V1L7 6l5 5V7a6 6 0 1 1-6 6H4a8 8 0 1 0 8-8z"/></svg>',
  fwd:'<svg viewBox="0 0 24 24"><path d="M12 5V1l5 5-5 5V7a6 6 0 1 0 6 6h2a8 8 0 1 1-8-8z"/></svg>',
  vol:'<svg viewBox="0 0 24 24"><path d="M3 9v6h4l5 5V4L7 9zm13.5 3A4.5 4.5 0 0 0 14 8v8a4.5 4.5 0 0 0 2.5-4zM14 3.2v2.1a7 7 0 0 1 0 13.4v2.1a9 9 0 0 0 0-17.6z"/></svg>',
  mute:'<svg viewBox="0 0 24 24"><path d="M16.5 12A4.5 4.5 0 0 0 14 8v2.2l2.5 2.5zM19 12a7 7 0 0 1-.6 2.8l1.5 1.5A9 9 0 0 0 14 3.2v2.1a7 7 0 0 1 5 6.7zM4.3 3 3 4.3 7.7 9H3v6h4l5 5v-6.7l4.3 4.2a7 7 0 0 1-2.3 1.2v2.1a9 9 0 0 0 3.7-1.8l2 2 1.3-1.3zM12 4 9.9 6.1 12 8.2z"/></svg>',
  fs:'<svg viewBox="0 0 24 24"><path d="M7 14H5v5h5v-2H7zm-2-4h2V7h3V5H5zm12 7h-3v2h5v-5h-2zM14 5v2h3v3h2V5z"/></svg>',
  next:'<svg viewBox="0 0 24 24"><path d="M16 6h2v12h-2zM6 18l8.5-6L6 6z"/></svg>',
  subs:'<svg viewBox="0 0 24 24"><path d="M20 4H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2zM4 12h4v2H4zm10 6H4v-2h10zm6 0h-4v-2h4zm0-4H10v-2h10z"/></svg>',
  back:'<svg viewBox="0 0 24 24"><path d="M20 11H7.8l5.6-5.6L12 4l-8 8 8 8 1.4-1.4L7.8 13H20z"/></svg>'};
function playIcon(){$('#pplay').innerHTML=vid.paused?VI.play:VI.pause;$('#pplay').dataset.l=vid.paused?'Play':'Pause'}
function muteIcon(){$('#pmute').innerHTML=vid.muted?VI.mute:VI.vol;$('#pmute').dataset.l=vid.muted?'Unmute':'Mute'}
// the slider's position (and, on phones, the red part of the bar and the time under it)
function ptimeShow(t){$('#ptime').textContent=TOUCH?fmtT(t)+' / '+fmtT(P.dur):fmtT(Math.max(0,P.dur-t))}
function seekShow(t){if(!P.dur)return;const f=Math.max(0,Math.min(1,t/P.dur));$('#pseek').value=f*1000;$('#pseek').style.setProperty('--pct',f*100+'%');$('#ptcur').textContent=fmtT(t)}
if(TOUCH){$('#player').classList.add('touch');$('#pcenter').append($('#prew'),$('#pplay'),$('#pfwd'));
  for(const b of ['#prew','#pplay','#pfwd'])$(b).classList.add('pcbtn');
  $('#prew').innerHTML=VI.rew+'<b>30</b>';$('#pfwd').innerHTML=VI.fwd+'<b>30</b>';$('#prew').title='Back 30 seconds';$('#pfwd').title='Forward 30 seconds';
  $('#pfs').innerHTML=VI.fs;$('#pnext').innerHTML=VI.next;muteIcon();playIcon();
  // tap the picture to show or hide the controls (pause with the big button)
  vid.onclick=()=>{if(!vid.paused){clearTimeout(P.idleT);$('#player').classList.add('idle')}};
  // the slider: tap anywhere to jump there, or drag
  const sw=document.querySelector('.seekwrap'),fr=e=>{const r=sw.getBoundingClientRect();return Math.max(0,Math.min(1,(e.clientX-r.left)/r.width))};
  let drag=false;const show=e=>{seekShow(fr(e)*P.dur);poke()};
  sw.addEventListener('pointerdown',e=>{if(!P.dur)return;drag=true;P.drag=true;try{sw.setPointerCapture(e.pointerId)}catch(_){}show(e);e.preventDefault()});
  sw.addEventListener('pointermove',e=>{if(drag)show(e)});
  sw.addEventListener('pointerup',e=>{if(!drag)return;drag=false;P.drag=false;seekTo(fr(e)*P.dur)});
  sw.addEventListener('pointercancel',()=>{drag=false;P.drag=false})}
else{vid.onclick=togglePlay;vid.ondblclick=()=>toggleFs();
  // computers: the big TV-style layout (title above the bar, a row of large buttons with names)
  $('#player').classList.add('desk');$('.pbot').prepend($('#ptitle'));$('#pback').innerHTML=VI.back;
  $('#prew').innerHTML='<span class="sk">'+VI.rew+'<b>10</b></span>';$('#pfwd').innerHTML='<span class="sk">'+VI.fwd+'<b>10</b></span>';
  $('#prew').dataset.l='Back 10s';$('#pfwd').dataset.l='Forward 10s';$('#pnext').dataset.l='Next Episode';$('#pfs').dataset.l='Full screen';
  $('#pfs').innerHTML=VI.fs;$('#pnext').innerHTML=VI.next;$('#psubbtn').innerHTML=VI.subs;muteIcon();playIcon()}
$('#pplay').onclick=togglePlay;
$('#prew').onclick=()=>seekTo(cur()-SKIP);$('#pfwd').onclick=()=>seekTo(cur()+SKIP);
$('#pback').onclick=closePlayer;$('#pnext').onclick=()=>{saveProg(true,true);const n=nextEp(P.it);if(n)playVideo(n,0)};
$('#pseek').oninput=e=>{P.drag=true;const f=e.target.value/1000;e.target.style.setProperty('--pct',f*100+'%');ptimeShow(f*P.dur)};
$('#pseek').onchange=e=>{P.drag=false;seekTo(e.target.value/1000*P.dur)};
$('#pvol').oninput=e=>{vid.volume=e.target.value;vid.muted=false;localStorage.hf_vol=vid.volume};
$('#pmute').onclick=()=>{vid.muted=!vid.muted;muteIcon()};
$('#psubs').onchange=e=>addSub(+e.target.value);
// Full screen: the whole player where the browser allows it (computers, Android, iPad). iPhones only
// let the video itself go full screen, in Apple's own player, so use that there
function toggleFs(){const d=document,el=$('#player');
  if(d.fullscreenElement||d.webkitFullscreenElement){const x=(d.exitFullscreen||d.webkitExitFullscreen).call(d);if(x&&x.catch)x.catch(()=>{});return}
  const req=el.requestFullscreen||el.webkitRequestFullscreen;
  if(req&&(d.fullscreenEnabled||d.webkitFullscreenEnabled)){const x=req.call(el);if(x&&x.catch)x.catch(()=>{});return}
  if(vid.webkitEnterFullscreen){try{vid.webkitEnterFullscreen()}catch(e){msg('Full screen works once the video has started')}return}
  msg('This browser does not allow full screen')}
$('#pfs').onclick=toggleFs;
// hide the controls after 3 s of no mouse movement (computers: not while the mouse rests on them)
function poke(){const p=$('#player');p.classList.remove('idle');clearTimeout(P.idleT);P.idleT=setTimeout(()=>{if(vid.paused)return;if(!TOUCH&&p.querySelector('.pov:hover'))return poke();p.classList.add('idle')},3000)}
$('#player').addEventListener('mousemove',poke);
// Phones: a tap while the controls are hidden only brings them back. Without this, the controls appear
// under the finger before the tap finishes, and the same tap would press whatever button is there
$('#player').addEventListener('touchstart',()=>{if(TOUCH&&$('#player').classList.contains('idle'))P.wakeTap=Date.now();poke()},{passive:true});
$('#player').addEventListener('click',e=>{if(P.wakeTap&&Date.now()-P.wakeTap<700){P.wakeTap=0;e.stopPropagation();e.preventDefault()}},true);
if(localStorage.hf_vol){vid.volume=+localStorage.hf_vol;$('#pvol').value=vid.volume}

/* ---------- ad breaks ---------- */
const autoSkip=()=>localStorage.hf_adskip==='1';
async function loadAds(id){try{const r=await api('/api/ads/'+id);if(!P.it||P.it.id!==id)return;P.ads=r.breaks||[];P.adsState=r.status;adsUI();
  clearTimeout(P.adsT);if(r.status==='running'||r.status==='queued')P.adsT=setTimeout(()=>loadAds(id),20000)}catch(e){}}
function adsUI(){const d=P.dur;$('#padbar').replaceChildren(...(d?P.ads.map(b=>{const i=h('i');i.style.left=b.start/d*100+'%';i.style.width=(b.end-b.start)/d*100+'%';return i}):[]));
  const t=P.ads.length?(autoSkip()?'Ads: auto-skip':'Ads: ask'):'';  // nothing at all for videos without ad breaks
  $('#pads').textContent=t;$('#pads').classList.toggle('hidden',!t)}
$('#pads').onclick=()=>{if(!P.it||!P.ads.length)return;localStorage.hf_adskip=autoSkip()?'0':'1';adsUI();
  msg(autoSkip()?'Ad breaks will be skipped automatically':'A Skip Ad button will show during ad breaks')};
// Called on every time update: auto-skip, or show the Skip Ad / Undo buttons
function adTick(t){const box=$('#pskip');if(!P.ads.length){box.classList.add('hidden');return}
  const b=P.ads.find(x=>t>=x.start-.3&&t<x.end-1&&!P.allow.has(x));
  const jumping=b&&P.adJump&&P.adJump.b===b&&Date.now()-P.adJump.at<5000;
  if(b&&autoSkip()&&!jumping){P.adJump={b,at:Date.now()};P.undo={b,until:Date.now()+8000};seekTo(b.end);msg(`Skipped an ad break (${fmtT(b.end-b.start)})`,3000)}
  const undo=P.undo&&Date.now()<P.undo.until?P.undo.b:null;
  const mode=b&&!autoSkip()?'skip':undo?'undo':null;P.box={mode,b:mode==='skip'?b:undo};
  box.classList.toggle('hidden',!mode);if(mode)$('#pskipbtn').textContent=mode==='skip'?'Skip Ad ▸▸':'Undo'}
$('#pskipbtn').onclick=()=>{const x=P.box||{};if(!x.b)return;
  if(x.mode==='skip'){P.adJump={b:x.b,at:Date.now()};seekTo(x.b.end)}else{P.allow.add(x.b);P.undo=null;seekTo(x.b.start)}
  $('#pskip').classList.add('hidden')};
$('#pnotad').onclick=async()=>{const x=P.box||{};if(!x.b||!P.it)return;const b=x.b;P.undo=null;$('#pskip').classList.add('hidden');
  P.ads=P.ads.filter(y=>y!==b);adsUI();if(x.mode==='undo')seekTo(b.start);
  try{const r=await post('/api/ads/'+P.it.id+'/not_ad',{start:b.start,end:b.end});if(r&&r.breaks)P.ads=r.breaks;adsUI()}catch(e){}
  msg("Got it. That part won't be skipped again.")};

/* ---------- music player ---------- */
const aud=$('#aud'),M={q:[],i:0,mode:'direct',off:0,shuf:false,rep:false,orig:[]};
function playQueue(list,i=0,shuffle=false){if(P.it)closePlayer();M.orig=[...list];M.q=[...list];M.shuf=shuffle;
  if(shuffle){const first=M.q.splice(i,1)[0];for(let k=M.q.length-1;k>0;k--){const j=Math.floor(Math.random()*(k+1));[M.q[k],M.q[j]]=[M.q[j],M.q[k]]}M.q.unshift(first);i=0}
  $('#mshuf').classList.toggle('on',M.shuf);M.i=i;loadTrack(0)}
function loadTrack(t){const tr=M.q[M.i];if(!tr)return;M.mode=(tr.direct||!S.transcode)?'direct':'tx';M.off=M.mode==='tx'?t:0;
  aud.src=M.mode==='direct'?'/media/'+tr.id:`/transcode/${tr.id}?t=${t}`;if(M.mode==='direct'&&t)aud.addEventListener('loadedmetadata',()=>aud.currentTime=t,{once:true});
  aud.play().catch(()=>{});$('#mbar').classList.remove('hidden');document.body.classList.add('mplay');
  const al=S.albumByKey['album:'+tr.albumArtist+'\u0001'+tr.album];
  $('#mnow').replaceChildren(art(al?al.cover:tr.id,tr.album,true),h('div',{class:'tt'},h('b',{},tr.title),h('span',{},tr.artist+' — '+tr.album)));
  $('#mnow').onclick=()=>matchMedia('(pointer:coarse)').matches?mfOpen():al&&openAlbum(al);  // phones: the big controls
  if(MF.open)mfInfo();
  if('mediaSession'in navigator){navigator.mediaSession.metadata=new MediaMetadata({title:tr.title,artist:tr.artist,album:tr.album,artwork:[{src:'/thumb/'+(al?al.cover:tr.id),sizes:'500x500',type:'image/jpeg'}]})}
  document.querySelectorAll('.trk').forEach(()=>{});}
const mcur=()=>M.off+(aud.currentTime||0),mdur=()=>(M.q[M.i]||{}).duration||(isFinite(aud.duration)?aud.duration:0);
function mnext(){if(M.i<M.q.length-1){M.i++;loadTrack(0)}else if(M.rep){M.i=0;loadTrack(0)}else{aud.pause()}}
function mprev(){if(mcur()>3){mseek(0);return}if(M.i>0){M.i--;loadTrack(0)}}
function mseek(t){if(M.mode==='direct')aud.currentTime=t;else loadTrack(t)}
function stopMusic(pauseOnly){aud.pause();if(!pauseOnly){mfClose();aud.removeAttribute('src');aud.load();$('#mbar').classList.add('hidden');document.body.classList.remove('mplay')}}
aud.addEventListener('ended',mnext);
aud.addEventListener('error',()=>{if(!aud.getAttribute('src'))return;if(M.mode==='direct'&&S.transcode){M.mode='tx';const tr=M.q[M.i];aud.src=`/transcode/${tr.id}?t=0`;M.off=0;aud.play().catch(()=>{})}else mnext()});
aud.addEventListener('play',()=>{mPlayIcon();mfPlayIcon()});aud.addEventListener('pause',()=>{mPlayIcon();mfPlayIcon()});
aud.addEventListener('timeupdate',()=>{const d=mdur();$('#mcur').textContent=fmtT(mcur());$('#mdur').textContent=fmtT(d);if(d&&!M.drag)$('#mseek').value=mcur()/d*1000;
  if(TOUCH&&d&&!M.sdrag)mpShow(mcur());
  if(MF.open&&!MF.drag&&!MF.skip)mfTime();
  // the lock screen's (and the car's) progress bar
  const ms=navigator.mediaSession;if(ms&&ms.setPositionState&&d&&Date.now()-(M.posAt||0)>1000){M.posAt=Date.now();try{ms.setPositionState({duration:d,position:Math.min(mcur(),d),playbackRate:1})}catch(e){}}});
$('#mplay').onclick=()=>aud.paused?aud.play():aud.pause();$('#mnext').onclick=mnext;$('#mprev').onclick=mprev;
$('#mseek').oninput=()=>M.drag=true;$('#mseek').onchange=e=>{M.drag=false;mseek(e.target.value/1000*mdur())};
$('#mvol').oninput=e=>{aud.volume=e.target.value;localStorage.hf_mvol=aud.volume};if(localStorage.hf_mvol){aud.volume=+localStorage.hf_mvol;$('#mvol').value=aud.volume}
$('#mclose').onclick=()=>stopMusic(false);
$('#mrep').onclick=()=>{M.rep=!M.rep;$('#mrep').classList.toggle('on',M.rep)};
$('#mshuf').onclick=()=>{const curT=M.q[M.i];M.shuf=!M.shuf;$('#mshuf').classList.toggle('on',M.shuf);
  if(M.shuf){const rest=M.q.filter(t=>t!==curT);for(let k=rest.length-1;k>0;k--){const j=Math.floor(Math.random()*(k+1));[rest[k],rest[j]]=[rest[j],rest[k]]}M.q=[curT,...rest];M.i=0}
  else{M.q=[...M.orig];M.i=Math.max(0,M.q.indexOf(curT))}};
if('mediaSession'in navigator){const ms=navigator.mediaSession;ms.setActionHandler('play',()=>aud.play());ms.setActionHandler('pause',()=>aud.pause());ms.setActionHandler('nexttrack',mnext);ms.setActionHandler('previoustrack',mprev);
  try{ms.setActionHandler('seekto',d=>{if(d.seekTime!=null)mseek(d.seekTime)})}catch(e){}}  // drag the lock screen's progress bar

/* ---------- Now Playing: big controls for phones and the car ---------- */
const ICON={
  play:'<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>',
  pause:'<svg viewBox="0 0 24 24"><path d="M6 5h4v14H6zm8 0h4v14h-4z"/></svg>',
  prev:'<svg viewBox="0 0 24 24"><path d="M6 6h2v12H6zm3.5 6 8.5 6V6z"/></svg>',
  next:'<svg viewBox="0 0 24 24"><path d="M16 6h2v12h-2zM6 18l8.5-6L6 6z"/></svg>',
  back:'<svg viewBox="0 0 24 24"><path d="M12 5V1L7 6l5 5V7a6 6 0 1 1-6 6H4a8 8 0 1 0 8-8z"/></svg>',
  fwd:'<svg viewBox="0 0 24 24"><path d="M12 5V1l5 5-5 5V7a6 6 0 1 0 6 6h2a8 8 0 1 1-8-8z"/></svg>',
  down:'<svg viewBox="0 0 24 24"><path d="M7.4 8.6 12 13.2l4.6-4.6L18 10l-6 6-6-6z"/></svg>',
  up:'<svg viewBox="0 0 24 24"><path d="M7.4 15.4 12 10.8l4.6 4.6L18 14l-6-6-6 6z"/></svg>',
  shuf:'<svg viewBox="0 0 24 24"><path d="M10.6 9.2 5.4 4 4 5.4l5.2 5.2zM14.5 4l2 2L4 18.6 5.4 20 18 7.5l2 2V4zm.3 9.4-1.4 1.4 3.1 3.1L14.5 20H20v-5.5l-2 2z"/></svg>',
  rep:'<svg viewBox="0 0 24 24"><path d="M7 7h10v3l4-4-4-4v3H5v6h2zm10 10H7v-3l-4 4 4 4v-3h12v-6h-2z"/></svg>'};
const MF={open:false,drag:false,lock:null};
[['#mfclose','down'],['#mfshuf','shuf'],['#mfrep','rep']].forEach(([s,k])=>$(s).innerHTML=ICON[k]);
$('#mfprev').innerHTML=ICON.prev+'Previous';$('#mfnext').innerHTML='Next'+ICON.next;
$('#mfback').innerHTML=ICON.back+'<b>15</b>';$('#mffwd').innerHTML=ICON.fwd+'<b>15</b>';
function mfPlayIcon(){$('#mfplay').innerHTML=aud.paused?ICON.play:ICON.pause}
function mfAlbum(tr){return S.albumByKey['album:'+tr.albumArtist+'\u0001'+tr.album]}
function mfInfo(){const tr=M.q[M.i];if(!tr)return;const al=mfAlbum(tr);
  $('#mfart').replaceChildren(art(al?al.cover:tr.id,tr.album,true));$('#mftitle').textContent=tr.title;$('#mfsub').textContent=tr.artist+' — '+tr.album;
  $('#mfshuf').classList.toggle('on',M.shuf);$('#mfrep').classList.toggle('on',M.rep);mfPlayIcon();mfTime()}
function mfTime(t){const d=mdur(),c=t==null?mcur():t,f=d?Math.max(0,Math.min(1,c/d)):0;
  $('#mffill').style.width=f*100+'%';$('#mfknob').style.left=f*100+'%';$('#mfcur').textContent=fmtT(c);$('#mfdur').textContent=fmtT(d);$('#mfseek').setAttribute('aria-valuenow',Math.round(c))}
// keep the screen on while it's open (a phone in a car mount)
async function mfWake(on){try{if(on&&!MF.lock&&'wakeLock'in navigator){MF.lock=await navigator.wakeLock.request('screen');MF.lock.addEventListener('release',()=>{MF.lock=null})}
  else if(!on&&MF.lock){const l=MF.lock;MF.lock=null;await l.release()}}catch(e){}}
document.addEventListener('visibilitychange',()=>{if(MF.open&&document.visibilityState==='visible')mfWake(true)});
function mfOpen(){if(!M.q[M.i])return;MF.open=true;mfInfo();$('#mfull').classList.remove('hidden');document.body.classList.add('noscroll');mfWake(true)}
function mfClose(){if(!MF.open)return;MF.open=false;$('#mfull').classList.add('hidden');if(!P.it)document.body.classList.remove('noscroll');mfWake(false)}
$('#mexpand').onclick=mfOpen;$('#mfclose').onclick=mfClose;
$('#mfsub').onclick=()=>{const tr=M.q[M.i],al=tr&&mfAlbum(tr);if(al){mfClose();openAlbum(al)}};
$('#mfplay').onclick=()=>aud.paused?aud.play():aud.pause();
$('#mfprev').onclick=mprev;$('#mfnext').onclick=mnext;
// Skip back / forward: each tap adds 15 s, so a double-tap is 30 s. The jump happens a moment after the
// last tap, as one seek; meanwhile the button turns red with the total and the slider shows where it lands
function mfSkipButton(sel,dir){const b=$(sel),lab=b.querySelector('b');let n=0,from=0,timer=0;
  const target=()=>{const d=mdur(),t=from+dir*15*n;return Math.max(0,d?Math.min(t,d-1):t)};
  b.onclick=()=>{if(!n)from=mcur();n++;MF.skip=true;b.classList.add('pend');lab.textContent=(dir<0?'−':'+')+15*n;mfTime(target());
    clearTimeout(timer);timer=setTimeout(()=>{const t=target();n=0;MF.skip=false;b.classList.remove('pend');lab.textContent='15';mseek(t)},450)}}
mfSkipButton('#mfback',-1);mfSkipButton('#mffwd',1);

/* Phones and tablets: the music bar in two rows with big buttons (computers keep the one-row bar) */
function mPlayIcon(){$('#mplay').innerHTML=aud.paused?(TOUCH?ICON.play:'&#9654;'):(TOUCH?ICON.pause:'&#10074;&#10074;')}
if(TOUCH){const mb=$('#mbar');mb.classList.add('touch');document.body.classList.add('touchbar');
  mb.insertBefore($('#mexpand'),mb.querySelector('.mctr'));$('#mexpand').innerHTML=ICON.up+'Big player';
  $('#mprev').innerHTML=ICON.prev;$('#mnext').innerHTML=ICON.next;mPlayIcon();
  // the slider along the top edge: tap anywhere to jump there, or drag
  const pr=$('#mprog'),fr=e=>{const r=pr.querySelector('.mptrk').getBoundingClientRect();return Math.max(0,Math.min(1,(e.clientX-r.left)/r.width))};
  const end=()=>{M.sdrag=false;pr.classList.remove('drag')};
  pr.addEventListener('pointerdown',e=>{if(!mdur())return;M.sdrag=true;pr.classList.add('drag');try{pr.setPointerCapture(e.pointerId)}catch(_){}mpShow(fr(e)*mdur());e.preventDefault()});
  pr.addEventListener('pointermove',e=>{if(M.sdrag)mpShow(fr(e)*mdur())});
  pr.addEventListener('pointerup',e=>{if(!M.sdrag)return;end();const d=mdur();if(d)mseek(Math.min(fr(e)*d,d-0.5))});
  pr.addEventListener('pointercancel',()=>{end();mpShow(mcur())})}
function mpShow(t){const d=mdur(),f=d?Math.max(0,Math.min(1,t/d)):0;$('#mprog i').style.width=f*100+'%';$('#mprog em').style.left=f*100+'%';
  $('#mpcur').textContent=fmtT(t);$('#mpdur').textContent=fmtT(d);$('#mprog').setAttribute('aria-valuenow',Math.round(t))}
$('#mfshuf').onclick=()=>{$('#mshuf').click();mfInfo()};$('#mfrep').onclick=()=>{$('#mrep').click();mfInfo()};
// The seek bar: tap anywhere to jump there, or drag (the knob grows under your finger)
{const sk=$('#mfseek'),frac=e=>{const r=sk.querySelector('.mftrack').getBoundingClientRect();return Math.max(0,Math.min(1,(e.clientX-r.left)/r.width))};
 const end=()=>{MF.drag=false;sk.classList.remove('drag')};
 sk.addEventListener('pointerdown',e=>{if(!mdur())return;MF.drag=true;sk.classList.add('drag');try{sk.setPointerCapture(e.pointerId)}catch(_){}mfTime(frac(e)*mdur())});
 sk.addEventListener('pointermove',e=>{if(MF.drag)mfTime(frac(e)*mdur())});
 sk.addEventListener('pointerup',e=>{if(!MF.drag)return;end();const d=mdur();if(d)mseek(Math.min(frac(e)*d,d-0.5))});
 sk.addEventListener('pointercancel',()=>{end();mfTime()})}

/* ---------- keyboard ---------- */
document.addEventListener('keydown',e=>{if(e.target.matches('input[type=search],input[type=text]'))return;
  if(P.it){const k=e.key;if(k===' '||k==='k'){e.preventDefault();togglePlay()}else if(k==='ArrowLeft'){seekTo(cur()-10)}else if(k==='ArrowRight'){seekTo(cur()+10)}
    else if(k==='ArrowUp'){e.preventDefault();vid.volume=Math.min(1,vid.volume+.1)}else if(k==='ArrowDown'){e.preventDefault();vid.volume=Math.max(0,vid.volume-.1)}
    else if(k==='f')toggleFs();else if(k==='m')$('#pmute').click();else if(k==='n')$('#pnext').click();else if(k==='s'&&!$('#pskip').classList.contains('hidden'))$('#pskipbtn').click();else if(k==='Escape'&&!document.fullscreenElement)closePlayer();poke();return}
  if(e.key==='Escape'&&MF.open)mfClose();
  else if(e.key==='Escape'&&!$('#modal').classList.contains('hidden'))closeModal();
  else if(e.key===' '&&!$('#mbar').classList.contains('hidden')&&e.target===document.body){e.preventDefault();$('#mplay').click()}
  else if(e.key==='/'){e.preventDefault();$('#search').focus()}});

/* ---------- profiles ---------- */
async function showGate(manage){const d=await api('/api/profiles');S.profiles=d.profiles;S.auth=d.auth;const g=$('#gate');
  const tiles=S.profiles.map(p=>h('div',{class:'prof',onclick:()=>!manage&&choose(p)},h('div',{class:'sq',style:'background:'+p.color},p.name[0].toUpperCase()),h('span',{},p.name),
     manage&&S.profiles.length>1?h('button',{class:'del',title:'Delete profile',onclick:async e=>{e.stopPropagation();if(confirm(`Delete profile "${p.name}" and its watch history?`)){await post('/api/profiles',{delete:p.id});if(S.profile&&S.profile.id===p.id){S.profile=null;localStorage.removeItem('hf_profile')}showGate(true)}}},'✕'):null));
  if(S.profiles.length<8)tiles.push(h('div',{class:'prof',onclick:async()=>{const n=prompt('Profile name');if(n&&n.trim()){await post('/api/profiles',{name:n.trim()});showGate(manage)}}},h('div',{class:'sq add'},'+'),h('span',{},'Add Profile')));
  g.replaceChildren(h('div',{class:'logo',style:'position:absolute;top:20px;left:4%'},'HOMEFLIX'),h('h1',{},manage?'Manage Profiles:':'Who’s watching?'),h('div',{class:'profs'},tiles),
    h('button',{class:'gbtn',onclick:()=>showGate(!manage)},manage?'DONE':'MANAGE PROFILES'));g.classList.remove('hidden')}
async function choose(p){S.profile=p;localStorage.hf_profile=p.id;$('#gate').classList.add('hidden');const av=$('#avatar');av.textContent=p.name[0].toUpperCase();av.style.background=p.color;av.title=p.name+' — switch profile';
  await loadState();S.version=-1;S.view='home';await loadLibrary()}

/* ---------- wiring ---------- */
document.querySelectorAll('[data-go]').forEach(a=>a.addEventListener('click',()=>go(a.dataset.go)));
$('#menuBtn').onclick=()=>$('#nav').classList.toggle('menu');
$('#avatar').onclick=()=>{if(P.it)closePlayer();showGate(false)};
let st;$('#search').addEventListener('input',e=>{clearTimeout(st);st=setTimeout(()=>{S.q=e.target.value;if(S.q.trim()){S.view='search';render()}else go('home')},200)});
$('#rescan').onclick=async()=>{await post('/api/rescan',{});$('#scan').classList.remove('hidden');setTimeout(loadLibrary,800)};
addEventListener('scroll',()=>$('#nav').classList.toggle('solid',scrollY>40),{passive:true});
addEventListener('beforeunload',()=>{if(P.it)navigator.sendBeacon&&navigator.sendBeacon('/api/progress',new Blob([JSON.stringify({profile:S.profile.id,id:P.it.id,pos:cur(),dur:P.dur})],{type:'application/json'}))});
(async()=>{const d=await api('/api/profiles');S.profiles=d.profiles;const saved=S.profiles.find(p=>p.id===localStorage.hf_profile);
  if(saved)choose(saved);else if(S.profiles.length===1&&S.profiles[0].name==='Me'&&!localStorage.hf_seen){localStorage.hf_seen=1;showGate(false)}else showGate(false)})();
</script>
</body></html>"""


# ----------------------------------------------------------------------------
def fill_version():
    global APP_HTML, LOGIN_HTML
    APP_HTML = APP_HTML.replace("__VERSION__", VERSION)
    LOGIN_HTML = LOGIN_HTML.replace("__VERSION__", VERSION)


def setup_output():
    """Make logging safe everywhere: pythonw (no console) and non-UTF-8 consoles."""
    if sys.stdout is None or sys.stderr is None:  # pythonw.exe, e.g. the Windows background task
        XDG_DATA.mkdir(parents=True, exist_ok=True)
        f = open(XDG_DATA / "homeflix.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or f
        sys.stderr = sys.stderr or f
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def main():
    global VENC
    setup_output()
    fill_version()
    load_settings(CONF_FILE)
    ap = argparse.ArgumentParser(description="Homeflix - Netflix-style media server for your LAN")
    ap.add_argument("--videos", action="append", metavar="DIR",
                    help="video folder (repeatable). Default: ~/Downloads/movies")
    ap.add_argument("--music", action="append", metavar="DIR", help="music folder (repeatable). Default: ~/Music")
    ap.add_argument("--host", default=os.environ.get("HOMEFLIX_HOST", "0.0.0.0"),
                    help="bind address (default 0.0.0.0 = whole LAN; use 127.0.0.1 for this PC only)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("HOMEFLIX_PORT", 8484)))
    ap.add_argument("--password", default=os.environ.get("HOMEFLIX_PASSWORD"),
                    help="require this password (or set HOMEFLIX_PASSWORD)")
    ap.add_argument("--rescan-minutes", type=float, default=15, help="auto-rescan interval, 0 to disable")
    ap.add_argument("--quality", choices=list(QUALITY), default=os.environ.get("HOMEFLIX_QUALITY", "high"),
                    help="quality for files that must be converted: balanced (lighter on CPU and Wi-Fi), "
                         "high (default) or max (or set HOMEFLIX_QUALITY)")
    ap.add_argument("--no-thumbs", action="store_true", help="don't generate thumbnails")
    ap.add_argument("--ads", choices=ADS_MODES, default=os.environ.get("HOMEFLIX_ADS", "off"),
                    help="commercial skipping: off (default), auto (find ad breaks in TV recordings: "
                         ".ts, .m2ts, .mpg) or all (every video) (or set HOMEFLIX_ADS)")
    ap.add_argument("--quiet", action="store_true", help="don't log every HTTP request")
    ap.add_argument("--version", action="version", version=f"Homeflix {VERSION}")
    a = ap.parse_args()

    videos = a.videos or [os.environ.get("HOMEFLIX_VIDEOS", "~/Downloads/movies")]
    music = a.music or [os.environ.get("HOMEFLIX_MUSIC", "~/Music")]
    roots = []
    for r in videos + music:
        for part in str(r).split(os.pathsep):  # ":" on Linux/macOS, ";" on Windows
            if part and part not in roots:
                roots.append(part)

    XDG_DATA.mkdir(parents=True, exist_ok=True)
    secret_file = XDG_DATA / "secret.key"
    if not secret_file.exists():
        secret_file.write_bytes(secrets.token_bytes(32))
        os.chmod(secret_file, 0o600)

    VENC = pick_encoder()
    lib = Library(roots, make_thumbs=not a.no_thumbs)
    Handler.lib = lib
    Handler.state = State(XDG_DATA / "state.json")
    Handler.password = a.password or None
    Handler.secret = secret_file.read_bytes()
    Handler.quiet = a.quiet
    Handler.quality = a.quality
    Handler.ads = AdFinder(lib, a.ads)
    Handler.ads.busy = lambda: Handler.converting > 0

    log(f"Homeflix {VERSION} starting (Python {sys.version.split()[0]}, {sys.platform})")
    if not FFMPEG:
        log("WARNING: ffmpeg/ffprobe not found - no thumbnails, durations or transcoding. Install: sudo apt install ffmpeg")
    elif not VENC:
        log("WARNING: ffmpeg has no H.264 encoder (libx264); only H.264 sources can be remuxed.")
    if a.host not in ("127.0.0.1", "localhost", "::1") and not Handler.password:
        log("NOTE: serving to your whole network WITHOUT a password. Use --password to require one.")

    threading.Thread(target=lib.scan, daemon=True).start()

    def rescan_loop():
        while True:
            time.sleep(max(60, a.rescan_minutes * 60))
            lib.scan()
    if a.rescan_minutes > 0:
        threading.Thread(target=rescan_loop, daemon=True).start()
    threading.Thread(target=Handler.ads.run, daemon=True).start()

    try:
        httpd = Server((a.host, a.port), Handler)
    except OSError as e:
        sys.exit(f"Could not listen on {a.host}:{a.port}: {e}")
    log(f"Homeflix {VERSION} serving {', '.join(str(r) for r in lib.roots)}")
    if FFMPEG and VENC:
        log(f"  Conversion quality: {a.quality} (change with --quality or HOMEFLIX_QUALITY)")
        log(f"  Commercial skipping: {a.ads}" + (" (turn on with --ads auto or HOMEFLIX_ADS=auto)" if a.ads == "off"
                                                  else " (change with --ads or HOMEFLIX_ADS)"))
    log(f"  This PC:      http://localhost:{a.port}")
    if a.host in ("0.0.0.0", "::", ""):
        for ip in lan_ips():
            log(f"  Your network: http://{ip}:{a.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("bye")


if __name__ == "__main__":
    main()
