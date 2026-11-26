using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Text;
using System.IO;
using System.IO.Compression;
using System.Net;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Win32;

internal sealed class SetupWizardForm : Form
{
    private struct Palette
    {
        public bool IsDark;
        public Color Canvas;
        public Color Card;
        public Color Elevated;
        public Color Border;
        public Color TextPrimary;
        public Color TextMuted;
        public Color RedPrimary;
        public Color RedAccent;
        public Color GreenOk;
        public Color AmberWarn;
        public Color RoseError;
        public Color LogBg;
    }

    private sealed class StageRow
    {
        public string Id;
        public int Index;
        public string Name;
        public string State = "Pending";
        public Panel Container;
        public Label StepBadge;
        public Label NameLabel;
        public Label StateLabel;
    }

    private sealed class ComponentRow
    {
        public string Id;
        public string Name;
        public string StageId;
        public bool Mandatory;
        public long EstimatedBytes;
        public long? DownloadBytes;
        public string State = "Pending";
        public int Percent;
        public bool Indeterminate;
        public Panel Card;
        public Label TitleLabel;
        public Label StateLabel;
        public Label DetailLabel;
        public Panel BarPanel;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Auto)]
    private struct MEMORYSTATUSEX
    {
        public uint dwLength;
        public uint dwMemoryLoad;
        public ulong ullTotalPhys;
        public ulong ullAvailPhys;
        public ulong ullTotalPageFile;
        public ulong ullAvailPageFile;
        public ulong ullTotalVirtual;
        public ulong ullAvailVirtual;
        public ulong ullAvailExtendedVirtual;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Auto, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GlobalMemoryStatusEx(ref MEMORYSTATUSEX lpBuffer);

    private static readonly Regex SecretRegex = new Regex(
        @"(?i)(?:AIza[\w-]{35}|sk-[A-Za-z0-9_-]{20,}|((?:authorization|cookie|api[-_ ]?key|access_token|refresh_token|password)\s*[:=]\s*)[^\s,;""'<>]+)",
        RegexOptions.Compiled);

    private readonly string _root;
    private readonly string _python;
    private readonly string _setupScript;
    private readonly WindowsJob _job;
    private readonly EventWaitHandle _stop;
    private readonly Action<string> _log;
    private readonly string _setupLogPath;
    private readonly Palette _pal;
    private readonly PrivateFontCollection _fonts = new PrivateFontCollection();
    private FontFamily _fontFamily;

    private readonly List<StageRow> _stages = new List<StageRow>();
    private readonly Dictionary<string, StageRow> _stageMap = new Dictionary<string, StageRow>();
    private readonly List<ComponentRow> _rows = new List<ComponentRow>();
    private readonly Dictionary<string, ComponentRow> _rowMap = new Dictionary<string, ComponentRow>();
    private readonly StringBuilder _logBuffer = new StringBuilder();
    private readonly object _procGate = new object();

    private CancellationTokenSource _cts = new CancellationTokenSource();
    private Process _activeProc;
    private bool _running;
    private bool _completedOk;
    private bool _retryRepairOnly;
    private bool _logsExpanded;
    private int _overallPercent;
    private string _overallMode = "Estimated";
    private int _currentStageIndex = 1;
    private int _totalStages = 8;

    private Label _stepSubtitleLabel;
    private Label _sysInfoLabel;
    private Label _spaceInfoLabel;
    private Label _activityLabel;
    private Label _overallPctLabel;
    private Label _metricsLabel;
    private Panel _overallBar;
    private CheckBox _cloudOnlyCheck;
    private FlowLayoutPanel _stageListPanel;
    private FlowLayoutPanel _compListPanel;
    private Panel _rightContainer;
    private Panel _summaryPanel;
    private Label _summaryBodyLabel;
    private Panel _logsDrawer;
    private TextBox _logTextBox;
    private Button _logsToggleBtn;
    private Button _copyLogsBtn;
    private Button _openLogBtn;
    private Button _retryBtn;
    private Button _cancelBtn;
    private Button _launchBtn;

    public SetupWizardForm(string root, string python, string setupScript, WindowsJob job, EventWaitHandle stop, Action<string> log)
    {
        _root = root;
        _python = python;
        _setupScript = setupScript;
        _job = job;
        _stop = stop;
        _log = log;
        _setupLogPath = ResolveSetupLogPath();
        _pal = DetectPalette();
        LoadFonts();

        Text = "AutoClip Studio Setup";
        Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
        BackColor = _pal.Canvas;
        ForeColor = _pal.TextPrimary;
        Font = MakeFont(9.5f, FontStyle.Regular);
        MinimumSize = new Size(800, 620);
        ClientSize = new Size(920, 690);
        StartPosition = FormStartPosition.CenterScreen;
        AutoScaleMode = AutoScaleMode.Dpi;
        DoubleBuffered = true;

        BuildLayout();
        LoadStaticSystemProbe();
        PopulateDefaultPlan();

        var stopTimer = new System.Windows.Forms.Timer { Interval = 250 };
        stopTimer.Tick += delegate
        {
            if (_stop.WaitOne(0))
            {
                CancelActiveWork();
                DialogResult = DialogResult.Cancel;
            }
        };
        stopTimer.Start();

        Shown += delegate { BeginInstall(false); };
        FormClosing += delegate
        {
            CancelActiveWork();
            stopTimer.Stop();
        };
    }

    private static Palette DetectPalette()
    {
        bool dark = true;
        try
        {
            using (var key = Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"))
            {
                if (key != null)
                {
                    object val = key.GetValue("AppsUseLightTheme");
                    if (val is int && (int)val == 1) dark = false;
                }
            }
        }
        catch { }

        if (dark)
        {
            return new Palette
            {
                IsDark = true,
                Canvas = Color.FromArgb(16, 17, 22),
                Card = Color.FromArgb(26, 28, 35),
                Elevated = Color.FromArgb(35, 38, 49),
                Border = Color.FromArgb(43, 46, 56),
                TextPrimary = Color.FromArgb(245, 245, 247),
                TextMuted = Color.FromArgb(163, 168, 181),
                RedPrimary = Color.FromArgb(229, 57, 53),
                RedAccent = Color.FromArgb(255, 90, 79),
                GreenOk = Color.FromArgb(46, 204, 113),
                AmberWarn = Color.FromArgb(245, 166, 35),
                RoseError = Color.FromArgb(255, 107, 122),
                LogBg = Color.FromArgb(14, 15, 20)
            };
        }
        return new Palette
        {
            IsDark = false,
            Canvas = Color.FromArgb(245, 246, 248),
            Card = Color.FromArgb(255, 255, 255),
            Elevated = Color.FromArgb(236, 237, 242),
            Border = Color.FromArgb(218, 221, 228),
            TextPrimary = Color.FromArgb(24, 26, 32),
            TextMuted = Color.FromArgb(92, 98, 112),
            RedPrimary = Color.FromArgb(229, 57, 53),
            RedAccent = Color.FromArgb(211, 47, 47),
            GreenOk = Color.FromArgb(32, 168, 92),
            AmberWarn = Color.FromArgb(214, 134, 10),
            RoseError = Color.FromArgb(215, 58, 73),
            LogBg = Color.FromArgb(248, 249, 251)
        };
    }

    private void LoadFonts()
    {
        try
        {
            string fontsDir = Path.Combine(_root, "autoclip_studio", "assets", "fonts", "poppins");
            if (Directory.Exists(fontsDir))
            {
                foreach (string ttf in Directory.GetFiles(fontsDir, "*.ttf"))
                    _fonts.AddFontFile(ttf);
                if (_fonts.Families.Length > 0)
                    _fontFamily = _fonts.Families[0];
            }
        }
        catch { }
    }

    private Font MakeFont(float size, FontStyle style)
    {
        if (_fontFamily != null && _fontFamily.IsStyleAvailable(style))
            return new Font(_fontFamily, size, style);
        return new Font("Segoe UI", size, style);
    }

    private void BuildLayout()
    {
        var rootLayout = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            Padding = new Padding(20, 16, 20, 16),
            ColumnCount = 1,
            RowCount = 6,
            BackColor = _pal.Canvas
        };
        rootLayout.RowStyles.Add(new RowStyle(SizeType.Absolute, 56));  // 0: Header
        rootLayout.RowStyles.Add(new RowStyle(SizeType.Absolute, 90));  // 1: System card
        rootLayout.RowStyles.Add(new RowStyle(SizeType.Absolute, 96));  // 2: Overall progress
        rootLayout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));  // 3: Stages + Checklist
        rootLayout.RowStyles.Add(new RowStyle(SizeType.Absolute, 0));   // 4: Expandable logs
        rootLayout.RowStyles.Add(new RowStyle(SizeType.Absolute, 48));  // 5: Footer buttons

        // 0: Header
        var header = new Panel { Dock = DockStyle.Fill, BackColor = _pal.Canvas };
        string logoPath = ResolveLogoPath();
        int textLeft = 0;
        if (logoPath != null)
        {
            try
            {
                var pic = new PictureBox
                {
                    Left = 0, Top = 4, Width = 136, Height = 44,
                    SizeMode = PictureBoxSizeMode.Zoom, BackColor = _pal.Canvas,
                    Image = Image.FromFile(logoPath)
                };
                header.Controls.Add(pic);
                textLeft = 148;
            }
            catch { }
        }
        var titleLbl = new Label
        {
            Left = textLeft, Top = 4, Width = 480, Height = 26, UseMnemonic = false,
            Text = "AutoClip Studio Setup",
            Font = MakeFont(14f, FontStyle.Bold), ForeColor = _pal.TextPrimary
        };
        _stepSubtitleLabel = new Label
        {
            Left = textLeft, Top = 30, Width = 520, Height = 20, UseMnemonic = false,
            Text = "Step 1 of 8  •  Initializing bootstrap environment",
            Font = MakeFont(9.5f, FontStyle.Regular), ForeColor = _pal.TextMuted
        };
        var archPill = new Label
        {
            Text = Environment.Is64BitOperatingSystem ? "Windows x86-64" : "Windows x86",
            Width = 128, Height = 26, Top = 12, UseMnemonic = false,
            Anchor = AnchorStyles.Top | AnchorStyles.Right,
            TextAlign = ContentAlignment.MiddleCenter,
            Font = MakeFont(8.5f, FontStyle.Bold),
            BackColor = _pal.Card, ForeColor = _pal.TextMuted
        };
        archPill.Paint += DrawRoundedBorder;
        header.Resize += delegate { archPill.Left = header.Width - archPill.Width; };
        header.Controls.AddRange(new Control[] { titleLbl, _stepSubtitleLabel, archPill });
        rootLayout.Controls.Add(header, 0, 0);

        // 1: System Compatibility Card
        var sysCard = MakeCardPanel(8);
        _sysInfoLabel = new Label
        {
            Left = 14, Top = 10, Width = 840, Height = 20, UseMnemonic = false,
            Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right,
            Font = MakeFont(9.5f, FontStyle.Bold), ForeColor = _pal.TextPrimary
        };
        _spaceInfoLabel = new Label
        {
            Left = 14, Top = 32, Width = 840, Height = 19, UseMnemonic = false,
            Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right,
            Font = MakeFont(8.8f, FontStyle.Regular), ForeColor = _pal.TextMuted
        };
        _cloudOnlyCheck = new CheckBox
        {
            Left = 14, Top = 57, Width = 20, Height = 20,
            Checked = false, BackColor = _pal.Card
        };
        var cloudOnlyText = new Label
        {
            Left = 36, Top = 57, Width = 660, Height = 20, UseMnemonic = false,
            ForeColor = _pal.TextPrimary, BackColor = _pal.Card,
            Font = MakeFont(9f, FontStyle.Regular), Cursor = Cursors.Hand,
            Text = "Cloud-only transcription (skip local Whisper Large v3 Turbo model downloads)"
        };
        cloudOnlyText.Click += delegate { if (_cloudOnlyCheck.Enabled) _cloudOnlyCheck.Checked = !_cloudOnlyCheck.Checked; };
        sysCard.Controls.AddRange(new Control[] { _sysInfoLabel, _spaceInfoLabel, _cloudOnlyCheck, cloudOnlyText });
        rootLayout.Controls.Add(sysCard, 0, 1);

        // 2: Overall Progress Card
        var progCard = MakeCardPanel(8);
        _overallPctLabel = new Label
        {
            Width = 210, Height = 22, Top = 10, Left = progCard.ClientSize.Width - 224,
            Anchor = AnchorStyles.Top | AnchorStyles.Right,
            UseMnemonic = false, Text = "0%  •  Estimated",
            TextAlign = ContentAlignment.MiddleRight,
            Font = MakeFont(9f, FontStyle.Bold), ForeColor = _pal.RedPrimary
        };
        _activityLabel = new Label
        {
            Left = 14, Top = 10, Width = 600, Height = 22, UseMnemonic = false,
            Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right,
            Text = "Preparing system validation...",
            Font = MakeFont(10f, FontStyle.Bold), ForeColor = _pal.TextPrimary
        };
        progCard.Layout += delegate
        {
            _overallPctLabel.Left = Math.Max(220, progCard.ClientSize.Width - _overallPctLabel.Width - 14);
            _activityLabel.Width = Math.Max(200, _overallPctLabel.Left - 22);
        };
        _overallBar = new Panel
        {
            Left = 14, Top = 38, Width = progCard.Width - 28, Height = 12,
            Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right,
            BackColor = _pal.Elevated
        };
        _overallBar.Paint += PaintOverallProgressBar;
        _metricsLabel = new Label
        {
            Left = 14, Top = 58, Width = 840, Height = 20, UseMnemonic = false,
            Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right,
            Text = "Stage telemetry initializing...",
            Font = MakeFont(8.8f, FontStyle.Regular), ForeColor = _pal.TextMuted
        };
        progCard.Controls.AddRange(new Control[] { _activityLabel, _overallPctLabel, _overallBar, _metricsLabel });
        rootLayout.Controls.Add(progCard, 0, 2);

        // 3: Two-Pane Body (Left: 8-stage stepper, Right: checklist / summary)
        var bodySplit = new TableLayoutPanel
        {
            Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 1,
            Margin = new Padding(0, 8, 0, 8), BackColor = _pal.Canvas
        };
        bodySplit.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 250));
        bodySplit.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));

        var stageCard = MakeCardPanel(0);
        stageCard.Margin = new Padding(0, 0, 10, 0);
        var stageTitle = new Label
        {
            Dock = DockStyle.Top, Height = 28, Text = "Setup Stages",
            Padding = new Padding(12, 8, 12, 0),
            Font = MakeFont(9.5f, FontStyle.Bold), ForeColor = _pal.TextPrimary
        };
        _stageListPanel = new FlowLayoutPanel
        {
            Dock = DockStyle.Fill, FlowDirection = FlowDirection.TopDown,
            WrapContents = false, AutoScroll = true, Padding = new Padding(8, 6, 8, 8),
            BackColor = _pal.Card
        };
        stageCard.Controls.Add(_stageListPanel);
        stageCard.Controls.Add(stageTitle);
        bodySplit.Controls.Add(stageCard, 0, 0);

        _rightContainer = new Panel { Dock = DockStyle.Fill, BackColor = _pal.Canvas };
        _compListPanel = new FlowLayoutPanel
        {
            Dock = DockStyle.Fill, FlowDirection = FlowDirection.TopDown,
            WrapContents = false, AutoScroll = true, Padding = new Padding(0, 0, 4, 0),
            BackColor = _pal.Canvas
        };
        _compListPanel.Resize += delegate
        {
            int w = Math.Max(320, _compListPanel.ClientSize.Width - 22);
            foreach (var r in _rows)
                if (r.Card != null) r.Card.Width = w;
        };

        _summaryPanel = MakeCardPanel(0);
        _summaryPanel.Visible = false;
        _summaryBodyLabel = new Label
        {
            Dock = DockStyle.Fill, Padding = new Padding(18),
            Font = MakeFont(10f, FontStyle.Regular), ForeColor = _pal.TextPrimary
        };
        _summaryPanel.Controls.Add(_summaryBodyLabel);
        _rightContainer.Controls.Add(_compListPanel);
        _rightContainer.Controls.Add(_summaryPanel);
        bodySplit.Controls.Add(_rightContainer, 1, 0);
        rootLayout.Controls.Add(bodySplit, 0, 3);

        // 4: Collapsible Logs Drawer
        _logsDrawer = MakeCardPanel(0);
        _logsDrawer.Visible = false;
        _logTextBox = new TextBox
        {
            Dock = DockStyle.Fill, Multiline = true, ReadOnly = true,
            ScrollBars = ScrollBars.Vertical, BorderStyle = BorderStyle.None,
            BackColor = _pal.LogBg, ForeColor = _pal.TextPrimary,
            Font = new Font("Consolas", 8.5f)
        };
        _logsDrawer.Controls.Add(_logTextBox);
        rootLayout.Controls.Add(_logsDrawer, 0, 4);

        // 5: Footer Buttons
        var footer = new Panel { Dock = DockStyle.Fill, BackColor = _pal.Canvas, Padding = new Padding(0, 8, 0, 0) };
        _logsToggleBtn = MakeButton("Show Logs", 0, 8, 108, false);
        _logsToggleBtn.Click += delegate { ToggleLogsDrawer(rootLayout); };
        _copyLogsBtn = MakeButton("Copy Diagnostics", 116, 8, 134, false);
        _copyLogsBtn.Click += CopyLogs;
        _openLogBtn = MakeButton("Open Log", 258, 8, 96, false);
        _openLogBtn.Click += OpenSetupLog;

        _launchBtn = MakeButton("Launch Application", 710, 8, 166, true);
        _launchBtn.Enabled = false;
        _launchBtn.Anchor = AnchorStyles.Top | AnchorStyles.Right;
        _launchBtn.Click += delegate { DialogResult = DialogResult.OK; };

        _cancelBtn = MakeButton("Cancel Setup", 594, 8, 108, false);
        _cancelBtn.Anchor = AnchorStyles.Top | AnchorStyles.Right;
        _cancelBtn.Click += OnCancelClicked;

        _retryBtn = MakeButton("Retry Failed", 478, 8, 108, false);
        _retryBtn.Anchor = AnchorStyles.Top | AnchorStyles.Right;
        _retryBtn.Enabled = false;
        _retryBtn.Click += delegate { BeginInstall(true); };

        footer.Resize += delegate
        {
            _launchBtn.Left = footer.Width - _launchBtn.Width;
            _cancelBtn.Left = _launchBtn.Left - _cancelBtn.Width - 8;
            _retryBtn.Left = _cancelBtn.Left - _retryBtn.Width - 8;
        };
        footer.Controls.AddRange(new Control[] { _logsToggleBtn, _copyLogsBtn, _openLogBtn, _retryBtn, _cancelBtn, _launchBtn });
        rootLayout.Controls.Add(footer, 0, 5);
        Controls.Add(rootLayout);
    }

    private Panel MakeCardPanel(int bottomMargin)
    {
        var panel = new Panel
        {
            Dock = DockStyle.Fill, BackColor = _pal.Card,
            Margin = new Padding(0, 6, 0, bottomMargin)
        };
        panel.Paint += DrawRoundedBorder;
        return panel;
    }

    private Button MakeButton(string text, int left, int top, int width, bool primary)
    {
        var btn = new Button
        {
            Left = left, Top = top, Width = width, Height = 34, Text = text,
            FlatStyle = FlatStyle.Flat, BackColor = primary ? _pal.RedPrimary : _pal.Elevated,
            ForeColor = primary ? Color.White : _pal.TextPrimary,
            Font = MakeFont(9.2f, primary ? FontStyle.Bold : FontStyle.Regular),
            Cursor = Cursors.Hand
        };
        btn.FlatAppearance.BorderColor = primary ? _pal.RedPrimary : _pal.Border;
        btn.FlatAppearance.BorderSize = 1;
        btn.EnabledChanged += delegate
        {
            if (primary)
            {
                btn.BackColor = btn.Enabled ? _pal.RedPrimary : _pal.Elevated;
                btn.ForeColor = btn.Enabled ? Color.White : _pal.TextMuted;
                btn.FlatAppearance.BorderColor = btn.Enabled ? _pal.RedPrimary : _pal.Border;
            }
            else
            {
                btn.ForeColor = btn.Enabled ? _pal.TextPrimary : _pal.TextMuted;
            }
        };
        return btn;
    }

    private void DrawRoundedBorder(object sender, PaintEventArgs e)
    {
        var c = (Control)sender;
        e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
        using (var path = RoundedRect(new Rectangle(0, 0, c.Width - 1, c.Height - 1), 10))
        using (var pen = new Pen(_pal.Border, 1))
            e.Graphics.DrawPath(pen, path);
    }

    private void PaintOverallProgressBar(object sender, PaintEventArgs e)
    {
        var bar = (Control)sender;
        e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
        using (var bg = new SolidBrush(_pal.Elevated))
        using (var bgPath = RoundedRect(new Rectangle(0, 0, bar.Width, bar.Height), 6))
            e.Graphics.FillPath(bg, bgPath);
        int fillW = (int)Math.Round(bar.Width * Math.Max(0, Math.Min(100, _overallPercent)) / 100.0);
        if (fillW > 2)
        {
            using (var fg = new SolidBrush(_completedOk ? _pal.GreenOk : _pal.RedPrimary))
            using (var fgPath = RoundedRect(new Rectangle(0, 0, fillW, bar.Height), 6))
                e.Graphics.FillPath(fg, fgPath);
        }
    }

    private static GraphicsPath RoundedRect(Rectangle bounds, int radius)
    {
        int d = radius * 2;
        var path = new GraphicsPath();
        path.AddArc(bounds.X, bounds.Y, d, d, 180, 90);
        path.AddArc(bounds.Right - d, bounds.Y, d, d, 270, 90);
        path.AddArc(bounds.Right - d, bounds.Bottom - d, d, d, 0, 90);
        path.AddArc(bounds.X, bounds.Bottom - d, d, d, 90, 90);
        path.CloseFigure();
        return path;
    }

    private void ToggleLogsDrawer(TableLayoutPanel rootLayout)
    {
        _logsExpanded = !_logsExpanded;
        _logsDrawer.Visible = _logsExpanded;
        rootLayout.RowStyles[4].Height = _logsExpanded ? 150 : 0;
        _logsToggleBtn.Text = _logsExpanded ? "Hide Logs" : "Show Logs";
    }

    private void CopyLogs(object sender, EventArgs e)
    {
        try
        {
            string txt = _logBuffer.Length > 0 ? _logBuffer.ToString() : "No setup diagnostics recorded yet.";
            Clipboard.SetText(txt);
        }
        catch { }
    }

    private void OpenSetupLog(object sender, EventArgs e)
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(_setupLogPath));
            if (!File.Exists(_setupLogPath))
                File.WriteAllText(_setupLogPath, _logBuffer.ToString(), Encoding.UTF8);
            Process.Start(new ProcessStartInfo(_setupLogPath) { UseShellExecute = true });
        }
        catch { }
    }

    private void LoadStaticSystemProbe()
    {
        string osDesc = Environment.Is64BitOperatingSystem ? "Windows x86-64" : "Windows x86";
        string cpuName = ReadCpuName();
        long ramBytes = ReadTotalRamBytes();
        string gpu = DetectNvidiaGpu();
        string hwSummary = gpu != null ? gpu + " (NVENC / CUDA)" : "CPU fallback (no NVIDIA GPU detected)";
        _sysInfoLabel.Text = string.Format("{0}  •  {1} ({2} threads)  •  RAM: {3}", osDesc, cpuName, Environment.ProcessorCount, FormatBytes(ramBytes));

        long freeBytes = new DriveInfo(Path.GetPathRoot(_root)).AvailableFreeSpace;
        _spaceInfoLabel.Text = string.Format("Acceleration: {0}  •  Available Disk: {1}", hwSummary, FormatBytes(freeBytes));
    }

    private void PopulateDefaultPlan()
    {
        string[] stageIds = { "system_compatibility", "python_runtime", "virtual_env", "core_packages", "media_tools", "hardware_accel", "transcription_models", "final_validation" };
        string[] stageNames = { "System Compatibility", "Python Runtime", "Virtual Environment", "Core Dependencies", "Media Tools", "Hardware Accel", "Transcription & Models", "Final Validation" };
        for (int i = 0; i < stageIds.Length; i++)
            AddStageItem(stageIds[i], i + 1, stageNames[i]);

        string manifestPath = Path.Combine(_root, "scripts", "dependencies.json");
        if (!File.Exists(manifestPath)) return;
        try
        {
            var serializer = new JavaScriptSerializer();
            var doc = serializer.Deserialize<Dictionary<string, object>>(File.ReadAllText(manifestPath, Encoding.UTF8));
            var comps = doc.ContainsKey("components") ? doc["components"] as System.Collections.ArrayList : null;
            if (comps == null) return;
            foreach (Dictionary<string, object> item in comps)
            {
                if (item.ContainsKey("platforms"))
                {
                    var plats = item["platforms"] as System.Collections.ArrayList;
                    if (plats != null && !plats.Contains("windows")) continue;
                }
                string id = Convert.ToString(item["id"]);
                string name = Convert.ToString(item["name"]);
                string stageId = item.ContainsKey("stage_id") ? Convert.ToString(item["stage_id"]) : "";
                bool mandatory = item.ContainsKey("mandatory") && Convert.ToBoolean(item["mandatory"]);
                long est = item.ContainsKey("estimated_installed_bytes") && item["estimated_installed_bytes"] != null ? Convert.ToInt64(item["estimated_installed_bytes"]) : 0;
                long? dl = item.ContainsKey("download_size_bytes") && item["download_size_bytes"] != null ? (long?)Convert.ToInt64(item["download_size_bytes"]) : null;
                AddComponentCard(id, name, stageId, mandatory, dl, est);
            }
        }
        catch { }
    }

    private void AddStageItem(string id, int index, string name)
    {
        var panel = new Panel { Width = 224, Height = 36, BackColor = _pal.Card, Margin = new Padding(0, 0, 0, 4) };
        var badge = new Label
        {
            Left = 4, Top = 7, Width = 22, Height = 22, Text = index.ToString(),
            TextAlign = ContentAlignment.MiddleCenter, BackColor = _pal.Elevated, ForeColor = _pal.TextMuted,
            Font = MakeFont(8.5f, FontStyle.Bold)
        };
        var nameLbl = new Label
        {
            Left = 32, Top = 2, Width = 188, Height = 17, Text = name, UseMnemonic = false,
            Font = MakeFont(8.8f, FontStyle.Bold), ForeColor = _pal.TextPrimary
        };
        var stateLbl = new Label
        {
            Left = 32, Top = 19, Width = 188, Height = 14, Text = "Pending", UseMnemonic = false,
            Font = MakeFont(7.8f, FontStyle.Regular), ForeColor = _pal.TextMuted
        };
        panel.Controls.AddRange(new Control[] { badge, nameLbl, stateLbl });
        _stageListPanel.Controls.Add(panel);
        var st = new StageRow { Id = id, Index = index, Name = name, Container = panel, StepBadge = badge, NameLabel = nameLbl, StateLabel = stateLbl };
        _stages.Add(st);
        _stageMap[id] = st;
    }

    private void AddComponentCard(string id, string name, string stageId, bool mandatory, long? dlBytes, long estBytes)
    {
        int cardWidth = Math.Max(460, _compListPanel.ClientSize.Width - 24);
        var card = new Panel { Width = cardWidth, Height = 56, BackColor = _pal.Card, Margin = new Padding(0, 0, 0, 6) };
        card.Paint += DrawRoundedBorder;
        var title = new Label
        {
            Left = 12, Top = 7, Width = cardWidth - 236, Height = 19, UseMnemonic = false,
            Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right,
            Text = name + (mandatory ? "  •  Required" : "  •  Optional"),
            Font = MakeFont(9f, FontStyle.Bold), ForeColor = _pal.TextPrimary
        };
        var state = new Label
        {
            Left = cardWidth - 224, Top = 7, Width = 212, Height = 19, UseMnemonic = false,
            Anchor = AnchorStyles.Top | AnchorStyles.Right,
            Text = "Pending", TextAlign = ContentAlignment.MiddleRight,
            Font = MakeFont(8.5f, FontStyle.Bold), ForeColor = _pal.TextMuted
        };
        var bar = new Panel
        {
            Left = 12, Top = 33, Width = 220, Height = 8,
            BackColor = _pal.Elevated
        };
        var row = new ComponentRow
        {
            Id = id, Name = name, StageId = stageId, Mandatory = mandatory,
            DownloadBytes = dlBytes, EstimatedBytes = estBytes,
            Card = card, TitleLabel = title, StateLabel = state, BarPanel = bar
        };
        bar.Paint += delegate(object s, PaintEventArgs pe)
        {
            pe.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            using (var bg = new SolidBrush(_pal.Elevated))
            using (var bgP = RoundedRect(new Rectangle(0, 0, bar.Width, bar.Height), 4))
                pe.Graphics.FillPath(bg, bgP);
            int pct = row.Indeterminate ? 40 : Math.Max(0, Math.Min(100, row.Percent));
            int w = (int)Math.Round(bar.Width * pct / 100.0);
            if (w > 2)
            {
                Color c = row.State == "Failed" ? _pal.RoseError : (row.State == "Ready" || row.State == "Reused Existing Installation") ? _pal.GreenOk : _pal.RedPrimary;
                using (var fg = new SolidBrush(c))
                using (var fgP = RoundedRect(new Rectangle(0, 0, w, bar.Height), 4))
                    pe.Graphics.FillPath(fg, fgP);
            }
        };
        var detail = new Label
        {
            Left = 244, Top = 30, Width = cardWidth - 256, Height = 18, UseMnemonic = false,
            Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right,
            Text = dlBytes.HasValue && dlBytes.Value > 0 ? FormatBytes(dlBytes.Value) : "Auto-detect",
            TextAlign = ContentAlignment.MiddleRight, Font = MakeFont(8.2f, FontStyle.Regular), ForeColor = _pal.TextMuted
        };
        row.DetailLabel = detail;
        card.Controls.AddRange(new Control[] { title, state, bar, detail });
        _compListPanel.Controls.Add(card);
        _rows.Add(row);
        _rowMap[id] = row;
    }

    private void BeginInstall(bool repairOnly)
    {
        if (_running) return;
        _running = true;
        _completedOk = false;
        _retryRepairOnly = repairOnly;
        _cts = new CancellationTokenSource();
        _retryBtn.Enabled = false;
        _launchBtn.Enabled = false;
        _cancelBtn.Enabled = true;
        _cloudOnlyCheck.Enabled = false;
        _summaryPanel.Visible = false;
        _compListPanel.Visible = true;
        if (!repairOnly)
        {
            _overallPercent = 0;
            _overallBar.Invalidate();
        }
        bool cloudOnly = _cloudOnlyCheck.Checked;

        ThreadPool.QueueUserWorkItem(delegate
        {
            try
            {
                RunInstallPipeline(cloudOnly, repairOnly);
            }
            catch (OperationCanceledException)
            {
                AppendLog("[setup] Setup cancelled by user.");
                SafeUi(delegate
                {
                    _activityLabel.Text = "Setup cancelled. Valid components and partial downloads preserved.";
                    _retryBtn.Enabled = true;
                    _cancelBtn.Enabled = false;
                    _cloudOnlyCheck.Enabled = true;
                });
            }
            catch (Exception error)
            {
                AppendLog("[error] " + error.Message);
                SafeUi(delegate
                {
                    _activityLabel.Text = "Setup failed: " + error.Message;
                    _retryBtn.Enabled = true;
                    _cancelBtn.Enabled = false;
                    _cloudOnlyCheck.Enabled = true;
                });
            }
            finally
            {
                _running = false;
            }
        });
    }

    private void OnCancelClicked(object sender, EventArgs e)
    {
        if (_running)
        {
            CancelActiveWork();
            _cancelBtn.Enabled = false;
            _activityLabel.Text = "Cancelling active setup step...";
        }
        else
        {
            DialogResult = DialogResult.Cancel;
        }
    }

    private void CancelActiveWork()
    {
        try { _cts.Cancel(); } catch { }
        lock (_procGate)
        {
            if (_activeProc != null)
            {
                try
                {
                    if (!_activeProc.HasExited)
                    {
                        KillProcessTree(_activeProc.Id);
                    }
                }
                catch { }
            }
        }
    }

    private static void KillProcessTree(int pid)
    {
        try
        {
            var tk = Process.Start(new ProcessStartInfo("taskkill", "/F /T /PID " + pid)
            {
                CreateNoWindow = true, UseShellExecute = false
            });
            if (tk != null) tk.WaitForExit(3000);
        }
        catch { }
    }

    private void RunInstallPipeline(bool cloudOnly, bool repairOnly)
    {
        UpdateStage("virtual_env", 3, "Running", "Preparing uv package bootstrapper", null);
        string uv = EnsureUvWithProgress();
        _cts.Token.ThrowIfCancellationRequested();

        string modeFlag = cloudOnly ? "--cloud-only" : (repairOnly ? "--repair-only" : "--cloud-only");
        // Note: large optional Whisper model download stays opt-in inside Dependency Manager when cloudOnly unchecked only triggers on explicit local install or setup step.
        var argsList = new List<string>();
        bool useExistingPy = File.Exists(_python) && CheckPythonVersion();
        string exe;
        if (useExistingPy)
        {
            exe = _python;
            argsList.Add(_setupScript);
        }
        else
        {
            exe = uv;
            argsList.AddRange(new[] { "run", "--no-project", "--managed-python", "--python", "3.12", "--", "python", _setupScript });
        }
        argsList.Add("--json-progress");
        argsList.Add("--no-tests");
        argsList.Add(cloudOnly ? "--cloud-only" : "--repair-only");

        var psi = new ProcessStartInfo(exe, QuoteArgs(argsList.ToArray()))
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
            lock (_procGate) { _activeProc = proc; }
            try
            {
                proc.ErrorDataReceived += delegate(object s, DataReceivedEventArgs ev)
                {
                    if (!string.IsNullOrEmpty(ev.Data)) AppendLog("[stderr] " + ev.Data);
                };
                proc.BeginErrorReadLine();

                string line;
                while ((line = proc.StandardOutput.ReadLine()) != null)
                {
                    _cts.Token.ThrowIfCancellationRequested();
                    if (line.StartsWith("{"))
                    {
                        try
                        {
                            var evt = serializer.Deserialize<Dictionary<string, object>>(line);
                            HandleJsonEvent(evt);
                            continue;
                        }
                        catch { }
                    }
                    AppendLog("[setup] " + line);
                }
                proc.WaitForExit();
                _cts.Token.ThrowIfCancellationRequested();
                if (proc.ExitCode != 0 && !_completedOk)
                    throw new InvalidOperationException("Setup exited with code " + proc.ExitCode + ". Review installation logs.");
            }
            finally
            {
                lock (_procGate) { _activeProc = null; }
            }
        }
    }

    private void HandleJsonEvent(Dictionary<string, object> evt)
    {
        if (!evt.ContainsKey("event")) return;
        string evType = Convert.ToString(evt["event"]);
        if (evType == "system_info")
        {
            SafeUi(delegate
            {
                string os = Str(evt, "os_detail");
                string cpu = Str(evt, "cpu");
                string gpu = Str(evt, "gpu");
                string accel = Str(evt, "hw_accel");
                long ram = Lng(evt, "ram_bytes") ?? 0;
                long free = Lng(evt, "disk_free_bytes") ?? 0;
                _sysInfoLabel.Text = string.Format("{0}  •  {1}  •  RAM: {2}", os, cpu, FormatBytes(ram));
                _spaceInfoLabel.Text = string.Format("GPU: {0} ({1})  •  Available Disk: {2}", gpu, accel, FormatBytes(free));
            });
            return;
        }
        if (evType == "log")
        {
            AppendLog(string.Format("[{0}] [{1}] {2}", Str(evt, "level"), Str(evt, "component"), Str(evt, "message")));
            return;
        }
        if (evType == "stage")
        {
            string sid = Str(evt, "stage_id");
            int sidx = (int)(Lng(evt, "stage_index") ?? _currentStageIndex);
            string state = Str(evt, "state");
            string detail = Str(evt, "detail");
            long? pct = Lng(evt, "overall_percent");
            string mode = Str(evt, "overall_mode");
            UpdateStage(sid, sidx, state, detail, pct, mode);
            return;
        }
        if (evType == "progress")
        {
            string id = Str(evt, "id");
            string state = Str(evt, "state");
            string ver = Str(evt, "version");
            string detail = Str(evt, "detail");
            long? dl = Lng(evt, "downloaded");
            long? tot = Lng(evt, "total");
            double? speed = Dbl(evt, "speed_bps");
            double? eta = Dbl(evt, "eta_seconds");
            double? cpct = Dbl(evt, "component_percent");
            double? opct = Dbl(evt, "overall_percent");
            string mode = Str(evt, "overall_mode");

            AppendLog(string.Format("[{0}] {1}: {2}", id, state, detail));
            SafeUi(delegate
            {
                int rowPct = cpct.HasValue ? (int)Math.Round(cpct.Value) : (state == "Ready" || state == "Reused Existing Installation" || state == "Skipped" ? 100 : -1);
                string rightDetail = detail;
                if (dl.HasValue)
                {
                    string dlStr = tot.HasValue && tot.Value > 0 ? FormatBytes(dl.Value) + " / " + FormatBytes(tot.Value) : FormatBytes(dl.Value);
                    rightDetail = dlStr;
                    string spStr = speed.HasValue && speed.Value > 0 ? FormatBytes((long)speed.Value) + "/s" : "—";
                    string etaStr = eta.HasValue ? FormatSeconds((long)Math.Round(eta.Value)) : "—";
                    _metricsLabel.Text = string.Format("Transfer: {0}  •  Speed: {1}  •  ETA: {2}", dlStr, spStr, etaStr);
                }
                else if (!string.IsNullOrEmpty(detail))
                {
                    _metricsLabel.Text = detail;
                }
                if (!string.IsNullOrEmpty(ver) && !string.IsNullOrEmpty(rightDetail) && !rightDetail.Contains(ver))
                    rightDetail = ver + "  •  " + rightDetail;
                UpdateRow(id, state, rowPct, rightDetail);
                if (opct.HasValue) SetOverall((int)Math.Round(opct.Value), mode);
                _activityLabel.Text = string.Format("{0}: {1}", id, string.IsNullOrEmpty(detail) ? state : detail);
            });
            return;
        }
        if (evType == "complete")
        {
            bool ok = evt.ContainsKey("ok") && Convert.ToBoolean(evt["ok"]);
            SafeUi(delegate
            {
                if (ok)
                {
                    _completedOk = true;
                    SetOverall(100, "Verified");
                    _cancelBtn.Enabled = false;
                    _retryBtn.Enabled = false;
                    _launchBtn.Enabled = true;
                    _cloudOnlyCheck.Enabled = true;
                    _activityLabel.Text = "Setup Complete — All required components verified.";
                    AcceptButton = _launchBtn;

                    long validated = Lng(evt, "required_validated") ?? 0;
                    long installed = Lng(evt, "installed_count") ?? 0;
                    long reused = Lng(evt, "reused_count") ?? 0;
                    long skipped = Lng(evt, "skipped_count") ?? 0;
                    double dur = Dbl(evt, "duration_seconds") ?? 0.0;
                    var notices = evt.ContainsKey("optional_notices") ? evt["optional_notices"] as System.Collections.ArrayList : null;

                    var sb = new StringBuilder();
                    sb.AppendLine("Setup Complete");
                    sb.AppendLine();
                    sb.AppendFormat("• Validated Required Components: {0}\r\n", validated);
                    sb.AppendFormat("• Newly Installed Components: {0}\r\n", installed);
                    sb.AppendFormat("• Reused Existing Installations: {0}\r\n", reused);
                    sb.AppendFormat("• Skipped Optional Components: {0}\r\n", skipped);
                    sb.AppendFormat("• Total Setup Duration: {0:0.0}s\r\n", dur);
                    if (notices != null && notices.Count > 0)
                    {
                        sb.AppendLine();
                        sb.AppendLine("Optional Notices:");
                        foreach (object n in notices) sb.AppendLine("  - " + Convert.ToString(n));
                    }
                    sb.AppendLine();
                    sb.AppendLine("Click Launch Application to start AutoClip Studio.");
                    _summaryBodyLabel.Text = sb.ToString();
                    _compListPanel.Visible = false;
                    _summaryPanel.Visible = true;
                }
                else
                {
                    _retryBtn.Enabled = true;
                    _cancelBtn.Enabled = false;
                    _cloudOnlyCheck.Enabled = true;
                    _activityLabel.Text = "Setup failed: " + Str(evt, "error");
                }
            });
        }
    }

    private string EnsureUvWithProgress()
    {
        string tools = Path.Combine(_root, "tools");
        string uv = Path.Combine(tools, "uv.exe");
        SafeUi(delegate { UpdateRow("uv", "Checking", 10, "Checking uv binary"); });
        if (File.Exists(uv))
        {
            SafeUi(delegate { UpdateRow("uv", "Reused Existing Installation", 100, "Verified"); });
            return uv;
        }
        Directory.CreateDirectory(tools);
        string toolchain = File.ReadAllText(Path.Combine(_root, "scripts", "toolchain.json"), Encoding.UTF8);
        var manifest = new JavaScriptSerializer().Deserialize<Dictionary<string, Dictionary<string, string>>>(toolchain);
        var release = manifest["uv"];
        string archive = Path.Combine(tools, "uv-bootstrap.zip");
        DownloadResumable("uv", release["url"], archive, release["sha256"]);
        SafeUi(delegate { UpdateRow("uv", "Configuring", 90, "Extracting uv.exe"); });
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
        SafeUi(delegate { UpdateRow("uv", "Ready", 100, "uv ready"); });
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
                                    _metricsLabel.Text = string.Format("Transfer: {0}  •  Speed: {1}/s  •  ETA: {2}",
                                        sizeTxt, FormatBytes((long)speed), eta.HasValue ? FormatSeconds((long)Math.Round(eta.Value)) : "—");
                                    UpdateRow(compId, "Downloading", pct, sizeTxt);
                                    if (pct >= 0) SetOverall(Math.Max(_overallPercent, 16 + (pct * 6) / 100), "Measured");
                                });
                                lastTick = sw.ElapsedMilliseconds;
                            }
                        }
                    }
                }
                if (!string.IsNullOrEmpty(expectedSha256))
                {
                    SafeUi(delegate { UpdateRow(compId, "Verifying", 100, "Verifying SHA-256"); });
                    using (var fs = File.OpenRead(part))
                    {
                        if (!string.Equals(Sha256Hex(fs), expectedSha256, StringComparison.OrdinalIgnoreCase))
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

    private void UpdateStage(string id, int index, string state, string detail, long? overallPct, string mode = "Estimated")
    {
        SafeUi(delegate
        {
            _currentStageIndex = index;
            _stepSubtitleLabel.Text = string.Format("Step {0} of {1}  •  {2}", index, _totalStages, detail);
            StageRow row;
            if (_stageMap.TryGetValue(id, out row))
            {
                row.State = state;
                row.StateLabel.Text = state;
                if (state == "Completed")
                {
                    row.StepBadge.BackColor = _pal.GreenOk;
                    row.StepBadge.ForeColor = Color.White;
                    row.StateLabel.ForeColor = _pal.GreenOk;
                }
                else if (state == "Failed")
                {
                    row.StepBadge.BackColor = _pal.RoseError;
                    row.StepBadge.ForeColor = Color.White;
                    row.StateLabel.ForeColor = _pal.RoseError;
                }
                else if (state == "Running" || state == "Checking" || state == "Verifying")
                {
                    row.StepBadge.BackColor = _pal.RedPrimary;
                    row.StepBadge.ForeColor = Color.White;
                    row.StateLabel.ForeColor = _pal.RedPrimary;
                }
                else
                {
                    row.StepBadge.BackColor = _pal.Elevated;
                    row.StepBadge.ForeColor = _pal.TextMuted;
                    row.StateLabel.ForeColor = _pal.TextMuted;
                }
            }
            if (overallPct.HasValue) SetOverall((int)overallPct.Value, mode);
        });
    }

    private void UpdateRow(string id, string state, int percent, string detail)
    {
        ComponentRow row;
        if (!_rowMap.TryGetValue(id, out row)) return;
        row.State = state;
        row.StateLabel.Text = state;
        row.StateLabel.ForeColor = state == "Failed" ? _pal.RoseError
            : (state == "Ready" || state == "Reused Existing Installation") ? _pal.GreenOk
            : (state == "Downloading" || state == "Installing" || state == "Configuring" || state == "Verifying" || state == "Checking") ? _pal.RedPrimary
            : _pal.TextMuted;
        if (!string.IsNullOrEmpty(detail)) row.DetailLabel.Text = detail;
        row.Indeterminate = percent < 0;
        if (percent >= 0) row.Percent = Math.Max(0, Math.Min(100, percent));
        row.BarPanel.Invalidate();
    }

    private void SetOverall(int percent, string mode)
    {
        int clamped = _completedOk ? 100 : Math.Max(_overallPercent, Math.Min(99, percent));
        _overallPercent = clamped;
        _overallMode = string.IsNullOrEmpty(mode) ? _overallMode : char.ToUpper(mode[0]) + mode.Substring(1);
        _overallPctLabel.Text = string.Format("{0}%  •  {1}", _overallPercent, _overallMode);
        _overallBar.Invalidate();
    }

    private void AppendLog(string message)
    {
        string clean = Redact(message);
        _log(clean);
        string line = DateTimeOffset.Now.ToString("HH:mm:ss") + " " + clean;
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(_setupLogPath));
            File.AppendAllText(_setupLogPath, line + Environment.NewLine, Encoding.UTF8);
        }
        catch { }
        SafeUi(delegate
        {
            _logBuffer.AppendLine(line);
            _logTextBox.AppendText(line + Environment.NewLine);
        });
    }

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
            using (var p = _job.Start(_python, new[] { "-c", "import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] < (3,13) else 1)" }, _root, null))
            {
                p.Wait(uint.MaxValue);
                return p.ExitCode() == 0;
            }
        }
        catch { return false; }
    }

    private string ResolveLogoPath()
    {
        foreach (string candidate in new[]
        {
            Path.Combine(_root, "autoclip_studio", "assets", "AutoClipTrans.png"),
            Path.Combine(_root, "autoclip_studio", "assets", "symbol.png"),
            Path.Combine(_root, "AutoClipTrans.png")
        })
        {
            if (File.Exists(candidate)) return candidate;
        }
        return null;
    }

    private string ResolveSetupLogPath()
    {
        string configured = Environment.GetEnvironmentVariable("AUTOCLIP_DATA_DIR");
        string dataRoot = string.IsNullOrWhiteSpace(configured)
            ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "AutoClip", "AutoClip Studio")
            : Path.GetFullPath(Path.IsPathRooted(configured) ? configured : Path.Combine(_root, configured));
        return Path.Combine(dataRoot, "logs", "setup.log");
    }

    private static string ReadCpuName()
    {
        try
        {
            using (var key = Registry.LocalMachine.OpenSubKey(@"HARDWARE\DESCRIPTION\System\CentralProcessor\0"))
            {
                object val = key != null ? key.GetValue("ProcessorNameString") : null;
                if (val != null) return val.ToString().Trim();
            }
        }
        catch { }
        return Environment.GetEnvironmentVariable("PROCESSOR_IDENTIFIER") ?? "x86_64 CPU";
    }

    private static long ReadTotalRamBytes()
    {
        var st = new MEMORYSTATUSEX { dwLength = (uint)Marshal.SizeOf(typeof(MEMORYSTATUSEX)) };
        return GlobalMemoryStatusEx(ref st) ? (long)st.ullTotalPhys : 0;
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

    private static string Redact(string text)
    {
        if (string.IsNullOrEmpty(text)) return "";
        return SecretRegex.Replace(text, delegate(Match m)
        {
            return m.Groups[1].Success ? m.Groups[1].Value + "[REDACTED]" : "[REDACTED]";
        });
    }

    private static string Str(Dictionary<string, object> d, string k)
    {
        return d.ContainsKey(k) && d[k] != null ? Convert.ToString(d[k]) : "";
    }

    private static long? Lng(Dictionary<string, object> d, string k)
    {
        if (!d.ContainsKey(k) || d[k] == null) return null;
        return Convert.ToInt64(d[k]);
    }

    private static double? Dbl(Dictionary<string, object> d, string k)
    {
        if (!d.ContainsKey(k) || d[k] == null) return null;
        return Convert.ToDouble(d[k]);
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

    private static string FormatSeconds(long sec)
    {
        if (sec < 60) return sec + "s";
        return string.Format("{0}m {1:00}s", sec / 60, sec % 60);
    }
}
