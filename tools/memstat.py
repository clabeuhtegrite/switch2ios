#!/usr/bin/env python3
"""Summary of a memory statistics file (DM_MEMSTAT): work/logs/memstat-*.tsv from ./switch2mac.py run --phone or --memstat, or Documents/memstat.tsv from the iPhone app.
The host writes one line a second, in MiB (nxnative/dm/host/memstat.m): the process footprint (what iOS's memory limit counts), what iOS has left (iPhone only), Metal's
allocations, and the footprint split by class: guest (the game's memory block), code, malloc (the host's heap), gpu (Metal / IOSurface regions), stack, file, other;
then what Metal holds by kind: the memory pools' no-copy buffers (guest memory: the ones the GPU uses, and all), packaged textures (uploaded copies of pool bytes), other
textures (render targets, GPU-written), heaps, other buffers, the pools' pages in memory, and the pipelines built (a count).
Hosts from 2026-11-04 on add the guest in a file (DM_GUEST_FILE: its pages leave the footprint and the guest's charge): its dirty pages, its blocks on disk and the room left
there, what the process read, wrote and paged in, the whole system's paging and free memory, what each sample took and the host's clock. A "-" is a value not read for that
sample (the region walk left out with DM_MEMSTAT_WALK_EVERY, no guest file), which the figures leave out. A line starting with # between the rows is a mark another part of the
host wrote (memstat_note): the marks are listed with the time of the row after them, and each one starts a section, for which the disk writes, the page-ins and the system's
page-outs are given per second. A flush is the guest file's dirty pages falling by 64 MiB or more within 2 s: the system writing them back to the file, or pages the host punched out of it (DM_GUEST_FILE_PUNCH).

  tools/memstat.py [file] [--from S] [--to S] [--budget SIZE] [--every S] [--sections RE] [--session FILE]

  file           default: the newest work/logs/memstat-*.tsv
  --from, --to   only these seconds (a part of the route, from the times in the run's log)
  --budget       the footprint a phone allows (default 5.5G, docs/ios.md): how long the run spent above it
  --every        the timeline's step in seconds (default 30): the highest footprint in each step, with its guest, gpu and malloc charge and Metal's allocations
  --sections     only the marks this regular expression finds start a section (default: every mark)
  --session      a session file of tools/phone_session.py (work/logs/phone-session-*.tsv): its steps and marks become marks at their seconds, so each step starts a section
"""
import argparse, re, statistics, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOTALS = [('phys_footprint', 'footprint'), ('metal_alloc', 'Metal allocated'), ('compressed', 'compressed (kernel)'), ('graphics', 'graphics ledger (kernel)')]
CLASSES = ['guest', 'code', 'malloc', 'gpu', 'stack', 'file', 'other']
KINDS = [('pools_resident', 'Metal: pools in use by GPU'), ('pools_all', 'Metal: all pools'), ('tex_packaged', 'Metal: packaged textures'), ('tex_other', 'Metal: other textures'),
         ('heaps', 'Metal: heaps'), ('buffers', 'Metal: other buffers'), ('guest_in_pools', 'guest pages in pools')]   # nvn_mtl.m mtl_mem_kinds (hosts from 2026-11-03 on)
LEVELS = [('external', 'file pages mapped'), ('guest_dirty', 'guest file: dirty'), ('file_alloc', 'guest file: on disk'), ('host_free', 'system: free'),
          ('host_inactive', 'system: inactive'), ('host_compressor', 'system: compressor')]   # hosts from 2026-11-04 on
FLUSH_MIB, FLUSH_S = 64, 2   # a flush: guest_dirty falls by this much within this many seconds
LIST_MAX = 100   # marks and sections listed at most


def size(s):   # bytes, or whole K / M / G, as the host's switches take them; returned in MiB
    u = {'k': 1 / 1024, 'm': 1, 'g': 1024}.get(s[-1:].lower())
    return float(s[:-1]) * u if u else float(s) / (1 << 20)


def load(path):   # → the rows, the marks (the index of the row after each, its text) and the header's comment
    head, rows, marks, note = None, [], [], ''
    for line in Path(path).read_text(errors='replace').splitlines():
        if not line: continue
        if line.startswith('#'):
            if head: marks.append((len(rows), line[1:].strip()))   # memstat_note, between two rows
            else: note += line
            continue
        f = line.split('\t')
        if f[0] == 'seconds': head = f; continue
        if not head or len(f) != len(head): continue   # a line cut off by the end of the run
        r = {}
        try:
            for k, v in zip(head, f):
                if v == '-': r[k] = None   # not read for this sample (memstat.m)
                elif k in CLASSES: r[k] = [int(x) for x in v.split('/')]   # virtual / resident / charged
                else: r[k] = float(v)
        except ValueError: continue   # cut off inside its last value
        rows.append(r)
    if not head: sys.exit(f'{path}: no memstat header (seconds, phys_footprint, ...)')
    return rows, marks, note


def gb(mib): return f'{mib / 1024:.2f}'


def part(r, c, i): return r[c][i] if r.get(c) is not None else None   # one of a class's virtual / resident / charged, None where the walk was left out


def flushes(rows):   # (start s, end s, MiB before, MiB after) of each fall of guest_dirty by FLUSH_MIB within FLUSH_S, the whole fall around it: the system writing the pages back to the file, or pages the host punched out of it (DM_GUEST_FILE_PUNCH)
    v = [(r['seconds'], r['guest_dirty']) for r in rows if r.get('guest_dirty') is not None]
    out, i = [], 0
    while i < len(v) - 1:
        if v[i + 1][1] >= v[i][1] or v[i + 1][0] - v[i][0] > FLUSH_S: i += 1; continue
        j = i + 1   # a run of falling samples, none more than FLUSH_S after the one before
        while j + 1 < len(v) and v[j + 1][1] < v[j][1] and v[j + 1][0] - v[j][0] <= FLUSH_S: j += 1
        hit = [(p, q) for p in range(i, j) for q in range(p + 1, j + 1) if v[q][0] - v[p][0] <= FLUSH_S and v[p][1] - v[q][1] >= FLUSH_MIB]
        if hit:
            p, q = min(h[0] for h in hit), max(h[1] for h in hit)
            out.append((v[p][0], v[q][0], v[p][1], v[q][1]))
        i = j
    return out


def regex(s):   # --sections: a usage error, not a traceback, for an expression re cannot compile
    try: return re.compile(s)
    except re.error as e: raise argparse.ArgumentTypeError(f'{s!r}: {e}')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('file', nargs='?')
    ap.add_argument('--from', dest='t0', type=float, default=0)
    ap.add_argument('--to', dest='t1', type=float, default=float('inf'))
    ap.add_argument('--budget', default='5.5G')
    ap.add_argument('--every', type=float, default=30)
    ap.add_argument('--sections', type=regex)
    ap.add_argument('--session')
    a = ap.parse_args()
    path = a.file
    if not path:
        files = sorted((ROOT / 'work/logs').glob('memstat-*.tsv'))
        if not files: sys.exit('no work/logs/memstat-*.tsv: run ./switch2mac.py run --phone first, or name the file')
        path = files[-1]
    every_row, marks, note = load(path)
    if a.session:   # tools/phone_session.py: seconds since the launch, the Mac's clock, the kind, the text; each one a mark before the first row at or after its seconds
        for line in Path(a.session).read_text(errors='replace').splitlines():
            f = line.split('\t')
            if len(f) < 4 or line.startswith('#') or f[0] == 'seconds': continue
            try: ts = float(f[0])
            except ValueError: continue
            marks.append((next((i for i, r in enumerate(every_row) if r['seconds'] >= ts), len(every_row)), f'{f[2]}: {f[3]}'))
        marks.sort(key=lambda m: m[0])
    win = [i for i, r in enumerate(every_row) if a.t0 <= r['seconds'] <= a.t1]
    rows = [every_row[i] for i in win]
    if not rows: sys.exit(f'{path}: no samples in that window')
    t = [r['seconds'] for r in rows]
    print(f'{Path(path).name}: {t[0]:.0f}-{t[-1]:.0f} s, {len(rows)} samples (GB = GiB)')
    print(f'{"":28}{"peak":>8}{"at s":>7}{"p95":>8}{"median":>8}{"last":>8}')

    def line(name, vals):   # the samples without the value ("-") are left out
        tv = [(ti, v) for ti, v in zip(t, vals) if v is not None]
        if not tv: return
        ts, vals = [x[0] for x in tv], [x[1] for x in tv]
        i = max(range(len(vals)), key=vals.__getitem__); s = sorted(vals)
        print(f'{name:28}{gb(vals[i]):>8}{ts[i]:>7.0f}{gb(s[min(len(s) - 1, int(len(s) * 0.95))]):>8}{gb(statistics.median(s)):>8}{gb(vals[-1]):>8}')
    for k, name in TOTALS:
        if k in rows[0]: line(name, [r[k] for r in rows])
    for c in CLASSES:
        if c in rows[0]: line(f'charged: {c}', [part(r, c, 2) for r in rows])
    if 'unattributed' in rows[0]: line('unattributed', [r['unattributed'] for r in rows])
    if any(r.get('pools_all') for r in rows):
        for k, name in KINDS: line(name, [r[k] for r in rows])
        pso = [int(r['pipelines']) for r in rows if r.get('pipelines') is not None]; print(f'{"pipelines (count)":28}{max(pso):>8}{"":7}{"":8}{int(statistics.median(pso)):>8}{pso[-1]:>8}')
    if 'guest' in rows[0]: line('resident: guest', [part(r, 'guest', 1) for r in rows])
    if any(r.get('available') for r in rows): line('available (iOS)', [r['available'] for r in rows])
    gfile = any(r.get('file_alloc') is not None for r in rows)   # a guest file (DM_GUEST_FILE): without one guest_dirty is 0 throughout
    for k, name in LEVELS:
        if (k != 'guest_dirty' or gfile) and any(r.get(k) is not None for r in rows): line(name, [r.get(k) for r in rows])
    vg = max((part(r, 'guest', 0) or 0 for r in rows), default=0) if 'guest' in rows[0] else 0
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
        r = step[k]; c = lambda n: gb(r[n][2]) if r.get(n) is not None else '-'
        print(f'{t[0] + k * a.every:>6.0f}{gb(r["phys_footprint"]):>11}{c("guest"):>8}{c("gpu"):>8}{c("malloc"):>8}{gb(r.get("metal_alloc", 0)):>8}')

    # Hosts from 2026-11-04 on: what the process and the system did, the marks and their sections, the flushes of the guest file, what the samples took
    def have(k): return any(r.get(k) is not None for r in rows)
    out = []
    if have('lifetime_max_footprint'): out.append(f'footprint peak since launch (kernel): {gb(max(r["lifetime_max_footprint"] for r in rows if r.get("lifetime_max_footprint") is not None))} GB')
    if have('disk_free'):
        df = [(r['disk_free'], r['seconds']) for r in rows if r.get('disk_free') is not None]; lo = min(df)
        out.append(f'disk free on the guest file\'s volume: lowest {gb(lo[0])} GB at {lo[1]:.0f} s, last {gb(df[-1][0])} GB')
    if have('sample_ms'):
        ms = [(r['sample_ms'], r['seconds']) for r in rows if r.get('sample_ms') is not None]; w = max(ms)
        left = sum(1 for r in rows if 'guest' in r and r['guest'] is None)
        out.append(f'slowest sample: {w[0]:.2f} ms at {w[1]:.0f} s (median {statistics.median(x[0] for x in ms):.2f} ms, {sum(x[0] > 5 for x in ms)} of {len(ms)} over 5 ms)'
                   + (f'; region walk left out in {left} samples (DM_MEMSTAT_WALK_EVERY)' if left else ''))
    if out: print('\n' + '\n'.join(out))

    # A mark lies between two rows: it gets the time of the row after it (the last row's when none came after), and the index of that row in the window
    wm = []
    for i, text in marks:
        if not every_row: break
        at = every_row[min(i, len(every_row) - 1)]['seconds']
        if a.t0 <= at <= a.t1: wm.append((i - win[0], at, text))
    if wm:
        print(f'\nmarks (memstat_note), at the time of the row after them: {len(wm)}')
        for _, at, text in wm[:LIST_MAX]: print(f'{at:>6.0f} s  {text}')
        if len(wm) > LIST_MAX: print(f'  ... {len(wm) - LIST_MAX} more (grep "^#" in the file)')

    m = re.search(r'pages of (\d+) bytes', note); page = int(m.group(1)) if m else 16384   # host_pageouts' unit (memstat.m's header line)
    if have('disk_written') or have('pageins') or have('host_pageouts'):
        def rate(k, i0, i1):   # per second from row i0 to row i1, between the first and the last of them that have the value
            v = [(rows[i]['seconds'], rows[i][k]) for i in range(i0, i1 + 1) if rows[i].get(k) is not None]
            return (v[-1][1] - v[0][1]) / (v[-1][0] - v[0][0]) if len(v) > 1 and v[-1][0] > v[0][0] else None
        def fmt(x, w, d): return f'{x:>{w}.{d}f}' if x is not None else f'{"-":>{w}}'
        starts = {}   # a section starts at the row after each mark the --sections expression finds (every mark without it)
        for i, _, text in wm:
            if i < len(rows) and (not a.sections or a.sections.search(text)): starts.setdefault(max(i, 0), []).append(text)
        bounds = sorted(set([0] + list(starts)))
        secs = [(b, (bounds[n + 1] if n + 1 < len(bounds) else len(rows)) - 1, ' | '.join(starts.get(b, ['(start)']))) for n, b in enumerate(bounds)]
        print(f'\nper second in each section, a section starting at each mark{" the --sections expression finds" if a.sections else ""}: what the process wrote to disk, its page-ins, the whole system\'s page-outs;'
              ' then the footprint\'s peak in the section and how much the malloc charge moved across it (MiB)')
        print(f'{"from":>6}{"to":>7}{"written MiB/s":>15}{"page-ins/s":>12}{"page-outs/s":>13}{"(MiB/s)":>10}{"peak":>7}{"malloc":>8}  section')
        def peak(i0, i1):
            v = [rows[k]['phys_footprint'] for k in range(i0, i1 + 1) if rows[k].get('phys_footprint') is not None]
            return f'{max(v):>7.0f}' if v else f'{"-":>7}'
        def dmalloc(b, i1):   # the malloc class's charged MiB, last row minus the row before the section
            v = [rows[k]['malloc'][2] for k in range(b, i1 + 1) if rows[k].get('malloc')]
            return f'{v[-1] - v[0]:>+8.0f}' if len(v) > 1 else f'{"-":>8}'
        for i0, i1, name in (secs if len(secs) > 1 else [])[:LIST_MAX] + [(0, len(rows) - 1, 'all')]:
            b = max(i0 - 1, 0)   # from the last row of the section before, so that the sections add up to the whole
            po = rate('host_pageouts', b, i1); pm = fmt(po * page / (1 << 20) if po is not None else None, 0, 2).strip()
            print(f'{rows[i0]["seconds"]:>6.0f}{rows[i1]["seconds"]:>7.0f}{fmt(rate("disk_written", b, i1), 15, 2)}{fmt(rate("pageins", b, i1), 12, 1)}{fmt(po, 13, 1)}'
                  f'{"(" + pm + ")":>10}{peak(i0, i1)}{dmalloc(b, i1)}  {name[:60]}')
        if len(secs) > LIST_MAX: print(f'  ... {len(secs) - LIST_MAX} more sections (--sections RE starts them at fewer marks)')

    if have('guest_dirty') and have('file_alloc'):
        fl = flushes(rows)
        gap = f', one every {(fl[-1][0] - fl[0][0]) / (len(fl) - 1):.0f} s on average' if len(fl) > 1 else ''
        nd = sum(1 for r in rows if 'guest_dirty' in r and r['guest_dirty'] is None)
        print(f'\nflushes (guest_dirty down {FLUSH_MIB} MiB or more within {FLUSH_S} s): {len(fl)}{gap}'
              + (f'; guest_dirty was not read in {nd} samples (DM_MEMSTAT_WALK_EVERY), where a flush shows only between two readings {FLUSH_S} s apart or less' if nd else ''))
        if fl: print(f'{"at s":>6}{"MiB":>8}{"in s":>6}{"dirty MiB before":>18}{"after":>8}{"s since the last":>18}')
        for n, (s0, s1, hi, lo) in enumerate(fl):
            print(f'{s0:>6.0f}{hi - lo:>8.0f}{s1 - s0:>6.0f}{hi:>18.0f}{lo:>8.0f}' + (f'{s0 - fl[n - 1][0]:>18.0f}' if n else ''))


if __name__ == '__main__':
    main()
