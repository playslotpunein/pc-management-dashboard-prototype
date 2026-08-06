namespace PlaySlot.Agent.Lock;

/// <summary>
/// Orchestrates the lock: Layer B (input) and Layer C (overlay) go up and down together,
/// with the panic hatch armed for as long as the lock is held.
///
/// The agent is the executor, not the decider. Nothing here works out *whether* a session
/// should end — that belongs to the server's session engine. This type only carries out
/// Lock and Unlock, which is what makes it swappable behind the WebSocket later.
///
/// Must be used from the UI thread; the hooks and forms both require it.
/// </summary>
internal sealed class LockController : IDisposable
{
    private readonly AgentOptions _options;
    private readonly InputBlocker _input = new();
    private readonly OverlayManager _overlays = new();
    private readonly PanicHatch _panic;

    private bool _disposed;

    public LockController(AgentOptions options)
    {
        _options = options;
        _input.BlockMouseMovement = options.BlockMouseMovement;

        _panic = new PanicHatch(options.MaxLockSeconds, options.PanicComboEnabled);
        _panic.ReleaseRequested += reason => Unlock(reason);

        _input.KeyObserved += _panic.ObserveKey;
    }

    public bool IsLocked { get; private set; }

    public void Lock(string reason)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);

        if (IsLocked)
        {
            Log.Info($"Lock requested but already locked ({reason})");
            return;
        }

        Log.Warn($"LOCKING — {reason}");

        IsLocked = true;

        // Overlay first. If the hooks fail to install, the customer still sees a lock
        // screen rather than a machine that looks perfectly usable.
        var releaseAt = _panic.AutoReleaseEnabled
            ? DateTime.UtcNow.AddSeconds(_options.MaxLockSeconds)
            : (DateTime?)null;

        _overlays.Show(_options.LockTitle, _options.LockMessage, BuildFooter(), releaseAt);

        if (!_input.Engage())
        {
            Log.Error("Input hooks did not fully engage — overlay is up but input is not blocked");
        }

        _panic.OnLockEngaged();
    }

    public void Unlock(string reason)
    {
        if (_disposed)
        {
            return;
        }

        if (!IsLocked)
        {
            Log.Info($"Unlock requested but not locked ({reason})");
            return;
        }

        Log.Warn($"UNLOCKING — {reason}");

        _panic.OnLockReleased();
        _input.Release();
        _overlays.Hide();

        IsLocked = false;
    }

    public string StatusLine =>
        $"unit={_options.UnitId} state={(IsLocked ? "locked" : "unlocked")} " +
        $"hooks={_input.IsActive} overlay={_overlays.IsShowing} panic=[{_panic.Describe()}]";

    private string? BuildFooter()
    {
        var parts = new List<string> { _options.UnitId };

        // Only advertised while the combo is live, which is development. Production runs
        // with --no-panic-combo and the customer sees just the unit id.
        if (_panic.ComboEnabled)
        {
            parts.Add($"emergency release: {_panic.ComboDescription}");
        }

        return string.Join("    ·    ", parts);
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        // Order matters on shutdown: never leave hooks installed with no overlay, and
        // never exit with input still swallowed.
        _input.KeyObserved -= _panic.ObserveKey;

        _input.Release();
        _input.Dispose();
        _overlays.Dispose();

        IsLocked = false;
        _disposed = true;
    }
}
