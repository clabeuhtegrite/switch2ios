#!/usr/bin/env python3
"""Compute shaders that store on a dispatch's first invocation only, in a built Mac shader library. Read-only.

Mario Kart World's trees, bushes, cacti and rocks vanish for 0.6 s to several seconds while their shadows stay. nxnative found the same kind of defect in Splatoon Raiders
(c8521ee, docs/reference/shaders.md "A store only a dispatch's first invocation makes"): a compute shader resets an append counter in its preamble,
`if (-local.x == workgroup.x << log2(local size)) buffer[k] = 0`, and the other invocations add to that counter hundreds of instructions later. The Switch starts the first
workgroup ahead of the rest; Apple's GPUs start workgroups in no order, so a workgroup that appended before the reset lost its entries, and the plants built from that list
flickered. nxnative's fix takes the store out of the shader (dmsh_build.py first_store) and the host makes it before the dispatch. Our port is not in this tree yet: it changes
the translation chain (every shader is built again), so first this scan says whether any of the game's compute shaders has the pattern.

Two levels:
  exact   nxnative's own pattern (its regular expression, unchanged): if a shader matches, its fix applies as it is
  near    our shaders go through nx2_lower and the Maxwell front end, so the same preamble can come out written another way: an `if` near the start of the kernel whose body
          only stores constants into storage buffer words, after code reading the workgroup and invocation ids. Each one is printed with the lines before it, to be read.
A shader of either level that also adds atomically into the buffer it resets is an append counter: the case nxnative fixed.

Usage: tools/first_store_scan.py <dir> [--map shader_map.tsv] [--lines N]
  <dir>: the library (work/cache/metal_shader_cache/b-<chain>, which holds <key>_b.json and <key>_b.metal), or a folder above it: the newest b-* folder under it is read.
  The shader names come from the profile's shader_map.tsv (work/profile/<title>/shader_map.tsv, found from <dir> when --map is left out).
"""
import argparse, glob, json, os, re, sys

# nxnative dm/gfx/tools/dmsh_build.py at c8521ee, unchanged
FIRST_STORE = re.compile(r'float (\w+) = as_type<float>\(gl_WorkGroupID\.x\);\s*float (\w+) = as_type<float>\(gl_LocalInvocationID\.x\);\s*int (\w+) = as_type<int>\(\1\) << (\d+);\s*'
                         r'int (\w+) = ~as_type<int>\(\2\);\s*int (\w+) = \5 \+ 1;\s*bool (\w+) = \6 != \3;\s*if \(!\7\)\s*\{\s*((?:\(\*\w+\.\w+\)\._m0\[\d+\] = \d+u;\s*)+)\}')
STORE = re.compile(r'\(\*\w+\.(\w+)\)\._m0\[(\d+)\] = (\d+)u;')


def exact(msl, local):
    m = FIRST_STORE.search(msl)
    if not m or not local or list(local[1:]) != [1, 1] or 1 << int(m.group(4)) != local[0]: return None
    return [(n, int(w), int(v)) for n, w, v in STORE.findall(m.group(8))]


# An `if` whose body is only constant stores into storage buffer words (written either way SPIRV-Cross writes a storage buffer member)
W = r'(?:\(\*\w+\.(\w+)\)\.|\w+\.(\w+)->)_m0\[(\d+)u?\] = (\d+)u?;'
GUARDED = re.compile(r'if \(([^;{}]{1,200}?)\)\s*\{\s*((?:(?:\(\*\w+\.\w+\)\.|\w+\.\w+->)_m0\[\d+u?\] = \d+u?;\s*)+)\}')
STORE_ANY = re.compile(W)
IDS = ('gl_WorkGroupID', 'gl_LocalInvocationID', 'gl_GlobalInvocationID', 'gl_LocalInvocationIndex')


def near(msl):
    k = msl.find('kernel void ')
    if k < 0: return []
    body = msl.find('{', msl.find(')\n', k))
    if body < 0: return []
    found = []
    for m in GUARDED.finditer(msl, body):
        pre = msl[body:m.start()]
        if len(pre) > 6000: break   # a preamble, not a store deep in the program
        if re.search(r'\b(for|while|do)\b\s*[\({]', pre): break   # past the first loop
        if not any(i in pre for i in IDS): continue
        found.append((m, [(a or b, int(w), int(v)) for a, b, w, v in STORE_ANY.findall(m.group(2))]))
    return found


def atomics_on(msl, names):
    return sorted({n for n in names if re.search(r'atomic_\w+_explicit\(\(device atomic_u?int\*\)&(?:\(\*\w+\.' + re.escape(n) + r'\)\.|\w+\.' + re.escape(n) + r'->)', msl)})


def library(d):
    if glob.glob(os.path.join(d, '*_b.json')): return d
    libs = [p for p in glob.glob(os.path.join(d, '**', 'b-*'), recursive=True) if os.path.isdir(p) and glob.glob(os.path.join(p, '*_b.json'))]
    if not libs: sys.exit(f'no shader library (<key>_b.json files) in or under {d}')
    return max(libs, key=os.path.getmtime)


def names(lib, path):
    if not path:
        up = os.path.abspath(lib)
        for _ in range(5):
            up = os.path.dirname(up); maps = glob.glob(os.path.join(up, 'profile', '*', 'shader_map.tsv'))
            if maps: path = maps[0]; break
    out = {}
    if path and os.path.exists(path):
        for l in open(path, errors='replace'):
            c = l.rstrip('\n').split('\t')
            if len(c) >= 5 and not l.startswith('#'): out.setdefault(c[3], c[4])
    return out, path


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('dir'); ap.add_argument('--map'); ap.add_argument('--lines', type=int, default=12)
    a = ap.parse_args()
    lib = library(a.dir); src, mpath = names(lib, a.map)
    print(f'library: {lib}' + (f'\nnames: {mpath}' if mpath else '\nnames: no shader_map.tsv found (give --map)'))
    counts = dict(cs=0, ts=0, ms=0, no_msl=0); hits_exact, hits_near = [], []
    for jp in sorted(glob.glob(os.path.join(lib, '*_b.json'))):
        try: j = json.load(open(jp))
        except (OSError, ValueError): continue
        st = j.get('stage')
        if st not in ('cs', 'ts', 'ms'): continue
        counts[st] += 1
        key = os.path.basename(jp)[:-len('_b.json')]
        mp = jp[:-len('.json')] + '.metal'
        if not os.path.exists(mp): counts['no_msl'] += 1; continue
        msl = open(mp, errors='replace').read(); local = j.get('threadgroup_size')
        e = exact(msl, local) if st == 'cs' else None
        if e: hits_exact.append((key, st, local, e, atomics_on(msl, [n for n, _, _ in e]))); continue
        for m, stores in near(msl):
            lines = msl[:m.start()].split('\n')
            hits_near.append((key, st, local, stores, atomics_on(msl, [n for n, _, _ in stores]), '\n'.join(lines[-a.lines:]) + msl[m.start():m.end()]))
    print(f'compute shaders: {counts["cs"]}, task: {counts["ts"]}, mesh: {counts["ms"]}' + (f' ({counts["no_msl"]} without their .metal)' if counts['no_msl'] else ''))
    print(f'\nexact (nxnative\'s pattern): {len(hits_exact)}')
    for key, st, local, stores, at in hits_exact:
        print(f'  {key} {st} local {local} stores {stores}' + (f' APPEND COUNTER (atomics on {", ".join(at)})' if at else '') + f'  {src.get(key, "")}')
    print(f'\nnear (to read): {len(hits_near)}, of which append counters: {sum(1 for h in hits_near if h[4])}')
    for key, st, local, stores, at, text in hits_near:
        print(f'\n--- {key} {st} local {local} stores {stores}' + (f' APPEND COUNTER (atomics on {", ".join(at)})' if at else '') + f'  {src.get(key, "")}')
        print(text)


if __name__ == '__main__':
    main()
