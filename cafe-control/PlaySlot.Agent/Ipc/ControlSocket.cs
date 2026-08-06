using System.Net.WebSockets;
using System.Text;
using System.Text.Json.Nodes;

namespace PlaySlot.Agent.Ipc;

/// <summary>
/// The persistent link to the control server.
///
/// Replaces the local control pipe as the way lock decisions arrive. The pipe had no
/// authentication at all — any process in the user's session could send an unlock, which
/// on a real cafe PC means the customer can free themselves by running the same exe.
/// Every message here is HMAC-signed against a per-device secret, in both directions.
///
/// The agent is the executor, not the decider. It never works out whether a session
/// should end; it applies what the server says, plus what the cache says when the server
/// is unreachable.
///
/// Two behaviours are worth reading carefully:
///
/// **Reconnect is expected, not exceptional.** A cafe PC reboots, wifi drops, the control
/// server gets restarted for an update. The socket reconnects with backoff and the server
/// re-sends current state on every connect, so no missed command needs replaying.
///
/// **The fail-safe waits before acting.** On losing the link the agent does nothing for
/// <see cref="AgentOptions.FailSafeDelaySeconds"/> — most disconnects are over in a
/// second and locking a paying customer for a blip is worse than the blip.
/// </summary>
internal sealed class ControlSocket : IDisposable
{
    private readonly AgentOptions _options;
    private readonly SessionCache _cache;
    private readonly Action<bool, string> _applyLock;
    private readonly SynchronizationContext _uiContext;

    private ClientWebSocket? _socket;
    private DateTime? _disconnectedSinceUtc;
    private bool _failSafeApplied;
    private bool _disposed;

    public ControlSocket(
        AgentOptions options,
        SessionCache cache,
        SynchronizationContext uiContext,
        Action<bool, string> applyLock)
    {
        _options = options;
        _cache = cache;
        _uiContext = uiContext;
        _applyLock = applyLock;
    }

    public bool IsConnected => _socket?.State == WebSocketState.Open;

    public async Task RunAsync(CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(_options.ServerUrl) ||
            string.IsNullOrWhiteSpace(_options.DeviceToken))
        {
            Log.Warn(
                "No --server-url or --device-token; running standalone on the control pipe. " +
                "Enrol the unit and pass both to take commands from the control server.");
            return;
        }

        var backoffSeconds = 1;

        while (!cancellationToken.IsCancellationRequested)
        {
            try
            {
                await ConnectAndServeAsync(cancellationToken).ConfigureAwait(false);

                // A clean return means the server closed the socket. Reset the backoff:
                // this was not a failure to connect.
                backoffSeconds = 1;
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                break;
            }
            catch (Exception ex)
            {
                Log.Warn($"Control server link failed: {ex.Message}");
            }

            OnDisconnected();

            // The fail-safe runs while disconnected, so the wait loop is where it lives
            // rather than in a separate timer that could outlive a reconnect.
            var waited = 0;

            while (waited < backoffSeconds && !cancellationToken.IsCancellationRequested)
            {
                EvaluateFailSafe();

                try
                {
                    await Task.Delay(TimeSpan.FromSeconds(1), cancellationToken)
                        .ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                    return;
                }

                waited++;
            }

            // Capped so a long outage does not turn into a ten-minute wait after the
            // server comes back.
            backoffSeconds = Math.Min(backoffSeconds * 2, 30);
        }
    }

    private async Task ConnectAndServeAsync(CancellationToken cancellationToken)
    {
        using var socket = new ClientWebSocket();
        _socket = socket;

        var uri = new Uri($"{_options.ServerUrl.TrimEnd('/')}/agent/{_options.UnitId}");

        Log.Info($"Connecting to {uri}");

        await socket.ConnectAsync(uri, cancellationToken).ConfigureAwait(false);

        // The token never travels in the URL — a query string ends up in access logs and
        // proxy history. Authentication is the first message instead.
        await SendAsync(socket, "hello", new JsonObject
        {
            ["agent_version"] = AgentOptions.Version,
        }, cancellationToken).ConfigureAwait(false);

        Log.Info("Connected to the control server");

        OnConnected();

        using var heartbeats = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        var heartbeatTask = HeartbeatLoopAsync(socket, heartbeats.Token);

        try
        {
            await ReceiveLoopAsync(socket, cancellationToken).ConfigureAwait(false);
        }
        finally
        {
            heartbeats.Cancel();

            try
            {
                await heartbeatTask.ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                // Expected on shutdown.
            }

            _socket = null;
        }
    }

    private async Task ReceiveLoopAsync(ClientWebSocket socket, CancellationToken cancellationToken)
    {
        var buffer = new byte[8192];

        while (socket.State == WebSocketState.Open && !cancellationToken.IsCancellationRequested)
        {
            var payload = new StringBuilder();
            WebSocketReceiveResult result;

            do
            {
                result = await socket
                    .ReceiveAsync(new ArraySegment<byte>(buffer), cancellationToken)
                    .ConfigureAwait(false);

                if (result.MessageType == WebSocketMessageType.Close)
                {
                    Log.Info($"Server closed the link ({result.CloseStatus})");
                    return;
                }

                payload.Append(Encoding.UTF8.GetString(buffer, 0, result.Count));
            }
            while (!result.EndOfMessage);

            HandleMessage(payload.ToString());
        }
    }

    private void HandleMessage(string raw)
    {
        JsonObject? envelope;

        try
        {
            envelope = JsonNode.Parse(raw)?.AsObject();
        }
        catch (Exception ex)
        {
            Log.Warn($"Unparseable message from the server: {ex.Message}");
            return;
        }

        if (envelope is null)
        {
            return;
        }

        var body = MessageSigner.Verify(_options.DeviceToken, envelope);

        if (body is null)
        {
            // Dropped, not acted on. An unsigned or forged message is exactly what an
            // attempt to unlock a machine from the cafe wifi looks like.
            Log.Warn("Dropped a server message that failed signature verification");
            return;
        }

        if (envelope["type"]?.GetValue<string>() != "state")
        {
            return;
        }

        var locked = body["locked"]?.GetValue<bool>() ?? false;
        var sessionEnd = ParseUtc(body["session_end_utc"]);
        var graceEnd = ParseUtc(body["grace_end_utc"]);

        _cache.Update(sessionEnd, graceEnd, locked);

        // The server has spoken, so any fail-safe decision is superseded.
        _failSafeApplied = false;

        var reason = locked ? "control server: lock" : "control server: unlock";

        // Hooks and forms both require the UI thread; the socket runs on a worker.
        _uiContext.Post(_ => _applyLock(locked, reason), null);
    }

    private async Task HeartbeatLoopAsync(ClientWebSocket socket, CancellationToken cancellationToken)
    {
        while (socket.State == WebSocketState.Open && !cancellationToken.IsCancellationRequested)
        {
            try
            {
                await Task.Delay(
                    TimeSpan.FromSeconds(_options.HeartbeatIntervalSeconds), cancellationToken)
                    .ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                return;
            }

            if (socket.State != WebSocketState.Open)
            {
                return;
            }

            try
            {
                await SendAsync(socket, "heartbeat", new JsonObject
                {
                    ["agent_version"] = AgentOptions.Version,
                    ["state"] = _cache.ServerSaysLocked ? "locked" : "active",
                }, cancellationToken).ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                Log.Warn($"Heartbeat to the control server failed: {ex.Message}");
                return;
            }
        }
    }

    private async Task SendAsync(
        ClientWebSocket socket, string type, JsonObject body, CancellationToken cancellationToken)
    {
        var envelope = MessageSigner.BuildEnvelope(
            _options.DeviceToken, _options.UnitId, type, body);

        var bytes = Encoding.UTF8.GetBytes(envelope.ToJsonString());

        await socket.SendAsync(
            new ArraySegment<byte>(bytes),
            WebSocketMessageType.Text,
            endOfMessage: true,
            cancellationToken).ConfigureAwait(false);
    }

    // ---- fail-safe ---------------------------------------------------------

    private void OnConnected()
    {
        _disconnectedSinceUtc = null;
        _failSafeApplied = false;
    }

    private void OnDisconnected()
    {
        _disconnectedSinceUtc ??= DateTime.UtcNow;
    }

    /// <summary>
    /// Zone 5. Runs once per second while the link is down.
    /// </summary>
    private void EvaluateFailSafe()
    {
        if (_disconnectedSinceUtc is null || _failSafeApplied)
        {
            return;
        }

        var down = DateTime.UtcNow - _disconnectedSinceUtc.Value;

        // Most disconnects last a second or two. Acting immediately would lock a paying
        // customer over a blip, which is precisely the outcome this branch exists to
        // avoid.
        if (down.TotalSeconds < _options.FailSafeDelaySeconds)
        {
            return;
        }

        var decision = _cache.Decide(DateTime.UtcNow);

        Log.Warn(
            $"Control server unreachable for {(int)down.TotalSeconds}s — " +
            $"fail-safe: {(decision.ShouldLock ? "LOCK" : "stay unlocked")} " +
            $"({decision.Reason})");

        _failSafeApplied = true;

        _uiContext.Post(
            _ => _applyLock(decision.ShouldLock, $"fail-safe: {decision.Reason}"), null);
    }

    private static DateTime? ParseUtc(JsonNode? node)
    {
        var text = node?.GetValue<string>();

        if (string.IsNullOrWhiteSpace(text))
        {
            return null;
        }

        return DateTime.TryParse(
            text,
            System.Globalization.CultureInfo.InvariantCulture,
            System.Globalization.DateTimeStyles.AdjustToUniversal |
            System.Globalization.DateTimeStyles.AssumeUniversal,
            out var parsed)
            ? parsed
            : null;
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        _socket?.Dispose();
        _disposed = true;
    }
}
