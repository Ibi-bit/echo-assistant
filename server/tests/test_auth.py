from app.config import is_local_host, is_local_request


def test_local_hosts():
    assert is_local_host("localhost")
    assert is_local_host("127.0.0.1")
    assert is_local_host("::1")
    assert is_local_host("192.168.1.20")
    assert is_local_host("172.18.0.2")
    assert not is_local_host("203.0.113.5")
    assert not is_local_host("172.32.0.1")


def test_forwarded_for_only_trusted_from_local_peer():
    assert is_local_request("192.168.1.20")
    assert is_local_request("127.0.0.1", "192.168.1.20")
    assert is_local_request("172.18.0.2", "192.168.1.20")
    assert not is_local_request("203.0.113.9")
    assert not is_local_request("203.0.113.9", "192.168.1.20")
    assert not is_local_request("172.18.0.2", "203.0.113.20")
