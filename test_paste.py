import subprocess
import time

def test_paste_cycle():
    print("Testing clipboard save -> paste -> restore cycle...")
    
    # 1. Check current clipboard
    initial_clip = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout
    print(f"1. Current clipboard: {initial_clip[:40]!r}... (len {len(initial_clip)})")
    
    test_text = "✨ [MiniFlow Test Paste] ✨"
    
    # 2. Put test text on clipboard
    p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE, text=True)
    p.communicate(test_text)
    
    # 3. Check clipboard has test text
    mid_clip = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout
    assert mid_clip == test_text, "Failed to copy test text!"
    print(f"2. Temporary clipboard updated to: {mid_clip!r}")
    
    # 4. Simulate paste
    print("3. Simulating Cmd+V keystroke...")
    script = '''
    tell application "System Events"
        keystroke "v" using {command down}
    end tell
    '''
    subprocess.run(["osascript", "-e", script])
    time.sleep(0.15)
    
    # 5. Restore initial clipboard
    p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE, text=True)
    p.communicate(initial_clip)
    
    restored_clip = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout
    assert restored_clip == initial_clip, "Failed to restore initial clipboard!"
    print(f"4. Original clipboard successfully restored!")
    print("All tests passed!")

if __name__ == "__main__":
    test_paste_cycle()
