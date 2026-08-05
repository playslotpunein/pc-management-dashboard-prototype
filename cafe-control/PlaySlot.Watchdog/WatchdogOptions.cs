namespace PlaySlot.Watchdog;

/// <summary>
/// Watchdog tuning, bound from the "Watchdog" section of appsettings.json.
/// Every value has a working default so the service still runs if the file is missing.
/// </summary>
internal sealed class WatchdogOptions
{
    public const string SectionName = "Watchdog";

    /// <summary>Windows service key. Must match the name used by install.ps1.</summary>
    public const string ServiceName = "PlaySlotWatchdog";

    /// <summary>Named pipe the agent connects to in order to heartbeat.</summary>
    public string HeartbeatPipeName { get; set; } = "PlaySlotAgentHeartbeat";

    /// <summary>
    /// Path to the agent executable. Relative paths resolve against the service's own
    /// directory, not the working directory — a service starts in System32.
    /// </summary>
    public string AgentPath { get; set; } = @"..\Agent\PlaySlot.Agent.exe";

    /// <summary>Arguments passed to the agent on relaunch.</summary>
    public string AgentArguments { get; set; } = string.Empty;

    /// <summary>Process name used to find running agents. No ".exe" suffix.</summary>
    public string AgentProcessName { get; set; } = "PlaySlot.Agent";

    /// <summary>How often the health check runs.</summary>
    public int PollSeconds { get; set; } = 5;

    /// <summary>
    /// A heartbeat older than this means the agent is hung. Must be comfortably
    /// larger than the agent's own beat interval or healthy agents get killed.
    /// </summary>
    public int HeartbeatTimeoutSeconds { get; set; } = 20;

    /// <summary>
    /// Health checks are suspended for this long after a relaunch, giving the new
    /// agent time to start and land its first heartbeat. Keep it above
    /// <see cref="HeartbeatTimeoutSeconds"/> or the watchdog will kill what it just started.
    /// </summary>
    public int GraceSeconds { get; set; } = 30;

    /// <summary>
    /// Back-off after a launch attempt that could not proceed — normally because
    /// nobody is logged in. Avoids hammering the console session at the lock screen.
    /// </summary>
    public int NoSessionRetrySeconds { get; set; } = 15;

    /// <summary>
    /// Give the agent a console window in the user's session. Useful while testing
    /// (you can see it appear on the desktop); turn off for the real agent.
    /// </summary>
    public bool ShowAgentWindow { get; set; } = true;

    /// <summary>Resolves <see cref="AgentPath"/> against the service directory.</summary>
    public string ResolveAgentPath() =>
        Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, AgentPath));
}
