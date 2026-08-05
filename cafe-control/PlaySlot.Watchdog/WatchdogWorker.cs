using System.Diagnostics;
using Microsoft.Extensions.Options;

namespace PlaySlot.Watchdog;

/// <summary>
/// The monitor loop. Checks agent health on a fixed interval and, when it fails,
/// clears out whatever is left of the old agent and relaunches into the interactive
/// session. The loop is written so that no single failure can end it — a watchdog
/// that dies quietly is worse than no watchdog.
/// </summary>
internal sealed class WatchdogWorker : BackgroundService
{
    private readonly ILogger<WatchdogWorker> _log;
    private readonly WatchdogOptions _options;
    private readonly HeartbeatListener _heartbeat;
    private readonly SessionLauncher _launcher;

    public WatchdogWorker(
        ILogger<WatchdogWorker> log,
        IOptions<WatchdogOptions> options,
        HeartbeatListener heartbeat,
        SessionLauncher launcher)
    {
        _log = log;
        _options = options.Value;
        _heartbeat = heartbeat;
        _launcher = launcher;
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        var agentPath = _options.ResolveAgentPath();

        _log.LogInformation(
            "Watchdog started. Agent={Agent} Poll={Poll}s HeartbeatTimeout={Timeout}s Grace={Grace}s",
            agentPath, _options.PollSeconds, _options.HeartbeatTimeoutSeconds, _options.GraceSeconds);

        if (_options.GraceSeconds <= _options.HeartbeatTimeoutSeconds)
        {
            _log.LogWarning(
                "GraceSeconds ({Grace}) is not greater than HeartbeatTimeoutSeconds ({Timeout}); " +
                "a freshly launched agent may be killed before its first heartbeat lands",
                _options.GraceSeconds, _options.HeartbeatTimeoutSeconds);
        }

        // An agent may already be running from a previous service lifetime. Hold off
        // long enough for it to check in rather than killing a perfectly healthy one.
        var suspendChecksUntil = DateTime.UtcNow.AddSeconds(_options.HeartbeatTimeoutSeconds);

        while (!stoppingToken.IsCancellationRequested)
        {
            try
            {
                if (DateTime.UtcNow >= suspendChecksUntil)
                {
                    if (!IsAgentHealthy(out var reason))
                    {
                        _log.LogWarning("Agent unhealthy: {Reason}", reason);

                        KillStaleAgents();

                        var pid = _launcher.LaunchInActiveSession(agentPath, _options.AgentArguments);

                        suspendChecksUntil = pid is null
                            // No interactive session yet — back off rather than spin.
                            ? DateTime.UtcNow.AddSeconds(_options.NoSessionRetrySeconds)
                            : DateTime.UtcNow.AddSeconds(_options.GraceSeconds);
                    }
                }
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                break;
            }
            catch (Exception ex)
            {
                // Swallow and keep going. The loop must outlive any individual fault.
                _log.LogError(ex, "Monitor iteration failed; continuing");
            }

            try
            {
                await Task.Delay(TimeSpan.FromSeconds(_options.PollSeconds), stoppingToken)
                    .ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                break;
            }
        }

        _log.LogInformation("Watchdog stopping");
    }

    /// <summary>
    /// Healthy requires both conditions. Process existence alone is not enough:
    /// a hung agent still holds its PID while doing nothing, which is exactly the
    /// case the heartbeat exists to catch.
    /// </summary>
    private bool IsAgentHealthy(out string reason)
    {
        if (CountAgentProcesses() == 0)
        {
            reason = "no agent process running";
            return false;
        }

        var age = _heartbeat.TimeSinceLastHeartbeat;

        if (age is null)
        {
            reason = "no heartbeat received since service start";
            return false;
        }

        if (age.Value.TotalSeconds > _options.HeartbeatTimeoutSeconds)
        {
            reason = $"process alive but last heartbeat was {age.Value.TotalSeconds:F0}s ago " +
                     $"(limit {_options.HeartbeatTimeoutSeconds}s)";
            return false;
        }

        reason = string.Empty;
        return true;
    }

    private int CountAgentProcesses()
    {
        var processes = Process.GetProcessesByName(_options.AgentProcessName);

        try
        {
            return processes.Length;
        }
        finally
        {
            foreach (var process in processes)
            {
                process.Dispose();
            }
        }
    }

    private void KillStaleAgents()
    {
        foreach (var process in Process.GetProcessesByName(_options.AgentProcessName))
        {
            try
            {
                _log.LogWarning("Killing stale agent PID {Pid}", process.Id);
                process.Kill(entireProcessTree: true);

                if (!process.WaitForExit(5000))
                {
                    _log.LogError("Agent PID {Pid} did not exit within 5s", process.Id);
                }
            }
            catch (Exception ex)
            {
                // Already gone, or access denied — either way, carry on to the relaunch.
                _log.LogError(ex, "Failed to kill agent process");
            }
            finally
            {
                process.Dispose();
            }
        }
    }
}
