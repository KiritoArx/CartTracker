import time, sys, subprocess
from pathlib import Path
from collections import deque

import numpy as np
import cv2
import mss
import requests
import psutil

# Windows window focus helpers
try:
    import win32gui, win32con, win32process
    WIN32_OK = True
except Exception:
    WIN32_OK = False

try:
    import keyboard
    KEYBOARD_OK = True
except Exception:
    KEYBOARD_OK = False

# --------------- CONFIG ---------------
PROCESS_NAME = "TopHeroes.exe"
USE_CLIENT_AREA = True             # kept for familiarity (not used here)
CAPTURE_EVERY_SEC = 0.9
REFRESH_RECT_EVERY_SEC = 2         # (ignored)
HOTKEY_STOP = "m"

# Bring game to front on start
BRING_TO_FRONT = True

# Optional: auto-launch if not running (set path correctly then flip to True)
AUTO_LAUNCH = False
GAME_EXE_PATH = r"C:\Program Files\TopHeroes\TopHeroes.exe"

# Your absolute templates folder
TEMPLATES_DIR = Path(r"C:\Users\crumb\Desktop\CartTracker\templates")
FILES = {
    "invite_label":  "invite_label.png",
    "carriage_icon": "carriage_icon.png",
    "join_button":   "join_button.png",
    "joined_button": "joined_button.png",
    "close_x":       "close_x.png",   # not used for alert
}

DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1417618033492623451/XRplkQ3uLiWXIBk25r1G5QjgF0FyCgTxV1GgZ0IzLckLcLhZdNCzTTrNHd1iziO53rPr"

# thresholds (slightly easier)
TH_MAIN   = 0.80   # invite_label / carriage_icon / join_button
TH_JOINED = 0.86   # joined_button

# wider multi-scale to handle DPI/zoom/anti-aliasing
SCALES = np.linspace(0.70, 1.40, 15)

# anti-spam
ALERT_COOLDOWN_SEC = 18

# one-line heartbeat so you know it’s scanning
HEARTBEAT_EVERY_N_FRAMES = 5

# print scores on heartbeat to help tuning
DEBUG_SCORES = True
# --------------------------------------


def send_webhook(msg):
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": msg}, timeout=5)
    except Exception:
        pass


def is_running(name: str) -> bool:
    name = name.lower()
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info["name"] or "").lower() == name:
                return True
        except psutil.Error:
            continue
    return False


def maybe_launch_game():
    if not AUTO_LAUNCH:
        return
    if not is_running(PROCESS_NAME) and GAME_EXE_PATH and Path(GAME_EXE_PATH).exists():
        try:
            subprocess.Popen([GAME_EXE_PATH], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def enum_hwnds_for_pid(pid):
    hwnds = []
    def cb(hwnd, extra):
        try:
            tid, hwnd_pid = win32process.GetWindowThreadProcessId(hwnd)
            if hwnd_pid == pid and win32gui.IsWindowVisible(hwnd):
                hwnds.append(hwnd)
        except Exception:
            pass
        return True
    win32gui.EnumWindows(cb, None)
    return hwnds


def focus_topheroes_window():
    if not WIN32_OK:
        return False
    # find process
    target_pid = None
    for p in psutil.process_iter(["name", "pid"]):
        if (p.info["name"] or "").lower() == PROCESS_NAME.lower():
            target_pid = p.info["pid"]
            break
    if not target_pid:
        return False

    hwnds = enum_hwnds_for_pid(target_pid)
    if not hwnds:
        return False

    # pick the first visible window with a non-empty title if possible
    hwnd = None
    for h in hwnds:
        title = win32gui.GetWindowText(h)
        if title:
            hwnd = h
            break
    if hwnd is None:
        hwnd = hwnds[0]

    try:
        # restore if minimized, bring to foreground
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        # Topmost trick to force focus
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
        win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
        win32gui.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def load_templates():
    bank = {}
    for key, fname in FILES.items():
        p = TEMPLATES_DIR / fname
        img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise FileNotFoundError(f"Template missing or unreadable: {p}")
        bank[key] = img
    return bank


def best_match_multi_scale(haystack_gray, needle_gray, method=cv2.TM_CCOEFF_NORMED):
    best_val, best_tl, best_br, best_s = -1.0, None, None, None
    hN, wN = needle_gray.shape[:2]
    for s in SCALES:
        w = max(8, int(wN * s)); h = max(8, int(hN * s))
        tpl = cv2.resize(needle_gray, (w, h), interpolation=cv2.INTER_AREA)
        if haystack_gray.shape[0] < h or haystack_gray.shape[1] < w:
            continue
        res = cv2.matchTemplate(haystack_gray, tpl, method)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if max_val > best_val:
            best_val = max_val
            best_tl = max_loc
            best_br = (max_loc[0] + w, max_loc[1] + h)
            best_s = s
    return best_val, best_tl, best_br, best_s


def main():
    # Auto-launch if you want
    maybe_launch_game()

    # Bring window to front so the chat is definitely visible
    if BRING_TO_FRONT:
        focus_topheroes_window()

    bank = load_templates()

    # Capture ALL monitors so placement never matters
    sct = mss.mss()
    monitor = sct.monitors[0]  # 0 == virtual screen (all displays)

    # hotkey
    stop_flag = {"v": False}
    if KEYBOARD_OK:
        try:
            keyboard.add_hotkey(HOTKEY_STOP, lambda: stop_flag.__setitem__("v", True))
        except Exception:
            pass  # Ctrl+C still works

    print(f"Watcher running. Scanning ALL monitors every {CAPTURE_EVERY_SEC:.1f}s. "
          f"Press '{HOTKEY_STOP}' or Ctrl+C to stop. Waiting for a carriage...")

    last_alert_ts = 0.0
    frame_i = 0

    try:
        while True:
            if stop_flag["v"]:
                break

            frame_i += 1
            img = np.array(sct.grab(monitor))         # BGRA across all displays
            gray = cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)

            # compute scores
            scores = {}
            for key in ("invite_label", "carriage_icon", "join_button"):
                sc, _, _, _ = best_match_multi_scale(gray, bank[key])
                scores[key] = sc
            joined_sc, _, _, _ = best_match_multi_scale(gray, bank["joined_button"])

            carriage = (
                scores["invite_label"]  >= TH_MAIN or
                scores["carriage_icon"] >= TH_MAIN or
                scores["join_button"]   >= TH_MAIN
            )

            # heartbeat (single line)
            if HEARTBEAT_EVERY_N_FRAMES and frame_i % HEARTBEAT_EVERY_N_FRAMES == 0:
                if DEBUG_SCORES:
                    print(
                        f"[waiting] invite={scores['invite_label']:.2f} "
                        f"icon={scores['carriage_icon']:.2f} "
                        f"join={scores['join_button']:.2f} "
                        f"joined={joined_sc:.2f}",
                        end="\r", flush=True
                    )
                else:
                    print("[waiting for carriage…]", end="\r", flush=True)

            now = time.time()
            if carriage and (now - last_alert_ts) >= ALERT_COOLDOWN_SEC:
                if joined_sc >= TH_JOINED:
                    msg = "🟢 **Carriage spotted** (already joined)."
                else:
                    msg = "🟢 **Carriage appeared!** Check chat now."
                print("\nALERT -> sending Discord webhook:", msg)
                send_webhook(msg)
                last_alert_ts = now

            # optional quick dump: hold 'd' to save a frame for inspection
            if KEYBOARD_OK and keyboard.is_pressed('d'):
                out = Path("debug_frame.png")
                cv2.imwrite(str(out), gray)
                print(f"\nSaved {out} for debugging.")

            time.sleep(CAPTURE_EVERY_SEC)

    except KeyboardInterrupt:
        pass
    finally:
        print("\nStopped.")


if __name__ == "__main__":
    main()
