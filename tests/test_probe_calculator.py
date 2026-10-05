import socket

import pytest

import tools.probe_calculator as probe


def test_network_fence_refuses_remote_hosts_and_allows_loopback():
    undo = probe.block_network()
    try:
        for host in ("api.cohere.com", "mist-ai.fly.dev", "8.8.8.8"):
            with pytest.raises(OSError, match="blocked"):
                socket.create_connection((host, 443), timeout=1)
            with pytest.raises(OSError, match="blocked"):
                socket.getaddrinfo(host, 443)
        s = socket.socket()
        try:
            with pytest.raises(OSError, match="blocked"):
                s.connect(("1.1.1.1", 443))
            with pytest.raises(OSError, match="blocked"):
                s.connect_ex(("1.1.1.1", 443))
        finally:
            s.close()
        # Loopback lookups still work (nothing is listening; only the fence matters).
        assert socket.getaddrinfo("127.0.0.1", 80)
    finally:
        undo()
    assert socket.create_connection.__module__ == "socket"


@pytest.mark.parametrize("host, ok", [("127.0.0.1", True), ("::1", True), ("localhost", True),
                                      ("10.0.0.1", False), ("example.com", False)])
def test_is_loopback(host, ok):
    assert probe._is_loopback(host) is ok


def test_probe_leaves_a_running_calculator_alone(monkeypatch, capsys):
    monkeypatch.setattr(probe, "pids_named", lambda _names: {1234})
    monkeypatch.setattr(probe, "block_network",
                        lambda: pytest.fail("must exit before doing anything"))
    assert probe.main(["--yes"]) == 0
    assert "already running" in capsys.readouterr().out


def test_variant_aborts_without_input_when_calculator_is_not_in_front(monkeypatch):
    ran = []
    api = type("A", (), {})()
    api._foreground_process_matches = lambda _app: False
    api._exec_sync = lambda *a: ran.append(a)
    monkeypatch.setattr(probe, "foreground_title", lambda: "main.py - Visual Studio Code")
    reports, aborted = probe.run_variant(api, [("type_text", "37*23")])
    assert ran == [] and reports == []
    assert "not in front" in aborted and "no input sent" in aborted
