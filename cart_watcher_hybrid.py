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

# --------------- CONFIG ---------------
PROCESS_NAME = "TopHeroes.exe"
USE_CLIENT_AREA = True
CAPTURE_EVERY_SEC = 0.9
REFRESH_RECT_EVERY_SEC = 2
HOTKEY_STOP = "m"

# Scan the full game window
CHAT_CROP = dict(x0=0.00, x1=1.00, y0=0.00, y1=1.00)

# Templates (template-only; no OCR)
TEMPLATE_DIR = Path("templates")
TEMPLATE_FILES = [
    "invite_label.png",   # "Invite" chunk
    "carriage_icon.png",  # carriage/horse icon
    "join_button.png",    # green Join button
    "joined_button.png",  # gray Joined button
    "close_x.png",        # X close icon
]

# Multi-scale matching
SCALES = [0.85, 0.9, 1.0, 1.1, 1.15]
TM_METHOD = cv2.TM_CCOEFF_NORMED
TM_THRESH = 0.75

# Discord webhook
ENABLE_DISCORD = True
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1417618033492623451/XRplkQ3uLiWXIBk25r1G5QjgF0FyCgTxV1GgZ0IzLckLcLhZdNCzTTrNHd1iziO53rPr"
DISCORD_USERNAME = "CartTracker"
DISCORD_AVATAR = None
MENTION = ""

# Cooldown
ALERT_COOLDOWN_SEC = 25
RECENT_MAX = 120

# Debug saving
DEBUG_SAVE_ON_ALERT = True
DEBUG_DIR = Path("debug_alerts")
# --------------------------------------

toaster = ToastNotifier()


def send_discord(content: str):
    if not ENABLE_DISCORD or not DISCORD_WEBHOOK_URL or "PUT_YOUR_DISCORD" in DISCORD_WEBHOOK_URL:
        return
    payload = {
        "username": DISCORD_USERNAME,
        "avatar_url": DISCORD_AVATAR,
        "content": (MENTION + " " + content).strip()
    }
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)
        if r.status_code >= 300:
            print(f"[Discord] Error {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[Discord] Exception: {e}")


def save_debug_alert(full_img, join_state, sig, tm_hits, tm_scores, saw_closex):
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        base = f"{ts}_{join_state}_{sig}"

        # Full screenshot
        full_img.save(DEBUG_DIR / f"{base}_full.png")

        # Metadata
        meta_path = DEBUG_DIR / f"{base}_meta.txt"
        with open(meta_path, "w", encoding="utf-8") as f:
            f.write(f"JoinState: {join_state}\n")
            f.write(f"tm_hits={tm_hits}, close_x={saw_closex}\n")
            f.write("Scores:\n")
            for k, v in tm_scores.items():
                f.write(f"  {k}: {v:.2f}\n")
    except Exception as e:
        print(f"[Debug] Failed to save alert debug: {e}")


def find_process_pid_by_name(name: str):
    name_lower = name.lower()
    for proc in psutil.process_iter(attrs=["name", "pid"]):
        try:
            if proc.info["name"] and proc.info["name"].lower() == name_lower:
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
        w = cl[2] - cl[0]
        h = cl[3] - cl[1]
        return {"left": lt[0], "top": lt[1], "width": w, "height": h}
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
    best = None
    best_area = 0
    for h in hwnds:
        try:
            l, t, r, b = win32gui.GetWindowRect(h)
            area = max(0, r - l) * max(0, b - t)
            if area > best_area:
                best_area = area
                best = h
        except win32gui.error:
            continue
    return best


def pil_from_mss(shot):
    return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def to_gray(np_rgb):
    return cv2.cvtColor(np_rgb, cv2.COLOR_RGB2GRAY)


def crop_rel(gray_img, rel):
    h, w = gray_img.shape[:2]
    x0 = int(w * rel["x0"]); x1 = int(w * rel["x1"]) 
    y0 = int(h * rel["y0"]); y1 = int(h * rel["y1"]) 
    x0 = max(0, min(x0, w-1)); x1 = max(1, min(x1, w))
    y0 = max(0, min(y0, h-1)); y1 = max(1, min(y1, h))
    return gray_img[y0:y1, x0:x1], (x0, y0, x1, y1)


def load_templates():
    loaded = []
    for fname in TEMPLATE_FILES:
        path = TEMPLATE_DIR / fname
        if not path.exists():
            print(f"[WARN] Template missing: {path}")
            continue
        img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            print(f"[WARN] Failed to read template: {path}")
            continue
        loaded.append((fname, img))
    return loaded


def match_templates(gray_crop, templates):
    hits = 0
    scores = {}
    for name, tmpl in templates:
        best = 0.0
        for s in SCALES:
            th, tw = tmpl.shape[:2]
            tw2 = max(10, int(tw * s))
            th2 = max(10, int(th * s))
            tmpl_s = cv2.resize(tmpl, (tw2, th2), interpolation=cv2.INTER_AREA)
            if gray_crop.shape[0] < th2 or gray_crop.shape[1] < tw2:
                continue
            res = cv2.matchTemplate(gray_crop, tmpl_s, TM_METHOD)
            min_val, max_val, _, _ = cv2.minMaxLoc(res)
            score = max_val if TM_METHOD in (cv2.TM_CCOEFF_NORMED, cv2.TM_CCORR_NORMED) else 1 - min_val
            if score > best:
                best = score
        scores[name] = best
        if best >= TM_THRESH:
            hits += 1
    return hits, scores


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


def main():
    templates = load_templates()
    if not templates:
        print("[ERROR] No templates loaded. Put your PNGs in ./templates")
    print(f"Loaded {len(templates)} templates.")

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
            np_rgb = np.array(pil_img)
            gray_full = to_gray(np_rgb)

            # Crop
            gray_crop, _ = crop_rel(gray_full, CHAT_CROP)

            # Template matching only
            tm_hits, tm_scores = match_templates(gray_crop, templates)

            # Determine join state
            score = lambda name: tm_scores.get(name, 0.0)
            saw_join    = score("join_button.png")   >= TM_THRESH
            saw_joined  = score("joined_button.png") >= TM_THRESH
            saw_invite  = score("invite_label.png")  >= TM_THRESH
            saw_car     = score("carriage_icon.png") >= TM_THRESH
            saw_closex  = score("close_x.png")       >= TM_THRESH

            join_state = "Unknown"
            if saw_join and not saw_joined:
                join_state = "Joinable"
            elif saw_joined and not saw_join:
                join_state = "Already Joined"
            elif saw_join and saw_joined:
                join_state = "Joinable" if score("join_button.png") >= score("joined_button.png") else "Already Joined"

            # Template-only rule: require a card cue and a join UI element
            has_card_cue = (saw_invite or saw_car)
            has_join_ui  = (saw_join or saw_joined)
            match_ok = has_card_cue and has_join_ui

            # Debug print
            print(
                f"TM hits={tm_hits} state={join_state} | "
                f"scores: invite={score('invite_label.png'):.2f}, car={score('carriage_icon.png'):.2f}, "
                f"join={score('join_button.png'):.2f}, joined={score('joined_button.png'):.2f}, x={score('close_x.png'):.2f}"
            )

            if match_ok:
                sig_source = f"{join_state}|{tm_scores}"
                sig = sha_short(sig_source)
                last_t = last_seen.get(sig, 0)
                if (time.time() - last_t) > ALERT_COOLDOWN_SEC:
                    msg = f"Cart Invite Detected — {join_state} | tm={tm_hits}" + (" (+X)" if saw_closex else "")
                    notify("Cart Invite Detected", msg)
                    append_log(f"{join_state} | {tm_scores}")
                    send_discord(msg)

                    if DEBUG_SAVE_ON_ALERT:
                        try:
                            save_debug_alert(
                                pil_img, join_state, sig,
                                tm_hits, tm_scores, saw_closex
                            )
                        except Exception as e:
                            print(f"[Debug] Exception during debug save: {e}")

                    last_seen[sig] = time.time()
                    recent.append(sig)

            # pace & hotkey responsive
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

