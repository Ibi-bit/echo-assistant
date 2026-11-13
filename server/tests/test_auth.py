from app.config import (
    is_local_host,
    is_local_request,
    is_secure_transport,
    redact_token,
)


def test_local_hosts():
    assert is_local_host("localhost")
    assert is_local_host("127.0.0.1")
    assert is_local_host("::1")
    assert is_local_host("[::1]")
    assert is_local_host("192.168.1.20")
    assert is_local_host("172.18.0.2")
    assert not is_local_host("203.0.113.5")
    assert not is_local_host("172.32.0.1")


def test_local_request_headers_must_come_from_local_peer():
    assert is_local_request("192.168.1.20")
    assert is_local_request("127.0.0.1", "192.168.1.20")
    assert is_local_request("172.18.0.2", "192.168.1.20")
    assert not is_local_request("203.0.113.9")
    assert not is_local_request("203.0.113.9", "192.168.1.20")
    assert not is_local_request("172.18.0.2", "203.0.113.20")


def test_public_traffic_needs_tls_at_a_local_proxy():
    assert is_secure_transport("127.0.0.1", "", "", "http")
    assert is_secure_transport("192.168.1.10", "", "", "http")
    assert is_secure_transport("127.0.0.1", "203.0.113.5", "https", "http")
    assert is_secure_transport("172.18.0.2", "8.8.8.8", "https", "ws")
    assert not is_secure_transport("127.0.0.1", "203.0.113.5", "http", "http")
    # Headers from a direct public client are not trusted.
    assert not is_secure_transport("203.0.113.5", "", "https", "http")
    assert not is_secure_transport("203.0.113.5", "", "", "ws")


def test_tokens_are_redacted_from_logs():
    assert redact_token("GET /ws?token=secret123&x=1 HTTP/1.1") == (
        "GET /ws?token=[redacted]&x=1 HTTP/1.1"
    )
    assert redact_token('WebSocket /ws?token=secret" 101') == (
        'WebSocket /ws?token=[redacted]" 101'
    )
    assert redact_token("no token here") == "no token here"
