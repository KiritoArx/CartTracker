import time
import hashlib
from collections import deque
from pathlib import Path

import psutil
import mss
import numpy as np
import cv2
from PIL import Image
import keyboard
from win10toast import ToastNotifier
import winsound
import requests

# Windows APIs
import win32gui
import win32con
import win32process

# ================== CONFIG (simple) ==================
PROCESS_NAME = "TopHeroes.exe"
USE_CLIENT_AREA = True
CAPTURE_EVERY_SEC = 0.9
REFRESH_RECT_EVERY_SEC = 2
HOTKEY_STOP = "m"

# Scan almost everything for cues; scan only lower chunk for buttons (faster + fewer false hits)
SEARCH_CROP = dict(x0=0.00, x1=1.00, y0=0.00, y1=1.00)  # for Invite/Carriage
BUTTON_CROP  = dict(x0=0.08, x1=0.92, y0=0.55, y1=0.96)  # for Join/Joined buttons

# Templates
TEMPLATE_DIR = Path("templates")
CUE_TEMPLATES = ["invite_label.png", "carriage_icon.png"]
JOIN_TEMPLATE = "join_button.png"
JOINED_TEMPLATE = "joined_button.png"

# Matching
SCALES_CUE   = [0.90, 1.00, 1.10]
SCALES_BTN   = [0.90, 1.00, 1.10]
TM_METHOD    = cv2.TM_CCOEFF_NORMED
CUE_THRESH   = 0.82     # see the cue somewhere
JOIN_THRESH  = 0.84     # green Join button present
JOINED_THRESH= 0.84     # gray Joined button present
WIN_MARGIN   = 0.02     # tiny edge so one button "wins" if both show

# Alerts
ALERT_COOLDOWN_SEC = 25
RECENT_MAX = 120

# Debug saving
DEBUG_SAVE_ON_ALERT = True
DEBUG_DIR = Path("debug_alerts")

# Discord (leave as-is if you want)
ENABLE_DISCORD = True
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1417618033492623451/XRplkQ3uLiWXIBk25r1G5QjgF0FyCgTxV1GgZ0IzLckLcLhZdNCzTTrNHd1iziO53rPr"
DISCORD_USERNAME = "CartTracker"
DISCORD_AVATAR = None
MENTION = ""
# =====================================================

toaster = ToastNotifier()

# ---------- Utils ----------
def pil_from_mss(shot):
    return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

def to_gray(np_rgb):
    return cv2.cvtColor(np_rgb, cv2.COLOR_RGB2GRAY)

def crop_rel(img, rel):
    """Crop by relative box on either gray or RGB image."""
    h, w = (img.shape[:2] if img.ndim >= 2 else (0, 0))
    x0 = int(w * rel["x0"]); x1 = int(w * rel["x1"])
    y0 = int(h * rel["y0"]); y1 = int(h * rel["y1"])
    x0 = max(0, min(x0, w-1)); x1 = max(1, min(x1, w))
    y0 = max(0, min(y0, h-1)); y1 = max(1, min(y1, h))
    return img[y0:y1, x0:x1], (x0, y0, x1, y1)

def sha_short(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:10]

def notify(title: str, msg: str):
    try:
        toaster.show_toast(title, msg, duration=4, threaded=True)
    except Exception:
        pass
    try:
        winsound.Beep(1400, 160)
        winsound.Beep(1000, 120)
    except Exception:
        pass

def append_log(line: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with open("cart_hits.log", "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {line}\n")

# ---------- Discord ----------
def send_discord(content: str):
    if not ENABLE_DISCORD or not DISCORD_WEBHOOK_URL or "PUT_YOUR_DISCORD" in DISCORD_WEBHOOK_URL:
        return
    payload = {"username": DISCORD_USERNAME, "avatar_url": DISCORD_AVATAR, "content": (MENTION + " " + content).strip()}
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)
        if r.status_code >= 300:
            print(f"[Discord] Error {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[Discord] Exception: {e}")

# ---------- Window ----------
def find_process_pid_by_name(name: str):
    for proc in psutil.process_iter(attrs=["name", "pid"]):
        try:
            if proc.info["name"] and proc.info["name"].lower() == name.lower():
                return proc.info["pid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return None

def enum_windows_for_pid(pid):
    results = []
    def callback(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            try:
                _, found_pid = win32process.GetWindowThreadProcessId(hwnd)
                if found_pid == pid:
                    l, t, r, b = win32gui.GetWindowRect(hwnd)
                    if (r - l) > 100 and (b - t) > 100:
                        results.append(hwnd)
            except win32gui.error:
                pass
        return True
    win32gui.EnumWindows(callback, None)
    return results

def bring_to_front(hwnd):
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
    except win32gui.error:
        pass

def get_capture_region(hwnd, use_client=True):
    if use_client:
        lt = win32gui.ClientToScreen(hwnd, (0, 0))
        cl = win32gui.GetClientRect(hwnd)
        return {"left": lt[0], "top": lt[1], "width": cl[2] - cl[0], "height": cl[3] - cl[1]}
    else:
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        return {"left": l, "top": t, "width": (r - l), "height": (b - t)}

def find_topheroes_window():
    pid = find_process_pid_by_name(PROCESS_NAME)
    if not pid:
        return None
    hwnds = enum_windows_for_pid(pid)
    if not hwnds:
        return None
    best, area_best = None, 0
    for h in hwnds:
        try:
            l, t, r, b = win32gui.GetWindowRect(h)
            area = max(0, r - l) * max(0, b - t)
            if area > area_best:
                area_best, best = area, h
        except win32gui.error:
            continue
    return best

# ---------- Template loading & matching ----------
def load_template_gray(name: str):
    p = TEMPLATE_DIR / name
    img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
    if img is None:
        print(f"[WARN] Missing or unreadable template: {p}")
    return img

def best_match_score(gray_img, tmpl, scales):
    """Return the best normalized correlation score across scales."""
    if tmpl is None:
        return 0.0
    best = 0.0
    th, tw = tmpl.shape[:2]
    for s in scales:
        tw2 = max(8, int(tw * s))
        th2 = max(8, int(th * s))
        if gray_img.shape[0] < th2 or gray_img.shape[1] < tw2:
            continue
        tmpl_s = cv2.resize(tmpl, (tw2, th2), interpolation=cv2.INTER_AREA)
        res = cv2.matchTemplate(gray_img, tmpl_s, TM_METHOD)
        _, max_val, _, _ = cv2.minMaxLoc(res)
        if max_val > best:
            best = max_val
    return float(best)

# ---------- Debug ----------
def save_debug_alert(full_img, join_state, sig, scores):
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        base = f"{ts}_{join_state}_{sig}"
        full_img.save(DEBUG_DIR / f"{base}_full.png")
        with open(DEBUG_DIR / f"{base}_meta.txt", "w", encoding="utf-8") as f:
            f.write(f"state={join_state}\n")
            for k, v in scores.items():
                f.write(f"{k}={v:.3f}\n")
    except Exception as e:
        print(f"[Debug] Save fail: {e}")

# ---------- Main ----------
def main():
    # Load templates
    tmpl_inv   = load_template_gray(CUE_TEMPLATES[0])
    tmpl_car   = load_template_gray(CUE_TEMPLATES[1])
    tmpl_join  = load_template_gray(JOIN_TEMPLATE)
    tmpl_joined= load_template_gray(JOINED_TEMPLATE)

    print("Templates loaded:",
          f"invite={'OK' if tmpl_inv is not None else 'X'}",
          f"carriage={'OK' if tmpl_car is not None else 'X'}",
          f"join={'OK' if tmpl_join is not None else 'X'}",
          f"joined={'OK' if tmpl_joined is not None else 'X'}")

    print(f"Looking for process: {PROCESS_NAME}")
    stop_flag = {"stop": False}

    def on_stop():
        stop_flag["stop"] = True
        print("\n[Hotkey] Stop requested...")

    keyboard.add_hotkey(HOTKEY_STOP, on_stop)
    print(f"Press '{HOTKEY_STOP}' at any time to stop.\n")

    sct = mss.mss()
    hwnd = None
    region = None
    last_rect_refresh = 0

    recent = deque(maxlen=RECENT_MAX)
    last_seen = {}

    try:
        while not stop_flag["stop"]:
            if hwnd is None:
                hwnd = find_topheroes_window()
                if hwnd:
                    title = win32gui.GetWindowText(hwnd)
                    print(f"Attached to window: {title} (hwnd={hwnd})")
                    bring_to_front(hwnd)
                    region = get_capture_region(hwnd, USE_CLIENT_AREA)
                    print(f"Initial capture region: {region}")
                else:
                    print("TopHeroes.exe not found. Retrying in 3s...")
                    time.sleep(3)
                    continue

            # Periodic region refresh
            now = time.time()
            if now - last_rect_refresh > REFRESH_RECT_EVERY_SEC:
                try:
                    region = get_capture_region(hwnd, USE_CLIENT_AREA)
                except win32gui.error:
                    print("Lost window. Re-attaching...")
                    hwnd = None
                    time.sleep(2)
                    continue
                last_rect_refresh = now

            # Capture
            try:
                shot = sct.grab(region)
            except Exception as e:
                print(f"Capture error: {e}. Re-attaching...")
                hwnd = None
                time.sleep(2)
                continue

            pil_img = pil_from_mss(shot)
            np_rgb_full = np.array(pil_img)
            gray_full = to_gray(np_rgb_full)
            # light blur can stabilize small lighting changes
            gray_full = cv2.GaussianBlur(gray_full, (3, 3), 0)

            # Crops
            gray_search, _ = crop_rel(gray_full, SEARCH_CROP)
            gray_btn, _    = crop_rel(gray_full, BUTTON_CROP)

            # 1) Cart cue: invite OR carriage anywhere in SEARCH_CROP
            sc_inv = best_match_score(gray_search, tmpl_inv, SCALES_CUE)
            sc_car = best_match_score(gray_search, tmpl_car, SCALES_CUE)
            cue_ok = (sc_inv >= CUE_THRESH) or (sc_car >= CUE_THRESH)

            # 2) Button: Join vs Joined in BUTTON_CROP
            sc_join   = best_match_score(gray_btn, tmpl_join,   SCALES_BTN)
            sc_joined = best_match_score(gray_btn, tmpl_joined, SCALES_BTN)

            # Simple state rule (template-only)
            state = "Unknown"
            if cue_ok:
                if (sc_join >= JOIN_THRESH) and (sc_join >= sc_joined + WIN_MARGIN):
                    state = "Joinable"
                elif (sc_joined >= JOINED_THRESH) and (sc_joined >= sc_join + WIN_MARGIN):
                    state = "Already Joined"

            print(f"cue(inv={sc_inv:.2f} car={sc_car:.2f} ok={cue_ok}) | "
                  f"join={sc_join:.2f} joined={sc_joined:.2f} => {state}")

            # Alert only when NEW & Joinable (you haven't joined yet)
            if state == "Joinable":
                # Use coarse signature from scores to rate-limit
                sig_source = f"{state}|{round(sc_join,3)}|{round(sc_inv,3)}|{round(sc_car,3)}"
                sig = sha_short(sig_source)
                last_t = last_seen.get(sig, 0)
                if (time.time() - last_t) > ALERT_COOLDOWN_SEC:
                    msg = "Cart Invite Detected — Joinable"
                    notify("Cart Invite Detected", msg)
                    append_log(f"{state} | inv={sc_inv:.3f} car={sc_car:.3f} join={sc_join:.3f} joined={sc_joined:.3f}")
                    send_discord(msg)
                    if DEBUG_SAVE_ON_ALERT:
                        save_debug_alert(pil_img, state, sig, {
                            "inv": sc_inv, "car": sc_car, "join": sc_join, "joined": sc_joined
                        })
                    last_seen[sig] = time.time()
                    recent.append(sig)

            # pacing + hotkey responsive sleep
            for _ in range(int(CAPTURE_EVERY_SEC * 10)):
                if stop_flag["stop"]:
                    break
                time.sleep(0.1)

    except KeyboardInterrupt:
        print("\n[Ctrl+C] Stopping...")
    finally:
        print("Exiting watcher.")
        try:
            keyboard.unhook_all_hotkeys()
        except Exception:
            pass

if __name__ == "__main__":
    main()
