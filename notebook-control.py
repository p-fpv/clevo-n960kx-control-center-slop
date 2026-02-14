#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Notebook Control Center - Undervolt + Fan Control + Keyboard Backlight
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import subprocess
import json
import os
import re
import sys
import threading
import time
import fcntl
import struct
import glob
import select

# Get original user home (before sudo)
ORIGINAL_USER = os.environ.get('SUDO_USER', os.environ.get('USER', 'root'))
ORIGINAL_HOME = os.path.expanduser(f"~{ORIGINAL_USER}") if ORIGINAL_USER != 'root' else os.path.expanduser("~")
CONFIG_DIR = os.path.join(ORIGINAL_HOME, ".config/notebook-control")
PROFILES_FILE = os.path.join(CONFIG_DIR, "profiles.json")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")

# Try matplotlib for graph view
try:
    import matplotlib
    matplotlib.use('TkAgg')
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False

# Try evdev for keyboard activity monitoring
try:
    import evdev
    from evdev import InputDevice, categorize, ecodes
    HAS_EVDEV = True
except ImportError:
    HAS_EVDEV = False

# ============================================================================
# FAN CONTROL - Direct ioctl to /dev/tuxedo_io
# ============================================================================

IOCTL_MAGIC = 0xEC
MAGIC_READ_CL = IOCTL_MAGIC + 1
MAGIC_WRITE_CL = IOCTL_MAGIC + 2
MAGIC_READ_UW = IOCTL_MAGIC + 3
MAGIC_WRITE_UW = IOCTL_MAGIC + 4

_IOC_NONE, _IOC_WRITE, _IOC_READ = 0, 1, 2
_IOC_NRSHIFT, _IOC_TYPESHIFT, _IOC_SIZESHIFT, _IOC_DIRSHIFT = 0, 8, 16, 30
PTR_SIZE = 8

def _IOC(d, t, n, s):
    return (d << _IOC_DIRSHIFT) | (t << _IOC_TYPESHIFT) | (n << _IOC_NRSHIFT) | (s << _IOC_SIZESHIFT)

def _IOR(t, n, s=PTR_SIZE): return _IOC(_IOC_READ, t, n, s)
def _IOW(t, n, s=PTR_SIZE): return _IOC(_IOC_WRITE, t, n, s)
def _IO(t, n): return _IOC(_IOC_NONE, t, n, 0)

R_HWCHECK_CL = _IOR(IOCTL_MAGIC, 0x05)
R_CL_FANINFO1 = _IOR(MAGIC_READ_CL, 0x10)
R_CL_FANINFO2 = _IOR(MAGIC_READ_CL, 0x11)
R_CL_FANINFO3 = _IOR(MAGIC_READ_CL, 0x12)
W_CL_FANSPEED = _IOW(MAGIC_WRITE_CL, 0x10)
W_CL_FANAUTO = _IOW(MAGIC_WRITE_CL, 0x11)

R_HWCHECK_UW = _IOR(IOCTL_MAGIC, 0x06)
R_UW_FANSPEED = _IOR(MAGIC_READ_UW, 0x10)
R_UW_FANSPEED2 = _IOR(MAGIC_READ_UW, 0x11)
R_UW_FAN_TEMP = _IOR(MAGIC_READ_UW, 0x12)
R_UW_FAN_TEMP2 = _IOR(MAGIC_READ_UW, 0x13)
W_UW_FANSPEED = _IOW(MAGIC_WRITE_UW, 0x10)
W_UW_FANSPEED2 = _IOW(MAGIC_WRITE_UW, 0x11)
W_UW_FANAUTO = _IO(MAGIC_WRITE_UW, 0x14)

DEVICE = "/dev/tuxedo_io"

def ioctl_read_int32(fd, req):
    buf = bytearray(PTR_SIZE)
    fcntl.ioctl(fd, req, buf)
    return struct.unpack('i', buf[:4])[0]

def ioctl_write_int32(fd, req, val):
    buf = struct.pack('i', val) + b'\x00' * (PTR_SIZE - 4)
    fcntl.ioctl(fd, req, buf)


class FanController:
    def __init__(self):
        self.fd = None
        self.platform = None
        self.connect()

    def connect(self):
        try:
            self.fd = os.open(DEVICE, os.O_RDWR)
            if ioctl_read_int32(self.fd, R_HWCHECK_CL) == 1:
                self.platform = "clevo"
            elif ioctl_read_int32(self.fd, R_HWCHECK_UW) == 1:
                self.platform = "uniwill"
        except:
            self.fd = None
            self.platform = None

    def disconnect(self):
        if self.fd:
            os.close(self.fd)
            self.fd = None

    def get_fan_info(self, fan_id=0):
        if not self.fd:
            return 0, 0
        try:
            if self.platform == "clevo":
                cmds = [R_CL_FANINFO1, R_CL_FANINFO2, R_CL_FANINFO3]
                info = ioctl_read_int32(self.fd, cmds[fan_id])
                speed = info & 0xff
                temp = (info >> 16) & 0xff
                if temp > 127: temp -= 256
                return round(speed * 100 / 255), temp
            elif self.platform == "uniwill":
                speed_cmd = R_UW_FANSPEED if fan_id == 0 else R_UW_FANSPEED2
                temp_cmd = R_UW_FAN_TEMP if fan_id == 0 else R_UW_FAN_TEMP2
                return ioctl_read_int32(self.fd, speed_cmd), ioctl_read_int32(self.fd, temp_cmd)
        except:
            pass
        return 0, 0

    def set_fan_speed(self, fan_id, speed_pct):
        if not self.fd:
            return
        try:
            if self.platform == "clevo":
                speed_raw = int(speed_pct * 2.55)
                speeds = []
                for i in range(3):
                    if i == fan_id:
                        speeds.append(speed_raw)
                    else:
                        info = ioctl_read_int32(self.fd, [R_CL_FANINFO1, R_CL_FANINFO2, R_CL_FANINFO3][i])
                        speeds.append(info & 0xff)
                arg = speeds[0] | (speeds[1] << 8) | (speeds[2] << 16)
                ioctl_write_int32(self.fd, W_CL_FANSPEED, arg)
            elif self.platform == "uniwill":
                speed_raw = int(speed_pct * 2)
                cmd = W_UW_FANSPEED if fan_id == 0 else W_UW_FANSPEED2
                ioctl_write_int32(self.fd, cmd, speed_raw)
        except Exception as e:
            print(f"Error setting fan speed: {e}")

    def set_auto(self):
        if not self.fd:
            return
        try:
            if self.platform == "clevo":
                ioctl_write_int32(self.fd, W_CL_FANAUTO, 0x0f)
            elif self.platform == "uniwill":
                fcntl.ioctl(self.fd, W_UW_FANAUTO)
        except:
            pass

    def set_manual_mode(self):
        """Переключить вентиляторы в ручной режим (отключить HW Auto)"""
        if not self.fd:
            return
        try:
            if self.platform == "clevo":
                # Запись 0 в регистр авто-режима отключает его
                ioctl_write_int32(self.fd, W_CL_FANAUTO, 0)
            elif self.platform == "uniwill":
                # Для Uniwill просто пишем скорость, это переключает режим
                pass
        except Exception as e:
            print(f"Error setting manual mode: {e}")


class FanCurve:
    DEFAULT = [(30, 0), (40, 20), (50, 35), (60, 50), (70, 70), (80, 90), (90, 100)]

    def __init__(self, points=None):
        self.points = list(points) if points else list(self.DEFAULT)
        self.points.sort(key=lambda p: p[0])

    def get_speed(self, temp):
        if temp <= self.points[0][0]: return self.points[0][1]
        if temp >= self.points[-1][0]: return self.points[-1][1]
        for i in range(len(self.points) - 1):
            t1, s1 = self.points[i]
            t2, s2 = self.points[i + 1]
            if t1 <= temp <= t2:
                return int(s1 + (temp - t1) / (t2 - t1) * (s2 - s1))
        return self.points[-1][1]

    def copy(self):
        return FanCurve([p for p in self.points])


# ============================================================================
# KEYBOARD ACTIVITY MONITOR - Using evdev
# ============================================================================

class KeyboardActivityMonitor:
    """
    Monitor keyboard activity using evdev to detect keypresses.
    Used for auto-off timer functionality.
    """

    def __init__(self):
        self.devices = []
        self.device_paths = []
        self.running = False
        self.last_activity_time = time.time()
        self.activity_callback = None
        self.monitor_thread = None
        self.has_evdev = HAS_EVDEV
        self._find_keyboards()

    def _find_keyboards(self):
        """Find all keyboard input devices"""
        if not HAS_EVDEV:
            # Fallback: use /dev/input/event* directly
            for i in range(20):
                path = f"/dev/input/event{i}"
                if os.path.exists(path):
                    self.device_paths.append(path)
            return

        try:
            # Get all input devices
            devices = [evdev.InputDevice(path) for path in evdev.list_devices()]

            for device in devices:
                # Check if device has keys (keyboard)
                capabilities = device.capabilities()
                if ecodes.EV_KEY in capabilities:
                    # Check for typical keyboard keys
                    keys = capabilities[ecodes.EV_KEY]
                    keyboard_keys = {ecodes.KEY_A, ecodes.KEY_SPACE, ecodes.KEY_ENTER,
                                    ecodes.KEY_LEFTSHIFT, ecodes.KEY_ESC}
                    if any(k in keys for k in keyboard_keys):
                        self.devices.append(device)
                        self.device_paths.append(device.path)

            if self.devices:
                print(f"Found {len(self.devices)} keyboard device(s)")
        except Exception as e:
            print(f"Error finding keyboards: {e}")
            # Fallback
            for i in range(20):
                path = f"/dev/input/event{i}"
                if os.path.exists(path):
                    self.device_paths.append(path)

    def is_available(self):
        """Check if keyboard monitoring is available"""
        return len(self.device_paths) > 0

    def start(self, activity_callback=None):
        """Start monitoring keyboard activity"""
        if self.running:
            return

        self.activity_callback = activity_callback
        self.running = True
        self.last_activity_time = time.time()

        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()

    def stop(self):
        """Stop monitoring"""
        self.running = False
        if self.monitor_thread:
            self.monitor_thread.join(timeout=1)

    def get_idle_time(self):
        """Get time since last keyboard activity in seconds"""
        return time.time() - self.last_activity_time

    def reset_idle_timer(self):
        """Reset the idle timer"""
        self.last_activity_time = time.time()

    def _monitor_loop(self):
        """Monitor keyboard events"""
        if not self.device_paths:
            return

        try:
            if HAS_EVDEV:
                self._monitor_evdev()
            else:
                self._monitor_raw()
        except Exception as e:
            print(f"Keyboard monitor error: {e}")

    def _monitor_evdev(self):
        """Monitor using evdev library"""
        # Open devices for reading
        open_devices = []
        for path in self.device_paths:
            try:
                dev = evdev.InputDevice(path)
                open_devices.append(dev)
            except:
                pass

        if not open_devices:
            return

        while self.running:
            try:
                # Use select to wait for events with timeout
                r, w, x = select.select([dev.fd for dev in open_devices], [], [], 0.5)

                for fd in r:
                    try:
                        for dev in open_devices:
                            if dev.fd == fd:
                                for event in dev.read():
                                    if event.type == ecodes.EV_KEY:
                                        self.last_activity_time = time.time()
                                        if self.activity_callback:
                                            self.activity_callback()
                                        break
                    except:
                        pass
            except:
                time.sleep(0.5)

        # Close devices
        for dev in open_devices:
            try:
                dev.close()
            except:
                pass

    def _monitor_raw(self):
        """Monitor using raw file reading (fallback)"""
        open_fds = []
        for path in self.device_paths[:3]:  # Limit to 3 devices
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                open_fds.append(fd)
            except:
                pass

        if not open_fds:
            return

        while self.running:
            try:
                r, w, x = select.select(open_fds, [], [], 0.5)

                for fd in r:
                    try:
                        # Read and discard events
                        os.read(fd, 4096)
                        self.last_activity_time = time.time()
                        if self.activity_callback:
                            self.activity_callback()
                    except:
                        pass
            except:
                time.sleep(0.5)

        for fd in open_fds:
            try:
                os.close(fd)
            except:
                pass


# ============================================================================
# KEYBOARD BACKLIGHT CONTROL - SysFS interface
# ============================================================================

class KeyboardBacklightController:
    """
    Controller for keyboard backlight via SysFS interface.
    Supports: white-only, 1-zone RGB, 3-zone RGB backlights.
    """

    # Backlight types (matching clevo_leds.h)
    BACKLIGHT_TYPE_NONE = 0x00
    BACKLIGHT_TYPE_FIXED_COLOR = 0x01  # White only
    BACKLIGHT_TYPE_3_ZONE_RGB = 0x02   # 3 zones RGB
    BACKLIGHT_TYPE_1_ZONE_RGB = 0x06   # Single zone RGB
    BACKLIGHT_TYPE_PER_KEY_RGB = 0xf3  # Per-key RGB

    # Preset colors
    PRESET_COLORS = {
        'WHITE':  (255, 255, 255),
        'RED':    (255,   0,   0),
        'GREEN':  (  0, 255,   0),
        'BLUE':   (  0,   0, 255),
        'YELLOW': (255, 255,   0),
        'CYAN':   (  0, 255, 255),
        'MAGENTA':(255,   0, 255),
        'ORANGE': (255, 165,   0),
        'PURPLE': (128,   0, 128),
        'PINK':   (255, 192, 203),
        'OFF':    (  0,   0,   0),
    }

    def __init__(self):
        self.backlight_type = self.BACKLIGHT_TYPE_NONE
        self.leds_base_path = "/sys/class/leds"
        self.kbd_backlight_paths = []
        self.max_brightness = 255
        self.num_zones = 0
        self.has_rgb = False
        # Saved state for fade/restore
        self.saved_brightness = 255
        self.saved_color = (255, 255, 255)
        self.detect_backlight()

    def detect_backlight(self):
        """Detect keyboard backlight type and available paths"""
        try:
            # Look for keyboard backlight LED devices
            led_dirs = glob.glob(os.path.join(self.leds_base_path, "*kbd_backlight*"))

            if not led_dirs:
                # Try alternative patterns
                led_dirs = glob.glob(os.path.join(self.leds_base_path, "*keyboard*"))
                led_dirs += glob.glob(os.path.join(self.leds_base_path, "rgb:*"))

            for led_dir in led_dirs:
                name = os.path.basename(led_dir)

                # Check for RGB multicolor
                multi_intensity_path = os.path.join(led_dir, "multi_intensity")
                if os.path.exists(multi_intensity_path):
                    self.has_rgb = True
                    self.kbd_backlight_paths.append(led_dir)
                else:
                    # White only backlight
                    self.kbd_backlight_paths.append(led_dir)

            # Remove duplicates
            self.kbd_backlight_paths = list(dict.fromkeys(self.kbd_backlight_paths))

            if not self.kbd_backlight_paths:
                self.backlight_type = self.BACKLIGHT_TYPE_NONE
                return

            # Determine backlight type
            first_path = self.kbd_backlight_paths[0]

            # Read max brightness
            max_brightness_path = os.path.join(first_path, "max_brightness")
            if os.path.exists(max_brightness_path):
                with open(max_brightness_path, 'r') as f:
                    self.max_brightness = int(f.read().strip())

            if self.has_rgb:
                if len(self.kbd_backlight_paths) >= 3:
                    self.backlight_type = self.BACKLIGHT_TYPE_3_ZONE_RGB
                    self.num_zones = 3
                else:
                    self.backlight_type = self.BACKLIGHT_TYPE_1_ZONE_RGB
                    self.num_zones = 1
            else:
                self.backlight_type = self.BACKLIGHT_TYPE_FIXED_COLOR
                self.num_zones = 1
                self.has_rgb = False

        except Exception as e:
            print(f"Error detecting keyboard backlight: {e}")
            self.backlight_type = self.BACKLIGHT_TYPE_NONE

    def is_available(self):
        """Check if keyboard backlight control is available"""
        return self.backlight_type != self.BACKLIGHT_TYPE_NONE

    def get_type_name(self):
        """Get human-readable backlight type name"""
        types = {
            self.BACKLIGHT_TYPE_NONE: "Not detected",
            self.BACKLIGHT_TYPE_FIXED_COLOR: "White only",
            self.BACKLIGHT_TYPE_1_ZONE_RGB: "1-Zone RGB",
            self.BACKLIGHT_TYPE_3_ZONE_RGB: "3-Zone RGB",
            self.BACKLIGHT_TYPE_PER_KEY_RGB: "Per-Key RGB",
        }
        return types.get(self.backlight_type, "Unknown")

    def get_brightness(self):
        """Get current brightness (0-max_brightness) - reads from system"""
        if not self.kbd_backlight_paths:
            return 0

        try:
            brightness_path = os.path.join(self.kbd_backlight_paths[0], "brightness")
            if os.path.exists(brightness_path):
                with open(brightness_path, 'r') as f:
                    return int(f.read().strip())
        except:
            pass
        return 0

    def set_brightness(self, brightness):
        """Set brightness for all zones"""
        if not self.kbd_backlight_paths:
            return False

        brightness = max(0, min(brightness, self.max_brightness))

        for path in self.kbd_backlight_paths:
            try:
                brightness_path = os.path.join(path, "brightness")
                if os.path.exists(brightness_path):
                    with open(brightness_path, 'w') as f:
                        f.write(str(brightness))
            except Exception as e:
                print(f"Error setting brightness for {path}: {e}")
                return False
        return True

    def get_color(self, zone=0):
        """Get current RGB color for a zone (returns tuple r,g,b)"""
        if not self.has_rgb or zone >= len(self.kbd_backlight_paths):
            return (255, 255, 255)

        try:
            multi_intensity_path = os.path.join(self.kbd_backlight_paths[zone], "multi_intensity")
            if os.path.exists(multi_intensity_path):
                with open(multi_intensity_path, 'r') as f:
                    values = f.read().strip().split()
                    if len(values) >= 3:
                        return (int(values[0]), int(values[1]), int(values[2]))
        except:
            pass
        return (255, 255, 255)

    def set_color(self, r, g, b, zone=None):
        """Set RGB color. If zone is None, set for all zones."""
        if not self.has_rgb:
            return False

        r = max(0, min(255, r))
        g = max(0, min(255, g))
        b = max(0, min(255, b))

        zones_to_set = [zone] if zone is not None else range(len(self.kbd_backlight_paths))

        for z in zones_to_set:
            if z >= len(self.kbd_backlight_paths):
                continue
            try:
                multi_intensity_path = os.path.join(self.kbd_backlight_paths[z], "multi_intensity")
                if os.path.exists(multi_intensity_path):
                    with open(multi_intensity_path, 'w') as f:
                        f.write(f"{r} {g} {b}")
            except Exception as e:
                print(f"Error setting color for zone {z}: {e}")
                return False
        return True

    def set_color_all_zones(self, r, g, b):
        """Set same color for all zones"""
        return self.set_color(r, g, b, zone=None)

    def save_state(self):
        """Save current brightness and color from system for later restoration"""
        self.saved_brightness = self.get_brightness()
        if self.has_rgb:
            self.saved_color = self.get_color(0)

    def restore_state(self):
        """Restore saved brightness and color"""
        self.set_brightness(self.saved_brightness)
        if self.has_rgb:
            self.set_color_all_zones(*self.saved_color)


# ============================================================================
# PASSWORD DIALOG
# ============================================================================

def ask_password():
    root = tk.Tk()
    root.title("Требуются права root")
    root.geometry("350x130")
    root.resizable(False, False)

    x = (root.winfo_screenwidth() - 350) // 2
    y = (root.winfo_screenheight() - 130) // 2
    root.geometry(f"+{x}+{y}")

    tk.Label(root, text="Программа запущена без sudo.\nВведите пароль пользователя:").pack(pady=10)

    pwd_var = tk.StringVar()
    entry = tk.Entry(root, textvariable=pwd_var, show="*", width=30)
    entry.pack(pady=5)
    entry.focus_set()

    def on_ok(event=None): root.quit()

    entry.bind('<Return>', on_ok)
    tk.Button(root, text="OK", command=on_ok, width=15).pack(pady=10)

    root.mainloop()
    pwd = pwd_var.get()
    root.destroy()
    return pwd


# ============================================================================
# MAIN APPLICATION
# ============================================================================

class NotebookControlApp:

    def __init__(self, root):
        self.root = root
        self.root.title("Notebook Control Center")
        self.root.geometry("1040x900")

        # Initialize all variables FIRST
        self.profile_name_var = tk.StringVar(value="Default")

        self.undervolt_vars = {
            'core': tk.IntVar(value=0),
            'cache': tk.IntVar(value=0),
            'gpu': tk.IntVar(value=0),
            'uncore': tk.IntVar(value=0),
            'analogio': tk.IntVar(value=0),
            'p1_power': tk.IntVar(value=0),
            'p1_time': tk.IntVar(value=0),
            'p2_power': tk.IntVar(value=0),
            'p2_time': tk.IntVar(value=0),
            'turbo_disable': tk.BooleanVar(value=False)
        }

        self.fan_vars = {
            'speed': tk.IntVar(value=50),
            'auto_control': tk.BooleanVar(value=False),
            'fan_select': tk.IntVar(value=0),
            'curve_mode': tk.IntVar(value=0),
            'view_mode': tk.IntVar(value=0),
            'editing_curve': tk.IntVar(value=0)
        }

        # Keyboard backlight variables
        self.kb_vars = {
            'brightness': tk.IntVar(value=255),
            'red': tk.IntVar(value=255),
            'green': tk.IntVar(value=255),
            'blue': tk.IntVar(value=255),
            'zone': tk.IntVar(value=0),
            'apply_all_zones': tk.BooleanVar(value=True),
            # Control options
            'control_brightness': tk.BooleanVar(value=True),  # If False, don't touch brightness
            # Auto-off timer variables
            'auto_off_enabled': tk.BooleanVar(value=False),
            'auto_off_timeout': tk.IntVar(value=30),
            # Fade options
            'fade_enabled': tk.BooleanVar(value=True),
            'fade_duration': tk.IntVar(value=500),  # milliseconds
        }

        # Apply on startup checkboxes
        self.undervolt_on_startup_var = tk.BooleanVar(value=False)
        self.fan_on_startup_var = tk.BooleanVar(value=False)
        self.kb_on_startup_var = tk.BooleanVar(value=False)

        # Fan curves
        self.fan_curve_common = FanCurve()
        self.fan_curve_cpu = FanCurve()
        self.fan_curve_gpu = FanCurve()

        self.fan_controller = FanController()
        self.kb_controller = KeyboardBacklightController()
        self.kb_monitor = KeyboardActivityMonitor()

        self.running = True
        self.is_11th_gen = False

        # Auto-off timer state
        self.kb_auto_off_active = False
        self.kb_was_turned_off = False
        self.auto_off_thread_running = False

        # Fade state
        self.fade_thread_running = False
        self.fade_target_brightness = 0
        self.fade_stop_flag = False  # Flag to interrupt ongoing fade

        # Check CPU gen
        self.check_cpu_gen()

        # Create UI
        self.create_ui()

        # Load settings
        self.load_settings()
        self.update_profile_list()

        # Start background tasks
        self.start_fan_thread()

        # Start brightness sync timer
        self.start_brightness_sync()

        # Read current undervolt values OR apply profile on startup
        if self.undervolt_on_startup_var.get():
            self.root.after(500, self.apply_on_startup)
        else:
            self.read_undervolt()

        # Apply keyboard backlight on startup
        if self.kb_on_startup_var.get():
            self.root.after(600, self.apply_kb_on_startup)

        # Start auto-off timer if enabled
        if self.kb_vars['auto_off_enabled'].get():
            self.root.after(700, self.start_kb_auto_off)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def apply_on_startup(self):
        """Apply settings on startup based on checkboxes"""
        if self.undervolt_on_startup_var.get():
            self.apply_undervolt()
        if self.fan_on_startup_var.get():
            self.fan_vars['auto_control'].set(True)
            self.status_var.set("Авто по кривой: ВКЛ (при старте)")

    def apply_kb_on_startup(self):
        """Apply keyboard backlight settings on startup"""
        if not self.kb_controller.is_available():
            return

        if self.kb_vars['control_brightness'].get():
            brightness = self.kb_vars['brightness'].get()
            self.kb_controller.set_brightness(brightness)

        if self.kb_controller.has_rgb:
            r = self.kb_vars['red'].get()
            g = self.kb_vars['green'].get()
            b = self.kb_vars['blue'].get()
            self.kb_controller.set_color_all_zones(r, g, b)

        self.status_var.set("Подсветка применена (при старте)")

    def check_cpu_gen(self):
        try:
            with open('/proc/cpuinfo', 'r') as f:
                if '11th Gen' in f.read() or 'Tiger Lake' in f.read():
                    self.is_11th_gen = True
        except:
            pass

    def create_ui(self):
        # Notebook for tabs
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # Tab 1: Undervolt
        self.undervolt_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.undervolt_frame, text="  Undervolt  ")
        self.create_undervolt_tab()

        # Tab 2: Fan Control
        self.fan_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.fan_frame, text="  Fan Control  ")
        self.create_fan_tab()

        # Tab 3: Keyboard Backlight
        self.kb_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.kb_frame, text="  Keyboard Backlight  ")
        self.create_keyboard_tab()

        # Status bar
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W).pack(fill=tk.X, side=tk.BOTTOM)

    # ===================== UNDERVOLT TAB =====================

    def create_undervolt_tab(self):
        top = ttk.Frame(self.undervolt_frame)
        top.pack(fill=tk.X, padx=10, pady=5)

        ttk.Button(top, text="Обновить (Read)", command=self.read_undervolt).pack(side=tk.LEFT, padx=5)
        ttk.Separator(top, orient='vertical').pack(side=tk.LEFT, fill=tk.Y, padx=10)

        ttk.Label(top, text="Профиль:").pack(side=tk.LEFT, padx=2)
        self.combo_profiles = ttk.Combobox(top, textvariable=self.profile_name_var, width=20)
        self.combo_profiles.pack(side=tk.LEFT, padx=5)
        ttk.Button(top, text="Загрузить", command=self.load_profile).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="Сохранить", command=self.save_profile).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="Удалить", command=self.delete_profile).pack(side=tk.LEFT, padx=2)

        ttk.Checkbutton(top, text="При старте", variable=self.undervolt_on_startup_var).pack(side=tk.RIGHT, padx=10)

        log_frame = ttk.LabelFrame(self.undervolt_frame, text="Вывод (undervolt --read)")
        log_frame.pack(fill=tk.X, padx=10, pady=5)

        self.log_area = scrolledtext.ScrolledText(log_frame, height=8, state='disabled')
        self.log_area.pack(fill=tk.X, padx=5, pady=5)

        volts_frame = ttk.LabelFrame(self.undervolt_frame, text="Смещение напряжения (mV)")
        volts_frame.pack(fill=tk.X, padx=10, pady=5)

        grid = ttk.Frame(volts_frame)
        grid.pack(fill=tk.X, padx=5, pady=5)

        labels = [('Core', 'core'), ('Cache', 'cache'), ('GPU', 'gpu'), ('Uncore', 'uncore'), ('AnalogIO', 'analogio')]
        for i, (lbl, key) in enumerate(labels):
            ttk.Label(grid, text=f"{lbl}:").grid(row=i//2, column=(i%2)*2, padx=5, pady=2, sticky='e')
            entry = ttk.Entry(grid, textvariable=self.undervolt_vars[key], width=10)
            entry.grid(row=i//2, column=(i%2)*2+1, padx=5, pady=2)
            if self.is_11th_gen and key != 'core':
                entry.config(state='disabled')

        if self.is_11th_gen:
            ttk.Label(volts_frame, text="ВНИМАНИЕ: 11-е поколение Intel - доступен только Core", foreground="red").pack(pady=5)

        power_frame = ttk.LabelFrame(self.undervolt_frame, text="Power Limits")
        power_frame.pack(fill=tk.X, padx=10, pady=5)

        pgrid = ttk.Frame(power_frame)
        pgrid.pack(fill=tk.X, padx=5, pady=5)

        ttk.Label(pgrid, text="PL1 (Long):").grid(row=0, column=0, padx=5, sticky='e')
        ttk.Entry(pgrid, textvariable=self.undervolt_vars['p1_power'], width=8).grid(row=0, column=1)
        ttk.Label(pgrid, text="W").grid(row=0, column=2)
        ttk.Entry(pgrid, textvariable=self.undervolt_vars['p1_time'], width=8).grid(row=0, column=3)
        ttk.Label(pgrid, text="sec").grid(row=0, column=4)

        ttk.Label(pgrid, text="PL2 (Short):").grid(row=1, column=0, padx=5, sticky='e')
        ttk.Entry(pgrid, textvariable=self.undervolt_vars['p2_power'], width=8).grid(row=1, column=1)
        ttk.Label(pgrid, text="W").grid(row=1, column=2)
        ttk.Entry(pgrid, textvariable=self.undervolt_vars['p2_time'], width=8).grid(row=1, column=3)
        ttk.Label(pgrid, text="sec").grid(row=1, column=4)

        misc_frame = ttk.LabelFrame(self.undervolt_frame, text="Прочее")
        misc_frame.pack(fill=tk.X, padx=10, pady=5)
        ttk.Checkbutton(misc_frame, text="Отключить Turbo Boost", variable=self.undervolt_vars['turbo_disable']).pack(anchor=tk.W, padx=10, pady=5)

        ttk.Button(self.undervolt_frame, text="ПРИМЕНИТЬ НАСТРОЙКИ", command=self.apply_undervolt).pack(fill=tk.X, padx=10, pady=15, ipady=10)

    def parse_undervolt_output(self, text):
        for line in text.split('\n'):
            m = re.search(r'(\w+):\s*([\-\d\.]+)\s*mV', line)
            if m and m.group(1).lower() in self.undervolt_vars:
                self.undervolt_vars[m.group(1).lower()].set(int(float(m.group(2))))

            if "turbo:" in line:
                self.undervolt_vars['turbo_disable'].set("disable" in line)

            m = re.search(r'powerlimit:\s*([\d\.]+)W.*short:\s*([\d\.]+)s.*\/\s*([\d\.]+)W.*long:\s*([\d\.]+)s', line)
            if m:
                self.undervolt_vars['p2_power'].set(int(float(m.group(1))))
                self.undervolt_vars['p2_time'].set(int(float(m.group(2))))
                self.undervolt_vars['p1_power'].set(int(float(m.group(3))))
                self.undervolt_vars['p1_time'].set(int(float(m.group(4))))

    def read_undervolt(self):
        try:
            result = subprocess.run(['undervolt', '--read'], capture_output=True, text=True, check=True)
            self.log_area.config(state='normal')
            self.log_area.delete(1.0, tk.END)
            self.log_area.insert(tk.END, result.stdout)
            self.log_area.config(state='disabled')
            self.parse_undervolt_output(result.stdout)
        except FileNotFoundError:
            self.log_area.config(state='normal')
            self.log_area.delete(1.0, tk.END)
            self.log_area.insert(tk.END, "Программа 'undervolt' не найдена.\nУстановите: pip install undervolt")
            self.log_area.config(state='disabled')
        except subprocess.CalledProcessError as e:
            self.log_area.config(state='normal')
            self.log_area.delete(1.0, tk.END)
            self.log_area.insert(tk.END, f"Ошибка:\n{e.stderr}")
            self.log_area.config(state='disabled')

    def apply_undervolt(self):
        cmd = ['undervolt']

        def get_int(var):
            try: return int(var.get())
            except: return 0

        core = get_int(self.undervolt_vars['core'])
        if core != 0: cmd.extend(['--core', str(core)])

        if not self.is_11th_gen:
            for p in ['cache', 'gpu', 'uncore', 'analogio']:
                v = get_int(self.undervolt_vars[p])
                if v != 0: cmd.extend([f'--{p}', str(v)])

        p1_p, p1_t = get_int(self.undervolt_vars['p1_power']), get_int(self.undervolt_vars['p1_time'])
        if p1_p > 0 and p1_t > 0: cmd.extend(['-p1', str(p1_p), str(p1_t)])

        p2_p, p2_t = get_int(self.undervolt_vars['p2_power']), get_int(self.undervolt_vars['p2_time'])
        if p2_p > 0 and p2_t > 0: cmd.extend(['-p2', str(p2_p), str(p2_t)])

        cmd.extend(['--turbo', '1' if self.undervolt_vars['turbo_disable'].get() else '0'])

        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                self.status_var.set("Undervolt применён")
                self.read_undervolt()
            else:
                self.status_var.set(f"Ошибка: {result.stderr[:50]}")
        except Exception as e:
            self.status_var.set(f"Ошибка: {e}")

    # ===================== FAN CONTROL TAB =====================

    def create_fan_tab(self):
        left = ttk.Frame(self.fan_frame)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)

        plat = self.fan_controller.platform.upper() if self.fan_controller.platform else "Not detected"
        ttk.Label(left, text=f"Platform: {plat}", font=('TkDefaultFont', 10, 'bold')).pack(anchor=tk.W, pady=5)

        status_frame = ttk.LabelFrame(left, text="Текущий статус")
        status_frame.pack(fill=tk.X, pady=5)

        self.fan_labels = {}
        for i in range(2):
            f = ttk.Frame(status_frame)
            f.pack(fill=tk.X, padx=5, pady=2)
            ttk.Label(f, text=f"{'CPU' if i==0 else 'GPU'}:").pack(side=tk.LEFT)
            self.fan_labels[i] = ttk.Label(f, text="--% @ --°C", font=('TkDefaultFont', 10))
            self.fan_labels[i].pack(side=tk.RIGHT)

        manual_frame = ttk.LabelFrame(left, text="Ручное управление")
        manual_frame.pack(fill=tk.X, pady=5)

        fan_sel = ttk.Frame(manual_frame)
        fan_sel.pack(fill=tk.X, padx=5, pady=2)
        ttk.Label(fan_sel, text="Вентилятор:").pack(side=tk.LEFT)
        ttk.Radiobutton(fan_sel, text="CPU", variable=self.fan_vars['fan_select'], value=0).pack(side=tk.LEFT, padx=5)
        ttk.Radiobutton(fan_sel, text="GPU", variable=self.fan_vars['fan_select'], value=1).pack(side=tk.LEFT, padx=5)
        ttk.Radiobutton(fan_sel, text="Оба", variable=self.fan_vars['fan_select'], value=2).pack(side=tk.LEFT, padx=5)

        speed_frame = ttk.Frame(manual_frame)
        speed_frame.pack(fill=tk.X, padx=5, pady=2)
        ttk.Label(speed_frame, text="Скорость:").pack(side=tk.LEFT)

        self.speed_scale = ttk.Scale(speed_frame, from_=0, to=100, variable=self.fan_vars['speed'], orient=tk.HORIZONTAL, command=self.on_speed_change)
        self.speed_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)

        self.speed_label = ttk.Label(speed_frame, text="50%", width=5)
        self.speed_label.pack(side=tk.RIGHT)

        btn_frame = ttk.Frame(manual_frame)
        btn_frame.pack(fill=tk.X, padx=5, pady=5)
        ttk.Button(btn_frame, text="Применить", command=self.apply_fan_speed).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="Авто", command=self.set_fan_auto).pack(side=tk.LEFT, padx=2)

        auto_frame = ttk.LabelFrame(left, text="Автоматическое управление")
        auto_frame.pack(fill=tk.X, pady=5)

        ttk.Checkbutton(auto_frame, text="Управление по кривой", variable=self.fan_vars['auto_control'], command=self.toggle_auto).pack(anchor=tk.W, padx=5, pady=2)

        mode_frame = ttk.Frame(auto_frame)
        mode_frame.pack(fill=tk.X, padx=5, pady=2)
        ttk.Label(mode_frame, text="Режим:").pack(side=tk.LEFT)
        ttk.Radiobutton(mode_frame, text="Общая кривая", variable=self.fan_vars['curve_mode'], value=0, command=self.on_curve_mode_change).pack(side=tk.LEFT, padx=5)
        ttk.Radiobutton(mode_frame, text="Раздельные", variable=self.fan_vars['curve_mode'], value=1, command=self.on_curve_mode_change).pack(side=tk.LEFT)

        ttk.Checkbutton(auto_frame, text="При старте", variable=self.fan_on_startup_var).pack(anchor=tk.W, padx=5, pady=5)

        right = ttk.Frame(self.fan_frame)
        right.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=5, pady=5)

        curve_header = ttk.Frame(right)
        curve_header.pack(fill=tk.X)

        ttk.Label(curve_header, text="Кривая вентилятора:", font=('TkDefaultFont', 10, 'bold')).pack(side=tk.LEFT)

        ttk.Radiobutton(curve_header, text="Список", variable=self.fan_vars['view_mode'], value=0, command=self.on_view_mode_change).pack(side=tk.LEFT, padx=10)
        ttk.Radiobutton(curve_header, text="График", variable=self.fan_vars['view_mode'], value=1, command=self.on_view_mode_change).pack(side=tk.LEFT)

        self.curve_selector_frame = ttk.Frame(curve_header)
        ttk.Label(self.curve_selector_frame, text="   Редактировать:").pack(side=tk.LEFT)
        ttk.Radiobutton(self.curve_selector_frame, text="CPU", variable=self.fan_vars['editing_curve'], value=0, command=self.on_editing_curve_change).pack(side=tk.LEFT, padx=5)
        ttk.Radiobutton(self.curve_selector_frame, text="GPU", variable=self.fan_vars['editing_curve'], value=1, command=self.on_editing_curve_change).pack(side=tk.LEFT)
        self.curve_selector_frame.pack(side=tk.RIGHT)

        self.curve_container = ttk.LabelFrame(right, text="")
        self.curve_container.pack(fill=tk.BOTH, expand=True, pady=5)

        self.create_curve_list_view()
        if HAS_MATPLOTLIB:
            self.create_curve_graph_view()

        self.show_curve_view()

        curve_btns = ttk.Frame(right)
        curve_btns.pack(fill=tk.X, pady=5)
        ttk.Button(curve_btns, text="Изменить", command=self.edit_curve_point).pack(side=tk.LEFT, padx=2)
        ttk.Button(curve_btns, text="Добавить", command=self.add_curve_point).pack(side=tk.LEFT, padx=2)
        ttk.Button(curve_btns, text="Удалить", command=self.remove_curve_point).pack(side=tk.LEFT, padx=2)
        ttk.Button(curve_btns, text="Сброс", command=self.reset_curve).pack(side=tk.LEFT, padx=2)
        ttk.Button(curve_btns, text="Сохранить", command=self.save_fan_settings).pack(side=tk.LEFT, padx=10)

        self.on_curve_mode_change()

    def create_curve_list_view(self):
        self.curve_list_frame = ttk.Frame(self.curve_container)

        columns = ('temp', 'speed')
        self.curve_tree = ttk.Treeview(self.curve_list_frame, columns=columns, show='headings', height=12)
        self.curve_tree.heading('temp', text='Температура (°C)')
        self.curve_tree.heading('speed', text='Скорость (%)')
        self.curve_tree.column('temp', width=120)
        self.curve_tree.column('speed', width=120)
        self.curve_tree.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.update_curve_list()

    def create_curve_graph_view(self):
        self.curve_graph_frame = ttk.Frame(self.curve_container)

        self.fig = Figure(figsize=(5, 4), dpi=90)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_xlabel("Температура (°C)")
        self.ax.set_ylabel("Скорость (%)")
        self.ax.set_xlim(20, 100)
        self.ax.set_ylim(0, 105)
        self.ax.grid(True, alpha=0.3)

        self.canvas = FigureCanvasTkAgg(self.fig, self.curve_graph_frame)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        self.canvas.mpl_connect('button_release_event', self.on_graph_click)

        self.update_curve_graph()

    def show_curve_view(self):
        self.curve_list_frame.pack_forget()
        if HAS_MATPLOTLIB:
            self.curve_graph_frame.pack_forget()

        if self.fan_vars['view_mode'].get() == 0:
            self.curve_list_frame.pack(fill=tk.BOTH, expand=True)
        else:
            if HAS_MATPLOTLIB:
                self.curve_graph_frame.pack(fill=tk.BOTH, expand=True)

        self.update_curve_display()

    def on_view_mode_change(self):
        self.show_curve_view()

    def on_curve_mode_change(self):
        if self.fan_vars['curve_mode'].get() == 0:
            self.curve_selector_frame.pack_forget()
        else:
            self.curve_selector_frame.pack(side=tk.RIGHT)
        self.update_curve_display()

    def on_editing_curve_change(self):
        self.update_curve_display()

    def get_current_curve(self):
        if self.fan_vars['curve_mode'].get() == 0:
            return self.fan_curve_common
        else:
            if self.fan_vars['editing_curve'].get() == 0:
                return self.fan_curve_cpu
            else:
                return self.fan_curve_gpu

    def set_current_curve(self, curve):
        if self.fan_vars['curve_mode'].get() == 0:
            self.fan_curve_common = curve
        else:
            if self.fan_vars['editing_curve'].get() == 0:
                self.fan_curve_cpu = curve
            else:
                self.fan_curve_gpu = curve

    def update_curve_display(self):
        self.update_curve_list()
        if HAS_MATPLOTLIB:
            self.update_curve_graph()

    def update_curve_list(self):
        for item in self.curve_tree.get_children():
            self.curve_tree.delete(item)

        curve = self.get_current_curve()
        for temp, speed in curve.points:
            self.curve_tree.insert('', tk.END, values=(temp, speed))

    def update_curve_graph(self):
        if not HAS_MATPLOTLIB:
            return

        curve = self.get_current_curve()

        self.ax.clear()
        self.ax.set_xlabel("Температура (°C)")
        self.ax.set_ylabel("Скорость (%)")
        self.ax.set_xlim(20, 100)
        self.ax.set_ylim(0, 105)
        self.ax.grid(True, alpha=0.3)

        temps = [p[0] for p in curve.points]
        speeds = [p[1] for p in curve.points]

        self.ax.plot(temps, speeds, 'b-', linewidth=2)
        self.ax.plot(temps, speeds, 'ro', markersize=8)
        self.ax.fill_between(temps, speeds, alpha=0.2)

        self.fig.tight_layout()
        self.canvas.draw()

    def on_graph_click(self, event):
        if not HAS_MATPLOTLIB:
            return

        if event.inaxes != self.ax:
            return

        if event.xdata is None or event.ydata is None:
            return

        click_temp, click_speed = event.xdata, event.ydata

        curve = self.get_current_curve()
        min_dist = float('inf')
        closest_idx = -1

        for i, (t, s) in enumerate(curve.points):
            dist = (t - click_temp) ** 2 + (s - click_speed) ** 2
            if dist < min_dist:
                min_dist = dist
                closest_idx = i

        if min_dist < 100 and closest_idx >= 0:
            self.root.after(10, lambda: self.open_edit_dialog(closest_idx))

    def on_speed_change(self, val):
        self.speed_label.config(text=f"{int(float(val))}%")

    def apply_fan_speed(self):
        if not self.fan_controller.platform:
            self.status_var.set("Fan controller не подключен")
            return

        # 1. Отключаем управление по кривой, чтобы не перезаписывало через секунду
        self.fan_vars['auto_control'].set(False)

        # 2. Отключаем аппаратный авто-режим (переход в ручной)
        self.fan_controller.set_manual_mode()

        fan_id = self.fan_vars['fan_select'].get()
        speed = self.fan_vars['speed'].get()

        # 3. Применяем скорость
        if fan_id == 2:
            self.fan_controller.set_fan_speed(0, speed)
            self.fan_controller.set_fan_speed(1, speed)
            self.status_var.set(f"Both fans: {speed}% (Manual)")
        else:
            self.fan_controller.set_fan_speed(fan_id, speed)
            self.status_var.set(f"Fan {fan_id}: {speed}% (Manual)")

    def set_fan_auto(self):
        if self.fan_controller.platform:
            self.fan_vars['auto_control'].set(False) # Также отключаем программную кривую
            self.fan_controller.set_auto()
            self.status_var.set("Fans: Auto mode")

    def toggle_auto(self):
        if self.fan_vars['auto_control'].get():
            # Включаем программный режим по кривой
            # ВАЖНО: Отключаем аппаратный авто-режим, чтобы он не сбивал скорость
            if self.fan_controller.platform:
                self.fan_controller.set_manual_mode()
            self.status_var.set("Авто по кривой: ВКЛ")
        else:
            # Выключаем программный режим, возвращаем управление железу
            if self.fan_controller.platform:
                self.fan_controller.set_auto()
            self.status_var.set("Авто по кривой: ВЫКЛ (hardware auto)")

    def edit_curve_point(self):
        sel = self.curve_tree.selection()
        if not sel:
            self.status_var.set("Выберите точку для редактирования")
            return
        idx = self.curve_tree.index(sel[0])
        self.open_edit_dialog(idx)

    def open_edit_dialog(self, idx):
        curve = self.get_current_curve()
        if idx < 0 or idx >= len(curve.points):
            return

        temp, speed = curve.points[idx]

        dlg = tk.Toplevel(self.root)
        dlg.title(f"Точка {idx+1}")
        dlg.geometry("300x200")
        dlg.transient(self.root)
        dlg.resizable(False, False)

        dlg.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - 300) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - 180) // 2
        dlg.geometry(f"+{x}+{y}")

        main_frame = ttk.Frame(dlg, padding=20)
        main_frame.pack(fill=tk.BOTH, expand=True)

        temp_frame = ttk.Frame(main_frame)
        temp_frame.pack(fill=tk.X, pady=10)
        ttk.Label(temp_frame, text="Температура (°C):", width=18).pack(side=tk.LEFT)
        temp_var = tk.IntVar(value=temp)
        ttk.Spinbox(temp_frame, from_=20, to=95, textvariable=temp_var, width=10).pack(side=tk.RIGHT)

        speed_frame = ttk.Frame(main_frame)
        speed_frame.pack(fill=tk.X, pady=10)
        ttk.Label(speed_frame, text="Скорость (%):", width=18).pack(side=tk.LEFT)
        speed_var = tk.IntVar(value=speed)
        ttk.Spinbox(speed_frame, from_=0, to=100, textvariable=speed_var, width=10).pack(side=tk.RIGHT)

        def save():
            curve.points[idx] = (temp_var.get(), speed_var.get())
            curve.points.sort(key=lambda p: p[0])
            self.update_curve_display()
            dlg.destroy()

        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=15)
        ttk.Button(btn_frame, text="Сохранить", command=save, width=12).pack(side=tk.LEFT, padx=10)
        ttk.Button(btn_frame, text="Отмена", command=dlg.destroy, width=12).pack(side=tk.RIGHT, padx=10)

    def add_curve_point(self):
        curve = self.get_current_curve()
        if len(curve.points) >= 15:
            self.status_var.set("Максимум 15 точек")
            return

        for i in range(len(curve.points) - 1):
            t1, s1 = curve.points[i]
            t2, s2 = curve.points[i + 1]
            if t2 - t1 > 5:
                curve.points.insert(i + 1, ((t1+t2)//2, (s1+s2)//2))
                break

        self.update_curve_display()

    def remove_curve_point(self):
        curve = self.get_current_curve()
        if len(curve.points) <= 2:
            return
        sel = self.curve_tree.selection()
        if sel:
            idx = self.curve_tree.index(sel[0])
            del curve.points[idx]
            self.update_curve_display()

    def reset_curve(self):
        curve = self.get_current_curve()
        curve.points = list(FanCurve.DEFAULT)
        self.update_curve_display()

    def save_fan_settings(self):
        self.save_settings()
        self.status_var.set("Кривые сохранены")

    # ===================== KEYBOARD BACKLIGHT TAB =====================

    def create_keyboard_tab(self):
        """Create keyboard backlight control tab"""
        # Main container - using grid for better layout control
        main_container = ttk.Frame(self.kb_frame, padding=10)
        main_container.pack(fill=tk.BOTH, expand=True)

        # Configure grid weights
        main_container.columnconfigure(0, weight=1)
        main_container.columnconfigure(1, weight=1)
        main_container.rowconfigure(0, weight=0)
        main_container.rowconfigure(1, weight=0)
        main_container.rowconfigure(2, weight=0)

        # Top section - status and type
        top_frame = ttk.Frame(main_container)
        top_frame.grid(row=0, column=0, columnspan=2, sticky='ew', pady=5)

        kb_type = self.kb_controller.get_type_name()
        ttk.Label(top_frame, text=f"Тип подсветки: {kb_type}",
                  font=('TkDefaultFont', 11, 'bold')).pack(side=tk.LEFT)

        if not self.kb_controller.is_available():
            ttk.Label(top_frame, text="  (Подсветка не обнаружена)",
                      foreground="orange").pack(side=tk.LEFT)
            info_frame = ttk.LabelFrame(main_container, text="Информация")
            info_frame.grid(row=1, column=0, columnspan=2, sticky='ew', pady=10)
            ttk.Label(info_frame, text="Подсветка клавиатуры не обнаружена.\n\n"
                      "Убедитесь, что модуль tuxedo_keyboard загружен:\n"
                      "  sudo modprobe tuxedo_keyboard\n\n"
                      "Или установите драйвер clevo-keyboard:\n"
                      "  https://github.com/wessel-novacustom/clevo-keyboard",
                      justify=tk.LEFT).pack(padx=10, pady=10)
            return

        # Left panel - Brightness and control options
        left_panel = ttk.Frame(main_container)
        left_panel.grid(row=1, column=0, sticky='nsew', padx=(0, 10), pady=5)

        # Brightness control
        brightness_frame = ttk.LabelFrame(left_panel, text="Яркость")
        brightness_frame.pack(fill=tk.X, pady=5)

        bright_inner = ttk.Frame(brightness_frame)
        bright_inner.pack(fill=tk.X, padx=10, pady=10)

        ttk.Label(bright_inner, text="Яркость:").pack(side=tk.LEFT)

        self.kb_brightness_scale = ttk.Scale(
            bright_inner, from_=0, to=self.kb_controller.max_brightness,
            variable=self.kb_vars['brightness'], orient=tk.HORIZONTAL,
            command=self.on_kb_brightness_change
        )
        self.kb_brightness_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=10)

        self.kb_brightness_label = ttk.Label(bright_inner, text="100%", width=6)
        self.kb_brightness_label.pack(side=tk.RIGHT)

        # Control options
        control_frame = ttk.LabelFrame(left_panel, text="Управление")
        control_frame.pack(fill=tk.X, pady=5)

        ttk.Checkbutton(control_frame, text="Управлять яркостью",
                       variable=self.kb_vars['control_brightness'],
                       command=self.on_control_brightness_toggle).pack(anchor=tk.W, padx=10, pady=5)

        ttk.Label(control_frame, text="(Если выключено - яркость не изменяется программой)",
                  foreground="gray", justify=tk.LEFT).pack(padx=10, pady=(0, 5))

        # Auto-off timer section
        auto_off_frame = ttk.LabelFrame(left_panel, text="Автовыключение при бездействии")
        auto_off_frame.pack(fill=tk.X, pady=5)

        auto_off_inner = ttk.Frame(auto_off_frame)
        auto_off_inner.pack(fill=tk.X, padx=10, pady=5)

        ttk.Checkbutton(auto_off_inner, text="Выключать подсветку через",
                       variable=self.kb_vars['auto_off_enabled'],
                       command=self.on_auto_off_toggle).pack(side=tk.LEFT)

        timeout_spin = ttk.Spinbox(auto_off_inner, from_=5, to=300, increment=5,
                                   textvariable=self.kb_vars['auto_off_timeout'], width=5)
        timeout_spin.pack(side=tk.LEFT, padx=5)

        ttk.Label(auto_off_inner, text="секунд").pack(side=tk.LEFT)

        # Status for auto-off
        self.auto_off_status_label = ttk.Label(auto_off_frame, text="", foreground="gray")
        self.auto_off_status_label.pack(anchor=tk.W, padx=10, pady=2)

        # Warning about evdev
        if not HAS_EVDEV:
            ttk.Label(auto_off_frame, text="Для работы таймера установите: pip install evdev",
                     foreground="orange").pack(anchor=tk.W, padx=10, pady=2)
        elif not self.kb_monitor.is_available():
            ttk.Label(auto_off_frame, text="Клавиатуры не обнаружены для мониторинга",
                     foreground="orange").pack(anchor=tk.W, padx=10, pady=2)

        # Fade options
        fade_frame = ttk.LabelFrame(left_panel, text="Плавное включение/выключение")
        fade_frame.pack(fill=tk.X, pady=5)

        fade_inner = ttk.Frame(fade_frame)
        fade_inner.pack(fill=tk.X, padx=10, pady=5)

        ttk.Checkbutton(fade_inner, text="Плавное затухание",
                       variable=self.kb_vars['fade_enabled']).pack(side=tk.LEFT)

        ttk.Label(fade_inner, text="Время:").pack(side=tk.LEFT, padx=(10, 0))

        ttk.Spinbox(fade_inner, from_=100, to=2000, increment=100,
                   textvariable=self.kb_vars['fade_duration'], width=5).pack(side=tk.LEFT, padx=5)

        ttk.Label(fade_inner, text="мс").pack(side=tk.LEFT)

        # Right panel - Color selection (RGB only)
        if self.kb_controller.has_rgb:
            right_panel = ttk.Frame(main_container)
            right_panel.grid(row=1, column=1, sticky='nsew', pady=5)

            # RGB Sliders
            rgb_frame = ttk.LabelFrame(right_panel, text="Цвет (RGB)")
            rgb_frame.pack(fill=tk.X, pady=5)

            # Red slider
            red_frame = ttk.Frame(rgb_frame)
            red_frame.pack(fill=tk.X, padx=10, pady=5)
            ttk.Label(red_frame, text="Красный:", width=10).pack(side=tk.LEFT)
            self.red_scale = ttk.Scale(red_frame, from_=0, to=255, variable=self.kb_vars['red'],
                                       orient=tk.HORIZONTAL, command=self.on_kb_color_change)
            self.red_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
            self.red_label = ttk.Label(red_frame, text="255", width=4)
            self.red_label.pack(side=tk.RIGHT)

            # Green slider
            green_frame = ttk.Frame(rgb_frame)
            green_frame.pack(fill=tk.X, padx=10, pady=5)
            ttk.Label(green_frame, text="Зелёный:", width=10).pack(side=tk.LEFT)
            self.green_scale = ttk.Scale(green_frame, from_=0, to=255, variable=self.kb_vars['green'],
                                         orient=tk.HORIZONTAL, command=self.on_kb_color_change)
            self.green_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
            self.green_label = ttk.Label(green_frame, text="255", width=4)
            self.green_label.pack(side=tk.RIGHT)

            # Blue slider
            blue_frame = ttk.Frame(rgb_frame)
            blue_frame.pack(fill=tk.X, padx=10, pady=5)
            ttk.Label(blue_frame, text="Синий:", width=10).pack(side=tk.LEFT)
            self.blue_scale = ttk.Scale(blue_frame, from_=0, to=255, variable=self.kb_vars['blue'],
                                        orient=tk.HORIZONTAL, command=self.on_kb_color_change)
            self.blue_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
            self.blue_label = ttk.Label(blue_frame, text="255", width=4)
            self.blue_label.pack(side=tk.RIGHT)

            # Color preview
            preview_frame = ttk.Frame(rgb_frame)
            preview_frame.pack(fill=tk.X, padx=10, pady=10)

            ttk.Label(preview_frame, text="Предпросмотр:").pack(side=tk.LEFT)
            self.color_preview = tk.Canvas(preview_frame, width=60, height=25, bg='white',
                                           highlightthickness=1, highlightbackground='gray')
            self.color_preview.pack(side=tk.LEFT, padx=10)

            self.hex_label = ttk.Label(preview_frame, text="#FFFFFF", font=('TkDefaultFont', 10, 'bold'))
            self.hex_label.pack(side=tk.LEFT, padx=10)

            # Read current color
            self.load_current_color()

            # Preset colors
            preset_frame = ttk.LabelFrame(right_panel, text="Предустановленные цвета")
            preset_frame.pack(fill=tk.X, pady=5)

            colors_grid = ttk.Frame(preset_frame)
            colors_grid.pack(padx=10, pady=10)

            row, col = 0, 0
            for name, (r, g, b) in self.kb_controller.PRESET_COLORS.items():
                hex_color = f'#{r:02x}{g:02x}{b:02x}'
                btn = tk.Button(colors_grid, bg=hex_color, width=3, height=1,
                               relief=tk.RAISED, bd=2,
                               command=lambda r=r, g=g, b=b: self.set_preset_color(r, g, b))
                btn.grid(row=row, column=col, padx=2, pady=2)
                self.create_tooltip(btn, name)
                col += 1
                if col > 5:
                    col = 0
                    row += 1

        # Bottom section - Apply buttons and startup option (fixed at bottom)
        bottom_frame = ttk.Frame(main_container)
        bottom_frame.grid(row=2, column=0, columnspan=2, sticky='ew', pady=10)

        ttk.Checkbutton(bottom_frame, text="Применять при старте",
                       variable=self.kb_on_startup_var).pack(side=tk.LEFT, padx=10)

        btn_frame = ttk.Frame(bottom_frame)
        btn_frame.pack(side=tk.RIGHT)

        ttk.Button(btn_frame, text="Применить цвет", command=self.apply_kb_color,
                  width=14).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Сохранить настройки", command=self.save_kb_settings,
                  width=18).pack(side=tk.LEFT, padx=5)

        # Read current brightness from system
        self.sync_brightness_from_system()

    def create_tooltip(self, widget, text):
        """Create a simple tooltip for a widget"""
        def show_tooltip(event):
            tooltip = tk.Toplevel(widget)
            tooltip.wm_overrideredirect(True)
            tooltip.wm_geometry(f"+{event.x_root+10}+{event.y_root+10}")
            label = ttk.Label(tooltip, text=text, background="lightyellow",
                             relief="solid", borderwidth=1, padding=2)
            label.pack()
            widget.tooltip = tooltip
            widget.after(2000, lambda: tooltip.destroy() if tooltip.winfo_exists() else None)

        def hide_tooltip(event):
            if hasattr(widget, 'tooltip'):
                widget.tooltip.destroy()

        widget.bind('<Enter>', show_tooltip)
        widget.bind('<Leave>', hide_tooltip)

    def sync_brightness_from_system(self):
        """Read current brightness from system and update UI"""
        if not self.kb_controller.is_available():
            return

        current_brightness = self.kb_controller.get_brightness()
        self.kb_vars['brightness'].set(current_brightness)
        self.on_kb_brightness_change(current_brightness)

    def on_control_brightness_toggle(self):
        """Handle control brightness checkbox toggle"""
        if self.kb_vars['control_brightness'].get():
            # When enabling control, sync from system
            self.sync_brightness_from_system()
            self.status_var.set("Управление яркостью: ВКЛ")
        else:
            self.status_var.set("Управление яркостью: ВЫКЛ")

    def load_current_color(self):
        """Load current color from hardware"""
        zone = self.kb_vars['zone'].get() if self.kb_controller.backlight_type == self.kb_controller.BACKLIGHT_TYPE_3_ZONE_RGB else 0
        r, g, b = self.kb_controller.get_color(zone)
        self.kb_vars['red'].set(r)
        self.kb_vars['green'].set(g)
        self.kb_vars['blue'].set(b)
        self.update_color_preview()

    def on_kb_brightness_change(self, val):
        """Handle brightness slider change"""
        brightness = int(float(val))
        if self.kb_controller.max_brightness <= 3:
            self.kb_brightness_label.config(text=str(brightness))
        else:
            percent = round(brightness * 100 / self.kb_controller.max_brightness)
            self.kb_brightness_label.config(text=f"{percent}%")

        # Apply brightness in real-time if control is enabled and not during fade
        if self.kb_vars['control_brightness'].get() and not self.fade_thread_running:
            self.kb_controller.set_brightness(brightness)

    def on_kb_color_change(self, val=None):
        """Handle RGB slider change"""
        self.update_color_preview()

    def on_kb_zone_change(self):
        """Handle zone selection change - load color for that zone"""
        self.load_current_color()

    def update_color_preview(self):
        """Update the color preview canvas"""
        r = self.kb_vars['red'].get()
        g = self.kb_vars['green'].get()
        b = self.kb_vars['blue'].get()

        hex_color = f'#{r:02x}{g:02x}{b:02x}'
        self.color_preview.config(bg=hex_color)
        self.hex_label.config(text=hex_color.upper())

        self.red_label.config(text=str(r))
        self.green_label.config(text=str(g))
        self.blue_label.config(text=str(b))

    def set_preset_color(self, r, g, b):
        """Set color from preset"""
        self.kb_vars['red'].set(r)
        self.kb_vars['green'].set(g)
        self.kb_vars['blue'].set(b)
        self.update_color_preview()

    def apply_kb_color(self):
        """Apply keyboard backlight color (and brightness if control enabled)"""
        if not self.kb_controller.is_available():
            self.status_var.set("Подсветка не обнаружена")
            return

        # Reset idle timer on manual action
        if self.kb_auto_off_active:
            self.kb_monitor.reset_idle_timer()

        # If backlight was turned off by auto-timer, restore saved state first
        if self.kb_was_turned_off:
            self.kb_was_turned_off = False
            if self.kb_vars['control_brightness'].get():
                if self.kb_vars['fade_enabled'].get():
                    self.fade_in_backlight()
                else:
                    self.kb_controller.restore_state()
                    self.kb_vars['brightness'].set(self.kb_controller.saved_brightness)
                    self.on_kb_brightness_change(self.kb_controller.saved_brightness)
            if self.kb_controller.has_rgb:
                self.kb_controller.set_color_all_zones(*self.kb_controller.saved_color)
            self.status_var.set("Подсветка восстановлена")
            return

        # Normal apply - use current slider values
        if self.kb_vars['control_brightness'].get():
            brightness = self.kb_vars['brightness'].get()
            self.kb_controller.set_brightness(brightness)

        if self.kb_controller.has_rgb:
            r = self.kb_vars['red'].get()
            g = self.kb_vars['green'].get()
            b = self.kb_vars['blue'].get()
            self.kb_controller.set_color_all_zones(r, g, b)

        self.status_var.set("Настройки подсветки применены")

    def save_kb_settings(self):
        """Save keyboard backlight settings"""
        # Reset idle timer on manual action
        if self.kb_auto_off_active:
            self.kb_monitor.reset_idle_timer()

        # If backlight was turned off by auto-timer, restore it first
        if self.kb_was_turned_off:
            self.kb_was_turned_off = False

            # Restore hardware state
            if self.kb_vars['control_brightness'].get():
                if self.kb_vars['fade_enabled'].get():
                    self.fade_in_backlight()
                else:
                    self.kb_controller.restore_state()
                    # Update vars immediately to saved values
                    self.kb_vars['brightness'].set(self.kb_controller.saved_brightness)

            # Update vars to correct values immediately for saving
            # (fade_in_backlight updates vars asynchronously, so we force update here to be safe)
            self.kb_vars['brightness'].set(self.kb_controller.saved_brightness)
            if self.kb_controller.has_rgb:
                self.kb_vars['red'].set(self.kb_controller.saved_color[0])
                self.kb_vars['green'].set(self.kb_controller.saved_color[1])
                self.kb_vars['blue'].set(self.kb_controller.saved_color[2])

            self.on_kb_brightness_change(self.kb_controller.saved_brightness)

        self.save_settings()
        self.status_var.set("Настройки подсветки сохранены")

    # ===================== BRIGHTNESS SYNC =====================

    def start_brightness_sync(self):
        """Start periodic brightness sync from system"""
        def sync_loop():
            while self.running:
                try:
                    if self.kb_controller.is_available():
                        # Only sync if not in the middle of a fade
                        if not self.fade_thread_running:
                            system_brightness = self.kb_controller.get_brightness()
                            current_setting = self.kb_vars['brightness'].get()

                            # Check if user turned on backlight externally (FN keys, KDE, etc.)
                            # when program thought it was off
                            if self.kb_was_turned_off and system_brightness > 0:
                                self.kb_was_turned_off = False
                                # Reset idle timer since user is active
                                if self.kb_auto_off_active:
                                    self.kb_monitor.reset_idle_timer()
                                # Update saved state
                                self.kb_controller.saved_brightness = system_brightness

                            # Update UI if system brightness changed (e.g., from KDE, FN keys)
                            if system_brightness != current_setting:
                                self.kb_vars['brightness'].set(system_brightness)
                                self.root.after(0, lambda v=system_brightness: self.on_kb_brightness_change(v))
                except:
                    pass
                time.sleep(2)

        threading.Thread(target=sync_loop, daemon=True).start()

    # ===================== AUTO-OFF TIMER =====================

    def on_auto_off_toggle(self):
        """Handle auto-off feature toggle"""
        if self.kb_vars['auto_off_enabled'].get():
            self.start_kb_auto_off()
        else:
            self.stop_kb_auto_off()

    def start_kb_auto_off(self):
        """Start the auto-off timer functionality"""
        if not self.kb_controller.is_available():
            return

        if not self.kb_monitor.is_available():
            self.auto_off_status_label.config(text="Мониторинг клавиатуры недоступен")
            return

        # Save current state for restoration
        self.kb_controller.save_state()
        self.kb_auto_off_active = True
        self.kb_was_turned_off = False

        # Start keyboard activity monitor
        self.kb_monitor.start(activity_callback=self.on_keyboard_activity)

        # Start the timer check thread
        self.auto_off_thread_running = True
        threading.Thread(target=self._auto_off_timer_loop, daemon=True).start()

        self.auto_off_status_label.config(text="Таймер активен")
        self.status_var.set("Автовыключение подсветки: ВКЛ")

    def stop_kb_auto_off(self):
        """Stop the auto-off timer functionality"""
        self.kb_auto_off_active = False
        self.auto_off_thread_running = False
        self.kb_monitor.stop()

        # Restore backlight if it was turned off (only if brightness control is enabled)
        if self.kb_was_turned_off:
            if self.kb_vars['control_brightness'].get():
                if self.kb_vars['fade_enabled'].get():
                    self.fade_in_backlight()
                else:
                    self.kb_controller.restore_state()
                    self.kb_vars['brightness'].set(self.kb_controller.saved_brightness)
                    self.on_kb_brightness_change(self.kb_controller.saved_brightness)
            self.kb_was_turned_off = False

        self.auto_off_status_label.config(text="Таймер отключен")
        self.status_var.set("Автовыключение подсветки: ВЫКЛ")

    def on_keyboard_activity(self):
        """Called when keyboard activity is detected"""
        if not self.kb_auto_off_active:
            return

        # Restore backlight if it was turned off
        if self.kb_was_turned_off:
            self.kb_was_turned_off = False
            if self.kb_vars['fade_enabled'].get():
                self.fade_in_backlight()
            else:
                self.kb_controller.restore_state()
                self.root.after(0, lambda: self.kb_vars['brightness'].set(self.kb_controller.saved_brightness))
                self.root.after(0, lambda: self.on_kb_brightness_change(self.kb_controller.saved_brightness))

    def _auto_off_timer_loop(self):
        """Background thread that checks for idle timeout"""
        while self.auto_off_thread_running and self.running:
            try:
                if self.kb_auto_off_active and not self.kb_was_turned_off:
                    timeout = self.kb_vars['auto_off_timeout'].get()
                    idle_time = self.kb_monitor.get_idle_time()

                    # Update status label
                    remaining = max(0, timeout - idle_time)
                    if remaining > 0:
                        self.root.after(0, lambda r=remaining:
                            self.auto_off_status_label.config(text=f"До выключения: {int(r)} сек"))
                    else:
                        self.root.after(0, lambda:
                            self.auto_off_status_label.config(text="Подсветка выключена"))

                    # Check if timeout reached
                    if idle_time >= timeout:
                        # Save state before turning off
                        self.kb_controller.save_state()
                        # Update saved_brightness from current system value
                        self.kb_controller.saved_brightness = self.kb_controller.get_brightness()

                        # Turn off backlight (only if brightness control is enabled)
                        if self.kb_vars['control_brightness'].get():
                            if self.kb_vars['fade_enabled'].get():
                                self.fade_out_backlight()
                            else:
                                self.kb_controller.set_brightness(0)
                            self.root.after(0, lambda: self.kb_vars['brightness'].set(0))
                            self.root.after(0, lambda: self.on_kb_brightness_change(0))

                        self.kb_was_turned_off = True

            except Exception as e:
                print(f"Auto-off timer error: {e}")

            time.sleep(1)

    # ===================== FADE EFFECTS =====================

    def _stop_current_fade(self):
        """Stop any ongoing fade and wait for it to finish"""
        if self.fade_thread_running:
            self.fade_stop_flag = True
            # Wait for the fade thread to finish (with timeout)
            timeout = 0.5
            start = time.time()
            while self.fade_thread_running and (time.time() - start) < timeout:
                time.sleep(0.01)
            self.fade_stop_flag = False

    def fade_out_backlight(self):
        """Smoothly fade out backlight to 0"""
        if not self.kb_vars['control_brightness'].get():
            # If brightness control is disabled, do NOT touch brightness
            return

        # Stop any ongoing fade first
        self._stop_current_fade()

        self.fade_thread_running = True
        self.fade_stop_flag = False
        start_brightness = self.kb_controller.get_brightness()
        duration = self.kb_vars['fade_duration'].get()
        steps = 20
        step_duration = duration / steps

        def fade():
            final_brightness = 0
            for i in range(steps + 1):
                if not self.running or not self.auto_off_thread_running or self.fade_stop_flag:
                    break
                brightness = int(start_brightness * (1 - i / steps))
                final_brightness = brightness
                self.kb_controller.set_brightness(brightness)
                self.root.after(0, lambda b=brightness: self.kb_vars['brightness'].set(b))
                self.root.after(0, lambda b=brightness: self.on_kb_brightness_change(b))
                time.sleep(step_duration / 1000)

            self.fade_thread_running = False
            # Ensure final update
            self.root.after(0, lambda: self.on_kb_brightness_change(final_brightness))

        threading.Thread(target=fade, daemon=True).start()

    def fade_in_backlight(self):
        """Smoothly fade in backlight from current to saved value"""
        if not self.kb_vars['control_brightness'].get():
            # If brightness control is disabled, do NOT touch brightness
            return

        # Stop any ongoing fade first
        self._stop_current_fade()

        self.fade_thread_running = True
        self.fade_stop_flag = False

        # Get CURRENT brightness (may be in the middle of fade out)
        current_brightness = self.kb_controller.get_brightness()
        target_brightness = self.kb_controller.saved_brightness

        # If already at or above target, just set it directly
        if current_brightness >= target_brightness:
            self.kb_controller.set_brightness(target_brightness)
            self.root.after(0, lambda: self.kb_vars['brightness'].set(target_brightness))
            self.root.after(0, lambda: self.on_kb_brightness_change(target_brightness))
            self.fade_thread_running = False
            return

        duration = self.kb_vars['fade_duration'].get()
        steps = 20
        step_duration = duration / steps

        def fade():
            final_brightness = target_brightness
            for i in range(steps + 1):
                if not self.running or self.fade_stop_flag:
                    break
                # Interpolate from current to target (not from 0!)
                brightness = int(current_brightness + (target_brightness - current_brightness) * i / steps)
                final_brightness = brightness
                self.kb_controller.set_brightness(brightness)
                self.root.after(0, lambda b=brightness: self.kb_vars['brightness'].set(b))
                self.root.after(0, lambda b=brightness: self.on_kb_brightness_change(b))
                time.sleep(step_duration / 1000)

            self.fade_thread_running = False
            # Ensure final update with target value
            self.root.after(0, lambda: self.on_kb_brightness_change(target_brightness))

        threading.Thread(target=fade, daemon=True).start()

    # ===================== PROFILES =====================

    def update_profile_list(self):
        profiles = []
        if os.path.exists(PROFILES_FILE):
            try:
                with open(PROFILES_FILE, 'r') as f:
                    profiles = list(json.load(f).keys())
            except:
                pass
        self.combo_profiles['values'] = profiles

    def load_profile(self):
        name = self.profile_name_var.get()
        if not name or not os.path.exists(PROFILES_FILE):
            self.status_var.set("Профиль не найден")
            return
        try:
            with open(PROFILES_FILE, 'r') as f:
                data = json.load(f)
            if name in data:
                p = data[name]
                for k, v in p.get('undervolt', {}).items():
                    if k in self.undervolt_vars:
                        self.undervolt_vars[k].set(int(v))
                self.status_var.set(f"Профиль '{name}' загружен")
        except Exception as e:
            self.status_var.set(f"Ошибка загрузки: {e}")

    def save_profile(self):
        name = self.profile_name_var.get()
        if not name:
            self.status_var.set("Введите имя профиля")
            return

        data = {}
        if os.path.exists(PROFILES_FILE):
            try:
                with open(PROFILES_FILE, 'r') as f:
                    data = json.load(f)
            except:
                pass

        data[name] = {
            'undervolt': {k: int(v.get()) for k, v in self.undervolt_vars.items()}
        }

        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(PROFILES_FILE, 'w') as f:
            json.dump(data, f, indent=2)

        self.update_profile_list()
        self.status_var.set(f"Профиль '{name}' сохранён")

    def delete_profile(self):
        name = self.profile_name_var.get()
        if not name:
            self.status_var.set("Выберите профиль для удаления")
            return

        if not os.path.exists(PROFILES_FILE):
            self.status_var.set("Файл профилей не найден")
            return

        try:
            with open(PROFILES_FILE, 'r') as f:
                data = json.load(f)

            if name not in data:
                self.status_var.set(f"Профиль '{name}' не найден")
                return

            del data[name]

            with open(PROFILES_FILE, 'w') as f:
                json.dump(data, f, indent=2)

            self.update_profile_list()
            self.status_var.set(f"Профиль '{name}' удалён")

            if self.profile_name_var.get() == name:
                self.profile_name_var.set("")
        except Exception as e:
            self.status_var.set(f"Ошибка удаления: {e}")

    def load_settings(self):
        if not os.path.exists(SETTINGS_FILE):
            return

        try:
            with open(SETTINGS_FILE, 'r') as f:
                s = json.load(f)

            if 'last_profile' in s:
                self.profile_name_var.set(s['last_profile'])
                if os.path.exists(PROFILES_FILE):
                    with open(PROFILES_FILE, 'r') as f:
                        data = json.load(f)
                    if s['last_profile'] in data:
                        p = data[s['last_profile']]
                        for k, v in p.get('undervolt', {}).items():
                            if k in self.undervolt_vars:
                                self.undervolt_vars[k].set(int(v))

            if 'undervolt_on_startup' in s:
                self.undervolt_on_startup_var.set(bool(s['undervolt_on_startup']))
            if 'fan_on_startup' in s:
                self.fan_on_startup_var.set(bool(s['fan_on_startup']))
            if 'auto_control' in s:
                self.fan_vars['auto_control'].set(bool(s['auto_control']))
            if 'curve_mode' in s:
                self.fan_vars['curve_mode'].set(int(s['curve_mode']))
            if 'view_mode' in s:
                self.fan_vars['view_mode'].set(int(s['view_mode']))

            # Keyboard backlight settings
            if 'kb_on_startup' in s:
                self.kb_on_startup_var.set(bool(s['kb_on_startup']))
            if 'kb_brightness' in s:
                self.kb_vars['brightness'].set(int(s['kb_brightness']))
            if 'kb_red' in s:
                self.kb_vars['red'].set(int(s['kb_red']))
            if 'kb_green' in s:
                self.kb_vars['green'].set(int(s['kb_green']))
            if 'kb_blue' in s:
                self.kb_vars['blue'].set(int(s['kb_blue']))
            if 'kb_apply_all_zones' in s:
                self.kb_vars['apply_all_zones'].set(bool(s['kb_apply_all_zones']))
            if 'kb_auto_off_enabled' in s:
                self.kb_vars['auto_off_enabled'].set(bool(s['kb_auto_off_enabled']))
            if 'kb_auto_off_timeout' in s:
                self.kb_vars['auto_off_timeout'].set(int(s['kb_auto_off_timeout']))
            if 'kb_control_brightness' in s:
                self.kb_vars['control_brightness'].set(bool(s['kb_control_brightness']))
            if 'kb_fade_enabled' in s:
                self.kb_vars['fade_enabled'].set(bool(s['kb_fade_enabled']))
            if 'kb_fade_duration' in s:
                self.kb_vars['fade_duration'].set(int(s['kb_fade_duration']))

            if 'fan_select' in s:
                self.fan_vars['fan_select'].set(int(s['fan_select']))
            if 'fan_speed' in s:
                self.fan_vars['speed'].set(int(s['fan_speed']))
                self.speed_label.config(text=f"{int(s['fan_speed'])}%")

            if 'fan_curve_common' in s:
                self.fan_curve_common = FanCurve(s['fan_curve_common'])
            if 'fan_curve_cpu' in s:
                self.fan_curve_cpu = FanCurve(s['fan_curve_cpu'])
            if 'fan_curve_gpu' in s:
                self.fan_curve_gpu = FanCurve(s['fan_curve_gpu'])

            self.show_curve_view()

            if hasattr(self, 'color_preview'):
                self.update_color_preview()

        except Exception as e:
            print(f"Error loading settings: {e}")

    def save_settings(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)

        s = {
            'last_profile': self.profile_name_var.get(),
            'undervolt_on_startup': self.undervolt_on_startup_var.get(),
            'fan_on_startup': self.fan_on_startup_var.get(),
            'auto_control': self.fan_vars['auto_control'].get(),
            'curve_mode': self.fan_vars['curve_mode'].get(),
            'view_mode': self.fan_vars['view_mode'].get(),
            'fan_select': self.fan_vars['fan_select'].get(),
            'fan_speed': self.fan_vars['speed'].get(),
            'fan_curve_common': self.fan_curve_common.points,
            'fan_curve_cpu': self.fan_curve_cpu.points,
            'fan_curve_gpu': self.fan_curve_gpu.points,
            # Keyboard backlight settings
            'kb_on_startup': self.kb_on_startup_var.get(),
            'kb_brightness': self.kb_vars['brightness'].get(),
            'kb_red': self.kb_vars['red'].get(),
            'kb_green': self.kb_vars['green'].get(),
            'kb_blue': self.kb_vars['blue'].get(),
            'kb_apply_all_zones': self.kb_vars['apply_all_zones'].get(),
            'kb_auto_off_enabled': self.kb_vars['auto_off_enabled'].get(),
            'kb_auto_off_timeout': self.kb_vars['auto_off_timeout'].get(),
            'kb_control_brightness': self.kb_vars['control_brightness'].get(),
            'kb_fade_enabled': self.kb_vars['fade_enabled'].get(),
            'kb_fade_duration': self.kb_vars['fade_duration'].get(),
        }

        with open(SETTINGS_FILE, 'w') as f:
            json.dump(s, f, indent=2)

    # ===================== BACKGROUND THREAD =====================

    def start_fan_thread(self):
        def loop():
            while self.running:
                try:
                    cpu_speed, cpu_temp = self.fan_controller.get_fan_info(0)
                    gpu_speed, gpu_temp = self.fan_controller.get_fan_info(1)

                    self.fan_labels[0].config(text=f"{cpu_speed}% @ {cpu_temp}°C")
                    self.fan_labels[1].config(text=f"{gpu_speed}% @ {gpu_temp}°C")

                    if self.fan_vars['auto_control'].get():
                        if self.fan_vars['curve_mode'].get() == 0:
                            max_temp = max(cpu_temp, gpu_temp)
                            speed = self.fan_curve_common.get_speed(max_temp)
                            for i in range(2):
                                self.fan_controller.set_fan_speed(i, speed)
                        else:
                            cpu_speed = self.fan_curve_cpu.get_speed(cpu_temp)
                            gpu_speed = self.fan_curve_gpu.get_speed(gpu_temp)
                            self.fan_controller.set_fan_speed(0, cpu_speed)
                            self.fan_controller.set_fan_speed(1, gpu_speed)
                except:
                    pass
                time.sleep(1)

        threading.Thread(target=loop, daemon=True).start()

    def on_close(self):
        self.running = False
        self.auto_off_thread_running = False
        self.fade_thread_running = False
        self.save_settings()

        if self.kb_monitor:
            self.kb_monitor.stop()

        if self.fan_controller:
            try:
                self.fan_controller.set_auto()
            except:
                pass
            self.fan_controller.disconnect()
        self.root.destroy()


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    if os.geteuid() != 0:
        pwd = ask_password()
        if pwd:
            cmd = ['sudo', '-S', sys.executable] + sys.argv
            result = subprocess.run(cmd, input=pwd.encode() + b'\n', capture_output=True)
            if result.returncode != 0:
                root = tk.Tk()
                root.withdraw()
                error_msg = result.stderr.decode('utf-8', errors='ignore').strip()
                if 'incorrect password' in error_msg.lower() or 'wrong password' in error_msg.lower() or result.returncode == 1:
                    messagebox.showerror("Ошибка", "Неверный пароль")
                else:
                    messagebox.showerror("Ошибка", f"Ошибка sudo:\n{error_msg[:200]}")
                root.destroy()
            sys.exit(result.returncode)
        else:
            sys.exit(1)

    root = tk.Tk()
    try:
        ttk.Style().theme_use('clam')
    except:
        pass

    app = NotebookControlApp(root)
    root.mainloop()
