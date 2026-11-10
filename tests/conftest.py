"""Suite-wide isolation: no test reads or writes the real HOME."""

import pytest

from lime import config, worktree


@pytest.fixture(autouse=True)
def _isolated_home(request, tmp_path_factory, monkeypatch):
    """Every test gets an empty HOME of its own, and the paths lime derived from HOME at
    import time follow it. `real` tests keep the operator's HOME: they need its logins."""
    if request.node.get_closest_marker("real"):
        return
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)  # git would read its config there
    monkeypatch.delenv("LIME_PROJECTS", raising=False)
    monkeypatch.setattr(config, "PI_AGENT_DIR", home / ".pi" / "agent")
    monkeypatch.setattr(config, "PI_SETTINGS", home / ".pi" / "agent" / "settings.json")
    monkeypatch.setattr(worktree, "WORKTREES", home / ".lime" / "worktrees")
    monkeypatch.setattr(worktree, "CC_PROJECTS", home / ".claude" / "projects")
