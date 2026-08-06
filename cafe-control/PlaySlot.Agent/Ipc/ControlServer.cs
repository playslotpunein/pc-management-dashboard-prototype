using System.IO.Pipes;
using System.Text;

namespace PlaySlot.Agent.Ipc;

/// <summary>
/// Accepts lock/unlock commands over a local named pipe.
///
/// This is Phase 1 scaffolding and nothing more. Zone 2's session engine owns these
/// decisions, and it will deliver them over an HMAC-signed WebSocket; this pipe exists
/// only so the lock can be driven before that server exists. Run with --no-control-pipe
/// once the WebSocket lands.
///
/// Commands are handled on a background thread, so they are marshalled onto the UI
/// thread before touching the lock — hooks and forms both require it.
/// </summary>
internal sealed class ControlServer
{
    private readonly AgentOptions _options;
    private readonly Func<string, string> _handler;
    private readonly SynchronizationContext _uiContext;

    public ControlServer(AgentOptions options, SynchronizationContext uiContext, Func<string, string> handler)
    {
        _options = options;
        _uiContext = uiContext;
        _handler = handler;
    }

    public async Task RunAsync(CancellationToken cancellationToken)
    {
        if (!_options.ControlPipeEnabled)
        {
            Log.Info("Control pipe disabled");
            return;
        }

        if (string.IsNullOrEmpty(_options.ControlToken))
        {
            Log.Warn(
                "Control pipe has no token — any process in this session can send unlock. " +
                "Development only; pass --control-token or --no-control-pipe.");
        }

        Log.Info($"Control pipe listening on {_options.ControlPipeName}");

        while (!cancellationToken.IsCancellationRequested)
        {
            try
            {
                using var server = new NamedPipeServerStream(
                    _options.ControlPipeName,
                    PipeDirection.InOut,
                    maxNumberOfServerInstances: 1,
                    PipeTransmissionMode.Byte,
                    PipeOptions.Asynchronous);

                await server.WaitForConnectionAsync(cancellationToken).ConfigureAwait(false);

                using var reader = new StreamReader(server, Encoding.UTF8, leaveOpen: true);
                await using var writer = new StreamWriter(server, new UTF8Encoding(false), leaveOpen: true)
                {
                    AutoFlush = true
                };

                var line = await reader.ReadLineAsync(cancellationToken).ConfigureAwait(false);

                if (line is null)
                {
                    continue;
                }

                var response = Dispatch(line.Trim());
                await writer.WriteLineAsync(response).ConfigureAwait(false);

                server.WaitForPipeDrain();
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                break;
            }
            catch (Exception ex)
            {
                Log.Error($"Control pipe iteration failed: {ex.Message}");

                try
                {
                    await Task.Delay(TimeSpan.FromSeconds(1), cancellationToken).ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                    break;
                }
            }
        }

        Log.Info("Control pipe stopped");
    }

    private string Dispatch(string line)
    {
        var command = line;

        if (!string.IsNullOrEmpty(_options.ControlToken))
        {
            // Format is "<token> <command>".
            var separator = line.IndexOf(' ');

            if (separator < 0)
            {
                return "ERR missing token";
            }

            var presented = line[..separator];
            command = line[(separator + 1)..].Trim();

            if (!CryptographicEquals(presented, _options.ControlToken))
            {
                Log.Warn("Control command rejected: bad token");
                return "ERR bad token";
            }
        }

        Log.Info($"Control command: {command}");

        // Hop to the UI thread and wait for the result. Send (not Post) so the caller
        // gets the real outcome rather than an optimistic acknowledgement.
        string? result = null;
        _uiContext.Send(_ => result = _handler(command), null);

        return result ?? "ERR no response";
    }

    /// <summary>Length-independent comparison, so the token cannot be probed by timing.</summary>
    private static bool CryptographicEquals(string a, string b)
    {
        var difference = a.Length ^ b.Length;

        for (var i = 0; i < a.Length && i < b.Length; i++)
        {
            difference |= a[i] ^ b[i];
        }

        return difference == 0;
    }
}
