#import <Cocoa/Cocoa.h>
#import <CommonCrypto/CommonDigest.h>
#import <CoreText/CoreText.h>
#include <fcntl.h>
#include <signal.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/sysctl.h>
#include <unistd.h>

@interface SetupWizardDelegate : NSObject <NSApplicationDelegate>
@property (strong) NSWindow *window;
@property (strong) NSImageView *logoView;
@property (strong) NSTextField *titleLabel;
@property (strong) NSTextField *subtitleLabel;
@property (strong) NSTextField *archBadge;
@property (strong) NSTextField *sysInfoLabel;
@property (strong) NSTextField *spaceInfoLabel;
@property (strong) NSTextField *activityLabel;
@property (strong) NSTextField *overallPctLabel;
@property (strong) NSTextField *metricsLabel;
@property (strong) NSProgressIndicator *progressBar;
@property (strong) NSButton *cloudOnlyCheck;
@property (strong) NSScrollView *stageScrollView;
@property (strong) NSView *stageListDoc;
@property (strong) NSScrollView *compScrollView;
@property (strong) NSView *compListDoc;
@property (strong) NSView *summaryCard;
@property (strong) NSTextField *summaryLabel;
@property (strong) NSButton *detailsToggleButton;
@property (strong) NSButton *copyLogsButton;
@property (strong) NSButton *openLogButton;
@property (strong) NSButton *retryButton;
@property (strong) NSButton *cancelButton;
@property (strong) NSButton *launchButton;
@property (strong) NSScrollView *logScrollView;
@property (strong) NSTextView *logTextView;
@property (strong) NSTask *activeTask;
@property (strong) NSMutableDictionary<NSString *, NSTextField *> *stageStateLabels;
@property (strong) NSMutableDictionary<NSString *, NSDictionary<NSString *, id> *> *compRowWidgets;
@property (copy) NSString *checkoutRoot;
@property (assign) BOOL cancelled;
@property (assign) BOOL running;
@property (assign) BOOL completedOk;
@property (assign) BOOL detailsExpanded;
@property (assign) BOOL isDarkMode;
@property (assign) double lastOverall;
@property (assign) int lockFd;
@end

@implementation SetupWizardDelegate

static BOOL DetectDarkAppearance(void) {
    NSAppearance *app = [NSApp effectiveAppearance];
    NSAppearanceName best = [app bestMatchFromAppearancesWithNames:@[NSAppearanceNameDarkAqua, NSAppearanceNameAqua]];
    return [best isEqualToString:NSAppearanceNameDarkAqua];
}

static NSColor *ColCanvas(BOOL dark) {
    return dark ? [NSColor colorWithRed:0.063 green:0.067 blue:0.086 alpha:1.0]
                : [NSColor colorWithRed:0.961 green:0.965 blue:0.973 alpha:1.0];
}
static NSColor *ColCard(BOOL dark) {
    return dark ? [NSColor colorWithRed:0.102 green:0.110 blue:0.137 alpha:1.0]
                : [NSColor whiteColor];
}
static NSColor *ColBorder(BOOL dark) {
    return dark ? [NSColor colorWithRed:0.169 green:0.180 blue:0.220 alpha:1.0]
                : [NSColor colorWithRed:0.855 green:0.867 blue:0.894 alpha:1.0];
}
static NSColor *ColText(BOOL dark) {
    return dark ? [NSColor colorWithRed:0.961 green:0.961 blue:0.969 alpha:1.0]
                : [NSColor colorWithRed:0.094 green:0.102 blue:0.125 alpha:1.0];
}
static NSColor *ColMuted(BOOL dark) {
    return dark ? [NSColor colorWithRed:0.639 green:0.659 blue:0.710 alpha:1.0]
                : [NSColor colorWithRed:0.361 green:0.384 blue:0.439 alpha:1.0];
}
static NSColor *ColRed(void) {
    return [NSColor colorWithRed:0.898 green:0.224 blue:0.208 alpha:1.0];
}
static NSColor *ColGreen(void) {
    return [NSColor colorWithRed:0.180 green:0.800 blue:0.443 alpha:1.0];
}
static NSColor *ColRose(void) {
    return [NSColor colorWithRed:1.000 green:0.420 blue:0.478 alpha:1.0];
}

static NSString *ResolveCheckoutRoot(void) {
    NSString *bundlePath = [[NSBundle mainBundle] bundlePath];
    NSString *marker = [[[NSBundle mainBundle] resourcePath] stringByAppendingPathComponent:@"checkout_root.txt"];
    if ([[NSFileManager defaultManager] fileExistsAtPath:marker]) {
        NSString *saved = [[NSString stringWithContentsOfFile:marker encoding:NSUTF8StringEncoding error:nil]
            stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceAndNewlineCharacterSet]];
        if (saved.length > 0 && [[NSFileManager defaultManager] fileExistsAtPath:[saved stringByAppendingPathComponent:@"main.py"]]) {
            return saved;
        }
    }
    NSString *parent = [bundlePath stringByDeletingLastPathComponent];
    for (int i = 0; i < 4; i++) {
        if ([[NSFileManager defaultManager] fileExistsAtPath:[parent stringByAppendingPathComponent:@"main.py"]]) {
            return parent;
        }
        parent = [parent stringByDeletingLastPathComponent];
    }
    return [[NSFileManager defaultManager] currentDirectoryPath];
}

static NSString *AppSupportDir(void) {
    NSString *configured = [[[NSProcessInfo processInfo] environment] objectForKey:@"AUTOCLIP_DATA_DIR"];
    if (configured.length > 0) {
        [[NSFileManager defaultManager] createDirectoryAtPath:configured withIntermediateDirectories:YES attributes:nil error:nil];
        return configured;
    }
    NSString *appSupport = [NSSearchPathForDirectoriesInDomains(NSApplicationSupportDirectory, NSUserDomainMask, YES) firstObject];
    NSString *dir = [appSupport stringByAppendingPathComponent:@"AutoClip Studio"];
    [[NSFileManager defaultManager] createDirectoryAtPath:dir withIntermediateDirectories:YES attributes:nil error:nil];
    return dir;
}

static NSString *SetupLogPath(void) {
    NSString *logDir = [AppSupportDir() stringByAppendingPathComponent:@"logs"];
    [[NSFileManager defaultManager] createDirectoryAtPath:logDir withIntermediateDirectories:YES attributes:nil error:nil];
    return [logDir stringByAppendingPathComponent:@"setup.log"];
}

static NSString *LauncherLogPath(void) {
    NSString *logDir = [AppSupportDir() stringByAppendingPathComponent:@"logs"];
    [[NSFileManager defaultManager] createDirectoryAtPath:logDir withIntermediateDirectories:YES attributes:nil error:nil];
    return [logDir stringByAppendingPathComponent:@"launcher.log"];
}

static NSString *RedactSecrets(NSString *input) {
    if (input.length == 0) return @"";
    NSError *err = nil;
    NSRegularExpression *rx = [NSRegularExpression regularExpressionWithPattern:
        @"(?:AIza[\\w-]{35}|sk-[A-Za-z0-9_-]{20,}|((?:authorization|cookie|api[-_ ]?key|access_token|refresh_token|password)\\s*[:=]\\s*)[^\\s,;\"'<>]+)"
        options:NSRegularExpressionCaseInsensitive error:&err];
    if (!rx) return input;
    NSMutableString *out = [input mutableCopy];
    NSArray<NSTextCheckingResult *> *matches = [rx matchesInString:input options:0 range:NSMakeRange(0, input.length)];
    for (NSTextCheckingResult *m in [matches reverseObjectEnumerator]) {
        if ([m rangeAtIndex:1].location != NSNotFound) {
            NSString *prefix = [input substringWithRange:[m rangeAtIndex:1]];
            [out replaceCharactersInRange:m.range withString:[prefix stringByAppendingString:@"[REDACTED]"]];
        } else {
            [out replaceCharactersInRange:m.range withString:@"[REDACTED]"];
        }
    }
    return out;
}

static void AppendLogFile(NSString *path, NSString *line) {
    NSString *cleaned = RedactSecrets(line);
    if (cleaned.length == 0) return;
    if (![[NSFileManager defaultManager] fileExistsAtPath:path]) {
        [[NSFileManager defaultManager] createFileAtPath:path contents:nil attributes:nil];
    }
    NSFileHandle *fh = [NSFileHandle fileHandleForWritingAtPath:path];
    if (!fh) return;
    [fh seekToEndOfFile];
    NSString *entry = [cleaned hasSuffix:@"\n"] ? cleaned : [cleaned stringByAppendingString:@"\n"];
    [fh writeData:[entry dataUsingEncoding:NSUTF8StringEncoding]];
    [fh closeFile];
}

static void RegisterBundledPoppinsFonts(NSString *checkoutRoot) {
    NSArray<NSString *> *dirs = @[
        [checkoutRoot stringByAppendingPathComponent:@"autoclip_studio/assets/fonts/poppins"],
        [checkoutRoot stringByAppendingPathComponent:@"autoclip_studio/assets/fonts"]
    ];
    for (NSString *fontsDir in dirs) {
        NSArray<NSString *> *files = [[NSFileManager defaultManager] contentsOfDirectoryAtPath:fontsDir error:nil];
        for (NSString *name in files) {
            if ([[name pathExtension].lowercaseString isEqualToString:@"ttf"]) {
                NSURL *url = [NSURL fileURLWithPath:[fontsDir stringByAppendingPathComponent:name]];
                CTFontManagerRegisterFontsForURL((__bridge CFURLRef)url, kCTFontManagerScopeProcess, NULL);
            }
        }
    }
}

static NSFont *AppFont(CGFloat size, BOOL bold) {
    NSString *name = bold ? @"Poppins-SemiBold" : @"Poppins-Regular";
    NSFont *font = [NSFont fontWithName:name size:size];
    if (font) return font;
    return bold ? [NSFont boldSystemFontOfSize:size] : [NSFont systemFontOfSize:size];
}

static NSMutableDictionary<NSString *, NSString *> *MergedEnvironment(NSDictionary<NSString *, NSString *> *extraEnv) {
    NSMutableDictionary *env = [NSMutableDictionary dictionaryWithDictionary:[[NSProcessInfo processInfo] environment]];
    NSString *existingPath = env[@"PATH"] ?: @"/usr/bin:/bin:/usr/sbin:/sbin";
    NSString *brewPaths = @"/opt/homebrew/opt/ffmpeg-full/bin:/opt/homebrew/bin:/usr/local/opt/ffmpeg-full/bin:/usr/local/bin";
    env[@"PATH"] = [NSString stringWithFormat:@"%@:%@", brewPaths, existingPath];
    env[@"PYTHONIOENCODING"] = @"utf-8";
    if (extraEnv) [env addEntriesFromDictionary:extraEnv];
    return env;
}

static NSString *FormatBytes(double bytes) {
    if (bytes < 1024.0) return [NSString stringWithFormat:@"%.0f B", bytes];
    if (bytes < 1024.0 * 1024.0) return [NSString stringWithFormat:@"%.0f KB", bytes / 1024.0];
    if (bytes < 1024.0 * 1024.0 * 1024.0) return [NSString stringWithFormat:@"%.1f MB", bytes / (1024.0 * 1024.0)];
    return [NSString stringWithFormat:@"%.2f GB", bytes / (1024.0 * 1024.0 * 1024.0)];
}

static NSString *SysctlString(const char *key) {
    size_t len = 0;
    if (sysctlbyname(key, NULL, &len, NULL, 0) != 0 || len == 0) return @"Apple Silicon";
    char *buf = (char *)malloc(len + 1);
    if (!buf) return @"Apple Silicon";
    NSString *res = @"Apple Silicon";
    if (sysctlbyname(key, buf, &len, NULL, 0) == 0) {
        buf[len] = '\0';
        res = [NSString stringWithUTF8String:buf] ?: @"Apple Silicon";
    }
    free(buf);
    return res;
}

static uint64_t SysctlUInt64(const char *key) {
    uint64_t val = 0;
    size_t len = sizeof(val);
    if (sysctlbyname(key, &val, &len, NULL, 0) == 0) return val;
    return 0;
}

static int RunCheckWithTimeout(NSString *exe, NSArray<NSString *> *args, NSString *cwd, NSTimeInterval timeoutSec, NSString **capturedOut) {
    NSTask *task = [[NSTask alloc] init];
    task.executableURL = [NSURL fileURLWithPath:exe];
    task.arguments = args;
    task.currentDirectoryURL = [NSURL fileURLWithPath:cwd];
    task.environment = MergedEnvironment(nil);
    NSPipe *pipe = [NSPipe pipe];
    task.standardOutput = pipe;
    task.standardError = pipe;

    NSError *err = nil;
    if (![task launchAndReturnError:&err]) {
        if (capturedOut) *capturedOut = err.localizedDescription ?: @"Could not start validation process.";
        return -1;
    }

    dispatch_semaphore_t done = dispatch_semaphore_create(0);
    __block NSData *outData = nil;
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{
        outData = [[pipe fileHandleForReading] readDataToEndOfFile];
        [task waitUntilExit];
        dispatch_semaphore_signal(done);
    });

    long timedOut = dispatch_semaphore_wait(done, dispatch_time(DISPATCH_TIME_NOW, (int64_t)(timeoutSec * NSEC_PER_SEC)));
    if (timedOut != 0) {
        if (task.isRunning) {
            [task terminate];
            usleep(200000);
            if (task.isRunning) kill(task.processIdentifier, SIGKILL);
        }
        if (capturedOut) *capturedOut = @"Startup environment check timed out after 20 seconds.";
        return -2;
    }
    NSString *text = outData ? [[NSString alloc] initWithData:outData encoding:NSUTF8StringEncoding] : @"";
    if (capturedOut) *capturedOut = text ?: @"";
    return task.terminationStatus;
}

static NSString *Sha256File(NSString *path) {
    NSData *data = [NSData dataWithContentsOfFile:path];
    if (!data) return @"";
    unsigned char digest[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256(data.bytes, (CC_LONG)data.length, digest);
    NSMutableString *hex = [NSMutableString stringWithCapacity:CC_SHA256_DIGEST_LENGTH * 2];
    for (int i = 0; i < CC_SHA256_DIGEST_LENGTH; i++) [hex appendFormat:@"%02x", digest[i]];
    return hex;
}

- (BOOL)acquireSetupLock {
    NSString *lockPath = [AppSupportDir() stringByAppendingPathComponent:@"launcher_setup.lock"];
    self.lockFd = open(lockPath.fileSystemRepresentation, O_CREAT | O_RDWR, 0644);
    if (self.lockFd < 0) return YES;
    if (flock(self.lockFd, LOCK_EX | LOCK_NB) != 0) {
        close(self.lockFd);
        self.lockFd = -1;
        return NO;
    }
    return YES;
}

- (void)releaseSetupLock {
    if (self.lockFd >= 0) {
        flock(self.lockFd, LOCK_UN);
        close(self.lockFd);
        self.lockFd = -1;
    }
}

- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    self.lockFd = -1;
    self.checkoutRoot = ResolveCheckoutRoot();
    RegisterBundledPoppinsFonts(self.checkoutRoot);
    self.isDarkMode = DetectDarkAppearance();

    NSString *venvPy = [self.checkoutRoot stringByAppendingPathComponent:@".venv/bin/python3"];
    NSString *setupPy = [self.checkoutRoot stringByAppendingPathComponent:@"scripts/setup.py"];
    NSString *mainPy = [self.checkoutRoot stringByAppendingPathComponent:@"main.py"];
    BOOL forceSetup = [[[NSProcessInfo processInfo] arguments] containsObject:@"--setup"];

    if (!forceSetup && [[NSFileManager defaultManager] fileExistsAtPath:venvPy]) {
        NSString *checkOut = nil;
        int rc = RunCheckWithTimeout(venvPy, @[setupPy, @"--check"], self.checkoutRoot, 20.0, &checkOut);
        if (rc == 0) {
            [self launchMainApp:venvPy mainScript:mainPy];
            [NSApp terminate:nil];
            return;
        }
        AppendLogFile(LauncherLogPath(), [NSString stringWithFormat:@"[launcher] startup validation exited %d:\n%@", rc, checkOut ?: @""]);
    } else if (!forceSetup) {
        AppendLogFile(LauncherLogPath(), @"[launcher] .venv/bin/python3 missing; opening First-Time Setup.");
    }

    if (![self acquireSetupLock]) {
        NSAlert *alert = [[NSAlert alloc] init];
        alert.messageText = @"AutoClip Studio Setup Already Running";
        alert.informativeText = @"Another AutoClip Studio setup window is already validating or installing dependencies.";
        [alert runModal];
        [NSApp terminate:nil];
        return;
    }

    [self buildSetupWindow];
    [self populateDefaultPlan];
    [self startBootstrapAsync:NO];
}

- (void)applicationWillTerminate:(NSNotification *)notification {
    [self terminateActiveTask];
    [self releaseSetupLock];
}

- (void)terminateActiveTask {
    NSTask *task = self.activeTask;
    if (task && task.isRunning) {
        pid_t pid = task.processIdentifier;
        [task terminate];
        usleep(200000);
        if (task.isRunning && pid > 0) {
            kill(pid, SIGKILL);
        }
    }
    self.activeTask = nil;
}

- (NSView *)makeCardWithFrame:(NSRect)frame {
    NSView *card = [[NSView alloc] initWithFrame:frame];
    card.wantsLayer = YES;
    card.layer.backgroundColor = ColCard(self.isDarkMode).CGColor;
    card.layer.borderColor = ColBorder(self.isDarkMode).CGColor;
    card.layer.borderWidth = 1.0;
    card.layer.cornerRadius = 12.0;
    return card;
}

- (void)styleCheckbox:(NSButton *)btn title:(NSString *)title {
    NSDictionary *attrs = @{
        NSForegroundColorAttributeName: ColText(self.isDarkMode),
        NSFontAttributeName: AppFont(12.0, NO),
    };
    btn.attributedTitle = [[NSAttributedString alloc] initWithString:title attributes:attrs];
}

- (void)buildSetupWindow {
    NSRect frame = NSMakeRect(0, 0, 880, 650);
    self.window = [[NSWindow alloc] initWithContentRect:frame
                                              styleMask:(NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable)
                                                backing:NSBackingStoreBuffered
                                                  defer:NO];
    self.window.title = @"AutoClip Studio Setup";
    self.window.minSize = NSMakeSize(780, 580);
    self.window.backgroundColor = ColCanvas(self.isDarkMode);
    [self.window center];

    NSView *content = self.window.contentView;
    content.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;

    // Header
    NSString *logoPath = [self.checkoutRoot stringByAppendingPathComponent:@"autoclip_studio/assets/AutoClipTrans.png"];
    if (![[NSFileManager defaultManager] fileExistsAtPath:logoPath]) {
        logoPath = [[NSBundle mainBundle] pathForResource:@"AutoClipTrans" ofType:@"png"];
    }
    int textLeft = 24;
    if (logoPath && [[NSFileManager defaultManager] fileExistsAtPath:logoPath]) {
        NSImage *img = [[NSImage alloc] initWithContentsOfFile:logoPath];
        self.logoView = [[NSImageView alloc] initWithFrame:NSMakeRect(24, 584, 132, 42)];
        self.logoView.imageScaling = NSImageScaleProportionallyUpOrDown;
        self.logoView.image = img;
        self.logoView.autoresizingMask = NSViewMinYMargin;
        [content addSubview:self.logoView];
        textLeft = 168;
    }

    self.titleLabel = [NSTextField labelWithString:@"AutoClip Studio Setup"];
    self.titleLabel.frame = NSMakeRect(textLeft, 602, 480, 26);
    self.titleLabel.font = AppFont(18, YES);
    self.titleLabel.textColor = ColText(self.isDarkMode);
    self.titleLabel.autoresizingMask = NSViewMinYMargin | NSViewWidthSizable;
    [content addSubview:self.titleLabel];

    self.subtitleLabel = [NSTextField labelWithString:@"Step 1 of 8  •  Initializing bootstrap environment"];
    self.subtitleLabel.frame = NSMakeRect(textLeft, 580, 520, 18);
    self.subtitleLabel.font = AppFont(12, NO);
    self.subtitleLabel.textColor = ColMuted(self.isDarkMode);
    self.subtitleLabel.autoresizingMask = NSViewMinYMargin | NSViewWidthSizable;
    [content addSubview:self.subtitleLabel];

    self.archBadge = [NSTextField labelWithString:@"macOS ARM64"];
    self.archBadge.frame = NSMakeRect(746, 594, 110, 22);
    self.archBadge.alignment = NSTextAlignmentCenter;
    self.archBadge.font = AppFont(11, YES);
    self.archBadge.textColor = ColMuted(self.isDarkMode);
    self.archBadge.autoresizingMask = NSViewMinYMargin | NSViewMinXMargin;
    [content addSubview:self.archBadge];

    // System compatibility card
    NSView *sysCard = [self makeCardWithFrame:NSMakeRect(24, 482, 832, 86)];
    sysCard.autoresizingMask = NSViewMinYMargin | NSViewWidthSizable;
    [content addSubview:sysCard];

    NSString *cpuBrand = SysctlString("machdep.cpu.brand_string");
    uint64_t ramBytes = SysctlUInt64("hw.memsize");
    NSDictionary *fsAttrs = [[NSFileManager defaultManager] attributesOfFileSystemForPath:self.checkoutRoot error:nil];
    double freeBytes = [fsAttrs[NSFileSystemFreeSize] doubleValue];
    NSString *osVer = [[NSProcessInfo processInfo] operatingSystemVersionString];

    self.sysInfoLabel = [NSTextField labelWithString:[NSString stringWithFormat:@"macOS %@ (arm64)  •  %@  •  RAM: %@",
        osVer, cpuBrand, FormatBytes((double)ramBytes)]];
    self.sysInfoLabel.frame = NSMakeRect(16, 56, 800, 18);
    self.sysInfoLabel.font = AppFont(12.5, YES);
    self.sysInfoLabel.textColor = ColText(self.isDarkMode);
    self.sysInfoLabel.autoresizingMask = NSViewWidthSizable;
    [sysCard addSubview:self.sysInfoLabel];

    self.spaceInfoLabel = [NSTextField labelWithString:[NSString stringWithFormat:@"Acceleration: Apple Silicon Unified Memory GPU (VideoToolbox + MLX)  •  Free Disk: %@",
        FormatBytes(freeBytes)]];
    self.spaceInfoLabel.frame = NSMakeRect(16, 34, 800, 17);
    self.spaceInfoLabel.font = AppFont(11.5, NO);
    self.spaceInfoLabel.textColor = ColMuted(self.isDarkMode);
    self.spaceInfoLabel.autoresizingMask = NSViewWidthSizable;
    [sysCard addSubview:self.spaceInfoLabel];

    self.cloudOnlyCheck = [NSButton checkboxWithTitle:@"" target:nil action:nil];
    self.cloudOnlyCheck.frame = NSMakeRect(16, 8, 660, 20);
    self.cloudOnlyCheck.state = NSControlStateValueOn;
    [self styleCheckbox:self.cloudOnlyCheck title:@"Cloud-only transcription (skip optional local Whisper model downloads)"];
    [sysCard addSubview:self.cloudOnlyCheck];

    // Overall progress card
    NSView *progCard = [self makeCardWithFrame:NSMakeRect(24, 386, 832, 86)];
    progCard.autoresizingMask = NSViewMinYMargin | NSViewWidthSizable;
    [content addSubview:progCard];

    self.activityLabel = [NSTextField labelWithString:@"Validating system compatibility..."];
    self.activityLabel.frame = NSMakeRect(16, 56, 600, 20);
    self.activityLabel.font = AppFont(13, YES);
    self.activityLabel.textColor = ColText(self.isDarkMode);
    self.activityLabel.autoresizingMask = NSViewWidthSizable;
    [progCard addSubview:self.activityLabel];

    self.overallPctLabel = [NSTextField labelWithString:@"0%  •  Estimated"];
    self.overallPctLabel.frame = NSMakeRect(630, 56, 186, 20);
    self.overallPctLabel.alignment = NSTextAlignmentRight;
    self.overallPctLabel.font = AppFont(12, YES);
    self.overallPctLabel.textColor = ColRed();
    self.overallPctLabel.autoresizingMask = NSViewMinXMargin;
    [progCard addSubview:self.overallPctLabel];

    self.progressBar = [[NSProgressIndicator alloc] initWithFrame:NSMakeRect(16, 30, 800, 18)];
    self.progressBar.style = NSProgressIndicatorStyleBar;
    self.progressBar.minValue = 0.0;
    self.progressBar.maxValue = 100.0;
    self.progressBar.indeterminate = YES;
    self.progressBar.autoresizingMask = NSViewWidthSizable;
    [self.progressBar startAnimation:nil];
    [progCard addSubview:self.progressBar];

    self.metricsLabel = [NSTextField labelWithString:@"Stage telemetry initializing..."];
    self.metricsLabel.frame = NSMakeRect(16, 8, 800, 17);
    self.metricsLabel.font = [NSFont monospacedSystemFontOfSize:11.0 weight:NSFontWeightRegular];
    self.metricsLabel.textColor = ColMuted(self.isDarkMode);
    self.metricsLabel.autoresizingMask = NSViewWidthSizable;
    [progCard addSubview:self.metricsLabel];

    // Two-pane body: Left Stage Stepper + Right Component Checklist / Completion Summary
    NSView *stageCard = [self makeCardWithFrame:NSMakeRect(24, 68, 250, 306)];
    stageCard.autoresizingMask = NSViewHeightSizable;
    [content addSubview:stageCard];

    self.stageScrollView = [[NSScrollView alloc] initWithFrame:NSMakeRect(8, 8, 234, 290)];
    self.stageScrollView.drawsBackground = NO;
    self.stageScrollView.hasVerticalScroller = YES;
    self.stageScrollView.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
    self.stageListDoc = [[NSView alloc] initWithFrame:NSMakeRect(0, 0, 234, 300)];
    self.stageScrollView.documentView = self.stageListDoc;
    [stageCard addSubview:self.stageScrollView];

    self.compScrollView = [[NSScrollView alloc] initWithFrame:NSMakeRect(286, 68, 570, 306)];
    self.compScrollView.drawsBackground = NO;
    self.compScrollView.hasVerticalScroller = YES;
    self.compScrollView.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
    self.compListDoc = [[NSView alloc] initWithFrame:NSMakeRect(0, 0, 570, 420)];
    self.compScrollView.documentView = self.compListDoc;
    [content addSubview:self.compScrollView];

    self.summaryCard = [self makeCardWithFrame:NSMakeRect(286, 68, 570, 306)];
    self.summaryCard.hidden = YES;
    self.summaryCard.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
    self.summaryLabel = [NSTextField wrappingLabelWithString:@""];
    self.summaryLabel.frame = NSMakeRect(20, 20, 530, 266);
    self.summaryLabel.font = AppFont(13, NO);
    self.summaryLabel.textColor = ColText(self.isDarkMode);
    self.summaryLabel.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
    [self.summaryCard addSubview:self.summaryLabel];
    [content addSubview:self.summaryCard];

    // Expandable installation logs drawer
    self.logScrollView = [[NSScrollView alloc] initWithFrame:NSMakeRect(24, 64, 832, 160)];
    self.logScrollView.hasVerticalScroller = YES;
    self.logScrollView.borderType = NSBezelBorder;
    self.logScrollView.hidden = YES;
    self.logScrollView.autoresizingMask = NSViewWidthSizable;
    self.logTextView = [[NSTextView alloc] initWithFrame:NSMakeRect(0, 0, 832, 160)];
    self.logTextView.editable = NO;
    self.logTextView.font = [NSFont monospacedSystemFontOfSize:11 weight:NSFontWeightRegular];
    self.logTextView.backgroundColor = ColCanvas(self.isDarkMode);
    self.logTextView.textColor = ColText(self.isDarkMode);
    self.logScrollView.documentView = self.logTextView;
    [content addSubview:self.logScrollView];

    // Footer buttons
    self.detailsToggleButton = [NSButton buttonWithTitle:@"Show Logs" target:self action:@selector(toggleDetailsClicked:)];
    self.detailsToggleButton.frame = NSMakeRect(24, 20, 108, 32);
    self.detailsToggleButton.bezelStyle = NSBezelStyleRounded;
    self.detailsToggleButton.autoresizingMask = NSViewMaxYMargin;
    [content addSubview:self.detailsToggleButton];

    self.copyLogsButton = [NSButton buttonWithTitle:@"Copy Diagnostics" target:self action:@selector(copyLogsClicked:)];
    self.copyLogsButton.frame = NSMakeRect(138, 20, 138, 32);
    self.copyLogsButton.bezelStyle = NSBezelStyleRounded;
    self.copyLogsButton.autoresizingMask = NSViewMaxYMargin;
    [content addSubview:self.copyLogsButton];

    self.openLogButton = [NSButton buttonWithTitle:@"Open Log" target:self action:@selector(openLogClicked:)];
    self.openLogButton.frame = NSMakeRect(282, 20, 96, 32);
    self.openLogButton.bezelStyle = NSBezelStyleRounded;
    self.openLogButton.autoresizingMask = NSViewMaxYMargin;
    [content addSubview:self.openLogButton];

    self.retryButton = [NSButton buttonWithTitle:@"Retry Failed" target:self action:@selector(retryClicked:)];
    self.retryButton.frame = NSMakeRect(472, 20, 110, 32);
    self.retryButton.bezelStyle = NSBezelStyleRounded;
    self.retryButton.enabled = NO;
    self.retryButton.autoresizingMask = NSViewMinXMargin | NSViewMaxYMargin;
    [content addSubview:self.retryButton];

    self.cancelButton = [NSButton buttonWithTitle:@"Cancel Setup" target:self action:@selector(cancelClicked:)];
    self.cancelButton.frame = NSMakeRect(590, 20, 114, 32);
    self.cancelButton.bezelStyle = NSBezelStyleRounded;
    self.cancelButton.autoresizingMask = NSViewMinXMargin | NSViewMaxYMargin;
    [content addSubview:self.cancelButton];

    self.launchButton = [NSButton buttonWithTitle:@"Launch Application" target:self action:@selector(launchClicked:)];
    self.launchButton.frame = NSMakeRect(712, 20, 144, 32);
    self.launchButton.bezelStyle = NSBezelStyleRounded;
    self.launchButton.bezelColor = ColRed();
    self.launchButton.enabled = NO;
    self.launchButton.autoresizingMask = NSViewMinXMargin | NSViewMaxYMargin;
    [content addSubview:self.launchButton];

    [self.window makeKeyAndOrderFront:nil];
    [NSApp activateIgnoringOtherApps:YES];
}

- (void)populateDefaultPlan {
    self.stageStateLabels = [NSMutableDictionary dictionary];
    self.compRowWidgets = [NSMutableDictionary dictionary];

    NSArray<NSArray<NSString *> *> *stages = @[
        @[@"system_compatibility", @"1. System Compatibility"],
        @[@"python_runtime", @"2. Python Runtime"],
        @[@"virtual_env", @"3. Virtual Environment"],
        @[@"core_packages", @"4. Core Dependencies"],
        @[@"media_tools", @"5. Media Tools"],
        @[@"hardware_accel", @"6. Hardware Accel"],
        @[@"transcription_models", @"7. Transcription & Models"],
        @[@"final_validation", @"8. Final Validation"],
    ];
    CGFloat y = 260;
    for (NSArray<NSString *> *pair in stages) {
        NSTextField *nameLbl = [NSTextField labelWithString:pair[1]];
        nameLbl.frame = NSMakeRect(8, y + 14, 218, 16);
        nameLbl.font = AppFont(11.5, YES);
        nameLbl.textColor = ColText(self.isDarkMode);
        [self.stageListDoc addSubview:nameLbl];

        NSTextField *stLbl = [NSTextField labelWithString:@"Pending"];
        stLbl.frame = NSMakeRect(22, y, 204, 14);
        stLbl.font = AppFont(10.5, NO);
        stLbl.textColor = ColMuted(self.isDarkMode);
        [self.stageListDoc addSubview:stLbl];
        self.stageStateLabels[pair[0]] = stLbl;
        y -= 34;
    }

    NSString *manifestPath = [self.checkoutRoot stringByAppendingPathComponent:@"scripts/dependencies.json"];
    NSData *data = [NSData dataWithContentsOfFile:manifestPath];
    NSDictionary *doc = data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:nil] : nil;
    NSArray *comps = [doc isKindOfClass:[NSDictionary class]] ? doc[@"components"] : nil;
    NSMutableArray<NSDictionary *> *applicable = [NSMutableArray array];
    for (NSDictionary *item in comps) {
        NSArray *plats = item[@"platforms"];
        if (plats && ![plats containsObject:@"macos"]) continue;
        [applicable addObject:item];
    }

    CGFloat rowH = 48.0;
    CGFloat totalH = MAX(306.0, applicable.count * (rowH + 6.0) + 8.0);
    self.compListDoc.frame = NSMakeRect(0, 0, 560, totalH);
    CGFloat cy = totalH - rowH - 6.0;
    for (NSDictionary *item in applicable) {
        NSString *cid = item[@"id"] ?: @"";
        NSString *name = item[@"name"] ?: cid;
        BOOL mandatory = [item[@"mandatory"] boolValue];
        NSView *card = [self makeCardWithFrame:NSMakeRect(0, cy, 550, rowH)];
        card.autoresizingMask = NSViewWidthSizable;

        NSTextField *tLbl = [NSTextField labelWithString:[NSString stringWithFormat:@"%@  •  %@", name, mandatory ? @"Required" : @"Optional"]];
        tLbl.frame = NSMakeRect(12, 24, 340, 16);
        tLbl.font = AppFont(11.5, YES);
        tLbl.textColor = ColText(self.isDarkMode);
        [card addSubview:tLbl];

        NSTextField *sLbl = [NSTextField labelWithString:@"Pending"];
        sLbl.frame = NSMakeRect(360, 24, 178, 16);
        sLbl.alignment = NSTextAlignmentRight;
        sLbl.font = AppFont(11, YES);
        sLbl.textColor = ColMuted(self.isDarkMode);
        [card addSubview:sLbl];

        NSTextField *dLbl = [NSTextField labelWithString:@"Waiting"];
        dLbl.frame = NSMakeRect(12, 6, 526, 15);
        dLbl.font = AppFont(10.5, NO);
        dLbl.textColor = ColMuted(self.isDarkMode);
        [card addSubview:dLbl];

        [self.compListDoc addSubview:card];
        self.compRowWidgets[cid] = @{@"state": sLbl, @"detail": dLbl};
        cy -= (rowH + 6.0);
    }
}

- (void)appendDiagnosticLine:(NSString *)line {
    NSString *cleaned = RedactSecrets(line);
    if (cleaned.length == 0) return;
    AppendLogFile(SetupLogPath(), cleaned);
    dispatch_async(dispatch_get_main_queue(), ^{
        NSString *entry = [cleaned hasSuffix:@"\n"] ? cleaned : [cleaned stringByAppendingString:@"\n"];
        NSAttributedString *as = [[NSAttributedString alloc] initWithString:entry attributes:@{
            NSForegroundColorAttributeName: ColText(self.isDarkMode),
            NSFontAttributeName: [NSFont monospacedSystemFontOfSize:11 weight:NSFontWeightRegular],
        }];
        [[self.logTextView textStorage] appendAttributedString:as];
        [self.logTextView scrollRangeToVisible:NSMakeRange(self.logTextView.string.length, 0)];
    });
}

- (void)toggleDetailsClicked:(id)sender {
    self.detailsExpanded = !self.detailsExpanded;
    NSRect winFrame = self.window.frame;
    CGFloat delta = self.detailsExpanded ? 180.0 : -180.0;
    winFrame.origin.y -= delta;
    winFrame.size.height += delta;
    [self.window setFrame:winFrame display:YES animate:YES];
    self.logScrollView.hidden = !self.detailsExpanded;
    if (self.detailsExpanded) {
        self.logScrollView.frame = NSMakeRect(24, 62, self.window.contentView.bounds.size.width - 48.0, 168);
    }
    self.detailsToggleButton.title = self.detailsExpanded ? @"Hide Logs" : @"Show Logs";
}

- (void)copyLogsClicked:(id)sender {
    NSString *text = self.logTextView.string.length > 0 ? self.logTextView.string : @"No setup diagnostics yet.";
    NSPasteboard *pb = [NSPasteboard generalPasteboard];
    [pb clearContents];
    [pb setString:text forType:NSPasteboardTypeString];
}

- (void)openLogClicked:(id)sender {
    NSString *path = SetupLogPath();
    if (![[NSFileManager defaultManager] fileExistsAtPath:path]) {
        [@"" writeToFile:path atomically:YES encoding:NSUTF8StringEncoding error:nil];
    }
    [[NSWorkspace sharedWorkspace] openURL:[NSURL fileURLWithPath:path]];
}

- (void)retryClicked:(id)sender {
    self.cancelled = NO;
    self.retryButton.enabled = NO;
    self.launchButton.enabled = NO;
    self.cancelButton.enabled = YES;
    self.summaryCard.hidden = YES;
    self.compScrollView.hidden = NO;
    [self startBootstrapAsync:YES];
}

- (void)cancelClicked:(id)sender {
    if (self.running) {
        self.cancelled = YES;
        [self terminateActiveTask];
        self.activityLabel.stringValue = @"Setup cancelled. Resumable partial downloads and completed items preserved.";
        self.retryButton.enabled = YES;
        self.cancelButton.title = @"Close";
        return;
    }
    [self releaseSetupLock];
    [NSApp terminate:nil];
}

- (void)launchClicked:(id)sender {
    if (!self.completedOk) return;
    NSString *venvPy = [self.checkoutRoot stringByAppendingPathComponent:@".venv/bin/python3"];
    NSString *mainPy = [self.checkoutRoot stringByAppendingPathComponent:@"main.py"];
    [self releaseSetupLock];
    [self launchMainApp:venvPy mainScript:mainPy];
    [NSApp terminate:nil];
}

- (void)handleProgressLine:(NSString *)line {
    if ([line hasPrefix:@"{"]) {
        NSData *data = [line dataUsingEncoding:NSUTF8StringEncoding];
        NSDictionary *obj = data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:nil] : nil;
        if ([obj isKindOfClass:[NSDictionary class]] && obj[@"event"]) {
            NSString *ev = obj[@"event"];
            if ([ev isEqualToString:@"stage"]) {
                NSString *sid = obj[@"stage_id"] ?: @"";
                NSNumber *idx = obj[@"stage_index"];
                NSNumber *tot = obj[@"stage_total"];
                NSString *st = obj[@"state"] ?: @"";
                NSString *detail = obj[@"detail"] ?: @"";
                NSNumber *pct = obj[@"overall_percent"];
                NSString *mode = obj[@"overall_mode"] ?: @"estimated";
                dispatch_async(dispatch_get_main_queue(), ^{
                    self.subtitleLabel.stringValue = [NSString stringWithFormat:@"Step %@ of %@  •  %@", idx ?: @1, tot ?: @8, detail];
                    NSTextField *stLbl = self.stageStateLabels[sid];
                    if (stLbl) {
                        stLbl.stringValue = st;
                        stLbl.textColor = [st isEqualToString:@"Completed"] ? ColGreen() :
                                          [st isEqualToString:@"Failed"] ? ColRose() :
                                          ([st isEqualToString:@"Running"] || [st isEqualToString:@"Checking"] || [st isEqualToString:@"Verifying"]) ? ColRed() :
                                          ColMuted(self.isDarkMode);
                    }
                    if (pct && ![pct isKindOfClass:[NSNull class]]) {
                        double p = MAX(self.lastOverall, MIN(99.0, [pct doubleValue]));
                        self.lastOverall = p;
                        [self.progressBar stopAnimation:nil];
                        self.progressBar.indeterminate = NO;
                        self.progressBar.doubleValue = p;
                        self.overallPctLabel.stringValue = [NSString stringWithFormat:@"%.0f%%  •  %@", p, [mode capitalizedString]];
                    }
                });
                return;
            }
            if ([ev isEqualToString:@"log"]) {
                [self appendDiagnosticLine:[NSString stringWithFormat:@"[%@] [%@] %@", obj[@"level"] ?: @"INFO", obj[@"component"] ?: @"setup", obj[@"message"] ?: @""]];
                return;
            }
            if ([ev isEqualToString:@"progress"]) {
                NSString *compId = obj[@"id"] ?: @"";
                NSString *state = obj[@"state"] ?: @"";
                NSString *detail = obj[@"detail"] ?: @"";
                NSNumber *opct = obj[@"overall_percent"];
                NSString *mode = obj[@"overall_mode"] ?: @"estimated";
                NSNumber *downloaded = obj[@"downloaded"];
                NSNumber *total = obj[@"total"];
                NSNumber *speed = obj[@"speed_bps"];
                NSNumber *eta = obj[@"eta_seconds"];

                [self appendDiagnosticLine:[NSString stringWithFormat:@"[%@] %@%@", compId, state, detail.length ? [@": " stringByAppendingString:detail] : @""]];
                dispatch_async(dispatch_get_main_queue(), ^{
                    if (detail.length > 0) self.activityLabel.stringValue = [NSString stringWithFormat:@"%@: %@", compId, detail];
                    NSDictionary *widgets = self.compRowWidgets[compId];
                    if (widgets) {
                        NSTextField *sLbl = widgets[@"state"];
                        NSTextField *dLbl = widgets[@"detail"];
                        sLbl.stringValue = state;
                        sLbl.textColor = [state isEqualToString:@"Failed"] ? ColRose() :
                                         ([state isEqualToString:@"Ready"] || [state isEqualToString:@"Reused Existing Installation"]) ? ColGreen() :
                                         ([state isEqualToString:@"Downloading"] || [state isEqualToString:@"Installing"] || [state isEqualToString:@"Configuring"] || [state isEqualToString:@"Verifying"] || [state isEqualToString:@"Checking"]) ? ColRed() :
                                         ColMuted(self.isDarkMode);
                        if (detail.length > 0) dLbl.stringValue = detail;
                    }
                    if (opct && ![opct isKindOfClass:[NSNull class]]) {
                        double p = MAX(self.lastOverall, MIN(99.0, [opct doubleValue]));
                        self.lastOverall = p;
                        [self.progressBar stopAnimation:nil];
                        self.progressBar.indeterminate = NO;
                        self.progressBar.doubleValue = p;
                        self.overallPctLabel.stringValue = [NSString stringWithFormat:@"%.0f%%  •  %@", p, [mode capitalizedString]];
                    }
                    if (downloaded && total && ![downloaded isKindOfClass:[NSNull class]] && ![total isKindOfClass:[NSNull class]] && [total doubleValue] > 0) {
                        double d = [downloaded doubleValue], t = [total doubleValue];
                        double sp = (speed && ![speed isKindOfClass:[NSNull class]]) ? [speed doubleValue] : 0.0;
                        double etaSec = (eta && ![eta isKindOfClass:[NSNull class]]) ? [eta doubleValue] : -1.0;
                        self.metricsLabel.stringValue = [NSString stringWithFormat:@"%@ / %@ (%.0f%%) • %@/s%@",
                            FormatBytes(d), FormatBytes(t), (d / t) * 100.0, FormatBytes(sp),
                            etaSec >= 0 ? [NSString stringWithFormat:@" • ETA %.0fs", etaSec] : @""];
                    } else if (detail.length > 0) {
                        self.metricsLabel.stringValue = detail;
                    }
                });
                return;
            }
            if ([ev isEqualToString:@"complete"]) {
                BOOL ok = [obj[@"ok"] boolValue];
                dispatch_async(dispatch_get_main_queue(), ^{
                    [self.progressBar stopAnimation:nil];
                    self.progressBar.indeterminate = NO;
                    if (ok) {
                        self.completedOk = YES;
                        self.progressBar.doubleValue = 100.0;
                        self.overallPctLabel.stringValue = @"100%  •  Verified";
                        self.activityLabel.stringValue = @"Setup Complete — All required components verified.";
                        self.launchButton.enabled = YES;
                        self.cancelButton.title = @"Close";
                        self.retryButton.enabled = NO;
                        NSArray *notices = [obj[@"optional_notices"] isKindOfClass:[NSArray class]] ? obj[@"optional_notices"] : @[];
                        NSString *noticeText = notices.count > 0 ? [@"\n\nOptional Notices:\n• " stringByAppendingString:[notices componentsJoinedByString:@"\n• "]] : @"";
                        self.summaryLabel.stringValue = [NSString stringWithFormat:
                            @"Setup Complete\n\n"
                            @"• Validated Required Components: %@\n"
                            @"• Newly Installed Components: %@\n"
                            @"• Reused Existing Installations: %@\n"
                            @"• Skipped Optional Components: %@\n"
                            @"• Total Duration: %@s%@"\n\n"
                            @"Click Launch Application to start AutoClip Studio.",
                            obj[@"required_validated"] ?: @0,
                            obj[@"installed_count"] ?: @0,
                            obj[@"reused_count"] ?: @0,
                            obj[@"skipped_count"] ?: @0,
                            obj[@"duration_seconds"] ?: @0,
                            noticeText];
                        self.compScrollView.hidden = YES;
                        self.summaryCard.hidden = NO;
                    }
                });
                return;
            }
        }
    }
    [self appendDiagnosticLine:line];
}

- (int)runStreamingProcess:(NSString *)exe args:(NSArray<NSString *> *)args extraEnv:(NSDictionary<NSString *, NSString *> *)extraEnv {
    NSTask *task = [[NSTask alloc] init];
    task.executableURL = [NSURL fileURLWithPath:exe];
    task.arguments = args;
    task.currentDirectoryURL = [NSURL fileURLWithPath:self.checkoutRoot];
    task.environment = MergedEnvironment(extraEnv);

    NSPipe *pipe = [NSPipe pipe];
    task.standardOutput = pipe;
    task.standardError = pipe;

    NSError *err = nil;
    if (![task launchAndReturnError:&err]) {
        [self appendDiagnosticLine:[NSString stringWithFormat:@"Failed to launch %@: %@", exe, err.localizedDescription]];
        return -1;
    }
    self.activeTask = task;

    NSFileHandle *reader = [pipe fileHandleForReading];
    NSMutableString *buffer = [NSMutableString string];
    while (YES) {
        NSData *chunk = [reader availableData];
        if (chunk.length == 0) break;
        NSString *piece = [[NSString alloc] initWithData:chunk encoding:NSUTF8StringEncoding];
        if (!piece) continue;
        [buffer appendString:piece];
        NSRange nl;
        while ((nl = [buffer rangeOfString:@"\n"]).location != NSNotFound) {
            NSString *line = [[buffer substringToIndex:nl.location] stringByTrimmingCharactersInSet:[NSCharacterSet newlineCharacterSet]];
            [buffer deleteCharactersInRange:NSMakeRange(0, nl.location + 1)];
            if (line.length > 0) [self handleProgressLine:line];
        }
    }
    if (buffer.length > 0) [self handleProgressLine:buffer];
    [task waitUntilExit];
    int status = task.terminationStatus;
    self.activeTask = nil;
    return status;
}

- (void)startBootstrapAsync:(BOOL)repairOnly {
    self.running = YES;
    self.completedOk = NO;
    if (!repairOnly) self.lastOverall = 0.0;
    BOOL cloudOnly = (self.cloudOnlyCheck.state == NSControlStateValueOn);
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
        NSString *toolsDir = [self.checkoutRoot stringByAppendingPathComponent:@"tools"];
        NSString *uvPath = [toolsDir stringByAppendingPathComponent:@"uv"];
        NSString *venvPy = [self.checkoutRoot stringByAppendingPathComponent:@".venv/bin/python3"];
        NSString *setupPy = [self.checkoutRoot stringByAppendingPathComponent:@"scripts/setup.py"];
        [[NSFileManager defaultManager] createDirectoryAtPath:toolsDir withIntermediateDirectories:YES attributes:nil error:nil];

        NSDictionary *uvEnv = @{
            @"UV_PYTHON_INSTALL_DIR": [self.checkoutRoot stringByAppendingPathComponent:@".runtime"],
            @"UV_CACHE_DIR": [self.checkoutRoot stringByAppendingPathComponent:@".uv-cache"],
            @"PYTHONIOENCODING": @"utf-8"
        };

        int rc = -1;
        NSString *modeFlag = cloudOnly ? @"--cloud-only" : @"--repair-only";
        if ([[NSFileManager defaultManager] fileExistsAtPath:venvPy]) {
            rc = [self runStreamingProcess:venvPy args:@[setupPy, @"--json-progress", @"--no-tests", modeFlag] extraEnv:uvEnv];
        }

        if (rc != 0 && !self.cancelled) {
            if (![[NSFileManager defaultManager] fileExistsAtPath:uvPath]) {
                NSString *toolchainPath = [self.checkoutRoot stringByAppendingPathComponent:@"scripts/toolchain.json"];
                NSData *tcData = [NSData dataWithContentsOfFile:toolchainPath];
                NSDictionary *tc = tcData ? [NSJSONSerialization JSONObjectWithData:tcData options:0 error:nil] : nil;
                NSDictionary *uvSpec = tc[@"uv_macos_arm64"];
                NSString *urlStr = uvSpec[@"url"];
                NSString *sha = uvSpec[@"sha256"];
                if (urlStr.length > 0) {
                    dispatch_async(dispatch_get_main_queue(), ^{
                        self.activityLabel.stringValue = @"Downloading native ARM64 uv bootstrapper...";
                    });
                    [self appendDiagnosticLine:[NSString stringWithFormat:@"Downloading uv from %@", urlStr]];
                    NSString *tarPath = [toolsDir stringByAppendingPathComponent:@"uv-arm64.tar.gz"];
                    NSData *dl = [NSData dataWithContentsOfURL:[NSURL URLWithString:urlStr]];
                    if (dl.length > 0) {
                        [dl writeToFile:tarPath atomically:YES];
                        if (sha.length == 0 || [[Sha256File(tarPath) lowercaseString] isEqualToString:[sha lowercaseString]]) {
                            [self runStreamingProcess:@"/usr/bin/tar" args:@[@"-xzf", tarPath, @"-C", toolsDir, @"--strip-components=1"] extraEnv:nil];
                        }
                        [[NSFileManager defaultManager] removeItemAtPath:tarPath error:nil];
                    }
                }
            }
            if (!self.cancelled && [[NSFileManager defaultManager] fileExistsAtPath:uvPath]) {
                rc = [self runStreamingProcess:uvPath args:@[@"run", @"--no-project", @"--managed-python", @"--python", @"3.12", @"--", @"python3", setupPy, @"--json-progress", @"--no-tests", modeFlag] extraEnv:uvEnv];
            }
        }

        self.running = NO;
        if (self.cancelled) return;
        dispatch_async(dispatch_get_main_queue(), ^{
            [self.progressBar stopAnimation:nil];
            self.progressBar.indeterminate = NO;
            if (rc != 0 && !self.completedOk) {
                self.activityLabel.stringValue = @"Setup failed. Review logs or click Retry Failed.";
                self.metricsLabel.stringValue = [NSString stringWithFormat:@"Log file: %@", SetupLogPath()];
                self.retryButton.enabled = YES;
                self.cancelButton.title = @"Close";
                if (!self.detailsExpanded) [self toggleDetailsClicked:nil];
            }
        });
    });
}

- (void)launchMainApp:(NSString *)pythonExe mainScript:(NSString *)mainPy {
    NSTask *appTask = [[NSTask alloc] init];
    appTask.executableURL = [NSURL fileURLWithPath:pythonExe];
    appTask.arguments = @[mainPy];
    appTask.currentDirectoryURL = [NSURL fileURLWithPath:self.checkoutRoot];
    appTask.environment = MergedEnvironment(@{@"AUTOCLIP_MANAGED_LAUNCH": @"1"});
    [appTask launchAndReturnError:nil];
}

@end

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSApplication *app = [NSApplication sharedApplication];
        [app setActivationPolicy:NSApplicationActivationPolicyRegular];
        SetupWizardDelegate *delegate = [[SetupWizardDelegate alloc] init];
        app.delegate = delegate;
        [app run];
    }
    return 0;
}
