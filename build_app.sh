#!/bin/bash
set -e

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_BUNDLE="$APP_DIR/WFlow.app"

echo "🔨 Building WFlow.app bundle..."

# Clean old bundle if exists
rm -rf "$APP_BUNDLE"

# Create directories
mkdir -p "$APP_BUNDLE/Contents/MacOS"
mkdir -p "$APP_BUNDLE/Contents/Resources"

# Copy Icon
if [ -f "$APP_DIR/AppIcon.icns" ]; then
    cp "$APP_DIR/AppIcon.icns" "$APP_BUNDLE/Contents/Resources/AppIcon.icns"
fi

# Create Info.plist
cat << 'EOF' > "$APP_BUNDLE/Contents/Info.plist"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>WFlow</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundleIdentifier</key>
    <string>com.anshu.wflow</string>
    <key>CFBundleName</key>
    <string>WFlow</string>
    <key>CFBundleDisplayName</key>
    <string>WFlow</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0.0</string>
    <key>CFBundleVersion</key>
    <string>1.0.0</string>
    <key>LSMinimumSystemVersion</key>
    <string>11.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSMicrophoneUsageDescription</key>
    <string>WFlow requires microphone access to record audio for speech-to-text dictation.</string>
    <key>NSAppleEventsUsageDescription</key>
    <string>WFlow requires accessibility permissions to simulate Cmd+V paste into your active application.</string>
</dict>
</plist>
EOF

# Compile Native Mach-O C Launcher
cat << 'EOF' > /tmp/wflow_launcher.c
#include <unistd.h>
#include <stdlib.h>
#include <stdio.h>
#include <limits.h>
#include <string.h>
#include <fcntl.h>
#include <mach-o/dyld.h>

int main(int argc, char *argv[]) {
    char exe_path[PATH_MAX];
    uint32_t size = sizeof(exe_path);
    if (_NSGetExecutablePath(exe_path, &size) != 0) {
        return 1;
    }
    
    char real_exe[PATH_MAX];
    if (realpath(exe_path, real_exe) == NULL) {
        strncpy(real_exe, exe_path, sizeof(real_exe));
    }
    
    char *p = strstr(real_exe, "/Contents/MacOS");
    if (p) {
        *p = '\0';
    }
    char *last_slash = strrchr(real_exe, '/');
    char base_dir[PATH_MAX];
    if (last_slash) {
        *last_slash = '\0';
        strncpy(base_dir, real_exe, sizeof(base_dir));
    } else {
        snprintf(base_dir, sizeof(base_dir), "%s/Documents/WFlow", getenv("HOME"));
    }
    
    char test_file[PATH_MAX];
    snprintf(test_file, sizeof(test_file), "%s/app_gui.py", base_dir);
    if (access(test_file, F_OK) != 0) {
        snprintf(base_dir, sizeof(base_dir), "%s/Documents/WFlow", getenv("HOME"));
    }
    
    chdir(base_dir);
    
    const char *home = getenv("HOME");
    if (home) {
        char log_path[PATH_MAX];
        snprintf(log_path, sizeof(log_path), "%s/Library/Logs/WFlow.log", home);
        int fd = open(log_path, O_WRONLY | O_CREAT | O_APPEND, 0644);
        if (fd >= 0) {
            dup2(fd, STDOUT_FILENO);
            dup2(fd, STDERR_FILENO);
            close(fd);
        }
    }
    
    char python_path[PATH_MAX];
    snprintf(python_path, sizeof(python_path), "%s/.venv/bin/python", base_dir);
    
    char script_path[PATH_MAX];
    snprintf(script_path, sizeof(script_path), "%s/app_gui.py", base_dir);
    
    char *args[] = {python_path, script_path, NULL};
    execv(python_path, args);
    return 1;
}
EOF

clang -O2 /tmp/wflow_launcher.c -o "$APP_BUNDLE/Contents/MacOS/WFlow"
chmod +x "$APP_BUNDLE/Contents/MacOS/WFlow"
rm -f /tmp/wflow_launcher.c

echo "✅ WFlow.app created successfully at: $APP_BUNDLE"
