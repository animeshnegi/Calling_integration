"""Moves preserve public imports and behavior without bringing HTTP into models."""
import ast
import importlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_settings_and_routes_are_separate():
    from app.admin import SettingsStore as legacy
    from app.models.settings import SettingsStore
    from app.routes.admin import register_admin

    assert legacy is SettingsStore
    assert SettingsStore.__module__ == "app.models.settings"
    assert register_admin.__module__ == "app.routes.admin"
    tree = ast.parse((ROOT / "app/models/settings.py").read_text())
    assert not any(isinstance(n, ast.ImportFrom) and n.module == "flask" for n in ast.walk(tree))
    assert not any(isinstance(n, ast.FunctionDef) and n.name == "register_admin" for n in tree.body)


def test_legacy_ari_modules_alias_the_same_state():
    for old, canonical in [
        ("app.asterisk_client", "app.asterisk.client"),
        ("app.ari_worker", "app.asterisk.worker"),
    ]:
        assert importlib.import_module(old) is importlib.import_module(canonical)


def test_softphone_library_order_and_no_inline_script():
    html = (ROOT / "app/templates/phone.html").read_text()
    assert "<script>" not in html
    assert html.index('/jssip.min.js') < html.index('/static/js/jssip-fallback.js') < html.index('/phone.js?v=13')
