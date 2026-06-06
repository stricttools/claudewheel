"""Default values for config, segments, options, state, and themes."""

from __future__ import annotations

from pathlib import Path


DISALLOWED_TOOLS = [
    "EnterPlanMode",
    "EnterWorktree",
    "ExitPlanMode",
    "ExitWorktree",
    "LSP",
    "NotebookEdit",
    "PushNotification",
    "RemoteTrigger",
    "Skill",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskOutput",
    "TaskStop",
    "TaskUpdate",
]

def build_canonical_shared_settings(scripts_dir: Path) -> dict:
    """Build the canonical shared-settings dict from current defaults.

    The hooks section mirrors what _HOOKS_TEMPLATE used to define in wizard.py.
    The disallowedTools section comes from DISALLOWED_TOOLS above.
    """
    return {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "matcher": "",
                    "hooks": [
                        {"type": "command", "command": str(scripts_dir / "hook-timestamp")},
                        {"type": "command", "command": str(scripts_dir / "hook-stamp-origin")},
                    ],
                }
            ],
            "PreToolUse": [
                {
                    "matcher": "Agent",
                    "hooks": [
                        {"type": "command", "command": str(scripts_dir / "hook-block-worktree")},
                    ],
                }
            ],
        },
        "disallowedTools": DISALLOWED_TOOLS[:],
    }


DEFAULT_CONFIG = {
    "theme": "dark",
    "enabled_segments": [
        "profile",
        "github",
        "version",
        "model",
        "directory",
        "mcp",
        "permissions",
    ],
    "default_flags": ["--dangerously-skip-permissions"],
    "health_check_on_launch": True,
    "minimap": "auto",
    "_schema_version": 0,
}

DEFAULT_SEGMENTS = [
    {
        "key": "profile",
        "label": "Profile",
        "show_options": True,
        "wrap": True,
        "min_width": 8,
        "max_width": 16,
        "required": True,
        "print_mode": True,
        "searchable": False,
        "tab_advances": True,
        "creatable": True,
    },
    {
        "key": "github",
        "label": "GH",
        "show_options": True,
        "wrap": True,
        "min_width": 4,
        "max_width": 12,
        "required": False,
        "print_mode": False,
        "searchable": False,
        "tab_advances": True,
        "creatable": True,
    },
    {
        "key": "version",
        "label": "Ver",
        "show_options": True,
        "wrap": True,
        "min_width": 6,
        "max_width": 10,
        "required": True,
        "print_mode": True,
        "searchable": False,
        "tab_advances": True,
    },
    {
        "key": "model",
        "label": "Model",
        "show_options": True,
        "wrap": True,
        "min_width": 10,
        "max_width": 24,
        "required": False,
        "print_mode": True,
        "searchable": True,
        "tab_advances": True,
        "creatable": True,
    },
    {
        "key": "directory",
        "label": "Dir",
        "show_options": True,
        "wrap": False,
        "min_width": 10,
        "max_width": 40,
        "required": True,
        "print_mode": True,
        "searchable": True,
        "freeform": True,
        "tab_advances": True,
    },
    {
        "key": "mcp",
        "label": "MCP",
        "show_options": True,
        "wrap": True,
        "min_width": 6,
        "max_width": 12,
        "required": False,
        "print_mode": False,
        "searchable": False,
        "tab_advances": True,
    },
    {
        "key": "permissions",
        "label": "Perms",
        "show_options": True,
        "wrap": True,
        "min_width": 6,
        "max_width": 12,
        "required": False,
        "print_mode": False,
        "searchable": False,
        "tab_advances": True,
    },
]

DEFAULT_OPTIONS = {
    "profile": {
        "values": [],
        "discovery": {
            "type": "claude_config_scan",
            "base_dir": "~",
        },
    },
    "github": {
        "values": [],
        "discovery": {
            "type": "gh_auth",
        },
    },
    "version": {
        "values": [],
        "discovery": {
            "type": "npm_and_local",
            "path": "~/.local/share/claude/versions",
            "count": 15,
        },
    },
    "directory": {
        "values": [],
        "discovery": {
            "type": "directory_scan",
            "parents": ["~/Projects", "~/repos", "~/src", "~/code", "~/dev", "~/Work", "~/work"],
            "state_field": "recent_dirs",
        },
    },
    "model": {
        # The [1m] suffix enables the 1M token context window for models that
        # support it (Opus 4.6, Opus 4.7, Sonnet 4.6). Claude Code strips the
        # suffix before sending to the API.
        "values": [
            "claude-opus-4-7",
            "claude-opus-4-7[1m]",
            "claude-opus-4-6",
            "claude-opus-4-6[1m]",
            "claude-sonnet-4-6",
            "claude-sonnet-4-6[1m]",
            "claude-haiku-4-5-20251001",
            "claude-sonnet-4-5-20241022",
        ],
    },
    "mcp": {"values": ["default", "strict"]},
    "permissions": {
        "values": [
            "bypass",
            "default",
            "plan",
        ],
    },
}

DEFAULT_STATE = {
    "last_config": {},
    "recent_dirs": [],
    "launch_count": 0,
}

DEFAULT_THEME_DARK = {
    "name": "dark",
    "global": {
        "bg": None,
        "fg": "#e0e0e0",
        "label_fg": "#888888",
        "separator_fg": "#444444",
        "separator_char": " | ",
        "empty_value_fg": "#555555",
        "empty_value_text": "---",
    },
    "segments": {
        "profile": {
            "value_fg": "#7ec8e3",
            "focus_bg": "#2a2a4e",
            "focus_fg": "#ffffff",
            "option_fg": "#5a8ea3",
        },
        "github": {
            "value_fg": "#a8d8a8",
            "focus_bg": "#2a4e2a",
            "focus_fg": "#ffffff",
            "option_fg": "#6a9a6a",
        },
        "version": {
            "value_fg": "#e8c87e",
            "focus_bg": "#4e4a2a",
            "focus_fg": "#ffffff",
            "option_fg": "#a8984e",
            "unavailable_fg": "#555555",
        },
        "model": {
            "value_fg": "#b8e8b8",
            "focus_bg": "#2a4e3a",
            "focus_fg": "#ffffff",
            "option_fg": "#6aaa7a",
        },
        "directory": {
            "value_fg": "#c8a8e8",
            "focus_bg": "#3a2a4e",
            "focus_fg": "#ffffff",
            "option_fg": "#8a6aa8",
        },
        "mcp": {
            "value_fg": "#e8a88e",
            "focus_bg": "#4e3a2a",
            "focus_fg": "#ffffff",
            "option_fg": "#a87a5e",
        },
        "permissions": {
            "value_fg": "#e88e8e",
            "focus_bg": "#4e2a2a",
            "focus_fg": "#ffffff",
            "option_fg": "#a85e5e",
            "unavailable_fg": "#555555",
        },
    },
    "search": {
        "cursor_fg": "#ffffff",
        "match_fg": "#ffff00",
        "no_match_fg": "#ff4444",
    },
    "overflow": {
        "arrow_fg": "#666666",
        "minimap_fg": "#444444",
        "minimap_focused_bg": "#ffffff",
        "minimap_char": "▪",
    },
}

DEFAULT_THEME_LIGHT = {
    "name": "light",
    "global": {
        "bg": None,
        "fg": "#1a1a1a",
        "label_fg": "#666666",
        "separator_fg": "#cccccc",
        "separator_char": " | ",
        "empty_value_fg": "#aaaaaa",
        "empty_value_text": "---",
    },
    "segments": {
        "profile": {
            "value_fg": "#1a6b8a",
            "focus_bg": "#d0e8f0",
            "focus_fg": "#000000",
            "option_fg": "#4a8ba3",
        },
        "github": {
            "value_fg": "#2a7a2a",
            "focus_bg": "#d0f0d0",
            "focus_fg": "#000000",
            "option_fg": "#4a9a4a",
        },
        "version": {
            "value_fg": "#8a7a1a",
            "focus_bg": "#f0e8d0",
            "focus_fg": "#000000",
            "option_fg": "#a89a4a",
            "unavailable_fg": "#bbbbbb",
        },
        "model": {
            "value_fg": "#2a6a3a",
            "focus_bg": "#d0f0d8",
            "focus_fg": "#000000",
            "option_fg": "#4a8a5a",
        },
        "directory": {
            "value_fg": "#6a3a8a",
            "focus_bg": "#e8d0f0",
            "focus_fg": "#000000",
            "option_fg": "#8a5aa8",
        },
        "mcp": {
            "value_fg": "#8a5a2a",
            "focus_bg": "#f0e0d0",
            "focus_fg": "#000000",
            "option_fg": "#a87a4a",
        },
        "permissions": {
            "value_fg": "#8a2a2a",
            "focus_bg": "#f0d0d0",
            "focus_fg": "#000000",
            "option_fg": "#a85a5a",
            "unavailable_fg": "#bbbbbb",
        },
    },
    "search": {
        "cursor_fg": "#000000",
        "match_fg": "#0066cc",
        "no_match_fg": "#cc0000",
    },
    "overflow": {
        "arrow_fg": "#999999",
        "minimap_fg": "#cccccc",
        "minimap_focused_bg": "#000000",
        "minimap_char": "▪",
    },
}
