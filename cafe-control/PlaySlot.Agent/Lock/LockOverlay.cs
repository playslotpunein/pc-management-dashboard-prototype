using System.Runtime.InteropServices;

namespace PlaySlot.Agent.Lock;

/// <summary>
/// Layer C — one borderless, always-on-top window covering a single monitor.
///
/// This is the layer the customer actually understands. It also covers the case where
/// input is not fully blocked but nothing useful is reachable, which is what makes it
/// worth keeping even after Layer A lands.
///
/// Staying on top of a fullscreen game is the fiddly part: a game that goes exclusive
/// fullscreen can push itself above a topmost window, so Z-order is re-asserted on a
/// timer rather than set once.
/// </summary>
internal sealed class LockOverlay : Form
{
    private readonly System.Windows.Forms.Timer _topmostTimer = new() { Interval = 1000 };
    private readonly Label _countdownLabel;
    private DateTime? _releaseAtUtc;

    public LockOverlay(Rectangle bounds, string title, string message, string? footer)
    {
        FormBorderStyle = FormBorderStyle.None;
        StartPosition = FormStartPosition.Manual;
        Bounds = bounds;
        BackColor = Color.FromArgb(11, 15, 25);
        ShowInTaskbar = false;
        TopMost = true;
        KeyPreview = true;
        DoubleBuffered = true;

        var layout = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            ColumnCount = 1,
            RowCount = 4,
            BackColor = Color.Transparent
        };

        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 38f));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 62f));

        layout.Controls.Add(new Label
        {
            Text = title,
            Font = new Font("Segoe UI", 42f, FontStyle.Bold),
            ForeColor = Color.FromArgb(240, 244, 255),
            AutoSize = false,
            Dock = DockStyle.Fill,
            Height = 90,
            TextAlign = ContentAlignment.MiddleCenter,
            BackColor = Color.Transparent
        }, 0, 1);

        layout.Controls.Add(new Label
        {
            Text = message,
            Font = new Font("Segoe UI", 18f, FontStyle.Regular),
            ForeColor = Color.FromArgb(150, 165, 195),
            AutoSize = false,
            Dock = DockStyle.Fill,
            Height = 48,
            TextAlign = ContentAlignment.MiddleCenter,
            BackColor = Color.Transparent
        }, 0, 2);

        _countdownLabel = new Label
        {
            Text = string.Empty,
            Font = new Font("Consolas", 11f, FontStyle.Regular),
            ForeColor = Color.FromArgb(95, 110, 140),
            AutoSize = false,
            Dock = DockStyle.Bottom,
            Height = 56,
            TextAlign = ContentAlignment.MiddleCenter,
            BackColor = Color.Transparent
        };

        if (!string.IsNullOrWhiteSpace(footer))
        {
            _countdownLabel.Text = footer;
        }

        Controls.Add(layout);
        Controls.Add(_countdownLabel);

        _topmostTimer.Tick += (_, _) => ReassertTopmost();
        _topmostTimer.Start();
    }

    /// <summary>Keeps the overlay out of Alt+Tab so it does not look like a normal window.</summary>
    protected override CreateParams CreateParams
    {
        get
        {
            var createParams = base.CreateParams;
            createParams.ExStyle |= WsExToolWindow;
            return createParams;
        }
    }

    /// <summary>Shows a live countdown when the panic hatch will release the lock.</summary>
    public void SetCountdown(DateTime? releaseAtUtc, string? footer)
    {
        _releaseAtUtc = releaseAtUtc;
        UpdateCountdown(footer);
    }

    public void UpdateCountdown(string? footer)
    {
        if (_releaseAtUtc is null)
        {
            _countdownLabel.Text = footer ?? string.Empty;
            return;
        }

        var remaining = _releaseAtUtc.Value - DateTime.UtcNow;
        var seconds = Math.Max(0, (int)Math.Ceiling(remaining.TotalSeconds));

        _countdownLabel.Text = string.IsNullOrWhiteSpace(footer)
            ? $"auto-release in {seconds}s"
            : $"{footer}    ·    auto-release in {seconds}s";
    }

    private void ReassertTopmost()
    {
        if (!IsHandleCreated || IsDisposed)
        {
            return;
        }

        // A game entering exclusive fullscreen can take the top slot from us, so this
        // runs every second rather than relying on TopMost alone.
        SetWindowPos(Handle, HwndTopmost, 0, 0, 0, 0, SwpNoMove | SwpNoSize | SwpNoActivate);
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            _topmostTimer.Stop();
            _topmostTimer.Dispose();
        }

        base.Dispose(disposing);
    }

    private const int WsExToolWindow = 0x00000080;
    private static readonly IntPtr HwndTopmost = new(-1);
    private const uint SwpNoSize = 0x0001;
    private const uint SwpNoMove = 0x0002;
    private const uint SwpNoActivate = 0x0010;

    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetWindowPos(
        IntPtr hWnd, IntPtr hWndInsertAfter, int x, int y, int cx, int cy, uint uFlags);
}
