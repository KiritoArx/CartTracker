import os
import time
import cv2
import psutil
import ctypes
import numpy as np
import mss
import win32gui
import win32process
import win32con
import keyboard
import requests
import threading

# ======================
# CONFIGURATION
# ======================
PROCESS_NAME = "TopHeroes.exe"

CAPTURE_EVERY_SEC = 0.9
MATCH_THRESHOLD = 0.7

HOTKEY_STOP = "m"
HOTKEY_DEBUG_SCREENSHOT = "p"

# Load webhook from env for safety. Set DISCORD_WEBHOOK_URL in your system env if you want alerts.
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1417618033492623451/XRplkQ3uLiWXIBk25r1G5QjgF0FyCgTxV1GgZ0IzLckLcLhZdNCzTTrNHd1iziO53rPr"


TEMPLATES_DIR = "templates"
CARRIAGE_DETECTION_TEMPLATES = {
    "invite_label": os.path.join(TEMPLATES_DIR, "invite_label.png"),
    "carriage_icon": os.path.join(TEMPLATES_DIR, "carriage_icon.png"),
    "join_button": os.path.join(TEMPLATES_DIR, "join_button.png"),
}
CARRIAGE_RESET_TEMPLATES = {
    "joined_button": os.path.join(TEMPLATES_DIR, "joined_button.png"),
    "close_x": os.path.join(TEMPLATES_DIR, "close_x.png"),
}

# ======================
# GLOBALS
# ======================
running = True
last_screenshot = None
script_lock = threading.Lock()

# ======================
# UTIL
# ======================
def _pid_has_name(pid: int, name: str) -> bool:
    try:
        p = psutil.Process(pid)
        return p.name().lower() == name.lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False

def find_game_window(process_name: str):
    """Return (hwnd, rect) for the first visible, enabled top level window owned by process_name. Otherwise (None, None)."""
    target = {"hwnd": None, "rect": None}

    def callback(h, _):
        if not win32gui.IsWindowVisible(h) or not win32gui.IsWindowEnabled(h):
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(h)
            if pid and _pid_has_name(pid, process_name):
                rect = win32gui.GetWindowRect(h)
                target["hwnd"] = h
                target["rect"] = rect
                return False  # stop enum
        except Exception:
            pass
        return True

    win32gui.EnumWindows(callback, None)
    return target["hwnd"], target["rect"]

def focus_window(hwnd):
    """Try to restore and bring window to foreground."""
    try:
        # Restore if minimized
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        # Simple foreground attempt
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        # Foreground permission workaround
        try:
            user32 = ctypes.windll.user32
            cur_thread = win32process.GetCurrentThreadId()
            fg_hwnd = user32.GetForegroundWindow()
            fg_thread = user32.GetWindowThreadProcessId(fg_hwnd, 0)
            user32.AttachThreadInput(cur_thread, fg_thread, True)
            win32gui.SetForegroundWindow(hwnd)
            user32.AttachThreadInput(cur_thread, fg_thread, False)
        except Exception:
            pass

def rect_to_monitor(rect):
    left, top, right, bottom = rect
    return {"left": left, "top": top, "width": right - left, "height": bottom - top}

def load_templates(paths_dict):
    """Read templates once and cache grayscale versions. Return dict[name] = gray_image."""
    loaded = {}
    for name, path in paths_dict.items():
        img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if img is None:
            raise FileNotFoundError(f"Template not found: {path}")
        if img.shape[-1] == 4:
            gray = cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
        else:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        loaded[name] = gray
    return loaded

def find_template(screen_gray, template_gray, threshold):
    """Basic template match, returns True if any location meets threshold."""
    if screen_gray.shape[0] < template_gray.shape[0] or screen_gray.shape[1] < template_gray.shape[1]:
        return False
    result = cv2.matchTemplate(screen_gray, template_gray, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, _ = cv2.minMaxLoc(result)
    return max_val >= threshold

def send_discord_notification(message):
    if not DISCORD_WEBHOOK_URL:
        print("WARN Discord webhook URL not set. Skipping notification.")
        return
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
        if r.status_code in (200, 204):
            print("Discord notification sent.")
        else:
            print(f"Discord error {r.status_code} {r.text}")
    except requests.RequestException as e:
        print(f"Discord request failed {e}")

def stop_script():
    global running
    print(f"\nHotkey {HOTKEY_STOP} pressed. Stopping...")
    running = False

def save_debug_screenshot():
    with script_lock:
        if last_screenshot is None:
            print("\n[DEBUG] No screenshot yet.")
            return
        ts = time.strftime("%Y%m%d_%H%M%S")
        fn = f"debug_screenshot_{ts}.png"
        cv2.imwrite(fn, last_screenshot)
        print(f"\n[DEBUG] Saved {fn}")

def setup_hotkeys():
    keyboard.add_hotkey(HOTKEY_STOP, stop_script)
    keyboard.add_hotkey(HOTKEY_DEBUG_SCREENSHOT, save_debug_screenshot)
    keyboard.wait()

# ======================
# MAIN BOT
# ======================
def run_bot():
    global last_screenshot

    print("Loading templates...")
    try:
        detection_templates = load_templates(CARRIAGE_DETECTION_TEMPLATES)
        reset_templates = load_templates(CARRIAGE_RESET_TEMPLATES)
    except Exception as e:
        print(f"ERROR while loading templates: {e}")
        print("Make sure your images exist under the templates folder with the expected names.")
        return

    carriage_notified = False
    last_focus_hwnd = None
    last_focus_time = 0.0

    print("\n=== Carriage Finder Started ===")
    print(f"Press {HOTKEY_STOP} to stop. Press {HOTKEY_DEBUG_SCREENSHOT} to save a debug screenshot.")

    with mss.mss() as sct:
        while running:
            loop_start = time.perf_counter()
            try:
                hwnd, rect = find_game_window(PROCESS_NAME)
                if not hwnd or not rect:
                    print(f"\rWaiting for game window {PROCESS_NAME} ...", end="", flush=True)
                    time.sleep(2)
                    continue

                # Bring to front only when needed
                if win32gui.GetForegroundWindow() != hwnd:
                    now = time.perf_counter()
                    if last_focus_hwnd != hwnd or (now - last_focus_time) > 2.0:
                        focus_window(hwnd)
                        last_focus_hwnd = hwnd
                        last_focus_time = now

                monitor = rect_to_monitor(rect)
                screen_bgra = np.array(sct.grab(monitor))

                with script_lock:
                    last_screenshot = screen_bgra

                screen_gray = cv2.cvtColor(screen_bgra, cv2.COLOR_BGRA2GRAY)

                if not carriage_notified:
                    print("\rScanning for Mythic Carriage ...", end="", flush=True)
                    for name, tmpl in detection_templates.items():
                        if find_template(screen_gray, tmpl, MATCH_THRESHOLD):
                            print(f"\n*** Mythic Carriage DETECTED via {name} ***")
                            send_discord_notification("A Mythic Carriage has appeared! ✨🐴✨")
                            carriage_notified = True
                            break
                else:
                    print("\rCarriage detected. Waiting for event to end ...", end="", flush=True)
                    for name, tmpl in reset_templates.items():
                        if find_template(screen_gray, tmpl, MATCH_THRESHOLD):
                            print(f"\nCarriage event concluded because {name} matched. Resuming search.")
                            carriage_notified = False
                            break

                # Keep target interval
                elapsed = time.perf_counter() - loop_start
                sleep_for = max(0.0, CAPTURE_EVERY_SEC - elapsed)
                time.sleep(sleep_for)

            except Exception as e:
                print(f"\nUnexpected error: {e}")
                time.sleep(3)

    print("\nScript stopped. Goodbye.")

if __name__ == "__main__":
    # Start hotkeys listener as daemon
    threading.Thread(target=setup_hotkeys, daemon=True).start()
    run_bot()
