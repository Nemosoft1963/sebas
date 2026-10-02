from pathlib import Path


def test_healthcheck_has_fixed_lan_scope_and_rejects_wildcards():
    script = Path('scripts/healthcheck.ps1').read_text(encoding='utf-8-sig')
    assert 'SEBAS_ALLOWED_8099_BINDINGS' in script
    assert 'docker compose config --format json' in script
    assert '$composeBindings' in script
    assert '$wildcardAllowed' in script and '0.0.0.0' in script
    assert '$port -eq 8099 -and $wildcardAllowed' in script
    assert '$_.LocalAddress -notin $allowed' in script
