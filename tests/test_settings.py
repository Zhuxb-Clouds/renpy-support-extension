from __future__ import annotations

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
