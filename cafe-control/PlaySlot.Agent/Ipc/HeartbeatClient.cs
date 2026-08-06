using System.IO.Pipes;
using System.Text;

namespace PlaySlot.Agent.Ipc;

/// <summary>
/// Sends "alive" to the watchdog on a fixed interval.
///
/// The contract is the watchdog's: connect to the heartbeat pipe, write the token,
/// disconnect. Beat faster than the watchdog's HeartbeatTimeoutSeconds or a healthy
/// agent gets killed.
///
/// The suppression flags are how the watchdog's hung-agent path gets tested — the
/// process stays alive and keeps its PID while the heartbeats stop.
/// </summary>
internal sealed class HeartbeatClient
{
    private readonly AgentOptions _options;
    private readonly DateTime _startedAt = DateTime.UtcNow;

    public HeartbeatClient(AgentOptions options) => _options = options;

    public async Task RunAsync(CancellationToken cancellationToken)
    {
        if (!_options.HeartbeatEnabled)
        {
            Log.Warn("Heartbeat disabled (--no-heartbeat) — the watchdog will treat this agent as hung");
        }

        while (!cancellationToken.IsCancellationRequested)
        {
            if (IsSuppressed())
            {
                Log.Info("heartbeat suppressed — waiting to be killed");
            }
            else
            {
                await SendAsync(cancellationToken).ConfigureAwait(false);
            }

            try
            {
                await Task.Delay(TimeSpan.FromSeconds(_options.HeartbeatIntervalSeconds), cancellationToken)
                    .ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                break;
            }
        }
    }

    private bool IsSuppressed()
    {
        if (!_options.HeartbeatEnabled)
        {
            return true;
        }

        return _options.HangAfterSeconds > 0 &&
               (DateTime.UtcNow - _startedAt).TotalSeconds >= _options.HangAfterSeconds;
    }

    private async Task SendAsync(CancellationToken cancellationToken)
    {
        try
        {
            using var client = new NamedPipeClientStream(
                ".", _options.HeartbeatPipeName, PipeDirection.Out, PipeOptions.Asynchronous);

            await client.ConnectAsync(2000, cancellationToken).ConfigureAwait(false);

            // Disposing the writer closes the pipe, which is what signals end-of-message
            // to the watchdog's reader.
            await using var writer = new StreamWriter(client, new UTF8Encoding(false)) { AutoFlush = true };
            await writer.WriteLineAsync("alive").ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (TimeoutException)
        {
            Log.Warn("watchdog pipe unavailable (is the service running?)");
        }
        catch (Exception ex)
        {
            Log.Warn($"heartbeat failed: {ex.Message}");
        }
    }
}
