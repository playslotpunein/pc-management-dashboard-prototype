namespace PlaySlot.Agent.Lock;

/// <summary>
/// The escape route, in two independent halves:
///
///   1. An auto-release timer. However the lock was engaged, it lets go after
///      MaxLockSeconds. A hung control channel or a logic bug cannot leave a machine
///      locked forever.
///   2. An emergency key combo (Ctrl+Alt+Shift+U). Works even while Layer B is
///      swallowing input, because InputBlocker reports keys to observers before it
///      drops them.
///
/// Both exist for development, where the machine holding your editor is also the
/// machine running the lock. Production disables them and lets the server be the only
/// way out.
/// </summary>
internal sealed class PanicHatch
{
    private static readonly int[] ComboModifiers = [VkControl, VkMenu, VkShift];

    private readonly HashSet<int> _heldKeys = [];
    private readonly System.Windows.Forms.Timer _autoRelease = new();

    private readonly int _maxLockSeconds;
    private readonly bool _comboEnabled;

    /// <summary>Raised when either half fires. The argument is a human-readable reason.</summary>
    public event Action<string>? ReleaseRequested;

    public PanicHatch(int maxLockSeconds, bool comboEnabled)
    {
        _maxLockSeconds = maxLockSeconds;
        _comboEnabled = comboEnabled;

        _autoRelease.Tick += (_, _) =>
        {
            _autoRelease.Stop();
            ReleaseRequested?.Invoke($"panic hatch: auto-release after {_maxLockSeconds}s");
        };
    }

    public bool AutoReleaseEnabled => _maxLockSeconds > 0;

    public bool ComboEnabled => _comboEnabled;

    public string ComboDescription => "Ctrl + Alt + Shift + U";

    /// <summary>Human-readable summary for the startup banner and the overlay footer.</summary>
    public string Describe() => Describe(_maxLockSeconds, _comboEnabled);

    /// <summary>
    /// Static form so the startup banner can report the configuration without
    /// constructing an instance (which would allocate a timer it never uses).
    /// </summary>
    public static string Describe(int maxLockSeconds, bool comboEnabled)
    {
        var parts = new List<string>();

        if (maxLockSeconds > 0)
        {
            parts.Add($"auto-release {maxLockSeconds}s");
        }

        if (comboEnabled)
        {
            parts.Add("Ctrl + Alt + Shift + U");
        }

        return parts.Count == 0 ? "DISABLED" : string.Join(" | ", parts);
    }

    public void OnLockEngaged()
    {
        _heldKeys.Clear();

        if (!AutoReleaseEnabled)
        {
            return;
        }

        _autoRelease.Interval = _maxLockSeconds * 1000;
        _autoRelease.Start();
    }

    public void OnLockReleased()
    {
        _autoRelease.Stop();
        _heldKeys.Clear();
    }

    /// <summary>
    /// Called from the keyboard hook for every key event while blocking. Must stay cheap;
    /// see the timing note in <see cref="InputBlocker"/>.
    /// </summary>
    public void ObserveKey(int virtualKey, bool isDown)
    {
        if (!_comboEnabled)
        {
            return;
        }

        var key = Normalise(virtualKey);

        if (!isDown)
        {
            _heldKeys.Remove(key);
            return;
        }

        _heldKeys.Add(key);

        if (key != VkU)
        {
            return;
        }

        foreach (var modifier in ComboModifiers)
        {
            if (!_heldKeys.Contains(modifier))
            {
                return;
            }
        }

        _heldKeys.Clear();
        ReleaseRequested?.Invoke($"panic hatch: {ComboDescription}");
    }

    /// <summary>
    /// Low-level hooks report the distinct left/right modifier keys, so collapse them
    /// onto the generic code the combo is expressed in.
    /// </summary>
    private static int Normalise(int virtualKey) => virtualKey switch
    {
        VkLControl or VkRControl => VkControl,
        VkLMenu or VkRMenu => VkMenu,
        VkLShift or VkRShift => VkShift,
        _ => virtualKey
    };

    private const int VkShift = 0x10;
    private const int VkControl = 0x11;
    private const int VkMenu = 0x12;
    private const int VkLShift = 0xA0;
    private const int VkRShift = 0xA1;
    private const int VkLControl = 0xA2;
    private const int VkRControl = 0xA3;
    private const int VkLMenu = 0xA4;
    private const int VkRMenu = 0xA5;
    private const int VkU = 0x55;
}
