"""Literal source boundaries checked before source bytes are opened.

Symlinks are not supported as evidence sources. This is path validation, not a
filesystem sandbox against another process swapping directories concurrently.

It also holds the one strict reader of `<vault>/.context/routes.json` that
every entry point uses (index, search, route, MCP, hook, status, tasks, packets,
memory): a config that cannot be trusted is refused, never read as "no
exclusions", because that would silently widen what may be quoted.
"""
from functools import lru_cache
import json
from pathlib import Path, PurePosixPath
import unicodedata

BLOCKED_PARTS = {'.git', '.context', '.context-runs', '.obsidian', '.trash',
                 '__pycache__', 'node_modules', '.venv'}


class SymlinkSource(ValueError):
    """A source path, or one of its parent directories, is a symlink."""


def relative_name(value):
    if not isinstance(value, str) or not value or '\\' in value:
        raise ValueError('Source path must be a nonempty relative POSIX path')
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or '..' in path.parts or ':' in path.parts[0]:
        raise ValueError('Source path escapes the vault')
    return path.as_posix().rstrip('/')


def _fold(value):
    """Case- and normalisation-insensitive form used only for matching.

    A case-insensitive file system (the macOS and Windows defaults) serves
    `Private/x.md` for a rule written as `private`, and macOS may hand back a
    decomposed (NFD) name for a rule typed composed (NFC). A literal comparison
    would let such a file through, so exclusion matching errs toward excluding:
    on a case-sensitive file system `Private/` and `private/` are then both
    excluded by either spelling. Characters stay literal otherwise (`%`, `_`,
    quotes are not wildcards).
    """
    return unicodedata.normalize('NFC', unicodedata.normalize('NFC', value).casefold())


@lru_cache(maxsize=64)
def _folded_prefixes(prefixes):
    return tuple(_fold(relative_name(prefix)) for prefix in prefixes)


def _blocked_or_prefixed(name, folded_prefixes):
    """The exclusion rules for a name already split on '/': a tool or dot part, or a
    configured prefix (whole components, case- and normalisation-insensitive)."""
    if any(part in BLOCKED_PARTS or part.startswith('.') for part in name.split('/')):
        return True
    folded = _fold(name)
    return any(folded == prefix or folded.startswith(prefix + '/') for prefix in folded_prefixes)


def excluded(name, prefixes=()):
    name = relative_name(name)
    return _blocked_or_prefixed(name, _folded_prefixes(tuple(prefixes)))


def exclusion_matcher(prefixes=()):
    """excluded() for names read from the index, with the prefixes folded once.

    String rules only, no file access: a name the policy cannot parse counts as
    excluded, so it is never ranked or read. Reads still go through
    source_path(), which validates the name again before any byte is opened.
    """
    folded_prefixes = _folded_prefixes(tuple(prefixes))
    known = {}

    def matches(name):
        hit = known.get(name)
        if hit is None:
            try:
                hit = _blocked_or_prefixed(relative_name(name), folded_prefixes)
            except ValueError:
                hit = True
            known[name] = hit
        return hit
    return matches


def walked_name_state(name, prefixes=()):
    """'excluded', 'unsupported' or None for a vault-relative name found by a walk.

    Exclusion wins: a name inside a tool, dot or configured-excluded path is never
    reported, even when the policy could not accept it as a source name.
    'unsupported' is a name relative_name() refuses (a backslash, or ':' in the
    first component): it cannot be read as a source, so the builder skips it.
    """
    if _blocked_or_prefixed(name, _folded_prefixes(tuple(prefixes))):
        return 'excluded'
    try:
        relative_name(name)
    except ValueError:
        return 'unsupported'
    return None


def source_path(vault, name, prefixes=()):
    name = relative_name(name)
    if excluded(name, prefixes):
        raise ValueError(f'Excluded source: {name}')
    root = Path(vault).resolve()
    current = root
    for part in PurePosixPath(name).parts:
        current = current / part
        if current.is_symlink():
            raise SymlinkSource(f'Symlink source is not supported: {name}')
    if not current.resolve().is_relative_to(root):
        raise ValueError(f'Source escapes the vault: {name}')
    return current


# ---------------------------------------------------------------------------
# routes.json: one strict loader for every entry point
# ---------------------------------------------------------------------------

CONFIG_NAME = 'routes.json'
CONFIG_LABEL = '.context/routes.json'
# Format version of routes.json this code understands. `init` writes
# `schema_version: 1`; a config without the key is legacy and read as 1.
CONFIG_SCHEMA_VERSION = 1
EXCLUSION_KEYS = ('exclude_prefixes', 'retrieval_exclude_prefixes')
# The index builder skips (and reports) files larger than this many bytes.
DEFAULT_MAX_FILE_BYTES = 2_000_000
MAX_FILE_BYTES_CAP = 50_000_000


class ConfigError(ValueError):
    """routes.json cannot be trusted; the caller must refuse, not widen scope."""


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError(f'duplicate key {key!r}')
        result[key] = value
    return result


def parse_config(text, label=CONFIG_LABEL):
    """Parse routes.json text strictly. Raises ConfigError with a fix-it message.

    Rejected: invalid JSON, duplicate keys at any depth (JSON parsers disagree
    on which one wins), a non-object top level, a schema_version newer than this
    code, exclusion values that are not a list of relative path strings (a bare
    string would otherwise be read character by character), an exclusion entry
    with blanks around it or around one of its components, or with an empty
    component (each would exclude nothing while looking like a rule), and a
    max_file_bytes that is not a whole number from 1 to MAX_FILE_BYTES_CAP.
    """
    try:
        data = json.loads(text, object_pairs_hook=_unique_keys)
    except ConfigError as exc:
        raise ConfigError(f'{label} has a {exc}; remove the repeated key') from None
    except ValueError as exc:
        where = f' (line {exc.lineno}, column {exc.colno}: {exc.msg})' if hasattr(exc, 'lineno') else ''
        raise ConfigError(f'{label} is not valid JSON{where}; fix it or regenerate it '
                          'with `context-layer init <vault> --force`') from None
    if not isinstance(data, dict):
        raise ConfigError(f'{label} must be a JSON object containing a routes object')
    version = data.get('schema_version', CONFIG_SCHEMA_VERSION)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ConfigError(f'{label}: schema_version must be a positive integer')
    if version > CONFIG_SCHEMA_VERSION:
        raise ConfigError(f'{label} has schema_version {version}, but this context-layer '
                          f'reads only up to {CONFIG_SCHEMA_VERSION}; upgrade context-layer')
    for key in EXCLUSION_KEYS:
        if key not in data:
            continue
        value = data[key]
        if not isinstance(value, list):
            raise ConfigError(f'{label}: {key} must be a list of path prefixes, '
                              f'not {type(value).__name__}')
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ConfigError(f'{label}: every {key} entry must be a non-empty string')
            problem = exclusion_entry_problem(item)
            if problem:
                raise ConfigError(f'{label}: {key} entry {item!r} {problem}')
    if 'max_file_bytes' in data:
        size = data['max_file_bytes']
        if isinstance(size, bool) or not isinstance(size, int) \
                or not 1 <= size <= MAX_FILE_BYTES_CAP:
            raise ConfigError(f'{label}: max_file_bytes must be a whole number of bytes from 1 '
                              f'to {MAX_FILE_BYTES_CAP:,}, not {size!r} (the default is '
                              f'{DEFAULT_MAX_FILE_BYTES:,})')
    return data


def exclusion_entry_problem(item, fix=True):
    """Why an exclusion entry cannot be trusted, or None. The loader refuses such an
    entry, with a fix-it hint when `fix`; `init` omits a generated one and says why.

    `"private/ "` names a folder called "private " (with a blank), so it would
    silently leave `private/` searchable; `"a//b"` has an empty component.
    """
    if item != item.strip():
        return 'has blanks around it' + (
            f', so it would exclude nothing; write {item.strip()!r}' if fix else '')
    if item.startswith('/'):
        return 'is not a vault-relative POSIX path'
    parts = item[:-1].split('/') if item.endswith('/') else item.split('/')
    if any(not part.strip() for part in parts):
        return 'has an empty path component' + (
            '; write one name per component, as in "private/" or "archive/2024/"'
            if fix else '')
    for part in parts:
        if part != part.strip():
            fixed = '/'.join(p.strip() for p in parts) + ('/' if item.endswith('/') else '')
            return f'has blanks around the component {part!r}' + (
                f', so it would exclude nothing; write {fixed!r}' if fix else '')
    try:
        relative_name(item)
    except ValueError:
        return 'is not a vault-relative POSIX path'
    return None


def load_config(path, required=False, label=None):
    """routes.json at `path` as a dict. A missing file is `{}` unless required."""
    path = Path(path)
    label = label or (CONFIG_LABEL if path.name == CONFIG_NAME
                      and path.parent.name == '.context' else path.name)
    if not path.is_file():
        if path.exists():
            raise ConfigError(f'{label} is not a regular file')
        if required:
            raise ConfigError(f'{label} not found; run `context-layer init <vault>` first')
        return {}
    try:
        text = path.read_text(encoding='utf-8')
    except (OSError, UnicodeError) as exc:
        detail = getattr(exc, 'strerror', None) or type(exc).__name__
        raise ConfigError(f'{label} cannot be read ({detail})') from None
    return parse_config(text, label)


def config_max_file_bytes(config):
    """The index builder's per-file size limit from an already validated config."""
    return config.get('max_file_bytes', DEFAULT_MAX_FILE_BYTES)


def config_exclusions(config):
    """Exclusion prefixes of an already validated config, in declaration order."""
    prefixes = []
    for key in EXCLUSION_KEYS:
        prefixes.extend(config.get(key, []))
    return tuple(prefixes)


def config_path(vault):
    return Path(vault) / '.context' / CONFIG_NAME


def load_exclusions(vault):
    """Every exclusion prefix for a vault; ConfigError if routes.json is unusable."""
    return config_exclusions(load_config(config_path(vault)))
