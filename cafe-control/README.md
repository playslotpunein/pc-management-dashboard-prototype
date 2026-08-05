# PlaySlot CafeControl — Watchdog

A Windows service that keeps the PlaySlot client agent alive on a managed café PC.

The service runs in **Session 0** and monitors `PlaySlot.Agent.exe`, which runs in the
user's **interactive session**. If the agent dies, is killed from Task Manager, or hangs,
the watchdog relaunches it onto the real desktop within seconds.

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
├── PlaySlot.Agent/             Test stub — the real agent comes later
│   └── Program.cs
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
broken. The stub prints an explicit warning in that case.

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

## Testing the failure paths

The stub agent has flags for driving the watchdog's recovery paths on demand.

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

## Notes for the real agent

The stub exists only to exercise the watchdog. When the real agent replaces it, keep:

- the **heartbeat contract** — connect to `PlaySlotAgentHeartbeat`, write `alive`,
  disconnect, at a shorter interval than `HeartbeatTimeoutSeconds`;
- the **process name** `PlaySlot.Agent`, or update `AgentProcessName`;
- `ShowAgentWindow: false`, so it runs without a console window.

The watchdog does not care what the agent does beyond that.
