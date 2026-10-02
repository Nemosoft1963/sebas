from pathlib import Path
import os
import re
import shutil
import subprocess

import pytest

ROOT=Path(__file__).resolve().parents[1]

def test_nginx_proxy_is_default_deny_tokened_and_path_limited():
    text=(ROOT/'docker/proxy/nginx.conf.template').read_text()
    entry=(ROOT/'docker/proxy/entrypoint.sh').read_text()
    assert 'location / { return 403; }' in text
    assert 'if ($token_ok = 0) { return 401; }' in text
    assert text.count('if ($token_ok = 0) { return 401; }') == 4
    assert 'client_max_body_size 10m' in text and 'limit_req_zone' in text and 'server_tokens off' in text
    assert '$request_method $uri $status' in text
    assert '$http_x_sebas_token' not in re.search(r"log_format safe ([^;]+);",text).group(1)
    location_lines=[line.strip() for line in text.splitlines() if line.strip().startswith('location ')]
    assert set(location_lines)=={
      'location = /api/health {',
      'location ~ "^/api/projects/[A-Za-z0-9_-]{1,64}/experience/import-success-cases$" {',
      'location ~ "^/api/projects/[A-Za-z0-9_-]{1,64}/agent-examples$" {',
      'location ~ "^/api/projects/[A-Za-z0-9_-]{1,64}/agent-examples/[A-Za-z0-9_-]{1,64}/learning/general$" {',
      'location / { return 403; }',
    }
    assert 'limit_except GET {' in text and 'limit_except POST {' in text and 'limit_except GET POST {' in text
    assert 'proxy_pass http://local-voice-ai-web:8000' in text
    assert 'proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for' in text
    regex_locations = [line for line in location_lines if line.startswith('location ~')]
    assert regex_locations and all(re.match(r'^location ~ "[^"]+" \{$', line) for line in regex_locations)
    assert all('{' not in line.split('"', 1)[0] for line in regex_locations)
    assert 'if [ -z "${SEBAS_PROXY_TOKEN:-}" ]' in entry and 'exit 1' in entry
    rules = 'allow 127.0.0.1; allow ${SEBAS_ALLOWED_CLIENT}; deny all;'
    assert rules in entry and rules.index('allow 127.0.0.1') < rules.index('deny all')
    assert '"${#SEBAS_PROXY_TOKEN}" -lt 16' in entry
    assert '*[!A-Za-z0-9._~+=-]*' in entry


def test_nginx_syntax_when_binary_is_available(tmp_path):
    binary = shutil.which('nginx')
    if not binary:
        pytest.skip('nginx binary is not installed')
    template = (ROOT/'docker/proxy/nginx.conf.template').read_text()
    rendered = template.replace('${SEBAS_PROXY_TOKEN}', '0123456789abcdef')
    rendered = rendered.replace('${SEBAS_CLIENT_ACCESS_RULES}', 'allow 127.0.0.1; deny all;')
    rendered = rendered.replace('local-voice-ai-web:8000', '127.0.0.1:8000')
    config = tmp_path/'nginx.conf'; config.write_text(rendered)
    result = subprocess.run([binary, '-t', '-c', str(config), '-p', str(tmp_path)],
                            capture_output=True, text=True, env={**os.environ})
    assert result.returncode == 0, result.stderr


def test_proxy_compose_and_lan_override_are_separate_and_strict():
    proxy=(ROOT/'docker-compose.proxy.yml').read_text()
    overlay=(ROOT/'docker-compose.lan-off.yml').read_text()
    assert '${SEBAS_PROXY_TOKEN:?SEBAS_PROXY_TOKEN is required}' in proxy
    assert '${PROXY_BIND_IP:?PROXY_BIND_IP is required}:8099:8080' in proxy
    assert 'external: true' in proxy and 'localsaporter_local_cowork_net' in proxy
    assert 'restart: unless-stopped' in proxy and 'healthcheck:' in proxy
    assert '127.0.0.1:8099:8000' in overlay and '!override' in overlay
    assert (ROOT/'scripts/enable_lan_proxy.ps1').read_text().count('-f docker-compose') >= 3


def test_no_secret_literal_and_protected_compose_not_replaced():
    nginx=(ROOT/'docker/proxy/nginx.conf.template').read_text()
    assert '${SEBAS_PROXY_TOKEN}' in nginx
    assert 'allow 192.' not in nginx
    base=(ROOT/'docker-compose.yml').read_text(encoding='utf-8-sig')
    assert 'sebas-lan-proxy' not in base
    assert 'local-voice-ai-web' in base and '127.0.0.1:8099:8000' in base
