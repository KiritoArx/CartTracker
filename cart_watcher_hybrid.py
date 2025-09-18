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
from collections import deque

# ======================
# CONFIGURATION
# ======================
PROCESS_NAME = "TopHeroes.exe"

CAPTURE_EVERY_SEC = 0.9
MATCH_THRESHOLD = 0.7

HOTKEY_STOP = "m"
HOTKEY_DEBUG_SCREENSHOT = "p"

# Prefer env var, else fallback (rotate if shared)
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
NO_ALERT_TEMPLATES = {
    "no_alert": os.path.join(TEMPLATES_DIR, "NoAlert.png"),
}

# ---- New: multi-signal rule & stability ----
REQUIRED_DETECTION_KEYS = ["invite_label", "carriage_icon", "join_button"]  # which templates count toward the vote
REQUIRED_DETECTION_MIN = 2       # K: need at least 2 of the above in the same frame
PERSIST_FRAMES = 2               # P: rule must hold for this many consecutive frames

SUPPRESS_COOLDOWN_SEC = 8.0      # when NoAlert is seen

# ======================
# GLOBALS
# ======================
running = True
last_screenshot = None
script_lock = threading.Lock()

# Track detection stability
rule_pass_history = deque(maxlen=PERSIST_FRAMES)
carriage_notified = False

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
    target = {"hwnd": None, "rect": None}

    def callback(h, _):
        if not win32gui.IsWindowVisible(h) or not win32gui.IsWindowEnabled(h):
            return True
        try:
            _tid, pid = win32process.GetWindowThreadProcessId(h)
            if pid and _pid_has_name(pid, process_name):
                rect = win32gui.GetWindowRect(h)
                target["hwnd"] = h
                target["rect"] = rect
                return False
        except Exception:
            return True
        return True

    try:
        win32gui.EnumWindows(callback, None)
    except Exception:
        return None, None

    return target["hwnd"], target["rect"]

def focus_window(hwnd):
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
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
    l, t, r, b = rect
    return {"left": l, "top": t, "width": r - l, "height": b - t}

def load_templates(paths_dict):
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

def match_score(screen_gray, template_gray):
    if screen_gray.shape[0] < template_gray.shape[0] or screen_gray.shape[1] < template_gray.shape[1]:
        return 0.0
    res = cv2.matchTemplate(screen_gray, template_gray, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, _ = cv2.minMaxLoc(res)
    return float(max_val)

def find_hits(screen_gray, template_bank, keys=None, threshold=MATCH_THRESHOLD):
    """Return dict {name:score} for all templates (or subset 'keys') that meet threshold."""
    hits = {}
    items = template_bank.items() if keys is None else [(k, template_bank[k]) for k in keys if k in template_bank]
    for name, tmpl in items:
        s = match_score(screen_gray, tmpl)
        if s >= threshold:
            hits[name] = s
    return hits

def _mask_webhook(url: str) -> str:
    if not url:
        return "(unset)"
    return url[:35] + "..." + url[-6:]

def check_webhook():
    if not DISCORD_WEBHOOK_URL or "REDACTED" in DISCORD_WEBHOOK_URL:
        print("WARN Discord webhook URL not set. Set env var DISCORD_WEBHOOK_URL or hardcode it.")
    else:
        print(f"Discord webhook configured: {_mask_webhook(DISCORD_WEBHOOK_URL)}")

def send_discord_notification(message):
    if not DISCORD_WEBHOOK_URL or "REDACTED" in DISCORD_WEBHOOK_URL:
        print("WARN Discord webhook URL not set. Skipping notification.")
        return
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
        if r.status_code in (200, 204):
            print("Discord notification sent.")
        elif r.status_code == 429:
            retry_after = float(r.headers.get("Retry-After", "1"))
            print(f"Discord rate limited. Retrying after {retry_after:.1f}s...")
            time.sleep(retry_after)
            r2 = requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
            if r2.status_code in (200, 204):
                print("Discord notification sent (after retry).")
            else:
                print(f"Discord error after retry {r2.status_code} {r2.text}")
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
    global last_screenshot, carriage_notified

    print("Loading templates...")
    try:
        detection_templates = load_templates(CARRIAGE_DETECTION_TEMPLATES)
        reset_templates = load_templates(CARRIAGE_RESET_TEMPLATES)
        suppress_templates = load_templates(NO_ALERT_TEMPLATES)
    except Exception as e:
        print(f"ERROR while loading templates: {e}")
        print("Make sure your images exist under the templates folder with the expected names.")
        return

    check_webhook()

    last_focus_hwnd = None
    last_focus_time = 0.0
    suppress_cooldown_until = 0.0

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

                # 1) Suppressor check
                if time.time() < suppress_cooldown_until:
                    print("\rSuppress cooldown active ...", end="", flush=True)
                else:
                    no_alert_hits = find_hits(screen_gray, suppress_templates)
                    if no_alert_hits:
                        print(f"\n[NoAlert] Found {', '.join(no_alert_hits.keys())}. Too late to join. Suppressing for {SUPPRESS_COOLDOWN_SEC}s.")
                        carriage_notified = False
                        rule_pass_history.clear()
                        suppress_cooldown_until = time.time() + SUPPRESS_COOLDOWN_SEC
                        # fall through to sleep at end

                # 2) Detection logic with multi-signal + stability
                if time.time() >= suppress_cooldown_until:
                    if not carriage_notified:
                        print("\rScanning for Mythic Carriage ...", end="", flush=True)

                        # vote among REQUIRED_DETECTION_KEYS
                        hits = find_hits(screen_gray, detection_templates, keys=REQUIRED_DETECTION_KEYS)
                        rule_pass = (len(hits) >= REQUIRED_DETECTION_MIN)

                        # remember recent rule outcomes
                        rule_pass_history.append(rule_pass)
                        stable = len(rule_pass_history) == PERSIST_FRAMES and all(rule_pass_history)

                        if stable:
                            # Optionally, check reset templates are NOT present at the same time
                            reset_hits = find_hits(screen_gray, reset_templates)
                            if reset_hits:
                                # If a reset UI is visible already, don't notify
                                print(f"\n[Info] Reset-state visible ({', '.join(reset_hits.keys())}), skipping alert.")
                                rule_pass_history.clear()
                            else:
                                # Fire alert once
                                names = ", ".join(sorted(hits.keys()))
                                print(f"\n*** Mythic Carriage DETECTED via {names} (stable {PERSIST_FRAMES} frames) ***")
                                send_discord_notification("A Mythic Carriage has appeared! ✨🐴✨")
                                carriage_notified = True
                                rule_pass_history.clear()
                    else:
                        print("\rCarriage detected. Waiting for event to end ...", end="", flush=True)
                        # look for reset cues to end event
                        reset_hits = find_hits(screen_gray, reset_templates)
                        if reset_hits:
                            print(f"\nCarriage event concluded because {', '.join(reset_hits.keys())} matched. Resuming search.")
                            carriage_notified = False
                            rule_pass_history.clear()

                # target rate
                elapsed = time.perf_counter() - loop_start
                time.sleep(max(0.0, CAPTURE_EVERY_SEC - elapsed))

            except Exception as e:
                print(f"\nUnexpected error: {e}")
                time.sleep(3)

    print("\nScript stopped. Goodbye.")

if __name__ == "__main__":
    threading.Thread(target=setup_hotkeys, daemon=True).start()
    run_bot()
