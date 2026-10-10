"""The Home Assistant add-on repository layout Supervisor expects."""

from pathlib import Path

from openscan import __version__

ROOT = Path(__file__).resolve().parent.parent
ADDON = ROOT / "homeassistant-addon" / "openscanhub"


def _keys(path: Path) -> dict:
    """Top-level `key: value` pairs of a simple YAML file (no PyYAML needed)."""
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line[0].isspace() and not line.startswith("#") and ":" in line:
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip().strip('"')
    return out


def test_repository_yaml_is_at_the_repository_root():
    # Supervisor only reads repository.yaml from the git root.
    repo = ROOT / "repository.yaml"
    assert repo.is_file()
    assert not (ROOT / "homeassistant-addon" / "repository.yaml").exists()
    keys = _keys(repo)
    assert keys["name"] and keys["url"].startswith("https://github.com/")


def test_addon_config_is_found_and_matches_the_version():
    keys = _keys(ADDON / "config.yaml")
    for key in ("name", "version", "slug", "arch", "ingress", "options", "schema"):
        assert key in keys
    assert keys["version"] == __version__
    assert f"OPENSCAN_VERSION={__version__}" in (ADDON / "Dockerfile").read_text()


def test_addon_does_not_map_the_home_assistant_config_folder():
    # Settings live in /data; mapping HA's config folder would expose
    # secrets.yaml to the add-on for nothing.
    text = (ADDON / "config.yaml").read_text()
    assert "config:rw" not in text and "config:ro" not in text
    assert "/data/" in (ADDON / "run.sh").read_text()
