using System.Diagnostics;
using PlaySlot.Agent.Ipc;
using PlaySlot.Agent.Lock;

namespace PlaySlot.Agent;

internal static class Program
{
    // WinForms requires a single-threaded apartment, and the low-level hooks require the
    // message pump that Application.Run provides on this same thread.
    [STAThread]
    private static int Main(string[] args)
    {
        if (args.Any(a => a is "--help" or "-h" or "/?"))
        {
            Console.WriteLine(AgentOptions.UsageText);
            return 0;
        }

        var options = AgentOptions.Parse(args);

        // Client mode: hand the command to a running agent and exit without starting
        // any of the machinery below.
        if (!string.IsNullOrWhiteSpace(options.SendCommand))
        {
            return ControlClient.SendAsync(options, options.SendCommand!).GetAwaiter().GetResult();
        }

        ApplicationConfiguration.Initialize();

        // WinForms installs its synchronization context when the first control handle is
        // created on the thread, not before. Capture Current any earlier and you get null,
        // or the base SynchronizationContext whose Send runs the delegate INLINE on the
        // calling thread instead of marshalling. That puts SetWindowsHookEx on a thread-pool
        // thread with no message pump, and Windows silently removes a low-level hook it
        // cannot service (LowLevelHooksTimeout, 5s by default): the lock engages, then input
        // returns a few seconds later with nothing logged. This control exists to force the
        // handle — and therefore the context — onto the pumping thread first.
        using var marshaller = new Control();
        _ = marshaller.Handle;

        PrintBanner(options);

        using var shutdown = new CancellationTokenSource();
        using var controller = new LockController(options);

        // Captured on the UI thread so background work can marshal back to it.
        var uiContext = SynchronizationContext.Current;

        // Fail loudly rather than degrade to an inline Send that silently breaks the lock.
        if (uiContext is not WindowsFormsSynchronizationContext)
        {
            throw new InvalidOperationException(
                $"Expected WindowsFormsSynchronizationContext, got {uiContext?.GetType().Name ?? "null"}. " +
                "Input hooks would install on a thread with no message pump and be dropped after ~5s.");
        }

        Log.Info($"UI thread {Environment.CurrentManagedThreadId} is pumping; hooks will install here");

        var heartbeat = new HeartbeatClient(options);
        _ = Task.Run(() => heartbeat.RunAsync(shutdown.Token), shutdown.Token);

        var control = new ControlServer(options, uiContext, command => Handle(controller, command));
        _ = Task.Run(() => control.RunAsync(shutdown.Token), shutdown.Token);

        if (options.LockAfterSeconds > 0)
        {
            ScheduleDemoLock(controller, options.LockAfterSeconds);
        }

        Console.CancelKeyPress += (_, e) =>
        {
            e.Cancel = true;
            Log.Info("Shutdown requested");
            shutdown.Cancel();
            Application.Exit();
        };

        // Whatever happens, do not exit with input still swallowed.
        AppDomain.CurrentDomain.ProcessExit += (_, _) => controller.Unlock("process exit");

        try
        {
            Application.Run();
        }
        finally
        {
            shutdown.Cancel();
            controller.Unlock("agent stopping");
        }

        return 0;
    }

    private static string Handle(LockController controller, string command)
    {
        switch (command.ToLowerInvariant())
        {
            case "lock":
                controller.Lock("control command");
                return $"OK locked ({controller.StatusLine})";

            case "unlock":
                controller.Unlock("control command");
                return $"OK unlocked ({controller.StatusLine})";

            case "status":
                return $"OK {controller.StatusLine}";

            case "ping":
                return "OK pong";

            default:
                return $"ERR unknown command '{command}'";
        }
    }

    private static void ScheduleDemoLock(LockController controller, int seconds)
    {
        var timer = new System.Windows.Forms.Timer { Interval = seconds * 1000 };

        timer.Tick += (_, _) =>
        {
            timer.Stop();
            timer.Dispose();
            controller.Lock($"--lock-after={seconds}");
        };

        timer.Start();

        Log.Info($"Demo lock scheduled in {seconds}s");
    }

    private static void PrintBanner(AgentOptions options)
    {
        using var process = Process.GetCurrentProcess();

        Console.Title = "PlaySlot.Agent";

        Log.Raw("PlaySlot.Agent — Phase 1 (Layers B + C)");
        Log.Raw(new string('-', 58));
        Log.Raw($"  PID          : {process.Id}");

        // The verification that matters when the watchdog launched this process.
        // Session 0 means CreateProcessAsUser did not take effect and the customer
        // would never see the overlay.
        Log.Raw($"  Session Id   : {process.SessionId}   <- must be >= 1, not 0");
        Log.Raw($"  User         : {Environment.UserDomainName}\\{Environment.UserName}");
        Log.Raw($"  Unit         : {options.UnitId}");
        Log.Raw($"  Monitors     : {Screen.AllScreens.Length}");
        Log.Raw($"  Heartbeat    : {(options.HeartbeatEnabled ? $"every {options.HeartbeatIntervalSeconds}s" : "DISABLED")}");
        Log.Raw($"  Control pipe : {(options.ControlPipeEnabled ? options.ControlPipeName : "disabled")}");
        Log.Raw($"  Panic hatch  : {PanicHatch.Describe(options.MaxLockSeconds, options.PanicComboEnabled)}");
        Log.Raw($"  Log file     : {Log.FilePath}");
        Log.Raw(new string('-', 58));

        if (process.SessionId == 0)
        {
            Log.Error("Running in Session 0 — NOT the interactive desktop. The overlay will not be visible.");
        }

        if (options.MaxLockSeconds == 0 && !options.PanicComboEnabled)
        {
            Log.Warn("Panic hatch fully disabled. A lock can only be released by the control channel.");
        }

        Log.Raw(string.Empty);
        Log.Info("Ready. Drive it with:  PlaySlot.Agent.exe --send lock   |   --send unlock   |   --send status");
    }
}
