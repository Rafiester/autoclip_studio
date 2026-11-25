using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Net;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

[assembly: AssemblyTitle("AutoClip Studio")]
[assembly: AssemblyProduct("AutoClip Studio")]
[assembly: AssemblyDescription("AutoClip Studio source launcher")]
[assembly: AssemblyVersion("1.0.0.0")]

internal static class Launcher
{
    private const string Title = "AutoClip Studio";
    private static readonly string Root = Path.GetFullPath(AppDomain.CurrentDomain.BaseDirectory);
    private static readonly string Python = Path.Combine(Root, ".venv", "Scripts", "python.exe");
    private static readonly string Pythonw = Path.ChangeExtension(Python, null) + "w.exe";
    private static readonly string SetupScript = Path.Combine(Root, "scripts", "setup.py");
    private static readonly string LogPath = Path.Combine(DataRoot(), "logs", "launcher.log");

    [STAThread]
    private static int Main(string[] arguments)
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        string option = arguments.Length == 0 ? "" : arguments[0];
        string instance = @"Local\AutoClipStudio-" + Hash(new MemoryStream(
            Encoding.UTF8.GetBytes(Root.TrimEnd('\\').ToUpperInvariant()))).Substring(0, 24);
        try
        {
            if (arguments.Length > 1 || (option != "" && option != "--check" && option != "--setup" && option != "--stop"))
                throw new ArgumentException("Supported options are --check, --setup, and --stop.");
            if (option == "--stop") return Stop(instance);
            using (var mutex = new Mutex(false, instance))
            {
                if (!WaitMutex(mutex, 0))
                {
                    if (option != "--check")
                        MessageBox.Show("AutoClip Studio is already running from this checkout.", Title);
                    return 1;
                }
                try
                {
                    using (var stop = new EventWaitHandle(false, EventResetMode.ManualReset, instance + "-stop"))
                    using (var job = new WindowsJob())
                    {
                        Directory.CreateDirectory(Path.GetDirectoryName(LogPath));
                        return Launch(job, stop, option);
                    }
                }
                finally { mutex.ReleaseMutex(); }
            }
        }
        catch (Exception error)
        {
            try { Log(error.ToString()); } catch (IOException) { } catch (UnauthorizedAccessException) { }
            if (option != "--check")
                MessageBox.Show(error.Message + "\n\nLauncher log: " + LogPath, Title,
                    MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }

    private static int Launch(WindowsJob job, EventWaitHandle stop, string option)
    {
        string entry = Path.Combine(Root, "main.py");
        if (!File.Exists(entry) || !File.Exists(SetupScript))
            throw new FileNotFoundException("Keep this launcher in the AutoClip Studio source checkout.");
        Environment.SetEnvironmentVariable("UV_PYTHON_INSTALL_DIR", Path.Combine(Root, ".runtime"));
        Environment.SetEnvironmentVariable("UV_CACHE_DIR", Path.Combine(Root, ".uv-cache"));
        Environment.SetEnvironmentVariable("PYTHONIOENCODING", "utf-8");
        Log("Checking the checkout runtime");
        bool ready = IsReady(job);
        if (option == "--check") return ready ? 0 : 1;
        if (stop.WaitOne(0)) return 0;
        if (option == "--setup" || !ready)
        {
            if (option != "--setup" && MessageBox.Show(
                "The project-local Python environment or media tools need setup or repair.\n\n" +
                "Install the required dependencies now? An internet connection may be needed.\n\n" +
                "Details: " + LogPath, Title, MessageBoxButtons.YesNo, MessageBoxIcon.Information) != DialogResult.Yes)
                return 1;
            if (!RunSetupDialog(job, stop)) return 1;
            if (option == "--setup" || stop.WaitOne(0)) return 0;
        }

        Environment.SetEnvironmentVariable("AUTOCLIP_MANAGED_LAUNCH", "1");
        Log("Launching source main.py");
        using (var application = job.Start(Pythonw, new[] { entry }, Root, LogPath))
        {
            while (!application.Wait(100))
            {
                if (!stop.WaitOne(0)) continue;
                Log("Requesting a graceful shutdown");
                for (int attempt = 0; attempt < 30; attempt++)
                {
                    job.RequestClose();
                    if (application.Wait(1000))
                    {
                        Log("Source process stopped");
                        return 0;
                    }
                }
                Log("Graceful shutdown timed out; closing the checkout job");
                return 0;
            }
            int code = application.ExitCode();
            Log("Source process exited with code " + code);
            if (code != 0 && !stop.WaitOne(0))
                throw new InvalidOperationException("AutoClip Studio could not start or exited unexpectedly.\n" +
                    "Application log: " + Path.Combine(DataRoot(), "logs", "autoclip.log"));
            return code;
        }
    }

    private static bool IsReady(WindowsJob job)
    {
        if (!File.Exists(Python))
        {
            Log("The project-local Python environment is missing");
            return false;
        }
        try { return Run(job, Python, SetupScript, "--check") == 0; }
        catch (System.ComponentModel.Win32Exception error)
        {
            Log(error.Message);
            return false;
        }
    }

    private static bool RunSetupDialog(WindowsJob job, EventWaitHandle stop)
    {
        using (var form = new Form())
        using (var timer = new System.Windows.Forms.Timer())
        {
            form.Text = Title + " Setup";
            form.Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
            form.ClientSize = new Size(425, 155);
            form.FormBorderStyle = FormBorderStyle.FixedDialog;
            form.StartPosition = FormStartPosition.CenterScreen;
            form.MaximizeBox = false;
            form.MinimizeBox = false;
            var text = new Label { Left = 18, Top = 17, Width = 385, Height = 42,
                Text = "Preparing the project-local Python environment and media tools. This may take a few minutes." };
            var bar = new ProgressBar { Left = 18, Top = 66, Width = 389, Height = 13,
                Style = ProgressBarStyle.Marquee, AccessibleName = "Preparing dependencies" };
            var cancel = new Button { Left = 307, Top = 102, Width = 100, Height = 30,
                Text = "Cancel setup", DialogResult = DialogResult.Cancel };
            form.Controls.AddRange(new Control[] { text, bar, cancel });
            form.CancelButton = cancel;
            Exception failure = null;
            timer.Interval = 200;
            timer.Tick += delegate
            {
                if (stop.WaitOne(0)) form.DialogResult = DialogResult.Cancel;
            };
            form.Shown += delegate
            {
                timer.Start();
                ThreadPool.QueueUserWorkItem(delegate
                {
                    try { Install(job); }
                    catch (Exception error) { failure = error; }
                    try
                    {
                        form.BeginInvoke((Action)delegate
                        {
                            form.DialogResult = failure == null ? DialogResult.OK : DialogResult.Abort;
                        });
                    }
                    catch (InvalidOperationException) { }
                });
            };
            DialogResult result = form.ShowDialog();
            if (result == DialogResult.Abort && failure != null) throw failure;
            return result == DialogResult.OK;
        }
    }

    private static void Install(WindowsJob job)
    {
        string uv = EnsureUv();
        bool supported = File.Exists(Python) && Run(job, Python, "-c",
            "import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] < (3,13) else 1)") == 0;
        int status = supported ? Run(job, Python, SetupScript) :
            Run(job, uv, "run", "--no-project", "--managed-python", "--python", "3.12", "--", "python", SetupScript);
        if (status != 0) throw new InvalidOperationException("AutoClip Studio setup failed. Check the launcher log for details.");
    }

    private static string EnsureUv()
    {
        string tools = Path.Combine(Root, "tools");
        string uv = Path.Combine(tools, "uv.exe");
        if (File.Exists(uv)) return uv;
        Directory.CreateDirectory(tools);
        string json = File.ReadAllText(Path.Combine(Root, "scripts", "toolchain.json"));
        var manifest = new JavaScriptSerializer().Deserialize<Dictionary<string, Dictionary<string, string>>>(json);
        var release = manifest["uv"];
        string pending = Path.Combine(tools, "uv-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(pending);
        try
        {
            string archive = Path.Combine(pending, "uv.zip");
            Log("Downloading the pinned uv bootstrap");
            ServicePointManager.SecurityProtocol |= SecurityProtocolType.Tls12;
            using (var client = new WebClient()) client.DownloadFile(release["url"], archive);
            using (var stream = File.OpenRead(archive))
                if (!String.Equals(Hash(stream), release["sha256"], StringComparison.OrdinalIgnoreCase))
                    throw new IOException("The uv download failed SHA-256 verification.");
            using (var zip = ZipFile.OpenRead(archive))
            {
                foreach (var entry in zip.Entries)
                {
                    if (entry.Name != "uv.exe") continue;
                    string staged = Path.Combine(pending, "uv.exe");
                    entry.ExtractToFile(staged);
                    File.Move(staged, uv);
                    return uv;
                }
            }
            throw new IOException("The verified uv archive does not contain uv.exe.");
        }
        finally
        {
            File.Delete(Path.Combine(pending, "uv.zip"));
            File.Delete(Path.Combine(pending, "uv.exe"));
            Directory.Delete(pending);
        }
    }

    private static int Run(WindowsJob job, string executable, params string[] arguments)
    {
        using (var process = job.Start(executable, arguments, Root, LogPath))
        {
            process.Wait(uint.MaxValue);
            return process.ExitCode();
        }
    }

    private static int Stop(string instance)
    {
        try
        {
            using (var stop = EventWaitHandle.OpenExisting(instance + "-stop"))
            {
                stop.Set();
                using (var mutex = Mutex.OpenExisting(instance))
                {
                    if (!WaitMutex(mutex, 35000)) return 1;
                    mutex.ReleaseMutex();
                }
            }
        }
        catch (WaitHandleCannotBeOpenedException) { }
        return 0;
    }

    private static bool WaitMutex(Mutex mutex, int timeout)
    {
        try { return mutex.WaitOne(timeout); }
        catch (AbandonedMutexException) { return true; }
    }

    private static string DataRoot()
    {
        string configured = Environment.GetEnvironmentVariable("AUTOCLIP_DATA_DIR");
        return String.IsNullOrWhiteSpace(configured)
            ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "AutoClip", Title)
            : Path.GetFullPath(Path.IsPathRooted(configured) ? configured : Path.Combine(Root, configured));
    }

    private static string Hash(Stream stream)
    {
        using (var sha = SHA256.Create())
            return BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
    }

    private static void Log(string message)
    {
        using (var stream = new FileStream(LogPath, FileMode.Append, FileAccess.Write, FileShare.ReadWrite))
        using (var writer = new StreamWriter(stream, new UTF8Encoding(false)))
            writer.WriteLine(DateTimeOffset.Now.ToString("o") + " " + message);
    }
}
