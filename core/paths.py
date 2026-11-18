"""Where programs live on *this* PC (so Jarvis works on any computer, not just the one it was built on)."""

import os
import shutil
import winreg
from functools import lru_cache
from pathlib import Path

PF = os.environ.get("ProgramFiles", r"C:\Program Files")
PF86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
LOCAL = os.environ.get("LOCALAPPDATA", "")


def _first(*candidates) -> str:
    for c in candidates:
        if c and Path(c).exists():
            return str(c)
    return ""


def _reg(root, key, value) -> str:
    try:
        with winreg.OpenKey(root, key) as k:
            return str(winreg.QueryValueEx(k, value)[0])
    except OSError:
        return ""


@lru_cache
def chrome() -> str:
    return _first(Path(PF) / r"Google\Chrome\Application\chrome.exe",
                  Path(PF86) / r"Google\Chrome\Application\chrome.exe",
                  Path(LOCAL) / r"Google\Chrome\Application\chrome.exe",
                  _reg(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe", ""))


@lru_cache
def edge() -> str:
    return _first(Path(PF86) / r"Microsoft\Edge\Application\msedge.exe", Path(PF) / r"Microsoft\Edge\Application\msedge.exe")


@lru_cache
def browser() -> str:
    """Chrome if installed, otherwise Edge (which every Windows PC has). Used for the HUD window."""
    return chrome() or edge()


@lru_cache
def steam_root() -> str:
    exe = _reg(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamExe")
    if exe and Path(exe).exists():
        return str(Path(exe).parent)
    return _first(Path(PF86) / "Steam", Path(PF) / "Steam")


@lru_cache
def steam_exe() -> str:
    root = steam_root()
    return _first(Path(root) / "steam.exe") if root else ""


@lru_cache
def vscode() -> str:
    return _first(Path(LOCAL) / r"Programs\Microsoft VS Code\Code.exe", Path(PF) / r"Microsoft VS Code\Code.exe",
                  shutil.which("code") and str(Path(shutil.which("code")).parent.parent / "Code.exe"))


@lru_cache
def discord_updater() -> str:
    return _first(Path(LOCAL) / r"Discord\Update.exe")
