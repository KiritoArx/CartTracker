import time
import cv2
import numpy as np
import mss
import requests
import keyboard
from pathlib import Path
from collections import deque

# ------------ CONFIG ------------
CAPTURE_EVERY_SEC = 0.8
HOTKEY_STOP = "m"

# Scan full screen (primary monitor via mss). Quiet terminal until an alert.
# Your templates folder (put the PNGs here or change path):
TEMPLATES_DIR = Path("templates")

# Template filenames (must exist)
FILES = {
    "invite_label":  "invite_label.png",    # carriage visible (tab label)
    "carriage_icon": "carriage_icon.png",   # horse head icon
    "join_button":   "join_button.png",     # green join button
    "joined_button": "joined_button.png",   # gray joined (no alert)
    "close_x":       "close_x.png",         # optional; not used for alerting
}

# Discord webhook (rotate if this ever leaks)
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1417618033492623451/XRplkQ3uLiWXIBk25r1G5QjgF0FyCgTxV1GgZ0IzLckLcLhZdNCzTTrNHd1iziO53rPr"

# Matching thresholds (tune if needed)
TH_MAIN = 0.83      # for invite_label / carriage_icon / join_button
TH_JOINED = 0.86    # for joined_button (a bit stricter)

# Multi-scale search settings (handles different UI sizes)
SCALES = np.linspace(0.80, 1.30, 9)  # try 9 scales from 80% to 130%

# Debounce: after one alert, wait this many seconds before sending another
ALERT_COOLDOWN_SEC = 18

# --------------------------------

def load_templates():
    bank = {}
    for key, fname in FILES.items():
        p = (TEMPLATES_DIR / fname)
        if not p.exists():
            # also allow absolute paths (like the ones you showed me)
            p = Path(fname)
        img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise FileNotFoundError(f"Template missing or unreadable: {p}")
        bank[key] = img
    return bank

def best_match_multi_scale(haystack_gray, needle_gray, method=cv2.TM_CCOEFF_NORMED):
    """
    Search haystack for needle across SCALES.
    Returns (max_val, top_left, bottom_right, scale).
    """
    best = (-1.0, None, None, None)
    hN, wN = needle_gray.shape[:2]
    for s in SCALES:
        # resize template
        w = max(8, int(wN * s))
        h = max(8, int(hN * s))
        tpl = cv2.resize(needle_gray, (w, h), interpolation=cv2.INTER_AREA)
        if haystack_gray.shape[0] < h or haystack_gray.shape[1] < w:
            continue
        res = cv2.matchTemplate(haystack_gray, tpl, method)
        min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)
        val = max_val if method in (cv2.TM_CCOEFF, cv2.TM_CCOEFF_NORMED, cv2.TM_CCORR, cv2.TM_CCORR_NORMED) else -min_val
        loc = max_loc if method in (cv2.TM_CCOEFF, cv2.TM_CCOEFF_NORMED, cv2.TM_CCORR, cv2.TM_CCORR_NORMED) else min_loc
        if val > best[0]:
            top_left = loc
            bottom_right = (top_left[0] + w, top_left[1] + h)
            best = (val, top_left, bottom_right, s)
    return best

def send_webhook(msg):
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": msg}, timeout=5)
    except Exception:
        # Stay quiet in terminal by design; if you want logs, print the exception.
        pass

def main():
    # Load templates
    bank = load_templates()
    # Prepare capture
    sct = mss.mss()
    monitor = sct.monitors[1]  # primary monitor
    # Silent until first alert
    last_alert_ts = 0.0
    recent_states = deque(maxlen=10)  # small memory for debugging if needed

    while True:
        if keyboard.is_pressed(HOTKEY_STOP):
            break

        img = np.array(sct.grab(monitor))  # BGRA
        frame = cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)

        # Try to detect a "carriage present" signal
        found_scores = {}

        # The three "carriage present" cues
        for key in ("invite_label", "carriage_icon", "join_button"):
            score, tl, br, sc = best_match_multi_scale(frame, bank[key])
            found_scores[key] = score

        # "Already joined" check (no alert; just marks state)
        joined_score, _, _, _ = best_match_multi_scale(frame, bank["joined_button"])
        found_scores["joined_button"] = joined_score

        carriage_present = (
            (found_scores["invite_label"]  >= TH_MAIN) or
            (found_scores["carriage_icon"] >= TH_MAIN) or
            (found_scores["join_button"]   >= TH_MAIN)
        )

        now = time.time()

        # Alert logic (debounced)
        if carriage_present and (now - last_alert_ts) >= ALERT_COOLDOWN_SEC:
            joined_flag = (joined_score >= TH_JOINED)
            # Compose message
            if joined_flag:
                msg = "🟢 **Carriage spotted** (already joined)."
            else:
                # emphasize to hurry
                msg = "🟢 **Carriage appeared!** Check chat now."
            send_webhook(msg)
            last_alert_ts = now

        # Keep terminal quiet per your request
        time.sleep(CAPTURE_EVERY_SEC)

if __name__ == "__main__":
    main()
