#import <Cocoa/Cocoa.h>
#import <CommonCrypto/CommonDigest.h>
#import <CoreText/CoreText.h>
#include <fcntl.h>
#include <signal.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

@interface SetupWizardDelegate : NSObject <NSApplicationDelegate>
@property (strong) NSWindow *window;
@property (strong) NSTextField *headingLabel;
@property (strong) NSTextField *statusLabel;
@property (strong) NSTextField *detailLabel;
@property (strong) NSTextField *metricsLabel;
@property (strong) NSProgressIndicator *progressBar;
@property (strong) NSButton *cloudOnlyCheck;
@property (strong) NSButton *detailsToggleButton;
@property (strong) NSButton *exportLogsButton;
@property (strong) NSButton *retryButton;
@property (strong) NSButton *cancelButton;
@property (strong) NSButton *launchButton;
@property (strong) NSScrollView *logScrollView;
@property (strong) NSTextView *logTextView;
@property (strong) NSTask *activeTask;
@property (copy) NSString *checkoutRoot;
@property (assign) BOOL cancelled;
@property (assign) BOOL detailsExpanded;
@property (assign) int lockFd;
@end

@implementation SetupWizardDelegate

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
    NSString *appSupport = [NSSearchPathForDirectoriesInDomains(NSApplicationSupportDirectory, NSUserDomainMask, YES) firstObject];
    NSString *dir = [appSupport stringByAppendingPathComponent:@"AutoClip Studio"];
    [[NSFileManager defaultManager] createDirectoryAtPath:dir withIntermediateDirectories:YES attributes:nil error:nil];
    return dir;
}

static NSString *LauncherLogPath(void) {
    NSString *logDir = [AppSupportDir() stringByAppendingPathComponent:@"logs"];
    [[NSFileManager defaultManager] createDirectoryAtPath:logDir withIntermediateDirectories:YES attributes:nil error:nil];
    return [logDir stringByAppendingPathComponent:@"launcher.log"];
}

static void AppendLauncherLog(NSString *line) {
    if (line.length == 0) return;
    NSString *logPath = LauncherLogPath();
    if (![[NSFileManager defaultManager] fileExistsAtPath:logPath]) {
        [[NSFileManager defaultManager] createFileAtPath:logPath contents:nil attributes:nil];
    }
    NSFileHandle *fh = [NSFileHandle fileHandleForWritingAtPath:logPath];
    if (!fh) return;
    [fh seekToEndOfFile];
    NSString *entry = [line hasSuffix:@"\n"] ? line : [line stringByAppendingString:@"\n"];
    [fh writeData:[entry dataUsingEncoding:NSUTF8StringEncoding]];
    [fh closeFile];
}

static void RegisterBundledPoppinsFonts(NSString *checkoutRoot) {
    NSString *fontsDir = [checkoutRoot stringByAppendingPathComponent:@"autoclip_studio/assets/fonts"];
    NSArray<NSString *> *files = [[NSFileManager defaultManager] contentsOfDirectoryAtPath:fontsDir error:nil];
    for (NSString *name in files) {
        if ([[name pathExtension].lowercaseString isEqualToString:@"ttf"]) {
            NSURL *url = [NSURL fileURLWithPath:[fontsDir stringByAppendingPathComponent:name]];
            CTFontManagerRegisterFontsForURL((__bridge CFURLRef)url, kCTFontManagerScopeProcess, NULL);
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

    NSString *venvPy = [self.checkoutRoot stringByAppendingPathComponent:@".venv/bin/python3"];
    NSString *setupPy = [self.checkoutRoot stringByAppendingPathComponent:@"scripts/setup.py"];
    NSString *mainPy = [self.checkoutRoot stringByAppendingPathComponent:@"main.py"];

    if ([[NSFileManager defaultManager] fileExistsAtPath:venvPy]) {
        NSString *checkOut = nil;
        int rc = RunCheckWithTimeout(venvPy, @[setupPy, @"--check"], self.checkoutRoot, 20.0, &checkOut);
        if (rc == 0) {
            [self launchMainApp:venvPy mainScript:mainPy];
            [NSApp terminate:nil];
            return;
        }
        AppendLauncherLog([NSString stringWithFormat:@"[launcher] startup validation exited %d:\n%@", rc, checkOut ?: @""]);
    } else {
        AppendLauncherLog(@"[launcher] .venv/bin/python3 missing; opening First-Time Setup.");
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
    [self startBootstrapAsync];
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

- (void)styleCheckbox:(NSButton *)btn title:(NSString *)title {
    NSDictionary *attrs = @{
        NSForegroundColorAttributeName: [NSColor colorWithRed:0.96 green:0.96 blue:0.97 alpha:1.0],
        NSFontAttributeName: AppFont(12.5, NO),
    };
    btn.attributedTitle = [[NSAttributedString alloc] initWithString:title attributes:attrs];
}

- (void)buildSetupWindow {
    NSRect frame = NSMakeRect(0, 0, 620, 320);
    self.window = [[NSWindow alloc] initWithContentRect:frame
                                              styleMask:(NSWindowStyleMaskTitled | NSWindowStyleMaskClosable)
                                                backing:NSBackingStoreBuffered
                                                  defer:NO];
    self.window.title = @"AutoClip Studio — First-Time Setup";
    // Force DarkAqua appearance so native checkboxes, buttons, and disabled states keep crisp contrast on dark surface.
    self.window.appearance = [NSAppearance appearanceNamed:NSAppearanceNameDarkAqua];
    self.window.backgroundColor = [NSColor colorWithRed:0.063 green:0.067 blue:0.086 alpha:1.0];
    [self.window center];

    NSView *content = self.window.contentView;
    content.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;

    self.headingLabel = [NSTextField labelWithString:@"AutoClip Studio Setup (Apple Silicon)"];
    self.headingLabel.frame = NSMakeRect(24, 266, 572, 28);
    self.headingLabel.font = AppFont(18, YES);
    self.headingLabel.textColor = [NSColor colorWithRed:0.96 green:0.96 blue:0.97 alpha:1.0];
    self.headingLabel.autoresizingMask = NSViewMinYMargin | NSViewWidthSizable;
    [content addSubview:self.headingLabel];

    self.statusLabel = [NSTextField labelWithString:@"Checking Python runtime and existing environment..."];
    self.statusLabel.frame = NSMakeRect(24, 232, 572, 22);
    self.statusLabel.font = AppFont(13, YES);
    self.statusLabel.textColor = [NSColor colorWithRed:0.96 green:0.96 blue:0.97 alpha:1.0];
    self.statusLabel.autoresizingMask = NSViewMinYMargin | NSViewWidthSizable;
    [content addSubview:self.statusLabel];

    self.progressBar = [[NSProgressIndicator alloc] initWithFrame:NSMakeRect(24, 200, 572, 18)];
    self.progressBar.style = NSProgressIndicatorStyleBar;
    self.progressBar.minValue = 0.0;
    self.progressBar.maxValue = 100.0;
    self.progressBar.indeterminate = YES;
    self.progressBar.autoresizingMask = NSViewMinYMargin | NSViewWidthSizable;
    [self.progressBar startAnimation:nil];
    [content addSubview:self.progressBar];

    self.detailLabel = [NSTextField labelWithString:@"Validating existing environment before installing missing dependencies..."];
    self.detailLabel.frame = NSMakeRect(24, 170, 572, 20);
    self.detailLabel.font = AppFont(12, NO);
    self.detailLabel.textColor = [NSColor colorWithRed:0.72 green:0.74 blue:0.79 alpha:1.0];
    self.detailLabel.autoresizingMask = NSViewMinYMargin | NSViewWidthSizable;
    [content addSubview:self.detailLabel];

    self.metricsLabel = [NSTextField labelWithString:@""];
    self.metricsLabel.frame = NSMakeRect(24, 146, 572, 18);
    self.metricsLabel.font = [NSFont monospacedSystemFontOfSize:11.5 weight:NSFontWeightRegular];
    self.metricsLabel.textColor = [NSColor colorWithRed:0.64 green:0.66 blue:0.71 alpha:1.0];
    self.metricsLabel.autoresizingMask = NSViewMinYMargin | NSViewWidthSizable;
    [content addSubview:self.metricsLabel];

    self.cloudOnlyCheck = [NSButton checkboxWithTitle:@"" target:nil action:nil];
    self.cloudOnlyCheck.frame = NSMakeRect(24, 112, 540, 22);
    self.cloudOnlyCheck.state = NSControlStateValueOn;
    self.cloudOnlyCheck.autoresizingMask = NSViewMinYMargin | NSViewWidthSizable;
    [self styleCheckbox:self.cloudOnlyCheck title:@"Skip optional local Whisper model download during setup (manage models in-app)"];
    [content addSubview:self.cloudOnlyCheck];

    self.detailsToggleButton = [NSButton buttonWithTitle:@"Show Diagnostics" target:self action:@selector(toggleDetailsClicked:)];
    self.detailsToggleButton.frame = NSMakeRect(24, 70, 140, 28);
    self.detailsToggleButton.bezelStyle = NSBezelStyleRounded;
    self.detailsToggleButton.autoresizingMask = NSViewMinYMargin;
    [content addSubview:self.detailsToggleButton];

    self.exportLogsButton = [NSButton buttonWithTitle:@"Copy Logs" target:self action:@selector(copyLogsClicked:)];
    self.exportLogsButton.frame = NSMakeRect(172, 70, 102, 28);
    self.exportLogsButton.bezelStyle = NSBezelStyleRounded;
    self.exportLogsButton.autoresizingMask = NSViewMinYMargin;
    [content addSubview:self.exportLogsButton];

    self.logScrollView = [[NSScrollView alloc] initWithFrame:NSMakeRect(24, 64, 572, 160)];
    self.logScrollView.hasVerticalScroller = YES;
    self.logScrollView.borderType = NSBezelBorder;
    self.logScrollView.hidden = YES;
    self.logScrollView.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
    self.logTextView = [[NSTextView alloc] initWithFrame:NSMakeRect(0, 0, 572, 160)];
    self.logTextView.editable = NO;
    self.logTextView.font = [NSFont monospacedSystemFontOfSize:11 weight:NSFontWeightRegular];
    self.logTextView.backgroundColor = [NSColor colorWithRed:0.09 green:0.10 blue:0.13 alpha:1.0];
    self.logTextView.textColor = [NSColor colorWithRed:0.86 green:0.88 blue:0.92 alpha:1.0];
    self.logScrollView.documentView = self.logTextView;
    [content addSubview:self.logScrollView];

    self.retryButton = [NSButton buttonWithTitle:@"Retry" target:self action:@selector(retryClicked:)];
    self.retryButton.frame = NSMakeRect(248, 20, 96, 32);
    self.retryButton.bezelStyle = NSBezelStyleRounded;
    self.retryButton.hidden = YES;
    self.retryButton.autoresizingMask = NSViewMaxYMargin;
    [content addSubview:self.retryButton];

    self.cancelButton = [NSButton buttonWithTitle:@"Cancel" target:self action:@selector(cancelClicked:)];
    self.cancelButton.frame = NSMakeRect(352, 20, 96, 32);
    self.cancelButton.bezelStyle = NSBezelStyleRounded;
    self.cancelButton.autoresizingMask = NSViewMaxYMargin;
    [content addSubview:self.cancelButton];

    self.launchButton = [NSButton buttonWithTitle:@"Launch App" target:self action:@selector(launchClicked:)];
    self.launchButton.frame = NSMakeRect(456, 20, 140, 32);
    self.launchButton.bezelStyle = NSBezelStyleRounded;
    self.launchButton.bezelColor = [NSColor colorWithRed:0.898 green:0.224 blue:0.208 alpha:1.0];
    self.launchButton.enabled = NO;
    self.launchButton.autoresizingMask = NSViewMaxYMargin;
    [content addSubview:self.launchButton];

    [self.window makeKeyAndOrderFront:nil];
    [NSApp activateIgnoringOtherApps:YES];
}

- (void)appendDiagnosticLine:(NSString *)line {
    if (line.length == 0) return;
    AppendLauncherLog(line);
    dispatch_async(dispatch_get_main_queue(), ^{
        NSString *entry = [line hasSuffix:@"\n"] ? line : [line stringByAppendingString:@"\n"];
        NSAttributedString *as = [[NSAttributedString alloc] initWithString:entry attributes:@{
            NSForegroundColorAttributeName: [NSColor colorWithRed:0.86 green:0.88 blue:0.92 alpha:1.0],
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
        self.logScrollView.frame = NSMakeRect(24, 62, 572, 168);
    }
    self.detailsToggleButton.title = self.detailsExpanded ? @"Hide Diagnostics" : @"Show Diagnostics";
}

- (void)copyLogsClicked:(id)sender {
    NSString *text = self.logTextView.string.length > 0 ? self.logTextView.string : @"No diagnostic logs yet.";
    NSPasteboard *pb = [NSPasteboard generalPasteboard];
    [pb clearContents];
    [pb setString:text forType:NSPasteboardTypeString];
}

- (void)retryClicked:(id)sender {
    self.cancelled = NO;
    self.retryButton.hidden = YES;
    self.launchButton.enabled = NO;
    self.progressBar.indeterminate = YES;
    [self.progressBar startAnimation:nil];
    [self startBootstrapAsync];
}

- (void)cancelClicked:(id)sender {
    self.cancelled = YES;
    [self terminateActiveTask];
    [self releaseSetupLock];
    [NSApp terminate:nil];
}

- (void)launchClicked:(id)sender {
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
        if ([obj isKindOfClass:[NSDictionary class]] && [obj[@"event"] isEqualToString:@"progress"]) {
            NSString *compId = obj[@"id"] ?: @"";
            NSString *state = obj[@"state"] ?: @"";
            NSString *detail = obj[@"detail"] ?: @"";
            NSNumber *pct = obj[@"percent"];
            NSNumber *downloaded = obj[@"downloaded"];
            NSNumber *total = obj[@"total"];
            NSNumber *speed = obj[@"speed_bps"];
            NSNumber *eta = obj[@"eta_seconds"];

            [self appendDiagnosticLine:[NSString stringWithFormat:@"[%@] %@%@", compId, state, detail.length ? [@": " stringByAppendingString:detail] : @""]];

            dispatch_async(dispatch_get_main_queue(), ^{
                if (detail.length > 0) {
                    self.statusLabel.stringValue = detail;
                } else if (state.length > 0) {
                    self.statusLabel.stringValue = [NSString stringWithFormat:@"%@ (%@)...", state, compId];
                }
                if (pct && ![pct isKindOfClass:[NSNull class]]) {
                    [self.progressBar stopAnimation:nil];
                    self.progressBar.indeterminate = NO;
                    self.progressBar.doubleValue = [pct doubleValue];
                }
                if (downloaded && total && ![downloaded isKindOfClass:[NSNull class]] && ![total isKindOfClass:[NSNull class]] && [total doubleValue] > 0) {
                    double d = [downloaded doubleValue];
                    double t = [total doubleValue];
                    double sp = (speed && ![speed isKindOfClass:[NSNull class]]) ? [speed doubleValue] : 0.0;
                    double etaSec = (eta && ![eta isKindOfClass:[NSNull class]]) ? [eta doubleValue] : -1.0;
                    NSString *etaStr = etaSec >= 0 ? [NSString stringWithFormat:@" • ETA %.0fs", etaSec] : @"";
                    NSString *spStr = sp > 0 ? [NSString stringWithFormat:@" • %@/s", FormatBytes(sp)] : @"";
                    self.metricsLabel.stringValue = [NSString stringWithFormat:@"%@ / %@ (%.0f%%)%@%@",
                        FormatBytes(d), FormatBytes(t), (d / t) * 100.0, spStr, etaStr];
                } else if (pct && ![pct isKindOfClass:[NSNull class]]) {
                    self.metricsLabel.stringValue = [NSString stringWithFormat:@"Progress: %.0f%%", [pct doubleValue]];
                }
            });
            return;
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

- (void)startBootstrapAsync {
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

        // Prefer existing .venv Python directly for selective repair so we don't needlessly download uv if already healthy.
        int rc = -1;
        NSString *modeFlag = cloudOnly ? @"--cloud-only" : @"--local-whisper";
        if ([[NSFileManager defaultManager] fileExistsAtPath:venvPy]) {
            dispatch_async(dispatch_get_main_queue(), ^{
                self.statusLabel.stringValue = @"Validating existing environment...";
                self.detailLabel.stringValue = @"Repairing only missing or incompatible required components.";
            });
            rc = [self runStreamingProcess:venvPy args:@[setupPy, @"--json-progress", @"--repair-only", @"--no-tests", modeFlag] extraEnv:uvEnv];
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
                        self.statusLabel.stringValue = @"Downloading native ARM64 uv bootstrapper...";
                        self.detailLabel.stringValue = urlStr;
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
                rc = [self runStreamingProcess:uvPath args:@[@"run", @"--no-project", @"--managed-python", @"--python", @"3.12", @"--", @"python3", setupPy, @"--json-progress", @"--repair-only", @"--no-tests", modeFlag] extraEnv:uvEnv];
            }
        }

        if (self.cancelled) return;
        dispatch_async(dispatch_get_main_queue(), ^{
            [self.progressBar stopAnimation:nil];
            self.progressBar.indeterminate = NO;
            if (rc == 0) {
                self.progressBar.doubleValue = 100.0;
                self.statusLabel.stringValue = @"All required dependencies verified.";
                self.detailLabel.stringValue = @"Launching AutoClip Studio...";
                self.metricsLabel.stringValue = @"100%";
                self.launchButton.enabled = YES;
                [self launchClicked:nil];
            } else {
                self.statusLabel.stringValue = @"Setup validation failed. Review diagnostics or click Retry.";
                self.detailLabel.stringValue = [NSString stringWithFormat:@"Log file: %@", LauncherLogPath()];
                self.retryButton.hidden = NO;
                if (!self.detailsExpanded) {
                    [self toggleDetailsClicked:nil];
                }
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
