"""Fable orchestrator: dispatch each pending resident request to an isolated Claude CLI
invocation (claude -p) and feed the responses back through runner.py's external protocol.

Each resident call is a fresh, tool-less, MCP-less `claude -p` process that receives ONLY
that request's system message, user message and JSON schema. Raw CLI output, timestamps,
usage and errors are logged under runs/<run>/external_calls/. Invalid outputs are sent back
to the same resident for a formatting correction; they are never replaced by orchestrator
judgment. engine.py and runner.py are not modified.
"""
import argparse, concurrent.futures, datetime, json, os, pathlib, shutil, subprocess, sys, threading, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from runner import validate, read, save  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent
PY = sys.executable
CLAUDE = shutil.which('claude')
LOCK = threading.Lock()


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def log(run_dir, msg):
    line = f'{stamp()} {msg}'
    print(line, flush=True)
    with LOCK, (run_dir / 'external_calls' / 'dispatch.log').open('a', encoding='utf8') as f:
        f.write(line + '\n')


def cli_version():
    try:
        return subprocess.run([CLAUDE, '--version'], capture_output=True, text=True, timeout=60).stdout.strip()
    except Exception as e:  # pragma: no cover
        return f'unknown ({type(e).__name__})'


def call_claude(cfg, system, user, schema, workdir):
    """One isolated resident invocation. Returns (record, parsed_or_None)."""
    cmd = [CLAUDE, '-p', '--no-session-persistence', '--model', cfg['model'], '--effort', cfg['effort'],
           '--tools', '', '--strict-mcp-config', '--setting-sources', '', '--output-format', 'json',
           '--max-turns', '4', '--system-prompt', system, '--json-schema', json.dumps(schema)]
    env = dict(os.environ)
    env.pop('OPENROUTER_API_KEY', None)  # residents never see the API key
    started = stamp(); t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, input=user, capture_output=True, text=True, encoding='utf8', errors='replace',
                              cwd=workdir, env=env, timeout=cfg['timeout'])
        stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as e:
        stdout, stderr, code = (e.stdout or ''), (e.stderr or '') + f'\nTIMEOUT after {cfg["timeout"]}s', -1
    elapsed = time.monotonic() - t0
    rec = dict(started=started, finished=stamp(), elapsed_seconds=round(elapsed, 2), exit_code=code,
               cli_flags=[c for c in cmd[1:] if c not in (system, json.dumps(schema))],
               raw_stdout=stdout, stderr=stderr[-4000:])
    parsed = None
    try:
        out = json.loads(stdout)
        rec['cli_result'] = {k: out.get(k) for k in ('is_error', 'subtype', 'api_error_status', 'stop_reason', 'terminal_reason',
                                                     'num_turns', 'duration_api_ms', 'session_id', 'uuid', 'total_cost_usd', 'usage', 'modelUsage')}
        rec['result_text'] = out.get('result')
        rec['models_used'] = list((out.get('modelUsage') or {}).keys())
        if out.get('is_error'):
            rec['error'] = f"cli_is_error: {str(out.get('result'))[:500]}"
        elif 'structured_output' in out:
            parsed = out['structured_output']
        else:
            # Fall back to the result text if the model wrote the JSON directly.
            try:
                parsed = json.loads(out.get('result') or '')
            except Exception:
                rec['error'] = 'no structured_output and result is not JSON'
    except Exception as e:
        rec['error'] = f'unparseable CLI stdout ({type(e).__name__}); exit {code}'
    return rec, parsed


def transient(rec):
    e = (rec.get('error') or '') + (rec.get('stderr') or '')
    status = (rec.get('cli_result') or {}).get('api_error_status')
    return status in (429, 500, 502, 503, 504, 529, 408) or any(k in e.lower() for k in ('rate limit', 'overloaded', 'timeout', '529', '429', 'econnreset', 'fetch failed', 'unparseable'))


def ask(run_dir, cfg, req, workdir):
    path = run_dir / 'external_calls' / f"w{req['week']:02}_{req['stage']}_{req['agent']}.json"
    if path.exists():
        cached = read(path)
        if cached.get('request') == req and cached.get('parsed') is not None:
            return cached['parsed']
        if cached.get('request') != req:
            raise RuntimeError(f'Cached request mismatch for {path.name}; refusing to reuse.')
    system, user = req['messages'][0]['content'], req['messages'][1]['content']
    assert req['messages'][0]['role'] == 'system' and req['messages'][1]['role'] == 'user' and len(req['messages']) == 2
    attempts = []; corrections = 0; parsed = None
    prompt = user
    for attempt in range(cfg['max_attempts']):
        rec, candidate = call_claude(cfg, system, prompt, req['schema'], workdir)
        rec['attempt'] = attempt; rec['kind'] = 'correction' if prompt is not user else 'initial'
        if candidate is not None:
            try:
                validate(candidate, req['schema']); rec['validation'] = 'ok'; parsed = candidate
            except Exception as e:
                rec['validation'] = str(e); rec['rejected_output'] = candidate
        attempts.append(rec)
        save(path, dict(request=req, model=cfg['model'], effort=cfg['effort'], provider='claude_cli', attempts=attempts, parsed=parsed))
        if parsed is not None:
            break
        if rec.get('validation') and rec.get('validation') != 'ok':
            # Formatting correction from the SAME resident with the same context; log both.
            corrections += 1
            if corrections > cfg['max_corrections']:
                raise RuntimeError(f'{path.name}: repeated schema failure after correction; paused, not replaced.')
            prompt = (user + '\n\nFORMAT CORRECTION REQUEST: your previous reply was\n' + json.dumps(candidate, ensure_ascii=False)[:3000]
                      + '\nIt failed validation: ' + rec['validation'] + '. Reply again with only a JSON object exactly matching the schema (same keys, no extras).')
            log(run_dir, f'{path.stem}: validation failed ({rec["validation"]}); requesting correction from same resident')
            continue
        if transient(rec) and attempt < cfg['max_attempts'] - 1:
            delay = min(120, 10 * (attempt + 1))
            log(run_dir, f'{path.stem}: transient failure ({rec.get("error")}); retry in {delay}s')
            time.sleep(delay); continue
        raise RuntimeError(f'{path.name}: call failed: {rec.get("error")}')
    return parsed


def runner_cmd(*args):
    p = subprocess.run([PY, str(ROOT / 'runner.py'), *args], capture_output=True, text=True, encoding='utf8', cwd=ROOT)
    if p.returncode != 0:
        raise RuntimeError(f'runner.py {" ".join(args)} failed:\n{p.stdout}\n{p.stderr}')
    return p.stdout.strip()


def update_manifest(run_dir, cfg):
    m = read(run_dir / 'manifest.json')
    m['backend'] = 'external'; m['provider'] = 'claude_cli'; m['model'] = cfg['model']; m['reasoning'] = cfg['effort']
    m['planned_weeks'] = cfg['weeks']
    m['resident_model'] = dict(model=cfg['model'], provider='claude_cli', reasoning_effort=cfg['effort'], cli_version=cli_version(),
                               invocation='claude -p --no-session-persistence --tools "" --strict-mcp-config --setting-sources "" --output-format json --json-schema <schema> --system-prompt <system message>; user message on stdin',
                               isolation='one fresh process per resident request; no tools, no MCP, no shared session, no repository access')
    m['orchestrator_model'] = dict(model='claude-fable-5-1', role='dispatch/logging only; makes no resident decisions', session='https://claude.ai/code/session_015L6zcq7ndpsNh1HPSNMNek')
    save(run_dir / 'manifest.json', m)


def usage_summary(run_dir):
    tot = dict(calls=0, attempts=0, corrections=0, failed_attempts=0, input_tokens=0, cache_creation_input_tokens=0, cache_read_input_tokens=0,
               output_tokens=0, thinking_tokens=0, list_cost_usd=0.0, models={}, elapsed_seconds=0.0)
    for f in sorted((run_dir / 'external_calls').glob('w*.json')):
        d = read(f); tot['calls'] += 1
        for a in d['attempts']:
            tot['attempts'] += 1; tot['elapsed_seconds'] += a.get('elapsed_seconds', 0)
            if a.get('kind') == 'correction': tot['corrections'] += 1
            if a.get('validation') != 'ok': tot['failed_attempts'] += 1
            for model, u in ((a.get('cli_result') or {}).get('modelUsage') or {}).items():
                tot['models'][model] = tot['models'].get(model, 0) + 1
                tot['input_tokens'] += u.get('inputTokens', 0); tot['cache_creation_input_tokens'] += u.get('cacheCreationInputTokens', 0)
                tot['cache_read_input_tokens'] += u.get('cacheReadInputTokens', 0); tot['output_tokens'] += u.get('outputTokens', 0)
                tot['thinking_tokens'] += u.get('thinkingTokens', 0) or 0; tot['list_cost_usd'] += u.get('costUSD', 0) or 0
    tot['note'] = 'list_cost_usd is the CLI-reported list-price equivalent; actual billing is via the CLI account/subscription, not OpenRouter.'
    save(run_dir / 'external_calls' / 'usage_summary.json', tot)
    return tot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', required=True); ap.add_argument('--model', default='claude-opus-5-5'); ap.add_argument('--effort', default='high')
    ap.add_argument('--weeks', type=int, default=52); ap.add_argument('--workers', type=int, default=10)
    ap.add_argument('--timeout', type=int, default=900); ap.add_argument('--max-attempts', type=int, default=6); ap.add_argument('--max-corrections', type=int, default=2)
    ap.add_argument('--max-phases', type=int, default=0, help='stop after N phases (testing)')
    a = ap.parse_args()
    if not CLAUDE: raise SystemExit('claude CLI not found')
    cfg = dict(model=a.model, effort=a.effort, weeks=a.weeks, timeout=a.timeout, max_attempts=a.max_attempts, max_corrections=a.max_corrections)
    run_dir = ROOT / 'runs' / a.run
    (run_dir / 'external_calls').mkdir(parents=True, exist_ok=True)
    workdir = pathlib.Path(os.environ.get('TEMP', str(ROOT))) / 'omelas_resident_cwd'; workdir.mkdir(parents=True, exist_ok=True)
    update_manifest(run_dir, cfg)
    log(run_dir, f'start run={a.run} model={cfg["model"]} effort={cfg["effort"]} workers={a.workers} cli={cli_version()}')
    phases = 0
    while True:
        cp = read(run_dir / 'checkpoint.json')
        if cp['week'] > a.weeks:
            break
        runner_cmd('external-next', '--run', a.run)
        reqs = read(run_dir / 'pending_requests.json')
        log(run_dir, f'week {cp["week"]} stage {cp["stage"]}: {len(reqs)} resident requests')
        if reqs:
            with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as pool:
                vals = list(pool.map(lambda r: ask(run_dir, cfg, r, workdir), reqs))
            responses = {r['agent']: v for r, v in zip(reqs, vals)}
        else:
            responses = {}
        save(run_dir / 'responses.json', responses)
        out = runner_cmd('external-accept', '--run', a.run, '--responses', str(run_dir / 'responses.json'))
        if out: log(run_dir, out)
        phases += 1
        if a.max_phases and phases >= a.max_phases:
            log(run_dir, 'stopping: max phases reached'); break
    tot = usage_summary(run_dir)
    log(run_dir, 'usage ' + json.dumps({k: v for k, v in tot.items() if k != 'note'}))
    cp = read(run_dir / 'checkpoint.json')
    log(run_dir, f'checkpoint week={cp["week"]} stage={cp["stage"]}')


if __name__ == '__main__':
    main()
