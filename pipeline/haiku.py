from __future__ import annotations
import contextlib
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from . import extract as _extract
from . import host as _host
from . import spawn_guard
from .types import HaikuResult, TokenUsage
HAIKU_INPUT_PRICE = 0.8 / 1000000
HAIKU_OUTPUT_PRICE = 4.0 / 1000000
HAIKU_CACHE_PRICE = 0.08 / 1000000
DEFAULT_MAX_TURNS = '4'
MAX_ALLOWED_TURNS = 20

def _resolve_max_turns() -> str:
    raw = os.environ.get('REMEMBER_MAX_TURNS', '').strip()
    if raw.isdigit() and 1 <= int(raw) <= MAX_ALLOWED_TURNS:
        return str(int(raw))
    return DEFAULT_MAX_TURNS
DEFAULT_MODEL = 'haiku'

def _resolve_model() -> str:
    raw = os.environ.get('REMEMBER_MODEL', '').strip()
    return raw if raw else DEFAULT_MODEL

def _resolve_claude_bin() -> str:
    override = os.environ.get('REMEMBER_CLAUDE_BIN', '').strip()
    if override:
        return override
    return shutil.which('claude') or 'claude'

def _resolve_codex_bin() -> str:
    override = os.environ.get('REMEMBER_CODEX_BIN', '').strip()
    if override:
        return override
    return shutil.which('codex') or 'codex'
_CHILD_ENV_OAUTH_NAME = 'CLAUDE_CODE_OAUTH_TOKEN'
NESTED_SUMMARIZER_ENV = 'REMEMBER_NESTED_SUMMARIZER'
_SUMMARIZER_PROVIDERS = frozenset({'claude', 'codex', 'auto'})

def _resolve_summarizer_provider() -> str:
    raw = os.environ.get('REMEMBER_SUMMARIZER', '').strip().lower()
    if not raw:
        return 'auto'
    if raw in _SUMMARIZER_PROVIDERS:
        return raw
    _warn(f"WARNING: ignoring REMEMBER_SUMMARIZER={raw!r} -- must be one of {sorted(_SUMMARIZER_PROVIDERS)}; using 'auto'")
    return 'auto'

def _resolve_summarizer_fallback() -> str | None:
    raw = os.environ.get('REMEMBER_SUMMARIZER_FALLBACK', '').strip().lower()
    if not raw:
        return None
    if raw == 'claude':
        return raw
    _warn(f"WARNING: ignoring REMEMBER_SUMMARIZER_FALLBACK={raw!r} -- 'claude' is the only supported fallback target; not falling back")
    return None

def _choose_summarizer_provider() -> str:
    provider = _resolve_summarizer_provider()
    if provider != 'auto':
        return provider
    path = _host.transcript_path()
    if not path:
        raw = (os.environ.get('REMEMBER_TRANSCRIPT_PATH') or '').strip()
        if raw:
            _warn(f"WARNING: REMEMBER_TRANSCRIPT_PATH={raw!r} names a transcript that no longer exists (exported, then vanished before this read) -- REMEMBER_SUMMARIZER=auto is falling back to 'claude', which may not be correct")
        return 'claude'
    envelope, envelope_unreadable, envelope_capped = _extract.sniff_file_envelope_status(path)
    if envelope == 'codex':
        return 'codex'
    if envelope == 'antigravity':
        _warn(f"WARNING: transcript {path!r} is an Antigravity session -- REMEMBER_SUMMARIZER=auto has no Antigravity-native summarizer and is falling back to 'claude'")
        return 'claude'
    if envelope == 'unrecognised':
        if envelope_unreadable:
            reason = 'unreadable'
        elif envelope_capped:
            reason = 'gave up after scanning too many unplaceable lines'
        else:
            reason = 'an unrecognised shape'
        _warn(f"WARNING: could not identify the host from transcript {path!r} ({reason}) -- REMEMBER_SUMMARIZER=auto is falling back to 'claude', which may not be correct")
    return 'claude'

def _child_env() -> dict[str, str]:
    drop_globs = _configured_drop_env()
    child = {k: v for k, v in os.environ.items() if (k == 'CLAUDE_CODE_OAUTH_TOKEN' or (k != 'CLAUDECODE' and k != 'CLAUDE_JOB_DIR' and (k != 'CLAUDE_PROJECT_DIR') and (not k.startswith('CLAUDE_CODE_')))) and (not any((fnmatch.fnmatchcase(k, g) for g in drop_globs)))}
    child['REMEMBER_NESTED_SUMMARIZER'] = '1'
    return child

def _usage_from_failure(stdout: object) -> TokenUsage | None:
    if isinstance(stdout, bytes):
        try:
            stdout = stdout.decode('utf-8', errors='replace')
        except Exception:
            return None
    if not isinstance(stdout, str) or not stdout.strip():
        return None
    try:
        payload = json.loads(stdout)
    except ValueError:
        return None
    if isinstance(payload, list):
        payload = payload[-1] if payload else {}
    if not isinstance(payload, dict):
        return None
    usage = _extract_tokens(payload)
    if usage.input or usage.output or usage.cache:
        return usage
    return None

def _log_failed_spend(what_happened: str, stdout: object) -> None:
    usage = _usage_from_failure(stdout)
    if usage is not None:
        _warn(f'call {what_happened} after spending tokens: {usage}')
    else:
        _warn(f"call {what_happened}; tokens already spent are unknown -- the failure carried no usage block, so this run's reported cost is lower than what it actually cost")
_FAILURE_DETAIL_MAX = 500

def _failure_detail(stdout: str, stderr: str) -> str:
    detail = ''
    stdout = (stdout or '').strip()
    stderr = (stderr or '').strip()
    if stdout:
        try:
            payload = json.loads(stdout)
        except ValueError:
            detail = stdout
        else:
            if isinstance(payload, dict):
                for key in ('error', 'result', 'message'):
                    value = payload.get(key)
                    if isinstance(value, dict):
                        value = value.get('message')
                    if isinstance(value, str) and value.strip():
                        detail = value.strip()
                        break
            detail = detail or stdout
    parts = [p for p in (detail, stderr) if p]
    if not parts:
        return '(no output on stdout or stderr)'
    joined = ' | '.join(parts)
    if len(joined) > _FAILURE_DETAIL_MAX:
        joined = joined[:_FAILURE_DETAIL_MAX] + '...'
    return joined

def _warn(message: str) -> None:
    try:
        remember_dir = os.environ.get('REMEMBER_DIR', '').strip()
        if remember_dir:
            from .log import log
            log('haiku', message, os.path.join(remember_dir, 'logs'))
        else:
            print(f'[haiku] {message}', file=sys.stderr)
    except Exception:
        pass

def _remember_dir_is_project_local(remember_dir: str) -> bool:
    project_dir = os.environ.get('MEMORY_PROJECT_DIR', '').strip()
    if not project_dir:
        return False
    try:
        remember_abs = os.path.realpath(remember_dir)
        project_abs = os.path.realpath(project_dir)
    except OSError:
        return True
    return remember_abs == project_abs or remember_abs.startswith(project_abs + os.sep)

def _config_candidates() -> list[str]:
    candidates = []
    merged = os.environ.get('REMEMBER_CONFIG', '').strip()
    if merged:
        candidates.append(merged)
    remember_dir = os.environ.get('REMEMBER_DIR', '').strip()
    if remember_dir and (not _remember_dir_is_project_local(remember_dir)):
        candidates.append(os.path.join(remember_dir, 'config.json'))
    candidates.append(os.path.join(os.path.expanduser('~'), '.remember', 'config.json'))
    return candidates
_DROP_ENV_ENTRY = re.compile('[A-Za-z_*?][A-Za-z0-9_*?]*')

def _configured_drop_env() -> tuple[str, ...]:
    for path in _config_candidates():
        try:
            with open(path, encoding='utf-8') as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(cfg, dict):
            continue
        haiku_cfg = cfg.get('haiku')
        if not isinstance(haiku_cfg, dict) or 'drop_env' not in haiku_cfg:
            continue
        value = haiku_cfg['drop_env']
        if not isinstance(value, list):
            _warn(f"WARNING: ignoring haiku.drop_env in {path} -- a {type(value).__name__} value, not a list of variable names or globs; nothing is dropped from the summarizer's environment")
            return ()
        globs = []
        for index, entry in enumerate(value):
            if isinstance(entry, str) and _DROP_ENV_ENTRY.fullmatch(entry):
                globs.append(entry.upper() if os.name == 'nt' else entry)
                continue
            if isinstance(entry, str):
                shape = f'a {len(entry)}-character string'
            else:
                shape = f'a {type(entry).__name__} value'
            _warn(f'WARNING: ignoring entry {index} of haiku.drop_env in {path} -- {shape}, not a variable name or a */? glob over letters, digits and underscores. The entry itself is not logged: it may hold a pasted value')
        return tuple(globs)
    return ()
_CREDENTIAL_FAILURE_MARKERS = ('credit balance', 'takes precedence', 'authentication', 'unauthorized', 'invalid api key', 'invalid x-api-key', 'rate limit')

def _drop_env_hint(detail: str) -> str:
    lowered = detail.lower()
    if not any((marker in lowered for marker in _CREDENTIAL_FAILURE_MARKERS)):
        return ''
    return ' -- the nested CLI inherits this environment as-is, and a credential variable set there for some other tool can out-rank your own login; list its name (or a glob such as PREFIX_*) in `haiku.drop_env` in config.json to keep it out of the summarizer (#703, #898)'
_HOOK_ISOLATION_FLAG = '--setting-sources'
_AUTH_FAILURE_MARKERS = ('not logged in', 'please run /login', 'invalid api key', 'invalid bearer token', 'authentication_error', 'failed to authenticate')
_SCANNED_FAILURE_FIELDS = ('error', 'result', 'message')
_SCANNED_FAILURE_LIST_FIELDS = ('errors',)

def _failure_haystack(stdout: str, stderr: str) -> str:
    stdout = stdout or ''
    stderr = stderr or ''
    try:
        payload = json.loads(stdout)
    except ValueError:
        return f'{stdout}\n{stderr}'.lower()
    if isinstance(payload, list):
        payload = payload[-1] if payload else None
    authored: list[str] = []
    if isinstance(payload, dict):
        for key in _SCANNED_FAILURE_FIELDS:
            value = payload.get(key)
            if isinstance(value, dict):
                value = value.get('message')
            if isinstance(value, str) and value.strip():
                authored.append(value)
        for key in _SCANNED_FAILURE_LIST_FIELDS:
            values = payload.get(key)
            if isinstance(values, list):
                authored.extend((e for e in values if isinstance(e, str)))
    if not authored:
        return f'{stdout}\n{stderr}'.lower()
    return '\n'.join(authored + [stderr]).lower()
_DECIDING_TOKENS = _AUTH_FAILURE_MARKERS + ('unknown option',)

def _marker_missed_by_the_scan(stdout: str, haystack: str) -> tuple[str, str] | None:
    try:
        payload = json.loads(stdout or '')
    except ValueError:
        return None
    if isinstance(payload, list):
        payload = payload[-1] if payload else None
    if not isinstance(payload, dict):
        return None
    skip = set(_SCANNED_FAILURE_FIELDS) | set(_SCANNED_FAILURE_LIST_FIELDS)
    for field, value in payload.items():
        if field in skip:
            continue
        try:
            text = json.dumps(value, default=str).lower()
        except (TypeError, ValueError):
            text = str(value).lower()
        for token in _DECIDING_TOKENS:
            if token in text and token not in haystack:
                return (token, field)
    return None

def _isolation_may_be_the_cause(stdout: str, stderr: str) -> bool:
    haystack = _failure_haystack(stdout, stderr)
    if 'unknown option' in haystack:
        return _HOOK_ISOLATION_FLAG in haystack
    if any((marker in haystack for marker in _AUTH_FAILURE_MARKERS)):
        return True
    missed = _marker_missed_by_the_scan(stdout, haystack)
    if missed:
        token, field = missed
        _warn(f"WARNING: this failure carries {token!r} in the terminal record's {field!r} field, which the marker scan does not read. It reads {', '.join((repr(f) for f in _SCANNED_FAILURE_FIELDS))}, the {_SCANNED_FAILURE_LIST_FIELDS[0]!r} list, and stderr (#318). NOT retrying without hook isolation on the strength of an unrecognised field -- that decision may not be reachable from arbitrary content (#202). If this is a genuine auth failure, capture is failing permanently and the fix is to add {field!r} to the scanned set: please file it against #320.")
    return False

def _build_cmd(tools: list[str] | None, isolate_hooks: bool) -> list[str]:
    allowed = tools or []
    cmd = [_resolve_claude_bin(), '-p', '--output-format', 'json', '--no-session-persistence', '--exclude-dynamic-system-prompt-sections', '--model', _resolve_model(), '--max-turns', _resolve_max_turns(), '--tools', ','.join(allowed), '--allowedTools', ','.join(allowed), '--mcp-config', '{"mcpServers":{}}', '--strict-mcp-config']
    if isolate_hooks:
        cmd += [_HOOK_ISOLATION_FLAG, '']
    return cmd

@contextlib.contextmanager
def _isolated_summarizer_cwd():
    d = tempfile.mkdtemp(prefix='remember-summarizer-cwd-')
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)
_CODEX_CHILD_ENV_ALLOW = frozenset({'PATH', 'HOME', 'LANG', 'LC_ALL', 'CODEX_HOME', 'TMPDIR', 'TEMP', 'TMP', 'SYSTEMROOT', 'USERPROFILE', 'APPDATA', 'PATHEXT', 'CODEX_API_KEY', 'HTTPS_PROXY', 'HTTP_PROXY', 'NO_PROXY', 'SSL_CERT_FILE', 'NODE_EXTRA_CA_CERTS'})
_CODEX_CHILD_ENV_ALLOW_UPPER = frozenset((name.upper() for name in _CODEX_CHILD_ENV_ALLOW))

def _codex_child_env() -> dict[str, str]:
    child = {k: v for k, v in os.environ.items() if k.upper() in _CODEX_CHILD_ENV_ALLOW_UPPER}
    child['REMEMBER_NESTED_SUMMARIZER'] = '1'
    return child

def _build_codex_cmd(output_file: str, cwd: str) -> list[str]:
    return [_resolve_codex_bin(), 'exec', '--sandbox', 'read-only', '--skip-git-repo-check', '--ephemeral', '--ignore-user-config', '-c', 'shell_environment_policy.inherit=none', '-C', cwd, '-o', output_file, '-']

def _call_codex(prompt: str, timeout: int=120) -> HaikuResult:
    try:
        slot = spawn_guard.claim(timeout=timeout)
    except spawn_guard.SummarizerSpawnDeclined as declined:
        _warn(f'WARNING: {declined}')
        raise
    if slot.degraded:
        _warn(f'WARNING: the summarizer spawn guard could not use {spawn_guard.record_dir()} ({slot.degraded}); this spawn is UNBOUNDED. Saves keep working -- an unusable runtime directory must not become a permanent save outage (#204) -- but nothing is counting summarizers until it is writable again.')
    try:
        with _isolated_summarizer_cwd() as summarizer_cwd:
            fd, out_path = tempfile.mkstemp(prefix='remember-codex-out-', suffix='.txt', dir=summarizer_cwd)
            os.close(fd)
            try:
                result = subprocess.run(_build_codex_cmd(out_path, summarizer_cwd), input=prompt, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout, env=_codex_child_env(), cwd=summarizer_cwd)
            except FileNotFoundError as missing:
                raise RuntimeError(f'codex CLI not found: {missing}') from missing
            except subprocess.TimeoutExpired as timed_out:
                _warn(f"WARNING: codex timed out after {timeout}s; codex's own usage/cost for this call (a different provider's figures, not tracked here) is unknown")
                raise RuntimeError(f'codex timed out after {timeout}s') from timed_out
            if result.returncode != 0:
                raise RuntimeError(f'codex exited {result.returncode}: {_failure_detail(result.stdout, result.stderr)}')
            try:
                with open(out_path, encoding='utf-8', errors='replace') as f:
                    text = f.read()
            except OSError as unreadable:
                raise RuntimeError(f'codex exited 0 but its output file could not be read: {unreadable}') from unreadable
    finally:
        slot.release()
    if not text.strip():
        raise RuntimeError('codex exited 0 but wrote no final message (-o file was empty)')
    model_skipped = text.strip().upper().startswith('SKIP')
    rejected = not model_skipped and (_is_non_summary(text) or _is_cli_notice(text))
    return HaikuResult(text=text, tokens=TokenUsage(), is_skip=model_skipped or rejected, is_rejected=rejected, provider='codex')

def call_haiku(prompt: str, tools: list[str] | None=None, timeout: int=120) -> HaikuResult:
    provider = _choose_summarizer_provider()
    if provider == 'codex':
        try:
            return _call_codex(prompt, timeout=timeout)
        except spawn_guard.SummarizerSpawnDeclined:
            raise
        except RuntimeError as codex_error:
            fallback = _resolve_summarizer_fallback()
            if fallback != 'claude':
                raise RuntimeError(f'could not summarize: {codex_error} (host-native summarizer unavailable, and no fallback is configured -- set REMEMBER_SUMMARIZER_FALLBACK=claude to opt into the Claude CLI as a fallback, or REMEMBER_SUMMARIZER=claude to always use it)') from codex_error
            _warn(f'WARNING: codex summarization failed ({codex_error}); falling back to claude -p because REMEMBER_SUMMARIZER_FALLBACK=claude is set. This bills Anthropic for what was meant to summarize on-host -- the same complaint #460 was filed over, now opted into rather than unconditional.')
    child = _child_env()
    try:
        slot = spawn_guard.claim(timeout=timeout)
    except spawn_guard.SummarizerSpawnDeclined as declined:
        _warn(f'WARNING: {declined}')
        raise
    if slot.degraded:
        _warn(f'WARNING: the summarizer spawn guard could not use {spawn_guard.record_dir()} ({slot.degraded}); this spawn is UNBOUNDED. Saves keep working -- an unusable runtime directory must not become a permanent save outage (#204) -- but nothing is counting summarizers until it is writable again.')

    def _run(isolate_hooks: bool):
        try:
            with _isolated_summarizer_cwd() as summarizer_cwd:
                return subprocess.run(_build_cmd(tools, isolate_hooks), input=prompt, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout, env=child, cwd=summarizer_cwd)
        except subprocess.TimeoutExpired as timed_out:
            _log_failed_spend(f'timed out after {timeout}s', timed_out.stdout)
            raise RuntimeError(f'claude timed out after {timeout}s')
    try:
        result = _run(isolate_hooks=True)
        if result.returncode != 0 and _isolation_may_be_the_cause(result.stdout, result.stderr):
            _isolation_haystack = _failure_haystack(result.stdout, result.stderr)
            if 'unknown option' in _isolation_haystack:
                _warn(f"WARNING: this CLI rejected {_HOOK_ISOLATION_FLAG} ({_failure_detail(result.stdout, result.stderr)}); retrying WITHOUT hook isolation so saves keep working. The nested call will run with your hooks registered -- a hook that blocks it returns its block message as if it were the model's reply (#202).")
            else:
                _warn(f"WARNING: this CLI failed authentication ({_failure_detail(result.stdout, result.stderr)}); retrying WITHOUT hook isolation in case the isolated run's own environment is simply missing a credential the normal one has. The nested call will run with your hooks registered -- a hook that blocks it returns its block message as if it were the model's reply (#202).")
            result = _run(isolate_hooks=False)
            if result.returncode != 0 and any((marker in _failure_haystack(result.stdout, result.stderr) for marker in _AUTH_FAILURE_MARKERS)):
                _warn(f"WARNING: the un-isolated retry failed with the same authentication error ({_failure_detail(result.stdout, result.stderr)}) -- hook isolation was not the cause. The CLI's own saved login has expired; refresh it (run `claude setup-token`, or log in again in your coding agent's own CLI). This plugin reads no credential of its own any more -- there is no setting here to configure (#129/#131/#860).")
    finally:
        slot.release()
    if result.returncode != 0:
        _log_failed_spend(f'exited {result.returncode}', result.stdout)
        detail = _failure_detail(result.stdout, result.stderr)
        raise RuntimeError(f'claude exited {result.returncode}: {detail}{_drop_env_hint(detail)}')
    return _parse_response(result.stdout)
DEFAULT_REJECT_PATTERN = "^\\s*(i (cannot|can't|can not|won't|will not|am unable|'m unable|am not able)|could you|please (provide|paste|share)|i'm sorry|i am sorry)\\b"

def _resolve_reject_pattern() -> 're.Pattern[str] | None':
    raw = os.environ.get('REMEMBER_REJECT_PATTERN', '').strip()
    if raw.lower() == 'none':
        return None
    pattern = raw if raw else DEFAULT_REJECT_PATTERN
    try:
        return re.compile(pattern, re.I)
    except re.error:
        return re.compile(DEFAULT_REJECT_PATTERN, re.I)

def _is_non_summary(text: str) -> bool:
    pattern = _resolve_reject_pattern()
    return bool(pattern.match(text or '')) if pattern else False
_CLI_NOTICE = re.compile('^\\s*\\w+ operation blocked by hook:', re.I)

def _is_cli_notice(text: str) -> bool:
    return bool(_CLI_NOTICE.match(text or ''))

def _parse_response(raw: str) -> HaikuResult:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f'invalid JSON from claude: {e}')
    if isinstance(data, list):
        text = ''
        for msg in reversed(data):
            if msg.get('type') == 'result':
                text = msg.get('result', '') or ''
                break
            content = msg.get('content', '')
            if isinstance(content, str) and content.strip():
                text = content
                break
            if isinstance(content, list):
                parts = [b.get('text', '') for b in content if isinstance(b, dict) and b.get('type') == 'text']
                if parts:
                    text = '\n'.join(parts)
                    break
        tokens = _extract_tokens(data[-1] if data else {})
    else:
        text = data.get('result') or ''
        tokens = _extract_tokens(data)
    model_skipped = text.strip().upper().startswith('SKIP')
    rejected = not model_skipped and (_is_non_summary(text) or _is_cli_notice(text))
    return HaikuResult(text=text, tokens=tokens, is_skip=model_skipped or rejected, is_rejected=rejected, provider='claude')

def _extract_tokens(data: dict) -> TokenUsage:
    usage = data.get('usage', {})
    input_tokens = usage.get('input_tokens', 0) or data.get('input_tokens', 0)
    output_tokens = usage.get('output_tokens', 0) or data.get('output_tokens', 0)
    cache_tokens = usage.get('cache_read_input_tokens', 0) or data.get('cache_read_input_tokens', 0)
    cost = data.get('total_cost_usd') or (input_tokens - cache_tokens) * HAIKU_INPUT_PRICE + output_tokens * HAIKU_OUTPUT_PRICE + cache_tokens * HAIKU_CACHE_PRICE
    return TokenUsage(input=input_tokens, output=output_tokens, cache=cache_tokens, cost_usd=cost)
