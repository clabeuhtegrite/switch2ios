#!/usr/bin/env python3
"""Summary of a memory statistics file (DM_MEMSTAT): work/logs/memstat-*.tsv from ./switch2mac.py run --phone or --memstat, or Documents/memstat.tsv from the iPhone app.
The host writes one line a second, in MiB (nxnative/dm/host/memstat.m): the process footprint (what iOS's memory limit counts), what iOS has left (iPhone only), Metal's
allocations, and the footprint split by class: guest (the game's memory block), code, malloc (the host's heap), gpu (Metal / IOSurface regions), stack, file, other.

  tools/memstat.py [file] [--from S] [--to S] [--budget SIZE] [--every S]

  file           default: the newest work/logs/memstat-*.tsv
  --from, --to   only these seconds (a part of the route, from the times in the run's log)
  --budget       the footprint a phone allows (default 5.5G, docs/ios.md): how long the run spent above it
  --every        the timeline's step in seconds (default 30): the highest footprint in each step, with its guest, gpu and malloc charge and Metal's allocations
"""
import argparse, statistics, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOTALS = [('phys_footprint', 'footprint'), ('metal_alloc', 'Metal allocated'), ('compressed', 'compressed (kernel)'), ('graphics', 'graphics ledger (kernel)')]
CLASSES = ['guest', 'code', 'malloc', 'gpu', 'stack', 'file', 'other']


def size(s):   # bytes, or whole K / M / G, as the host's switches take them; returned in MiB
    u = {'k': 1 / 1024, 'm': 1, 'g': 1024}.get(s[-1:].lower())
    return float(s[:-1]) * u if u else float(s) / (1 << 20)


def load(path):
    head, rows = None, []
    for line in Path(path).read_text(errors='replace').splitlines():
        if not line or line.startswith('#'): continue
        f = line.split('\t')
        if f[0] == 'seconds': head = f; continue
        if not head or len(f) != len(head): continue   # a line cut off by the end of the run
        r = {}
        for k, v in zip(head, f):
            if k in CLASSES: r[k] = [int(x) for x in v.split('/')]   # virtual / resident / charged
            else: r[k] = float(v)
        rows.append(r)
    if not head: sys.exit(f'{path}: no memstat header (seconds, phys_footprint, ...)')
    return rows


def gb(mib): return f'{mib / 1024:.2f}'


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('file', nargs='?')
    ap.add_argument('--from', dest='t0', type=float, default=0)
    ap.add_argument('--to', dest='t1', type=float, default=float('inf'))
    ap.add_argument('--budget', default='5.5G')
    ap.add_argument('--every', type=float, default=30)
    a = ap.parse_args()
    path = a.file
    if not path:
        files = sorted((ROOT / 'work/logs').glob('memstat-*.tsv'))
        if not files: sys.exit('no work/logs/memstat-*.tsv: run ./switch2mac.py run --phone first, or name the file')
        path = files[-1]
    rows = [r for r in load(path) if a.t0 <= r['seconds'] <= a.t1]
    if not rows: sys.exit(f'{path}: no samples in that window')
    t = [r['seconds'] for r in rows]
    print(f'{Path(path).name}: {t[0]:.0f}-{t[-1]:.0f} s, {len(rows)} samples (GB = GiB)')
    print(f'{"":28}{"peak":>8}{"at s":>7}{"p95":>8}{"median":>8}{"last":>8}')

    def line(name, vals):
        i = max(range(len(vals)), key=vals.__getitem__); s = sorted(vals)
        print(f'{name:28}{gb(vals[i]):>8}{t[i]:>7.0f}{gb(s[min(len(s) - 1, int(len(s) * 0.95))]):>8}{gb(statistics.median(s)):>8}{gb(vals[-1]):>8}')
    for k, name in TOTALS:
        if k in rows[0]: line(name, [r[k] for r in rows])
    for c in CLASSES:
        if c in rows[0]: line(f'charged: {c}', [r[c][2] for r in rows])
    if 'unattributed' in rows[0]: line('unattributed', [r['unattributed'] for r in rows])
    if 'guest' in rows[0]: line('resident: guest', [r['guest'][1] for r in rows])
    if any(r.get('available') for r in rows): line('available (iOS)', [r['available'] for r in rows])
    vg = max(r['guest'][0] for r in rows) if 'guest' in rows[0] else 0
    if vg: print(f'guest block (virtual): {gb(vg)} GB')

    budget = size(a.budget); over = [r for r in rows if r['phys_footprint'] > budget]
    print(f'above {gb(budget)} GB: {len(over)} of {len(rows)} samples' + (f', first at {over[0]["seconds"]:.0f} s' if over else ''))

    print(f'\ntimeline: the highest footprint in each {a.every:.0f} s, with that sample\'s charge (GB)')
    print(f'{"s":>6}{"footprint":>11}{"guest":>8}{"gpu":>8}{"malloc":>8}{"Metal":>8}')
    step = {}
    for r in rows:
        k = int((r['seconds'] - t[0]) // a.every)
        if k not in step or r['phys_footprint'] > step[k]['phys_footprint']: step[k] = r
    for k in sorted(step):
        r = step[k]; c = lambda n: gb(r[n][2]) if n in r else '-'
        print(f'{t[0] + k * a.every:>6.0f}{gb(r["phys_footprint"]):>11}{c("guest"):>8}{c("gpu"):>8}{c("malloc"):>8}{gb(r.get("metal_alloc", 0)):>8}')


if __name__ == '__main__':
    main()
