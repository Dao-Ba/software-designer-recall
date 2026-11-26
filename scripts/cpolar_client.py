"""Read the HTTPS URL of a named tunnel from a local Cpolar dashboard."""
import json
import urllib.error
import urllib.request

from app_config import cpolar_config


def _request(url, payload=None, token=''):
    headers = {'Accept': 'application/json'}
    if payload is not None:
        headers['Content-Type'] = 'application/json'
    if token:
        headers['Authorization'] = 'Bearer ' + token
    request = urllib.request.Request(url, data=None if payload is None else json.dumps(payload).encode('utf-8'), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise RuntimeError('Cpolar 接口需要登录，请在 config/cpolar.json 填写 username 和 password') from exc
        raise RuntimeError(f'Cpolar 接口返回 HTTP {exc.code}，请检查接口地址和路径') from exc
    except (urllib.error.URLError, ValueError) as exc:
        raise RuntimeError(f'无法读取 Cpolar 本地接口：{exc}') from exc


def tunnel_status():
    config = cpolar_config()
    base = config['api_base_url']
    token = ''
    if config['username'] or config['password']:
        login = _request(base + config['login_path'], {'email': config['username'], 'password': config['password']})
        token = login.get('data', {}).get('token', '')
        if not token:
            raise RuntimeError('Cpolar 登录失败，请检查 config/cpolar.json 中的账号和密码')
    result = _request(base + config['tunnels_path'], token=token)
    body = result.get('data', result)
    tunnels = body.get('items', body.get('tunnels', [])) if isinstance(body, dict) else []
    if not isinstance(tunnels, list):
        raise RuntimeError('Cpolar 隧道列表格式无法识别，请检查接口路径')
    name = config['tunnel_name']
    matched = [item for item in tunnels if isinstance(item, dict) and (not name or item.get('name') == name)]
    urls = []
    for item in matched:
        for key in ('public_url', 'publicUrl'):
            value = item.get(key)
            if isinstance(value, str):
                urls.append(value)
            elif isinstance(value, list):
                urls.extend(x for x in value if isinstance(x, str))
    https_urls = [url for url in urls if url.startswith('https://')]
    return {'tunnel_name': name, 'public_url': https_urls[0] if https_urls else '',
            'found': bool(matched), 'online_count': len(tunnels)}
