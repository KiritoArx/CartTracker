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

# Scan the full game window (adjust later if you want)
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

# Per-template thresholds + exclusive margin so one button must clearly beat the other
PER_THR = {
    "invite_label.png": 0.72,
    "carriage_icon.png": 0.72,
    "join_button.png":   0.80,
    "joined_button.png": 0.80,
    "close_x.png":       0.75,
}
EXCLUSIVE_MARGIN = 0.10  # winner must beat the other by at least this much

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

# ---------- Discord ----------
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

# ---------- Debug ----------
def save_debug_alert(full_img, join_state, sig, tm_hits, tm_scores, saw_closex):
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        base = f"{ts}_{join_state}_{sig}"
        full_img.save(DEBUG_DIR / f"{base}_full.png")
        meta_path = DEBUG_DIR / f"{base}_meta.txt"
        with open(meta_path, "w", encoding="utf-8") as f:
            f.write(f"JoinState: {join_state}\n")
            f.write(f"tm_hits={tm_hits}, close_x={saw_closex}\n")
            f.write("Scores:\n")
            for k, v in tm_scores.items():
                f.write(f"  {k}: {v:.3f}\n")
    except Exception as e:
        print(f"[Debug] Failed to save alert debug: {e}")

# ---------- Window helpers ----------
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

# ---------- Image utils ----------
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
    """
    Returns:
      hits: int
      info: dict[name] = {
          'score': best_score,
          'pt': (x, y) top-left in gray_crop,
          'size': (w, h) of the scaled template that won
      }
    """
    hits = 0
    info = {}
    for name, tmpl in templates:
        best = -1.0
        best_pt = None
        best_sz = None
        for s in SCALES:
            th, tw = tmpl.shape[:2]
            tw2 = max(10, int(tw * s))
            th2 = max(10, int(th * s))
            if gray_crop.shape[0] < th2 or gray_crop.shape[1] < tw2:
                continue
            tmpl_s = cv2.resize(tmpl, (tw2, th2), interpolation=cv2.INTER_AREA)
            res = cv2.matchTemplate(gray_crop, tmpl_s, TM_METHOD)
            min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)
            score = max_val if TM_METHOD in (cv2.TM_CCOEFF_NORMED, cv2.TM_CCORR_NORMED) else 1 - min_val
            if score > best:
                best = score
                best_pt = max_loc if TM_METHOD in (cv2.TM_CCOEFF_NORMED, cv2.TM_CCORR_NORMED) else min_loc
                best_sz = (tw2, th2)
        if best < 0:
            best = 0.0
        info[name] = {"score": best, "pt": best_pt, "size": best_sz}
        if best >= TM_THRESH:
            hits += 1
    return hits, info

def _get_score(info, name):
    return info.get(name, {}).get("score", 0.0)

def _get_box(info, name):
    ent = info.get(name, {})
    return ent.get("pt"), ent.get("size")

def _crop_box(np_rgb, pt, size):
    if pt is None or size is None:
        return None
    x, y = pt
    w, h = size
    h_img, w_img = np_rgb.shape[:2]
    x2, y2 = min(w_img, x + w), min(h_img, y + h)
    if x < 0 or y < 0 or x >= w_img or y >= h_img or x2 - x < 5 or y2 - y < 5:
        return None
    return np_rgb[y:y2, x:x2, :]

def is_green_button(rgb_roi):
    # HSV gate for UI green
    hsv = cv2.cvtColor(rgb_roi, cv2.COLOR_RGB2HSV)
    h_mean = hsv[...,0].mean()
    s_mean = hsv[...,1].mean()
    v_mean = hsv[...,2].mean()
    return (35 <= h_mean <= 90) and (s_mean >= 90) and (v_mean >= 80)

def is_gray_pill(rgb_roi):
    # Gray: low saturation, mid/high brightness
    hsv = cv2.cvtColor(rgb_roi, cv2.COLOR_RGB2HSV)
    s_mean = hsv[...,1].mean()
    v_mean = hsv[...,2].mean()
    return (s_mean <= 40) and (70 <= v_mean <= 220)

def classify_state(tm_info, rgb_crop):
    thr = lambda k: PER_THR.get(k, 0.75)

    sj   = _get_score(tm_info, "join_button.png")
    sjd  = _get_score(tm_info, "joined_button.png")
    sinv = _get_score(tm_info, "invite_label.png")
    scar = _get_score(tm_info, "carriage_icon.png")

    has_card_cue = (sinv >= thr("invite_label.png")) or (scar >= thr("carriage_icon.png"))
    if not has_card_cue:
        return "Unknown", False

    j_pt, j_sz   = _get_box(tm_info, "join_button.png")
    jd_pt, jd_sz = _get_box(tm_info, "joined_button.png")

    join_roi   = _crop_box(rgb_crop, j_pt, j_sz)
    joined_roi = _crop_box(rgb_crop, jd_pt, jd_sz)

    join_green  = (join_roi is not None)   and is_green_button(join_roi)
    joined_gray = (joined_roi is not None) and is_gray_pill(joined_roi)

    joinable_strong = (sj  >= thr("join_button.png"))   and (sj  - sjd >= EXCLUSIVE_MARGIN) and join_green
    joined_strong   = (sjd >= thr("joined_button.png")) and (sjd - sj  >= EXCLUSIVE_MARGIN) and joined_gray

    if joinable_strong:
        return "Joinable", True
    if joined_strong:
        return "Already Joined", True

    # Fallbacks if one is clearly present and the other is weak
    if (sj >= thr("join_button.png")) and join_green and (sjd < thr("joined_button.png") - 0.05):
        return "Joinable", True
    if (sjd >= thr("joined_button.png")) and joined_gray and (sj < thr("join_button.png") - 0.05):
        return "Already Joined", True

    return "Unknown", False

# ---------- Misc ----------
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

# ---------- Main ----------
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
            np_rgb_full = np.array(pil_img)

            # OPTIONAL slight blur to stabilize scores
            gray_full = to_gray(np_rgb_full)
            gray_full = cv2.GaussianBlur(gray_full, (3, 3), 0)

            # Crop BOTH gray and RGB with the same box so locations line up
            gray_crop, (x0, y0, x1, y1) = crop_rel(gray_full, CHAT_CROP)
            rgb_crop = np_rgb_full[y0:y1, x0:x1, :]

            # Template matching (returns scores + locations)
            tm_hits, tm_info = match_templates(gray_crop, templates)

            # Classify using color + exclusivity
            join_state, match_ok = classify_state(tm_info, rgb_crop)

            def g(name):
                return tm_info.get(name, {}).get("score", 0.0)

            print(
                f"TM hits={tm_hits} state={join_state} | "
                f"scores: invite={g('invite_label.png'):.2f}, car={g('carriage_icon.png'):.2f}, "
                f"join={g('join_button.png'):.2f}, joined={g('joined_button.png'):.2f}, x={g('close_x.png'):.2f}"
            )

            # Only alert when it's actually Joinable (new cart + you haven't joined)
            if match_ok and join_state == "Joinable":
                # compact signature based on scores to rate-limit
                compact_scores = {k: round(v.get('score', 0.0), 3) for k, v in tm_info.items()}
                sig_source = f"{join_state}|{compact_scores}"
                sig = sha_short(sig_source)
                last_t = last_seen.get(sig, 0)
                if (time.time() - last_t) > ALERT_COOLDOWN_SEC:
                    msg = f"Cart Invite Detected — {join_state} | tm={tm_hits}"
                    notify("Cart Invite Detected", msg)
                    append_log(f"{join_state} | {compact_scores}")
                    send_discord(msg)

                    if DEBUG_SAVE_ON_ALERT:
                        try:
                            saw_closex = g('close_x.png') >= PER_THR.get('close_x.png', 0.75)
                            save_debug_alert(
                                pil_img, join_state, sig, tm_hits,
                                {k: float(v) for k, v in compact_scores.items()},
                                saw_closex
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
