"""Global hotkey handling with platform-specific backends."""

import os
import platform
import select
import threading
import time
from typing import Callable


def is_wayland() -> bool:
    """Check if running on Wayland.

    Note: We default to pynput (X11) because it works on Wayland via XWayland.
    The xdg-desktop-portal GlobalShortcuts is experimental and doesn't work
    reliably on all desktop environments (e.g., KDE Plasma).

    Set TURBO_WHISPER_USE_PORTAL=1 to try the portal backend.
    """
    if os.environ.get("TURBO_WHISPER_USE_PORTAL") == "1":
        return os.environ.get("XDG_SESSION_TYPE") == "wayland"
    return False  # Default to pynput (works via XWayland)


def _format_hotkey_for_portal(hotkey_combo: list[str]) -> str:
    """Convert hotkey combo to portal format (e.g., 'CTRL+SHIFT+space')."""
    parts = []
    for key in hotkey_combo:
        key_lower = key.lower()
        if key_lower in ("ctrl", "ctrl_l", "ctrl_r"):
            parts.append("CTRL")
        elif key_lower in ("alt", "alt_l", "alt_r"):
            parts.append("ALT")
        elif key_lower in ("shift", "shift_l", "shift_r"):
            parts.append("SHIFT")
        elif key_lower in ("super", "cmd"):
            parts.append("SUPER")
        else:
            parts.append(key_lower)
    return "+".join(parts)


class PortalHotkeyManager:
    """Wayland hotkey manager using xdg-desktop-portal GlobalShortcuts."""

    def __init__(
        self,
        hotkey_combo: list[str],
        on_activate: Callable[[], None],
        on_deactivate: Callable[[], None] | None = None,
    ):
        """Initialize portal hotkey manager."""
        # Import here to make dependencies optional
        import dbus
        from dbus.mainloop.glib import DBusGMainLoop
        from gi.repository import GLib

        self.on_activate = on_activate
        self.on_deactivate = on_deactivate
        self.hotkey_combo = hotkey_combo
        self.hotkey_str = _format_hotkey_for_portal(hotkey_combo)
        self._running = False
        self._loop = None
        self._thread = None
        self._session = None

        # D-Bus setup
        DBusGMainLoop(set_as_default=True)
        self._bus = dbus.SessionBus()
        self._portal = self._bus.get_object(
            "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop"
        )

        self._GLib = GLib
        self._dbus = dbus

    def _on_activated(self, session_handle, shortcut_id, timestamp, options):
        """Handle shortcut activation."""
        if shortcut_id == "turbo-whisper-toggle":
            self.on_activate()

    def _on_session_created(self, response, results):
        """Handle session creation response."""
        if response != 0:
            print(f"Portal: Failed to create session (response={response})")
            return

        self._session = results.get("session_handle")
        if not self._session:
            print("Portal: No session handle in response")
            return

        print(f"Portal: Session created: {self._session}")

        # Register Activated signal on the session path (not desktop path)
        self._bus.add_signal_receiver(
            self._on_activated,
            signal_name="Activated",
            dbus_interface="org.freedesktop.portal.GlobalShortcuts",
            bus_name="org.freedesktop.portal.Desktop",
            path=self._session,
        )
        print(f"Portal: Listening for Activated on {self._session}")

        # Bind the shortcut
        shortcuts = [
            (
                "turbo-whisper-toggle",
                {
                    "description": self._dbus.String("Toggle Turbo Whisper recording"),
                    "preferred-trigger": self._dbus.String(self.hotkey_str),
                },
            ),
        ]

        try:
            self._portal.BindShortcuts(
                self._session,
                shortcuts,
                "",  # parent_window
                {},  # options
                dbus_interface="org.freedesktop.portal.GlobalShortcuts",
            )
            print(f"Portal: Bound shortcut with preferred trigger: {self.hotkey_str}")
        except Exception as e:
            print(f"Portal: Failed to bind shortcuts: {e}")

    def _run_loop(self):
        """Run GLib main loop in background thread."""
        self._loop = self._GLib.MainLoop()
        self._loop.run()

    def start(self) -> None:
        """Start listening for hotkeys via portal."""
        if self._running:
            return

        self._running = True

        # Create session
        options = {
            "handle_token": self._dbus.String("turbo_whisper"),
            "session_handle_token": self._dbus.String("turbo_whisper_session"),
        }

        try:
            reply = self._portal.CreateSession(
                options, dbus_interface="org.freedesktop.portal.GlobalShortcuts"
            )

            # Listen for response
            self._bus.add_signal_receiver(
                self._on_session_created,
                signal_name="Response",
                dbus_interface="org.freedesktop.portal.Request",
                bus_name="org.freedesktop.portal.Desktop",
                path=reply,
            )

            print("Portal: Creating session...")

        except Exception as e:
            print(f"Portal: Failed to create session: {e}")
            self._running = False
            return

        # Start GLib main loop in background thread
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop listening for hotkeys."""
        self._running = False
        if self._loop:
            self._loop.quit()
            self._loop = None
        self._thread = None


class HotkeyManager:
    """Manages global hotkey registration using pynput (X11/Windows/macOS)."""

    def __init__(
        self,
        hotkey_combo: list[str],
        on_activate: Callable[[], None],
        on_deactivate: Callable[[], None] | None = None,
    ):
        """
        Initialize hotkey manager.

        Args:
            hotkey_combo: List of key names, e.g., ["alt", "space"]
            on_activate: Function to call when hotkey combo is pressed
            on_deactivate: Function to call when hotkey combo is released
        """
        # Import here to make dependency optional (only needed on non-Wayland)
        from pynput import keyboard

        self._keyboard = keyboard

        self.on_activate = on_activate
        self.on_deactivate = on_deactivate
        self.hotkey_combo = self._parse_hotkey(hotkey_combo)
        self.hotkey_chars = self._get_char_keys(hotkey_combo)
        self.current_keys = set()
        self.current_chars = set()
        self._combo_active = False
        self.listener = None
        self._running = False
        self._last_trigger = 0
        self._debounce_ms = 300  # Prevent double triggers

    def _get_char_keys(self, combo: list[str]) -> set:
        """Extract single character keys from combo."""
        return {k.lower() for k in combo if len(k) == 1}

    def _parse_hotkey(self, combo: list[str]) -> set:
        """Parse hotkey string names to pynput keys."""
        kb = self._keyboard
        key_map = {
            "alt": kb.Key.alt,
            "alt_l": kb.Key.alt_l,
            "alt_r": kb.Key.alt_r,
            "ctrl": kb.Key.ctrl,
            "ctrl_l": kb.Key.ctrl_l,
            "ctrl_r": kb.Key.ctrl_r,
            "shift": kb.Key.shift,
            "shift_l": kb.Key.shift_l,
            "shift_r": kb.Key.shift_r,
            "cmd": kb.Key.cmd,
            "super": kb.Key.cmd,
            "space": kb.Key.space,
            "tab": kb.Key.tab,
            "enter": kb.Key.enter,
            "esc": kb.Key.esc,
            "backspace": kb.Key.backspace,
            "f1": kb.Key.f1,
            "f2": kb.Key.f2,
            "f3": kb.Key.f3,
            "f4": kb.Key.f4,
            "f5": kb.Key.f5,
            "f6": kb.Key.f6,
            "f7": kb.Key.f7,
            "f8": kb.Key.f8,
            "f9": kb.Key.f9,
            "f10": kb.Key.f10,
            "f11": kb.Key.f11,
            "f12": kb.Key.f12,
        }

        parsed = set()
        for key_name in combo:
            key_lower = key_name.lower()
            if key_lower in key_map:
                parsed.add(key_map[key_lower])
            elif len(key_lower) == 1:
                # Single character key
                parsed.add(kb.KeyCode.from_char(key_lower))
            else:
                print(f"Warning: Unknown key '{key_name}'")

        return parsed

    def _on_press(self, key) -> None:
        """Handle key press event."""
        kb = self._keyboard
        # Track character keys separately
        if hasattr(key, "char") and key.char:
            self.current_chars.add(key.char.lower())
        else:
            self.current_keys.add(key)

        # Check for alt variants
        if key in (kb.Key.alt_l, kb.Key.alt_r):
            self.current_keys.add(kb.Key.alt)
        if key in (kb.Key.ctrl_l, kb.Key.ctrl_r):
            self.current_keys.add(kb.Key.ctrl)
        if key in (kb.Key.shift_l, kb.Key.shift_r):
            self.current_keys.add(kb.Key.shift)

        # Check if hotkey combo is pressed (special keys + char keys).
        # Character keys are tracked in current_chars (not current_keys), so only
        # the non-character members of the combo need to be in current_keys;
        # char_keys_match below covers the letter keys. Without this, any hotkey
        # containing a letter (e.g. super+h) silently never fires.
        special_keys_match = all(
            k in self.current_keys for k in self.hotkey_combo if not isinstance(k, kb.KeyCode)
        )
        char_keys_match = self.hotkey_chars.issubset(self.current_chars)

        if special_keys_match and char_keys_match:
            # Debounce to prevent double triggers
            now = time.time() * 1000
            if (not self._combo_active) and now - self._last_trigger > self._debounce_ms:
                self._last_trigger = now
                self._combo_active = True
                self.on_activate()

    def _on_release(self, key) -> None:
        """Handle key release event."""
        kb = self._keyboard
        # Clear character keys
        if hasattr(key, "char") and key.char:
            self.current_chars.discard(key.char.lower())
        else:
            self.current_keys.discard(key)

        # Also remove generic versions
        if key in (kb.Key.alt_l, kb.Key.alt_r):
            self.current_keys.discard(kb.Key.alt)
        if key in (kb.Key.ctrl_l, kb.Key.ctrl_r):
            self.current_keys.discard(kb.Key.ctrl)
        if key in (kb.Key.shift_l, kb.Key.shift_r):
            self.current_keys.discard(kb.Key.shift)

        special_keys_match = self.hotkey_combo.issubset(self.current_keys)
        char_keys_match = self.hotkey_chars.issubset(self.current_chars)
        combo_still_active = special_keys_match and char_keys_match
        if self._combo_active and (not combo_still_active):
            self._combo_active = False
            if self.on_deactivate:
                self.on_deactivate()

    def start(self) -> None:
        """Start listening for hotkeys."""
        if self._running:
            return

        self._running = True
        self.listener = self._keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
        )
        self.listener.start()

    def stop(self) -> None:
        """Stop listening for hotkeys."""
        self._running = False
        if self.listener:
            self.listener.stop()
            self.listener = None


class EvdevHotkeyManager:
    """Linux hotkey manager using evdev (works on Wayland)."""

    def __init__(self, hotkey_combo: list[str], callback: Callable[[], None]):
        from evdev import ecodes

        self.callback = callback
        self._ecodes = ecodes
        self._required_keys = self._parse_hotkey(hotkey_combo)
        if not self._required_keys:
            raise ValueError(f"No valid evdev keys parsed from: {hotkey_combo}")

        self._running = False
        self._thread = None
        self._devices = []
        self._pressed_keys = set()
        self._combo_active = False

    def _parse_hotkey(self, combo: list[str]) -> set[int]:
        ec = self._ecodes
        key_map = {
            "alt": ec.KEY_LEFTALT,
            "alt_l": ec.KEY_LEFTALT,
            "alt_r": ec.KEY_RIGHTALT,
            "ctrl": ec.KEY_LEFTCTRL,
            "ctrl_l": ec.KEY_LEFTCTRL,
            "ctrl_r": ec.KEY_RIGHTCTRL,
            "shift": ec.KEY_LEFTSHIFT,
            "shift_l": ec.KEY_LEFTSHIFT,
            "shift_r": ec.KEY_RIGHTSHIFT,
            "super": ec.KEY_LEFTMETA,
            "cmd": ec.KEY_LEFTMETA,
            "space": ec.KEY_SPACE,
            "tab": ec.KEY_TAB,
            "enter": ec.KEY_ENTER,
            "esc": ec.KEY_ESC,
            "backspace": ec.KEY_BACKSPACE,
            "f1": ec.KEY_F1,
            "f2": ec.KEY_F2,
            "f3": ec.KEY_F3,
            "f4": ec.KEY_F4,
            "f5": ec.KEY_F5,
            "f6": ec.KEY_F6,
            "f7": ec.KEY_F7,
            "f8": ec.KEY_F8,
            "f9": ec.KEY_F9,
            "f10": ec.KEY_F10,
            "f11": ec.KEY_F11,
            "f12": ec.KEY_F12,
        }
        parsed: set[int] = set()
        for key_name in combo:
            mapped = key_map.get(key_name.lower())
            if mapped is not None:
                parsed.add(mapped)
            else:
                print(f"Warning: Unknown evdev key '{key_name}'")
        return parsed

    def _is_keyboard(self, device) -> bool:
        ec = self._ecodes
        try:
            caps = device.capabilities()
        except OSError:
            return False
        keys = caps.get(ec.EV_KEY, [])
        if not keys:
            return False
        keyboard_markers = {ec.KEY_A, ec.KEY_Z, ec.KEY_SPACE, ec.KEY_ENTER, ec.KEY_F8}
        return any(k in keys for k in keyboard_markers)

    def _open_devices(self) -> None:
        from evdev import InputDevice, list_devices

        devices = []
        for path in list_devices():
            try:
                device = InputDevice(path)
                if self._is_keyboard(device):
                    devices.append(device)
                else:
                    device.close()
            except PermissionError:
                raise
            except Exception:
                continue

        if not devices:
            raise RuntimeError("No readable keyboard input devices found for evdev hotkeys")
        self._devices = devices

    def _release_devices(self) -> None:
        for device in self._devices:
            try:
                device.close()
            except Exception:
                pass
        self._devices = []

    def _run(self) -> None:
        ec = self._ecodes
        try:
            self._open_devices()
        except PermissionError:
            print("evdev hotkey: permission denied reading /dev/input/event*")
            print("Fix with: sudo usermod -aG input $USER (then log out/in)")
            self._running = False
            return
        except Exception as e:
            print(f"evdev hotkey unavailable: {e}")
            self._running = False
            return

        fd_map = {dev.fd: dev for dev in self._devices}
        while self._running:
            rlist, _, _ = select.select(list(fd_map.keys()), [], [], 0.2)
            for fd in rlist:
                device = fd_map[fd]
                for event in device.read():
                    if event.type != ec.EV_KEY:
                        continue
                    keycode = event.code
                    if event.value in (1, 2):
                        self._pressed_keys.add(keycode)
                    elif event.value == 0:
                        self._pressed_keys.discard(keycode)

                    combo_now = self._required_keys.issubset(self._pressed_keys)
                    if combo_now and not self._combo_active:
                        self._combo_active = True
                        self.callback()
                    elif (not combo_now) and self._combo_active:
                        self._combo_active = False

        self._release_devices()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.4)
        self._thread = None
        self._release_devices()


class X11GrabHotkeyManager:
    """X11 global hotkey via a server-side XGrabKey.

    Unlike the pynput (XRecord) listener in HotkeyManager, which only
    *observes* keys and therefore lets the combo's letter (e.g. the 'h' in
    Super+h) leak through to the focused window, an X server keyboard grab
    intercepts the combo so the keypress is delivered only to us and is
    never typed into the focused application. Requires python-xlib.
    """

    def __init__(self, hotkey_combo: list[str], callback: Callable[[], None]):
        self.callback = callback
        self.hotkey_combo = [k.lower() for k in hotkey_combo]
        self._running = False
        self._thread = None
        self._disp = None
        self._keycode = None
        self._last_trigger = 0.0
        self._debounce_ms = 300

    def _resolve(self, disp):
        """Map the combo to (modifier_mask, keycode); (None, None) if not resolvable."""
        from Xlib import X

        mod = 0
        keyname = None
        mods = {
            "super": X.Mod4Mask,
            "cmd": X.Mod4Mask,
            "ctrl": X.ControlMask,
            "control": X.ControlMask,
            "alt": X.Mod1Mask,
            "option": X.Mod1Mask,
            "shift": X.ShiftMask,
        }
        for k in self.hotkey_combo:
            if k in mods:
                mod |= mods[k]
            else:
                keyname = k
        if keyname is None:
            return None, None
        special = {"space": " ", "enter": "\r", "return": "\r", "tab": "\t", "esc": "\x1b"}
        ch = special.get(keyname, keyname)
        if len(ch) != 1:
            return None, None
        keycode = disp.keysym_to_keycode(ord(ch))
        if not keycode:
            return None, None
        return mod, keycode

    def _run(self) -> None:
        from Xlib import X
        from Xlib.display import Display

        try:
            disp = Display()
        except Exception as e:  # needs a live X server
            print(f"X11Grab: cannot open display: {e}")
            return
        self._disp = disp
        root = disp.screen().root
        mod, keycode = self._resolve(disp)
        if keycode is None:
            print("X11Grab: could not resolve hotkey to a keycode")
            return
        self._keycode = keycode

        # Grab the combo for every combination of the "hidden" lock modifiers
        # (NumLock=Mod2, CapsLock=Lock, ScrollLock=Mod5) so the grab matches
        # regardless of their state. A mask combo already grabbed by another
        # app is just skipped.
        lock_variants = [
            0,
            X.Mod2Mask,
            X.LockMask,
            X.Mod2Mask | X.LockMask,
            X.Mod5Mask,
            X.Mod5Mask | X.Mod2Mask,
        ]
        grabbed = 0
        for lv in lock_variants:
            try:
                root.grab_key(keycode, mod | lv, False, X.GrabModeAsync, X.GrabModeAsync)
                grabbed += 1
            except Exception:
                pass
        if not grabbed:
            print("X11Grab: grab_key failed for all modifier combinations")
            return

        # OR KeyPressMask onto root's existing event mask (don't clobber others).
        try:
            cur = root.get_attributes().your_event_mask
            root.change_attributes(event_mask=cur | X.KeyPressMask)
        except Exception:
            root.change_attributes(event_mask=X.KeyPressMask)
        disp.flush()
        print(f"X11Grab: active (keycode {keycode}, {grabbed} mask variants)")

        while self._running:
            try:
                ev = disp.next_event()
            except Exception:
                break
            if ev.type == X.KeyPress:
                now = time.time() * 1000
                if now - self._last_trigger > self._debounce_ms:
                    self._last_trigger = now
                    self.callback()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        # Release the grab so the key isn't left grabbed after quit.
        try:
            if self._disp is not None and self._keycode is not None:
                from Xlib import X

                self._disp.screen().root.ungrab_key(self._keycode, X.AnyModifier)
                self._disp.flush()
        except Exception:
            pass
        self._thread = None


def create_hotkey_manager(
    hotkey_combo: list[str],
    on_activate: Callable[[], None],
    on_deactivate: Callable[[], None] | None = None,
) -> HotkeyManager | PortalHotkeyManager | EvdevHotkeyManager | None:
    """
    Create appropriate hotkey manager for the current platform.

    Returns:
        HotkeyManager for X11/Windows/macOS
        PortalHotkeyManager for Wayland
        None if no backend is available
    """
    if platform.system() == "Linux" and os.environ.get("XDG_SESSION_TYPE") == "wayland":
        try:
            manager = EvdevHotkeyManager(hotkey_combo, on_activate)
            print("Using evdev for global hotkeys (Wayland)")
            return manager
        except ImportError as e:
            print(f"evdev hotkeys unavailable (missing dependency): {e}")
        except Exception as e:
            print(f"evdev hotkeys unavailable: {e}")

    if is_wayland():
        try:
            manager = PortalHotkeyManager(hotkey_combo, on_activate, on_deactivate)
            print("Using xdg-desktop-portal for global hotkeys (Wayland)")
            if on_deactivate:
                print("Portal backend does not support release events; using press-only behavior")
            return manager
        except ImportError as e:
            print(f"Portal hotkeys unavailable (missing dependencies): {e}")
            print("Install with: pip install dbus-python PyGObject")
            return None
        except Exception as e:
            print(f"Portal hotkeys unavailable: {e}")
            return None
    else:
        if os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            return X11GrabHotkeyManager(hotkey_combo, on_activate)
        return HotkeyManager(hotkey_combo, on_activate, on_deactivate)
