# ByteSqueeze-TSD

A polished, self-hosted media optimization controller for managing HandBrake and FFmpeg jobs across movie and TV libraries.

ByteSqueeze-TSD is built for Plex, Jellyfin, Emby, NAS, and homelab users who want to reduce media file sizes without giving up control over quality, audio, subtitles, presets, hardware acceleration, worker routing, or file safety.

Completed output files are tagged with `-TSD`, short for "Transcoded", so the app can skip media that has already been processed.

[![Docker Pulls](https://img.shields.io/docker/pulls/kevina1724/bytesqueeze-tsd?style=for-the-badge&logo=docker)](https://hub.docker.com/r/kevina1724/bytesqueeze-tsd)

## Docker Image Handles

| Component | Current image | Legacy image (still supported) |
| --- | --- | --- |
| Controller and Web UI | [`kevina1724/bytesqueeze-tsd`](https://hub.docker.com/r/kevina1724/bytesqueeze-tsd) | [`kevina1724/handbrake-tsd-helper`](https://hub.docker.com/r/kevina1724/handbrake-tsd-helper) |
| Headless worker | [`kevina1724/bytesqueeze-tsd-worker`](https://hub.docker.com/r/kevina1724/bytesqueeze-tsd-worker) | [`kevina1724/handbrake-tsd-worker`](https://hub.docker.com/r/kevina1724/handbrake-tsd-worker) |

New installations should use the current ByteSqueeze image handles. The legacy
handles are published from the same builds with matching tags and image digests,
so existing Compose files and deployments will continue receiving updates.

```bash
# Current image handles
docker pull kevina1724/bytesqueeze-tsd:latest
docker pull kevina1724/bytesqueeze-tsd-worker:latest

# Legacy-compatible image handles
docker pull kevina1724/handbrake-tsd-helper:latest
docker pull kevina1724/handbrake-tsd-worker:latest
```

## Highlights

- Web dashboard for local and remote HandBrake encoding
- Modern dark UI with a clean Home overview plus Library, Jobs, Autopilot, Size Wizard, and Settings workspaces
- One-shot encoder, batch tools, queue history, and live progress
- Size Wizard with Simple Mode, Advanced Mode, previews, and AI-assisted recommendations
- Library scanner for mapped movie and show folders
- Poster view, show tracking, seasons, episodes, and recommendation sorting
- Storage-savings history with output-size and runtime prediction
- Intel QSV, software encoders, HEVC, H.264, and AV1 planning support
- Multi-node controller/worker encoding
- Remote-transfer mode for workers that do not have media drives mounted
- Bounded Autopilot with observe/manage modes, schedules, queue caps, and explained decisions
- Transactional node protocol v2 with safe pairing retries, automatic session recovery, and diagnostics
- Universal ByteSqueeze Android, iPhone, and iPad companion with secure pairing, poster library, remote job controls, automation, and node health
- Native Windows worker with a compact tray UI, one-time pairing, multi-GPU discovery, selectable work storage, and NVIDIA/AMD/Intel/CPU Smart-job preference
- Versioned mobile API with hashed tokens, refresh, read/control scopes, and device revocation
- Safer cleanup behavior for failed or canceled jobs

## V3 Interface

V3 is the primary interface, built around a cinema-operations workspace. It adds a persistent desktop sidebar, a phone-friendly bottom dock, a global command center (`Ctrl/Cmd+K` or `/`), faster Library search and queue actions, clearer Smart Preset surfaces, and a visible Source → Intent → Preview → Queue workflow in Size Wizard. The poster framework and encoding engine are unchanged.

V2 Classic remains available as an option. Open **Settings > Interface**, select **V2 Classic**, and save. For an immediate one-page switch, add `?ui=v2` to any ByteSqueeze URL. The saved V3/V2 choice and comfortable/compact density are independent of encoding settings.

V3 is published in the standard `latest` and `main` Docker channels:

```bash
docker pull kevina1724/bytesqueeze-tsd:latest
docker pull kevina1724/bytesqueeze-tsd-worker:latest
```

Windows gaming PCs can join the same queue without Docker. Download and extract
`ByteSqueeze-Windows-Worker.zip`, run `ByteSqueezeWorker.exe` from the
extracted folder, then pair the URL and code shown in its compact window.
then leave it running in the notification area. It bundles HandBrakeCLI and
FFmpeg, detects every display adapter, verifies the encoders that HandBrake
actually exposes, and lets Smart jobs prefer NVIDIA NVENC, AMD VCN/VCE, Intel
Quick Sync, or the CPU. See the [Windows Worker guide](docs/WINDOWS_WORKER.md).

## Release 3.12 Autopilot Refresh

The 3.12 experience is designed for set-it-up-and-let-it-run operation without making users hunt through Settings or giving automation unlimited control:

- **Observe mode** scans and explains what it would queue without changing the queue.
- **Manage mode** queues only stable, allowed media within the configured schedule, batch limit, and maximum active-job count.
- **Readiness checks** call out missing library mappings, presets, durable storage, hardware acceleration, and worker availability.
- **Dedicated Autopilot workspace** keeps the guided tour, preview training, guardrails, decisions, and completed-encode feedback together.
- **Continuous learning** lets you correct picture, playback, audio, subtitle, or size choices after watching a completed learned encode.
- **Decision history** shows why each item was eligible, skipped, or left waiting.
- **Optional AI advisor** supports local planning, Gemini Flash, OpenAI, or planner-only operation while the validated Size Wizard remains authoritative.
- **Production runtime** uses a single Gunicorn process with threaded HTTP handling so queue and scheduler ownership stays deterministic.

Autopilot is disabled and set to Observe by default. Its page now creates and displays the short accurate previews needed to train Smart Presets, with a visible review counter and no hidden prerequisite. See the [complete Autopilot guide](docs/AUTOPILOT.md).

## ByteSqueeze Mobile

ByteSqueeze is the universal Flutter companion for managing a TSD controller from Android, iPhone, or iPad. It can browse movie and show posters, track shows, queue server-side Smart Preset jobs, use the Size Wizard, manage active jobs, tune Autopilot, review learned preset decisions, and monitor workers, storage savings, and events. All transcoding remains on the Docker controller and its workers.

The mobile V3 interface mirrors the web workspace with an adaptive desktop
sidebar, polished phone navigation, a searchable command center, real Smart
comparison previews, full-season queues, mobile GPU-capacity controls, and a
persistent **Settings → Interface & UI → V2 Classic** fallback. Interface
density and V3/V2 selection are local to each device and never alter encoding
settings.

The same signed application runs on iPhone and iPad. iPad uses the wide workspace automatically and supports portrait, landscape, Split View, keyboard, and pointer input without a separate implementation.

- Source: [`mobile/bytesqueeze`](mobile/bytesqueeze)
- Product and mobile API notes: [`docs/BYTESQUEEZE.md`](docs/BYTESQUEEZE.md)
- iPhone/iPad and TestFlight setup: [`docs/ios-testflight.md`](docs/ios-testflight.md)
- Latest Android APK: [GitHub Releases](https://github.com/kevin1724/ByteSqueeze-TSD/releases/latest)
- Fast pairing: open **Settings > Linked Nodes**, generate a one-time code under Companion app access, then select **Open app & pair**
- Paired-device cleanup: revoke access, permanently forget an individual app, or clear inactive app records from the same controller panel

LAN HTTP is supported for home-server use. Use a trusted HTTPS reverse proxy when connecting from outside the home network; do not expose the controller directly to the public internet.

## Why Use It

HandBrake is powerful, but managing a large library by hand is slow. This project adds a web UI, queue system, history, predictions, library scanning, and worker-node support around HandBrakeCLI.

Use it when you want to:

- Shrink large movies and shows
- Keep track of how much storage you saved
- Queue encodes from a browser
- Avoid re-encoding files that are already done
- Use presets without opening the HandBrake desktop app
- Send jobs to another machine on your network
- Keep original files safe unless an encode fully succeeds

## Quick Start

Clone the repo:

```bash
git clone https://github.com/kevin1724/ByteSqueeze-TSD.git
cd ByteSqueeze-TSD
```

Edit `docker-compose.yml` and mount your media folders:

```yaml
services:
  hb-web:
    ports:
      - "8081:8080"
    volumes:
      - /path/to/movies:/media/Movies
      - /path/to/shows:/media/Shows
      - ./data:/app/data
      - ./presets:/presets
```

Start the app:

```bash
docker compose up -d
```

Open the web UI:

```text
http://SERVER-IP:8081
```

Recommended first setup:

1. Open Settings.
2. Select your CPU profile.
3. Confirm or upload your HandBrake presets.
4. Map movie and show folders.
5. Optionally add TMDb credentials for preferred artwork. Leave keyless artwork enabled so local sidecars, TVmaze, and Apple Search can fill any gaps without an API key.
6. Run a Library scan and track the shows you want in the release calendar.
7. Queue a few test encodes.
8. Open **Autopilot**, complete the guided preview training, and save the Safe starter or Balanced policy in Observe mode.

## Core Pages

### Home and Jobs

Home is the default page and is also available at `/dashboard`. It is a quiet overview of queue health, storage savings, library totals, workers, Autopilot readiness, and recent activity. Open `/jobs` for file and folder search plus queue operations.

- Browse folders and video files
- Select a file and encode it with presets
- Open a file in the Size Wizard
- Run batch tools
- View queued, running, completed, and failed jobs
- Track ETA, progress, estimated final size, saved size, and runtime
- Cancel running jobs
- View linked worker-node status

### Size Wizard

The Size Wizard helps choose better settings before starting an encode.

Simple Mode keeps the workflow easy:

- Pick a file
- Choose a quality goal
- Set a target size
- Choose speed or compression preference
- Keep or filter audio and subtitle languages
- Review the live plan
- Queue the encode

Advanced Mode keeps the full technical surface available:

- Codec and encoder
- AV1, HEVC, H.264, software, and QSV choices
- Bit depth
- Frame rate
- Resolution and downscale controls
- Audio tracks
- Subtitle tracks
- Language selection
- Crop, deinterlace, filters, two-pass, and extra args
- Saved wizard presets
- Fast and accurate previews

The wizard also includes an optional AI advisor. Open **Settings > AI & API Keys** to paste and test a Google Gemini or OpenAI key, or choose built-in local or planner-only operation. Normal advisor chats send compact probe facts and selected options, not media content. The separately opt-in beta per-episode scene feature can send representative JPEG stills; its switch and privacy disclosure are under **Settings > Smart Presets**. The deterministic planner continues to validate and own the final HandBrake plan. The complete [AI Advisor setup guide](docs/AI_ADVISOR.md) includes Gemini and OpenAI walkthroughs, Docker Compose examples, privacy details, sample questions, and troubleshooting.

Smart Presets add a learning loop on top of that safe planner:

- Choose the main goal, playback compatibility, hardware preference, and audio strategy
- Configure preservation-first protections in **Settings → Smart Presets**; they apply to movies, episodes, seasons, Autopilot, linked nodes, and ByteSqueeze
- Keep source resolution, black bars, and display aspect ratio so tight episode targets cannot silently become 720p
- Keep every audio and subtitle language, or select an explicit language list
- Require original audio passthrough, or opt into E-AC3 5.1 at 640 kbps when audio conversion is acceptable
- Generate three source-aware candidates ranked by quality, savings, speed, compatibility, and prior feedback
- Re-probe every episode independently for HDR transfer/primaries, bit depth, resolution, frame rate, and codec; complete-season/show queues never reuse another episode's media decision
- Lock every Smart encode to a constant frame rate derived from that file's own source average; preset mappings, AI tuning, and worker adaptation cannot switch it to VFR/PFR or another FPS
- Enforce a codec-aware per-episode quality floor, preserve supported HDR dynamic metadata, and keep HDR10+/Dolby Vision on a compatible 10-bit encoder
- Optionally enable beta cloud scene analysis to classify each episode's motion, grain, darkness, and complexity from representative stills, with a bounded bitrate adjustment and deterministic fallback
- Apply a candidate and inspect the same short HandBrake encode that a real job will use
- Approve the preview or mark quality, size, speed, or compatibility concerns
- Keep all preference history local in `data/smart_presets.json`
- Unlock automatic selection after the visible minimum of consistent reviews, currently two approvals for the default profile
- Keep learning after playback by rating completed learned jobs on the Autopilot page

The learned model is intentionally explainable. It uses similar source type, HDR state, resolution, codec, encoder family, target ratio, and output resolution to weight preview and post-encode reviews. The deterministic Size Wizard remains authoritative over HandBrake arguments, while the selected optional advisor helps evaluate and explain safe choices. When learning is ready, **Smart learned preset** is available from Jobs and Library, and Autopilot Manage mode can choose it automatically.

### Library

The Library page scans the movie and show folders you map in Settings.

- Cached scans
- Movie poster grid
- Show cards with seasons and episodes
- Artwork priority: TMDb when configured, then local `poster.jpg`/`folder.jpg`/`cover.jpg`, TVmaze show art, and Apple movie art
- Upcoming episode calendar for tracked and untracked library shows
- Complete title catalog plus recently added rails
- Sort by likely storage savings
- Filter by title, quality, type, and status
- Generate a real matched-frame and side-by-side Smart encode preview without leaving the Library
- Apply one-time Smart guardrails for resolution, compatibility, audio, subtitles, encoder, and size/detail balance
- Queue movies, episodes, seasons, shows, or selected batches
- Track show release dates and optionally auto-queue new episodes after their downloaded files become stable
- Send jobs to local encoding or linked workers

The scanner ignores files that already contain `-TSD`.

### Settings

Settings are split into cleaner sections:

- Encoding defaults
- Preset management
- CPU profile
- Intel QSV availability
- Optional local, Gemini Flash, OpenAI, or planner-only Size Wizard advisor
- Library folder mapping
- TMDb-first artwork when configured, with keyless artwork and episode-release metadata as the automatic fallback
- Auto scan
- Companion-app access and device revocation
- Linked nodes
- Events
- Storage savings

Events and storage savings use scrollable tables so the page stays compact.

## File Safety

The app is designed to protect original files.

- Failed jobs delete the incomplete `-TSD` output, not the source file
- Canceled jobs preserve the source file
- Already encoded `-TSD` files are skipped by scans and queues
- Remote-transfer output is verified before being written beside the original
- Original deletion only happens after a successful encode and verification
- Clear actions ask for confirmation

Keep backups of important media before enabling any aggressive cleanup behavior.

## Auto Scan

Auto scan can keep the Library updated without constantly probing every file.

By default, it is designed to run every 30 minutes. On each pass it:

1. Checks whether auto scan is enabled.
2. Skips if an encode is running and skip-while-encoding is enabled.
3. Loads the previous file and directory index.
4. Checks mapped movie and show directory timestamps, reusing unchanged directory contents.
5. Reads file metadata only in changed folders or for files still inside the stability window.
6. Parses only new or changed video files.
7. Ignores `-TSD` files.
8. Marks missing files as removed.
9. Rebuilds and saves the Library cache only when its catalog changed.
10. Auto-queues tracked episodes only after file stability checks pass.

This keeps routine scans lightweight even on large SMB/NFS libraries. A full
verification pass runs every 12 hours by default to catch uncommon in-place file
changes that do not update a directory timestamp. Set
`TSD_LIBRARY_FULL_VERIFY_HOURS` to an integer from 1 to 168 to change that safety
interval.

## Autopilot

Autopilot reuses the incremental Library index and existing file-safety checks. A decision cycle:

1. Scans only mapped media roots and ignores `-TSD` outputs.
2. Waits for new or changed files to pass the configured stability window.
3. Excludes files already owned by queued or running jobs.
4. Applies movie/show, minimum-size, and predicted-savings policy rules.
5. Sorts eligible work by predicted savings and source size.
6. In Observe mode, records recommendations only.
7. In Manage mode, waits until Smart Preset preview training is ready, then queues no more than the per-scan limit and never exceeds the active-job cap.

Autopilot does not weaken the existing output verification or original-file protections.

The dedicated web workspace provides a first-time tour, an explicit accurate-preview training panel, a visible review target, editable Safe starter/Balanced/Hands-off profiles, decision reasons, and optional feedback after a completed learned encode has actually been watched. Running a decision cycle does not silently start training. See [`docs/AUTOPILOT.md`](docs/AUTOPILOT.md) for the full workflow and troubleshooting guide.

## Headless Worker Encoding

The recommended worker is a separate, headless container. It has no media
browser, library scanner, settings website, or mapped media drives. The main
controller sends temporary jobs to the worker's `/work` drive and receives each
verified result when its encode finishes. Hardware workers can run multiple
encodes at the controller-managed limit; CPU/software work always runs alone.

- The main container owns the media library and queue
- Workers only expose authenticated node/health APIs
- The pairing code is printed in `docker logs` and can be regenerated without a restart
- `/work` is the worker's only required mount
- Remote transfer is selected automatically; path mappings are unnecessary
- Pairing is idempotently recoverable for the same controller when a network response is lost
- Node state writes are serialized and atomic, with backup recovery
- Protocol discovery negotiates capabilities while remaining compatible with older workers
- Paired nodes use trusted tokens for commands
- Workers report heartbeat, status, progress, completed jobs, and errors
- Worker jobs appear in the main Jobs queue, where completed and failed worker history can be cleared with the local history
- Nodes can reconnect after normal offline periods
- Prediction history is tracked per worker
- Per-worker GPU capacity is configured only on the main node website

The controller can send jobs to the local node, the best available worker, or a selected worker.

See the versioned [Headless Worker Setup guide](docs/HEADLESS_WORKER.md) for
complete Linux, Unraid, and Windows work-drive mapping, pairing, multi-encode,
update, and troubleshooting instructions.

Use `GET /api/nodes/diagnostics` to inspect protocol, monitor health, heartbeat failures, and linked-node totals.

### Start and pair a worker

On the worker machine, copy `docker-compose.worker.yml`, choose the host folder
that should hold the current encode, and start it:

```bash
TSD_WORKER_WORK_DIR=/path/to/fast/transcode-drive docker compose -f docker-compose.worker.yml up -d
docker logs bytesqueeze-worker
```

The log contains a banner like:

```text
ByteSqueeze headless worker is ready
Pairing code:  ABCDE-FGHJK
```

Generate a new one-time code without restarting the worker or interrupting an
active encode:

```bash
docker exec bytesqueeze-worker python -m worker.app pairing-code
```

On the main server, open **Settings → Linked Workers**, enter the worker URL
(for example `http://192.168.1.50:8082`) and that code, then select **Pair and
verify**. The controller records its reachable URL, negotiates the protocol,
forces remote-transfer mode, and immediately verifies the secure connection.

## ByteSqueeze Android Companion

ByteSqueeze ships from the shared Flutter project in `mobile/bytesqueeze`. The
phone is a remote control only; Docker controllers and workers perform every
encode.

- Discovery: `GET /api/mobile/v1/discovery`
- Pairing: `POST /api/mobile/v1/pair`
- Token rotation: `POST /api/mobile/v1/token/refresh`
- Read endpoints for status, jobs, nodes, events, library, release calendar, and Library preview progress
- Scoped queue, node-target, show-monitoring, and Autopilot controls
- Native Size Wizard planning and queueing for Library movies and individual episodes
- Matched Library preview frames, per-season Smart Queue actions, and transient tuning from the phone
- Shared accurate-preview review and Smart Preset feedback from the phone
- Focused default screens with optional secondary controls and Stats for nerds under Interface & UI
- Primary home address plus an optional Tailscale/away address with automatic connection failover
- Browser-admin controls for one-tap app links, pairing codes, revoking access, forgetting devices, and clearing inactive app records

Access and refresh tokens are returned only to the client and stored on the server as hashes. Keep the web UI and mobile API on a trusted LAN or behind your own authenticated reverse proxy; the main web UI does not yet provide user accounts.

### Transfer lifecycle

1. The controller grants temporary authenticated file access.
2. The worker downloads the source file under `/work/jobs`.
3. The worker encodes locally.
4. The worker uploads the finished `-TSD` file back.
5. The controller verifies the output.
6. The controller writes the output beside the original file.
7. Temporary files are cleaned up.

If the controller is offline when the worker finishes, the worker keeps the finished output and waits to upload it later.

Full controller containers can still act as legacy mounted-media workers, but
that advanced mode is no longer required for the normal setup.

## Intel QSV / Quick Sync

The Docker image builds HandBrakeCLI with QSV support and installs Intel media runtime packages where available.
QSV encoders automatically request QSV hardware decoding for supported H.264
and HEVC sources. The setting applies to both controller and headless-worker
encodes:

```dotenv
TSD_HW_DECODE=auto
```

- `auto` uses QSV decode when the selected video encoder is Intel QSV.
- `qsv` prefers QSV decode for supported H.264/HEVC sources.
- `off` disables hardware decoding.

Unsupported streams stay on software decode. If an attempted QSV decode fails,
ByteSqueeze removes only that attempt's partial output and retries once with
software decoding while keeping the selected encoder. Encode logs show the
source and target resolution, actual preset and encoder, requested decode path,
and whether HandBrake verified the active QSV path.

Linux defaults to QSV adapter `0` and `/dev/dri/renderD128`. Override these only
on multi-GPU hosts with `TSD_QSV_ADAPTER` and `TSD_QSV_RENDER_DEVICE`. The image
maps HandBrake's adapter index to its detected DRM render node instead of
passing the numeric index to VAAPI as a device name. Container startup and each
QSV encode run VAAPI and QSV device preflight checks; their output includes the
render-node listing, selected node, active Intel VA driver, and adapter index.
When Linux HandBrake downloads decoded QSV frames for its software filter
pipeline, the encoder log explicitly reports QSV hardware decode and QSV
hardware encode through a system-memory transfer; it no longer mislabels that
verified hardware-decode case as an encode-only path.

Rebuild after QSV-related Dockerfile changes:

```bash
docker compose build --no-cache hb-web
docker compose up -d hb-web
```

Check QSV inside the container:

```bash
docker exec -it hb-web check-qsv
```

Expected signs of working QSV:

```text
/dev/dri exists
vainfo returns Intel driver details
QSV device preflight: passed
HandBrake lists qsv_h264, qsv_h265, or qsv_h265_10bit
```

Requirements:

- Intel CPU with enabled iGPU
- Host exposes `/dev/dri/renderD128`
- Compose maps `/dev/dri:/dev/dri`
- Container has access to `video` and `render` groups
- QSV render-device availability is enabled in Settings

Intel `F` and `KF` desktop CPUs usually do not include an iGPU.

### Concurrent hardware transcodes

**Settings → General → Encoding settings** controls the local node default.
**Settings → Linked Workers** adds a separate **Simultaneous hardware
transcodes** value for every paired worker. The safe default is `1`; `2` is a
practical starting point for modern Intel Quick Sync systems. Values up to `8`
are available for tested hardware. The limit applies to QSV, NVIDIA, AMD,
VideoToolbox, and VAAPI jobs. CPU/software encodes always run alone, and a queued
CPU job keeps its FIFO position instead of being bypassed by later GPU jobs.
Lowering the limit does not stop work already running; it only prevents another
job from starting until usage is below the new limit.

### Output container

**Settings → General → Encoding settings → Output Container** selects MKV,
MP4, or Auto for newly queued jobs. MKV remains the default for existing
installations. Auto inspects each file independently and uses MP4 when the
effective video, audio, and subtitle output is compatible. It safely keeps MKV
when a selected track needs features such as DTS/DTS-HD passthrough, PGS/VobSub
bitmap subtitles, or styled ASS/SSA subtitles. The requested policy is
snapshotted with each job and sent to linked workers; workers return the actual
per-file choice so the HandBrake muxer and final `.mkv` or `.mp4` filename stay
aligned. MP4 also has an optional **Web Optimized / Fast Start** switch; it is
disabled for MKV and Auto.

### Audio optimization

Every new encode inventories all audio streams before it starts. The default
**Preserve / Passthrough** policy keeps every track, language, channel layout,
and track metadata. **Optimize lossless audio** only proposes changes for
eligible lossless tracks, while efficient AAC, AC3, E-AC3, MP3, and Opus tracks
remain untouched. Atmos/DTS:X-capable tracks, commentary, removal, and channel
downmixing require an explicit choice in the per-track review.

The Queue can also create an **Audio optimization only** job. It remuxes with
FFmpeg, copies video, subtitles, attachments, chapters, and metadata, and only
encodes selected audio tracks. Completed jobs expose **Optimize Audio** so an
existing AV1/H.265/H.264 output can be improved without re-encoding video.
ByteSqueeze validates the temporary output before installing it and records
measured video, audio, and total storage savings separately.

## Official Docker Images

### Controller and Web UI

Current handle:

```bash
docker pull kevina1724/bytesqueeze-tsd:latest
```

Legacy-compatible handle:

```bash
docker pull kevina1724/handbrake-tsd-helper:latest
```

Run:

```bash
docker run -d \
  --name bytesqueeze-tsd \
  -p 8081:8080 \
  -v /path/to/media:/media/Media \
  -v /path/to/data:/app/data \
  -v /path/to/presets:/presets \
  --device /dev/dri:/dev/dri \
  kevina1724/bytesqueeze-tsd:latest
```

The `latest` and `main` images are published automatically from `main`. Stable
controller releases also publish `3.25.0` and `3.25` tags. The legacy
`kevina1724/handbrake-tsd-helper` name is published from the same build and will
continue receiving identical updates, so existing installations do not break.

### Headless Worker

The encoding-only worker has its own public Docker Hub image:

[kevina1724/bytesqueeze-tsd-worker on Docker Hub](https://hub.docker.com/r/kevina1724/bytesqueeze-tsd-worker)

`latest` and `main` follow the main branch. Stable worker releases also publish
`2.13.5` and `2.13` tags:

```bash
docker pull kevina1724/bytesqueeze-tsd-worker:latest
```

Legacy-compatible handle:

```bash
docker pull kevina1724/handbrake-tsd-worker:latest
```

It needs one writable mount and no media mounts:

```bash
docker run -d \
  --name bytesqueeze-worker \
  -p 8082:8080 \
  -e TSD_WORKER_NAME="Garage Worker" \
  -v /path/to/fast/transcode-drive:/work \
  kevina1724/bytesqueeze-tsd-worker:latest

docker logs bytesqueeze-worker
```

The legacy `kevina1724/handbrake-tsd-worker` name is also published from the
same worker build for backward compatibility.

## Runtime Data

Runtime state is stored in `data/`.

Common files:

```text
data/jobs.json
data/settings.json
data/storage_stats.json
data/events.json
data/beta_library_cache.json
data/beta_scan_index.json
data/beta_tracked_shows.json
data/beta_autoscan_status.json
data/linked_nodes.json
data/mobile_devices.json
data/wizard_presets.json
data/smart_presets.json
data/logs/
```

Do not commit private runtime data, API keys, node secrets, cache files, or logs.

Safe staging example:

```bash
git add README.md Dockerfile docker-compose.yml .gitignore webui worker presets tests
git status --short
```

## Project Layout

```text
ByteSqueeze-TSD/
|-- Dockerfile
|-- docker-compose.yml
|-- README.md
|-- presets/
|-- worker/
|-- webui/
|   `-- app/
|       |-- jobs.py
|       |-- node_linking.py
|       |-- routes.py
|       |-- settings.py
|       |-- smart_presets.py
|       |-- storage_stats.py
|       |-- wizard_llm.py
|       |-- static/
|       `-- templates/
|-- tests/
`-- data/
```

## Troubleshooting

### No files appear in Jobs

Check your Docker volume mounts. Media should be mounted under a path the app can browse, commonly `/media/...`.

### Library scan is empty

Open Settings and map at least one movie folder or show folder.

### Posters do not show

Refresh the Library after changing poster settings. ByteSqueeze prefers TMDb
when a key or read token is configured. Otherwise, or when no TMDb poster is
available, it uses `poster.jpg`, `folder.jpg`, or `cover.jpg` beside the media
before trying TVmaze/Apple.

### QSV does not show up

Run:

```bash
docker exec -it hb-web check-qsv
```

Confirm `/dev/dri` exists in the container and that the host iGPU is enabled.

### Worker pairing fails

Run `docker logs bytesqueeze-worker`, use the newest code, and confirm the URL
is reachable from the controller. Pairing codes are one-use and expire after an
hour. Run `docker exec bytesqueeze-worker python -m worker.app pairing-code` to
print a fresh code without restarting or deleting an existing pairing.

### Worker cannot access a media path

The headless worker never accesses controller media paths. Confirm the worker
has enough free space in `/work` for the source and temporary output and that
the controller URL is reachable from the worker.

### Job looks stuck

Check the job progress, ETA, linked node status, and job log. If needed, cancel the job from the Jobs page.

Restart only when the process is truly stuck:

```bash
docker restart hb-web
```

## Updating

```bash
git pull
docker compose up -d --build
```

After larger UI updates, hard-refresh the browser.

## Roadmap

Current focus:

- Improve Size Wizard recommendations
- Polish Library workflows for shows, seasons, and episodes
- Expand hardware encoder support
- Build the Android companion client on top of the mobile API v1 contract
- Add web user authentication and per-user permissions
- Add notification hooks

## Contributing

Issues, feature ideas, test results, and pull requests are welcome.

Helpful reports include the source filename, codec, resolution, preset or wizard settings, whether the job was local or remote, and the relevant log lines.

## License

MIT. Free for personal, commercial, and homelab use.
