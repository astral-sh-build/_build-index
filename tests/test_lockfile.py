import tomllib
from pathlib import Path


LOCKFILE = Path("uv.lock")
PUBLIC_PYPI = "https://pypi.org/simple"


def test_lockfile_uses_public_pypi() -> None:
    lock = tomllib.loads(LOCKFILE.read_text(encoding="utf-8"))

    registries = {
        package["source"]["registry"]
        for package in lock["package"]
        if "registry" in package["source"]
    }

    assert registries == {PUBLIC_PYPI}
