"""TmuxMultiplexer — libtmux-backed `Multiplexer`.

Models *tmux windows* as `Pane`s (paige's domain term — see
`domain/pane.py` docstring). One libtmux Server can hold multiple
tmux sessions; we iterate all of them so a pane started by hand
in any session is still discoverable + manageable.

All blocking libtmux calls are wrapped in `asyncio.to_thread`.

Quirk worth keeping: when sending a typed prompt to Claude Code's
TUI, sending text + Enter as a single batch can be interpreted as
a newline rather than submit. Splitting into "send text" → 0.5 s
sleep → "send Enter" works reliably. We replicate v1's behavior
here for `enter=True, literal=True` calls.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import libtmux  # type: ignore[import-untyped]

from ..domain.host import LOCAL_HOST_ID
from ..domain.pane import Pane

if TYPE_CHECKING:  # pragma: no cover
    pass

logger = logging.getLogger(__name__)


class TmuxMultiplexer:
    """libtmux-backed Multiplexer. Implements `paige.ports.multiplexer.Multiplexer`.

    `default_session` is where new panes go by default. If the
    session doesn't exist yet, `create_pane` creates it.

    `socket_name` overrides libtmux's default socket — useful in
    tests to keep paige's tmux server isolated from the user's.
    """

    def __init__(
        self,
        default_session: str = "paige",
        socket_name: str | None = None,
    ) -> None:
        self._default_session = default_session
        self._socket_name = socket_name
        self._server: libtmux.Server | None = None

    # ── server lazy init ─────────────────────────────────────────

    def _get_server(self) -> libtmux.Server:
        if self._server is None:
            kwargs: dict[str, Any] = {}
            if self._socket_name is not None:
                kwargs["socket_name"] = self._socket_name
            self._server = libtmux.Server(**kwargs)
        return self._server

    # ── pane discovery ───────────────────────────────────────────

    async def list_panes(self, *, host_id: str = LOCAL_HOST_ID) -> list[Pane]:
        # `host_id` is part of the Multiplexer Protocol so the
        # MultiplexerRouter can dispatch by host. This adapter is the
        # local-host concrete impl — it ignores the parameter and
        # always reads the box paige itself runs on.
        del host_id
        return await asyncio.to_thread(self._list_panes_sync)

    def _list_panes_sync(self) -> list[Pane]:
        out: list[Pane] = []
        for session in self._get_server().sessions:
            for window in session.windows:
                pane = self._window_to_domain(window, session.session_name or "")
                if pane is not None:
                    out.append(pane)
        return out

    @staticmethod
    def _window_to_domain(window: Any, session_name: str) -> Pane | None:
        wid: str | None = window.window_id
        wname: str | None = window.window_name
        if wid is None:
            return None
        active = window.active_pane
        cwd_str: str = active.pane_current_path if active is not None else ""
        return Pane(
            pane_id=wid,
            pane_name=wname or "",
            cwd=Path(cwd_str) if cwd_str else Path(),
            multiplexer_session=session_name,
        )

    async def find_pane(self, pane_id: str, *, host_id: str = LOCAL_HOST_ID) -> Pane | None:
        del host_id  # local-only adapter; see list_panes for context.
        return await asyncio.to_thread(self._find_pane_sync, pane_id)

    def _find_pane_sync(self, pane_id: str) -> Pane | None:
        for session in self._get_server().sessions:
            for window in session.windows:
                if window.window_id == pane_id:
                    return self._window_to_domain(window, session.session_name or "")
        return None

    def _find_window_sync(self, pane_id: str) -> Any | None:
        for session in self._get_server().sessions:
            for window in session.windows:
                if window.window_id == pane_id:
                    return window
        return None

    # ── lifecycle ────────────────────────────────────────────────

    async def create_pane(
        self,
        name: str,
        cwd: Path,
        command: str = "",
        *,
        host_id: str = LOCAL_HOST_ID,
    ) -> Pane:
        del host_id
        return await asyncio.to_thread(self._create_pane_sync, name, cwd, command)

    def _create_pane_sync(self, name: str, cwd: Path, command: str) -> Pane:
        server = self._get_server()
        session = server.sessions.filter(session_name=self._default_session)
        sess = (
            session[0]
            if session
            else server.new_session(
                session_name=self._default_session,
                attach=False,
                start_directory=str(cwd),
            )
        )
        window = sess.new_window(
            window_name=name,
            start_directory=str(cwd),
            attach=False,
        )
        if command:
            pane = window.active_pane
            if pane is not None:
                pane.send_keys(command, enter=True, literal=True)
        result = self._window_to_domain(window, sess.session_name or "")
        if result is None:
            raise RuntimeError(f"new window has no window_id (name={name!r})")
        return result

    async def kill_pane(self, pane_id: str, *, host_id: str = LOCAL_HOST_ID) -> bool:
        del host_id
        return await asyncio.to_thread(self._kill_pane_sync, pane_id)

    def _kill_pane_sync(self, pane_id: str) -> bool:
        window = self._find_window_sync(pane_id)
        if window is None:
            return False
        window.kill()
        return True

    async def rename_pane(
        self, pane_id: str, new_name: str, *, host_id: str = LOCAL_HOST_ID
    ) -> bool:
        del host_id
        return await asyncio.to_thread(self._rename_pane_sync, pane_id, new_name)

    def _rename_pane_sync(self, pane_id: str, new_name: str) -> bool:
        window = self._find_window_sync(pane_id)
        if window is None:
            return False
        window.rename_window(new_name)
        return True

    # ── I/O ──────────────────────────────────────────────────────

    async def send_keys(
        self,
        pane_id: str,
        text: str,
        *,
        enter: bool = True,
        literal: bool = True,
        host_id: str = LOCAL_HOST_ID,
    ) -> bool:
        del host_id  # see list_panes for context.
        # Claude Code's TUI sometimes reads a rapid Enter after text
        # as a newline rather than submit. Split text + 0.5 s + Enter
        # for the literal-with-enter case; everything else (special
        # keys like Up / Escape, no-enter sends) goes straight through.
        if literal and enter:
            ok = await asyncio.to_thread(self._send_keys_sync, pane_id, text, False, True)
            if not ok:
                return False
            await asyncio.sleep(0.5)
            return await asyncio.to_thread(self._send_keys_sync, pane_id, "", True, False)
        return await asyncio.to_thread(self._send_keys_sync, pane_id, text, enter, literal)

    def _send_keys_sync(self, pane_id: str, text: str, enter: bool, literal: bool) -> bool:
        window = self._find_window_sync(pane_id)
        if window is None:
            return False
        pane = window.active_pane
        if pane is None:
            return False
        # Exit copy-mode (or any other key-intercepting mode) first.
        # With `mouse on`, a mouse-wheel scroll silently puts tmux
        # into copy-mode; once there, every character we send is
        # interpreted by copy-mode's key handler. The default
        # bindings include `g` → `(goto line)` prompt, `f` → `(jump
        # forward)`, etc., which open a tmux command-prompt that
        # eats the rest of the input. Sending `q` first invokes
        # copy-mode's `cancel` binding (q → cancel) and drops us
        # back to the live pane; the actual text then reaches the
        # running program. `pane_in_mode` is tmux's `0`/`1` flag.
        in_mode = self._pane_in_mode(pane)
        if in_mode:
            try:
                pane.send_keys("q", enter=False, literal=False)
            except Exception as e:
                logger.debug("copy-mode exit (q) failed for %s: %s", pane_id, e)
        try:
            pane.send_keys(text, enter=enter, literal=literal)
        except Exception as e:
            logger.debug("send_keys failed for %s: %s", pane_id, e)
            return False
        return True

    @staticmethod
    def _pane_in_mode(pane: Any) -> bool:
        """True iff the pane is currently in a tmux-side mode
        (copy-mode, view-mode, customize-mode, ...).

        libtmux 0.56 doesn't expose `pane_in_mode` as a Pane
        attribute — only `copy_mode` / `clock_mode` / etc. *methods*
        for entering modes. We query directly via
        `display-message -p '#{pane_in_mode}'` against the pane's
        own server. Defensive against errors — when in doubt return
        False so we don't inject an unwanted `q` into a normal
        pane."""
        try:
            server = pane.server
            out = server.cmd("display-message", "-p", "-t", pane.pane_id, "#{pane_in_mode}")
            stdout: Any = out.stdout
        except Exception:
            return False
        if not stdout:
            return False
        first = stdout[0] if isinstance(stdout, list) else stdout  # type: ignore[reportUnknownArgumentType]
        return str(first).strip() == "1"  # type: ignore[reportUnknownArgumentType]

    async def capture(self, pane_id: str, *, host_id: str = LOCAL_HOST_ID) -> str | None:
        del host_id
        return await asyncio.to_thread(self._capture_sync, pane_id, False)

    async def capture_with_ansi(self, pane_id: str, *, host_id: str = LOCAL_HOST_ID) -> str | None:
        del host_id
        return await asyncio.to_thread(self._capture_sync, pane_id, True)

    def _capture_sync(self, pane_id: str, with_ansi: bool) -> str | None:
        window = self._find_window_sync(pane_id)
        if window is None:
            return None
        pane = window.active_pane
        if pane is None:
            return None
        try:
            lines: Any = pane.capture_pane(escape_sequences=with_ansi)
        except Exception as e:
            logger.debug("capture failed for %s: %s", pane_id, e)
            return None
        if isinstance(lines, list):
            parts: list[str] = [str(line) for line in lines]  # type: ignore[reportUnknownVariableType]
            return "\n".join(parts)
        return str(lines)

    async def get_foreground_pid(self, pane_id: str, *, host_id: str = LOCAL_HOST_ID) -> int | None:
        del host_id
        return await asyncio.to_thread(self._get_foreground_pid_sync, pane_id)

    def _get_foreground_pid_sync(self, pane_id: str) -> int | None:
        window = self._find_window_sync(pane_id)
        if window is None:
            return None
        pane = window.active_pane
        if pane is None:
            return None
        pid_str = pane.pane_pid
        if pid_str is None:
            return None
        try:
            return int(pid_str)
        except (TypeError, ValueError):
            return None
