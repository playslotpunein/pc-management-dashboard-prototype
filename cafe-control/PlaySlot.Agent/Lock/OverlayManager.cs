using Microsoft.Win32;

namespace PlaySlot.Agent.Lock;

/// <summary>
/// Owns one <see cref="LockOverlay"/> per monitor and keeps that set correct while the
/// lock is up. Multi-monitor is the usual source of gaps: cover only the primary screen
/// and the customer still has a working second display.
/// </summary>
internal sealed class OverlayManager : IDisposable
{
    private readonly List<LockOverlay> _overlays = [];
    private readonly System.Windows.Forms.Timer _countdownTimer = new() { Interval = 500 };

    private string _title = string.Empty;
    private string _message = string.Empty;
    private string? _footer;
    private DateTime? _releaseAtUtc;
    private bool _cursorHidden;
    private bool _disposed;

    public OverlayManager()
    {
        _countdownTimer.Tick += (_, _) =>
        {
            foreach (var overlay in _overlays)
            {
                overlay.UpdateCountdown(_footer);
            }
        };

        // A display being plugged in or resolution changing mid-lock would otherwise
        // leave an uncovered monitor.
        SystemEvents.DisplaySettingsChanged += OnDisplaySettingsChanged;
    }

    public bool IsShowing => _overlays.Count > 0;

    public void Show(string title, string message, string? footer, DateTime? releaseAtUtc)
    {
        _title = title;
        _message = message;
        _footer = footer;
        _releaseAtUtc = releaseAtUtc;

        Build();

        if (releaseAtUtc is not null)
        {
            _countdownTimer.Start();
        }
    }

    public void Hide()
    {
        _countdownTimer.Stop();
        Teardown();
    }

    /// <summary>
    /// Pulls the overlays back to the front without rebuilding them. Used when re-arming a
    /// lock that is already up: if the hooks were dropped, the customer had working input
    /// and may well have brought another window to the foreground on top of the overlay.
    /// </summary>
    public void Raise()
    {
        foreach (var overlay in _overlays)
        {
            overlay.BringToFront();
        }

        _overlays.FirstOrDefault()?.Activate();
    }

    private void Build()
    {
        Teardown();

        foreach (var screen in Screen.AllScreens)
        {
            var overlay = new LockOverlay(screen.Bounds, _title, _message, _footer);
            overlay.SetCountdown(_releaseAtUtc, _footer);
            overlay.Show();
            _overlays.Add(overlay);
        }

        // Focus one of them so keyboard input has somewhere harmless to land.
        _overlays.FirstOrDefault()?.Activate();

        // Cursor.Hide is application-wide and reference-counted, so it is paired with
        // the Show in Teardown rather than set per form.
        if (!_cursorHidden)
        {
            Cursor.Hide();
            _cursorHidden = true;
        }

        Log.Info($"Overlay shown across {_overlays.Count} monitor(s)");
    }

    private void Teardown()
    {
        if (_cursorHidden)
        {
            Cursor.Show();
            _cursorHidden = false;
        }

        foreach (var overlay in _overlays)
        {
            overlay.Hide();
            overlay.Dispose();
        }

        if (_overlays.Count > 0)
        {
            Log.Info("Overlay removed");
        }

        _overlays.Clear();
    }

    private void OnDisplaySettingsChanged(object? sender, EventArgs e)
    {
        if (!IsShowing)
        {
            return;
        }

        Log.Info("Display configuration changed; rebuilding overlay");
        Build();
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        SystemEvents.DisplaySettingsChanged -= OnDisplaySettingsChanged;

        _countdownTimer.Stop();
        _countdownTimer.Dispose();
        Teardown();

        _disposed = true;
    }
}
