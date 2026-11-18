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

Python 3.8+ standard library only. ffmpeg/ffprobe are optional but strongly
recommended (thumbnails, durations, tags, transcoding).
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

VERSION = "1.2.0"

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
    def thumb(self, it):
        if it.get("_img") and os.path.isfile(it["_img"]):
            return it["_img"]
        out = self.thumb_dir / f"{it['id']}.jpg"
        miss = self.thumb_dir / f"{it['id']}.none"
        if out.exists():
            return str(out)
        if miss.exists() or not self.make_thumbs:
            return None
        lk = self.thumb_locks.setdefault(it["id"], threading.Lock())
        with lk:
            if out.exists():
                return str(out)
            with self.thumb_sem:
                ok = self._gen_thumb(it, out)
        if not ok:
            miss.touch()
            return None
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
            if not (self.thumb_dir / f"{it['id']}.jpg").exists():
                self.thumb(it)
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
            if not cache.exists():
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
                cmd += ["-c:a", "copy"]
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
.bad{color:#e87c03;font-size:14px;margin-bottom:12px;display:none}</style></head><body>
<form method="post" action="/login"><h1>HOMEFLIX</h1><div class="bad" id="bad">Incorrect password.</div>
<input type="password" name="password" placeholder="Password" autofocus required><button>Sign In</button></form>
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
#pseek{flex:1}
.pctl{display:flex;align-items:center;gap:18px;margin-top:8px}
.pctl button{background:none;border:0;font-size:24px;min-width:32px}
.pctl .sp{flex:1}
.pctl select{background:#000a;color:#fff;border:1px solid #666;padding:4px}
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
 #mbar{grid-template-columns:1fr auto;height:72px}
 .mright,.mseek{display:none}
 .ep .art{width:100px;flex-basis:100px}
 .mhead.album{flex-direction:column;align-items:flex-start}.mhead.album .art{width:150px;flex-basis:auto}
}
</style></head>
<body>
<div id="gate" class="hidden"></div>
<header id="nav">
  <button id="menuBtn" class="iconbtn" style="display:none" aria-label="Menu">&#9776;</button>
  <div class="logo" data-go="home">HOMEFLIX</div>
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
  <div class="pov ptop"><button id="pback" title="Back (Esc)">&#8592;</button><div id="ptitle"></div></div>
  <div class="pov pbot">
    <div class="seekrow"><input type="range" id="pseek" min="0" max="1000" value="0" step="1"><span id="ptime">0:00</span></div>
    <div class="pctl">
      <button id="pplay" title="Play/Pause (Space)">&#10074;&#10074;</button>
      <button id="prew" title="Back 10s (&larr;)">&#8634;</button>
      <button id="pfwd" title="Forward 10s (&rarr;)">&#8635;</button>
      <button id="pmute" title="Mute (M)">&#128266;</button>
      <input type="range" id="pvol" min="0" max="1" step="0.05" value="1" style="width:90px">
      <span class="sp"></span>
      <span id="pmode" class="small"></span>
      <select id="psubs" title="Subtitles"></select>
      <button id="pnext" title="Next episode (N)" class="hidden">&#9197;</button>
      <button id="pfs" title="Full screen (F)">&#9974;</button>
    </div>
  </div>
</div>

<div id="mbar" class="hidden">
  <div class="now" id="mnow"></div>
  <div class="mctr">
    <div class="mbtns">
      <button id="mshuf" title="Shuffle">&#8644;</button>
      <button id="mprev" title="Previous">&#9198;</button>
      <button id="mplay" class="pp" title="Play/Pause">&#9654;</button>
      <button id="mnext" title="Next">&#9197;</button>
      <button id="mrep" title="Repeat">&#8635;</button>
    </div>
    <div class="mseek"><span id="mcur">0:00</span><input type="range" id="mseek" min="0" max="1000" value="0"><span id="mdur">0:00</span></div>
  </div>
  <div class="mright"><span>&#128264;</span><input type="range" id="mvol" min="0" max="1" step="0.05" value="1"><button id="mclose" title="Close">&#10005;</button></div>
  <audio id="aud" preload="auto"></audio>
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
  if(id){const i=h('img',{loading:'lazy',alt:'',src:'/thumb/'+id});i.onload=()=>w.classList.add('loaded');i.onerror=()=>i.remove();w.append(i)}return w}

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
  const subs=$('#psubs');subs.replaceChildren(h('option',{value:-1},'Subtitles: Off'),...(it.subs||[]).map((l,i)=>h('option',{value:i},l)));subs.classList.toggle('hidden',!(it.subs||[]).length);
  $('#pnext').classList.toggle('hidden',!nextEp(it));
  $('#player').classList.remove('hidden');document.body.classList.add('noscroll');
  if(document.fullscreenEnabled&&!document.fullscreenElement&&matchMedia('(pointer:coarse)').matches)$('#player').requestFullscreen().catch(()=>{});
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
vid.addEventListener('loadedmetadata',()=>{if(P.mode==='tx'&&P.hevc&&!vid.videoWidth)return hevcFail();if(P.mode==='direct'){if(isFinite(vid.duration))P.dur=vid.duration;if(P.pending){vid.currentTime=P.pending;P.pending=0}}});
vid.addEventListener('waiting',()=>spin(true));vid.addEventListener('playing',()=>{if(P.mode==='tx'&&P.hevc&&!vid.videoWidth)return hevcFail();spin(false);$('#pplay').innerHTML='&#10074;&#10074;'});
vid.addEventListener('canplay',()=>spin(false));
vid.addEventListener('pause',()=>{$('#pplay').innerHTML='&#9654;';saveProg(true)});
vid.addEventListener('error',()=>{if(!P.it||!vid.getAttribute('src'))return;spin(false);
  if(P.mode==='direct'&&S.transcode){const t=cur();P.mode='tx';msg('Converting this file for your browser…');loadVid(t)}
  else if(P.mode==='tx'&&P.hevc)hevcFail()
  else msg(S.transcode?'Sorry, this file could not be played.':'Your browser cannot play this format and ffmpeg is not installed on the server.',6000)});
vid.addEventListener('timeupdate',()=>{const t=cur();if(!P.drag&&P.dur)$('#pseek').value=t/P.dur*1000;$('#ptime').textContent=fmtT(t)+' / '+fmtT(P.dur);if(Date.now()-P.lastSave>10000)saveProg()});
vid.addEventListener('ended',async()=>{if(P.mode==='tx'&&P.dur&&cur()<P.dur-30){return}await saveProg(true,true);const n=nextEp(P.it);if(n){msg('Next episode…');playVideo(n,0)}else closePlayer()});
async function saveProg(force,done){if(!P.it)return;P.lastSave=Date.now();const t=cur();if(!force&&t<5)return;const body={profile:S.profile.id,id:P.it.id,pos:t,dur:P.dur};if(done)body.done=true;
  S.progress[P.it.id]={pos:t,dur:P.dur,done:done||(P.dur&&t>=P.dur*.93),updated:Date.now()/1000};try{await post('/api/progress',body)}catch(e){}}
function closePlayer(){if(!P.it)return;saveProg(true);P.it=null;vid.pause();vid.removeAttribute('src');vid.load();
  if(document.fullscreenElement)document.exitFullscreen().catch(()=>{});$('#player').classList.add('hidden');document.body.classList.remove('noscroll');render()}
const togglePlay=()=>vid.paused?vid.play():vid.pause();
$('#pplay').onclick=togglePlay;vid.onclick=togglePlay;vid.ondblclick=()=>toggleFs();
$('#prew').onclick=()=>seekTo(cur()-10);$('#pfwd').onclick=()=>seekTo(cur()+10);
$('#pback').onclick=closePlayer;$('#pnext').onclick=()=>{saveProg(true,true);const n=nextEp(P.it);if(n)playVideo(n,0)};
$('#pseek').oninput=e=>{P.drag=true;$('#ptime').textContent=fmtT(e.target.value/1000*P.dur)+' / '+fmtT(P.dur)};
$('#pseek').onchange=e=>{P.drag=false;seekTo(e.target.value/1000*P.dur)};
$('#pvol').oninput=e=>{vid.volume=e.target.value;vid.muted=false;localStorage.hf_vol=vid.volume};
$('#pmute').onclick=()=>{vid.muted=!vid.muted;$('#pmute').innerHTML=vid.muted?'&#128263;':'&#128266;'};
$('#psubs').onchange=e=>addSub(+e.target.value);
function toggleFs(){if(document.fullscreenElement)document.exitFullscreen();else $('#player').requestFullscreen().catch(()=>{})}
$('#pfs').onclick=toggleFs;
function poke(){const p=$('#player');p.classList.remove('idle');clearTimeout(P.idleT);P.idleT=setTimeout(()=>{if(!vid.paused)p.classList.add('idle')},3000)}
$('#player').addEventListener('mousemove',poke);$('#player').addEventListener('touchstart',poke,{passive:true});
if(localStorage.hf_vol){vid.volume=+localStorage.hf_vol;$('#pvol').value=vid.volume}

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
  $('#mnow').onclick=()=>al&&openAlbum(al);
  if('mediaSession'in navigator){navigator.mediaSession.metadata=new MediaMetadata({title:tr.title,artist:tr.artist,album:tr.album,artwork:[{src:'/thumb/'+(al?al.cover:tr.id),sizes:'500x500',type:'image/jpeg'}]})}
  document.querySelectorAll('.trk').forEach(()=>{});}
const mcur=()=>M.off+(aud.currentTime||0),mdur=()=>(M.q[M.i]||{}).duration||(isFinite(aud.duration)?aud.duration:0);
function mnext(){if(M.i<M.q.length-1){M.i++;loadTrack(0)}else if(M.rep){M.i=0;loadTrack(0)}else{aud.pause()}}
function mprev(){if(mcur()>3){mseek(0);return}if(M.i>0){M.i--;loadTrack(0)}}
function mseek(t){if(M.mode==='direct')aud.currentTime=t;else loadTrack(t)}
function stopMusic(pauseOnly){aud.pause();if(!pauseOnly){aud.removeAttribute('src');aud.load();$('#mbar').classList.add('hidden');document.body.classList.remove('mplay')}}
aud.addEventListener('ended',mnext);
aud.addEventListener('error',()=>{if(!aud.getAttribute('src'))return;if(M.mode==='direct'&&S.transcode){M.mode='tx';const tr=M.q[M.i];aud.src=`/transcode/${tr.id}?t=0`;M.off=0;aud.play().catch(()=>{})}else mnext()});
aud.addEventListener('play',()=>$('#mplay').innerHTML='&#10074;&#10074;');aud.addEventListener('pause',()=>$('#mplay').innerHTML='&#9654;');
aud.addEventListener('timeupdate',()=>{const d=mdur();$('#mcur').textContent=fmtT(mcur());$('#mdur').textContent=fmtT(d);if(d&&!M.drag)$('#mseek').value=mcur()/d*1000});
$('#mplay').onclick=()=>aud.paused?aud.play():aud.pause();$('#mnext').onclick=mnext;$('#mprev').onclick=mprev;
$('#mseek').oninput=()=>M.drag=true;$('#mseek').onchange=e=>{M.drag=false;mseek(e.target.value/1000*mdur())};
$('#mvol').oninput=e=>{aud.volume=e.target.value;localStorage.hf_mvol=aud.volume};if(localStorage.hf_mvol){aud.volume=+localStorage.hf_mvol;$('#mvol').value=aud.volume}
$('#mclose').onclick=()=>stopMusic(false);
$('#mrep').onclick=()=>{M.rep=!M.rep;$('#mrep').classList.toggle('on',M.rep)};
$('#mshuf').onclick=()=>{const curT=M.q[M.i];M.shuf=!M.shuf;$('#mshuf').classList.toggle('on',M.shuf);
  if(M.shuf){const rest=M.q.filter(t=>t!==curT);for(let k=rest.length-1;k>0;k--){const j=Math.floor(Math.random()*(k+1));[rest[k],rest[j]]=[rest[j],rest[k]]}M.q=[curT,...rest];M.i=0}
  else{M.q=[...M.orig];M.i=Math.max(0,M.q.indexOf(curT))}};
if('mediaSession'in navigator){const ms=navigator.mediaSession;ms.setActionHandler('play',()=>aud.play());ms.setActionHandler('pause',()=>aud.pause());ms.setActionHandler('nexttrack',mnext);ms.setActionHandler('previoustrack',mprev)}

/* ---------- keyboard ---------- */
document.addEventListener('keydown',e=>{if(e.target.matches('input[type=search],input[type=text]'))return;
  if(P.it){const k=e.key;if(k===' '||k==='k'){e.preventDefault();togglePlay()}else if(k==='ArrowLeft'){seekTo(cur()-10)}else if(k==='ArrowRight'){seekTo(cur()+10)}
    else if(k==='ArrowUp'){e.preventDefault();vid.volume=Math.min(1,vid.volume+.1)}else if(k==='ArrowDown'){e.preventDefault();vid.volume=Math.max(0,vid.volume-.1)}
    else if(k==='f')toggleFs();else if(k==='m')$('#pmute').click();else if(k==='n')$('#pnext').click();else if(k==='Escape'&&!document.fullscreenElement)closePlayer();poke();return}
  if(e.key==='Escape'&&!$('#modal').classList.contains('hidden'))closeModal();
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
    ap.add_argument("--quiet", action="store_true", help="don't log every HTTP request")
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

    try:
        httpd = Server((a.host, a.port), Handler)
    except OSError as e:
        sys.exit(f"Could not listen on {a.host}:{a.port}: {e}")
    log(f"Homeflix {VERSION} serving {', '.join(str(r) for r in lib.roots)}")
    if FFMPEG and VENC:
        log(f"  Conversion quality: {a.quality} (change with --quality or HOMEFLIX_QUALITY)")
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
