#import <Cocoa/Cocoa.h>
#import <CommonCrypto/CommonDigest.h>
#include <spawn.h>
#include <sys/stat.h>

extern char **environ;

@interface SetupWizardDelegate : NSObject <NSApplicationDelegate>
@property (strong) NSWindow *window;
@property (strong) NSTextField *statusLabel;
@property (strong) NSTextField *detailLabel;
@property (strong) NSProgressIndicator *progressBar;
@property (strong) NSButton *cloudOnlyCheck;
@property (strong) NSButton *cancelButton;
@property (strong) NSButton *launchButton;
@property (copy) NSString *checkoutRoot;
@property (assign) BOOL cancelled;
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

static int RunSyncProcess(NSString *exe, NSArray<NSString *> *args, NSString *cwd, NSDictionary<NSString *, NSString *> *extraEnv) {
    NSTask *task = [[NSTask alloc] init];
    task.executableURL = [NSURL fileURLWithPath:exe];
    task.arguments = args;
    task.currentDirectoryURL = [NSURL fileURLWithPath:cwd];
    NSMutableDictionary *env = [NSMutableDictionary dictionaryWithDictionary:[[NSProcessInfo processInfo] environment]];
    if (extraEnv) [env addEntriesFromDictionary:extraEnv];
    task.environment = env;
    task.standardOutput = [NSFileHandle fileHandleWithNullDevice];
    task.standardError = [NSFileHandle fileHandleWithNullDevice];
    NSError *err = nil;
    if (![task launchAndReturnError:&err]) return -1;
    [task waitUntilExit];
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

- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    self.checkoutRoot = ResolveCheckoutRoot();
    NSString *venvPy = [self.checkoutRoot stringByAppendingPathComponent:@".venv/bin/python3"];
    NSString *setupPy = [self.checkoutRoot stringByAppendingPathComponent:@"scripts/setup.py"];
    NSString *mainPy = [self.checkoutRoot stringByAppendingPathComponent:@"main.py"];

    if ([[NSFileManager defaultManager] fileExistsAtPath:venvPy] &&
        RunSyncProcess(venvPy, @[setupPy, @"--check"], self.checkoutRoot, nil) == 0) {
        [self launchMainApp:venvPy mainScript:mainPy];
        [NSApp terminate:nil];
        return;
    }

    [self buildSetupWindow];
    [self startBootstrapAsync];
}

- (void)buildSetupWindow {
    NSRect frame = NSMakeRect(0, 0, 560, 290);
    self.window = [[NSWindow alloc] initWithContentRect:frame
                                              styleMask:(NSWindowStyleMaskTitled | NSWindowStyleMaskClosable)
                                                backing:NSBackingStoreBuffered
                                                  defer:NO];
    self.window.title = @"AutoClip Studio — First-Time Setup";
    self.window.backgroundColor = [NSColor colorWithRed:0.063 green:0.067 blue:0.086 alpha:1.0];
    [self.window center];

    NSView *content = self.window.contentView;
    NSTextField *heading = [NSTextField labelWithString:@"AutoClip Studio Setup (Apple Silicon)"];
    heading.frame = NSMakeRect(24, 236, 512, 26);
    heading.font = [NSFont boldSystemFontOfSize:17];
    heading.textColor = [NSColor colorWithRed:0.96 green:0.96 blue:0.97 alpha:1.0];
    [content addSubview:heading];

    self.statusLabel = [NSTextField labelWithString:@"Checking Apple Silicon runtime and media tools..."];
    self.statusLabel.frame = NSMakeRect(24, 198, 512, 22);
    self.statusLabel.textColor = [NSColor colorWithRed:0.96 green:0.96 blue:0.97 alpha:1.0];
    [content addSubview:self.statusLabel];

    self.progressBar = [[NSProgressIndicator alloc] initWithFrame:NSMakeRect(24, 166, 512, 18)];
    self.progressBar.indeterminate = YES;
    [self.progressBar startAnimation:nil];
    [content addSubview:self.progressBar];

    self.detailLabel = [NSTextField labelWithString:@"Installing native ARM64 Python, PySide6, FFmpeg, and MLX Whisper..."];
    self.detailLabel.frame = NSMakeRect(24, 134, 512, 20);
    self.detailLabel.textColor = [NSColor colorWithRed:0.64 green:0.66 blue:0.71 alpha:1.0];
    [content addSubview:self.detailLabel];

    self.cloudOnlyCheck = [NSButton checkboxWithTitle:@"Cloud-only transcription (skip local Whisper model)" target:nil action:nil];
    self.cloudOnlyCheck.frame = NSMakeRect(24, 96, 460, 22);
    [content addSubview:self.cloudOnlyCheck];

    self.cancelButton = [NSButton buttonWithTitle:@"Cancel" target:self action:@selector(cancelClicked:)];
    self.cancelButton.frame = NSMakeRect(340, 22, 92, 32);
    [content addSubview:self.cancelButton];

    self.launchButton = [NSButton buttonWithTitle:@"Launch Application" target:self action:@selector(launchClicked:)];
    self.launchButton.frame = NSMakeRect(442, 22, 136, 32);
    self.launchButton.bezelColor = [NSColor colorWithRed:0.898 green:0.224 blue:0.208 alpha:1.0];
    self.launchButton.enabled = NO;
    [content addSubview:self.launchButton];

    [self.window makeKeyAndOrderFront:nil];
    [NSApp activateIgnoringOtherApps:YES];
}

- (void)cancelClicked:(id)sender {
    self.cancelled = YES;
    [NSApp terminate:nil];
}

- (void)launchClicked:(id)sender {
    NSString *venvPy = [self.checkoutRoot stringByAppendingPathComponent:@".venv/bin/python3"];
    NSString *mainPy = [self.checkoutRoot stringByAppendingPathComponent:@"main.py"];
    [self launchMainApp:venvPy mainScript:mainPy];
    [NSApp terminate:nil];
}

- (void)startBootstrapAsync {
    BOOL cloudOnly = (self.cloudOnlyCheck.state == NSControlStateValueOn);
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
        NSString *toolsDir = [self.checkoutRoot stringByAppendingPathComponent:@"tools"];
        NSString *uvPath = [toolsDir stringByAppendingPathComponent:@"uv"];
        NSString *setupPy = [self.checkoutRoot stringByAppendingPathComponent:@"scripts/setup.py"];
        [[NSFileManager defaultManager] createDirectoryAtPath:toolsDir withIntermediateDirectories:YES attributes:nil error:nil];

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
                });
                NSString *tarPath = [toolsDir stringByAppendingPathComponent:@"uv-arm64.tar.gz"];
                NSData *dl = [NSData dataWithContentsOfURL:[NSURL URLWithString:urlStr]];
                [dl writeToFile:tarPath atomically:YES];
                if (sha.length == 0 || [[Sha256File(tarPath) lowercaseString] isEqualToString:[sha lowercaseString]]) {
                    RunSyncProcess(@"/usr/bin/tar", @[@"-xzf", tarPath, @"-C", toolsDir, @"--strip-components=1"], self.checkoutRoot, nil);
                }
                [[NSFileManager defaultManager] removeItemAtPath:tarPath error:nil];
            }
        }

        NSDictionary *uvEnv = @{
            @"UV_PYTHON_INSTALL_DIR": [self.checkoutRoot stringByAppendingPathComponent:@".runtime"],
            @"UV_CACHE_DIR": [self.checkoutRoot stringByAppendingPathComponent:@".uv-cache"],
            @"PYTHONIOENCODING": @"utf-8"
        };
        NSString *modeFlag = cloudOnly ? @"--cloud-only" : @"--local-whisper";
        int rc = RunSyncProcess(uvPath, @[@"run", @"--no-project", @"--managed-python", @"--python", @"3.12", @"--", @"python3", setupPy, @"--no-tests", modeFlag], self.checkoutRoot, uvEnv);
        dispatch_async(dispatch_get_main_queue(), ^{
            [self.progressBar stopAnimation:nil];
            self.progressBar.indeterminate = NO;
            if (rc == 0) {
                self.progressBar.doubleValue = 100.0;
                self.statusLabel.stringValue = @"All dependencies verified.";
                self.launchButton.enabled = YES;
                [self launchClicked:nil];
            } else {
                self.statusLabel.stringValue = @"Setup failed. Check logs in ~/Library/Application Support/AutoClip Studio/logs.";
            }
        });
    });
}

- (void)launchMainApp:(NSString *)pythonExe mainScript:(NSString *)mainPy {
    NSTask *appTask = [[NSTask alloc] init];
    appTask.executableURL = [NSURL fileURLWithPath:pythonExe];
    appTask.arguments = @[mainPy];
    appTask.currentDirectoryURL = [NSURL fileURLWithPath:self.checkoutRoot];
    NSMutableDictionary *env = [NSMutableDictionary dictionaryWithDictionary:[[NSProcessInfo processInfo] environment]];
    env[@"AUTOCLIP_MANAGED_LAUNCH"] = @"1";
    appTask.environment = env;
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
