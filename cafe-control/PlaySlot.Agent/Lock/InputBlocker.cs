using System.Runtime.InteropServices;

namespace PlaySlot.Agent.Lock;

/// <summary>
/// Layer B — low-level keyboard and mouse hooks.
///
/// SetWindowsHookEx with WH_KEYBOARD_LL / WH_MOUSE_LL, returning a non-zero value to
/// swallow the event before it reaches any application. Pure user mode, no driver and
/// no code signing, which is why this is the first layer to build.
///
/// What it catches: Alt+Tab, the Windows key, Alt+F4, application shortcuts — nearly
/// everything. What it cannot catch: Ctrl+Alt+Del and the UAC prompt, both of which run
/// on the secure desktop where only a kernel-mode filter driver (Layer A) can reach.
///
/// Requires a running message pump on the installing thread, so this must be engaged
/// from the UI thread that calls Application.Run.
/// </summary>
internal sealed class InputBlocker : IDisposable
{
    private delegate IntPtr LowLevelProc(int nCode, IntPtr wParam, IntPtr lParam);

    // These two fields are the reason the hook does not crash. Passing a lambda straight
    // to SetWindowsHookEx leaves nothing managed holding the delegate, the GC collects
    // it, and the first keystroke afterwards jumps into freed memory. Keeping the
    // delegates in fields for the lifetime of the hook is mandatory, not stylistic.
    private LowLevelProc? _keyboardProc;
    private LowLevelProc? _mouseProc;

    private IntPtr _keyboardHook;
    private IntPtr _mouseHook;
    private bool _disposed;

    /// <summary>Raised for every key event seen while blocking. (virtual key code, is down)</summary>
    public event Action<int, bool>? KeyObserved;

    public bool IsActive => _keyboardHook != IntPtr.Zero || _mouseHook != IntPtr.Zero;

    /// <summary>Swallow mouse movement too. Clicks and wheel are always swallowed.</summary>
    public bool BlockMouseMovement { get; set; }

    /// <summary>
    /// Installs both hooks. Safe to call when already engaged. Returns false if either
    /// hook failed to install — the caller should still show the overlay, since a
    /// partially blocked machine with a visible lock screen beats no lock at all.
    /// </summary>
    public bool Engage()
    {
        ObjectDisposedException.ThrowIf(_disposed, this);

        if (IsActive)
        {
            return true;
        }

        var module = GetModuleHandle(null);
        var ok = true;

        _keyboardProc = KeyboardProc;
        _keyboardHook = SetWindowsHookEx(WhKeyboardLl, _keyboardProc, module, 0);

        if (_keyboardHook == IntPtr.Zero)
        {
            Log.Error($"Keyboard hook failed to install (win32 {Marshal.GetLastWin32Error()})");
            _keyboardProc = null;
            ok = false;
        }

        _mouseProc = MouseProc;
        _mouseHook = SetWindowsHookEx(WhMouseLl, _mouseProc, module, 0);

        if (_mouseHook == IntPtr.Zero)
        {
            Log.Error($"Mouse hook failed to install (win32 {Marshal.GetLastWin32Error()})");
            _mouseProc = null;
            ok = false;
        }

        Log.Info($"Input hooks engaged (keyboard={_keyboardHook != IntPtr.Zero}, mouse={_mouseHook != IntPtr.Zero})");
        return ok;
    }

    public void Release()
    {
        if (_keyboardHook != IntPtr.Zero)
        {
            UnhookWindowsHookEx(_keyboardHook);
            _keyboardHook = IntPtr.Zero;
        }

        if (_mouseHook != IntPtr.Zero)
        {
            UnhookWindowsHookEx(_mouseHook);
            _mouseHook = IntPtr.Zero;
        }

        _keyboardProc = null;
        _mouseProc = null;

        Log.Info("Input hooks released");
    }

    // Hook callbacks run on the installing thread and block all input while they run.
    // Windows drops a hook that exceeds LowLevelHooksTimeout (300ms by default), so
    // everything on this path has to stay cheap — no I/O, no locks, no allocation
    // beyond the unavoidable.
    private IntPtr KeyboardProc(int nCode, IntPtr wParam, IntPtr lParam)
    {
        if (nCode < 0)
        {
            return CallNextHookEx(IntPtr.Zero, nCode, wParam, lParam);
        }

        try
        {
            var message = (int)wParam;
            var isDown = message is WmKeyDown or WmSysKeyDown;
            var info = Marshal.PtrToStructure<KBDLLHOOKSTRUCT>(lParam);

            // Observers (the panic hatch) see the key before it is dropped. This is the
            // only way the emergency combo can work while input is being swallowed.
            KeyObserved?.Invoke((int)info.vkCode, isDown);
        }
        catch (Exception ex)
        {
            Log.Error($"Keyboard hook observer threw: {ex.Message}");
        }

        // Non-zero: consumed, do not pass on.
        return 1;
    }

    private IntPtr MouseProc(int nCode, IntPtr wParam, IntPtr lParam)
    {
        if (nCode < 0)
        {
            return CallNextHookEx(IntPtr.Zero, nCode, wParam, lParam);
        }

        if (!BlockMouseMovement && (int)wParam == WmMouseMove)
        {
            return CallNextHookEx(IntPtr.Zero, nCode, wParam, lParam);
        }

        return 1;
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        Release();
        _disposed = true;
    }

    // ---- Win32 -------------------------------------------------------------

    private const int WhKeyboardLl = 13;
    private const int WhMouseLl = 14;
    private const int WmKeyDown = 0x0100;
    private const int WmSysKeyDown = 0x0104;
    private const int WmMouseMove = 0x0200;

    [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern IntPtr SetWindowsHookEx(int idHook, LowLevelProc lpfn, IntPtr hMod, uint dwThreadId);

    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool UnhookWindowsHookEx(IntPtr hhk);

    [DllImport("user32.dll")]
    private static extern IntPtr CallNextHookEx(IntPtr hhk, int nCode, IntPtr wParam, IntPtr lParam);

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern IntPtr GetModuleHandle(string? lpModuleName);

    [StructLayout(LayoutKind.Sequential)]
    private struct KBDLLHOOKSTRUCT
    {
        public uint vkCode;
        public uint scanCode;
        public uint flags;
        public uint time;
        public IntPtr dwExtraInfo;
    }
}
