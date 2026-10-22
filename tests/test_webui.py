import json
import socket
import threading
import urllib.error
import urllib.request

import _socket
import pytest

from webui import server
from webui.jobs import Job, JobManager


@pytest.fixture
def base(monkeypatch):
    # The suite refuses sockets; these tests talk only to a server on this machine.
    monkeypatch.setattr(socket.socket, "connect", _socket.socket.connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _socket.socket.connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", _socket.getaddrinfo)
    srv = server.Server(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _get(url, headers=None):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {})) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_page_and_meta_are_served(base):
    assert _get(base + "/")[0] == 200
    status, body = _get(base + "/api/meta")
    assert status == 200 and json.loads(body)["providers"]


def test_ticker_validation_names_the_asset_type(base):
    data = json.loads(_get(base + "/api/validate?ticker=btcusd")[1])
    assert data["ticker"] == "BTC-USD" and "fundamentals" not in data["analysts"]
    assert json.loads(_get(base + "/api/validate?ticker=//")[1])["ok"] is False


def test_foreign_host_and_traversal_are_refused(base):
    assert _get(base + "/api/meta", {"Host": "evil.example"})[0] == 403
    assert _get(base + "/../cli/main.py")[0] == 404


def test_post_needs_json(base):
    req = urllib.request.Request(base + "/api/runs", data=b"{}", method="POST", headers={"Content-Type": "text/plain"})
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req)
    assert err.value.code == 415


def test_one_job_at_a_time():
    manager, gate = JobManager(), threading.Event()
    first = manager.start(Job("analysis", "a"), lambda j: gate.wait(5))
    with pytest.raises(RuntimeError):
        manager.start(Job("analysis", "b"), lambda j: None)
    gate.set()
    first.wait_for(0, 2)
