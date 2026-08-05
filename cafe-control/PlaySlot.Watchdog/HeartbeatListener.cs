using System.IO.Pipes;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using Microsoft.Extensions.Options;

namespace PlaySlot.Watchdog;

/// <summary>
/// Accepts heartbeats from the agent over a named pipe and records when the last
/// one landed. Process liveness alone is not a health signal — a hung agent keeps
/// its PID — so this timestamp is the other half of the health check.
/// </summary>
internal sealed class HeartbeatListener : BackgroundService
{
    private const string AliveToken = "alive";

    private readonly ILogger<HeartbeatListener> _log;
    private readonly WatchdogOptions _options;

    // Written by the pipe loop, read by the monitor loop. Ticks rather than DateTime
    // so reads and writes are atomic via Interlocked.
    private long _lastHeartbeatTicks;

    public HeartbeatListener(ILogger<HeartbeatListener> log, IOptions<WatchdogOptions> options)
    {
        _log = log;
        _options = options.Value;
    }

    /// <summary>Last heartbeat, or null if none has been received since startup.</summary>
    public DateTime? LastHeartbeatUtc
    {
        get
        {
            var ticks = Interlocked.Read(ref _lastHeartbeatTicks);
            return ticks == 0 ? null : new DateTime(ticks, DateTimeKind.Utc);
        }
    }

    public TimeSpan? TimeSinceLastHeartbeat =>
        LastHeartbeatUtc is { } last ? DateTime.UtcNow - last : null;

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        _log.LogInformation("Heartbeat listener starting on pipe {Pipe}", _options.HeartbeatPipeName);

        var security = BuildPipeSecurity();

        while (!stoppingToken.IsCancellationRequested)
        {
            try
            {
                // A fresh server instance per connection. The agent reconnects on every
                // beat, so the brief gap between dispose and re-create is harmless.
                using var server = NamedPipeServerStreamAcl.Create(
                    _options.HeartbeatPipeName,
                    PipeDirection.In,
                    maxNumberOfServerInstances: 1,
                    PipeTransmissionMode.Byte,
                    PipeOptions.Asynchronous,
                    inBufferSize: 0,
                    outBufferSize: 0,
                    pipeSecurity: security);

                await server.WaitForConnectionAsync(stoppingToken).ConfigureAwait(false);

                using var reader = new StreamReader(server, Encoding.UTF8);

                // ReadToEnd rather than ReadLine: it returns when the agent disconnects,
                // so a client that writes without a trailing newline still works.
                var payload = await reader.ReadToEndAsync(stoppingToken).ConfigureAwait(false);

                if (payload.Contains(AliveToken, StringComparison.OrdinalIgnoreCase))
                {
                    Interlocked.Exchange(ref _lastHeartbeatTicks, DateTime.UtcNow.Ticks);
                    _log.LogDebug("Heartbeat received");
                }
                else
                {
                    _log.LogWarning("Ignoring unrecognised heartbeat payload: {Payload}", payload.Trim());
                }
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                break;
            }
            catch (Exception ex)
            {
                // Never let a bad connection take the listener down.
                _log.LogError(ex, "Heartbeat listener iteration failed; retrying");

                try
                {
                    await Task.Delay(TimeSpan.FromSeconds(1), stoppingToken).ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                    break;
                }
            }
        }

        _log.LogInformation("Heartbeat listener stopped");
    }

    /// <summary>
    /// The service runs as LocalSystem but the agent runs as the interactive standard
    /// user. A pipe created with default security would reject that user, so the DACL
    /// is set explicitly. This is the usual reason heartbeats never arrive.
    /// </summary>
    private static PipeSecurity BuildPipeSecurity()
    {
        var security = new PipeSecurity();

        security.AddAccessRule(new PipeAccessRule(
            new SecurityIdentifier(WellKnownSidType.AuthenticatedUserSid, null),
            PipeAccessRights.ReadWrite | PipeAccessRights.CreateNewInstance,
            AccessControlType.Allow));

        security.AddAccessRule(new PipeAccessRule(
            new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null),
            PipeAccessRights.FullControl,
            AccessControlType.Allow));

        security.AddAccessRule(new PipeAccessRule(
            new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid, null),
            PipeAccessRights.FullControl,
            AccessControlType.Allow));

        return security;
    }
}
