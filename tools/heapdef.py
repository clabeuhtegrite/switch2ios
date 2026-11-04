#!/usr/bin/env python3
"""Mario Kart World's heap definitions, read from your own dump, and copies with some heaps resized for a RomFS overlay.

The game's heap sizes are in System/HeapSize/<variant>.HeapDef.byml.zs (Default, Product, PilotME, PilotME24GB, each also as .Nin_Ounce_NVN2): a BYML dictionary
Heaps, heap name -> {Size: KB, Parent: heap name}, where a Size of -1 takes what its parent has left (version 1.0.0 reads Default.Nin_Ounce_NVN2). `set` writes copies with new sizes into an overlay folder, which the
host reads before the game's own RomFS when DM_ROMFS_OVERLAY names it (nxnative/dm/host/hle_fs.c). The copies are made from your dump and stay on your machine, in work/
(out of git), like everything else built from it. Nothing here prints more than heap names and sizes.

  tools/heapdef.py list [--romfs PATH] [--variant NAME]            each file's heaps as a tree: name, MiB, and the totals
  tools/heapdef.py set NAME=MiB ... [--romfs PATH] [--variant NAME] [--out DIR]
  tools/heapdef.py selftest                                          the BYML reader and writer on made-up files (no game data)

  --romfs    the RomFS: an image (default work/extract/RomFS.img) or an extracted folder
  --variant  one file, by its name before .HeapDef.byml.zs (default: every file in System/HeapSize)
  --out      the overlay folder (default work/overlay); the copies go to <out>/System/HeapSize/. Run with DM_ROMFS_OVERLAY=<the folder, absolute>
  NAME=MiB   a heap and its new size in MiB. A heap a file lacks is skipped in that file; a heap no file has stops the tool, as does a parent left smaller than its children
Each `set` starts again from the dump's sizes and replaces the copies a previous one wrote, so give it every heap to change at once. Without DM_ROMFS_OVERLAY the game
reads its own files again. The copies are zstd frames of raw blocks: no compression and no dictionary, which any zstd decoder reads the same way.
"""
import argparse, shlex, struct, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEAPDIR = 'System/HeapSize'
SUFFIX = '.HeapDef.byml.zs'


def zstd_decompress(data):
    try: from compression import zstd   # Python 3.14+
    except ImportError:
        try: from backports import zstd
        except ImportError: zstd = None
    try:
        if zstd: return zstd.decompress(data)
        try: import zstandard
        except ImportError: sys.exit('no zstd module: Python 3.14 or later (compression.zstd), or pip install backports.zstd')
        return zstandard.ZstdDecompressor().decompress(data, max_output_size=64 << 20)
    except Exception as err:   # each module has its own error class
        raise ValueError(f'cannot decompress it ({err}); a file compressed with a zstd dictionary needs it, and this tool loads none') from None


def zstd_raw(data):
    """One zstd frame holding data in raw blocks of at most 128 KB: a 4-byte content size, single segment, no checksum, no dictionary ID"""
    out = bytearray(struct.pack('<IBI', 0xFD2FB528, 0xA0, len(data)))   # descriptor 0xA0: content size field of 4 bytes, single segment
    at = 0
    while True:
        n = min(1 << 17, len(data) - at); last = at + n == len(data)
        out += (int(last) | 0 << 1 | n << 3).to_bytes(3, 'little') + data[at:at + n]   # block header: last flag, type 0 (raw), size
        at += n
        if last: return bytes(out)


class Byml:
    """The parts of BYML (version 2 and later, either byte order) a heap table needs: dictionaries, arrays, strings and numbers, read in place so a number can be
    changed without writing the file again"""
    def __init__(self, data):
        self.d = bytearray(data)
        magic = bytes(self.d[:2])
        if magic not in (b'YB', b'BY'): raise ValueError('not a BYML file')
        self.e = '<' if magic == b'YB' else '>'
        self.version = self.u16(2)
        self.keys = self.table(self.u32(4)); self.strs = self.table(self.u32(8))
        self.root = self.u32(0xC)

    def u16(self, o): return struct.unpack_from(self.e + 'H', self.d, o)[0]
    def u32(self, o): return struct.unpack_from(self.e + 'I', self.d, o)[0]
    def u24(self, o): return int.from_bytes(self.d[o:o + 3], 'little' if self.e == '<' else 'big')

    def table(self, o):
        if not o: return []
        if self.d[o] != 0xC2: raise ValueError(f'string table expected at {o:#x}, found node type {self.d[o]:#x}')
        return [bytes(self.d[o + s:self.d.index(0, o + s)]).decode('utf-8') for s in (self.u32(o + 4 + 4 * i) for i in range(self.u24(o + 1)))]

    def items(self, o):
        """A dictionary's entries as (key, type, offset of the 4-byte value field)"""
        if self.d[o] != 0xC1: raise ValueError(f'dictionary expected at {o:#x}, found node type {self.d[o]:#x}')
        return [(self.keys[self.u24(e)], self.d[e + 3], e + 4) for e in (o + 4 + 8 * i for i in range(self.u24(o + 1)))]

    def elements(self, o):
        """An array's elements as (type, offset of the 4-byte value field)"""
        if self.d[o] != 0xC0: raise ValueError(f'array expected at {o:#x}, found node type {self.d[o]:#x}')
        n = self.u24(o + 1); v = o + 4 + (n + 3) // 4 * 4
        return [(self.d[o + 4 + i], v + 4 * i) for i in range(n)]

    NUM = {0xD1: ('i', 4, 0), 0xD3: ('I', 4, 0), 0xD4: ('q', 8, 1), 0xD5: ('Q', 8, 1)}   # format, width, stored at an offset

    def number_at(self, t, field):
        """(where the number is, its struct format) for a number node whose value field is at field"""
        if t not in self.NUM: raise ValueError(f'number expected, found node type {t:#x}')
        f, _, indirect = self.NUM[t]
        return (self.u32(field) if indirect else field), f

    def value(self, t, field):
        if t == 0xA0: return self.strs[self.u32(field)]
        if t in self.NUM: at, f = self.number_at(t, field); return struct.unpack_from(self.e + f, self.d, at)[0]
        if t == 0xD0: return bool(self.u32(field))
        if t == 0xFF: return None
        return ('node', t, self.u32(field))   # containers and anything else: left as a reference

    def heaps(self):
        """[(name, size in KB, parent or '', (offset, format) of the size)] from the root dictionary's Heaps (a dictionary by name, or an array of dictionaries with a Name)"""
        top = {k: (t, f) for k, t, f in self.items(self.root)}
        if 'Heaps' not in top: raise ValueError(f'no Heaps in the root dictionary (keys: {", ".join(sorted(top))})')
        t, f = top['Heaps']
        if t == 0xC1: rows = [(k, tt, ff) for k, tt, ff in self.items(self.u32(f))]
        elif t == 0xC0: rows = [(None, tt, ff) for tt, ff in self.elements(self.u32(f))]
        else: raise ValueError(f'Heaps is node type {t:#x}, not a dictionary or an array')
        out = []
        for name, tt, ff in rows:
            if tt != 0xC1: raise ValueError(f'heap {name}: node type {tt:#x}, not a dictionary')
            h = {k: (t2, f2) for k, t2, f2 in self.items(self.u32(ff))}
            if name is None:
                if 'Name' not in h: raise ValueError('a heap in the Heaps array has no Name')
                name = self.value(*h['Name'])
            if 'Size' not in h: raise ValueError(f'heap {name} has no Size')
            where = self.number_at(*h['Size'])
            parent = self.value(*h['Parent']) if 'Parent' in h else ''
            out.append((name, self.value(*h['Size']), parent if isinstance(parent, str) else '', where))
        return out

    def set_size(self, where, kb):
        at, f = where
        struct.pack_into(self.e + f, self.d, at, kb)


def mib(kb): return 'rest' if kb < 0 else f'{kb / 1024:.1f}'


def show(name, heaps):
    kids = {}
    for h in heaps: kids.setdefault(h[2], []).append(h)
    top = [h for h in heaps if h[2] not in {x[0] for x in heaps}]
    print(f'{name}: {len(heaps)} heaps, top level {sum(h[1] for h in top if h[1] > 0) / 1024:.1f} MiB '
          f'(with the children of the "rest" heaps: {(sum(h[1] for h in top if h[1] > 0) + sum(k[1] for h in top if h[1] < 0 for k in kids.get(h[0], []) if k[1] > 0)) / 1024:.1f} MiB)')
    def walk(h, depth):
        print(f'  {"  " * depth}{h[0]:<{40 - 2 * depth}} {mib(h[1]):>9} MiB')
        for k in sorted(kids.get(h[0], []), key=lambda x: -x[1]): walk(k, depth + 1)
    for h in sorted(top, key=lambda x: -x[1]): walk(h, 0)


def sources(romfs, variant):
    """{file name: bytes} of the heap definitions, from a RomFS image or folder"""
    romfs = Path(romfs)
    if romfs.is_dir():
        files = {p.name: p.read_bytes() for p in sorted((romfs / HEAPDIR).glob('*' + SUFFIX))}
    else:
        sys.path.insert(0, str(ROOT / 'nxnative/dm/tools'))
        from romfs_image import read_index
        idx = read_index(romfs); files = {}
        with open(romfs, 'rb') as f:
            for path, (isdir, off, n) in sorted(idx.items()):
                if isdir or not path.startswith(HEAPDIR + '/') or not path.endswith(SUFFIX) or '/' in path[len(HEAPDIR) + 1:]: continue
                f.seek(off); files[path[len(HEAPDIR) + 1:]] = f.read(n)
    if variant: files = {k: v for k, v in files.items() if k == variant + SUFFIX}
    if not files: sys.exit(f'no {HEAPDIR}/{variant + SUFFIX if variant else "*" + SUFFIX} in {romfs}')
    return files


def patch(raw, sizes):
    """raw (a .byml.zs file) with the heaps in sizes ({name: KB}) resized: (new .zs bytes, [(name, old KB, new KB)]); checks that nothing else changed"""
    b = Byml(zstd_decompress(raw)); before = bytes(b.d); hs = b.heaps(); done = []
    for name, kb, parent, where in hs:
        if name in sizes:
            b.set_size(where, sizes[name]); done.append((name, kb, sizes[name]))
    after = {h[0]: h[1] for h in Byml(bytes(b.d)).heaps()}
    for name, kb, parent, where in hs:
        want = sizes.get(name, kb)
        if after[name] != want: raise RuntimeError(f'{name}: {after[name]} KB after the change, {want} expected')
    changed = sum(x != y for x, y in zip(before, b.d))
    if changed > 8 * len(done): raise RuntimeError(f'{changed} bytes changed for {len(done)} sizes')
    kids = {}
    for name, kb, parent, where in hs: kids.setdefault(parent, []).append(after[name])
    for name, kb, parent, where in hs:
        if after[name] > 0 and sum(k for k in kids.get(name, []) if k > 0) > after[name]:
            raise RuntimeError(f'{name} ({mib(after[name])} MiB) would be smaller than its children ({sum(k for k in kids[name] if k > 0) / 1024:.1f} MiB)')
    out = zstd_raw(bytes(b.d))
    if zstd_decompress(out) != bytes(b.d): raise RuntimeError('the zstd frame does not read back')
    return out, done


def cmd_list(a):
    bad = 0
    for name, raw in sources(a.romfs, a.variant).items():
        try: heaps = [h[:3] for h in Byml(zstd_decompress(raw)).heaps()]
        except ValueError as err: print(f'{name}: {err}\n'); bad += 1; continue
        show(name[:-len(SUFFIX)], heaps); print()
    if bad: sys.exit(f'{bad} files not read')


def cmd_set(a):
    sizes = {}
    for kv in a.sizes:
        if '=' not in kv: sys.exit(f'{kv}: expected NAME=MiB')
        k, v = kv.split('=', 1)
        try: m = float(v)
        except ValueError: sys.exit(f'{kv}: the size is a number of MiB')
        if m <= 0: sys.exit(f'{kv}: the size must be above 0 (a heap of -1 takes what its parent has left; the tool does not make one)')
        sizes[k] = round(m * 1024)
    out = Path(a.out) / HEAPDIR; made = {}
    for name, raw in sources(a.romfs, a.variant).items():   # every file checked before any is written
        try: data, done = patch(raw, sizes)
        except (ValueError, RuntimeError) as err: sys.exit(f'{name}: {err}')
        if done: made[name] = (data, done)
        else: print(f'{name}: none of these heaps, left out')
    missing = sorted(set(sizes) - {n for _, done in made.values() for n, _, _ in done})
    if missing: sys.exit(f'no file has {", ".join(missing)}; nothing written')
    old = sorted(out.glob('*' + SUFFIX)) if out.is_dir() else []
    for f in old: f.unlink()
    if old: print(f'{len(old)} copies of a previous run removed from {out}')
    out.mkdir(parents=True, exist_ok=True)
    for name, (data, done) in made.items():
        (out / name).write_bytes(data)
        print(f'{name}: ' + ', '.join(f'{n} {mib(o)} -> {mib(w)} MiB' for n, o, w in done) + f'  ({out / name})')
    print(f'\nrun with DM_ROMFS_OVERLAY={shlex.quote(str(Path(a.out).resolve()))}')


def build(root, e='<', version=7):
    """A BYML file from Python dictionaries, lists, strings and ints (for the self-test: the reader is checked against files whose content is known)"""
    keys, strs = set(), set()
    def scan(v):
        if isinstance(v, dict): keys.update(v); [scan(x) for x in v.values()]
        elif isinstance(v, list): [scan(x) for x in v]
        elif isinstance(v, str): strs.add(v)
    scan(root); keys, strs = sorted(keys), sorted(strs)
    out = bytearray(16)
    def u32(v): return struct.pack(e + 'I', v)
    def head(t, n): return bytes([t]) + n.to_bytes(3, 'little' if e == '<' else 'big')
    def align():
        while len(out) % 4: out.append(0)
    def table(lst):
        if not lst: return 0
        at = len(out); offs, blob = [], bytearray(); base = 4 + 4 * (len(lst) + 1)
        for s in lst: offs.append(base + len(blob)); blob += s.encode() + b'\0'
        offs.append(base + len(blob)); out.extend(head(0xC2, len(lst)) + b''.join(u32(o) for o in offs) + blob); align(); return at
    def node(v):
        """(type, value field) for v, writing containers out first"""
        if isinstance(v, bool): return 0xD0, int(v)
        if isinstance(v, int): return 0xD1, v & 0xFFFFFFFF
        if isinstance(v, str): return 0xA0, strs.index(v)
        if isinstance(v, dict):
            kids = [(keys.index(k), node(x)) for k, x in sorted(v.items())]
            at = len(out); out.extend(head(0xC1, len(kids)))
            for ki, (t, f) in kids: out.extend(ki.to_bytes(3, 'little' if e == '<' else 'big') + bytes([t]) + u32(f))
            return 0xC1, at
        if isinstance(v, list):
            kids = [node(x) for x in v]
            at = len(out); out.extend(head(0xC0, len(kids)) + bytes(t for t, _ in kids)); align()
            for t, f in kids: out.extend(u32(f))
            return 0xC0, at
        raise TypeError(type(v))
    kt = table(keys); st = table(strs); t, r = node(root)
    out[0:16] = (b'YB' if e == '<' else b'BY') + struct.pack(e + 'H', version) + u32(kt) + u32(st) + u32(r)
    return bytes(out)


def cmd_selftest(a):
    heaps = {'Graphics': {'Size': 3000 * 1024, 'Parent': ''}, 'Textures': {'Size': 2000 * 1024, 'Parent': 'Graphics'},
             'Rest': {'Size': -1, 'Parent': ''}, 'Resources': {'Size': 400 * 1024, 'Parent': 'Rest'}, 'Sound': {'Size': 100 * 1024}}
    for e in '<>':
        for shape in ('dict', 'array'):
            root = {'Heaps': heaps if shape == 'dict' else [dict(v, Name=k) for k, v in heaps.items()]}
            raw = zstd_raw(build(root, e))
            got = {n: (kb, p) for n, kb, p, _ in Byml(zstd_decompress(raw)).heaps()}
            assert got == {k: (v['Size'], v.get('Parent', '')) for k, v in heaps.items()}, (e, shape, got)
            new, done = patch(raw, {'Textures': 1500 * 1024})
            got2 = {n: kb for n, kb, _, _ in Byml(zstd_decompress(new)).heaps()}
            assert got2 == dict({k: v['Size'] for k, v in heaps.items()}, Textures=1500 * 1024), got2
            assert done == [('Textures', 2000 * 1024, 1500 * 1024)], done
            try: patch(raw, {'Graphics': 1000 * 1024}); raise AssertionError('a parent smaller than its child was accepted')
            except RuntimeError: pass
    big = bytes(range(256)) * 2000   # several raw blocks
    assert zstd_decompress(zstd_raw(big)) == big
    print('selftest: ok (BYML little- and big-endian, heaps as a dictionary and as an array, sizes changed in place, raw zstd frames)')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    for n in ('list', 'set'):
        p = sub.add_parser(n); p.add_argument('--romfs', default=str(ROOT / 'work/extract/RomFS.img')); p.add_argument('--variant')
    sub.choices['set'].add_argument('--out', default=str(ROOT / 'work/overlay')); sub.choices['set'].add_argument('sizes', nargs='+')
    sub.add_parser('selftest')
    a = ap.parse_args()
    {'list': cmd_list, 'set': cmd_set, 'selftest': cmd_selftest}[a.cmd](a)


if __name__ == '__main__':
    main()
