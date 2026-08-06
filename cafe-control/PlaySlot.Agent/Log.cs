namespace PlaySlot.Agent;

/// <summary>
/// Console plus a rolling file under %LOCALAPPDATA%. The file matters because the
/// production agent runs with no console window (the watchdog uses CREATE_NO_WINDOW),
/// so stdout goes nowhere.
/// </summary>
internal static class Log
{
    private const long MaxBytes = 2 * 1024 * 1024;

    private static readonly object Gate = new();
    private static readonly string LogPath = BuildLogPath();

    public static void Info(string message) => Write("INFO ", message, ConsoleColor.Gray);

    public static void Warn(string message) => Write("WARN ", message, ConsoleColor.Yellow);

    public static void Error(string message) => Write("ERROR", message, ConsoleColor.Red);

    public static void Raw(string message)
    {
        Console.WriteLine(message);
        Append(message);
    }

    private static void Write(string level, string message, ConsoleColor colour)
    {
        var line = $"[{DateTime.Now:HH:mm:ss}] {level} {message}";

        lock (Gate)
        {
            var previous = Console.ForegroundColor;

            try
            {
                Console.ForegroundColor = colour;
                Console.WriteLine(line);
            }
            catch (IOException)
            {
                // No console attached — the file is the record.
            }
            finally
            {
                try
                {
                    Console.ForegroundColor = previous;
                }
                catch (IOException)
                {
                    // Ignored for the same reason.
                }
            }
        }

        Append(line);
    }

    private static void Append(string line)
    {
        lock (Gate)
        {
            try
            {
                var info = new FileInfo(LogPath);

                if (info.Exists && info.Length > MaxBytes)
                {
                    var previous = LogPath + ".1";

                    if (File.Exists(previous))
                    {
                        File.Delete(previous);
                    }

                    File.Move(LogPath, previous);
                }

                File.AppendAllText(LogPath, line + Environment.NewLine);
            }
            catch
            {
                // Logging must never take the agent down.
            }
        }
    }

    private static string BuildLogPath()
    {
        try
        {
            var directory = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "PlaySlot");

            Directory.CreateDirectory(directory);
            return Path.Combine(directory, "agent.log");
        }
        catch
        {
            return Path.Combine(Path.GetTempPath(), "playslot-agent.log");
        }
    }

    public static string FilePath => LogPath;
}
