# DSH Version Manager · User Guide

> This is the English edition of `docs/dsh管理器使用说明.md`. The Chinese edition is
> the authoritative one when they differ. Other documents under `docs/` are currently
> Chinese only.

## 1. What it manages

The manager runs **several mutually isolated DeepSeek Harness (dsh) versions on one
Windows machine** and gives you one window to drive them all.

Each version gets its own **slot** with:

| | Per-slot |
|---|---|
| Process | its own `node` process |
| Port | its own `--port` (4080 / 5080 / 7080 …) |
| Data | its own `DSH_HOME` (`.dsh-home` inside the slot) |
| Runtime | its own bundled `node` |
| Temp | its own `TEMP`/`TMP` |

Nothing is shared, so a broken test version cannot touch the one you use daily.
**Up to 6 slots.**

## 2. Requirements

- Windows 10 / 11
- The bundled `runtime\python\pythonw.exe` (3.8+ with tkinter), or a system Python with tkinter
- Node.js is **not** required on the host — each slot carries its own
- ~0.7–1.3 GB of disk per version

## 3. Install and first launch

1. Unpack the release anywhere (e.g. `D:\DSH-TM`).
2. Double-click `start-manager.bat` (or the desktop shortcut).
3. **The first install or adopt asks where to put versions** — this location is then
   locked. To change it later use **Settings → Directories → Move install root…**
   (it moves every version together with its data; no uninstall needed).

### Desktop shortcut (optional, recommended)

The manager does **not** create a desktop icon by itself — after unzipping you only have
`start-manager.bat`, and going into the folder every time is tedious. Two ways:

| Way | How | Notes |
|---|---|---|
| **Run the script** (recommended) | `python build_lnk.py` in the manager directory | Creates `dsh-manager.lnk` with the dsh icon. Standard library only. **Portable package users: `runtime\python\python.exe build_lnk.py`** |
| **Manual shortcut** | Right-click `start-manager.bat` → Send to → Desktop (create shortcut) | No script needed, but the icon is the plain `.bat` icon |

> The shortcut stores an **absolute path**. If you later move the folder, run
> `build_lnk.py` again.

## 4. Interface

### 4.1 Cards

Each slot is one card:

- Title: `≡ dshV1 · 0.2.2-rc.3` (slot name · actually installed version)
- Status line: `Stopped :4080` / `Running pid=1234 :4080 ✓` / `Starting …`
- Buttons: **Start / Stop**, **Open browser** (carries the login token),
  **Copy URL**, **Program dir**, **Data dir**

Drag a card to reorder. Hidden slots keep their process, port and data — they are just
not drawn.

### 4.2 Settings

Everything that used to be a row of buttons now lives in **Settings**:

- **Version management** — Install official dsh / Scan and adopt / Uninstall
- **Interface** — which slots to show, order, browser, theme, interface language
- **Directories** — install root, temp root, move
- **Maintenance** — environment check, slot health check, regenerate bats
- **Log** — view, clear, open the log file

### 4.3 Slot health check

Reports how the current state differs from what is registered. **Read-only by default.**
Pick a slot and click **Repair…** to fill in only what is missing; nothing you already
have is deleted or overwritten. The confirmation dialog lists exactly which files will
be created.

| Missing | What **Repair…** does |
|---|---|
| `node/node.exe` | Copies one from another slot or the bundled runtime |
| `.dsh-home` / `.tmp` | Creates empty directories (sessions / credentials do not come back) |
| `dsh.bat`, `dsh-cli.bat`, `kill-<port>.bat` | Generates **only the missing ones** from the current config; existing bats — including ones you edited by hand — are never touched |
| `lib/bin.js`, dependencies, dsh's `package.json` | **Not repaired** (that would be a reinstall); use "Install official dsh" |

Repair runs the health check again afterwards and reports anything it could not fix,
instead of claiming success. **Note:** a bat whose *contents* are wrong (port / `TEMP`
not matching the config) is outside Repair's scope — it only fills in missing files,
never rewrites existing ones. Use Settings → Maintenance →
"Generate start / kill / CLI bats" for that.

### 4.4 A dsh started outside the manager

If something else started dsh on the same port, the card shows
`Running (external) pid=…`. The manager will **never** kill a process it did not
start unless you explicitly click Stop and confirm.

## 5. Adding slots

### 5.1 Install an official version

Settings → Version management → **Install official dsh** → pick a version (or type
`latest` / `next` / a specific number). It downloads from the npm registry, and falls
back to `registry.npmmirror.com` automatically on `ECONNRESET` / timeout.

**You can keep using other versions while it installs** — the progress window does not
lock the main window.

### 5.2 Scan and adopt an existing dsh

Settings → Version management → **Scan and adopt**. Two paths per entry:

- **Re-attach in place** — only edits config (seconds). Used for slot directories that
  already sit under the version root.
- **Copy** — copies into the version root (hundreds of MB, slower). The original is
  left untouched.

Both paths run the same health check: a missing `bin.js` or missing dependencies is
refused with a clear reason instead of "looks fine, explodes on start". **Neither path
repairs anything** in the source install — adopting only copies and registers a dsh
that already runs correctly.

When a detached slot is re-attached in place, the manager regenerates **only the
missing** launch bats afterwards (and rewrites all three if the port changed); bats you
edited by hand are never overwritten.

## 6. Removing slots

### 6.1 Uninstall

**Uninstall** stops the process, backs up user data, then deletes the whole slot
directory.

Backed up: `.dsh-home` (sessions / credentials / plugin config), `package.json`,
version info. **Not** backed up: `node_modules`, `node`, and anything **you** put in
the slot directory — those are deleted too. The confirmation dialog names them.

### 6.2 Detach

**Detach** only removes the slot from `config.json`. Not one byte on disk changes.
`dsh.bat` / `kill-<port>.bat` / `dsh-cli.bat` stay in the slot, so double-clicking
still starts it. Detach it and later re-adopt it as-is (same port, no copying).

### 6.3 CLI equivalents

```bat
python dsh-tm.py list
python dsh-tm.py start --all
python dsh-tm.py stop --all
python dsh-tm.py doctor --json     :: one-shot read-only diagnosis
python dsh-tm.py kill-stray
```

`doctor --json` merges config validation, the health check and the slot-shape check
into one read-only report. Every issue carries a fixed English `code` / `source` /
`action`; every slot gets `level` and `actions[]`. All JSON results also carry
`config_problems[]` — a `port` / `--port` mismatch is reported there and makes
`start` / `stop` refuse the affected slot. Every result also carries `effect`:
`read` or `runtime_write` (process state only). The CLI never emits `disk_write` —
install / adopt / uninstall stay in the GUI. An unknown slot name is listed in
`unknown_slots[]`. Full field and error-code reference:
[Agent API contract](agent-api.md).

## 7. Logs

`dsh-manager.log` next to the program. It includes dsh's own stdout/stderr.
Tokens in entry URLs are masked in place (same length, file size unchanged).

## 8. Configuration (`config.json`)

Generated on first run from `config/config.example.json`.

### Placeholders

| Placeholder | Expands to |
|---|---|
| `<base>` | the install root |
| `<home>` | the user profile |
| `<self>` | this manager's directory |
| `<npm_global>` | the npm global prefix |

Write paths with placeholders so the whole tree can be moved or copied to another machine.

### Per-slot fields

| Field | Meaning |
|---|---|
| `label` | fixed slot name (`dshV1`), shown in the card title |
| `port` | used for the pre-launch check and status display |
| `extra_args` | real launch arguments — **`--port` here is what actually listens** |
| `bin_js`, `node_exe` | entry point and runtime, both per-slot |
| `dsh_home`, `temp_dir` | data directory and temp directory |

### Three rules for hand-editing

1. `port` and `extra_args`'s `--port` must be **the same**, or the status never updates.
2. Keep `<base>` / `<home>` placeholders — do not write absolute drive letters.
3. Stop the manager before editing (it keeps config in memory).

## 9. Security and privacy

- **The log contains your Windows username and absolute paths.** Redact before posting.
- The `dsh备份` backup folder may contain **plaintext credentials** (`.dsh-home`).
- **Copy URL** puts a token into the clipboard — do not share it.
- `config.json` holds absolute paths.
- The manager never terminates processes it did not start, and never deletes outside
  the slot root.

See [SECURITY.md](../SECURITY.md) for how to report an issue privately.

## 10. FAQ

**Nothing happens when I double-click.** Check `dsh-manager.log`. If tkinter is missing
you get a native message box explaining it (the `.pyw` has no console).

**A version shows "Stopped" but the port is busy.** Something else holds the port. The
card shows the owner; the manager will not kill it.

**I uninstalled a version but a `dshV2-2` appeared later.** A leftover directory held
the number. New installs skip numbers whose directories still exist on disk.

**Can I run two versions at once?** Yes — that is the whole point. Each has its own
port, data directory and runtime.

**The interface is in English but the docs are Chinese.** The English user guide is
this file; other documents under `docs/` are Chinese only for now.

## 11. Related documents

| Document | Language |
|---|---|
| [README](../README.md) | Chinese |
| [FAQ](常见问题.md) | Chinese |
| [Security policy](../SECURITY.md) | English |
| [Contributing](../CONTRIBUTING.md) | English + Chinese |
