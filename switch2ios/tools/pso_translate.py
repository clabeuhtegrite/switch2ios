#!/usr/bin/env python3
"""The fork's recorded pipeline list in this chain's shader keys, so the iPhone app warms up what the fork's sessions recorded (Grand Prix, free roam, menus).

Both chains key a Switch 2 shader by the same bytes, a BNSH variation's control section and code area as the game files hold them, hashed differently:
  the fork (nx2_split.py):                       sha256(the whole control section, 0xd8 bytes, + the code area)[:16]
  nxnative (shader_split.py skey and key_ctl):   sha256(the first 0x48 control bytes with +0x30..+0x33 as zero + the code area)[:16]
so walking the game's BNSH files as both splitters do gives each old key its new one. Shaders the game fills in at run time (the fork's map lists them as dump/<key>, from
DM_NVN_SHDUMP's <key>.ctl and <key>.code) are keyed from their saved bytes with nxnative's formula (--dumps), when those bytes still give the old key.
Run it with the Python the build runs with: a .zs file needs compression.zstd (3.14), backports.zstd or zstandard, and an NSO main dm/tools/nso.py's lz4 or zbic.

  switch2ios/tools/pso_translate.py --romfs <RomFS folder> [--main <main NSO or image>] [--dumps <folder>]... --old-map <the fork's shader_map.tsv>
                                    --new-map <dm/games/0400C3F00006E000/shader_map.tsv> [--first <a list recorded with this chain>] [--note TEXT] <the fork's pso_manifest.tsv> <out.tsv>

Rows: S <key>_b <stage>, C <key>_b, R <pso_skey in hex>. A pso_skey starts with its vertex and fragment shader keys as text, 24 bytes each; the rest is render state, laid out the
same in both chains (dm/gfx/nvn/nvn_pso_skey.h and nvn_obj.h: 440 bytes, or 432 for a row from before the alpha test field). A row is written only when each of its keys has a new
key that the new map lists. --first's rows come first, in their order, then the translated rows not already there: the order is first-use order, and nothing is sorted.
Checks printed: how many of the old map's game-file keys the walk gave again (if nearly all, the walk and the old formula are right; under 90 % it stops), how many of the new keys
the new map lists, and the rows kept and dropped by kind. Exit status 1 when a check fails.
"""
import argparse, collections, hashlib, os, re, struct, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]   # the repository (switch2ios/tools/ is two below it)
CTL_LEN, CTL_MAGIC, NVN2_KEY_CTL = 0xd8, bytes.fromhex('34127698'), 0x48
SKIP = ('.bntx', '.bphsh', '.bphsc')   # what nx2_split.py does not read for shaders


def zstd_decompress(b):
    for name in ('compression.zstd', 'backports.zstd'):
        try:
            mod = __import__(name, fromlist=['decompress'])
            return mod.decompress(b)
        except ImportError:
            continue
    try:
        import zstandard
    except ImportError:
        sys.exit('[RED] no zstd module for the .zs files: Python 3.14 has compression.zstd; on an older one, python3 -m pip install backports.zstd')
    return zstandard.ZstdDecompressor().stream_reader(b).read(1 << 31)   # as nx2_split.py read them


def old_key(ctl, code): return hashlib.sha256(ctl + code).hexdigest()[:16]


def new_key(ctl, code):
    c = ctl[:NVN2_KEY_CTL]
    return hashlib.sha256(c[:0x30] + bytes(4) + c[0x34:] + code).hexdigest()[:16]


def nvn2(ctl): return len(ctl) >= NVN2_KEY_CTL and ctl[:4] == CTL_MAGIC and ctl[4:8] == b'\xc9\0\0\0'


def add(ctl, code, keys, stat):   # the sections nx2_split.py keys: 0xd8 bytes, the code area's length at +0x44 (both hosts hash that many code bytes), c1's start at +0x40 inside it
    c1o, total = struct.unpack_from('<2I', ctl, 0x40) if len(ctl) == CTL_LEN else (0, 0)
    if len(ctl) != CTL_LEN or total != len(code) or c1o > len(code): stat['section nx2_split.py skips'] += 1; return
    if not nvn2(ctl): stat['not an NVN2 control section'] += 1; return
    o, n = old_key(ctl, code), new_key(ctl, code)
    if keys.setdefault(o, n) != n: stat['old key with two new keys'] += 1
    stat['shaders'] += 1


def bnsh_at(b, base, keys, stat):   # as nx2_split.py bnsh_at and shader_split.py bnsh_shaders walk a BNSH
    if b[base + 0x60:base + 0x64] != b'grsc': stat['BNSH without grsc'] += 1; return
    try:
        nvar, pvar = struct.unpack_from('<IQ', b, base + 0x7c)
        for v in range(nvar):
            prog = struct.unpack_from('<Q', b, base + pvar + 0x40 * v + 0x10)[0]
            if not prog: continue
            for si in range(6):   # vs hs ds gs ps cs
                sc = struct.unpack_from('<Q', b, base + prog + 8 + 8 * si)[0]
                if not sc: continue
                pctl, pcode, ncode, nctl = struct.unpack_from('<QQII', b, base + sc + 8)
                ctl, code = b[base + pctl:base + pctl + nctl], b[base + pcode:base + pcode + ncode]
                if len(ctl) != nctl or len(code) != ncode: stat['section past the end'] += 1; continue
                add(ctl, code, keys, stat)
    except struct.error:
        stat['BNSH table past the end'] += 1


def scan(b, keys, stat):
    at = 0
    while (at := b.find(b'BNSH\0\0\0\0', at)) >= 0:
        size = struct.unpack_from('<I', b, at + 0x1c)[0] if at + 0x20 <= len(b) else 0
        stat['BNSH'] += 1
        bnsh_at(b, at, keys, stat)
        at += max(size, 8)


def read_map(path):   # key -> source
    out = {}
    for line in open(path, encoding='utf-8'):
        c = line.rstrip('\n').split('\t')
        if len(c) > 4 and re.fullmatch(r'[0-9a-f]{16}', c[3]): out[c[3]] = c[4]
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--romfs', required=True); ap.add_argument('--main'); ap.add_argument('--dumps', action='append', default=[])
    ap.add_argument('--old-map', required=True); ap.add_argument('--new-map', required=True); ap.add_argument('--first'); ap.add_argument('--note')
    ap.add_argument('manifest'); ap.add_argument('out')
    a = ap.parse_args()

    keys, stat = {}, collections.Counter()
    for root, _, files in os.walk(a.romfs):
        for f in sorted(files):
            if f.startswith('.') or 'Nin_Ounce_NVN2' not in f or f.endswith('.json') or any(x in f for x in SKIP): continue
            b = open(os.path.join(root, f), 'rb').read()
            if f.endswith('.zs'): b = zstd_decompress(b)
            stat['files'] += 1
            scan(b, keys, stat)
    if a.main:
        b = open(a.main, 'rb').read()
        if b[:4] == b'NSO0':
            sys.path.insert(0, str(ROOT / 'dm/tools')); import nso
            b = bytes(nso.load(b, a.main).image)
        scan(b, keys, stat)
    for d in a.dumps:   # DM_NVN_SHDUMP's <old key>.ctl and .code: the old key is the name, the new one is hashed from the saved bytes
        for f in sorted(os.listdir(d)):
            name = f[:-4]
            if not f.endswith('.ctl') or not re.fullmatch(r'[0-9a-f]{16}', name) or not os.path.exists(os.path.join(d, name + '.code')): continue
            ctl = open(os.path.join(d, f), 'rb').read(CTL_LEN); code = open(os.path.join(d, name + '.code'), 'rb').read()
            if len(ctl) != CTL_LEN or not nvn2(ctl): stat['dump: not an NVN2 control section'] += 1; continue
            n = struct.unpack_from('<I', ctl, 0x44)[0]
            if len(code) < n: stat['dump: code area shorter than its control section says'] += 1; continue
            code = code[:n]
            # The fork's host keyed the shader when the game bound it, and the code area was saved later, at its first draw: the game may have rewritten it in between, and the
            # new key would then be the later bytes'. Only a dump whose bytes still give its name is the shader its key names
            if old_key(ctl, code) != name: stat['dump: bytes changed since the fork keyed them'] += 1; continue
            if keys.setdefault(name, new_key(ctl, code)) != new_key(ctl, code): stat['old key with two new keys'] += 1
            stat['dumps'] += 1
    print('walk:', dict(stat), f'{len(keys)} old keys')

    old_map, new_map = read_map(a.old_map), read_map(a.new_map)
    files_old = [k for k, s in old_map.items() if not s.startswith('dump/')]
    dumps_old = [k for k, s in old_map.items() if s.startswith('dump/')]
    got_files, got_dumps = sum(k in keys for k in files_old), sum(k in keys for k in dumps_old)
    new_known = sum(keys[k] in new_map for k in old_map if k in keys)
    print(f'old map: {got_files} of its {len(files_old)} game-file keys walked again, {got_dumps} of its {len(dumps_old)} run-time dumps given')
    print(f'new keys of the old map\'s shaders that the new map lists: {new_known} of {sum(k in keys for k in old_map)}')
    walked = set(keys.values())   # near 0 would mean the new formula is not the new chain's (the new map also lists shaders the fork never read, such as agl's BAHS files)
    print(f'new keys of every walked shader that the new map lists: {sum(n in new_map for n in walked)} of {len(walked)}; the new map has {len(new_map)}')
    ok = files_old and got_files >= 0.9 * len(files_old)
    if not ok: print('[RED] the walk gave back under 90 % of the old map\'s game-file keys: the folder, the walk or the old formula is not what the fork used; nothing written'); return 1

    def tr(k):   # '<16 hex><suffix>' -> the same with the new key, or None
        n = keys.get(k[:16])
        return n + k[16:] if n and n in new_map else None

    rows, why = [], collections.Counter()
    for line in open(a.manifest, encoding='utf-8'):
        line = line.rstrip('\n')
        if not line or line.startswith('#'): continue
        c = line.split('\t')
        if c[0] in ('S', 'C') and len(c) >= 2:
            n = tr(c[1])
            if not n: why[c[0] + ' dropped'] += 1; continue
            rows.append('\t'.join([c[0], n] + c[2:])); why[c[0] + ' kept'] += 1
        elif c[0] == 'R' and len(c) >= 2 and len(c[1]) in (864, 880) and re.fullmatch(r'[0-9a-fA-F]+', c[1]):
            b = bytearray.fromhex(c[1]); good = True
            for off in (0, 24):
                k = bytes(b[off:off + 24]).split(b'\0')[0].decode('ascii', 'replace')
                if not k: continue
                n = tr(k)
                if not n or len(n) >= 24: good = False; break
                b[off:off + 24] = n.encode() + bytes(24 - len(n))
            if not good: why['R dropped'] += 1; continue
            rows.append('R\t' + b.hex()); why['R kept'] += 1
        else:
            why['other dropped'] += 1
    print('rows:', dict(sorted(why.items())))

    out, seen = [], set()
    first = [l.rstrip('\n') for l in open(a.first, encoding='utf-8') if l.strip() and not l.startswith('#')] if a.first else []
    for line in first + rows:
        if line not in seen: seen.add(line); out.append(line)
    note = a.note or ('the switch2ios fork\'s list, recorded while playing Mario Kart World 1.0.0 (Grand Prix, free roam, menus), with its shader keys translated to this chain '
                      '(switch2ios/tools/pso_translate.py)' + (f', after {len(first)} rows recorded with this chain' if first else ''))
    with open(a.out, 'w', encoding='utf-8') as f:
        f.write('# Prewarm seed list (shipped in the app, next to shader_map.tsv). Rows: S<TAB>shader key<TAB>stage / R<TAB>render pipeline key (pso_skey hex) / C<TAB>compute shader key.\n')
        f.write(f'# {note}: identifiers and render state only, no game data. Row order = first-use order; do not sort.\n')
        for line in out: f.write(line + '\n')
    print(f'wrote {a.out}: {len(out)} rows ({len(first)} from --first, {len(out) - min(len(first), len(out))} more translated)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
