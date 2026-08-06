using System.IO.Pipes;
using System.Text;

namespace PlaySlot.Agent.Ipc;

/// <summary>
/// The --send side of the control pipe. The same binary acts as the client so there is
/// no second tool to build or deploy:
///
///     PlaySlot.Agent.exe --send lock
///     PlaySlot.Agent.exe --send status
/// </summary>
internal static class ControlClient
{
    public static async Task<int> SendAsync(AgentOptions options, string command)
    {
        try
        {
            using var client = new NamedPipeClientStream(
                ".", options.ControlPipeName, PipeDirection.InOut, PipeOptions.Asynchronous);

            await client.ConnectAsync(3000).ConfigureAwait(false);

            await using var writer = new StreamWriter(client, new UTF8Encoding(false), leaveOpen: true)
            {
                AutoFlush = true
            };

            using var reader = new StreamReader(client, Encoding.UTF8, leaveOpen: true);

            var payload = string.IsNullOrEmpty(options.ControlToken)
                ? command
                : $"{options.ControlToken} {command}";

            await writer.WriteLineAsync(payload).ConfigureAwait(false);

            var response = await reader.ReadLineAsync().ConfigureAwait(false);

            Console.WriteLine(response ?? "(no response)");

            return response is not null && response.StartsWith("OK", StringComparison.Ordinal) ? 0 : 1;
        }
        catch (TimeoutException)
        {
            Console.Error.WriteLine(
                $"No agent listening on pipe '{options.ControlPipeName}'. Is the agent running?");
            return 2;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"Send failed: {ex.Message}");
            return 2;
        }
    }
}
