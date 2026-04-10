"""
MenuSystem - minimal finite-state container for UI navigation.

The TerminalUI owns its own per-state item lists; this class is just
a typed state stack with a context dict so UI can remember
selections between transitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional


class MenuState(Enum):
    MAIN_MENU = 1
    LIVE_DATA = 2
    DTC_READ = 3
    DTC_CLEAR_CONFIRM = 4
    MODULE_SCAN = 5
    BIDIR_GATE = 6           # VIN / advanced-mode status + unlock
    BIDIR_MENU = 7           # list of bidir commands for active profile
    CAPABILITY_SCAN = 8
    SESSION_INFO = 9
    DATA_LOGGING_MENU = 10
    SETTINGS = 11


@dataclass
class MenuItem:
    label: str
    action: Callable[[], None]
    enabled: bool = True
    data: Any = None


class MenuSystem:
    def __init__(self) -> None:
        self.current_state: MenuState = MenuState.MAIN_MENU
        self.history: list[tuple[MenuState, dict]] = []
        self.context: dict = {}

    def navigate(self, state: MenuState, context: Optional[dict] = None) -> None:
        self.history.append((self.current_state, dict(self.context)))
        self.current_state = state
        self.context = context or {}

    def back(self) -> MenuState:
        if self.history:
            self.current_state, self.context = self.history.pop()
        return self.current_state

    def get_current_state(self) -> MenuState:
        return self.current_state
