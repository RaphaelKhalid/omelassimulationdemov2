"""Copy per-run replay, manifest, checkpoint, usage and refusal files from the runs directory into results/runs/."""
import shutil
from runner import RUNS, ROOT, export_run

DEST = ROOT / 'results' / 'runs'
for d in sorted(RUNS.glob('*')):
    if not (d / 'manifest.json').exists() or not (d / 'weeks').exists(): continue
    export_run(d)
    out = DEST / d.name; out.mkdir(parents=True, exist_ok=True)
    for name in ('replay.json', 'manifest.json', 'checkpoint.json', 'refusals.jsonl'):
        if (d / name).exists(): shutil.copy2(d / name, out / name)
    for name in ('usage_summary.json', 'dispatch.log'):
        if (d / 'external_calls' / name).exists(): shutil.copy2(d / 'external_calls' / name, out / name)
    print('packaged', d.name)
