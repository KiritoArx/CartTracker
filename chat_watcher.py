import time
import sys
import psutil
import pytesseract
import mss
import numpy as np
import cv2
from PIL import Image
import threading

# Windows-specific imports
import win32gui
import win32con
import win32process
import win32api

# Global hotkey lib (may need admin)
import keyboard

# --------------- CONFIG ---------------
PROCESS_NAME = "TopHeroes.exe"   # as shown in Task Manager "Details"
REFRESH_RECT_EVERY_SEC = 3       # how often to refresh window rect in case you drag the window
CAPTURE_EVERY_SEC = 2            # OCR cadence
HOTKEY_STOP = "m"                # press 'm' to stop
USE_CLIENT_AREA = True           # capture client area (inside window)
# Tesseract path (adjust if yours is different)
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
# --------------------------------------


def find_process_pid_by_name(name: str):
    """Return first PID that matches process name (case-insensitive), else None."""
    name_lower = name.lower()
    for proc in psutil.process_iter(attrs=["name", "pid"]):
        try:
            if proc.info["name"] and proc.info["name"].lower() == name_lower:
                return proc.info["pid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return None


def enum_windows_for_pid(pid):
    """Return a list of top-level visible window handles for a given PID."""
    results = []

    def callback(hwnd, results_list):
        if win32gui.IsWindowVisible(hwnd):
            try:
                _, found_pid = win32process.GetWindowThreadProcessId(hwnd)
                if found_pid == pid:
                    # filter out tool/zero-size windows
                    rect = win32gui.GetWindowRect(hwnd)
                    if rect and (rect[2] - rect[0] > 50) and (rect[3] - rect[1] > 50):
                        results_list.append(hwnd)
            except win32gui.error:
                pass
        return True

    win32gui.EnumWindows(callback, results)
    return results


def bring_to_front(hwnd):
    """Restore if minimized and bring to foreground."""
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        # Force to foreground (may fail if different input queue, but try)
        win32gui.SetForegroundWindow(hwnd)
    except win32gui.error:
        # Try alternative methods if needed
        try:
            shell = win32gui.FindWindow("Shell_TrayWnd", None)
            win32gui.SetForegroundWindow(shell)
            time.sleep(0.1)
            win32gui.SetForegroundWindow(hwnd)
        except win32gui.error:
            pass


def get_capture_region(hwnd, use_client=True):
    """
    Return a dict for mss region: {"top": y, "left": x, "width": w, "height": h}
    If use_client is True, capture only client area (inside borders).
    """
    if use_client:
        # Client rect (0,0,w,h) relative to client; convert to screen coords
        left_top = win32gui.ClientToScreen(hwnd, (0, 0))
        client_rect = win32gui.GetClientRect(hwnd)  # (left, top, right, bottom) in client coords
        width = client_rect[2] - client_rect[0]
        height = client_rect[3] - client_rect[1]
        return {"left": left_top[0], "top": left_top[1], "width": width, "height": height}
    else:
        # Whole window including borders/title
        rect = win32gui.GetWindowRect(hwnd)  # (l, t, r, b)
        return {"left": rect[0], "top": rect[1], "width": rect[2] - rect[0], "height": rect[3] - rect[1]}


def find_topheroes_window():
    """Find TopHeroes.exe main window handle, or None."""
    pid = find_process_pid_by_name(PROCESS_NAME)
    if not pid:
        return None
    hwnds = enum_windows_for_pid(pid)
    if not hwnds:
        return None
    # Heuristic: pick the first one with a title and biggest area
    best_hwnd = None
    best_area = 0
    for h in hwnds:
        try:
            l, t, r, b = win32gui.GetWindowRect(h)
            area = max(0, r - l) * max(0, b - t)
            if area > best_area:
                best_area = area
                best_hwnd = h
        except win32gui.error:
            continue
    return best_hwnd


def ocr_image(pil_image):
    """Run OCR on a PIL Image and return text."""
    # (Optional) preprocess: convert to grayscale, slight threshold can help with chat UIs
    img_cv = cv2.cvtColor(np.array(pil_image), cv2.COLOR_RGB2BGR)
    gray = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)
    # Adaptive threshold can help with variable backgrounds
    thr = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                cv2.THRESH_BINARY, 31, 9)
    pil_processed = Image.fromarray(thr)
    return pytesseract.image_to_string(pil_processed)


def main():
    print(f"Looking for process: {PROCESS_NAME}")
    stop_flag = {"stop": False}

    # Hotkey to exit
    def on_stop():
        stop_flag["stop"] = True
        print("\n[Hotkey] Stop requested...")

    keyboard.add_hotkey(HOTKEY_STOP, on_stop)
    print(f"Press '{HOTKEY_STOP}' at any time to stop.\n")

    sct = mss.mss()
    last_rect_refresh = 0
    hwnd = None
    region = None

    try:
        while not stop_flag["stop"]:
            if hwnd is None:
                hwnd = find_topheroes_window()
                if hwnd:
                    try:
                        title = win32gui.GetWindowText(hwnd)
                    except win32gui.error:
                        title = "(unknown)"
                    print(f"Attached to window: {title} (hwnd={hwnd})")
                    bring_to_front(hwnd)
                    region = get_capture_region(hwnd, USE_CLIENT_AREA)
                    print(f"Initial capture region: {region}")
                else:
                    print("TopHeroes.exe not found. Waiting 3s and retrying...")
                    time.sleep(3)
                    continue

            # Periodically refresh region in case you move/resize the window
            now = time.time()
            if now - last_rect_refresh > REFRESH_RECT_EVERY_SEC:
                try:
                    region = get_capture_region(hwnd, USE_CLIENT_AREA)
                except win32gui.error:
                    # Window may have closed
                    print("Lost window. Re-attaching...")
                    hwnd = None
                    time.sleep(2)
                    continue
                last_rect_refresh = now

            # Bring to front (light touch; avoids constant stealing focus)
            bring_to_front(hwnd)

            # Grab screenshot of the region
            try:
                shot = sct.grab(region)
            except Exception as e:
                print(f"Capture error: {e}. Re-attaching...")
                hwnd = None
                time.sleep(2)
                continue

            # Convert to PIL for OCR
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

            # OCR
            try:
                text = ocr_image(img)
            except Exception as e:
                print(f"OCR error: {e}")
                text = ""

            if text.strip():
                print("Detected:")
                print(text.strip())
                print("-" * 60)

            # Pace the loop
            for _ in range(int(CAPTURE_EVERY_SEC * 10)):  # fine-grained wait so hotkey is responsive
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
