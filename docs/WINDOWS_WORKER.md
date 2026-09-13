# ByteSqueeze Windows Worker

The Windows Worker turns a gaming PC or workstation into a paired ByteSqueeze
node without requiring Docker. It runs the same transfer-only worker API,
queue, retry logic, preset snapshots, and source/output validation as the
Docker worker.

## Install and pair

1. Download `ByteSqueeze-Windows-Worker.zip` from the project release.
2. Verify its published SHA-256 checksum, extract the complete archive to a
   local folder, and run `ByteSqueezeWorker.exe` from that folder. Do not move
   the executable away from its adjacent `_internal` runtime directory.
3. Allow the app through Windows Firewall on private networks when prompted.
4. In the main ByteSqueeze server, open **Settings → Linked Nodes**.
5. Approve the one-time Windows Firewall prompt. The rule accepts only the
   local subnet and Tailscale addresses, even when Windows labels the active
   network Public. You can reopen it later with **Allow controller access**.
6. Copy the worker URL shown in the app, enter its one-time pairing code, and
   pair it.
7. Close the window when finished. The worker remains visible in the Windows
   notification area and continues accepting jobs.

Run the complete extracted folder from a local Windows drive. The worker is
deliberately distributed as an uncompressed portable application rather than a
single self-extracting PyInstaller binary. This avoids unpacking executable
code into a random temporary `_MEI...` folder at startup, which is a common
source of antivirus false positives. It also keeps the persistent Start with
Windows shortcut valid.

The release workflow supports Authenticode signing when the repository secrets
`WINDOWS_SIGNING_CERTIFICATE_BASE64` and
`WINDOWS_SIGNING_CERTIFICATE_PASSWORD` contain a trusted PFX certificate.
Without a trusted signing identity, Windows may still display an
unknown-publisher SmartScreen warning because reputation is controlled by
Microsoft rather than application code. Never disable Defender globally; use
the official GitHub release, verify the checksum, and submit any named Defender
detection to Microsoft as a false positive.

State and pairing credentials live in
`%LOCALAPPDATA%\ByteSqueeze Worker\state`. Incoming source copies and completed
job outputs default to `%LOCALAPPDATA%\ByteSqueeze Worker\jobs`; HandBrake,
FFmpeg, and pre-encode scratch files default to
`%LOCALAPPDATA%\ByteSqueeze Worker\temp`. Both data paths are selectable in the
app and apply to newly received jobs as soon as settings are saved.

## Hardware selection

The app inventories all Windows display adapters and then checks the bundled
HandBrakeCLI encoder list. A GPU is only advertised to the controller when the
actual HandBrake build exposes the matching encoder family.

- **Auto** prefers NVIDIA NVENC, then AMD VCN/VCE, then Intel Quick Sync, and
  finally the CPU for adaptive Smart jobs.
- **NVIDIA NVENC**, **AMD VCN/VCE**, **Intel Quick Sync**, and **CPU/software**
  set the preferred family for adaptive Smart jobs.
- **GPU routing** can assign AV1, H.265, and H.264 jobs to different detected
  adapters. When enabled, compatible idle-GPU fallback keeps another adapter
  busy instead of waiting for the preferred one.
- Explicitly selected/locked presets are never rewritten by this preference.
- **Parallel GPU jobs** controls worker hardware capacity from 1–8. Software
  jobs remain exclusive to protect CPU responsiveness.

HandBrake supports H.264/H.265 on compatible NVIDIA, AMD, and Intel hardware.
AV1 availability depends on the GPU generation, driver, and what HandBrake
reports at startup. Update the vendor GPU driver if an expected encoder is not
listed. AMD VCN hardware decoding is not enabled by ByteSqueeze because the
current HandBrake VCN path is hardware encode only; video decode safely remains
on the CPU.

## Build from source

Run PowerShell from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build-windows-worker.ps1
```

The build downloads the official HandBrakeCLI release and a current FFmpeg
Windows essentials build, installs the Python packaging dependencies, and
creates a portable application folder and release archive:

```text
dist\windows-worker\ByteSqueezeWorker\ByteSqueezeWorker.exe
dist\windows-worker\ByteSqueeze-Windows-Worker.zip
```

For an offline/local tool build, put `HandBrakeCLI.exe`, `ffmpeg.exe`, and
`ffprobe.exe` on `PATH`, then use:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build-windows-worker.ps1 -UseInstalledTools
```

## Troubleshooting

- The worker listens on TCP port `8082` by default. Allow that port on private
  networks and do not expose it directly to the public internet. If pairing
  stays on Discovering, click **Allow controller access**, approve Windows,
  and retry with the exact URL displayed in the worker.
- Pairing codes expire after one hour. Generate a new code from the window or
  notification-area menu.
- The Compute panel shows the GPU driver, encoder families, CPU thread count,
  and work-disk free space. **Scan again** refreshes it after a driver update.
- Failed job logs remain in the persistent state directory and are also
  available from the controller's Queue screen.
- Hardware probes, ffprobe scans, and encoder launches run without opening
  transient Command Prompt windows behind the notification-area app.
