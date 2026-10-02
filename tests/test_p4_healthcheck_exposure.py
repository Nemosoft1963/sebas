from pathlib import Path


def test_healthcheck_has_fixed_lan_scope_and_rejects_wildcards():
    script = Path('scripts/healthcheck.ps1').read_text(encoding='utf-8-sig')
    assert 'docker-compose.lan-off.yml' in script and 'docker-compose.proxy.yml' in script
    assert 'Compose web loopback only' in script
    assert 'Compose proxy LAN only' in script
    assert 'Runtime binding $Name' in script
    assert 'Proxy healthy' in script
    assert '$wildcardAllowed' in script and '0.0.0.0' in script
    assert '$port -eq 8099 -and $wildcardAllowed' in script
    assert '$_.LocalAddress -notin $allowed' in script
