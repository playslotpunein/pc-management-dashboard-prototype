# PlaySlot CafeControl — Agent and Watchdog

Session enforcement for a managed café PC, in two processes.

**`PlaySlot.Agent`** runs in the user's interactive session and holds the lock: it blocks
input (Layer B) and covers every monitor with an overlay (Layer C).

**`PlaySlot.Watchdog`** runs as a Windows service in Session 0 and keeps the agent alive.
If the agent dies, is killed from Task Manager, or hangs, the watchdog relaunches it onto
the real desktop within seconds.

Together they cover phases 1 and 2 of the build order — enough to demo a lock that works
and survives a kill attempt.

| Layer | What | Status |
|---|---|---|
| **A** | Kernel filter driver — catches Ctrl+Alt+Del | Phase 4, not built |
| **B** | Low-level hooks — swallows everything else | ✅ `InputBlocker.cs` |
| **C** | Fullscreen overlay — what the customer sees | ✅ `LockOverlay.cs` |
| **D** | Policy hardening — standard user, no Task Manager | ✅ `scripts/harden.ps1` |
| — | Watchdog + auto-restart | ✅ `PlaySlot.Watchdog` |

---

## Why this is not just `Process.Start`

Two constraints shape the whole design.

**A service cannot draw on screen.** Session 0 has been isolated from the interactive
desktop since Windows Vista. Anything a service starts with `Process.Start` also runs in
Session 0 — invisible to the logged-in user and unable to show a window. Getting a process
onto the actual desktop means duplicating the console session's user token and calling
`CreateProcessAsUser` against window station `winsta0\default`. That is what
`SessionLauncher.cs` does.

**A hung process still has a PID.** Checking that the agent exists is not a health check —
an agent that has deadlocked looks identical to a healthy one. So health requires *both*
conditions:

| Signal | Catches |
|---|---|
| Process exists | Agent killed, crashed, or never started |
| Heartbeat fresher than 20s | Agent hung, deadlocked, or wedged |

The agent connects to a named pipe (`PlaySlotAgentHeartbeat`) every 5 seconds and writes
`alive`. The watchdog records the timestamp; anything older than the timeout counts as dead.

---

## Layout

```
PlaySlot.CafeControl.sln
├── PlaySlot.Watchdog/          The Windows service
│   ├── Program.cs              Host setup, Event Log wiring
│   ├── WatchdogWorker.cs       Monitor loop, health check, relaunch
│   ├── SessionLauncher.cs      Session 0 -> interactive desktop (P/Invoke)
│   ├── HeartbeatListener.cs    Named pipe server
│   ├── WatchdogOptions.cs      Tuning, bound from appsettings.json
│   └── appsettings.json
├── PlaySlot.Agent/             The client agent (interactive session)
│   ├── Program.cs              Entry, message pump, control dispatch
│   ├── AgentOptions.cs         Command-line configuration
│   ├── Log.cs                  Console + %LOCALAPPDATA%\PlaySlot\agent.log
│   ├── Lock/
│   │   ├── LockController.cs   Orchestrates B and C as one unit
│   │   ├── InputBlocker.cs     Layer B — WH_KEYBOARD_LL / WH_MOUSE_LL
│   │   ├── LockOverlay.cs      Layer C — one borderless topmost window
│   │   ├── OverlayManager.cs   One overlay per monitor, rebuilt on display change
│   │   └── PanicHatch.cs       Auto-release timer + emergency combo
│   └── Ipc/
│       ├── HeartbeatClient.cs  "alive" to the watchdog every 5s
│       ├── ControlServer.cs    Local lock/unlock pipe (stands in for the server)
│       └── ControlClient.cs    The --send side of that pipe
└── scripts/
    ├── publish.ps1
    ├── install.ps1
    ├── uninstall.ps1
    └── redeploy.ps1
```

Every script resolves paths from `$PSScriptRoot`, so the repo works from any location
after a clone. The only absolute path is the install target, exposed as `-InstallRoot`
at the top of `install.ps1`.

---

## Prerequisites

- **Windows 10/11**, x64. Home edition is fine — nothing here needs Group Policy.
- **.NET 10 SDK** — `dotnet --version` should report `10.x`.
- **Administrator rights** for install/uninstall (service registration).

---

## Build and install

```powershell
# 1. Normal prompt — no elevation needed
.\scripts\publish.ps1

# 2. ELEVATED prompt (right-click PowerShell > Run as administrator)
.\scripts\install.ps1
```

Or the whole loop in one command from an elevated prompt:

```powershell
.\scripts\redeploy.ps1
```

### Which scripts need elevation

| Script | Elevation | What it does |
|---|---|---|
| `publish.ps1` | **No** | Builds both projects to `publish/` |
| `install.ps1` | **Yes** | Copies to Program Files, registers + starts the service |
| `uninstall.ps1` | **Yes** | Stops, deletes the service, removes the install folder |
| `redeploy.ps1` | **Yes** | uninstall → publish → install |
| `harden.ps1` | **Yes** | Layer D — policy hardening for the customer account |
| `unharden.ps1` | **Yes** | Reverses `harden.ps1` |

Each script detects a non-elevated prompt and exits with a clear message rather than
failing halfway through.

---

## Verifying the agent landed in the interactive session

This is the thing worth checking, because the failure is silent: a wrongly-launched agent
runs perfectly happily in Session 0 where the customer can never see it.

**The visible check.** Within about 30 seconds of installing, a console window titled
*PlaySlot.Agent (test stub)* should appear on your desktop:

```
PlaySlot.Agent — test stub
----------------------------------------------------
  PID          : 7312
  Session Id   : 1   <- must be >= 1, not 0
  User         : CAFE-PC\gamer
  Interactive  : True
  Pipe         : PlaySlotAgentHeartbeat
----------------------------------------------------
[21:14:03] heartbeat sent
```

**`Session Id` is the answer.** `1` or higher means `CreateProcessAsUser` worked and the
agent is on the real desktop. `0` means it is stuck in Session 0 and the launch path is
broken. The agent prints an explicit warning in that case.

**The scripted check**, if you would rather not rely on the window:

```powershell
Get-Process PlaySlot.Agent | Select-Object Id, SessionId, ProcessName
```

Compare against the service itself, which should always be Session 0:

```powershell
Get-Process PlaySlot.Watchdog | Select-Object Id, SessionId
```

A correct deployment shows the watchdog at `SessionId 0` and the agent at `1` or higher.

---

## Layer D — policy hardening

Closes the bypasses that involve no code at all: killing the agent from Task Manager,
undoing the policy in regedit, and rebooting into safe mode.

```powershell
# ELEVATED prompt. The account must have signed in once and be signed out now.
.\scripts\harden.ps1 -UserName cafe
.\scripts\harden.ps1 -UserName cafe -WhatIf     # preview without changing anything
```

Windows Home has no Group Policy editor, so the same values `gpedit` would write are set
directly in the registry. One consequence matters: the policies live in the customer's
own `HKCU` hive, which a standard user can normally write to. The script therefore also
denies that user write access to the policy key — **without the ACL step the customer
just deletes the value and gets Task Manager back.**

Safe mode is handled by registering the watchdog under `SafeBoot\Minimal` and
`SafeBoot\Network` so it runs there too, rather than by trying to block safe mode. A lock
that survives safe mode beats a boot path you hope nobody finds.

Two guards, because this script is easy to point at the wrong account: it refuses to
harden the account running it, and refuses any account in Administrators.

| Attempt | Stopped by |
|---|---|
| Kill the agent in Task Manager | `DisableTaskMgr` + the watchdog restarting it |
| Delete that policy in regedit | `DisableRegistryTools` + the Deny ACL |
| Reboot into safe mode | Watchdog registered for safe boot; WinRE disabled |
| Boot from a USB stick | **Firmware password — must be set by hand** |

Reverse it all with `.\scripts\unharden.ps1 -UserName cafe`. The account itself is left
alone; delete it manually if you want it gone.

⚠️ The BIOS/UEFI supervisor password and boot order cannot be scripted. Without them the
machine boots off a stick and every layer above is irrelevant.

---

## The lock stack (Layers B and C)

### Read this before you run it

The agent blocks keyboard and mouse input and covers every monitor. Run it carelessly on
the machine holding your editor and you lock yourself out of your own desktop.

**Two independent escapes are on by default**, and you should leave them on until you
have a VM:

| Escape | Default | Turn off with |
|---|---|---|
| Auto-release after 60s | on | `--max-lock-seconds=0` |
| `Ctrl + Alt + Shift + U` | on | `--no-panic-combo` |

The combo works *while input is being swallowed*, because `InputBlocker` reports keys to
observers before it drops them. And because Layer B cannot touch the secure attention
sequence, **Ctrl+Alt+Del still works** — that is the last resort on a dev box, and it is
exactly the gap Layer A closes later.

The overlay prints the combo in its footer whenever it is enabled, so you can always see
your way out. Production runs with `--no-panic-combo --max-lock-seconds=0`, and then the
footer shows only the unit id.

### Driving it

There is no server yet, so lock and unlock arrive over a local named pipe. The same
binary is the client:

```powershell
.\PlaySlot.Agent.exe --send lock
.\PlaySlot.Agent.exe --send unlock
.\PlaySlot.Agent.exe --send status
```

Or lock on a delay for a hands-off demo:

```powershell
.\PlaySlot.Agent.exe --lock-after=10 --unit=PC-04
```

⚠️ This pipe is **Phase 1 scaffolding**. It has no authentication unless you pass
`--control-token`, which means any process in the same session can send `unlock`. Zone 2's
HMAC-signed WebSocket replaces it; run with `--no-control-pipe` once that lands.

### What each layer catches

| Attempt | Caught by | Notes |
|---|---|---|
| Alt+Tab, Windows key, Alt+F4 | Layer B | Swallowed before the shell reacts |
| Clicking anything | Layer B | Buttons and wheel dropped; movement allowed by default |
| Seeing the desktop | Layer C | Borderless topmost, every monitor |
| Fullscreen game on top | Layer C | Z-order re-asserted once a second |
| Plugging in a second monitor | Layer C | Overlay rebuilt on display change |
| **Ctrl+Alt+Del** | **nothing yet** | Needs Layer A (Phase 4) |
| Killing the agent | Watchdog | Relaunched within ~5s |

Mouse *movement* passes through on purpose — the overlay already covers everything, and a
frozen cursor reads as a crashed machine. `--block-mouse-move` changes that.

---

## Testing the failure paths

The agent has flags for driving the watchdog's recovery paths on demand.

**Killed agent** — the Task Manager case:

```powershell
Stop-Process -Name PlaySlot.Agent -Force
```

A new agent window should appear within ~5 seconds.

**Hung agent** — process alive, no heartbeat. This is the case process-existence checks
miss. Set the argument in `appsettings.json` next to the installed exe, then restart:

```jsonc
// C:\Program Files\PlaySlot\Watchdog\appsettings.json
"AgentArguments": "--no-heartbeat"
```

```powershell
Restart-Service PlaySlotWatchdog
```

The agent starts, never beats, and the watchdog kills and relaunches it roughly every
20–30 seconds. The Event Log entry reads
`process alive but last heartbeat was 21s ago (limit 20s)`.

Use `--hang-after=30` instead to beat normally and then stop, simulating an agent that
wedges partway through a session.

**Service auto-restart** — recovery actions are configured for restart 5s after each of
the first three failures:

```powershell
sc.exe qfailure PlaySlotWatchdog
```

---

## Configuration

`appsettings.json` sits next to the installed exe and is read at service start, so it can
be edited in place — no rebuild, just `Restart-Service PlaySlotWatchdog`.

| Setting | Default | Notes |
|---|---|---|
| `AgentPath` | `..\Agent\PlaySlot.Agent.exe` | Relative to the service exe, **not** the working directory |
| `AgentArguments` | *(empty)* | Passed through on relaunch |
| `PollSeconds` | `5` | Health check interval |
| `HeartbeatTimeoutSeconds` | `20` | Older than this ⇒ hung |
| `GraceSeconds` | `30` | Checks suspended after a relaunch. Must exceed the heartbeat timeout, or the watchdog kills what it just started |
| `NoSessionRetrySeconds` | `15` | Back-off when nobody is logged in |
| `ShowAgentWindow` | `true` | Console window for the stub. Set `false` for the real agent |

---

## Logs

Service output goes to the Windows Event Log, so it is readable with nobody logged in:

**Event Viewer → Windows Logs → Application**, source `PlaySlotWatchdog`.

Or from a prompt:

```powershell
Get-WinEvent -LogName Application -MaxEvents 40 |
    Where-Object ProviderName -eq 'PlaySlotWatchdog' |
    Format-List TimeCreated, LevelDisplayName, Message
```

Running the exe directly from a console instead of as a service works too, which is the
easiest way to debug it:

```powershell
& 'C:\Program Files\PlaySlot\Watchdog\PlaySlot.Watchdog.exe'
```

---

## Troubleshooting

**Agent never starts, log says "no active console session".**
Nobody is logged in, or the machine is at the lock screen. Expected — the watchdog backs
off and retries every 15 seconds.

**Agent starts but no heartbeats arrive.**
Almost always pipe permissions. The service runs as LocalSystem while the agent runs as a
standard user, and a default pipe DACL rejects that. `HeartbeatListener.BuildPipeSecurity`
grants Authenticated Users explicitly — if you change that code, this is the symptom.

**Service will not start, exit code 1053.**
Usually a missing `appsettings.json` next to the exe, or a corrupt install. Run the exe
directly from a console to see the real exception.

**`sc.exe` says "The specified service already exists".**
Run `uninstall.ps1`, or use `redeploy.ps1`. If the service is stuck in *marked for
deletion*, close Services.msc and Event Viewer — an open handle blocks removal.

**Uninstall leaves the agent running.**
By design — the uninstall does not kill user processes unless asked. Re-run with
`-StopAgent`.

---

## Agent command line

```
Lock control
  --lock-after=<secs>        lock automatically N seconds after start (demo)
  --max-lock-seconds=<secs>  panic hatch: auto-release after N seconds (0 = never)
  --no-panic-combo           disable the Ctrl+Alt+Shift+U emergency release
  --block-mouse-move         swallow mouse movement as well as clicks
  --unit=<id>                unit id shown on the overlay
  --lock-title=<text>        overlay heading
  --lock-message=<text>      overlay body text

Control channel (Phase 1 stand-in for the server WebSocket)
  --send=<lock|unlock|status|ping>   send a command to a running agent, then exit
  --control-pipe=<name>      pipe name (default PlaySlotAgentControl)
  --control-token=<secret>   require this token on inbound commands
  --no-control-pipe          do not listen for commands at all

Watchdog contract
  --pipe=<name>              heartbeat pipe (default PlaySlotAgentHeartbeat)
  --interval=<secs>          heartbeat interval (default 5)
  --no-heartbeat             never heartbeat — tests the watchdog's hung path
  --hang-after=<secs>        heartbeat, then stop after N seconds
```

Arguments reach the installed agent through the watchdog's `AgentArguments` setting.

### Production settings

The defaults are tuned for a development machine. A real venue unit wants:

```jsonc
"AgentArguments": "--unit=PC-04 --max-lock-seconds=0 --no-panic-combo --no-control-pipe",
"ShowAgentWindow": false
```

That leaves the server as the only thing that can release a lock — which is the point, and
also why you should not set it until the server exists.

---

## What is not built yet

- **Layer A** (kernel filter driver) — Ctrl+Alt+Del is still open. Phase 4, via the
  Interception library first.
- **Layer D** (policy hardening) — standard user, Task Manager disabled, safe boot off.
  Phase 2's second half. On Windows Home this is registry work, not Group Policy.
- **The server** — the session engine that decides *when* to lock. Until Phase 3, the
  control pipe stands in for it.

The agent is deliberately the executor, not the decider: it carries out `lock` and
`unlock` and works out nothing on its own. That is what lets the WebSocket drop in behind
`ControlServer` later without touching the lock code.
