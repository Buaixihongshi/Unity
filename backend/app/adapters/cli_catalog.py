"""Model lists for local CLIs.

Each known CLI lists its models either as fixed entries (Claude Code only
accepts aliases) or through a listing subcommand (`codex debug models`,
`agy models`). The rules live here rather than in `cli-providers.json` because
they describe the CLI binary itself, and managed environments export provider
config with a fixed field allowlist. Listing runs the local binary without
sending a prompt, so it costs no model usage; results are cached per process.
Models that list their own effort levels (Codex) carry them per entry, which
narrows the levels the CLI accepts in general. Antigravity's listed models
already fix their effort (`-low`/`-high` variants) or reject `--effort`, so they
carry an empty list; only its default or a hand-typed base model takes one.
"""
import json
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone

from backend.app.adapters.model_selection import EFFORT_LEVELS

CATALOG_TIMEOUT_SECONDS = 20
MAX_OUTPUT_BYTES = 2_000_000
MAX_MODELS = 500

_cache = {}
_lock = threading.Lock()


class CatalogError(Exception):
    pass


# Antigravity names a model's fixed effort as its suffix (`gemini-3.8-flash-high`);
# such a model rejects `--effort` even when no listing has been fetched yet.
_FIXED_EFFORT_SUFFIX = re.compile(r'-(?:minimal|low|medium|high|xhigh|max)$')

# provider id -> {"models": [(id, label)]} or {"args": [...], "format": parser name};
# "fixed_effort" marks CLIs whose listed models cannot take a separate effort.
CATALOG_SPECS = {
    'claude': {'models': [('fable', 'Fable（最新）'), ('opus', 'Opus（最新）'), ('sonnet', 'Sonnet（最新）')]},
    'codex': {'args': ['debug', 'models'], 'format': 'codex_json'},
    'antigravity': {'args': ['models'], 'format': 'tab_separated', 'fixed_effort': True},
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _valid_id(model_id):
    return (isinstance(model_id, str) and model_id.strip() and len(model_id) <= 200
            and not any(ord(c) < 32 for c in model_id))


def _entry(model_id, label):
    model_id = model_id.strip()
    label = label.strip() if isinstance(label, str) and label.strip() else model_id
    return dict(id=model_id, label=label[:200], vendor=None)


def _codex_efforts(item):
    levels = item.get('supported_reasoning_levels')
    if not isinstance(levels, list):
        return None
    efforts = [level.get('effort') for level in levels if isinstance(level, dict)]
    return [effort for effort in efforts if effort in EFFORT_LEVELS['codex']] or None


def parse_codex_json(output):
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        raise CatalogError('模型列表格式无法识别') from exc
    items = data.get('models') if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise CatalogError('模型列表格式无法识别')
    listed = [item for item in items if isinstance(item, dict) and _valid_id(item.get('slug'))
              and item.get('visibility', 'list') == 'list']
    listed.sort(key=lambda item: item.get('priority') if isinstance(item.get('priority'), (int, float)) else 1e9)
    models = []
    for item in listed:
        entry = _entry(item['slug'], item.get('display_name'))
        efforts = _codex_efforts(item)
        if efforts:
            entry['efforts'] = efforts
        models.append(entry)
    return models


def parse_tab_separated(output):
    models = []
    for line in output.splitlines():
        if '\t' not in line:
            continue  # progress lines such as "Fetching available models..."
        model_id, _, label = line.partition('\t')
        if _valid_id(model_id):
            models.append(_entry(model_id, label))
    return models


_PARSERS = {'codex_json': parse_codex_json, 'tab_separated': parse_tab_separated}


def _list_models(provider, spec):
    if 'models' in spec:
        return [_entry(model_id, label) for model_id, label in spec['models']]
    if not shutil.which(provider.command):
        raise CatalogError(f'未找到命令 {provider.command}，请先安装 {provider.label}')
    try:
        completed = subprocess.run([provider.command, *spec['args']], stdin=subprocess.DEVNULL,
                                   capture_output=True, check=False, timeout=CATALOG_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise CatalogError('获取模型列表超时') from exc
    except OSError as exc:
        raise CatalogError('无法运行命令') from exc
    if completed.returncode != 0:
        raise CatalogError(f'{provider.label} 未能列出模型（退出码 {completed.returncode}）')
    output = completed.stdout[:MAX_OUTPUT_BYTES].decode('utf-8', errors='replace')
    models = _PARSERS[spec['format']](output)
    if spec.get('fixed_effort'):
        for model in models:
            model['efforts'] = []
    return models


def cli_catalog(provider, *, scope='', refresh=False):
    """Return the cached list, listing once when nothing is cached or a refresh is requested."""
    spec = CATALOG_SPECS.get(provider.id)
    if spec is None:
        return dict(connection_id=provider.id, status='unsupported', models=[], fetched_at=None,
                    error='该 CLI 不提供模型列表，可在角色表中手动输入模型 ID')
    key = (scope, provider.id)
    with _lock:
        cached = _cache.get(key)
    if cached and not refresh:
        return dict(cached)
    try:
        models, seen = [], set()
        for model in _list_models(provider, spec):
            if model['id'] not in seen and len(models) < MAX_MODELS:
                seen.add(model['id'])
                models.append(model)
        if not models:
            raise CatalogError('没有发现可用模型')
        result = dict(connection_id=provider.id, status='ready', models=models, fetched_at=_now(), error=None)
    except CatalogError as exc:
        if cached:
            return dict(cached, status='stale', error=str(exc))
        return dict(connection_id=provider.id, status='error', models=[], fetched_at=None, error=str(exc))
    with _lock:
        _cache[key] = result
    return dict(result)


def has_fixed_effort(provider_id, model):
    """Whether the model name itself fixes its effort, which needs no listing to tell."""
    return provider_id == 'antigravity' and bool(_FIXED_EFFORT_SUFFIX.search(model))


def model_efforts(provider_id, model, models):
    """Effort levels a known model accepts, or None when its capability is unknown.

    `models` is the CLI's listing, or None when it has none or could not be
    fetched. Unknown covers hand-typed models, CLIs without a listing and failed
    listings; callers then fall back to the levels the CLI accepts in general.
    """
    if has_fixed_effort(provider_id, model):
        return []
    listed = next((m for m in models or () if m['id'] == model), None)
    return None if listed is None else listed.get('efforts')
