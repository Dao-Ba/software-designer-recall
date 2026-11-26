"""Read environment-specific settings from config/ on every request."""
import json
from pathlib import Path
from urllib.parse import urlparse

CONFIG_DIR = Path(__file__).resolve().parents[1] / 'config'
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(name):
    path = CONFIG_DIR / (name + '.json')
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        raise ValueError(f'配置文件 {path} 无法读取：{exc}') from exc
    if not isinstance(data, dict):
        raise ValueError(f'配置文件 {path} 必须是 JSON 对象')
    local_path = CONFIG_DIR / (name + '.local.json')
    if local_path.exists():
        try:
            overrides = json.loads(local_path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError) as exc:
            raise ValueError(f'配置文件 {local_path} 无法读取：{exc}') from exc
        if not isinstance(overrides, dict):
            raise ValueError(f'配置文件 {local_path} 必须是 JSON 对象')
        data.update(overrides)
    return data


def _local_url(value, label):
    parsed = urlparse(value if isinstance(value, str) else '')
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1') or parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
        raise ValueError(f'{label} 必须是本机 HTTP 地址，例如 http://127.0.0.1:9200')
    try:
        if not parsed.port:
            raise ValueError()
    except ValueError as exc:
        raise ValueError(f'{label} 必须包含有效端口') from exc
    return value.rstrip('/')


def model_config():
    data = _read('model')
    base = data.get('ollama_base_url')
    parsed = urlparse(base if isinstance(base, str) else '')
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
        raise ValueError('ollama_base_url 必须是 HTTP 或 HTTPS 服务地址，不含路径')
    default = data.get('default_model', '')
    if not isinstance(default, str):
        raise ValueError('default_model 必须是字符串')
    return {'ollama_base_url': base.rstrip('/'), 'default_model': default.strip()}


def server_config():
    data = _read('server')
    host = data.get('host')
    port = data.get('port')
    if host not in ('127.0.0.1', 'localhost', '::1'):
        raise ValueError('server.json 的 host 只允许本机地址')
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('server.json 的 port 必须在 1 到 65535 之间')
    return {'host': host, 'port': port}


def exam_config():
    """Resolve the active question bank without moving an existing database."""
    data = _read('exam')
    exam_id = data.get('id')
    title = data.get('title')
    if not isinstance(exam_id, str) or not exam_id or len(exam_id) > 60 or not all(
            char.isascii() and (char.isalnum() or char in '_-') for char in exam_id):
        raise ValueError('exam.json 的 id 只能使用 1–60 个英文字母、数字、下划线或连字符')
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 100:
        raise ValueError('exam.json 的 title 必须是 1–100 个字的名称')

    def project_path(key, suffix=None):
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'exam.json 的 {key} 必须是项目内的相对路径')
        relative = Path(value.strip())
        if relative.is_absolute() or '..' in relative.parts or ':' in value:
            raise ValueError(f'exam.json 的 {key} 必须是项目内的相对路径')
        result = (PROJECT_ROOT / relative).resolve()
        if not result.is_relative_to(PROJECT_ROOT.resolve()) or result == PROJECT_ROOT.resolve():
            raise ValueError(f'exam.json 的 {key} 必须位于项目内')
        if suffix and result.suffix.lower() != suffix:
            raise ValueError(f'exam.json 的 {key} 必须以 {suffix} 结尾')
        return result

    notes_dir = project_path('notes_dir')
    database = project_path('database', '.sqlite3')
    if not notes_dir.is_relative_to((PROJECT_ROOT / 'notes').resolve()):
        raise ValueError('exam.json 的 notes_dir 必须位于 notes 目录内')
    if not database.is_relative_to((PROJECT_ROOT / 'data').resolve()):
        raise ValueError('exam.json 的 database 必须位于 data 目录内')
    if notes_dir == database or notes_dir.is_relative_to(database.parent) or database.is_relative_to(notes_dir):
        raise ValueError('exam.json 的笔记目录和数据库路径不能重叠')
    return {'id': exam_id, 'title': title.strip(), 'notes_dir': notes_dir, 'database': database}


def cpolar_config():
    data = _read('cpolar')
    data['api_base_url'] = _local_url(data.get('api_base_url'), 'api_base_url')
    for key in ('login_path', 'tunnels_path'):
        value = data.get(key)
        if not isinstance(value, str) or not value.startswith('/') or value.startswith('//'):
            raise ValueError(f'cpolar.json 的 {key} 必须是以 / 开始的路径')
    for key in ('username', 'password', 'tunnel_name'):
        if not isinstance(data.get(key), str):
            raise ValueError(f'cpolar.json 的 {key} 必须是字符串')
    return data
