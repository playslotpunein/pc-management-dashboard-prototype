namespace PlaySlot.Agent;

/// <summary>
/// Command-line configuration. Every value has a default that is safe to run on a
/// development machine — notably <see cref="MaxLockSeconds"/>, which releases the lock
/// by itself so a bug cannot strand you behind your own overlay.
/// </summary>
internal sealed class AgentOptions
{
    // ---- Watchdog contract (do not change without updating the watchdog) ----

    public string HeartbeatPipeName { get; private set; } = "PlaySlotAgentHeartbeat";
    public int HeartbeatIntervalSeconds { get; private set; } = 5;
    public bool HeartbeatEnabled { get; private set; } = true;

    /// <summary>Stop heartbeating after N seconds. 0 disables. Used to test the hung path.</summary>
    public int HangAfterSeconds { get; private set; }

    // ---- Control channel ----

    /// <summary>
    /// Local pipe used to drive lock/unlock while there is no server. This is Phase 1
    /// scaffolding: Zone 2's HMAC-signed WebSocket replaces it, at which point the pipe
    /// should be switched off with --no-control-pipe.
    /// </summary>
    public string ControlPipeName { get; private set; } = "PlaySlotAgentControl";

    public bool ControlPipeEnabled { get; private set; } = true;

    /// <summary>Shared secret the sender must present. Empty means no check.</summary>
    public string ControlToken { get; private set; } = string.Empty;

    // ---- Lock behaviour ----

    public string UnitId { get; private set; } = "UNASSIGNED";

    public string LockTitle { get; private set; } = "Session ended";

    public string LockMessage { get; private set; } = "Please see the counter to continue.";

    /// <summary>
    /// Panic hatch. The lock releases itself after this many seconds no matter what.
    /// 0 disables the auto-release — production only, and only once you trust it.
    /// </summary>
    public int MaxLockSeconds { get; private set; } = 60;

    /// <summary>
    /// Emergency release combo (Ctrl+Alt+Shift+U). The other half of the panic hatch.
    /// Disable for production, where the only way out should be the server.
    /// </summary>
    public bool PanicComboEnabled { get; private set; } = true;

    /// <summary>
    /// Swallow mouse movement as well as clicks. Off by default: the overlay already
    /// covers everything, and a frozen cursor reads as a crashed machine.
    /// </summary>
    public bool BlockMouseMovement { get; private set; }

    /// <summary>Lock immediately on start, after N seconds. 0 disables. For demos.</summary>
    public int LockAfterSeconds { get; private set; }

    // ---- Client mode ----

    /// <summary>When set, the process sends this command to a running agent and exits.</summary>
    public string? SendCommand { get; private set; }

    public static AgentOptions Parse(string[] args)
    {
        var options = new AgentOptions();

        options.SendCommand = GetValue(args, "--send");

        if (GetValue(args, "--pipe") is { } pipe) options.HeartbeatPipeName = pipe;
        if (GetValue(args, "--control-pipe") is { } control) options.ControlPipeName = control;
        if (GetValue(args, "--control-token") is { } token) options.ControlToken = token;
        if (GetValue(args, "--unit") is { } unit) options.UnitId = unit;
        if (GetValue(args, "--lock-title") is { } title) options.LockTitle = title;
        if (GetValue(args, "--lock-message") is { } message) options.LockMessage = message;

        options.HeartbeatIntervalSeconds =
            ParseInt(GetValue(args, "--interval"), options.HeartbeatIntervalSeconds);
        options.HangAfterSeconds = ParseInt(GetValue(args, "--hang-after"), 0);
        options.MaxLockSeconds = ParseInt(GetValue(args, "--max-lock-seconds"), options.MaxLockSeconds, allowZero: true);
        options.LockAfterSeconds = ParseInt(GetValue(args, "--lock-after"), 0);

        if (HasFlag(args, "--no-heartbeat")) options.HeartbeatEnabled = false;
        if (HasFlag(args, "--no-control-pipe")) options.ControlPipeEnabled = false;
        if (HasFlag(args, "--no-panic-combo")) options.PanicComboEnabled = false;
        if (HasFlag(args, "--block-mouse-move")) options.BlockMouseMovement = true;

        return options;
    }

    public static string UsageText => """
        PlaySlot.Agent — client agent (Phase 1: Layers B and C)

          Lock control
            --lock-after=<secs>       lock automatically N seconds after start (demo)
            --max-lock-seconds=<secs> panic hatch: auto-release after N seconds (0 = never)
            --no-panic-combo          disable the Ctrl+Alt+Shift+U emergency release
            --block-mouse-move        swallow mouse movement as well as clicks
            --unit=<id>               unit id shown on the overlay
            --lock-title=<text>       overlay heading
            --lock-message=<text>     overlay body text

          Control channel (Phase 1 stand-in for the server WebSocket)
            --send=<lock|unlock|status|ping>   send a command to a running agent, then exit
            --control-pipe=<name>     pipe name (default PlaySlotAgentControl)
            --control-token=<secret>  require this token on inbound commands
            --no-control-pipe         do not listen for commands at all

          Watchdog contract
            --pipe=<name>             heartbeat pipe (default PlaySlotAgentHeartbeat)
            --interval=<secs>         heartbeat interval (default 5)
            --no-heartbeat            never heartbeat — tests the watchdog's hung path
            --hang-after=<secs>       heartbeat, then stop after N seconds

          --help                      show this text
        """;

    private static bool HasFlag(string[] args, string name) =>
        args.Any(a => string.Equals(a, name, StringComparison.OrdinalIgnoreCase));

    /// <summary>Accepts both "--opt=value" and "--opt value".</summary>
    private static string? GetValue(string[] args, string name)
    {
        for (var i = 0; i < args.Length; i++)
        {
            if (args[i].StartsWith(name + "=", StringComparison.OrdinalIgnoreCase))
            {
                return args[i][(name.Length + 1)..];
            }

            if (string.Equals(args[i], name, StringComparison.OrdinalIgnoreCase) && i + 1 < args.Length)
            {
                return args[i + 1];
            }
        }

        return null;
    }

    private static int ParseInt(string? value, int fallback, bool allowZero = false)
    {
        if (!int.TryParse(value, out var parsed))
        {
            return fallback;
        }

        return parsed > 0 || (allowZero && parsed == 0) ? parsed : fallback;
    }
}
