<div align="center">

# 🎬 Homeflix

### Your movies. Your music. Your couch. No subscription.

**A streaming service for your own media library, in one Python file.**

No Docker. No database. No account. No `pip install`. Just `python3 homeflix.py`.

![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue?logo=python&logoColor=white)
![Dependencies: zero](https://img.shields.io/badge/dependencies-zero-brightgreen)
![Single file](https://img.shields.io/badge/single%20file-~1500%20lines-orange)
![Self-hosted](https://img.shields.io/badge/self--hosted-100%25-purple)

</div>

---

<p align="center">
  <img src="docs/screenshots/home.png" alt="Homeflix home screen with a hero banner and a Continue Watching row" width="100%">
</p>

You already own the movies, the shows and the albums. They're sitting in a folder named `Downloads` with filenames like `The.Show.S02E07.1080p.WEB.x265-GRP.mkv`.

**Homeflix turns that folder into a polished streaming app.** Open it on any phone, laptop or smart TV browser in your house. You get poster rows, a "Who's watching?" screen, Continue Watching, binge-ready TV seasons, and a music player that works from your phone's lock screen.

It's one file and uses nothing outside the Python standard library. You can read all of it in an afternoon, and it runs on whatever machine already has your media on it.

<p align="center">
  <img src="docs/screenshots/player.jpg" alt="Homeflix video player mid-episode, showing the seek bar, skip, volume and next-episode controls" width="100%">
  <br><sub>The player resumes where you left off, and the controls fade away while you watch.</sub>
</p>

<sub>Screenshots use a demo library of made-up titles with generated artwork.</sub>

## ✨ Why Homeflix?

| | Homeflix | Typical media server |
|---|---|---|
| **Install** | One Python file | Docker images, databases, accounts |
| **Dependencies** | Python standard library (ffmpeg optional) | Dozens of packages and services |
| **Phones home?** | Never. No telemetry, no cloud login | Often requires an online account |
| **Client apps** | Any web browser | Often platform-specific apps |
| **Auditable** | ~1,500 lines you can actually read | Hundreds of thousands |

## 🍿 What you get

### A streaming-app experience for your own files
- **"Who's watching?" profiles.** Everyone in the house gets their own watch history and **My List**.

  <p align="center"><img src="docs/screenshots/profiles.png" alt="Who's watching? profile picker with four profiles and an Add Profile button" width="70%"></p>

- **A home screen worth scrolling.** Hero banner, **Continue Watching**, My List, Recently Added, TV Shows, Movies, New Music and genre rows.
- **Instant search.** Press `/` and type. It searches titles, shows, artists and albums.

### TV shows that just work
- Episodes are **grouped into shows and seasons automatically**, from `Show.Name.S01E02.mkv` or `Show/Season 1/1x02.mkv`.
- Season pickers, per-episode progress bars, and **next-episode autoplay** for binge nights.

### Messy filenames, cleaned up
- `Movie.2010.1080p.BluRay.x264-YIFY` shows up as ***Movie (2010)***.
- `Inception (2010)/movie.mkv` uses the folder name.
- Thumbnails are grabbed from each video automatically in the background. Drop a `poster.jpg` next to a file to use your own art.

### Plays (almost) anything
- **Direct streaming** with instant byte-range seeking for browser-friendly files (H.264 / VP9 / AV1 in MP4, MKV, WebM).
- **Converted as it plays** with ffmpeg for everything else: HEVC/x265, AVI, WMV, old MPEG-4, ALAC, WMA and more. Seeking still works.
- **HEVC/x265 in original quality** on devices that can decode it (iPhone, iPad, Mac, many Android phones and TVs). The video is passed through untouched, with no re-encoding.
- **High-quality conversion by default**, with three levels to match your CPU and network: `balanced`, `high` and `max`.
- **Subtitles:** sidecar `.srt`/`.vtt` files (`Movie.en.srt`, `Subs/` folders) plus text subtitles embedded in MKVs.

### A real music player, too
- Albums are built from your tags, with folder structure as the fallback.
- Cover art is taken from embedded artwork or `cover.jpg` / `folder.jpg`.
- A **persistent player bar** with shuffle and repeat that keeps playing while you browse.
- **Lock-screen and media-key controls** on phones.

<p align="center"><img src="docs/screenshots/music.png" alt="Album view with the track list, the current track highlighted, and the music player bar at the bottom" width="100%"></p>

### Keyboard-first player
`Space` play/pause · `←` `→` skip 10s · `↑` `↓` volume · `F` full screen · `M` mute · `N` next episode · `Esc` back

## 🚀 Up and running in 60 seconds

### One-command install (Pop!_OS / Ubuntu)

```bash
git clone https://github.com/crash101g/homeflix.git && cd homeflix
bash install.sh
```

The installer:
- installs ffmpeg if it's missing
- copies the app to `~/src/homeflix`
- asks for your port, media folders and an optional password
- adds a `homeflix` command that runs the app with those settings
- if UFW is active, offers to open the port **to your LAN subnet only**

Nothing runs in the background. Start Homeflix when you want it:

```bash
homeflix            # Ctrl+C to stop. Flags like --quality max work here too.
```

Then open **`http://<your-PC-IP>:8484`** on any device in the house. That's it.

### Want it as a service?

Running it yourself is the default. If you'd rather have it in the background, re-run the installer with one of these:

| Command | What it does |
|---|---|
| `bash install.sh --service` | Adds a systemd user service and starts it. It does **not** start at login. |
| `bash install.sh --autostart` | Same, but also starts it every time you log in (and offers to keep it running when you log out). |
| `bash install.sh --remove-service` | Removes the service. The `homeflix` command still works. |
| `bash install.sh --uninstall` | Removes the service and the `homeflix` command. Your settings and watch history are kept. |

### Or just run it

```bash
python3 homeflix.py                                   # ~/Downloads/movies + ~/Music on :8484
python3 homeflix.py --password 'something' --port 9000
python3 homeflix.py --videos ~/Videos --videos /mnt/nas/movies --music ~/Music
python3 homeflix.py --host 127.0.0.1                  # this PC only
```

| Option | What it does |
|---|---|
| `--videos DIR` | Video folder (repeatable) |
| `--music DIR` | Music folder (repeatable) |
| `--host` / `--port` | Bind address and port (default `0.0.0.0:8484`) |
| `--password` | Require a password to use the app |
| `--quality LEVEL` | Quality for files that must be converted: `balanced`, `high` (default) or `max` |
| `--rescan-minutes N` | Auto-rescan interval (default 15, `0` disables) |
| `--no-thumbs` | Skip thumbnail generation |
| `--quiet` | Don't log every HTTP request |

To make a quality stick, add `HOMEFLIX_QUALITY=max` (or `balanced`) to `~/.config/homeflix/env`. The `homeflix` command and the service both read that file. If you use the service, run `systemctl --user restart homeflix` afterwards.

New files show up on their own: Homeflix rescans every 15 minutes, or right away when you press the **⟳** button.

## 🔒 Secure by design

A tool that serves files from your disk should be paranoid, so Homeflix is:

- **No path traversal, by construction.** Files are served only by opaque IDs from the scanned index. Request paths never touch the filesystem.
- **Password protection done properly.** HMAC session cookie (HttpOnly, SameSite=Lax), a 1.5-second delay on every wrong guess, and the API returns 401 until you log in.
- **Hardened frontend.** Strict Content-Security-Policy and `nosniff`, and every filename is rendered as text, never through `innerHTML`.
- **LAN-first.** The installer only opens the firewall to your local subnet.

> ⚠️ Homeflix speaks plain HTTP and is meant for a trusted home network. **Don't port-forward it to the internet.** For access away from home, use Tailscale/WireGuard or put it behind a TLS reverse proxy.

## 📁 Where things live

| What | Where |
|---|---|
| App | `~/src/homeflix/homeflix.py` |
| `homeflix` command | `~/.local/bin/homeflix` |
| Settings (port, folders, password, quality) | `~/.config/homeflix/env` |
| Profiles, watch progress, My List | `~/.local/share/homeflix/state.json` |
| Thumbnails, probe cache, extracted subs | `~/.cache/homeflix/` (safe to delete) |

**Service commands** (only if you installed with `--service` or `--autostart`)
```bash
systemctl --user start homeflix        # start
systemctl --user stop homeflix         # stop
systemctl --user restart homeflix      # restart
journalctl --user -u homeflix -f       # follow logs
bash install.sh --remove-service       # remove
```

## 🧰 Requirements

- **Python 3.8+.** That's the only hard requirement.
- **ffmpeg / ffprobe** (optional, strongly recommended) for thumbnails, durations, tags, embedded subtitles and converting formats browsers can't play.

## 📜 License

MIT. See [LICENSE](LICENSE).

---

<div align="center">

**Stop paying monthly to rent what you already own.**

⭐ If Homeflix turned your Downloads folder into movie night, star the repo.

</div>
