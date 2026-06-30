"""AgentSeal v5.0.0 — Full-screen Textual TUI.

Full-screen Textual terminal UI with:
- Full-screen alternate buffer
- Scrollable conversation log (chat-thread style)
- Bottom input box with slash-command autocomplete
- /wizard command to unlock file loading mode
- Live progress bars during audit
- Inline evidence cards
- Custom truecolor themes
- Stylized filled-in logo with gradient
"""

from __future__ import annotations

import asyncio
import os
import platform
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rich.align import Align
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from rich.console import Group
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.suggester import SuggestFromList
from textual.widgets import Footer, Header, Input, RichLog, Static, Label, ListView, ListItem, Tree, Collapsible

# ---------------------------------------------------------------------------
# Solid filled block wordmark (pyfiglet 'ansi_shadow' font) — heavy █████ blocks
# No frame (frame made it too wide and got chopped in 80-char terminals).
# ---------------------------------------------------------------------------

LOGO_LINES = [
    " █████╗  ██████╗ ███████╗███╗   ██╗████████╗███████╗███████╗ █████╗ ██╗     ",
    "██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝██╔════╝██╔════╝██╔══██╗██║     ",
    "███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║   ███████╗█████╗  ███████║██║     ",
    "██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║   ╚════██║██╔══╝  ██╔══██║██║     ",
    "██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║   ███████║███████╗██║  ██║███████╗",
    "╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝   ╚══════╝╚══════╝╚═╝  ╚═╝╚══════╝",
    "                                                                            ",
    "  ◆━━  contamination auditor for ai agent benchmarks  ━━◆                  ",
]

SPINNER_FRAMES = ["|", "/", "-", "\\"]

# Slash commands — /wizard opens the file browser panel
SLASH_COMMANDS = [
    "/help",
    "/audit",
    "/pro",
    "/auto",
    "/token",
    "/hf",
    "/status",
    "/wizard",
    "/esc",
    "/report",
    "/reports",
    "/open",
    "/copy",
    "/stop",
    "/new",
    "/history",
    "/theme",
    "/clear",
    "/quit",
]

# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SealTheme:
    name: str
    label: str
    bg: str
    panel_bg: str
    fg: str
    fg_dim: str
    accent: str
    accent_2: str
    crit: str
    high: str
    medium: str
    low: str
    clean: str
    # the /token command used .green which didn't exist → AttributeError
    # crash on Windows. accent_2 is the brightest theme color (used for
    # "audit complete" success messages elsewhere).
    green: str = ""  # set in __post_init__ to mirror accent_2

    def __post_init__(self):
        if not self.green:
            # frozen=True dataclass requires object.__setattr__
            object.__setattr__(self, "green", self.accent_2)

THEMES = [
    SealTheme("ember", "Ember (warm cream on dark, orange accent)",
        bg="#1a1410", panel_bg="#261e16", fg="#f5e6c8", fg_dim="#8a7a64",
        accent="#ff8c42", accent_2="#ffaa66",
        crit="#ff4d4d", high="#ff7a45", medium="#ffc94d", low="#7ec8e3", clean="#6b7280"),
    SealTheme("midnight", "Midnight (cool blue-gray on deep navy)",
        bg="#0d1117", panel_bg="#161b22", fg="#e6edf3", fg_dim="#7d8590",
        accent="#58a6ff", accent_2="#79c0ff",
        crit="#ff6b6b", high="#ff9844", medium="#ffcc44", low="#56d4dd", clean="#7d8590"),
    SealTheme("monochrome", "Monochrome (pure grayscale)",
        bg="#0a0a0a", panel_bg="#1a1a1a", fg="#e0e0e0", fg_dim="#808080",
        accent="#e0e0e0", accent_2="#c0c0c0",
        crit="#ffffff", high="#c0c0c0", medium="#a0a0a0", low="#808080", clean="#606060"),
]

THEME_BY_NAME = {t.name: t for t in THEMES}

# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------

def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """Convert hex color to RGB. Handles 3-char and 6-char hex, None, and invalid input."""
    if not hex_color or not isinstance(hex_color, str):
        return (255, 255, 255)
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except (ValueError, IndexError):
        return (255, 255, 255)

def _interpolate(c1, c2, t):
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))

def _rgb_to_ansi(r, g, b):
    return f"\x1b[38;2;{r};{g};{b}m"

def render_logo_gradient(theme: SealTheme) -> str:
    """Render logo with a smooth per-character horizontal gradient.

    Renders every non-space character individually based on its column position, producing a smooth
    left-to-right color transition across the wordmark. Spaces stay
    transparent so the letter shapes pop.

    The gradient uses three stops (cream → amber → accent) for a warm
    "sunset ember" look that matches the AgentSeal brand.
    """
    if theme is None:
        theme = THEMES[0]
    accent = _hex_to_rgb(theme.accent)        # deep orange (right)
    accent_2 = _hex_to_rgb(theme.accent_2)    # amber (middle)
    cream = (255, 235, 205)                    # warm cream (left)

    # Three-stop gradient: cream → accent_2 → accent
    stops = [(0.0, cream), (0.5, accent_2), (1.0, accent)]

    def _color_at(t: float) -> tuple[int, int, int]:
        for i in range(len(stops) - 1):
            t0, c0 = stops[i]
            t1, c1 = stops[i + 1]
            if t0 <= t <= t1:
                local = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
                return _interpolate(c0, c1, local)
        return stops[-1][1]

    max_len = max(len(line) for line in LOGO_LINES)
    out_lines: list[str] = []
    for line in LOGO_LINES:
        padded = line.ljust(max_len)
        chars: list[str] = []
        # Bold on for the whole line
        chars.append("\x1b[1m")
        current_code: str | None = None
        for i, ch in enumerate(padded):
            if ch == " ":
                if current_code is not None:
                    chars.append("\x1b[0m")
                    current_code = None
                chars.append(" ")
            else:
                t = i / max(1, max_len - 1)
                r, g, b = _color_at(t)
                code = f"\x1b[38;2;{r};{g};{b}m"
                if code != current_code:
                    chars.append(code)
                    current_code = code
                chars.append(ch)
        if current_code is not None:
            chars.append("\x1b[0m")
        out_lines.append("".join(chars))
    return "\n".join(out_lines)

# ---------------------------------------------------------------------------
# Cockpit state
# ---------------------------------------------------------------------------

@dataclass
class CockpitState:
    phase: str = "idle"
    phase_message: str = ""
    start_ts: float = field(default_factory=time.time)
    audit_running: bool = False
    audit_complete: bool = False
    wizard_unlocked: bool = False
    results_count: int = 0
    contaminated_count: int = 0
    total_instances: int = 0
    report_paths: dict[str, str] = field(default_factory=dict)

    def elapsed(self) -> float:
        return time.time() - self.start_ts

# ---------------------------------------------------------------------------
# The App
# ---------------------------------------------------------------------------

class SealInput(Input):
    """Bottom input box with > prompt."""
    BORDER_TITLE = "Input"

    def on_key(self, event) -> None:
        """Accept slash-command completion with Tab or right arrow."""
        if event.key not in ("tab", "right"):
            return
        value = self.value or ""
        if not value.startswith("/"):
            return
        cursor = getattr(self, "cursor_position", len(value))
        if cursor != len(value):
            return
        head = value.split(maxsplit=1)[0]
        matches = [cmd for cmd in SLASH_COMMANDS if cmd.startswith(head.lower())]
        if len(matches) == 1:
            suffix = value[len(head):]
            self.value = matches[0] + (" " if not suffix else suffix)
            try:
                self.cursor_position = len(self.value)
            except Exception:
                pass
            event.prevent_default()
            event.stop()

class AgentSealApp(App):
    """AgentSeal v5.0.0 — full-screen terminal cockpit."""

    CSS = """
    Screen {
        background: $background;
        color: $foreground;
    }
    #header-bar {
        height: 14;
        dock: top;
        background: $panel;
        border-bottom: solid $boost;
        padding: 0 1;
    }
    #logo-area {
        width: auto;
        height: 14;
        padding: 0 1;
        content-align: left middle;
        color: $accent;
        background: $panel;
    }
    #status-area {
        height: 14;
        padding: 0 1;
        content-align: center middle;
        background: $panel;
    }
    #conversation {
        height: 1fr;
        border: solid $boost;
        background: $background;
    }
    /* REASONING BOX — Textual Collapsible widget, styled as a dark "black box"
       docked above the input bar. Collapses to a 1-line title bar; expands
       to a scrollable dark-panel showing fetch + M1-M4 reasoning events. */
    #reasoning-box {
        dock: bottom;
        max-height: 50%;
        background: #0a0a0a;
        border-top: solid $accent;
        padding: 0;
        margin: 0;
    }
    #reasoning-box CollapsibleTitle {
        background: $panel;
        color: $accent;
        padding: 0 1;
        text-style: bold;
    }
    #reasoning-log {
        background: #0a0a0a;
        color: #b0b0b0;
        border: none;
        padding: 0 1;
        scrollbar-size: 1 1;
    }
    #input-bar {
        height: 3;
        dock: bottom;
        background: $panel;
        border-top: solid $boost;
        padding: 0 1;
    }
    SealInput {
        height: 3;
        border: solid $boost;
        background: $background;
    }
    HistoryScreen {
        align: center middle;
    }
    #history-list {
        width: 70%;
        height: 70%;
        background: $panel;
        border: solid $accent;
        padding: 1 2;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", show=True),
        Binding("ctrl+l", "clear_log", "Clear", show=True),
        Binding("ctrl+t", "cycle_theme", "Theme", show=True),
        Binding("ctrl+a", "quick_audit", "Audit", show=True),
        # toggled a separate bottom panel; now they control whether fetch /
        # M1-M4 reasoning events stream into the main RichLog.
        Binding("ctrl+right", "expand_thinking", "Expand", show=False),
        Binding("ctrl+left", "collapse_thinking", "Collapse", show=False),
    ]

    theme_name: reactive[str] = reactive("ember")
    spinner_frame: reactive[int] = reactive(0)
    thinking_expanded: reactive[bool] = reactive(False)

    def __init__(self, **kwargs):
        super().__init__()
        self.state = CockpitState()
        self._spinner_timer = None
        self._audit_task = None
        self._current_worker = None
        self._cancel_requested = False  # Flag checked by audit thread

    @property
    def cockpit_theme(self) -> SealTheme:
        """The current AgentSeal theme (not Textual's App.theme)."""
        return THEME_BY_NAME.get(self.theme_name, THEMES[0])

    def _apply_theme(self, theme: SealTheme) -> None:
        """Apply a theme to the app.

        work in Textual 8.2.7 (design_tokens isn't a recognized property).
        We now use Textual's official theme system: register the theme via
        App.register_theme(), then set App.theme to switch to it. This
        properly updates all CSS variables ($background, $foreground, etc.)
        and triggers a re-render.
        """
        self.dark = True
        # Register the theme with Textual's theme system (idempotent)
        try:
            from textual.theme import Theme as TextualTheme
            ttheme = TextualTheme(
                name=theme.name,
                primary=theme.accent,
                background=theme.bg,
                foreground=theme.fg,
                panel=theme.panel_bg,
                dark=True,
            )
            # register_theme is idempotent (safe to call multiple times)
            if hasattr(self, 'register_theme'):
                self.register_theme(ttheme)
            # Switch to the theme — this triggers CSS variable updates
            if hasattr(self, 'theme'):
                self.theme = theme.name
        except Exception:
            pass
        # Fallback: also set design_tokens (for older Textual versions)
        try:
            self.design_tokens = {
                "background": theme.bg,
                "foreground": theme.fg,
                "panel": theme.panel_bg,
                "boost": theme.fg_dim,
                "accent": theme.accent,
            }
        except Exception:
            pass
        try:
            self.refresh_css()
        except Exception:
            pass

    def compose(self) -> ComposeResult:
        with Horizontal(id="header-bar"):
            yield Static(Text.from_ansi(render_logo_gradient(self.cockpit_theme)), id="logo-area")
            yield Static(self._render_status(), id="status-area")
        # Main console — audit output, tables, verdicts
        yield RichLog(id="conversation", markup=True, wrap=True, auto_scroll=True)
        # REASONING BOX — a Textual Collapsible widget docked above the input.
        # Collapses to a 1-line title bar; expands to show fetch + M1-M4 events
        # in a dark scrollable RichLog. Ctrl+→ expands, Ctrl+← collapses.
        # This replaces the old separate #thinking-panel + #thinking-label.
        from textual.widgets import Collapsible
        with Collapsible(id="reasoning-box", title="▸ Reasoning Window (Ctrl+→ to expand)", collapsed=True):
            yield RichLog(id="reasoning-log", markup=True, wrap=True, auto_scroll=True)
        suggester = SuggestFromList(SLASH_COMMANDS, case_sensitive=False)
        with Container(id="input-bar"):
            yield SealInput(
                placeholder="> Type / for commands.  /wizard to browse files.  /audit to run.",
                id="input",
                suggester=suggester,
            )

    def on_mount(self) -> None:
        self._apply_theme(self.cockpit_theme)
        self.title = "AgentSeal v5.0.0"
        # so /token persists across TUI restarts without needing setx/bashrc.
        self._load_persisted_token()
        self._print_welcome()
        # Spinner timer — fires 10x/sec; the callback is a no-op when idle.
        self._spinner_timer = self.set_interval(0.3, self._tick_spinner)
        # Update the reasoning box title with the current phase
        self._update_reasoning_title()

    def _stop_spinner(self) -> None:
        """Stop the spinner timer (call when audit completes / app exits)."""
        if self._spinner_timer is not None:
            try:
                self._spinner_timer.stop()
            except Exception:
                pass

    def _start_spinner(self) -> None:
        """Resume the spinner timer (call when a new audit starts)."""
        if self._spinner_timer is not None:
            try:
                self._spinner_timer.resume()
            except Exception:
                pass

    def _load_persisted_token(self) -> None:
        """Auto-load GitHub token from ~/.agentseal_token on TUI startup.

        When a user saves a token through the TUI, it is written to
        ~/.agentseal_token. On the next launch, AgentSeal reads it back into
        os.environ so /audit and /pro can use it automatically.

        This is a no-op if GITHUB_TOKEN is already set in the environment
        (e.g. user exported it in their shell), or if the file doesn't exist.
        """
        import os as _os
        try:
            if _os.environ.get("GITHUB_TOKEN") or _os.environ.get("GH_TOKEN"):
                return  # env var takes precedence; don't override
            token_file = Path.home() / ".agentseal_token"
            if token_file.exists() and not token_file.is_symlink():
                from .github_auth import normalize_secret_text
                token = normalize_secret_text(token_file.read_text(encoding="utf-8"), kind="github")
                if token and len(token) >= 10:
                    _os.environ["GITHUB_TOKEN"] = token
                    _os.environ["GH_TOKEN"] = token
        except Exception:
            pass  # never crash the TUI over a token-load failure

    def _tick_spinner(self) -> None:
        if self.state.audit_running:
            self.spinner_frame = (self.spinner_frame + 1) % len(SPINNER_FRAMES)
            self._update_status()
            self._update_reasoning_title()

    def _render_status(self) -> Text:
        t = Text()
        if self.state.audit_complete:
            t.append("● audit complete\n", style=f"bold {self.cockpit_theme.accent_2}")
        elif self.state.audit_running:
            spinner = SPINNER_FRAMES[self.spinner_frame]
            t.append(f"{spinner} {self.state.phase}\n", style=f"bold {self.cockpit_theme.accent}")
        elif self.state.wizard_unlocked:
            t.append("⚡ wizard mode\n", style=f"bold {self.cockpit_theme.accent}")
        else:
            t.append("○ idle\n", style=self.cockpit_theme.fg_dim)
        t.append(f"elapsed {self.state.elapsed():.1f}s\n", style=self.cockpit_theme.fg_dim)
        if self.state.audit_running and self.state.total_instances <= 0:
            msg = (self.state.phase_message or self.state.phase or "preparing").strip()
            if len(msg) > 44:
                msg = msg[:41] + "..."
            t.append(f"stage {msg}", style=self.cockpit_theme.fg_dim)
        else:
            t.append(f"contaminated {self.state.contaminated_count}/{self.state.total_instances}", style=self.cockpit_theme.fg_dim)
        return t

    def _update_status(self) -> None:
        try:
            s = self.query_one("#status-area", Static)
            s.update(self._render_status())
        except Exception:
            pass

    def _log(self, text):
        try:
            log = self.query_one("#conversation", RichLog)
            if isinstance(text, str):
                text = Text(text)
            log.write(text)
        except Exception:
            pass

    def _print_welcome(self) -> None:
        self._log(Text.from_ansi(render_logo_gradient(self.cockpit_theme)))
        self._log(Text(""))
        self._log(Text("AgentSeal v5.0.0 — Contamination Auditor for AI Agent Benchmarks", style=f"bold {self.cockpit_theme.fg}"))
        self._log(Text("Deterministic · Local · No AI API · No network calls to models", style=self.cockpit_theme.fg_dim))
        self._log(Text(""))
        # Show the same clean command list as /help
        self._cmd_help()

    def _redact_input_for_log(self, value: str) -> str:
        """Redact secrets before echoing user input into the TUI log.

        Also redacts raw token pastes that do not begin with `/token`. Users
        often paste a GitHub PAT directly into the input bar; the logger should
        never echo that secret.
        This helper only changes display text; command handling still receives
        the original case-preserved input.
        """
        try:
            raw = (value or "").strip()
            from .github_auth import normalize_secret_text, looks_like_github_token, looks_like_hf_token
            gh = normalize_secret_text(raw, kind="github")
            hf = normalize_secret_text(raw, kind="hf")
            raw_l = raw.lower()
            # If the whole submitted text contains a recognizable secret, never
            # render it in the RichLog, even when it was not a slash command.
            if looks_like_github_token(gh):
                if raw_l.startswith("/token"):
                    body = raw[1:].strip()
                    cmd, _sep, arg = body.partition(" ")
                    arg_l = arg.strip().lower()
                    if arg_l in {"", "paste", "test", "clear"}:
                        return f"/{cmd.lower()}" + (f" {arg.strip()}" if arg.strip() else "")
                    if arg_l.startswith("file ") or arg_l == "file":
                        return f"/{cmd.lower()} {arg.strip()}"
                    return f"/{cmd.lower()} [redacted-github-token]"
                return "[redacted-github-token]"
            if looks_like_hf_token(hf):
                if raw_l.startswith("/hf"):
                    body = raw[1:].strip()
                    cmd, _sep, arg = body.partition(" ")
                    arg_l = arg.strip().lower()
                    if arg_l in {"", "paste", "test", "clear"}:
                        return f"/{cmd.lower()}" + (f" {arg.strip()}" if arg.strip() else "")
                    if arg_l.startswith("file ") or arg_l == "file":
                        return f"/{cmd.lower()} {arg.strip()}"
                    return f"/{cmd.lower()} [redacted-hf-token]"
                return "[redacted-hf-token]"
            if not raw.startswith("/"):
                return raw
            body = raw[1:].strip()
            cmd, _sep, arg = body.partition(" ")
            cmd_l = cmd.lower()
            arg_s = arg.strip()
            arg_l = arg_s.lower()
            if cmd_l in {"token", "hf"}:
                safe_words = {"", "paste", "test", "clear"}
                if arg_l in safe_words:
                    return f"/{cmd_l}" + (f" {arg_s}" if arg_s else "")
                if arg_l.startswith("file ") or arg_l == "file":
                    return f"/{cmd_l} {arg_s}"
                return f"/{cmd_l} [redacted-secret]"
            return raw
        except Exception:
            text = str(value or "")
            return "/[redacted]" if text.startswith(("/token", "/hf")) else "[redacted-if-secret]"

    def _try_handle_raw_secret_input(self, value: str) -> bool:
        """Accept a raw pasted token without requiring `/token <pat>`.

        This fixes the common Windows/TUI failure mode where the user pastes a
        PAT directly into the input bar. The old behavior logged the secret and
        said "Type / for commands". Now AgentSeal recognizes the token, stores
        it, and never prints it raw.
        """
        try:
            from .github_auth import normalize_secret_text, looks_like_github_token, looks_like_hf_token
            gh = normalize_secret_text(value, kind="github")
            if looks_like_github_token(gh):
                self._log(Text("  Detected a raw GitHub token paste; setting it securely.", style=self.cockpit_theme.fg_dim))
                self._set_and_persist_token(gh)
                return True
            hf = normalize_secret_text(value, kind="hf")
            if looks_like_hf_token(hf):
                self._log(Text("  Detected a raw HuggingFace token paste; setting it securely.", style=self.cockpit_theme.fg_dim))
                self._set_and_persist_hf_token(hf)
                return True
        except Exception as exc:
            self._log(Text(f"  ✗ Token auto-detect failed: {exc}", style=self.cockpit_theme.crit))
            return True
        return False

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if not value:
            return
        self._log(Text(f"> {self._redact_input_for_log(value)}", style=f"bold {self.cockpit_theme.accent}"))
        event.input.value = ""
        if value.startswith("/"):
            self._handle_slash(value)
        elif self._try_handle_raw_secret_input(value):
            return
        else:
            self._log(Text("  Type / for commands. /wizard to browse files.", style=self.cockpit_theme.fg_dim))

    def _handle_slash(self, value: str) -> None:
        # Preserve argument case. GitHub/HF tokens are case-sensitive; the old
        # parser lower-cased `subcmd`, corrupting `/token github_pat_...`.
        body = value[1:].strip()
        cmd_raw, _, arg_raw = body.partition(" ")
        cmd = cmd_raw.lower().strip()
        arg_raw = arg_raw.strip()
        arg_first, _, arg_rest = arg_raw.partition(" ")
        subcmd = arg_first.lower() if arg_first else ""
        canonical = {c[1:]: c[1:] for c in SLASH_COMMANDS}
        aliases = {"h": "help", "?": "help", "reports": "report", "q": "quit", "exit": "quit", "path": "copy"}
        valid = set(canonical) | set(aliases)
        if cmd and cmd not in valid and len(cmd) >= 3:
            import difflib
            close = difflib.get_close_matches(cmd, list(canonical), n=1, cutoff=0.72)
            if close:
                corrected = close[0]
                self._log(Text(f"  Autocorrected /{cmd} -> /{corrected}", style=self.cockpit_theme.fg_dim))
                cmd = corrected
        if cmd in aliases:
            cmd = aliases[cmd]

        if not cmd:
            self._cmd_help()
            return

        if cmd == "help":
            self._cmd_help()
        elif cmd == "audit":
            self._cmd_audit_verified(subcmd)
        elif cmd == "pro":
            self._cmd_audit_pro(subcmd)
        elif cmd == "auto":
            self._cmd_auto(arg_raw)
        elif cmd == "token":
            self._cmd_token(arg_raw)
        elif cmd == "hf":
            self._cmd_hf(arg_raw)
        elif cmd == "status":
            self._cmd_status()
        elif cmd == "wizard":
            self._cmd_wizard()
        elif cmd == "esc":
            if subcmd == "wizard" or subcmd == "":
                try:
                    self.pop_screen()
                except Exception:
                    pass
            else:
                self._log(Text(f"  Unknown /esc target: '{subcmd}'. Use: /esc wizard", style=self.cockpit_theme.medium))
        elif cmd == "report":
            self._cmd_report()
        elif cmd == "open":
            self._cmd_open_report(arg_raw)
        elif cmd == "copy":
            self._cmd_copy_path(arg_raw)
        elif cmd == "stop":
            self._cmd_stop_audit()
        elif cmd == "new":
            self._cmd_new()
        elif cmd == "history":
            if subcmd == "clear":
                self._cmd_history_clear()
            else:
                self._cmd_history()
        elif cmd == "theme":
            self._cmd_theme(subcmd)
        elif cmd == "clear":
            self.action_clear_log()
        elif cmd == "quit":
            self._log(Text("  Quitting...", style=self.cockpit_theme.fg_dim))
            self.exit()
        else:
            self._log(Text(f"  Unknown: /{cmd}. Try /help.", style=self.cockpit_theme.medium))

    def _cmd_help(self) -> None:
        self._log(Text("AgentSeal v5.0.0 commands:", style=f"bold {self.cockpit_theme.accent}"))
        self._log(Text(""))
        from .github_auth import has_token, has_hf_token
        gh_ok = has_token()
        hf_ok = has_hf_token()
        if not gh_ok or not hf_ok:
            self._log(Text("  ── Recommended: bring your tokens ──", style=f"bold {self.cockpit_theme.medium}"))
            if not gh_ok:
                self._log(Text("    • GitHub token  → /token paste  (unlocks 5000/hr API + code search)", style=f"bold {self.cockpit_theme.accent}"))
                self._log(Text("      Get one: https://github.com/settings/tokens (classic, read-only)", style=self.cockpit_theme.fg_dim))
            else:
                self._log(Text("    ✓ GitHub token set", style=self.cockpit_theme.accent_2))
            if not hf_ok:
                self._log(Text("    • HuggingFace token → /hf paste  (unlocks gated datasets like Multi-SWE-bench)", style=f"bold {self.cockpit_theme.accent}"))
                self._log(Text("      Get one: https://huggingface.co/settings/tokens (Read type)", style=self.cockpit_theme.fg_dim))
            else:
                self._log(Text("    ✓ HuggingFace token set", style=self.cockpit_theme.accent_2))
            self._log(Text(""))
        self._log(Text("  /audit     Audit SWE-bench Verified (all instances)", style=self.cockpit_theme.fg))
        self._log(Text("  /audit N   Audit first N instances (e.g. /audit 10)", style=self.cockpit_theme.fg_dim))
        self._log(Text("  /pro       Audit SWE-bench Pro (all instances)", style=self.cockpit_theme.fg))
        self._log(Text("  /pro N     Audit first N instances (e.g. /pro 10)", style=self.cockpit_theme.fg_dim))
        self._log(Text("  /auto      Autonomous audit — auto-discovers, downloads, and audits ANY benchmark", style=f"bold {self.cockpit_theme.accent}"))
        self._log(Text("  /auto [name] [N]  List or audit benchmark (e.g. /auto multi-swe-bench 10)", style=self.cockpit_theme.fg_dim))
        self._log(Text("  /token paste  Read GitHub token from clipboard", style=f"bold {self.cockpit_theme.accent}"))
        self._log(Text("  /token test   Verify GitHub token works with a real API call", style=f"bold {self.cockpit_theme.accent}"))
        self._log(Text("  /token file <path>  Read token from a text file (e.g. /token file C:\\token.txt)", style=f"bold {self.cockpit_theme.accent}"))
        self._log(Text("  /token [pat]  Set token directly. /token to check status, /token clear to remove", style=self.cockpit_theme.fg))
        self._log(Text("  /hf paste    Read HuggingFace token from clipboard", style=f"bold {self.cockpit_theme.accent}"))
        self._log(Text("  /hf test     Verify HuggingFace token works", style=f"bold {self.cockpit_theme.accent}"))
        self._log(Text("  /hf [tok]    Set HF token directly. /hf to check status, /hf clear to remove", style=self.cockpit_theme.fg))
        self._log(Text("  /status   Show live GitHub rate-limit status (core API + code search)", style=f"bold {self.cockpit_theme.accent}"))
        self._log(Text("  /wizard    Open file browser to select your data", style=f"bold {self.cockpit_theme.accent}"))
        self._log(Text("  /esc       Close the wizard panel", style=self.cockpit_theme.fg))
        self._log(Text("  /open [name]  Open report in browser (e.g. /open swebench_pro)", style=f"bold {self.cockpit_theme.accent}"))
        self._log(Text("  /copy [fmt] [name]  Copy path (e.g. /copy json, /copy md custom_data)", style=f"bold {self.cockpit_theme.accent}"))
        self._log(Text("  /stop      Stop running audit (immediate)", style=self.cockpit_theme.crit))
        self._log(Text("  /new       Clear screen and start fresh", style=self.cockpit_theme.fg))
        self._log(Text("  /history   Show previous reports", style=self.cockpit_theme.fg))
        self._log(Text("  /history clear  Delete all saved reports", style=self.cockpit_theme.crit))
        self._log(Text("  /report    View existing reports", style=self.cockpit_theme.fg))
        self._log(Text("  /theme     Switch theme", style=self.cockpit_theme.fg))
        self._log(Text("  /clear     Clear log (Ctrl+L)", style=self.cockpit_theme.fg))
        self._log(Text("  /quit      Quit (Ctrl+C)", style=self.cockpit_theme.fg))
        self._log(Text(""))
        self._log(Text("Tips:", style=f"bold {self.cockpit_theme.accent}"))
        self._log(Text("  /wizard    Browse files, select your data, run audit", style=self.cockpit_theme.fg_dim))
        self._log(Text("  Ctrl+→    Expand reasoning black box (see fetch + M1-M4)", style=self.cockpit_theme.fg_dim))
        self._log(Text("  Ctrl+←    Collapse reasoning black box", style=self.cockpit_theme.fg_dim))
        self._log(Text(""))
        self._log(Text("Themes:", style=f"bold {self.cockpit_theme.accent}"))
        for t in THEMES:
            marker = "●" if t.name == self.theme_name else " "
            self._log(Text(f"  {marker} {t.name:12} {t.label}", style=self.cockpit_theme.fg))

    def _cmd_theme(self, arg: str) -> None:
        if not arg:
            self._log(Text("Themes:", style=f"bold {self.cockpit_theme.accent}"))
            for t in THEMES:
                marker = "●" if t.name == self.theme_name else " "
                self._log(Text(f"  {marker} {t.name:12} {t.label}", style=self.cockpit_theme.fg))
            self._log(Text("  Usage: /theme <name>", style=self.cockpit_theme.fg_dim))
            return
        t = THEME_BY_NAME.get(arg.lower())
        if t is None:
            self._log(Text(f"  Unknown theme: {arg}", style=self.cockpit_theme.medium))
            return
        self._apply_theme(t)
        self.theme_name = t.name
        self._log(Text(f"  Theme: {t.name}", style=self.cockpit_theme.accent_2))

    # ── /token command ────────────────────────────────────────────────────
    # GITHUB_TOKEN` only affects NEW terminals, not the one they're in.
    # This command lets them paste the token directly into the TUI and have
    # it take effect IMMEDIATELY for the next /audit or /pro. It also
    # persists to ~/.agentseal_token so it survives TUI restarts.
    _TOKEN_FILE = Path.home() / ".agentseal_token"

    def _cmd_token(self, arg: str) -> None:
        """Set, check, or clear the GitHub token.

        /token              → show current status
        /token paste        → read token from system clipboard (use this if paste into the input bar doesn't work)
        /token file <path>  → read token from a text file
        /token <pat>        → set token directly (takes effect immediately, persists)
        /token clear        → remove the persisted token
        """
        import os as _os
        from pathlib import Path as _Path

        # --- /token clear ---
        if arg.lower() == "clear":
            try:
                if self._TOKEN_FILE.exists():
                    self._TOKEN_FILE.unlink()
            except Exception:
                pass
            # Also clear from the current process
            _os.environ.pop("GITHUB_TOKEN", None)
            _os.environ.pop("GH_TOKEN", None)
            self._log(Text("  ✓ GitHub token cleared from process + disk.", style=self.cockpit_theme.accent_2))
            self._log(Text("    Future audits will use the 60 req/hr unauthenticated rate limit.", style=self.cockpit_theme.fg_dim))
            return

        # --- /token paste → read from system clipboard ---
        # right-click paste on Windows Terminal. This bypasses Textual
        # entirely by calling the OS clipboard tool via subprocess.
        if arg.lower() == "paste":
            self._log(Text("  Reading system clipboard...", style=self.cockpit_theme.fg_dim))
            token = self._read_clipboard()
            if not token:
                self._log(Text("  ✗ Could not read clipboard on this system.", style=self.cockpit_theme.crit))
                self._log(Text("    Alternatives:", style=self.cockpit_theme.fg_dim))
                self._log(Text("      1. /token <github-token>  (set token directly)", style=self.cockpit_theme.fg_dim))
                self._log(Text("      2. /token file C:\\Users\\you\\token.txt  (read from a file)", style=self.cockpit_theme.fg_dim))
                self._log(Text("      3. Copy the token, paste into Notepad, save as token.txt, then /token file token.txt", style=self.cockpit_theme.fg_dim))
                return
            from .github_auth import normalize_secret_text
            token = normalize_secret_text(token, kind="github")
            self._log(Text(f"  ✓ Read a candidate GitHub token from clipboard ({len(token)} chars after cleanup).", style=self.cockpit_theme.accent_2))
            # Fall through to the set+persist logic below by reusing `token`
            self._set_and_persist_token(token)
            return

        # --- /token file <path> → read from a text file ---
        if arg.lower().startswith("file ") or arg.lower() == "file":
            file_path = arg[5:].strip() if len(arg) > 5 else ""
            if not file_path:
                self._log(Text("  Usage: /token file <path>", style=self.cockpit_theme.medium))
                self._log(Text("    Example: /token file C:\\Users\\you\\token.txt", style=self.cockpit_theme.fg_dim))
                self._log(Text("    The file should contain just the token (one line, no quotes).", style=self.cockpit_theme.fg_dim))
                return
            try:
                # Expand ~ and env vars
                file_path = _os.path.expanduser(_os.path.expandvars(file_path))
                p = _Path(file_path)
                if not p.exists():
                    self._log(Text(f"  ✗ File not found: {file_path}", style=self.cockpit_theme.crit))
                    return
                from .github_auth import normalize_secret_text
                token = normalize_secret_text(p.read_text(encoding="utf-8"), kind="github")
                if not token or len(token) < 10:
                    self._log(Text(f"  ✗ File looks empty or too short ({len(token)} chars).", style=self.cockpit_theme.crit))
                    return
                self._log(Text(f"  ✓ Read {len(token)} chars from {file_path}", style=self.cockpit_theme.accent_2))
                self._set_and_persist_token(token)
            except Exception as e:
                self._log(Text(f"  ✗ Error reading file: {e}", style=self.cockpit_theme.crit))
            return

        # --- /token (no arg) → show status ---
        if not arg:
            from .github_auth import get_token, mask_secret
            current = get_token()
            if current:
                # Show first 12 + last 4 chars so they can verify it's theirs
                masked = mask_secret(current)
                self._log(Text("  ✓ GitHub token is SET in the current process.", style=self.cockpit_theme.accent_2))
                self._log(Text(f"    Token (masked): {masked}", style=self.cockpit_theme.fg_dim))
                self._log(Text(f"    Independent-source search: ENABLED", style=self.cockpit_theme.accent_2))
            elif self._TOKEN_FILE.exists():
                self._log(Text("  ⚠ Token is persisted to disk but NOT loaded in this process.", style=self.cockpit_theme.medium))
                self._log(Text(f"    File: {self._TOKEN_FILE}", style=self.cockpit_theme.fg_dim))
                self._log(Text("    Run /token paste to reload it, or restart the TUI.", style=self.cockpit_theme.fg_dim))
            else:
                self._log(Text("  ✗ No GitHub token set.", style=self.cockpit_theme.crit))
                self._log(Text("    Without a token: 60 req/hr rate limit, independent-source search disabled.", style=self.cockpit_theme.fg_dim))
                self._log(Text("    Set it with: /token paste  (reads clipboard — recommended)", style=self.cockpit_theme.accent))
                self._log(Text("           or: /token <github-token>", style=self.cockpit_theme.fg_dim))
                self._log(Text("           or: /token file C:\\path\\to\\token.txt  (read from file)", style=self.cockpit_theme.fg_dim))
                self._log(Text("    Create one at: https://github.com/settings/tokens", style=self.cockpit_theme.fg_dim))
            return

        # --- /token test → verify the token works with a real API call ---
        if arg.lower() == "test":
            self._log(Text("  Verifying token with GitHub API...", style=self.cockpit_theme.fg_dim))
            try:
                from .github_fetch import verify_token
                success, message = verify_token()
                if success:
                    self._log(Text(f"  ✓ {message}", style=self.cockpit_theme.accent_2))
                    self._log(Text("    Token is valid. Run /audit 10 to start.", style=self.cockpit_theme.accent))
                else:
                    self._log(Text(f"  ✗ {message}", style=self.cockpit_theme.crit))
            except Exception as e:
                self._log(Text(f"  ✗ Error verifying token: {e}", style=self.cockpit_theme.crit))
            return

        # --- /token <pat> → set + persist (fall through to helper) ---
        from .github_auth import normalize_secret_text
        token = normalize_secret_text(arg, kind="github")
        self._set_and_persist_token(token)

    def _cmd_hf(self, arg: str) -> None:
        """HuggingFace token management — mirrors /token.

        /hf                → show status (is a token set? valid?)
        /hf paste          → read token from system clipboard
        /hf test           → verify the token works against the HF API
        /hf clear          → remove the persisted token
        /hf <hf-token>      → set token directly
        """
        self._log(Text("  /hf — HuggingFace token management", style=f"bold {self.cockpit_theme.accent}"))

        arg_cmd = (arg or "").strip().lower()
        # --- /hf clear ---
        if arg_cmd == "clear":
            try:
                from .github_auth import clear_hf_token
                clear_hf_token()
                self._log(Text("  ✓ HuggingFace token cleared.", style=self.cockpit_theme.accent_2))
            except Exception as e:
                self._log(Text(f"  ✗ Could not clear: {e}", style=self.cockpit_theme.crit))
            return

        # --- /hf paste → read from system clipboard ---
        if arg_cmd == "paste":
            token = self._read_clipboard()
            if not token:
                self._log(Text("  ✗ Clipboard is empty or unreadable.", style=self.cockpit_theme.crit))
                self._log(Text("    Copy your HF token (https://huggingface.co/settings/tokens) first, then /hf paste.", style=self.cockpit_theme.fg_dim))
                return
            from .github_auth import normalize_secret_text
            token = normalize_secret_text(token, kind="hf")
            self._set_and_persist_hf_token(token)
            return

        # --- /hf test → verify against HF API ---
        if arg_cmd == "test":
            self._log(Text("  Verifying HuggingFace token…", style=self.cockpit_theme.fg_dim))
            try:
                import requests
                from .github_auth import get_hf_token, get_hf_auth_headers
                tok = get_hf_token()
                if not tok:
                    self._log(Text("  ✗ No HuggingFace token set. Run /hf paste first.", style=self.cockpit_theme.crit))
                    return
                r = requests.get("https://huggingface.co/api/whoami-v2",
                                 headers=get_hf_auth_headers(), timeout=15)
                if r.status_code == 200:
                    data = r.json()
                    name = data.get("name", "?")
                    from .github_auth import mask_secret
                    masked = mask_secret(tok, prefix=6, suffix=4)
                    self._log(Text(f"  ✓ Token valid (user: {name}, token: {masked}).", style=self.cockpit_theme.accent_2))
                    self._log(Text("    Gated datasets you've accepted will now download. Run /auto <name>.", style=self.cockpit_theme.accent))
                elif r.status_code == 401:
                    self._log(Text("  ✗ HTTP 401 — token is invalid or expired. Generate a new one at https://huggingface.co/settings/tokens", style=self.cockpit_theme.crit))
                else:
                    self._log(Text(f"  ✗ HTTP {r.status_code} — unexpected response.", style=self.cockpit_theme.crit))
            except Exception as e:
                self._log(Text(f"  ✗ Error verifying HF token: {e}", style=self.cockpit_theme.crit))
            return

        # --- /hf (no arg) → show status ---
        if not arg:
            from .github_auth import get_hf_token, has_hf_token, _HF_TOKEN_FILE
            if has_hf_token():
                tok = get_hf_token() or ""
                from .github_auth import mask_secret
                masked = mask_secret(tok, prefix=6, suffix=4)
                self._log(Text(f"  ✓ HuggingFace token is set (masked: {masked}).", style=self.cockpit_theme.accent_2))
                try:
                    if _HF_TOKEN_FILE.exists():
                        self._log(Text(f"    File: {_HF_TOKEN_FILE}", style=self.cockpit_theme.fg_dim))
                except Exception:
                    pass
                self._log(Text("    Run /hf test to verify, or /auto <benchmark> to download + audit.", style=self.cockpit_theme.fg_dim))
            else:
                self._log(Text("  ✗ No HuggingFace token set.", style=self.cockpit_theme.medium))
                self._log(Text("    Needed for gated datasets (e.g. Multi-SWE-bench).", style=self.cockpit_theme.fg_dim))
                self._log(Text("    Get one at https://huggingface.co/settings/tokens (Read type), then:", style=self.cockpit_theme.fg_dim))
                self._log(Text("      /hf paste   (reads clipboard — recommended)", style=f"bold {self.cockpit_theme.accent}"))
            return

        # --- /hf <token> → set + persist ---
        from .github_auth import normalize_secret_text
        token = normalize_secret_text(arg, kind="hf")
        self._set_and_persist_hf_token(token)

    def _set_and_persist_hf_token(self, token: str) -> None:
        """Set the HF token in the process env + persist to ~/.agentseal_hf_token."""
        if not token:
            self._log(Text("  ✗ Empty token.", style=self.cockpit_theme.crit))
            return
        # clipboard (e.g. a URL, a sentence) doesn't silently "succeed".
        # HF tokens start with "hf_" and are 20-50 chars. Warn (don't reject)
        # so unusual token formats still work.
        from .github_auth import looks_like_hf_token, normalize_secret_text, mask_secret
        token = normalize_secret_text(token, kind="hf")
        if not token:
            self._log(Text("  ✗ Empty HuggingFace token after cleanup.", style=self.cockpit_theme.crit))
            return
        if not looks_like_hf_token(token):
            self._log(Text(f"  ⚠ Candidate does not match known HF token format ({mask_secret(token, prefix=4)}).", style=self.cockpit_theme.medium))
            self._log(Text("    Token format was not recognized as a standard HuggingFace token. Proceeding anyway.", style=self.cockpit_theme.fg_dim))
        if len(token) < 10 or len(token) > 200:
            self._log(Text(f"  ⚠ Token length {len(token)} looks unusual (expected 20-50). Proceeding anyway.", style=self.cockpit_theme.medium))
        # Set in process environment (takes effect immediately).
        # tui.py imports `os` at module level (line 17) — use it directly.
        os.environ["HF_TOKEN"] = token
        self._log(Text("  ✓ HuggingFace token set for this session.", style=self.cockpit_theme.accent_2))
        # Persist to disk
        try:
            from .github_auth import persist_hf_token
            if persist_hf_token(token):
                from .github_auth import _HF_TOKEN_FILE
                self._log(Text(f"    Persisted to: {_HF_TOKEN_FILE} (mode 0600)", style=self.cockpit_theme.fg_dim))
            else:
                self._log(Text("    ⚠ Could not persist to disk (token is set for this session only).", style=self.cockpit_theme.medium))
        except Exception as e:
            self._log(Text(f"    ⚠ Could not persist: {e}", style=self.cockpit_theme.medium))
        self._log(Text("    Run /hf test to verify, or /auto <benchmark> to download + audit.", style=self.cockpit_theme.accent))

    def _cmd_status(self) -> None:
        """Show live GitHub rate-limit status.

        budget BEFORE running an audit. Reads from the central rate-limit
        tracker which is updated by every GitHub API call in the codebase.
        """
        from .github_auth import rate_tracker, has_token, get_token, check_preflight, mask_secret

        self._log(Text("  GitHub API Status", style=f"bold {self.cockpit_theme.accent}"))

        # Token status
        if has_token():
            token = get_token() or ""
            masked = mask_secret(token)
            self._log(Text(f"  ✓ Token: SET ({masked})", style=self.cockpit_theme.accent_2))
        else:
            self._log(Text("  ✗ Token: NOT SET", style=self.cockpit_theme.crit))
            self._log(Text("    Run /token paste to set it (unlocks 5000/hr)", style=self.cockpit_theme.fg_dim))
            return

        # Refresh from API for live numbers
        self._log(Text("  Fetching live rate-limit status from GitHub...", style=self.cockpit_theme.fg_dim))
        if rate_tracker.refresh_from_api():
            status_text = rate_tracker.format_status()
            for line in status_text.split("\n"):
                if "EXHAUSTED" in line or "✗" in line:
                    self._log(Text(line, style=self.cockpit_theme.crit))
                elif "LOW" in line or "⚠" in line:
                    self._log(Text(line, style=self.cockpit_theme.medium))
                else:
                    self._log(Text(line, style=self.cockpit_theme.fg))
        else:
            self._log(Text("  ⚠ Could not fetch rate-limit status (network error?)", style=self.cockpit_theme.medium))

        # Pre-flight check for a 10-instance audit
        self._log(Text(""))
        self._log(Text("  Pre-flight check (10-instance audit):", style=f"bold {self.cockpit_theme.accent}"))
        warnings = check_preflight(sample_size=10)
        if not warnings:
            self._log(Text("  ✓ All clear — sufficient budget for a 10-instance audit", style=self.cockpit_theme.accent_2))
        else:
            for w in warnings:
                self._log(Text(f"  ⚠ {w}", style=self.cockpit_theme.medium))

        self._log(Text(""))
        self._log(Text("  Run /audit 10 to start, or /audit for the full 500-instance scan.", style=self.cockpit_theme.accent))

    def _cmd_auto(self, arg: str) -> None:
        """Autonomous benchmark audit — /auto [name].

        /auto              → list known benchmarks
        /auto multi-swe-bench → discover, download, audit Multi-SWE-bench
        /auto humaneval    → discover, download, audit HumanEval (solution mode)
        /auto owner/name   → try any HuggingFace dataset ID
        """
        if not arg or arg.strip() == "":
            # List known benchmarks
            self._log(Text("  Known benchmarks (type /auto <name> to audit):", style=f"bold {self.cockpit_theme.accent}"))
            self._log(Text(""))
            try:
                from .auto_discover import list_known_benchmarks
                benchmarks = list_known_benchmarks()
                pr_diff_benchmarks = [b for b in benchmarks if b.audit_type == "pr_diff"]
                solution_benchmarks = [b for b in benchmarks if b.audit_type == "solution"]

                if pr_diff_benchmarks:
                    self._log(Text("  PR-diff benchmarks (GitHub PR-based):", style=self.cockpit_theme.fg))
                    for b in pr_diff_benchmarks:
                        langs = ", ".join(b.languages[:3])
                        self._log(Text(f"    {b.name:25s} {b.instances:5d} instances  [{langs}]  {b.description[:50]}", style=self.cockpit_theme.fg_dim))
                if solution_benchmarks:
                    self._log(Text("  Solution benchmarks (standalone code):", style=self.cockpit_theme.fg))
                    for b in solution_benchmarks:
                        langs = ", ".join(b.languages[:3])
                        self._log(Text(f"    {b.name:25s} {b.instances:5d} instances  [{langs}]  {b.description[:50]}", style=self.cockpit_theme.fg_dim))
                self._log(Text(""))
                self._log(Text("  Or type any HuggingFace dataset name: /auto owner/dataset-name", style=self.cockpit_theme.fg_dim))
            except Exception as e:
                self._log(Text(f"  Error loading benchmark registry: {e}", style=self.cockpit_theme.crit))
            return

        if self.state.audit_running:
            self._log(Text("  Audit already running. Use /stop first.", style=self.cockpit_theme.medium))
            return

        # Parse: /auto multi-swe-bench  OR  /auto multi-swe-bench 50
        # trailing integer as the sample size and join the remaining words into
        # the benchmark name, normalizing spaces/underscores to hyphens for known
        # registry aliases. This preserves explicit HuggingFace IDs such as
        # "owner/dataset-name".
        parts = arg.split()
        sample_size = 0
        if parts and parts[-1].isdigit():
            sample_size = int(parts[-1])
            parts = parts[:-1]
        benchmark_name = " ".join(parts).strip()
        if not benchmark_name:
            self._log(Text("  Missing benchmark name. Use /auto to list known benchmarks.", style=self.cockpit_theme.medium))
            return
        if "/" not in benchmark_name:
            benchmark_name = benchmark_name.lower().replace("_", "-").replace(" ", "-")

        self.state.audit_running = True
        self.state.start_ts = time.time()
        self.state.audit_complete = False
        self.state.phase = "auto: starting"
        self.state.phase_message = benchmark_name
        self.state.total_instances = 0  # dataset rows are not loaded yet; avoid misleading contaminated 0/N during download
        self._log(Text(""))
        self._log(Text(f"▸ Auditing {benchmark_name} ({sample_size if sample_size > 0 else 'auto'} instances)...  Ctrl+→ to see details",
                       style=f"bold {self.cockpit_theme.accent}"))
        self._update_status()
        self._clear_reasoning_log(f"Reasoning: /auto {benchmark_name}")
        self._log_reasoning("/auto first loads benchmark rows from HuggingFace/GitHub/PyPI; CodeSeal + Stack v2 Bloom activate after rows are loaded.")
        try:
            from .github_auth import has_hf_token
            if not has_hf_token():
                self._log_reasoning("HF token not set: public datasets can download; gated/private datasets require /hf paste then /hf test.")
        except Exception:
            pass
        self._cancel_requested = False

        # Large downloads can emit many progress events. Coalesce UI updates so
        # the terminal remains responsive.
        progress_state = {"last_emit": 0.0, "last_status": 0.0, "last_key": ""}

        def _is_progress_milestone(stage: str, message: str) -> bool:
            text = f"{stage} {message}".lower()
            milestone_words = (
                "starting", "checking", "token", "auth", "loaded from cache",
                "downloading + combining", "shard ", "combined shard",
                "finished", "cached", "loading", "loaded dataframe",
                "schema", "audit mode", "sample", "loaded ", "failed",
                "skipped", "http ", "retry", "activate after",
            )
            return any(w in text for w in milestone_words)

        def on_progress(stage: str, message: str):
            now = time.monotonic()
            stage = str(stage or "work")[:40]
            message = str(message or "")[:240]
            key = f"{stage}|{message}"
            self.state.phase = f"auto: {stage}"
            self.state.phase_message = message

            should_log = (
                key != progress_state["last_key"]
                and (
                    _is_progress_milestone(stage, message)
                    or now - progress_state["last_emit"] >= 1.25
                )
            )
            should_status = now - progress_state["last_status"] >= 0.35

            if should_log:
                progress_state["last_emit"] = now
                progress_state["last_key"] = key
                self._safe_call(self._log_reasoning, f"[auto:{stage}] {message}")
            if should_status:
                progress_state["last_status"] = now
                self._safe_call(self._update_status)

        def run_auto_audit():
            try:
                from .auto_discover import run_auto
                from .engine import AgentSealEngine
                from .schemas import AuditConfig
                from .report import write_html, write_json, write_markdown
                from pathlib import Path
                from datetime import datetime

                # 1. Discover + download + load
                result = run_auto(benchmark_name, sample_size=sample_size, progress_callback=on_progress)
                info = result["benchmark_info"]
                instances = result["instances"]
                audit_type = result["audit_type"]

                self.state.total_instances = len(instances)
                self._safe_call(self._log, Text(f"  ✓ {len(instances)} instances loaded", style=self.cockpit_theme.accent_2))
                self._safe_call(self._log, Text(f"  Audit mode: {audit_type}", style=self.cockpit_theme.fg_dim))
                self._safe_call(self._log, Text(f"  Languages: {', '.join(info.languages[:5])}", style=self.cockpit_theme.fg_dim))
                try:
                    from .stack_v2_filter import get_filter_stats
                    stats = get_filter_stats()
                    self._safe_call(
                        self._log_reasoning,
                        f"Stack v2 Bloom active: {stats.get('filter_type')} "
                        f"(FP ~{(stats.get('estimated_false_positive_rate') or 0) * 100:.2f}%)",
                    )
                except Exception as exc:
                    self._safe_call(self._log_reasoning, f"Stack v2 Bloom status unavailable: {exc}")
                try:
                    from .codeseal_detector import get_bundled_model_status
                    cs = get_bundled_model_status()
                    self._safe_call(
                        self._log_reasoning,
                        f"CodeSeal active: sqlite={cs.get('sqlite_exists')} loaded={cs.get('loaded')}",
                    )
                except Exception as exc:
                    self._safe_call(self._log_reasoning, f"CodeSeal status unavailable: {exc}")

                # 2. Configure audit
                config = AuditConfig(
                    benchmark=info.name,
                    corpus_source=f"auto-discovered {audit_type} ({info.hf_id})",
                    sample_size=sample_size,
                    audit_type=audit_type,
                )

                # 3. Run audit
                self._safe_call(self._log, Text(f"  Starting audit...", style=f"bold {self.cockpit_theme.accent}"))

                def on_audit_progress(phase, completed, total, msg):
                    self.state.phase = phase
                    self.state.total_instances = total
                    step = max(1, total // 10) if total else 1
                    if completed % step == 0 or completed == total:
                        self._safe_call(self._log, Text(f"  {phase:20s} {completed}/{total}  {msg}", style=self.cockpit_theme.fg_dim))
                        self._safe_call(self._update_status)

                def on_evidence(instance_id, risk, match_type, similarity, msg):
                    from .schemas import RiskLevel
                    if risk in (RiskLevel.CRITICAL, RiskLevel.HIGH):
                        self._safe_call(
                            self._log_reasoning,
                            f"[{risk.value.upper():7}] {instance_id[:40]}  {msg[:80]}",
                        )

                def on_reasoning(instance_id, text):
                    self._safe_call(self._log_reasoning, text)

                def cancel_check():
                    return self._cancel_requested

                engine = AgentSealEngine(
                    instances=instances,
                    config=config,
                    on_progress=on_audit_progress,
                    on_evidence=on_evidence,
                    on_reasoning=on_reasoning,
                    cancel_check=cancel_check,
                )
                report = engine.run()

                # 4. Generate report
                reports_dir = Path.home() / ".agentseal" / "reports"
                reports_dir.mkdir(parents=True, exist_ok=True)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                base_name = f"{info.name}_{timestamp}"

                json_path = write_json(report, reports_dir / f"{base_name}.json")
                md_path = write_markdown(report, reports_dir / f"{base_name}.md")
                html_path = write_html(report, reports_dir / f"{base_name}.html")
                self.state.report_paths = {
                    "json": str(json_path.resolve()),
                    "md": str(md_path.resolve()),
                    "html": str(html_path.resolve()),
                }

                self._safe_call(self._log, Text(f"  ✓ Audit complete!", style=self.cockpit_theme.accent_2))
                self._safe_call(self._log, Text(f"  Reports:", style=self.cockpit_theme.fg))
                self._safe_call(self._log, Text(f"    HTML: {html_path}", style=self.cockpit_theme.fg_dim))
                self._safe_call(self._log, Text(f"    JSON: {json_path}", style=self.cockpit_theme.fg_dim))
                self._safe_call(self._log, Text(f"    MD:   {md_path}", style=self.cockpit_theme.fg_dim))

                # Print summary
                s = report.summary
                self.state.contaminated_count = s.instances_with_patch_exposure
                self.state.total_instances = s.total_instances
                self.state.audit_complete = True
                self.state.phase = "complete"
                self._safe_call(self._log, Text(f"  Total: {s.total_instances}  Critical: {s.critical_count}  Rate: {s.contamination_rate*100:.1f}%", style=f"bold {self.cockpit_theme.accent}"))

            except Exception as e:
                self._safe_call(self._log, Text(f"  ✗ Auto audit failed: {e}", style=self.cockpit_theme.crit))
                import traceback
                self._safe_call(self._log, Text(f"    {traceback.format_exc()[-200:]}", style=self.cockpit_theme.fg_dim))
            finally:
                self.state.audit_running = False
                self._safe_call(self._update_status)
                self._safe_call(self._update_reasoning_title)

        # parquet + audit). Textual requires thread=True for sync workers;
        # otherwise it raises: "Request to run a non-async function as an async
        # worker".
        self._current_worker = self.run_worker(
            run_auto_audit,
            thread=True,
            exclusive=True,
            start=True,
            name="run_auto_audit",
        )

    def _read_clipboard(self) -> str:
        """Read text from the system clipboard, cross-platform.

        the OS clipboard tool via subprocess. Works on Windows (PowerShell
        Get-Clipboard), macOS (pbpaste), and Linux (xclip/xsel/wl-paste).
        """
        import os as _os
        import shutil as _shutil
        import subprocess as _sp
        import sys as _sys

        # Windows: use PowerShell Get-Clipboard
        if _os.name == "nt":
            # Try PowerShell Get-Clipboard first (most reliable)
            try:
                result = _sp.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     "Get-Clipboard -Format Text -TextFormatType UnicodeText"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()
            except Exception:
                pass
            # Fallback: try pwsh (PowerShell 7+) if powershell isn't available
            if _shutil.which("pwsh"):
                try:
                    result = _sp.run(
                        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", "Get-Clipboard"],
                        capture_output=True, text=True, timeout=5,
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        return result.stdout.strip()
                except Exception:
                    pass
            return ""

        # macOS: pbpaste
        if _sys.platform == "darwin":
            try:
                result = _sp.run(["pbpaste"], capture_output=True, text=True, timeout=5)
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()
            except Exception:
                pass
            return ""

        # Linux: try xclip, xsel, then wl-paste (Wayland)
        # xclip
        if _shutil.which("xclip"):
            try:
                result = _sp.run(
                    ["xclip", "-o", "-selection", "clipboard"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()
            except Exception:
                pass
        # xsel
        if _shutil.which("xsel"):
            try:
                result = _sp.run(
                    ["xsel", "--clipboard", "--output"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()
            except Exception:
                pass
        # wl-paste (Wayland)
        if _shutil.which("wl-paste"):
            try:
                result = _sp.run(
                    ["wl-paste", "--no-newline"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()
            except Exception:
                pass

        return ""

    def _set_and_persist_token(self, token: str) -> None:
        """Internal helper: set the token in os.environ + persist to disk.

        Called by _cmd_token after the token has been sourced (direct arg,
        clipboard, or file). Handles validation, persistence, and logging.
        """
        import os as _os

        from .github_auth import looks_like_github_token, normalize_secret_text, mask_secret
        token = normalize_secret_text(token, kind="github")
        if not token:
            self._log(Text("  ✗ Empty GitHub token after cleanup.", style=self.cockpit_theme.crit))
            return
        # Basic sanity: GitHub tokens start with known prefixes. Warn only; do
        # not reject because GitHub may add future token families. Never print
        # the raw token or its long prefix.
        if not looks_like_github_token(token):
            self._log(Text(f"  ⚠ Candidate does not match known GitHub token formats ({mask_secret(token, prefix=6)}).", style=self.cockpit_theme.medium))
            self._log(Text("    Accepted prefixes: ghp_, github_pat_, gho_, ghu_, ghs_, ghr_, gh.", style=self.cockpit_theme.fg_dim))
            self._log(Text("    The token will still be set; run /token test to verify it with GitHub.", style=self.cockpit_theme.fg_dim))

        # Warn if the token looks suspiciously short (classic PATs are 40+ chars,
        # fine-grained are 60+ chars). A 20-char token is probably truncated.
        if len(token) < 20:
            self._log(Text(f"  ⚠ Token is only {len(token)} chars — it may be truncated.", style=self.cockpit_theme.medium))
            self._log(Text("    Classic PATs are 40+ chars, fine-grained are 60+ chars.", style=self.cockpit_theme.fg_dim))
            self._log(Text("    If paste cut it off, try: /token file C:\\path\\to\\token.txt", style=self.cockpit_theme.fg_dim))

        # Set in the current process (takes effect immediately for /audit, /pro)
        _os.environ["GITHUB_TOKEN"] = token
        _os.environ["GH_TOKEN"] = token  # some tools check this alias

        # responses don't get returned instead of re-fetching with the token.
        try:
            from .github_fetch import clear_cache
            clear_cache()
            self._log(Text("  ✓ Fetch cache cleared (old errors won't be reused).", style=self.cockpit_theme.fg_dim))
        except Exception:
            pass

        # Persist to disk so it survives TUI restarts
        persisted = False
        try:
            self._TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            # and mode 0600 in a single open() call, eliminating the TOCTOU
            # window where the file was world-readable between write_text()
            # and os.chmod(). A local attacker could read the GitHub token in
            # that window.
            flags = _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC
            if hasattr(_os, "O_NOFOLLOW"):
                flags |= _os.O_NOFOLLOW
            _fd = _os.open(str(self._TOKEN_FILE), flags, 0o600)
            try:
                _os.write(_fd, token.encode("utf-8"))
            finally:
                _os.close(_fd)
            persisted = True
        except Exception as e:
            self._log(Text(f"  ⚠ Could not persist token to disk: {e}", style=self.cockpit_theme.medium))
            self._log(Text("    Token is set for THIS session only. You'll need to /token again next launch.", style=self.cockpit_theme.fg_dim))

        # Verify it's actually in the environment
        verify = _os.environ.get("GITHUB_TOKEN")
        if verify != token:
            self._log(Text("  ✗ Failed to set token in process environment.", style=self.cockpit_theme.crit))
            return

        masked = mask_secret(token)
        self._log(Text("  ✓ GitHub token SET — takes effect immediately.", style=self.cockpit_theme.accent_2))
        self._log(Text(f"    Token (masked): {masked}  ({len(token)} chars)", style=self.cockpit_theme.fg_dim))
        if persisted:
            self._log(Text(f"    Persisted to: {self._TOKEN_FILE}", style=self.cockpit_theme.fg_dim))
        self._log(Text("    Rate limit: 5,000 req/hr  ·  Independent-source search: ENABLED", style=self.cockpit_theme.accent_2))
        self._log(Text("    Run /token test to verify the token works, or /audit 10 to start.", style=self.cockpit_theme.accent))

    def _cmd_wizard(self) -> None:
        """Open the file browser wizard panel.

        visual file browser. The user navigates with arrow keys, selects
        a file, sees an analysis summary, then confirms with Y/N.

        No path typing required — just /wizard and browse.
        """
        def on_file_selected(path):
            """Called when the wizard screen is dismissed with a file path."""
            if path is None:
                return  # user cancelled (Esc)
            # Run the audit on the selected file
            self._analyze_and_run(path)

        # as the second argument, NOT to the WizardScreen constructor.
        # Textual's push_screen(screen, callback) is what wires dismiss()
        # results to the callback. The old code passed the callback to
        # WizardScreen(__init__) which stored it in self._callback but
        # never used it — so dismiss() worked but the callback never fired,
        # and the audit never ran.
        self.push_screen(WizardScreen(), on_file_selected)

    def _analyze_and_run(self, p: Path) -> None:
        """Analyze a selected file and run the appropriate audit.

        Called after the wizard's file browser is dismissed with a path.

        which re-read the file via pd.read_parquet() — crashing on .jsonl files.
        Now we convert the already-loaded DataFrame to BenchmarkInstance objects
        and call the engine directly. No file re-reading.
        Also: uses the file name as the benchmark name, not "SWE-bench Verified".
        """
        self._log(Text(f"  ✓ Selected: {p.name}", style=self.cockpit_theme.accent_2))

        ext = p.suffix.lower()
        import pandas as pd
        import re as _re
        try:
            if ext == ".parquet":
                df = pd.read_parquet(p)
            elif ext == ".jsonl":
                import json as _json
                rows = []
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            rows.append(_json.loads(line))
                df = pd.DataFrame(rows)
            elif ext == ".json":
                df = pd.read_json(p)
            else:
                self._log(Text(f"  ✗ Unsupported: {ext}. Use .parquet, .jsonl, or .json", style=f"bold {self.cockpit_theme.crit}"))
                return
        except Exception as e:
            self._log(Text(f"  ✗ Failed to load: {e}", style=f"bold {self.cockpit_theme.crit}"))
            return

        total = len(df)
        self._log(Text(f"  Loaded {total} instances from {p.name}", style=self.cockpit_theme.fg))

        if total == 0:
            self._log(Text(f"  ✗ File contains 0 instances. Nothing to audit.", style=f"bold {self.cockpit_theme.crit}"))
            return

        from .auto_discover import detect_audit_type, detect_schema, instances_from_dataframe

        schema = detect_schema(df)
        required = ["instance_id", "patch"]
        missing = [c for c in required if c not in schema]
        if missing:
            self._log(Text(f"  ✗ Could not map required fields: {missing}", style=f"bold {self.cockpit_theme.crit}"))
            self._log(Text(f"    Need aliases for: {required}", style=self.cockpit_theme.fg_dim))
            self._log(Text(f"    Found columns: {list(df.columns)}", style=self.cockpit_theme.fg_dim))
            return

        audit_type = detect_audit_type(df, schema)
        instances = instances_from_dataframe(df, schema)

        # Use the file stem as the benchmark name (NOT "SWE-bench Verified")
        benchmark_name = p.stem

        self._log(Text(f"  Schema: {schema}", style=self.cockpit_theme.fg_dim))
        self._log(Text(f"  Detected: {audit_type} audit", style=self.cockpit_theme.accent_2))

        # Run the audit directly with the loaded instances
        self._run_custom_audit(instances, benchmark_name, audit_type)

    def _run_custom_audit(self, instances: list, benchmark_name: str, audit_type: str = "pr_diff") -> None:
        """Run a PR-diff audit on pre-loaded instances.

        wizard loads a custom data file. It creates the engine directly with
        the already-loaded instances — no file re-reading, no parquet crash.

        Uses benchmark_name (the file stem) in all messages and reports,
        NOT "SWE-bench Verified".
        """
        if self.state.audit_running:
            self._log(Text("  Audit already running. Use /stop first.", style=self.cockpit_theme.medium))
            return
        self.state.audit_running = True
        self.state.start_ts = time.time()
        self.state.audit_complete = False
        self.state.phase = "starting"
        self._log(Text(""))
        self._log(Text(f"▸ Auditing {benchmark_name} ({len(instances)} instances)...  Ctrl+→ to see details",
                       style=f"bold {self.cockpit_theme.accent}"))
        self._update_status()
        self._clear_reasoning_log(f"Reasoning: /wizard → {benchmark_name}")
        self._cancel_requested = False
        self._current_worker = self.run_worker(
            lambda: self._do_custom_audit_threaded(instances, benchmark_name, audit_type),
            thread=True,
            exclusive=True,
            start=True,
        )
        self._update_reasoning_title()

    def _do_custom_audit_threaded(self, instances: list, benchmark_name: str, audit_type: str = "pr_diff"):
        """Sync function that runs the custom audit in a background thread."""
        from .engine import AgentSealEngine
        from .report import write_json, write_markdown, write_html
        from .schemas import AuditConfig

        def on_progress(phase, completed, total, msg):
            self.state.phase = phase
            self.state.total_instances = total
            step = max(1, total // 10)
            if completed % step == 0 or completed == total:
                self._safe_call(self._log,
                    Text(f"  [{completed}/{total}] {phase}", style=self.cockpit_theme.fg_dim))
                self._safe_call(self._update_status)

        def on_evidence(instance_id, risk, match_type, similarity, msg):
            from .schemas import RiskLevel
            if risk in (RiskLevel.CRITICAL, RiskLevel.HIGH):
                color = self.cockpit_theme.crit if risk == RiskLevel.CRITICAL else self.cockpit_theme.high
                self._safe_call(self._log_reasoning,
                    f"[{risk.value.upper():7}] {instance_id[:40]}  {msg[:60]}")

        def on_reasoning(instance_id, text):
            self._safe_call(self._log_reasoning, text)

        def cancel_check():
            return self._cancel_requested

        corpus_source = "github-pr-diffs" if audit_type == "pr_diff" else "custom solution-mode data"
        config = AuditConfig(benchmark=benchmark_name, corpus_source=corpus_source, audit_type=audit_type)
        engine = AgentSealEngine(instances=instances, config=config,
                                 on_progress=on_progress, on_evidence=on_evidence,
                                 on_reasoning=on_reasoning, cancel_check=cancel_check)

        try:
            report = engine.run()
        except Exception as exc:
            self._safe_call(self._log,
                Text(f"  ✗ Audit failed: {exc}", style=f"bold {self.cockpit_theme.crit}"))
            self.state.audit_running = False
            self.state.phase = "failed"
            self._safe_call(self._update_status)
            self._safe_call(self._update_reasoning_title)
            return

        # Collapse reasoning on completion
        self._safe_call(self._update_reasoning_title)

        if self._cancel_requested:
            self._safe_call(self._log,
                Text("  ■ Audit stopped.", style=f"bold {self.cockpit_theme.crit}"))
            self.state.audit_running = False
            self.state.phase = "stopped"
            self._safe_call(self._update_status)
            return

        s = report.summary
        self.state.contaminated_count = s.instances_with_patch_exposure
        self.state.total_instances = s.total_instances
        self.state.audit_running = False
        self.state.audit_complete = True
        self.state.phase = "complete"

        # Build summary table
        table = Table(title=f"{benchmark_name} Summary", show_header=True,
                      header_style=f"bold {self.cockpit_theme.accent}")
        table.add_column("Metric", style="dim")
        table.add_column("Value", justify="right")
        table.add_row("Total instances", str(s.total_instances))
        table.add_row("Patch exposed", f"{s.instances_with_patch_exposure} ({s.patch_exposure_rate*100:.1f}%)")
        table.add_row("Problem statement exposed", f"{s.instances_with_problem_statement_exposure} ({s.problem_statement_exposure_rate*100:.1f}%)")
        table.add_row("Test patch exposed", f"{s.instances_with_test_patch_exposure} ({s.test_patch_exposure_rate*100:.1f}%)")
        table.add_row("Repos in training corpus", f"{s.instances_with_repo_in_corpus} ({s.repo_in_corpus_rate*100:.1f}%)")
        table.add_row("Contamination rate", f"{s.contamination_rate*100:.2f}%", style=f"bold {self.cockpit_theme.crit}")
        table.add_row("Critical", str(s.critical_count), style=self.cockpit_theme.crit)
        table.add_row("High", str(s.high_count), style=self.cockpit_theme.high)
        table.add_row("Medium", str(s.medium_count), style=self.cockpit_theme.medium)
        table.add_row("Low", str(s.low_count))
        table.add_row("Clean", str(s.clean_count), style=self.cockpit_theme.clean)

        # Write reports — use ABSOLUTE paths
        out_dir = Path.cwd() / "examples" / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        name = f"custom_{benchmark_name}"
        json_path = write_json(report, out_dir / f"{name}.json")
        md_path = write_markdown(report, out_dir / f"{name}.md")
        html_path = write_html(report, out_dir / f"{name}.html")
        self.state.report_paths = {
            "json": str(json_path.resolve()),
            "md": str(md_path.resolve()),
            "html": str(html_path.resolve()),
        }

        self._safe_call(self._log, Text(""))
        self._safe_call(self._log,
            Text(f"● Audit Complete — {s.contamination_rate*100:.1f}% contaminated",
                 style=f"bold {self.cockpit_theme.accent_2}"))
        self._safe_call(self._log, Text(""))
        self._safe_call(self._log, table)
        self._safe_call(self._log, Text(""))
        self._safe_call(self._log, Text("Reports written:", style=f"bold {self.cockpit_theme.accent}"))
        self._safe_call(self._log, Text(f"  json      {json_path.resolve()}", style=self.cockpit_theme.accent_2))
        self._safe_call(self._log, Text(f"  markdown  {md_path.resolve()}", style=self.cockpit_theme.accent_2))
        self._safe_call(self._log, Text(f"  html      {html_path.resolve()}", style=self.cockpit_theme.accent_2))
        self._safe_call(self._log, Text(""))
        from .report import open_in_browser
        opened = open_in_browser(html_path.resolve())
        if opened:
            self._safe_call(self._log,
                Text("  ↗ Opened HTML report in browser", style=f"bold {self.cockpit_theme.accent_2}"))
        else:
            self._safe_call(self._log,
                Text("  Type /open to open the report in browser", style=f"bold {self.cockpit_theme.accent}"))
        self._safe_call(self._log, Text(""))
        self._safe_call(self._update_status)

    def _cmd_audit_verified(self, arg: str = "") -> None:
        """Audit SWE-bench Verified. Optional sample size: /audit 10"""
        if self.state.audit_running:
            self._log(Text("  Audit already running. Use /stop first.", style=self.cockpit_theme.medium))
            return
        data_path = self._find_data("swebench_verified.parquet")
        if not data_path:
            self._log(Text("  ✗ SWE-bench Verified data not found.", style=self.cockpit_theme.crit))
            self._log(Text("    Download from: https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified", style=self.cockpit_theme.fg_dim))
            return
        sample = self._parse_sample_arg(arg, default_full=0)
        if sample is None:
            return  # error already logged
        # Warns BEFORE the audit starts if the budget is insufficient, so the
        # user can wait for reset or set a token instead of getting a partial report.
        try:
            from .github_auth import check_preflight
            warnings = check_preflight(sample_size=sample)
            for w in warnings:
                self._log(Text(f"  ⚠ {w}", style=self.cockpit_theme.medium))
        except Exception:
            pass
        self._run_verified_audit(data_path, sample)

    def _cmd_audit_pro(self, arg: str = "") -> None:
        """Audit SWE-bench Pro. Optional sample size: /pro 10"""
        if self.state.audit_running:
            self._log(Text("  Audit already running. Use /stop first.", style=self.cockpit_theme.medium))
            return
        data_path = self._find_data("swebench_pro.parquet")
        if not data_path:
            self._log(Text("  ✗ SWE-bench Pro data not found.", style=self.cockpit_theme.crit))
            self._log(Text("    Download from: https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro", style=self.cockpit_theme.fg_dim))
            return
        sample = self._parse_sample_arg(arg, default_full=0)
        if sample is None:
            return
        try:
            from .github_auth import check_preflight
            warnings = check_preflight(sample_size=sample)
            for w in warnings:
                self._log(Text(f"  ⚠ {w}", style=self.cockpit_theme.medium))
        except Exception:
            pass
        self._run_pro_audit(data_path, custom=False, sample=sample)

    def _parse_sample_arg(self, arg: str, default_full: int = 0) -> Optional[int]:
        """Parse a sample-size argument like '10' or '' (empty = full).

        Returns the sample size, or None if the arg was invalid (in which
        case an error is logged). Empty/missing arg returns default_full
        (0 = 'all instances').

        sample = int(arg)` silently ignored non-digit args (running a full
        audit instead of the intended sample) AND treated 0 as 'all' (so
        '/pro 0' ran a 35-second full audit instead of doing nothing).
        """
        if not arg or not arg.strip():
            return default_full
        s = arg.strip()
        if not s.isdigit():
            self._log(Text(f"  ✗ Invalid sample size: '{arg}'. Expected a positive integer (e.g. /pro 10).",
                           style=self.cockpit_theme.medium))
            self._log(Text(f"    Omit the number for a full audit: /pro",
                           style=self.cockpit_theme.fg_dim))
            return None
        n = int(s)
        if n == 0:
            self._log(Text(f"  ✗ Sample size 0 is ambiguous. Omit the number for a full audit, or use /pro 1+.",
                           style=self.cockpit_theme.medium))
            return None
        return n

    def _cmd_copy_path(self, arg: str = "") -> None:
        """Copy a report path to clipboard. Usage: /copy [format] [name]

        Examples:
          /copy            — copy latest HTML path
          /copy json       — copy latest JSON path
          /copy md         — copy latest Markdown path
          /copy html       — copy latest HTML path
          /copy json swebench_pro  — copy JSON path for swebench_pro report
          /copy md custom_mydata   — copy MD path for custom_mydata report

        previous code only tried xclip with check=False, so on systems without
        xclip the user was told '✓ Copied' but the clipboard was empty.
        instead of silently defaulting to HTML.
        """
        import subprocess
        import platform
        parts = arg.split() if arg else []
        fmt = "html"
        name = None
        if parts:
            candidate = parts[0].lower()
            valid_fmts = ("html", "json", "md", "markdown")
            if candidate in valid_fmts:
                fmt = candidate
                if len(parts) > 1:
                    name = " ".join(parts[1:]).strip().lower().replace(" ", "_")
            else:
                # path and printed '✓ Copied html path' — a false success.
                self._log(Text(f"  ✗ Unknown format: '{parts[0]}'. Use html, json, md, or markdown.",
                               style=self.cockpit_theme.medium))
                return
        ext = {"html": "html", "json": "json", "md": "md", "markdown": "md"}.get(fmt, "html")
        key = ext  # ext and key are always identical; the old code had a redundant dict

        # Find the file
        file_path = None
        report_dirs = [Path.cwd() / "examples" / "reports", Path("examples/reports")]

        if name:
            # Search by name
            for d in report_dirs:
                if d.exists():
                    for pattern in [f"{name}.{ext}", f"*{name}*.{ext}"]:
                        matches = list(d.glob(pattern))
                        if matches:
                            file_path = matches[0].resolve()
                            break
                    if file_path:
                        break
        elif self.state.report_paths and key in self.state.report_paths:
            # Use the most recent report
            file_path = self.state.report_paths[key]
        else:
            # Find most recent by modification time
            for d in report_dirs:
                if d.exists():
                    files = sorted(d.glob(f"*.{ext}"), key=lambda p: p.stat().st_mtime, reverse=True)
                    if files:
                        file_path = files[0].resolve()
                        break

        if not file_path:
            self._log(Text(f"  ✗ No {ext} report found.", style=self.cockpit_theme.crit))
            if name:
                self._log(Text(f"    Searched for: {name}.{ext}", style=self.cockpit_theme.fg_dim))
            return

        # silent and the user was told '✓ Copied' even when nothing was copied.
        copied = False
        try:
            system = platform.system()
            path_bytes = str(file_path).encode()
            if system == "Windows":
                r = subprocess.run(["clip"], input=path_bytes, check=False, timeout=5)
                copied = (r.returncode == 0)
            elif system == "Darwin":
                r = subprocess.run(["pbcopy"], input=path_bytes, check=False, timeout=5)
                copied = (r.returncode == 0)
            else:
                # Linux: try xclip, then xsel, then wl-copy (Wayland)
                for cmd in [
                    ["xclip", "-selection", "clipboard"],
                    ["xsel", "--clipboard", "--input"],
                    ["wl-copy"],
                ]:
                    try:
                        r = subprocess.run(cmd, input=path_bytes, check=False, timeout=5)
                        if r.returncode == 0:
                            copied = True
                            break
                    except (FileNotFoundError, subprocess.TimeoutExpired):
                        continue
        except Exception:
            copied = False

        if copied:
            self._log(Text(f"  ✓ Copied {ext} path: {file_path}", style=f"bold {self.cockpit_theme.accent_2}"))
        else:
            # No clipboard utility worked — print the path so the user can copy manually
            self._log(Text(f"  (clipboard unavailable) Path: {file_path}", style=f"bold {self.cockpit_theme.accent_2}"))
            self._log(Text(f"    Install xclip/xsel/wl-copy to enable auto-copy.", style=self.cockpit_theme.fg_dim))

    def _cmd_stop_audit(self) -> None:
        """Stop the currently running audit immediately.

        BETWEEN instances — so a long-running network fetch (20s timeout)
        kept the audit alive for up to 20 seconds after /stop. We now also
        abort in-flight HTTP requests by monkeypatching requests.get to
        raise immediately, so the current instance fails fast and the
        cancel_check between instances kicks in within ~1 second.
        """
        if not self.state.audit_running:
            self._log(Text("  No audit is running.", style=self.cockpit_theme.fg_dim))
            return
        # Set the cancel flag — checked between instances AND now by the
        # patched fetcher below.
        self._cancel_requested = True
        # Abort in-flight HTTP requests: patch requests.get to raise
        # immediately. The audit thread's try/except will catch the
        # RequestException and move to the next instance (which the
        # cancel_check then blocks).
        try:
            import requests as _requests
            _orig_get = _requests.get
            def _aborted_get(*a, **kw):
                if self._cancel_requested:
                    raise _requests.RequestException("cancelled by /stop")
                return _orig_get(*a, **kw)
            _requests.get = _aborted_get
            # Also patch the module-level references the fetchers imported
            from . import github_fetch as _gh
            from . import pro_audit as _pa
            _gh.requests.get = _aborted_get
            _pa.requests.get = _aborted_get
        except Exception:
            pass
        # Cancel the Textual worker
        if self._current_worker is not None:
            try:
                self._current_worker.cancel()
            except Exception:
                pass
        try:
            for worker in list(self.workers):
                try:
                    worker.cancel()
                except Exception:
                    pass
        except Exception:
            pass
        self.state.audit_running = False
        self.state.phase = "stopped"
        self._current_worker = None
        self._log(Text("  ■ Audit stopped.", style=f"bold {self.cockpit_theme.crit}"))
        self._update_status()

    def _cmd_new(self) -> None:
        """Clear the screen and start fresh."""
        try:
            self.query_one("#conversation", RichLog).clear()
        except Exception:
            pass
        self.state = CockpitState()
        self._print_welcome()
        self._update_status()

    def _cmd_history(self) -> None:
        """Show a popup list of previous reports with arrow key navigation."""
        report_dirs = [Path.cwd() / "examples" / "reports", Path("examples/reports")]
        reports = []
        for d in report_dirs:
            if d.exists():
                for f in d.glob("*.html"):
                    mtime = f.stat().st_mtime
                    import datetime
                    dt = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
                    reports.append((dt, f.stem, f))
        if not reports:
            self._log(Text("  No previous reports found.", style=self.cockpit_theme.fg_dim))
            return
        reports.sort(key=lambda x: x[0], reverse=True)
        # Build list items
        items = []
        for dt, name, path in reports[:20]:
            label = f"  {name}  ({dt})"
            item = ListItem(Label(label))
            item._report_name = name
            item._report_path = str(path)
            items.append(item)
        # Push a modal screen with the list
        from textual.screen import ModalScreen
        from textual.containers import Vertical

        class HistoryScreen(ModalScreen):
            def __init__(self, items_list):
                super().__init__()
                self._items = items_list
                self._selected = None

            def compose(self):
                with Vertical(id="history-list"):
                    yield Label(Text("Previous Reports (↑↓ to navigate, Enter to open, Esc to close)",
                               style="bold accent"))
                    yield ListView(*self._items)

            def on_mount(self) -> None:
                # into the list before ↑↓ navigation worked, despite the
                # prompt advertising arrow-key navigation.
                try:
                    lv = self.query_one(ListView)
                    lv.focus()
                except Exception:
                    pass

            def on_list_view_selected(self, event):
                self._selected = event.item
                self.dismiss(event.item)

            def on_key(self, event):
                if event.key == "escape":
                    self.dismiss(None)

        def on_history_selected(item):
            if item is None:
                return
            name = getattr(item, "_report_name", "")
            path = getattr(item, "_report_path", "")
            # Show report info in the console (don't auto-open browser)
            self._log(Text(f"  📄 Report: {name}", style=f"bold {self.cockpit_theme.accent_2}"))
            self._log(Text(f"     Path: {path}", style=self.cockpit_theme.fg_dim))
            # Check if JSON version exists for summary
            json_path = Path(path).with_suffix('.json')
            if json_path.exists():
                try:
                    import json as _json
                    data = _json.loads(json_path.read_text())
                    s = data.get('summary', {})
                    total = s.get('total_instances', '?')
                    contaminated = s.get('instances_with_patch_exposure', '?')
                    rate = s.get('contamination_rate', 0)
                    self._log(Text(f"     Instances: {total}  Contaminated: {contaminated} ({rate*100:.1f}%)", style=self.cockpit_theme.fg))
                    self._log(Text(f"     Critical: {s.get('critical_count', 0)}  High: {s.get('high_count', 0)}  Clean: {s.get('clean_count', 0)}", style=self.cockpit_theme.fg_dim))
                except Exception:
                    pass
            self._log(Text(f"     Type /open {name} to open in browser", style=f"bold {self.cockpit_theme.accent}"))
            self._log(Text(""))

        self.push_screen(HistoryScreen(items), on_history_selected)

    def _cmd_history_clear(self) -> None:
        """Delete all report files from the reports directory.

        /history clear — removes all .json, .md, .html files from
        examples/reports/. This does NOT delete the current session's
        state (report_paths) — it clears the on-disk report history.
        """
        report_dirs = [Path.cwd() / "examples" / "reports", Path("examples/reports")]
        deleted = 0
        for d in report_dirs:
            if d.exists():
                for ext in ["*.json", "*.md", "*.html"]:
                    for f in d.glob(ext):
                        try:
                            f.unlink()
                            deleted += 1
                        except Exception:
                            pass
        # Clear the current session's report paths too
        self.state.report_paths = {}
        self._log(Text(f"  ✓ Deleted {deleted} report file(s) from history.", style=f"bold {self.cockpit_theme.accent_2}"))
        self._log(Text(""))

    def _cmd_open_report(self, arg: str = "") -> None:
        """Open a report in the browser. Usage: /open [name]

        Without arg: opens the most recent report.
        With name: opens the matching report (e.g. /open swebench_pro, /open custom_mydata)
        """
        from .report import open_in_browser
        report_dirs = [Path.cwd() / "examples" / "reports", Path("examples/reports")]

        if arg:
            # User specified a name — find the matching report
            name = arg.strip().lower().replace(" ", "_")
            for d in report_dirs:
                if d.exists():
                    # Try exact match, then partial match
                    for pattern in [f"{name}.html", f"*{name}*.html"]:
                        matches = list(d.glob(pattern))
                        if matches:
                            html_path = matches[0].resolve()
                            self._log(Text(f"  Opening: {html_path}", style=self.cockpit_theme.accent_2))
                            if open_in_browser(html_path):
                                self._log(Text("  ↗ Opened in browser", style=f"bold {self.cockpit_theme.accent_2}"))
                            else:
                                self._log(Text(f"  ✗ Could not open. Path: {html_path}", style=self.cockpit_theme.crit))
                            return
            self._log(Text(f"  ✗ No report matching '{arg}' found.", style=self.cockpit_theme.crit))
            # Show available reports
            available = []
            for d in report_dirs:
                if d.exists():
                    available.extend(f.stem for f in d.glob("*.html"))
            if available:
                self._log(Text(f"  Available: {', '.join(sorted(set(available)))}", style=self.cockpit_theme.fg_dim))
            return

        # No arg — open most recent
        if self.state.report_paths and "html" in self.state.report_paths:
            html_path = self.state.report_paths["html"]
            self._log(Text(f"  Opening: {html_path}", style=self.cockpit_theme.accent_2))
            if open_in_browser(html_path):
                self._log(Text("  ↗ Opened in browser", style=f"bold {self.cockpit_theme.accent_2}"))
            else:
                self._log(Text(f"  ✗ Could not open. Path: {html_path}", style=self.cockpit_theme.crit))
            return

        # Find most recent by file modification time
        for d in report_dirs:
            if d.exists():
                htmls = sorted(d.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
                if htmls:
                    self._log(Text(f"  Opening: {htmls[0]}", style=self.cockpit_theme.accent_2))
                    if open_in_browser(htmls[0].resolve()):
                        self._log(Text("  ↗ Opened in browser", style=f"bold {self.cockpit_theme.accent_2}"))
                    else:
                        self._log(Text(f"  ✗ Could not open. Path: {htmls[0].resolve()}", style=self.cockpit_theme.crit))
                    return
        self._log(Text("  ✗ No reports found. Run /pro or /audit first.", style=self.cockpit_theme.crit))

    def _cmd_report(self) -> None:
        """View existing reports — shows ALL reports in the reports directory."""
        report_dirs = [Path.cwd() / "examples" / "reports", Path("examples/reports")]
        reports = {}
        for d in report_dirs:
            if d.exists():
                for f in d.glob("*.json"):
                    reports[f.stem] = f
        if not reports:
            self._log(Text("  No reports found. Run /audit, /pro, or /wizard first.", style=self.cockpit_theme.fg_dim))
            return
        self._log(Text(f"Existing reports ({len(reports)}):", style=f"bold {self.cockpit_theme.accent}"))
        # Sort by modification time (newest first)
        sorted_reports = sorted(reports.items(), key=lambda x: x[1].stat().st_mtime if x[1].exists() else 0, reverse=True)
        for name, path in sorted_reports:
            html = path.with_suffix(".html")
            html_exists = html.exists()
            self._log(Text(f"  📄 {name}", style=self.cockpit_theme.fg))
            self._log(Text(f"     JSON: {path}", style=self.cockpit_theme.fg_dim))
            if html_exists:
                self._log(Text(f"     HTML: {html}", style=self.cockpit_theme.accent_2))
            self._log(Text(f"     Type /open {name} to open in browser", style=self.cockpit_theme.fg_dim))
            self._log(Text(""))

    def _find_data(self, filename: str) -> Optional[Path]:
        """Find a data file using the shared find_data_file helper."""
        from .loaders import find_data_file
        return find_data_file(filename)

    def _safe_call(self, fn, *args, **kwargs):
        """Call a UI function from a worker thread.

        clause made RuntimeError dead code (Exception is its base) AND silently
        swallowed EVERY error from UI updates — hiding real bugs behind a
        generic stderr print. We now catch only RuntimeError (the legitimate
        'no running event loop / app shutting down' case, which is expected
        during teardown) and let every other exception propagate so it
        surfaces in the audit log instead of vanishing.
        """
        try:
            self.call_from_thread(fn, *args, **kwargs)
        except RuntimeError:
            # App not running (test mode, or shutting down) — call directly.
            # This is the ONLY expected failure mode for call_from_thread.
            # The previous code caught Exception and printed to stderr,
            # silently swallowing real UI bugs. Now fn() exceptions propagate
            # to the audit's outer try/except so they surface in the log.
            fn(*args, **kwargs)
        # Unexpected call_from_thread errors should surface during testing.

    def _run_pro_audit(self, data_path: Path, custom: bool = False, sample: int = 0) -> None:
        """Run SWE-bench Pro audit in a background THREAD (not async task).

        Uses thread=True so blocking I/O (network calls to GitHub) doesn't
        freeze the Textual event loop. The spinner in the status bar animates
        while the audit runs.
        """
        if self.state.audit_running:
            self._log(Text("  Audit already running.", style=self.cockpit_theme.medium))
            return
        self.state.audit_running = True
        self.state.start_ts = time.time()
        self.state.audit_complete = False
        self.state.phase = "starting"
        self._log(Text(""))
        label = "custom" if custom else "SWE-bench Pro"
        # status bar (top). Here we print the initial "▸ Auditing..." line.
        # The reasoning black box starts COLLAPSED — user presses Ctrl+→
        # to expand and see fetch / M1-M4 details.
        if sample > 0:
            self._log(Text(f"▸ Auditing {label} ({sample} instances)...  Ctrl+→ to see details",
                           style=f"bold {self.cockpit_theme.accent}"))
        else:
            self._log(Text(f"▸ Auditing {label} (all instances)...  Ctrl+→ to see details",
                           style=f"bold {self.cockpit_theme.accent}"))
        self._update_status()
        self._clear_reasoning_log(f"Reasoning: /pro {sample if sample > 0 else 'all'}")
        self._cancel_requested = False
        # Do NOT auto-expand — user chooses when to open the black box
        self._current_worker = self.run_worker(
            lambda: self._do_pro_audit_threaded(data_path, custom, sample),
            thread=True,
            exclusive=True,
            start=True,
        )

    def _do_pro_audit_threaded(self, data_path: Path, custom: bool, sample: int = 0):
        """Sync function that runs in a background thread. Updates the UI
        via _safe_call so the spinner keeps animating."""
        from .pro_audit import audit_swebench_pro, results_to_report
        from .report import write_json, write_markdown, write_html

        def on_progress(current, total, msg):
            self.state.phase = msg[:40]
            self.state.total_instances = total
            step = max(1, total // 10)
            if current % step == 0 or current == total:
                self._safe_call(self._log,
                    Text(f"  [{current}/{total}] {msg}", style=self.cockpit_theme.fg_dim))
                self._safe_call(self._update_status)

        def on_reasoning(instance_id, text):
            """Live reasoning callback — ONLY updates the thinking panel.
            Does NOT log to main console (user expands with Ctrl+→ to see)."""
            self._safe_call(self._log_reasoning, text)

        def cancel_check():
            return self._cancel_requested

        def do_work():
            return audit_swebench_pro(data_path, sample_size=sample,
                                      on_progress=on_progress,
                                      on_reasoning=on_reasoning,
                                      cancel_check=cancel_check)

        try:
            results = do_work()
        except Exception as exc:
            self._safe_call(self._log,
                Text(f"  ✗ Audit failed: {exc}", style=f"bold {self.cockpit_theme.crit}"))
            self.state.audit_running = False
            self.state.phase = "failed"
            self._safe_call(self._update_status)
            return

        # Check if cancelled
        if self._cancel_requested:
            self._safe_call(self._log,
                Text(f"  ■ Audit stopped. {len(results)} instances processed before stop.",
                     style=f"bold {self.cockpit_theme.crit}"))
            self.state.audit_running = False
            self.state.phase = "stopped"
            self._safe_call(self._update_status)
            if not results:
                return

        report = results_to_report(results)
        if custom:
            report.config.benchmark = f"custom ({data_path.name})"

        s = report.summary
        self.state.contaminated_count = s.instances_with_patch_exposure
        self.state.total_instances = s.total_instances
        self.state.audit_running = False
        self.state.audit_complete = True
        self.state.phase = "complete"

        # Build summary table — matches CLI output exactly
        table = Table(title=f"{report.config.benchmark} Summary", show_header=True,
                      header_style=f"bold {self.cockpit_theme.accent}")
        table.add_column("Metric", style="dim")
        table.add_column("Value", justify="right")
        table.add_row("Total instances", str(s.total_instances))
        table.add_row("Patch exposed", f"{s.instances_with_patch_exposure} ({s.patch_exposure_rate*100:.1f}%)")
        table.add_row("Problem statement exposed", f"{s.instances_with_problem_statement_exposure} ({s.problem_statement_exposure_rate*100:.1f}%)")
        table.add_row("Test patch exposed", f"{s.instances_with_test_patch_exposure} ({s.test_patch_exposure_rate*100:.1f}%)")
        table.add_row("Repos in training corpus", f"{s.instances_with_repo_in_corpus} ({s.repo_in_corpus_rate*100:.1f}%)")
        table.add_row("Contamination rate", f"{s.contamination_rate*100:.2f}%", style=f"bold {self.cockpit_theme.crit}")
        table.add_row("Critical", str(s.critical_count), style=self.cockpit_theme.crit)
        table.add_row("High", str(s.high_count), style=self.cockpit_theme.high)
        table.add_row("Medium", str(s.medium_count), style=self.cockpit_theme.medium)
        table.add_row("Low", str(s.low_count))
        table.add_row("Clean", str(s.clean_count), style=self.cockpit_theme.clean)

        # Write reports — use ABSOLUTE paths so they work from any CWD
        out_dir = Path.cwd() / "examples" / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        name = f"custom_{data_path.stem}" if custom else "swebench_pro_audit"
        json_path = write_json(report, out_dir / f"{name}.json")
        md_path = write_markdown(report, out_dir / f"{name}.md")
        html_path = write_html(report, out_dir / f"{name}.html")
        # Store ABSOLUTE paths
        self.state.report_paths = {
            "json": str(json_path.resolve()),
            "md": str(md_path.resolve()),
            "html": str(html_path.resolve()),
        }

        # Log results — matches CLI output format
        self._safe_call(self._log, Text(""))
        self._safe_call(self._log,
            Text(f"● Audit Complete — {s.contamination_rate*100:.1f}% contaminated",
                 style=f"bold {self.cockpit_theme.accent_2}"))
        self._safe_call(self._log, Text(""))
        self._safe_call(self._log, table)
        self._safe_call(self._log, Text(""))
        self._safe_call(self._log, Text("Reports written:", style=f"bold {self.cockpit_theme.accent}"))
        self._safe_call(self._log, Text(f"  json      {json_path.resolve()}", style=self.cockpit_theme.accent_2))
        self._safe_call(self._log, Text(f"  markdown  {md_path.resolve()}", style=self.cockpit_theme.accent_2))
        self._safe_call(self._log, Text(f"  html      {html_path.resolve()}", style=self.cockpit_theme.accent_2))
        self._safe_call(self._log, Text(""))
        # Auto-open HTML in browser (use absolute path)
        from .report import open_in_browser
        opened = open_in_browser(html_path.resolve())
        if opened:
            self._safe_call(self._log,
                Text("  ↗ Opened HTML report in browser", style=f"bold {self.cockpit_theme.accent_2}"))
        else:
            self._safe_call(self._log,
                Text("  Type /open to open the report in browser", style=f"bold {self.cockpit_theme.accent}"))
        self._safe_call(self._log, Text(""))
        self._safe_call(self._update_status)

    def _run_verified_audit(self, data_path: Path, sample: int = 0) -> None:
        """Run SWE-bench Verified audit in a background THREAD."""
        if self.state.audit_running:
            self._log(Text("  Audit already running.", style=self.cockpit_theme.medium))
            return
        self.state.audit_running = True
        self.state.start_ts = time.time()
        self.state.audit_complete = False
        self.state.phase = "starting"
        self._log(Text(""))
        # Black box starts collapsed; user presses Ctrl+→ to expand.
        if sample > 0:
            self._log(Text(f"▸ Auditing SWE-bench Verified ({sample} instances)...  Ctrl+→ to see details",
                           style=f"bold {self.cockpit_theme.accent}"))
        else:
            self._log(Text(f"▸ Auditing SWE-bench Verified (all instances)...  Ctrl+→ to see details",
                           style=f"bold {self.cockpit_theme.accent}"))
        self._update_status()
        self._clear_reasoning_log(f"Reasoning: /audit {sample if sample > 0 else 'all'}")
        self._cancel_requested = False
        # Do NOT auto-expand — user chooses when to open the black box
        self._current_worker = self.run_worker(
            lambda: self._do_verified_audit_threaded(data_path, sample),
            thread=True,
            exclusive=True,
            start=True,
        )

    def _do_verified_audit_threaded(self, data_path: Path, sample: int = 0):
        """Sync function that runs in a background thread."""
        from .engine import AgentSealEngine
        from .loaders import load_swebench_verified, load_swebench_sample
        from .report import write_json, write_markdown, write_html
        from .schemas import AuditConfig

        def on_progress(phase, completed, total, msg):
            self.state.phase = phase
            self.state.total_instances = total
            step = max(1, total // 10)
            if completed % step == 0 or completed == total:
                self._safe_call(self._log,
                    Text(f"  [{completed}/{total}] {phase}", style=self.cockpit_theme.fg_dim))
                self._safe_call(self._update_status)

        def on_evidence(instance_id, risk, match_type, similarity, msg):
            from .schemas import RiskLevel
            if risk in (RiskLevel.CRITICAL, RiskLevel.HIGH):
                color = self.cockpit_theme.crit if risk == RiskLevel.CRITICAL else self.cockpit_theme.high
                # Evidence goes to thinking panel only, not main console
                self._safe_call(self._log_reasoning,
                    f"[{risk.value.upper():7}] {instance_id[:40]}  {msg[:60]}")

        def on_reasoning(instance_id, text):
            """Live reasoning callback — ONLY updates the thinking panel."""
            self._safe_call(self._log_reasoning, text)

        def cancel_check():
            return self._cancel_requested

        def do_work():
            if sample > 0:
                instances = load_swebench_sample(data_path, n=sample)
            else:
                instances = load_swebench_verified(data_path)
            config = AuditConfig(benchmark="swe-bench-verified", corpus_source="github-pr-diffs")
            engine = AgentSealEngine(instances=instances, config=config,
                                     on_progress=on_progress, on_evidence=on_evidence,
                                     on_reasoning=on_reasoning, cancel_check=cancel_check)
            return engine.run()

        try:
            report = do_work()
        except Exception as exc:
            self._safe_call(self._log,
                Text(f"  ✗ Audit failed: {exc}", style=f"bold {self.cockpit_theme.crit}"))
            self.state.audit_running = False
            self.state.phase = "failed"
            self._safe_call(self._update_status)
            return

        # Check if cancelled
        if self._cancel_requested:
            self._safe_call(self._log,
                Text("  ■ Audit stopped.", style=f"bold {self.cockpit_theme.crit}"))
            self.state.audit_running = False
            self.state.phase = "stopped"
            self._safe_call(self._update_status)
            return

        s = report.summary
        self.state.contaminated_count = s.instances_with_patch_exposure
        self.state.total_instances = s.total_instances
        self.state.audit_running = False
        self.state.audit_complete = True
        self.state.phase = "complete"

        table = Table(title="SWE-bench Verified Summary", show_header=True,
                      header_style=f"bold {self.cockpit_theme.accent}")
        table.add_column("Metric", style="dim")
        table.add_column("Value", justify="right")
        table.add_row("Total instances", str(s.total_instances))
        table.add_row("Patch exposed", f"{s.instances_with_patch_exposure} ({s.patch_exposure_rate*100:.1f}%)")
        table.add_row("Problem statement exposed", f"{s.instances_with_problem_statement_exposure} ({s.problem_statement_exposure_rate*100:.1f}%)")
        table.add_row("Test patch exposed", f"{s.instances_with_test_patch_exposure} ({s.test_patch_exposure_rate*100:.1f}%)")
        table.add_row("Repos in training corpus", f"{s.instances_with_repo_in_corpus} ({s.repo_in_corpus_rate*100:.1f}%)")
        table.add_row("Contamination rate", f"{s.contamination_rate*100:.2f}%", style=f"bold {self.cockpit_theme.crit}")
        table.add_row("Critical", str(s.critical_count), style=self.cockpit_theme.crit)
        table.add_row("High", str(s.high_count), style=self.cockpit_theme.high)
        table.add_row("Medium", str(s.medium_count), style=self.cockpit_theme.medium)
        table.add_row("Low", str(s.low_count))
        table.add_row("Clean", str(s.clean_count), style=self.cockpit_theme.clean)

        out_dir = Path.cwd() / "examples" / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        json_path = write_json(report, out_dir / "agentseal_audit.json")
        md_path = write_markdown(report, out_dir / "agentseal_audit.md")
        html_path = write_html(report, out_dir / "agentseal_audit.html")
        self.state.report_paths = {
            "json": str(json_path.resolve()),
            "md": str(md_path.resolve()),
            "html": str(html_path.resolve()),
        }

        self._safe_call(self._log, Text(""))
        self._safe_call(self._log,
            Text(f"● Audit Complete — {s.contamination_rate*100:.1f}% contaminated",
                 style=f"bold {self.cockpit_theme.accent_2}"))
        self._safe_call(self._log, Text(""))
        self._safe_call(self._log, table)
        self._safe_call(self._log, Text(""))
        self._safe_call(self._log, Text("Reports written:", style=f"bold {self.cockpit_theme.accent}"))
        self._safe_call(self._log, Text(f"  json      {json_path.resolve()}", style=self.cockpit_theme.accent_2))
        self._safe_call(self._log, Text(f"  markdown  {md_path.resolve()}", style=self.cockpit_theme.accent_2))
        self._safe_call(self._log, Text(f"  html      {html_path.resolve()}", style=self.cockpit_theme.accent_2))
        self._safe_call(self._log, Text(""))
        # Auto-open HTML in browser
        from .report import open_in_browser
        opened = open_in_browser(html_path.resolve())
        if opened:
            self._safe_call(self._log,
                Text("  ↗ Opened HTML report in browser", style=f"bold {self.cockpit_theme.accent_2}"))
        else:
            self._safe_call(self._log,
                Text("  Type /open to open the report in browser", style=f"bold {self.cockpit_theme.accent}"))
        self._safe_call(self._log, Text(""))
        self._safe_call(self._update_status)

    def action_expand_thinking(self) -> None:
        """Ctrl+Right — expand the reasoning box (Textual Collapsible widget).

        Uses the Collapsible widget's built-in collapsed property for a
        clean, animated expand. No more manual ┌─/└─ markers.
        """
        try:
            box = self.query_one("#reasoning-box")
            box.collapsed = False
        except Exception:
            pass

    def action_collapse_thinking(self) -> None:
        """Ctrl+Left — collapse the reasoning box."""
        try:
            box = self.query_one("#reasoning-box")
            box.collapsed = True
        except Exception:
            pass

    def _log_reasoning(self, text):
        """Stream a reasoning event into the reasoning box's RichLog.

        Events ALWAYS go into the #reasoning-log (even when collapsed) so
        the user can expand later and see the full history. The Collapsible
        widget handles the expand/collapse UI cleanly.
        """
        try:
            log = self.query_one("#reasoning-log", RichLog)
            if isinstance(text, str):
                log.write(Text(text, style="dim #b0b0b0"))
            else:
                log.write(text)
        except Exception:
            pass

    def _update_reasoning_title(self) -> None:
        """Update the Collapsible title bar to show live audit status."""
        try:
            box = self.query_one("#reasoning-box")
            from textual.widgets import Collapsible
            if isinstance(box, Collapsible):
                if self.state.audit_running:
                    spinner = SPINNER_FRAMES[self.spinner_frame]
                    phase = self.state.phase[:30] if self.state.phase else "auditing"
                    box.title = f"{spinner} {phase}  (Ctrl+→ expand · Ctrl+← collapse)"
                elif self.state.audit_complete:
                    box.title = f"✓ Audit complete  (Ctrl+→ to review reasoning window)"
                else:
                    box.title = f"▸ Reasoning Window  (Ctrl+→ to expand)"
        except Exception:
            pass

    def _clear_reasoning_log(self, command_label: str = "") -> None:
        """Clear the reasoning log for a new command.

        starts, the reasoning log is cleared so reasoning from the previous
        command doesn't merge with the new one. A separator header is written
        showing which command this reasoning belongs to.
        """
        try:
            log = self.query_one("#reasoning-log", RichLog)
            log.clear()
            if command_label:
                from rich.text import Text as _T
                log.write(_T(f"── {command_label} ──", style="bold #ff8c42 on #0a0a0a"))
                log.write(_T("", style="dim"))
        except Exception:
            pass

    def action_clear_log(self) -> None:
        """Clear the conversation log."""
        try:
            self.query_one("#conversation", RichLog).clear()
            self._log(Text("(cleared)", style=self.cockpit_theme.fg_dim))
        except Exception:
            pass

    def action_quick_audit(self) -> None:
        self._cmd_audit_pro()


# =============================================================================
# Wizard Screen — file browser panel (v0.7)
# =============================================================================

class WizardScreen(ModalScreen):
    """Auto-detect benchmark data files on the system.

    Scans Downloads, Desktop, Documents, and home directory for .parquet,
    .jsonl, and .json files. For each candidate, checks if it has the
    required SWE-bench columns (instance_id, repo, patch). Only files
    that pass the column check are shown.

    If no benchmark files are found, shows a clear message.
    """

    CSS = """
    WizardScreen {
        align: center middle;
    }
    #wizard-panel {
        width: 80%;
        height: 80%;
        background: $panel;
        border: solid $accent;
        padding: 1 2;
    }
    #wizard-title {
        color: $accent;
        text-style: bold;
        margin-bottom: 1;
    }
    #wizard-hint {
        color: $foreground 60%;
        margin-bottom: 1;
    }
    #wizard-list {
        height: 1fr;
        border: solid $boost;
        background: $background;
        margin: 1 0;
        scrollbar-size: 1 1;
    }
    #wizard-summary {
        height: 1fr;
        background: $background;
        border: solid $accent;
        padding: 1;
        margin: 1 0;
        display: none;
    }
    #wizard-scanning {
        height: 1fr;
        background: $background;
        border: solid $boost;
        padding: 1;
        margin: 1 0;
        color: $accent;
    }
    """

    SUPPORTED_EXTENSIONS = {'.parquet', '.jsonl', '.json'}
    REQUIRED_COLUMNS = {'instance_id', 'repo', 'patch'}
    # Directories to scan (relative to home). Empty string = home itself.
    # On Windows, also checks OneDrive-redirected locations.
    SCAN_DIRS = ['Downloads', 'Desktop', 'Documents', 'OneDrive/Downloads',
                 'OneDrive/Desktop', 'OneDrive/Documents', '']

    def __init__(self, callback=None):
        super().__init__()
        self._callback = callback
        self._selected_path = None
        self._found_files = []
        self._showing_summary = False  # True when summary panel is visible (success OR error)

    def compose(self) -> ComposeResult:
        from textual.containers import Vertical
        with Vertical(id="wizard-panel"):
            yield Label("🧙 Wizard — Auto-detected benchmark files", id="wizard-title")
            yield Label("↑↓ navigate · Enter select · Esc cancel", id="wizard-hint")
            yield Static("Scanning system for benchmark data files...", id="wizard-scanning")
            yield ListView(id="wizard-list")
            yield Static("", id="wizard-summary")

    def on_mount(self) -> None:
        # Scan in background to avoid blocking
        self.set_timer(0.1, self._scan_system)

    def _scan_system(self) -> None:
        """Scan the system for benchmark data files.

        Searches Downloads, Desktop, Documents, OneDrive variants, and home
        directory for .parquet, .jsonl, .json files. Shows ALL files with
        supported extensions — the column validation happens when the user
        selects a file (_analyze_file). This way:
        - Files with valid columns → show "Run audit? Y/N"
        - Files with missing columns → show clear error
        - Files that fail to load → show clear error
        - No files are silently hidden due to scan-time exceptions
        """
        scanning = self.query_one("#wizard-scanning", Static)
        list_view = self.query_one("#wizard-list", ListView)
        hint = self.query_one("#wizard-hint", Label)

        home = Path.home()
        candidates = []
        scanned_dirs = []

        # Scan each target directory (1 level deep — check subfolders too)
        for dir_name in self.SCAN_DIRS:
            if dir_name:
                scan_dir = home / dir_name
            else:
                scan_dir = home
            if not scan_dir.exists() or not scan_dir.is_dir():
                continue
            scanned_dirs.append(str(scan_dir))
            try:
                for entry in scan_dir.iterdir():
                    if entry.is_file() and entry.suffix.lower() in self.SUPPORTED_EXTENSIONS:
                        if entry.name.startswith('.') or entry.name in ('swebench_verified.parquet', 'swebench_pro.parquet'):
                            continue
                        candidates.append(entry)
                    elif entry.is_dir() and not entry.name.startswith('.'):
                        # Scan one level deep into subfolders
                        subdir = entry
                        scanned_dirs.append(str(subdir))
                        try:
                            for f in subdir.iterdir():
                                if f.is_file() and f.suffix.lower() in self.SUPPORTED_EXTENSIONS:
                                    if f.name.startswith('.') or f.name in ('swebench_verified.parquet', 'swebench_pro.parquet'):
                                        continue
                                    candidates.append(f)
                        except (PermissionError, OSError):
                            continue
            except (PermissionError, OSError):
                continue

        # Deduplicate (a file might appear in both Downloads and OneDrive/Downloads)
        seen = set()
        unique = []
        for f in candidates:
            resolved = str(f.resolve())
            if resolved not in seen:
                seen.add(resolved)
                unique.append(f)
        candidates = unique

        # Build the list — show ALL supported files (validation on select)
        valid_files = []
        for f in candidates:
            try:
                size_str = self._format_size(f.stat().st_size)
                # Try a quick column check for the label (non-fatal)
                col_count = self._quick_column_count(f)
                valid_files.append((f, size_str, col_count))
            except Exception:
                # Even if stat fails, show the file (user can investigate)
                valid_files.append((f, "?", 0))

        self._found_files = valid_files

        # Hide scanning message
        scanning.display = False

        if not valid_files:
            hint.update("No benchmark files found · Esc to close")
            list_view.display = False
            scanning.display = True
            scanning.update(
                f"No benchmark data files found on your system.\n\n"
                f"AgentSeal scanned {len(scanned_dirs)} directories:\n"
                + "\n".join(f"  • {d}" for d in scanned_dirs) + "\n\n"
                f"Looking for: .parquet, .jsonl, .json files\n\n"
                f"Download a benchmark dataset (e.g. from HuggingFace) to:\n"
                f"  ~/Downloads/\n\n"
                f"Press Esc to close."
            )
            return

        # Populate the list with found files
        hint.update(f"Found {len(valid_files)} file(s) · ↑↓ navigate · Enter select · Esc cancel")
        list_view.display = True
        items = []
        for f, size_str, col_count in valid_files:
            if col_count > 0:
                label = Label(f"  📄 {f.name}  ({size_str}, {col_count} cols)  —  {f.parent.name}/")
            else:
                label = Label(f"  📄 {f.name}  ({size_str})  —  {f.parent.name}/")
            item = ListItem(label)
            item._file_path = str(f)
            items.append(item)
        list_view.clear()
        for item in items:
            list_view.append(item)

    def _quick_column_count(self, f: Path) -> int:
        """Quickly count columns without loading the full dataset.

        Returns 0 if the file can't be read (non-fatal — the file still
        shows in the list, and _analyze_file will show the error when
        selected).
        """
        try:
            import pandas as pd
            ext = f.suffix.lower()
            if ext == '.parquet':
                # Read only Parquet metadata; loading the whole dataset here
                # makes /wizard feel frozen on large benchmarks.
                try:
                    import pyarrow.parquet as pq
                    return len(pq.ParquetFile(f).schema_arrow.names)
                except Exception:
                    df = pd.read_parquet(f)
                    return len(df.columns)
            elif ext == '.jsonl':
                import json as _json
                with open(f, 'r', encoding='utf-8') as fh:
                    first_line = fh.readline().strip()
                    if first_line:
                        return len(_json.loads(first_line).keys())
                return 0
            elif ext == '.json':
                df = pd.read_json(f)
                return len(df.columns)
            return 0
        except Exception:
            return 0

    def _format_size(self, size: int) -> str:
        """Format file size as human-readable string."""
        if size < 1024:
            return f"{size}B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f}KB"
        else:
            return f"{size / (1024 * 1024):.1f}MB"

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Handle file selection from the list."""
        path_str = getattr(event.item, '_file_path', None)
        if not path_str:
            return
        path = Path(path_str)
        if path.exists() and path.is_file():
            self._analyze_file(path)

    def _analyze_file(self, path: Path) -> None:
        """Analyze the selected file and show a summary + Y/N prompt.

        Handles all edge cases: empty files, malformed JSON, missing columns.
        """
        import pandas as pd
        import re as _re

        list_view = self.query_one("#wizard-list", ListView)
        summary = self.query_one("#wizard-summary", Static)
        hint = self.query_one("#wizard-hint", Label)

        try:
            ext = path.suffix.lower()
            if ext == ".parquet":
                df = pd.read_parquet(path)
            elif ext == ".jsonl":
                import json as _json
                rows = []
                with open(path, "r", encoding="utf-8") as f:
                    for i, line in enumerate(f, 1):
                        line = line.strip()
                        if line:
                            try:
                                rows.append(_json.loads(line))
                            except _json.JSONDecodeError as e:
                                # Show clear error for malformed JSON
                                list_view.display = False
                                summary.display = True
                                self._showing_summary = True
                                hint.update("Press N to go back, Esc to cancel")
                                summary.update(
                                    f"✗ Malformed JSON on line {i}\n"
                                    f"   File: {path.name}\n"
                                    f"   Error: {e}\n\n"
                                    f"   Fix the JSON or remove the bad line.\n"
                                    f"   Press N to select a different file."
                                )
                                return
                df = pd.DataFrame(rows)
            elif ext == ".json":
                df = pd.read_json(path)
            else:
                return

            total = len(df)
            cols = list(df.columns)
            size_str = self._format_size(path.stat().st_size)

            # Check for empty dataset
            if total == 0:
                list_view.display = False
                summary.display = True
                self._showing_summary = True
                hint.update("Press N to go back, Esc to cancel")
                summary.update(
                    f"📄 {path.name} ({size_str})\n"
                    f"   Format: {ext}\n"
                    f"   ✗ Empty dataset (0 instances)\n\n"
                    f"   This file has no data rows.\n"
                    f"   Press N to select a different file."
                )
                return

            from .auto_discover import detect_audit_type, detect_schema
            schema = detect_schema(df)
            required = ["instance_id", "patch"]
            missing = [c for c in required if c not in schema]
            if missing:
                list_view.display = False
                summary.display = True
                self._showing_summary = True
                hint.update("Press N to go back, Esc to cancel")
                summary.update(
                    f"📄 {path.name} ({size_str})\n"
                    f"   Format: {ext}\n"
                    f"   Instances: {total}\n"
                    f"   Columns: {cols}\n"
                    f"   ✗ Could not map required fields: {missing}\n\n"
                    f"   Need aliases for: {required}\n\n"
                    f"   Press N to select a different file."
                )
                return

            audit_type = detect_audit_type(df, schema)

            list_view.display = False
            summary.display = True
            self._showing_summary = True
            hint.update("Press Y or Enter to run audit · N to go back · Esc to cancel")

            summary.update(
                f"📄 {path.name} ({size_str})\n"
                f"   Format: {ext}\n"
                f"   Instances: {total}\n"
                f"   Columns: {cols}\n"
                f"   Schema: {schema}\n"
                f"   Detected: {audit_type} audit\n\n"
                f"   ▸ Run audit? Press Y or Enter for yes, N for no"
            )
            self._selected_path = path

        except Exception as e:
            list_view.display = False
            summary.display = True
            self._showing_summary = True
            hint.update("Press N to go back, Esc to cancel")
            summary.update(
                f"✗ Failed to load: {path.name}\n"
                f"   Error: {e}\n\n"
                f"   Press N to select a different file."
            )

    def on_key(self, event) -> None:
        """Handle Y/N/Enter/Esc keypresses.

        Uses _showing_summary flag to handle both success and error states.
        When summary is visible: Y/Enter confirms, N goes back, Esc closes.
        When summary is NOT visible (file list): only Esc closes, ListView
        handles arrow keys and Enter for navigation.
        """
        if self._showing_summary:
            # Summary panel is visible (either success with Y/N prompt,
            # or error with N/Esc prompt)
            if event.key in ("y", "Y", "enter") and self._selected_path is not None:
                event.prevent_default()
                event.stop()
                self.dismiss(self._selected_path)
            elif event.key in ("n", "N"):
                # Go back to file list (works for both success and error)
                event.prevent_default()
                event.stop()
                self._selected_path = None
                self._showing_summary = False
                list_view = self.query_one("#wizard-list", ListView)
                summary = self.query_one("#wizard-summary", Static)
                hint = self.query_one("#wizard-hint", Label)
                if self._found_files:
                    list_view.display = True
                    summary.display = False
                    hint.update(f"Found {len(self._found_files)} benchmark file(s) · ↑↓ navigate · Enter select · Esc cancel")
                else:
                    list_view.display = False
                    summary.display = False
                    hint.update("No benchmark files found · Esc to close")
            elif event.key == "escape":
                event.prevent_default()
                event.stop()
                self.dismiss(None)
            else:
                # Swallow all other keys to prevent bubbling
                event.prevent_default()
                event.stop()
        else:
            # In the file list view — only handle escape
            if event.key == "escape":
                event.prevent_default()
                event.stop()
                self.dismiss(None)


def run_tui():
    """Launch the AgentSeal v5.0.0 TUI (full-screen Textual app).

    Always tries the Textual TUI first. Only falls back to interactive mode
    if Textual is unavailable or crashes. The previous isatty() check caused
    the TUI to not launch when running through .cmd shims or certain
    terminal configurations.
    """
    import sys
    try:
        app = AgentSealApp()
        app.run()
    except Exception as exc:
        from rich.console import Console
        c = Console()
        c.print(f"[yellow]TUI unavailable ({exc}); falling back to interactive mode.[/yellow]")
        from .interactive import run_interactive
        run_interactive()


__all__ = ["AgentSealApp", "run_tui"]
