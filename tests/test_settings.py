from __future__ import annotations

import json

import lsp_server


def test_update_settings_accepts_nested_client_settings() -> None:
    lsp_server._update_settings(
        {
            "formatting": {"enabled": False, "indentSize": 2},
            "diagnostics": {"enabled": False, "fullOnSave": True},
        }
    )

    assert lsp_server._formatting_enabled() is False
    assert lsp_server._diagnostics_enabled() is False
    assert lsp_server._full_diagnostics_on_save() is True

    lsp_server._update_settings(
        {
            "renpy-lsp": {
                "formatting": {"enabled": True},
                "diagnostics": {"enabled": True, "fullOnSave": False},
            }
        }
    )

    assert lsp_server._formatting_enabled() is True
    assert lsp_server._diagnostics_enabled() is True
    assert lsp_server._full_diagnostics_on_save() is False


def _save_style_state() -> tuple:
    return (
        dict(lsp_server._settings["formatting"]),
        dict(lsp_server._vscode_style),
        lsp_server._format_config_path,
    )


def _restore_style_state(state: tuple) -> None:
    formatting, vscode_style, config_path = state
    lsp_server._settings["formatting"].update(formatting)
    lsp_server._vscode_style.update(vscode_style)
    lsp_server._format_config_path = config_path


def test_update_settings_accepts_blank_lines_mode() -> None:
    state = _save_style_state()
    try:
        lsp_server._update_settings({"formatting": {"blankLines": "betweenSay"}})
        assert lsp_server._settings["formatting"]["blankLines"] == "betweenSay"

        # Invalid values fall back to the current mode.
        lsp_server._update_settings({"formatting": {"blankLines": "bogus"}})
        assert lsp_server._settings["formatting"]["blankLines"] == "betweenSay"
    finally:
        _restore_style_state(state)


def test_format_config_file_overrides_vscode_style(tmp_path, monkeypatch) -> None:
    state = _save_style_state()
    cfg = tmp_path / ".renpy-format.json"
    cfg.write_text(
        json.dumps({"indentSize": 2, "blankLines": "strip"}), encoding="utf-8"
    )
    monkeypatch.setattr(lsp_server, "_format_config_path", str(cfg))
    try:
        lsp_server._update_settings(
            {"formatting": {"indentSize": 4, "blankLines": "collapse"}}
        )
        assert lsp_server._settings["formatting"]["indentSize"] == 2
        assert lsp_server._settings["formatting"]["blankLines"] == "strip"
    finally:
        _restore_style_state(state)


def test_format_config_file_partial_override_keeps_vscode_fallback(
    tmp_path, monkeypatch
) -> None:
    state = _save_style_state()
    cfg = tmp_path / ".renpy-format.json"
    cfg.write_text(json.dumps({"blankLines": "betweenSay"}), encoding="utf-8")
    monkeypatch.setattr(lsp_server, "_format_config_path", str(cfg))
    try:
        lsp_server._update_settings(
            {"formatting": {"indentSize": 2, "blankLines": "collapse"}}
        )
        # Only blankLines is overridden; indentSize keeps the VS Code value.
        assert lsp_server._settings["formatting"]["indentSize"] == 2
        assert lsp_server._settings["formatting"]["blankLines"] == "betweenSay"
    finally:
        _restore_style_state(state)


def test_format_config_invalid_values_fall_back(tmp_path, monkeypatch) -> None:
    state = _save_style_state()
    cfg = tmp_path / ".renpy-format.json"
    cfg.write_text(
        json.dumps({"indentSize": "wide", "blankLines": "lots"}), encoding="utf-8"
    )
    monkeypatch.setattr(lsp_server, "_format_config_path", str(cfg))
    try:
        lsp_server._update_settings(
            {"formatting": {"indentSize": 4, "blankLines": "collapse"}}
        )
        assert lsp_server._settings["formatting"]["indentSize"] == 4
        assert lsp_server._settings["formatting"]["blankLines"] == "collapse"
    finally:
        _restore_style_state(state)


def test_format_config_removal_restores_vscode_style(tmp_path, monkeypatch) -> None:
    state = _save_style_state()
    cfg = tmp_path / ".renpy-format.json"
    cfg.write_text(json.dumps({"blankLines": "strip"}), encoding="utf-8")
    try:
        monkeypatch.setattr(lsp_server, "_format_config_path", str(cfg))
        lsp_server._update_settings({"formatting": {"blankLines": "collapse"}})
        assert lsp_server._settings["formatting"]["blankLines"] == "strip"

        # Config file "deleted" → VS Code settings become authoritative again.
        monkeypatch.setattr(lsp_server, "_format_config_path", None)
        lsp_server._apply_format_config()
        assert lsp_server._settings["formatting"]["blankLines"] == "collapse"
    finally:
        _restore_style_state(state)
