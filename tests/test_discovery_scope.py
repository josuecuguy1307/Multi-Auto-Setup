"""D5: the scope guard. It must refuse everything outside this machine's own private subnets, whatever the caller asks."""

from __future__ import annotations

import ipaddress
import socket

import pytest

from envrec.discovery.scope import RateLimiter, ScopeCode, ScopeError, allow_multicast, scope_for_interface, validate_target

OWN = lambda: [("en0", ipaddress.IPv4Interface("192.0.2.30/26")), ("lo0", ipaddress.IPv4Interface("10.9.0.5/16"))]  # noqa: E731


def refused(spec, **kw):
    with pytest.raises(ScopeError) as e:
        validate_target(spec, own=kw.pop("own", OWN), **kw)
    return e.value.code


def test_a_public_address_is_refused():
    assert refused("8.8.8.8") == ScopeCode.public_address
    assert refused("1.1.1.1") == ScopeCode.public_address


def test_loopback_and_unspecified_are_refused():
    assert refused("127.0.0.1") == ScopeCode.not_private and refused("0.0.0.0") == ScopeCode.not_private


def test_a_private_address_on_a_different_subnet_is_refused():
    assert refused("192.168.77.9") == ScopeCode.wrong_subnet
    assert refused("172.16.0.1") == ScopeCode.wrong_subnet
    assert refused("192.0.2.200") == ScopeCode.wrong_subnet  # same /24 text, but this interface is only a /26


def test_an_address_inside_an_own_subnet_is_allowed():
    assert [str(a) for a in validate_target("192.0.2.31", own=OWN)] == ["192.0.2.31"]
    assert len(validate_target("192.0.2.0/26", own=OWN)) == 62


def test_an_oversized_range_is_refused_against_the_cap():
    assert refused("10.9.0.0/16") == ScopeCode.too_wide
    assert refused("10.9.0.0/23") == ScopeCode.too_wide
    assert len(validate_target("10.9.0.0/22", own=OWN, cap_prefix=22)) == 1022  # the widest allowed cap works when asked for explicitly
    assert refused("10.9.0.0/21", cap_prefix=22) == ScopeCode.too_wide


def test_a_cap_outside_the_allowed_band_is_refused():
    for bad in (16, 21, 31):
        with pytest.raises(ScopeError) as e:
            scope_for_interface(cap_prefix=bad, own=OWN)
        assert e.value.code == ScopeCode.bad_cap


def test_a_hostname_that_resolves_to_a_public_address_is_refused():
    def resolver(host, port, family=0):
        return [(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))]
    assert refused("printer.example.com", resolver=resolver) == ScopeCode.hostname_public


def test_a_hostname_with_one_public_and_one_private_address_is_refused():
    def resolver(host, port, family=0):
        return [(socket.AF_INET, 0, 0, "", ("192.0.2.40", 0)), (socket.AF_INET, 0, 0, "", ("8.8.4.4", 0))]
    assert refused("mixed.local", resolver=resolver) == ScopeCode.hostname_public


def test_a_hostname_that_resolves_inside_the_scope_is_allowed_and_one_outside_is_not():
    ok = lambda h, p, family=0: [(socket.AF_INET, 0, 0, "", ("192.0.2.40", 0))]  # noqa: E731
    assert [str(a) for a in validate_target("nas.local", own=OWN, resolver=ok)] == ["192.0.2.40"]
    far = lambda h, p, family=0: [(socket.AF_INET, 0, 0, "", ("192.168.50.2", 0))]  # noqa: E731
    assert refused("far.local", resolver=far) == ScopeCode.wrong_subnet


def test_unresolvable_names_are_refused_with_their_own_code():
    def boom(*a, **k):
        raise socket.gaierror("no such host")
    assert refused("nowhere.invalid", resolver=boom) == ScopeCode.unresolvable


def test_a_machine_without_a_lan_interface_cannot_scan():
    with pytest.raises(ScopeError) as e:
        validate_target("192.0.2.1", own=lambda: [])
    assert e.value.code == ScopeCode.no_interface


def test_multicast_is_allowed_only_for_ssdp_and_mdns():
    allow_multicast("239.255.255.250", 1900)
    allow_multicast("224.0.0.251", 5353)
    for bad in (("224.0.0.1", 1900), ("239.255.255.250", 53), ("239.1.2.3", 5353)):
        with pytest.raises(ScopeError):
            allow_multicast(*bad)
    assert refused("239.255.255.250") == ScopeCode.multicast_not_allowed


def test_the_default_scope_is_the_own_subnet_narrowed_to_the_cap():
    s = scope_for_interface(cap_prefix=24, own=lambda: [("en0", ipaddress.IPv4Interface("10.9.4.5/16"))])
    assert str(s.network) == "10.9.4.0/24" and s.own_address == ipaddress.IPv4Address("10.9.4.5") and s.host_count == 253
    small = scope_for_interface(own=OWN, interface="en0")
    assert str(small.network) == "192.0.2.0/26" and small.host_count == 61


def test_link_local_is_in_scope_only_when_it_is_an_own_subnet():
    own = lambda: [("en5", ipaddress.IPv4Interface("169.254.10.2/16"))]  # noqa: E731
    assert validate_target("169.254.10.20", own=own, cap_prefix=24)
    assert refused("169.254.99.1", own=own) == ScopeCode.wrong_subnet


def test_the_rate_limiter_spaces_events():
    t = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        t[0] += s
    rl = RateLimiter(10, clock=lambda: t[0], sleep=sleep)
    for _ in range(5):
        rl.wait()
    assert abs(sum(slept) - 0.4) < 1e-9  # five events at 10 per second: the first is free, four wait 0.1 s each
