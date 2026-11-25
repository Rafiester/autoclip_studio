using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal sealed class SetupWizardForm : Form
{
    private static readonly Color BgApp = Color.FromArgb(16, 17, 22);
    private static readonly Color BgCard = Color.FromArgb(26, 28, 35);
    private static readonly Color BgElevated = Color.FromArgb(34, 37, 46);
    private static readonly Color BorderSubtle = Color.FromArgb(43, 46, 56);
    private static readonly Color RedPrimary = Color.FromArgb(229, 57, 53);
    private static readonly Color TextPrimary = Color.FromArgb(245, 245, 247);
    private static readonly Color TextMuted = Color.FromArgb(163, 168, 181);
    private static readonly Color GreenOk = Color.FromArgb(46, 204, 113);
    private static readonly Color AmberWarn = Color.FromArgb(245, 166, 35);

    private sealed class ComponentRow
    {
        public string Id;
        public string Name;
        public bool Mandatory;
        public long EstimatedBytes;
        public long? DownloadBytes;
        public Panel Card;
        public Label TitleLabel;
        public Label StateLabel;
        public Label DetailLabel;
        public ProgressBar Progress;
        public string State = "Pending";
    }

    private readonly string _root;
    private readonly string _python;
    private readonly string _setupScript;
    private readonly WindowsJob _job;
    private readonly EventWaitHandle _stop;
    private readonly Action<string> _log;

    private readonly List<ComponentRow> _rows = new List<ComponentRow>();
    private readonly Dictionary<string, ComponentRow> _rowMap = new Dictionary<string, ComponentRow>();
    private readonly ManualResetEventSlim _pauseGate = new ManualResetEventSlim(true);
    private readonly CancellationTokenSource _cts = new CancellationTokenSource();

    private Label _sysInfoLabel;
    private Label _spaceInfoLabel;
    private Label _activityLabel;
    private Label _metricsLabel;
    private Label _completedLabel;
    private ProgressBar _overallBar;
    private CheckBox _cloudOnlyCheck;
    private Button _pauseBtn;
    private Button _retryBtn;
    private Button _cancelBtn;
    private Button _launchBtn;
    private FlowLayoutPanel _listPanel;
    private bool _running;

    public SetupWizardForm(string root, string python, string setupScript, WindowsJob job, EventWaitHandle stop, Action<string> log)
    {
        _root = root;
        _python = python;
        _setupScript = setupScript;
        _job = job;
        _stop = stop;
        _log = log;

        Text = "AutoClip Studio — First-Time Setup";
        Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
        BackColor = BgApp;
        ForeColor = TextPrimary;
        Font = new Font("Segoe UI", 9.5f);
        FormBorderStyle = FormBorderStyle.FixedDialog;
        StartPosition = FormStartPosition.CenterScreen;
        MaximizeBox = false;
        AutoScaleMode = AutoScaleMode.Dpi;
        ClientSize = new Size(740, 610);

        BuildLayout();
        LoadManifestAndProbe();

        var stopTimer = new System.Windows.Forms.Timer { Interval = 250 };
        stopTimer.Tick += delegate
        {
            if (_stop.WaitOne(0))
            {
                _cts.Cancel();
                DialogResult = DialogResult.Cancel;
            }
        };
        stopTimer.Start();

        Shown += delegate { BeginInstall(); };
        FormClosing += delegate
        {
            _cts.Cancel();
            _pauseGate.Set();
            stopTimer.Stop();
        };
    }

    private void BuildLayout()
    {
        var header = new Panel { Left = 20, Top = 16, Width = 700, Height = 54, BackColor = BgApp };
        string logoPath = ResolveLogoPath();
        if (logoPath != null)
        {
            try
            {
                var pic = new PictureBox
                {
                    Left = 0, Top = 4, Width = 148, Height = 44,
                    SizeMode = PictureBoxSizeMode.Zoom, BackColor = BgApp,
                    Image = Image.FromFile(logoPath)
                };
                header.Controls.Add(pic);
            }
            catch { }
        }
        var title = new Label
        {
            Left = logoPath != null ? 160 : 0, Top = 4, Width = 520, Height = 24,
            Text = "First-Time Setup & Dependency Verification",
            Font = new Font("Segoe UI", 13f, FontStyle.Bold), ForeColor = TextPrimary
        };
        var subtitle = new Label
        {
            Left = logoPath != null ? 160 : 0, Top = 30, Width = 520, Height = 20,
            Text = "Verifying and preparing local components before launching AutoClip Studio.",
            ForeColor = TextMuted
        };
        header.Controls.Add(title);
        header.Controls.Add(subtitle);
        Controls.Add(header);

        var infoCard = new Panel { Left = 20, Top = 78, Width = 700, Height = 74, BackColor = BgCard };
        infoCard.Paint += DrawCardBorder;
        _sysInfoLabel = new Label { Left = 14, Top = 10, Width = 672, Height = 20, ForeColor = TextPrimary };
        _spaceInfoLabel = new Label { Left = 14, Top = 31, Width = 672, Height = 20, ForeColor = TextMuted };
        _cloudOnlyCheck = new CheckBox
        {
            Left = 14, Top = 50, Width = 480, Height = 20, ForeColor = TextPrimary,
            Text = "Cloud-only transcription (skip downloading local Whisper Large v3 Turbo model)"
        };
        infoCard.Controls.AddRange(new Control[] { _sysInfoLabel, _spaceInfoLabel, _cloudOnlyCheck });
        Controls.Add(infoCard);

        var overallCard = new Panel { Left = 20, Top = 160, Width = 700, Height = 92, BackColor = BgCard };
        overallCard.Paint += DrawCardBorder;
        _activityLabel = new Label
        {
            Left = 14, Top = 10, Width = 470, Height = 20,
            Text = "Checking environment...", Font = new Font("Segoe UI", 9.5f, FontStyle.Bold), ForeColor = TextPrimary
        };
        _completedLabel = new Label
        {
            Left = 490, Top = 10, Width = 196, Height = 20,
            TextAlign = ContentAlignment.MiddleRight, ForeColor = TextMuted
        };
        _overallBar = new ProgressBar { Left = 14, Top = 36, Width = 672, Height = 16, Maximum = 100 };
        _metricsLabel = new Label
        {
            Left = 14, Top = 60, Width = 672, Height = 20,
            Text = "Speed: —  •  Downloaded: —  •  ETA: —", ForeColor = TextMuted
        };
        overallCard.Controls.AddRange(new Control[] { _activityLabel, _completedLabel, _overallBar, _metricsLabel });
        Controls.Add(overallCard);

        _listPanel = new FlowLayoutPanel
        {
            Left = 20, Top = 262, Width = 700, Height = 286,
            FlowDirection = FlowDirection.TopDown, WrapContents = false, AutoScroll = true,
            BackColor = BgApp
        };
        Controls.Add(_listPanel);

        _pauseBtn = MakeButton("Pause", 20, 560, 90, false);
        _pauseBtn.Click += TogglePause;
        _retryBtn = MakeButton("Retry Failed", 118, 560, 110, false);
        _retryBtn.Enabled = false;
        _retryBtn.Click += delegate { BeginInstall(); };
        _cancelBtn = MakeButton("Cancel", 448, 560, 100, false);
        _cancelBtn.Click += delegate
        {
            _cts.Cancel();
            _pauseGate.Set();
            DialogResult = DialogResult.Cancel;
        };
        _launchBtn = MakeButton("Launch Application", 556, 560, 164, true);
        _launchBtn.Enabled = false;
        _launchBtn.Click += delegate { DialogResult = DialogResult.OK; };
        Controls.AddRange(new Control[] { _pauseBtn, _retryBtn, _cancelBtn, _launchBtn });
    }

    private Button MakeButton(string text, int left, int top, int width, bool primary)
    {
        var btn = new Button
        {
            Left = left, Top = top, Width = width, Height = 34, Text = text,
            FlatStyle = FlatStyle.Flat, BackColor = primary ? RedPrimary : BgElevated,
            ForeColor = TextPrimary, Font = new Font("Segoe UI", 9.5f, primary ? FontStyle.Bold : FontStyle.Regular)
        };
        btn.FlatAppearance.BorderColor = primary ? RedPrimary : BorderSubtle;
        btn.FlatAppearance.BorderSize = 1;
        return btn;
    }

    private void DrawCardBorder(object sender, PaintEventArgs e)
    {
        var panel = (Control)sender;
        using (var pen = new Pen(BorderSubtle, 1))
            e.Graphics.DrawRectangle(pen, 0, 0, panel.Width - 1, panel.Height - 1);
    }

    private string ResolveLogoPath()
    {
        foreach (string candidate in new[]
        {
            Path.Combine(_root, "autoclip_studio", "assets", "AutoClipTrans.png"),
            Path.Combine(_root, "AutoClipTrans.png")
        })
        {
            if (File.Exists(candidate)) return candidate;
        }
        return null;
    }

    private void LoadManifestAndProbe()
    {
        string osDesc = Environment.Is64BitOperatingSystem ? "Windows x64" : "Windows x86";
        string gpuDesc = DetectNvidiaGpu() ?? "CPU mode (no NVIDIA GPU detected)";
        _sysInfoLabel.Text = string.Format("System: {0}  •  GPU: {1}", osDesc, gpuDesc);

        long freeBytes = new DriveInfo(Path.GetPathRoot(_root)).AvailableFreeSpace;
        long totalDownload = 0;
        long totalInstalled = 0;

        string manifestPath = Path.Combine(_root, "scripts", "dependencies.json");
        if (File.Exists(manifestPath))
        {
            var serializer = new JavaScriptSerializer();
            var doc = serializer.Deserialize<Dictionary<string, object>>(File.ReadAllText(manifestPath, Encoding.UTF8));
            var comps = doc.ContainsKey("components") ? doc["components"] as System.Collections.ArrayList : null;
            if (comps != null)
            {
                foreach (Dictionary<string, object> item in comps)
                {
                    string id = Convert.ToString(item["id"]);
                    string name = Convert.ToString(item["name"]);
                    bool mandatory = item.ContainsKey("mandatory") && Convert.ToBoolean(item["mandatory"]);
                    long estInst = item.ContainsKey("estimated_installed_bytes") && item["estimated_installed_bytes"] != null
                        ? Convert.ToInt64(item["estimated_installed_bytes"]) : 0;
                    long? dlBytes = item.ContainsKey("download_size_bytes") && item["download_size_bytes"] != null
                        ? (long?)Convert.ToInt64(item["download_size_bytes"]) : null;
                    if (dlBytes.HasValue) totalDownload += dlBytes.Value;
                    totalInstalled += estInst;
                    AddComponentCard(id, name, mandatory, dlBytes, estInst);
                }
            }
        }
        _spaceInfoLabel.Text = string.Format(
            "Required Download: ~{0} (+ packages)  •  Est. Installed: ~{1}  •  Available Disk: {2}",
            FormatBytes(totalDownload), FormatBytes(totalInstalled), FormatBytes(freeBytes));
        UpdateOverallProgress();
    }

    private void AddComponentCard(string id, string name, bool mandatory, long? dlBytes, long estBytes)
    {
        var card = new Panel { Width = 674, Height = 52, BackColor = BgCard, Margin = new Padding(0, 0, 0, 6) };
        card.Paint += DrawCardBorder;
        var title = new Label
        {
            Left = 12, Top = 7, Width = 430, Height = 18,
            Text = name + (mandatory ? "  (Required)" : "  (Optional)"),
            Font = new Font("Segoe UI", 9.5f, FontStyle.Bold), ForeColor = TextPrimary
        };
        var state = new Label
        {
            Left = 450, Top = 7, Width = 210, Height = 18,
            Text = "Pending", TextAlign = ContentAlignment.MiddleRight, ForeColor = TextMuted
        };
        var bar = new ProgressBar { Left = 12, Top = 30, Width = 340, Height = 10, Maximum = 100 };
        var detail = new Label
        {
            Left = 362, Top = 27, Width = 298, Height = 18,
            Text = dlBytes.HasValue ? FormatBytes(dlBytes.Value) : "Dynamic size",
            TextAlign = ContentAlignment.MiddleRight, ForeColor = TextMuted
        };
        card.Controls.AddRange(new Control[] { title, state, bar, detail });
        _listPanel.Controls.Add(card);

        var row = new ComponentRow
        {
            Id = id, Name = name, Mandatory = mandatory, DownloadBytes = dlBytes,
            EstimatedBytes = estBytes, Card = card, TitleLabel = title,
            StateLabel = state, DetailLabel = detail, Progress = bar
        };
        _rows.Add(row);
        _rowMap[id] = row;
    }

    private void TogglePause(object sender, EventArgs e)
    {
        if (_pauseGate.IsSet)
        {
            _pauseGate.Reset();
            _pauseBtn.Text = "Resume";
            SetActivity("Installation paused.");
        }
        else
        {
            _pauseGate.Set();
            _pauseBtn.Text = "Pause";
            SetActivity("Resuming installation...");
        }
    }

    private void BeginInstall()
    {
        if (_running) return;
        _running = true;
        _retryBtn.Enabled = false;
        _launchBtn.Enabled = false;
        bool cloudOnly = _cloudOnlyCheck.Checked;
        _cloudOnlyCheck.Enabled = false;

        ThreadPool.QueueUserWorkItem(delegate
        {
            try
            {
                RunInstallPipeline(cloudOnly);
                SafeUi(delegate
                {
                    SetActivity("All required dependencies verified. Launching AutoClip Studio...");
                    _overallBar.Value = 100;
                    _launchBtn.Enabled = true;
                    _pauseBtn.Enabled = false;
                    DialogResult = DialogResult.OK;
                });
            }
            catch (Exception error)
            {
                _log("Setup failed: " + error);
                SafeUi(delegate
                {
                    SetActivity("Setup failed: " + error.Message);
                    _retryBtn.Enabled = true;
                    _cloudOnlyCheck.Enabled = true;
                });
            }
            finally
            {
                _running = false;
            }
        });
    }

    private void RunInstallPipeline(bool cloudOnly)
    {
        string uv = EnsureUvWithProgress();
        _cts.Token.ThrowIfCancellationRequested();

        SafeUi(delegate { SetActivity("Installing Python runtime, packages, and media tools..."); });
        string modeFlag = cloudOnly ? "--cloud-only" : "--local-whisper";
        string[] args = File.Exists(_python) && CheckPythonVersion()
            ? new[] { _setupScript, "--json-progress", "--no-tests", modeFlag }
            : new[] { "run", "--no-project", "--managed-python", "--python", "3.12", "--", "python", _setupScript, "--json-progress", "--no-tests", modeFlag };
        string exe = args[0] == _setupScript ? _python : uv;

        var psi = new ProcessStartInfo(exe, QuoteArgs(args))
        {
            WorkingDirectory = _root,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8
        };
        psi.EnvironmentVariables["UV_PYTHON_INSTALL_DIR"] = Path.Combine(_root, ".runtime");
        psi.EnvironmentVariables["UV_CACHE_DIR"] = Path.Combine(_root, ".uv-cache");
        psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";

        var serializer = new JavaScriptSerializer();
        using (var proc = Process.Start(psi))
        {
            proc.ErrorDataReceived += delegate(object s, DataReceivedEventArgs ev)
            {
                if (!string.IsNullOrEmpty(ev.Data)) _log("[setup] " + ev.Data);
            };
            proc.BeginErrorReadLine();

            string line;
            while ((line = proc.StandardOutput.ReadLine()) != null)
            {
                _cts.Token.ThrowIfCancellationRequested();
                _pauseGate.Wait(_cts.Token);
                if (line.StartsWith("{"))
                {
                    try
                    {
                        var evt = serializer.Deserialize<Dictionary<string, object>>(line);
                        HandleJsonProgress(evt);
                        continue;
                    }
                    catch { }
                }
                _log("[setup] " + line);
            }
            proc.WaitForExit();
            if (proc.ExitCode != 0)
                throw new InvalidOperationException("Setup exited with code " + proc.ExitCode + ". See launcher log.");
        }

        // Mark remaining informational rows ready
        SafeUi(delegate
        {
            foreach (var row in _rows)
            {
                if (row.State == "Pending" || row.State == "Checking" || row.State == "Installing")
                    UpdateRow(row.Id, "Ready", 100, "");
            }
            UpdateOverallProgress();
        });
    }

    private void HandleJsonProgress(Dictionary<string, object> evt)
    {
        if (!evt.ContainsKey("id") || !evt.ContainsKey("state")) return;
        string id = Convert.ToString(evt["id"]);
        string state = Convert.ToString(evt["state"]);
        long? dl = evt.ContainsKey("downloaded") && evt["downloaded"] != null ? (long?)Convert.ToInt64(evt["downloaded"]) : null;
        long? tot = evt.ContainsKey("total") && evt["total"] != null ? (long?)Convert.ToInt64(evt["total"]) : null;
        double? speed = evt.ContainsKey("speed_bps") && evt["speed_bps"] != null ? (double?)Convert.ToDouble(evt["speed_bps"]) : null;
        double? eta = evt.ContainsKey("eta_seconds") && evt["eta_seconds"] != null ? (double?)Convert.ToDouble(evt["eta_seconds"]) : null;
        string detail = evt.ContainsKey("detail") && evt["detail"] != null ? Convert.ToString(evt["detail"]) : "";

        SafeUi(delegate
        {
            int pct = -1;
            if (state == "Ready" || state == "Reused existing") pct = 100;
            else if (dl.HasValue && tot.HasValue && tot.Value > 0) pct = (int)Math.Min(100, (dl.Value * 100) / tot.Value);

            string info = detail;
            if (dl.HasValue)
            {
                info = tot.HasValue && tot.Value > 0
                    ? string.Format("{0} / {1}", FormatBytes(dl.Value), FormatBytes(tot.Value))
                    : FormatBytes(dl.Value);
            }
            if (speed.HasValue && speed.Value > 0)
            {
                string etaTxt = eta.HasValue ? string.Format("{0:0}s", eta.Value) : "—";
                _metricsLabel.Text = string.Format("Speed: {0}/s  •  Downloaded: {1}  •  ETA: {2}",
                    FormatBytes((long)speed.Value), info, etaTxt);
            }
            SetActivity(string.Format("{0}: {1}", id, state));
            UpdateRow(id, state, pct, info);
            UpdateOverallProgress();
        });
    }

    private string EnsureUvWithProgress()
    {
        string tools = Path.Combine(_root, "tools");
        string uv = Path.Combine(tools, "uv.exe");
        SafeUi(delegate { UpdateRow("uv", "Checking", -1, ""); });
        if (File.Exists(uv))
        {
            SafeUi(delegate { UpdateRow("uv", "Reused existing", 100, "Verified"); UpdateOverallProgress(); });
            return uv;
        }
        Directory.CreateDirectory(tools);
        string toolchain = File.ReadAllText(Path.Combine(_root, "scripts", "toolchain.json"), Encoding.UTF8);
        var manifest = new JavaScriptSerializer().Deserialize<Dictionary<string, Dictionary<string, string>>>(toolchain);
        var release = manifest["uv"];
        string archive = Path.Combine(tools, "uv-bootstrap.zip");
        DownloadResumable("uv", release["url"], archive, release["sha256"]);
        SafeUi(delegate { UpdateRow("uv", "Extracting", -1, "Extracting uv.exe"); });
        using (var zip = ZipFile.OpenRead(archive))
        {
            foreach (var entry in zip.Entries)
            {
                if (entry.Name != "uv.exe") continue;
                string tmp = uv + ".tmp";
                entry.ExtractToFile(tmp, true);
                if (File.Exists(uv)) File.Delete(uv);
                File.Move(tmp, uv);
                break;
            }
        }
        File.Delete(archive);
        SafeUi(delegate { UpdateRow("uv", "Ready", 100, "Ready"); UpdateOverallProgress(); });
        return uv;
    }

    private void DownloadResumable(string compId, string url, string target, string expectedSha256)
    {
        string part = target + ".part";
        ServicePointManager.SecurityProtocol |= SecurityProtocolType.Tls12;
        for (int attempt = 0; attempt < 3; attempt++)
        {
            _cts.Token.ThrowIfCancellationRequested();
            long existing = File.Exists(part) ? new FileInfo(part).Length : 0;
            try
            {
                var req = (HttpWebRequest)WebRequest.Create(url);
                req.Timeout = 30000;
                req.UserAgent = "AutoClipStudio-Setup/1.0";
                if (existing > 0) req.AddRange(existing);

                using (var resp = (HttpWebResponse)req.GetResponse())
                {
                    bool resumed = existing > 0 && resp.StatusCode == HttpStatusCode.PartialContent;
                    if (!resumed) existing = 0;
                    long? total = resp.ContentLength > 0 ? (long?)(existing + resp.ContentLength) : null;
                    if (total.HasValue)
                    {
                        long free = new DriveInfo(Path.GetPathRoot(target)).AvailableFreeSpace;
                        if (free < total.Value - existing)
                            throw new IOException("Insufficient disk space for " + Path.GetFileName(target));
                    }

                    long downloaded = existing;
                    var sw = Stopwatch.StartNew();
                    long lastTick = 0;
                    using (var inStream = resp.GetResponseStream())
                    using (var outStream = new FileStream(part, resumed ? FileMode.Append : FileMode.Create, FileAccess.Write))
                    {
                        byte[] buf = new byte[65536];
                        int read;
                        while ((read = inStream.Read(buf, 0, buf.Length)) > 0)
                        {
                            _cts.Token.ThrowIfCancellationRequested();
                            _pauseGate.Wait(_cts.Token);
                            outStream.Write(buf, 0, read);
                            downloaded += read;
                            if (sw.ElapsedMilliseconds - lastTick >= 120)
                            {
                                double speed = (downloaded - existing) / Math.Max(0.001, sw.Elapsed.TotalSeconds);
                                double? eta = total.HasValue && speed > 0 ? (double?)((total.Value - downloaded) / speed) : null;
                                long dCopy = downloaded;
                                long? tCopy = total;
                                SafeUi(delegate
                                {
                                    int pct = tCopy.HasValue && tCopy.Value > 0 ? (int)Math.Min(100, dCopy * 100 / tCopy.Value) : -1;
                                    string sizeTxt = tCopy.HasValue ? FormatBytes(dCopy) + " / " + FormatBytes(tCopy.Value) : FormatBytes(dCopy);
                                    _metricsLabel.Text = string.Format("Speed: {0}/s  •  Downloaded: {1}  •  ETA: {2}",
                                        FormatBytes((long)speed), sizeTxt, eta.HasValue ? string.Format("{0:0}s", eta.Value) : "—");
                                    UpdateRow(compId, "Downloading", pct, sizeTxt);
                                });
                                lastTick = sw.ElapsedMilliseconds;
                            }
                        }
                    }
                }
                if (!string.IsNullOrEmpty(expectedSha256))
                {
                    SafeUi(delegate { UpdateRow(compId, "Verifying", 100, "SHA-256"); });
                    using (var fs = File.OpenRead(part))
                    {
                        string actual = Sha256Hex(fs);
                        if (!string.Equals(actual, expectedSha256, StringComparison.OrdinalIgnoreCase))
                        {
                            File.Delete(part);
                            throw new IOException("SHA-256 verification failed for " + Path.GetFileName(target));
                        }
                    }
                }
                if (File.Exists(target)) File.Delete(target);
                File.Move(part, target);
                return;
            }
            catch (OperationCanceledException) { throw; }
            catch (Exception)
            {
                if (attempt >= 2) throw;
                Thread.Sleep((1 << attempt) * 1000);
            }
        }
    }

    private void UpdateRow(string id, string state, int percent, string detail)
    {
        ComponentRow row;
        if (!_rowMap.TryGetValue(id, out row)) return;
        row.State = state;
        row.StateLabel.Text = state;
        row.StateLabel.ForeColor = state == "Failed" ? RedPrimary
            : (state == "Ready" || state == "Reused existing") ? GreenOk
            : (state == "Downloading" || state == "Installing" || state == "Extracting" || state == "Verifying") ? AmberWarn
            : TextMuted;
        if (!string.IsNullOrEmpty(detail)) row.DetailLabel.Text = detail;
        if (percent < 0)
        {
            row.Progress.Style = ProgressBarStyle.Marquee;
        }
        else
        {
            row.Progress.Style = ProgressBarStyle.Continuous;
            row.Progress.Value = Math.Max(0, Math.Min(100, percent));
        }
    }

    private void UpdateOverallProgress()
    {
        if (_rows.Count == 0) return;
        int done = 0;
        foreach (var row in _rows)
        {
            if (row.State == "Ready" || row.State == "Reused existing") done++;
        }
        _completedLabel.Text = string.Format("{0} / {1} components ready", done, _rows.Count);
        _overallBar.Value = Math.Min(100, (done * 100) / _rows.Count);
    }

    private void SetActivity(string text) { _activityLabel.Text = text; }

    private void SafeUi(Action action)
    {
        if (IsDisposed) return;
        try { BeginInvoke(action); }
        catch (InvalidOperationException) { }
    }

    private bool CheckPythonVersion()
    {
        try
        {
            return LauncherRunPython("-c", "import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] < (3,13) else 1)") == 0;
        }
        catch { return false; }
    }

    private int LauncherRunPython(params string[] args)
    {
        using (var p = _job.Start(_python, args, _root, null))
        {
            p.Wait(uint.MaxValue);
            return p.ExitCode();
        }
    }

    private static string DetectNvidiaGpu()
    {
        try
        {
            var psi = new ProcessStartInfo("nvidia-smi", "--query-gpu=name,memory.total --format=csv,noheader")
            {
                UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true
            };
            using (var p = Process.Start(psi))
            {
                string outStr = p.StandardOutput.ReadLine();
                p.WaitForExit(3000);
                return string.IsNullOrWhiteSpace(outStr) ? null : outStr.Trim();
            }
        }
        catch { return null; }
    }

    private static string QuoteArgs(string[] args)
    {
        var sb = new StringBuilder();
        foreach (string arg in args)
        {
            if (sb.Length > 0) sb.Append(' ');
            sb.Append('"').Append(arg.Replace("\"", "\\\"")).Append('"');
        }
        return sb.ToString();
    }

    private static string Sha256Hex(Stream stream)
    {
        using (var sha = SHA256.Create())
            return BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
    }

    private static string FormatBytes(long bytes)
    {
        double val = Math.Max(0, bytes);
        string[] units = { "B", "KB", "MB", "GB" };
        int idx = 0;
        while (val >= 1024 && idx < units.Length - 1) { val /= 1024; idx++; }
        return idx < 2 ? string.Format("{0:0} {1}", val, units[idx]) : string.Format("{0:0.0} {1}", val, units[idx]);
    }
}
