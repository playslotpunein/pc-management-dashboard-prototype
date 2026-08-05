using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Extensions.Options;

namespace PlaySlot.Watchdog;

/// <summary>
/// Starts a process in the active interactive session from a service running in Session 0.
///
/// Process.Start cannot do this. A service lives in Session 0, which has been isolated
/// from the interactive desktop since Vista, so anything it starts runs there too —
/// invisible to the logged-in user, unable to draw a window. Getting onto the real
/// desktop means duplicating the console session's user token and calling
/// CreateProcessAsUser against window station "winsta0\default".
/// </summary>
internal sealed class SessionLauncher
{
    private readonly ILogger<SessionLauncher> _log;
    private readonly WatchdogOptions _options;

    public SessionLauncher(ILogger<SessionLauncher> log, IOptions<WatchdogOptions> options)
    {
        _log = log;
        _options = options.Value;
    }

    /// <summary>
    /// Launches <paramref name="exePath"/> in the active console session.
    /// Returns the new process id, or null when there is no interactive session to
    /// launch into (nobody logged in, or sitting at the lock screen).
    /// </summary>
    public int? LaunchInActiveSession(string exePath, string? arguments = null)
    {
        if (!File.Exists(exePath))
        {
            _log.LogError("Agent executable not found at {Path}", exePath);
            return null;
        }

        var sessionId = WTSGetActiveConsoleSessionId();
        if (sessionId == InvalidSessionId)
        {
            _log.LogInformation("No active console session; deferring agent launch");
            return null;
        }

        var userToken = IntPtr.Zero;
        var primaryToken = IntPtr.Zero;
        var environmentBlock = IntPtr.Zero;
        var processInfo = default(PROCESS_INFORMATION);

        try
        {
            // Requires SE_TCB_NAME, which is why the service must run as LocalSystem.
            if (!WTSQueryUserToken(sessionId, out userToken))
            {
                var error = Marshal.GetLastWin32Error();

                if (error is ErrorNoToken or ErrorNoSuchLogonSession)
                {
                    _log.LogInformation(
                        "Session {SessionId} has no logged-on user; deferring agent launch", sessionId);
                    return null;
                }

                _log.LogError(new Win32Exception(error),
                    "WTSQueryUserToken failed for session {SessionId}", sessionId);
                return null;
            }

            if (!DuplicateTokenEx(
                    userToken,
                    MaximumAllowed,
                    IntPtr.Zero,
                    SECURITY_IMPERSONATION_LEVEL.SecurityIdentification,
                    TOKEN_TYPE.TokenPrimary,
                    out primaryToken))
            {
                _log.LogError(new Win32Exception(Marshal.GetLastWin32Error()), "DuplicateTokenEx failed");
                return null;
            }

            // Without the user's own environment block the agent inherits the service's,
            // so %USERPROFILE% and %APPDATA% point at the SYSTEM profile.
            if (!CreateEnvironmentBlock(out environmentBlock, primaryToken, false))
            {
                _log.LogWarning(new Win32Exception(Marshal.GetLastWin32Error()),
                    "CreateEnvironmentBlock failed; continuing with an inherited environment");
                environmentBlock = IntPtr.Zero;
            }

            var startupInfo = new STARTUPINFO
            {
                cb = Marshal.SizeOf<STARTUPINFO>(),
                // The interactive window station and desktop. Omitting this is the other
                // classic way to end up with an invisible process.
                lpDesktop = @"winsta0\default",
                dwFlags = StartfUseShowWindow,
                wShowWindow = _options.ShowAgentWindow ? SwShow : SwHide
            };

            var creationFlags = CreateUnicodeEnvironment |
                                (_options.ShowAgentWindow ? CreateNewConsole : CreateNoWindow);

            // Quoted so a path containing spaces is parsed as a single argument.
            var commandLine = string.IsNullOrWhiteSpace(arguments)
                ? $"\"{exePath}\""
                : $"\"{exePath}\" {arguments}";

            // CreateProcessAsUser may write into lpCommandLine, so it gets a mutable
            // buffer rather than a marshalled string literal.
            var commandLineBuffer = new StringBuilder(commandLine, commandLine.Length + 64);

            var workingDirectory = Path.GetDirectoryName(exePath);

            if (!CreateProcessAsUser(
                    primaryToken,
                    null,
                    commandLineBuffer,
                    IntPtr.Zero,
                    IntPtr.Zero,
                    false,
                    creationFlags,
                    environmentBlock,
                    workingDirectory,
                    ref startupInfo,
                    out processInfo))
            {
                _log.LogError(new Win32Exception(Marshal.GetLastWin32Error()),
                    "CreateProcessAsUser failed for {Path}", exePath);
                return null;
            }

            var pid = (int)processInfo.dwProcessId;
            _log.LogInformation("Launched agent PID {Pid} in session {SessionId}", pid, sessionId);
            return pid;
        }
        catch (Exception ex)
        {
            _log.LogError(ex, "Unexpected failure launching {Path}", exePath);
            return null;
        }
        finally
        {
            if (processInfo.hProcess != IntPtr.Zero) CloseHandle(processInfo.hProcess);
            if (processInfo.hThread != IntPtr.Zero) CloseHandle(processInfo.hThread);
            if (environmentBlock != IntPtr.Zero) DestroyEnvironmentBlock(environmentBlock);
            if (primaryToken != IntPtr.Zero) CloseHandle(primaryToken);
            if (userToken != IntPtr.Zero) CloseHandle(userToken);
        }
    }

    // ---- Win32 constants ----------------------------------------------------

    private const uint InvalidSessionId = 0xFFFFFFFF;
    private const uint MaximumAllowed = 0x02000000;
    private const uint CreateUnicodeEnvironment = 0x00000400;
    private const uint CreateNewConsole = 0x00000010;
    private const uint CreateNoWindow = 0x08000000;
    private const int StartfUseShowWindow = 0x00000001;
    private const short SwHide = 0;
    private const short SwShow = 5;
    private const int ErrorNoToken = 1008;
    private const int ErrorNoSuchLogonSession = 1312;

    // ---- Win32 interop ------------------------------------------------------

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint WTSGetActiveConsoleSessionId();

    [DllImport("wtsapi32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool WTSQueryUserToken(uint sessionId, out IntPtr phToken);

    [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool DuplicateTokenEx(
        IntPtr hExistingToken,
        uint dwDesiredAccess,
        IntPtr lpTokenAttributes,
        SECURITY_IMPERSONATION_LEVEL impersonationLevel,
        TOKEN_TYPE tokenType,
        out IntPtr phNewToken);

    [DllImport("userenv.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CreateEnvironmentBlock(
        out IntPtr lpEnvironment,
        IntPtr hToken,
        [MarshalAs(UnmanagedType.Bool)] bool bInherit);

    [DllImport("userenv.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool DestroyEnvironmentBlock(IntPtr lpEnvironment);

    [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CreateProcessAsUser(
        IntPtr hToken,
        string? lpApplicationName,
        StringBuilder? lpCommandLine,
        IntPtr lpProcessAttributes,
        IntPtr lpThreadAttributes,
        [MarshalAs(UnmanagedType.Bool)] bool bInheritHandles,
        uint dwCreationFlags,
        IntPtr lpEnvironment,
        string? lpCurrentDirectory,
        ref STARTUPINFO lpStartupInfo,
        out PROCESS_INFORMATION lpProcessInformation);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CloseHandle(IntPtr hObject);

    private enum SECURITY_IMPERSONATION_LEVEL
    {
        SecurityAnonymous = 0,
        SecurityIdentification = 1,
        SecurityImpersonation = 2,
        SecurityDelegation = 3
    }

    private enum TOKEN_TYPE
    {
        TokenPrimary = 1,
        TokenImpersonation = 2
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct STARTUPINFO
    {
        public int cb;
        public string? lpReserved;
        public string? lpDesktop;
        public string? lpTitle;
        public int dwX;
        public int dwY;
        public int dwXSize;
        public int dwYSize;
        public int dwXCountChars;
        public int dwYCountChars;
        public int dwFillAttribute;
        public int dwFlags;
        public short wShowWindow;
        public short cbReserved2;
        public IntPtr lpReserved2;
        public IntPtr hStdInput;
        public IntPtr hStdOutput;
        public IntPtr hStdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct PROCESS_INFORMATION
    {
        public IntPtr hProcess;
        public IntPtr hThread;
        public uint dwProcessId;
        public uint dwThreadId;
    }
}
