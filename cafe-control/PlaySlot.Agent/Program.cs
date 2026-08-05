using System.Diagnostics;
using System.IO.Pipes;
using System.Text;

// PlaySlot.Agent — test stub.
//
// Stands in for the real client agent while the watchdog is being built. It does one
// thing: connect to the watchdog's named pipe every few seconds and write "alive".
// The flags exist to drive the watchdog's failure paths on demand.
//
//   --no-heartbeat        never beat at all; the process stays up, so this exercises
//                         the hung-agent path (PID present, heartbeat stale)
//   --hang-after=<secs>   beat normally, then stop after N seconds — simulates an
//                         agent that hangs partway through a session
//   --pipe=<name>         override the pipe name (default PlaySlotAgentHeartbeat)
//   --interval=<secs>     override the beat interval (default 5)

const string DefaultPipeName = "PlaySlotAgentHeartbeat";
const int DefaultIntervalSeconds = 5;

var pipeName = GetOption(args, "--pipe") ?? DefaultPipeName;
var intervalSeconds = ParseInt(GetOption(args, "--interval"), DefaultIntervalSeconds);
var noHeartbeat = HasFlag(args, "--no-heartbeat");
var hangAfterSeconds = ParseInt(GetOption(args, "--hang-after"), 0);

using var process = Process.GetCurrentProcess();

Console.Title = "PlaySlot.Agent (test stub)";
Console.WriteLine("PlaySlot.Agent — test stub");
Console.WriteLine(new string('-', 52));
Console.WriteLine($"  PID          : {process.Id}");

// The verification that matters. Session 0 means the watchdog launched it as a plain
// child process and the user would never see it; 1 or higher means it landed on the
// real desktop via CreateProcessAsUser.
Console.WriteLine($"  Session Id   : {process.SessionId}   <- must be >= 1, not 0");
Console.WriteLine($"  User         : {Environment.UserDomainName}\\{Environment.UserName}");
Console.WriteLine($"  Interactive  : {Environment.UserInteractive}");
Console.WriteLine($"  Working dir  : {Environment.CurrentDirectory}");
Console.WriteLine($"  Pipe         : {pipeName}");

if (noHeartbeat)
{
    Console.WriteLine("  Heartbeat    : DISABLED (--no-heartbeat)");
}
else if (hangAfterSeconds > 0)
{
    Console.WriteLine($"  Heartbeat    : every {intervalSeconds}s, stopping after {hangAfterSeconds}s");
}
else
{
    Console.WriteLine($"  Heartbeat    : every {intervalSeconds}s");
}

Console.WriteLine(new string('-', 52));

if (process.SessionId == 0)
{
    Console.WriteLine();
    Console.WriteLine("  WARNING: running in Session 0 — this is NOT the interactive desktop.");
    Console.WriteLine("  The watchdog fell back to a plain launch instead of CreateProcessAsUser.");
    Console.WriteLine();
}

using var cancellation = new CancellationTokenSource();

Console.CancelKeyPress += (_, e) =>
{
    e.Cancel = true;
    Console.WriteLine("Shutting down...");
    cancellation.Cancel();
};

var startedAt = DateTime.UtcNow;
var token = cancellation.Token;

while (!token.IsCancellationRequested)
{
    var elapsed = DateTime.UtcNow - startedAt;
    var stopped = noHeartbeat || (hangAfterSeconds > 0 && elapsed.TotalSeconds >= hangAfterSeconds);

    if (stopped)
    {
        Console.WriteLine($"[{DateTime.Now:HH:mm:ss}] heartbeat suppressed — waiting to be killed");
    }
    else
    {
        await SendHeartbeatAsync(pipeName, token);
    }

    try
    {
        await Task.Delay(TimeSpan.FromSeconds(intervalSeconds), token);
    }
    catch (OperationCanceledException)
    {
        break;
    }
}

return 0;

static async Task SendHeartbeatAsync(string pipeName, CancellationToken token)
{
    try
    {
        using var client = new NamedPipeClientStream(
            ".", pipeName, PipeDirection.Out, PipeOptions.Asynchronous);

        await client.ConnectAsync(2000, token);

        // Disposing the writer closes the pipe, which is what signals end-of-message
        // to the watchdog's reader.
        await using var writer = new StreamWriter(client, new UTF8Encoding(false))
        {
            AutoFlush = true
        };

        await writer.WriteLineAsync("alive");

        Console.WriteLine($"[{DateTime.Now:HH:mm:ss}] heartbeat sent");
    }
    catch (OperationCanceledException)
    {
        throw;
    }
    catch (TimeoutException)
    {
        Console.WriteLine($"[{DateTime.Now:HH:mm:ss}] watchdog pipe not available (service running?)");
    }
    catch (Exception ex)
    {
        Console.WriteLine($"[{DateTime.Now:HH:mm:ss}] heartbeat failed: {ex.Message}");
    }
}

static bool HasFlag(string[] args, string name) =>
    args.Any(a => string.Equals(a, name, StringComparison.OrdinalIgnoreCase));

// Accepts both "--opt=value" and "--opt value".
static string? GetOption(string[] args, string name)
{
    for (var i = 0; i < args.Length; i++)
    {
        var arg = args[i];

        if (arg.StartsWith(name + "=", StringComparison.OrdinalIgnoreCase))
        {
            return arg[(name.Length + 1)..];
        }

        if (string.Equals(arg, name, StringComparison.OrdinalIgnoreCase) && i + 1 < args.Length)
        {
            return args[i + 1];
        }
    }

    return null;
}

static int ParseInt(string? value, int fallback) =>
    int.TryParse(value, out var parsed) && parsed > 0 ? parsed : fallback;
