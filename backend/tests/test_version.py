import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from youju import version
from youju.api import app as api


@pytest.mark.parametrize("value", [None, {}, [], {"revision": "short"}, {"revision": "x" * 40}])
def test_missing_or_invalid_build_never_invents_revision(tmp_path, monkeypatch, value):
    path = tmp_path / "_build.json"
    if value is not None:
        path.write_text(json.dumps(value))
    monkeypatch.setattr(version, "BUILD_FILE", path)
    assert version.build_revision() is None


def test_deployed_version_and_admin_boundary(tmp_path, monkeypatch):
    path = tmp_path / "_build.json"
    path.write_text(json.dumps({"revision": "a" * 40, "secret": "must-not-leak"}))
    monkeypatch.setattr(version, "BUILD_FILE", path)
    settings = SimpleNamespace(role="admin")
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    client = TestClient(api.app)
    assert client.get("/api/v1/version").status_code == 404
    response = client.get("/api/v1/version", headers={"X-WX-SOURCE": "cloud"})
    assert response.json() == {"revision": "a" * 40, "role": "admin"}
    assert response.headers["cache-control"] == "no-store"
    settings.role = "api"
    assert client.get("/api/v1/version").json() == {"revision": "a" * 40, "role": "api"}
    assert client.get("/api/admin/overview").status_code == 404
