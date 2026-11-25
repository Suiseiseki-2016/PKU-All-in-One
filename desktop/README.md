# PKU All in Notion — desktop shell

Tauri 2 thin shell: **download one installer → install → double-click → desktop window**.

Production ships a **bundled relocatable Python runtime** + thin `pku-sync` sidecar
(`externalBin`). Dev mode (`tauri dev`) stages a stub sidecar and uses PATH
`pku-sync`.

## Runtime flow

1. Resolve panel binary (bundled sidecar / bundled `python -m pku_sync` / PATH)
2. CWD = `%USERPROFILE%\PKU-All-in-Notion` (or `~/PKU-All-in-Notion`)
3. Spawn `panel --no-browser`
4. Poll `GET http://127.0.0.1:8791|8792|8793/healthz` (~30s)
5. Navigate the system WebView to `/app`
6. Single-instance lock; kill the panel process tree on window close
7. Append launcher lines to `panel.log` in the app dir

## Prerequisites

| Tool | Notes |
| --- | --- |
| Rust (stable) | `rustup` / `cargo` |
| Node.js + npm | For `@tauri-apps/cli` |
| uv | Stages the private Python runtime on Windows and macOS release builds |
| WebView2 | **Windows** — Evergreen Runtime (NSIS can bootstrap) |
| `pku-sync` on PATH | **Dev only** (`tauri dev` fallback) |

**First-party acceptance is Windows.** WSL/Linux is useful for editing and for
`cargo build --bin pku-sync-sidecar`, but often lacks WebView2 / GTK–WebKit
`-dev` packages; do not treat WSL `tauri build` failure as a product blocker.

macOS release builds run on an Apple Silicon Mac and produce a native DMG.

## Develop (PATH `pku-sync`)

```bash
cd desktop
npm install
# optional explicit stub: npm run stage-sidecar:dev
npm run tauri dev
```

`beforeDevCommand` runs `stage-sidecar:dev` (builds the thin sidecar; no Python
tree). The shell then finds `pku-sync` via `UV_TOOL_BIN_DIR` → `~/.local/bin` →
`PATH`.

### Windows WebView check

发布候选的桌面生命周期检查使用 `desktop/scripts/smoke-release.ps1`。默认创建全新
临时数据目录，保留学生目录；也可指定不存在的绝对路径 `-AppDataDir` 和新的
`-ResultPath` 保存结果。检查实际后台进程身份、两次启动、单实例、无辅助窗口及
正常关窗释放。这个烟测不代替真实登录、同步、学习流程及完整发布验收。

```bat
cd desktop
npm install
npm run tauri dev
```

Expect loading copy 「正在启动本机服务…」, then `/app` inside the window; second
launch focuses the existing window; close frees ports `8791–8793`.

## Build on Windows (release)

```powershell
# Optional: produce dist\pku_course_sync-*-py3-none-any.whl for a pinned runtime install
uv build

cd desktop
npm install
npm run build
```

`beforeBuildCommand` runs `stage-sidecar`, which:

1. `cargo build --release --bin pku-sync-sidecar` → `src-tauri/binaries/pku-sync-x86_64-pc-windows-msvc.exe`
2. Reuse or install a uv-managed Python 3.11, then copy the complete interpreter into `src-tauri/resources/runtime`
3. Build a wheel if needed and install it with its dependencies into that private runtime

Tauri then embeds `externalBin` + `resources/runtime` and produces:

- **NSIS setup (primary):** `src-tauri\target\release\bundle\nsis\*-setup.exe`
- **Updater signature:** matching `*.exe.sig` in the same directory
- Unpacked exe + sidecar + resources under `src-tauri\target\release\`

### On-demand recording notes

从「我的课程」保存教学网账号并更新课程目录，然后在设置中连接 Notion、选择保存位置并创建学习主页。打开课程后按需整理一节课堂录像；同步目录不会下载或转写媒体。同步失败时可以重试，已保存账号和 Notion 连接会保留。

### Campus-first learning home

The student dashboard uses Teaching Network course, recording, and attachment-name metadata as its starting point. Syncing the catalog does not download videos or attachments. Recordings define the lesson sequence; slides are linked only when their lecture number matches exactly one recording. Uncertain slides stay at course level for review. A recording is downloaded, transcribed, and published only after its own confirmation.

For the shortest first-run Notion flow, configure the public Notion connection with an optional **blank learning-home template** in the Notion Developer portal. In the OAuth prompt the student can duplicate that template; Notion shares the new page with the connection and returns `duplicated_template_id`, which the desktop app adopts as the learning home. Standard OAuth instead requires the student to share a page; the desktop app then lets them select that authorized page and creates a learning home beneath it. Notion's standard public OAuth does not grant general workspace-wide page access.
### Automatic updates

Automatic update checks are disabled by default in the open-source BYOK build (`plugins.updater.endpoints` is empty in `src-tauri/tauri.conf.json`). If you host your own signed Tauri releases, configure `endpoints` and `pubkey` in `src-tauri/tauri.conf.json` and sign builds with `TAURI_SIGNING_PRIVATE_KEY` and `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`.

### Optional Inno wrapper (Chinese wizard)

```powershell
powershell -File desktop\scripts\stage-inno-payload.ps1
& "D:\Inno Setup 6\ISCC.exe" installer\windows\pku-all-in-notion.iss
```

Output: `dist\PKU-All-in-Notion-Setup-<version>.exe`

See `installer/windows/README.md`.

## Build on macOS (Apple Silicon)

Prerequisites: Xcode Command Line Tools, Rust, Node.js/npm and uv.

```bash
uv build --wheel
cd desktop
npm ci
npm run build:macos
```

The build stages a complete private Python 3.11 runtime, then bundles it with
the Tauri window and sidecar. The build target defaults to
`/private/tmp/pku-all-in-notion-target` to avoid iCloud/Finder metadata
breaking code signing. Set `PKU_MACOS_TARGET_DIR` for another local volume.

Local DMGs use ad-hoc signing. Public distribution requires Developer ID
Application signing and Apple notarization.

## What the user double-clicks

1. **Install:** `PKU-All-in-Notion-Setup-*.exe` (NSIS or Inno) or macOS DMG
2. **Daily use:** Start Menu / Desktop **「PKU All in One」** → Tauri exe

Data/credentials remain under `%USERPROFILE%\PKU-All-in-Notion` (or `~/PKU-All-in-Notion`).

## Layout

| Path | Role |
| --- | --- |
| `src/` | Loading / error UI before `/app` |
| `src-tauri/` | Spawn, healthz, navigate, single-instance, cleanup |
| `sidecar-launcher/` | Standalone Rust crate → `externalBin` `pku-sync` (no Tauri/GTK deps) |
| `src-tauri/binaries/` | Staged `pku-sync-<triple>.exe` (`externalBin`) |
| `src-tauri/resources/runtime/` | Staged relocatable Python + `pku-sync` package |
| `scripts/stage-sidecar.*` | Build-host staging for sidecar + runtime |
| `scripts/stage-inno-payload.ps1` | Copy release tree into `installer/windows/payload` |

## Local release verification

```powershell
uv sync --extra dev
uv run python -m compileall -q pku_sync
uv run python -m pku_sync.panel.bundle_frontend --check
uv run python scripts/check_frontend_js.py
uv run pytest -q
uv build --wheel
cd desktop
npm ci
npm run build
```
