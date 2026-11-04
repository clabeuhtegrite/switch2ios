#!/usr/bin/env python3
"""switch2mac: build a native Mac app of a Switch 2 game from your own dump.

  ./switch2mac.py setup                       Python packages into .venv (once)
  ./switch2mac.py build <dump folder>         the whole build: game files, code, shaders, host, app
  ./switch2mac.py refresh                     take in the shaders the app saved while you played, and pack the app again
  ./switch2mac.py install                     move the finished app to /Applications (replacing the old one)
  ./switch2mac.py run [VAR=value ...]         run the build straight from ./work with a log in work/logs (for porting and debugging: docs/porting.md)
  ./switch2mac.py run --phone [VAR=value ...] the same with the iPhone app's switches and memory statistics: try the phone's settings on the Mac first (docs/ios.md)
  ./switch2mac.py ios [--xcode] [--romfs]     the iPhone app (an unsigned IPA for LiveContainer, or an Xcode project to sign and run): after build (docs/ios.md);
                                              --romfs packs the game files into it (<name>-full.ipa), else they are copied into the app's Documents

The dump folder holds exefs/ (main, main.npdm, sdk, ...) and romfs/, or base/exefs and base/romfs.
Everything the build writes goes to ./work (--work names another folder). Nothing of the game is in this repository.
Each supported game has a folder in games/ (game.json names it); its profile is nxnative/dm/games/<title ID>.
"""
import argparse, datetime, hashlib, json, os, re, shutil, struct, subprocess, sys, tempfile, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NX = ROOT / 'nxnative'
VENV = ROOT / '.venv'
IOS_CACHE = 'cache/ios_shader_cache'   # the iOS Metal libraries, beside the Mac's (work/cache/metal_shader_cache)
GAMES = {g['title_id']: dict(g, dir=p.parent) for p in sorted((ROOT / 'games').glob('*/game.json')) for g in [json.loads(p.read_text())]}
SKIP = {'.listing', '.DS_Store'}   # a dump tool's FTP listings and Finder's files: never game files


def say(msg): print(f'\n== {msg}', flush=True)


def run(cmd, env=None, cwd=None):
    print('$', ' '.join(str(c) for c in cmd), flush=True)
    r = subprocess.run([str(c) for c in cmd], env=env, cwd=cwd)
    if r.returncode: sys.exit(f'\nstopped: {Path(str(cmd[0])).name} failed ({r.returncode})')


def py():
    p = VENV / 'bin/python'
    if not p.exists(): sys.exit('run ./switch2mac.py setup first')
    return p


def env():
    e = dict(os.environ, PATH=f'{VENV / "bin"}:{os.environ["PATH"]}', DMSH_BORDER_EMU='0', DM_ALLOW_OTHER_METAL='1')
    if 'DOTNET_ROOT' not in e:
        for d in ('/usr/local/share/dotnet', '/opt/homebrew/opt/dotnet/libexec', str(Path.home() / '.dotnet')):
            if Path(d, 'dotnet').exists(): e['DOTNET_ROOT'] = d; break
    return e


def macos():   # this Mac's macOS as major.minor (e.g. 26.5): the app is built for the Mac that builds it, like its Metal shaders
    v = subprocess.run(['sw_vers', '-productVersion'], capture_output=True, text=True).stdout.strip().split('.')
    return f'{v[0]}.{v[1] if len(v) > 1 else 0}'


def setup(a):
    say('Python packages (.venv)')
    if not VENV.exists(): run([sys.executable, '-m', 'venv', VENV])
    run([py(), '-m', 'pip', 'install', '-q', '-r', ROOT / 'requirements.txt'])
    say('checks')
    for tool, why in (('xcrun', 'Xcode'), ('clang++', 'Xcode command line tools')):
        print(f'  {tool}: {"ok" if shutil.which(tool) else "MISSING (" + why + ")"}')
    r = subprocess.run(['xcrun', '-sdk', 'macosx', 'metal', '--version'], capture_output=True, text=True)
    print(f'  Metal compiler: {"ok" if r.returncode == 0 else "MISSING (Xcode → Settings → Components → Metal Toolchain)"}')
    print(f'  .NET: {"ok (" + env()["DOTNET_ROOT"] + ")" if "DOTNET_ROOT" in env() else "MISSING (.NET 10 SDK from dot.net)"}')
    v = macos()
    print(f'  macOS {v}: ' + ('ok (the app is built for this version and later)' if int(v.split('.')[0]) >= 26 else 'TOO OLD (macOS 26 or later is needed: the runtime draws with Metal 4)'))
    print('  for the iPhone app only (./switch2mac.py ios):')
    for line in ios_missing() or ['ok']: print(f'    {line}')


def ios_missing():   # what the iPhone app's build needs that this Mac lacks, one line each
    out = [f'{tool}: MISSING ({why})' for tool, why in (('xcodegen', 'brew install xcodegen'), ('xcodebuild', 'Xcode')) if not shutil.which(tool)]
    r = subprocess.run(['xcrun', '-sdk', 'iphoneos', 'metal', '--version'], capture_output=True, text=True) if shutil.which('xcrun') else None
    if not r or r.returncode: out.append('iOS Metal compiler: MISSING (Xcode → Settings → Components: the iOS platform and the Metal Toolchain)')
    return out


def dump_dirs(d):
    d = Path(d).expanduser().resolve()
    for base in (d, d / 'base'):
        if (base / 'exefs/main').is_file() and (base / 'romfs').is_dir(): return base / 'exefs', base / 'romfs'
    sys.exit(f'no exefs/main and romfs/ in {d} (or in {d}/base)')


def title_and_build(exefs):
    npdm = (exefs / 'main.npdm').read_bytes()
    aci = struct.unpack_from('<I', npdm, 0x70)[0]   # ACI0 offset in the META header
    tid = f'{struct.unpack_from("<Q", npdm, aci + 0x10)[0]:016X}'
    sys.path.insert(0, str(NX / 'dm/tools')); import nso
    return tid, nso.read(exefs / 'main').build_id[:16].hex()


def check_version(tid, bid):
    if tid not in GAMES: sys.exit(f'title {tid} is not supported yet (this repository has: {", ".join(g["name"] + " " + t for t, g in GAMES.items())}).\n'
                                  'Adding a game: docs/porting.md')
    rows = [l.split('\t') for l in (NX / f'dm/games/{tid}/game.tsv').read_text().splitlines() if l.startswith('build\t')]
    known = {c.split('=', 1)[1]: r[2] for r in rows for c in r[3:] if c.startswith('main=')}
    if bid in known:
        print(f'  {GAMES[tid]["name"]} {known[bid]} (main {bid}): tested')
        return
    sys.exit(f'main build ID {bid} is not a version this profile knows ({", ".join(known.values())}).\n'
             f'This version is UNTESTED: its build line (game.tsv) and module list must be added first (see {GAMES[tid]["dir"].relative_to(ROOT)}/README.md, "Other versions").')


def stage(romfs, out):   # a symlink tree of the RomFS without the dump tool's dot-files, so they never end up in the image
    if out.exists(): shutil.rmtree(out)
    n = 0
    for root, _, files in os.walk(romfs):
        rel = Path(root).relative_to(romfs); (out / rel).mkdir(parents=True, exist_ok=True)
        for f in files:
            if f in SKIP: continue
            os.symlink(Path(root) / f, out / rel / f); n += 1
    print(f'  {n:,} game files linked')


def paths(a):
    w = Path(a.work).resolve()
    return w, w / 'extract', w / 'build', w / 'shader', w / 'cache/metal_shader_cache'


def profile(w, tid):   # a fresh copy of the repo's profile in work/profile: the shader split rewrites its tables, the repo stays as it is
    prof = w / 'profile' / tid
    if prof.exists(): shutil.rmtree(prof)
    shutil.copytree(NX / f'dm/games/{tid}', prof)
    return prof


def runtime_shaders(tid, w):
    """Shaders the game assembles while it runs are not in its usual shader containers, but their parts are in its files. games/<game>/runtime_shaders.tsv says where each
    one's control section and code are (file + offset, as positions only: no game data), and its key, sha256(control + code), checks what is read. They go to work/runtime
    as <key>.ctl / <key>.code, the way the app saves shaders it meets (refresh), so the first launch already has them."""
    import zstandard
    table = GAMES[tid]['dir'] / 'runtime_shaders.tsv'
    out = w / 'runtime'; out.mkdir(exist_ok=True)
    if not table.exists(): return None
    rows = [l.rstrip('\n').split('\t') for l in table.open() if l.strip() and not l.startswith('#')]
    blobs, bad = {}, 0
    def blob(name):
        if name not in blobs:
            if name == 'main': blobs[name] = (w / 'shader/main.img').read_bytes()
            else:
                b = (w / 'input/romfs' / name).read_bytes()
                blobs[name] = zstandard.ZstdDecompressor().stream_reader(b).read(1 << 31) if name.endswith('.zs') else b
        return blobs[name]
    for key, cf, co, kf, ko, n in rows:
        ctl = blob(cf)[int(co, 16):int(co, 16) + 0xd8]; code = blob(kf)[int(ko, 16):int(ko, 16) + int(n)]
        if hashlib.sha256(ctl + code).hexdigest()[:16] != key: bad += 1; continue
        (out / f'{key}.ctl').write_bytes(ctl); (out / f'{key}.code').write_bytes(code)
    print(f'  {len(rows) - bad} of {len(rows)} run-time shaders taken from the dump' + (f' ({bad} did not match: another game version?)' if bad else ''))
    return out


def shaders(a, tid, w, sh, cache, e, ios=None):   # ios: a folder for the iOS Metal libraries as well (IOSOUT; the Mac's then come from the compile cache)
    prof = profile(w, tid); tools = NX / 'dm/gfx/tools'
    e.update(DM_GAMES_DIR=str(w / 'profile'), DM_GAMES_FIRST='1')   # every tool (and the host build) takes the work copy
    lower = tools / 'nx2_lower/nx2_lower'
    if not lower.exists():
        say('the shader lowering tool (nx2_lower)')
        run(['clang++', '-std=c++20', '-O2', '-w', '-Wno-c++11-narrowing', '-I.', 'nx2_lower.cpp', '-o', 'nx2_lower'], cwd=tools / 'nx2_lower')
    e = dict(e, DM_GAME=tid)
    run([py(), NX / 'dm/tools/nso.py', w / 'extract/ExeFS/main', sh / 'main.img'], env=e)
    say('shaders: split (with the run-time shaders listed for this game' + (', and those the app saved while you played)' if (w / 'captures').is_dir() and any((w / 'captures').iterdir()) else ')'))
    rt = runtime_shaders(tid, w)
    caps = [str(p) for p in (rt, w / 'captures') if p and p.is_dir() and any(p.iterdir())]
    e = dict(e, NX2_DUMPS=':'.join(caps))
    run([py(), tools / 'nx2_split.py', w / 'input/romfs', sh / 'main.img', sh / 'pkg', prof / 'shader_map.tsv', prof / 'shader_facts.tsv', prof / 'shader_pairs.tsv', sh / 'rejects.tsv'], env=e)
    say('shaders: Metal libraries (the long step: 10-20 minutes the first time, all cores' + ('; the iOS ones take about as long again the first time)' if ios else ')'))
    run([py(), tools / 'own_compilers.py', sh / 'pkg', cache], env=dict(e, IOSOUT=str(ios)) if ios else e)
    chain = re.search(r'"([0-9a-f]{12})"', (NX / 'dm/gfx/nvn/dmsh_chain.h').read_text())[1]   # the translator chain version names the library folder
    lib = cache / f'b-{chain}'
    run([py(), tools / 'nx2_res.py', prof / 'shader_map.tsv', sh / 'pkg', sh / 'pkg/cs_layout.tsv', sh / 'res'], env=e)
    for d in [lib] + ([Path(ios) / f'b-{chain}'] if ios else []):   # the NVN2 compute bindings go into each platform's reflection
        run([py(), tools / 'nx2_reflect.py', d, sh / 'pkg/cs_layout.tsv', sh / 'res'], env=e)
    return lib


def host_and_app(a, tid, w, lib, e):
    g = GAMES[tid]; prof = w / 'profile' / tid
    say('host (the native runtime)')
    run(['make', '-C', NX / 'dm/host', f'GAME={tid}', f'MACOS_MIN={macos()}', 'dmhost'], env=e)   # for this Mac's macOS, not nxnative's fixed 26.6
    font = w / 'build/font'
    if not (font / 'nintendo_ext.ttf').exists():
        font.mkdir(parents=True, exist_ok=True); run([py(), NX / 'dm/tools/make_ext_font.py', font / 'nintendo_ext.ttf'], env=e)
    say('app')
    run([py(), g['dir'] / 'launcher.py', NX, w / 'build/patched', w / 'build/launcher.json'], env=e)
    app = w / f'{g["name"]}.app'
    run([py(), NX / 'dm/mac/pack_app.py', app, '--host', NX / 'dm/host/dmhost', '--patched', w / 'build/patched', '--romfs', w / 'extract/RomFS.img',
         '--msl', lib, '--tables', prof, '--font', font, '--env-file', g['dir'] / 'env.txt', '--settings', w / 'build/launcher.json', '--launcher-window', 'off',
         '--title-id', tid, '--name', g['name'], '--version', g['version'], '--lang', 'en-US', '--marker', g['marker']], env=e)
    (w / 'title.txt').write_text(tid)
    print(f'\ndone: {app}\nnext: ./switch2mac.py install')


def build(a):
    e = env(); w, ex, b, sh, cache = paths(a)
    if 'DOTNET_ROOT' not in e: sys.exit('.NET is missing: run ./switch2mac.py setup for the checks')
    exefs, romfs = dump_dirs(a.dump)
    say('the dump'); tid, bid = title_and_build(exefs); check_version(tid, bid)
    for d in (ex / 'ExeFS', b, sh): d.mkdir(parents=True, exist_ok=True)
    say('game files: code modules and the RomFS image (a few minutes; reused when unchanged)')
    for f in exefs.iterdir():
        if f.is_file() and f.name not in SKIP: run(['cp', '-c', f, ex / 'ExeFS' / f.name])
    stage(romfs, w / 'input/romfs')
    run([py(), NX / 'dm/tools/romfs_image.py', '--reuse', w / 'input/romfs', ex / 'RomFS.img'], env=e)
    say('code: rewrite the game modules for the Mac')
    mods = subprocess.run([py(), NX / 'dm/games/game.py', tid, 'modules'], capture_output=True, text=True, check=True).stdout.split()
    for m in mods: run([py(), NX / 'dm/tools/patch_nso.py', ex / 'ExeFS' / m, b / 'patched', m], env=e)
    lib = shaders(a, tid, w, sh, cache, e)
    host_and_app(a, tid, w, lib, e)


def refresh(a):
    e = env(); w, ex, b, sh, cache = paths(a)
    tf = w / 'title.txt'
    if not tf.exists(): sys.exit('no finished build in ' + str(w) + ': run build first')
    tid = tf.read_text().strip()
    caps = w / 'captures'; caps.mkdir(exist_ok=True); new = []
    for saved in (Path.home() / f'Library/Application Support/nxnative/{tid}/shdump', w / 'run/shdump'):   # the installed app's captures, and run's
        got = [p for p in saved.glob('*') if p.is_file() and not (caps / p.name).exists()] if saved.is_dir() else []
        for p in got: shutil.copy2(p, caps / p.name)
        print(f'{len(got)} new files from {saved}'); new += got
    print(f'{sum(1 for _ in caps.glob("*.ctl"))} shaders captured in all')
    if not new and not a.force: sys.exit('nothing new to build in (play the game a while first, or --force)')
    lib = shaders(a, tid, w, sh, cache, e)
    host_and_app(a, tid, w, lib, e)


def ios(a):
    """The iPhone app from the finished build: the same rewritten modules and RomFS image, the shaders again with Metal libraries for iOS as well, then nxnative's build_ipa.sh:
    an unsigned IPA (work/<name>.ipa) for LiveContainer, or with --xcode a project to sign with your own team and run from Xcode (logs and a debugger on the phone). The game files
    stay out of the app: copy work/extract/RomFS.img into the app's Documents folder as romfs.img (docs/ios.md). With --romfs they go into the app instead (work/<name>-full.ipa,
    the size of the RomFS image more: one file to install, but every new build means installing all of it again). Nothing here leaves this Mac."""
    e = env(); w, ex, b, sh, cache = paths(a)
    tf = w / 'title.txt'
    if not tf.exists(): sys.exit('no finished build: run build first')
    missing = ios_missing()
    if missing: sys.exit('the iPhone app needs:\n  ' + '\n  '.join(missing))
    tid = tf.read_text().strip(); g = GAMES[tid]; ios_lib = w / IOS_CACHE
    shaders(a, tid, w, sh, cache, e, ios=ios_lib)
    say('launcher settings for the phone')
    run([py(), g['dir'] / 'launcher.py', NX, b / 'patched', b / 'launcher_ios.json', 'ios'], env=dict(e, DM_SHOW_FPS='YES'))
    ipa = w / subprocess.run([py(), NX / 'dm/games/game.py', tid, 'ipa'], capture_output=True, text=True, check=True, env=e).stdout.strip()
    if a.romfs: ipa = ipa.with_name(f'{ipa.stem}-full{ipa.suffix}')   # beside the slim one, which stays the one to rebuild while the image sits in Documents
    ie = dict(e, GAME=tid, W=str(w), PATCHED=str(b / 'patched'), ROMFS=str(ex / 'RomFS.img'), MSL_IOS=str(ios_lib), AOC='',
              DM_DISPLAY_NAME=g['name'], DM_APP_VERSION=g['version'], DM_SHOW_FPS='1', DM_LAUNCHER_JSON=str(b / 'launcher_ios.json'))
    if not a.romfs: ie['DM_COPY_ROMFS'] = 'NO'   # the app then carries the image's identity only (romfs.id) and takes Documents/romfs.img; with --romfs, build_ipa.sh packs <app>/romfs.img
    envf = g['dir'] / 'env_ios.txt'
    if envf.exists(): ie['DM_ENV_FILE'] = str(envf)   # the phone's switches, as dm/env.txt in the app; Documents/env.txt on the phone overrides each of them
    say('iPhone app: Xcode project' if a.xcode else 'iPhone app: xcodebuild, then the IPA (several minutes)')
    run(['bash', NX / 'dm/ios/tools/build_ipa.sh', '--prepare-xcode' if a.xcode else ipa], env=ie)
    if a.xcode: print(f'\ndone: {w / "xcode"}/DmIOS.xcodeproj (Signing & Capabilities: your team; then Run on the phone, Release)')
    elif a.romfs: print(f'\ndone: {ipa}, the game files inside\nnext: install it with LiveContainer, with room for it twice on the phone while it installs (docs/ios.md)')
    else: print(f'\ndone: {ipa}\nnext: install it with LiveContainer, then copy {ex / "RomFS.img"} into the app\'s Documents folder as romfs.img (docs/ios.md)')


def install(a):
    w = paths(a)[0]; tf = w / 'title.txt'
    if not tf.exists(): sys.exit('no finished build: run build first')
    g = GAMES[tf.read_text().strip()]; app = w / f'{g["name"]}.app'; dst = Path('/Applications') / app.name
    if subprocess.run(['pgrep', '-f', f'{dst}/Contents/MacOS'], capture_output=True).returncode == 0: sys.exit(f'{g["name"]} is running: quit it first')
    if dst.exists(): shutil.rmtree(dst)
    shutil.move(str(app), dst); Path(str(app) + '.key').unlink(missing_ok=True)
    print(f'installed: {dst}\nThe first start after each install waits 1-3 minutes while macOS scans the app (no window, no Dock icon yet): wait, do not open it twice.')


GB = 1 << 30   # as the host's [mem] lines count it (hle_svc.c)
DISK_START, DISK_STOP = 25 * GB, 5 * GB   # run's disk guard: the room on the disk of the guest file's folder that a run needs to start, and the room under which the game is stopped
DISK_POLL, DISK_EVERY, DISK_AFTER, DISK_SAMPLE = 2, 30, 30, 10   # seconds: the guard's checks, its log lines, its last line after the game has exited; the disk sampler's rows


def gb(n): return '?' if n is None else f'{n / GB:.1f}'


def host_temp():   # the host's folder for the guest file without DM_GUEST_FILE_DIR: on macOS confstr(_CS_DARWIN_USER_TEMP_DIR), which ignores TMPDIR (hle_svc.c guest_file_init)
    if sys.platform == 'darwin':
        try: return os.confstr(65537) or tempfile.gettempdir()   # _CS_DARWIN_USER_TEMP_DIR
        except (OSError, ValueError): pass
    return tempfile.gettempdir()


def disk_free(d):   # bytes free to this user on the disk of folder d (statvfs f_bavail, as the host's guest file lines read it), or None when it cannot be read
    try: return shutil.disk_usage(d).free
    except OSError: return None


def disk_guard(p, d, free0, t0, done, stopped, out):
    """run's disk guard while the game runs (on with DM_GUEST_FILE, DM_DISK_GUARD=1 or 0 forces it): the room left on the disk of the guest file's folder every 2 s and in the log every 30 s, and under 5 GB the game
    is stopped before the disk fills, with SIGTERM (the host's graceful exit), then SIGKILL 5 s later. With DM_GUEST_FILE the kernel writes the guest's pages out to a file in
    that folder, and macOS swaps to the same disk when memory runs short; what either does on a full disk was not tried. The signals do not wait on the lines, since writing one waits for the
    terminal, which may have stopped reading: SIGTERM goes before the [RED] line, and SIGKILL comes from a thread of its own (the host logs before it arms its own 1.5 s exit, and that log line
    waits on this process reading its output)."""
    n, unread, every = 0, False, max(1, round(DISK_EVERY / DISK_POLL))
    try:
        while not done.wait(DISK_POLL):
            free = disk_free(d); n += 1; t = time.monotonic() - t0
            if free is None:
                if not unread: out(f'[switch2mac] [RED] [disk] cannot read the free space on the disk of {d} at {t:.0f} s: until it can, nothing stops the game if the disk fills\n')
                unread = True; continue
            unread = False
            if free < DISK_STOP:
                stopped.set(); p.terminate()
                def kill():
                    if not done.wait(5) and p.poll() is None: p.kill(); out('[switch2mac] [RED] [disk] the game still ran 5 s after SIGTERM: killed (SIGKILL)\n')
                threading.Thread(target=kill, daemon=True).start()
                out(f'[switch2mac] [RED] [disk] {gb(free)} GB free at {t:.0f} s, under {DISK_STOP // GB}: the game is stopped before the disk fills (SIGTERM, then SIGKILL after 5 s)\n')
                return
            if n % every == 0: out(f'[switch2mac] [disk] {gb(free)} GB free at {t:.0f} s ({round((free - free0) / GB, 1) + 0:+.1f} since the start)\n')   # + 0: no -0.0
    except Exception as x: sys.stderr.write(f'[switch2mac] [RED] [disk] the disk guard stopped ({x!r}): nothing stops the game if the disk fills\n')


def ioreg_disks(text):
    """The disks in ioreg -r -c IOBlockStorageDriver -k Statistics: [name, bytes written, bytes read] for each block storage driver (a subclass too, as an optical drive's), the name being
    that of the media below it (APPLE SSD AP0512Z Media); None where the text lacks it: a drive without media has no name, and a key missing from its Statistics stays None."""
    disks = []
    for line in text.splitlines():
        o = re.search(r'\+-o (.+?)\s+<class (\w+)', line)
        if o and o[2].endswith('BlockStorageDriver'): disks.append([None, None, None])
        elif not disks: continue
        elif o:
            if disks[-1][0] is None and o[2].endswith('Media'): disks[-1][0] = o[1]   # the first media below the driver: the whole disk (its partitions come after it)
        elif '"Statistics"' in line:
            for i, k in ((1, 'Write'), (2, 'Read')):
                m = re.search(r'"Bytes \(' + k + r'\)"\s*=\s*(\d+)', line)
                if m: disks[-1][i] = int(m[1])
    return disks


def internal_disk(disks, name=None):
    """Among ioreg_disks: the disk named name (the one the first sample chose), else the first APPLE SSD (a Mac's own), else the first with media and statistics, else the first with statistics.
    None when there is none: ioreg did not answer, or the disk named is gone."""
    if name: return next((x for x in disks if x[0] == name), None)
    for ok in (lambda x: (x[0] or '').startswith('APPLE SSD'), lambda x: x[0] and x[1] is not None, lambda x: x[1] is not None or x[2] is not None):
        d = next((x for x in disks if ok(x)), None)
        if d: return d
    return None


def vm_counts(text):   # vm_stat's counts by name (Pageins, Swapouts, ...), and its page size in bytes, None when its first line does not give it
    n = {m[1]: int(m[2]) for m in re.finditer(r'^"?([^":\n]+?)"?:\s+(\d+)\.?\s*$', text, re.M)}
    ps = re.search(r'page size of (\d+) bytes', text)
    return n, int(ps[1]) if ps else None


def disk_sampler(path, d, t0, done):
    """run's disk statistics (work/logs/disk-<time>.tsv; on with --phone, DM_DISK_SAMPLER=1 or 0 forces it): every 10 s, and once more when the game has exited, what the internal disk has
    written and read in all (ioreg's block storage statistics), vm_stat's page-ins, page-outs, swap-ins and swap-outs, and the room left on the disk of the guest file's folder: what the
    system writes and pages for DM_GUEST_FILE (docs/ios.md). It fails soft: a value a tool does not give stays empty, a tool gets 10 s, and an error ends the sampler with a line saying so,
    never the game."""
    def tool(*cmd):
        try: return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError): return ''
    try:
        with open(path, 'w') as f:
            name, rows, nxt = None, 0, t0
            while True:
                t = time.monotonic() - t0
                disk = internal_disk(ioreg_disks(tool('ioreg', '-r', '-c', 'IOBlockStorageDriver', '-k', 'Statistics', '-w', '0')), name)   # -w 0: lines never cut at a width
                vm, page = vm_counts(tool('vm_stat')); free = disk_free(d)
                if not rows:   # the disk the first sample chose stays the one of every row, so that its counters are one disk's
                    name = disk and disk[0]
                    f.write(f'# disk_written, disk_read: bytes the internal disk ({(name or "no media name") if disk else "none found at the start"}) has written and read since the Mac started (ioreg, IOBlockStorageDriver '
                            f"Statistics); pageins, pageouts, swapins, swapouts: vm_stat's counts since the Mac started, in pages of {page or '?'} bytes; free: bytes free on the disk of {d}; "
                            'an empty field: the tool did not give it\nseconds\tdisk_written\tdisk_read\tpageins\tpageouts\tswapins\tswapouts\tfree\n')
                vals = [disk and disk[1], disk and disk[2]] + [vm.get(k) for k in ('Pageins', 'Pageouts', 'Swapins', 'Swapouts')] + [free]
                f.write('\t'.join([f'{t:.0f}'] + ['' if v is None else str(v) for v in vals]) + '\n'); f.flush(); rows += 1
                if done.is_set(): return
                nxt += DISK_SAMPLE; done.wait(max(0, nxt - time.monotonic()))
    except Exception as x: sys.stderr.write(f'[switch2mac] [RED] [disk] the disk sampler stopped ({x!r}): the game goes on\n')


def run_game(a):
    """The build straight from ./work, without packing an app: the way to try a new game or a fix. Output goes to the terminal and work/logs/run-<time>.log;
    VAR=value arguments add host switches (docs/porting.md lists the useful ones). work/run/ctl.txt is the host's control file (DM_CTL): write a command
    line into it while the game runs (shot, passdump, texraw, memdump, ...). Shaders the game meets that the build lacks are saved to work/run/shdump.
    When the run uses the guest file (DM_GUEST_FILE > 0, as run --phone does; DM_DISK_GUARD=1 or 0 forces it on or off), the disk guard (disk_guard) watches the disk of the guest file's
    folder (DM_GUEST_FILE_DIR, relative to work/run, else the host's temporary folder, host_temp): with under 25 GB free (as statvfs counts it) the game does not start, the log gets the
    free space every 30 s and once more 30 s after the game has exited, and under 5 GB the game is stopped. With --phone, work/logs/disk-<time>.tsv gets the
    internal disk's writes and the system's paging every 10 s (disk_sampler; DM_DISK_SAMPLER=1 or 0 forces it)."""
    w = paths(a)[0]; tf = w / 'title.txt'
    if not tf.exists(): sys.exit('no finished build: run build first')
    tid = tf.read_text().strip(); g = GAMES[tid]; r = w / 'run'
    for d in ('save', 'temp', 'shdump'): (r / d).mkdir(parents=True, exist_ok=True)
    (r / 'ctl.txt').write_text(''); (w / 'logs').mkdir(exist_ok=True)
    stamp = f'{datetime.datetime.now():%Y%m%d-%H%M%S}'
    e = dict(os.environ, DM_ROOT=str(w), DM_ROMFS=str(w / 'extract/RomFS.img'), DM_SAVE=str(r / 'save'), DM_TEMP=str(r / 'temp'), DM_NVN_SHDUMP=str(r / 'shdump'),
             DM_MSL_DIR=str(w / 'cache/metal_shader_cache'), DM_FONT_DIR=str(w / 'build/font'), DM_NVN_SHMAP=str(w / 'profile' / tid / 'shader_map.tsv'),
             DM_GAMES_DIR=str(w / 'profile'), DM_GAMES_FIRST='1', DM_LANG='en-US', DM_WINDOW='1', DM_AUDIO='1', DM_CTL=str(r / 'ctl.txt'))
    if a.phone or a.memstat:   # memory statistics every second (memstat.m) and the address regions every 2 s (hle_svc.c), beside the log
        e.update(DM_MEMSTAT=str(w / 'logs' / f'memstat-{stamp}.tsv'), DM_REGIONS=str(w / 'logs' / f'regions-{stamp}.tsv'))
    def switches(envf):
        for l in (envf.read_text().splitlines() if envf.exists() else []):
            if '=' in l and not l.lstrip().startswith('#'): k, v = l.split('=', 1); e.setdefault(k.strip(), v.strip())
    if a.phone:   # the iPhone app's switches (env_ios.txt), then its launcher's defaults (handheld, pipelines through the MTLDevice, ...): they win over the Mac app's env.txt
        switches(g['dir'] / 'env_ios.txt')
        lj = w / 'build/launcher_ios.json'
        run([py(), g['dir'] / 'launcher.py', NX, w / 'build/patched', lj, 'ios'])
        for row in json.loads(lj.read_text())['settings']: e.setdefault(row['env'], str(row['default']))
    switches(g['dir'] / 'env.txt')   # VAR=value arguments win over every file
    for kv in a.vars:
        if '=' not in kv: sys.exit(f'{kv}: expected VAR=value')
        k, v = kv.split('=', 1); e[k] = v
    cmd = [str(NX / 'dm/host/dmhost'), str(w / 'build/patched')]
    log = w / 'logs' / f'run-{stamp}.log'
    print(' '.join(f'{k}={e[k]}' for k in sorted(e) if k.startswith('DM_')) + ' ' + ' '.join(cmd))
    if a.print: return
    gfile = re.match(r'\s*\+?(\d+)', e.get('DM_GUEST_FILE', ''))   # as the host reads it (hle_svc.c guest_file_init: atoi > 0)
    guard = {'1': True, '0': False}.get(e.get('DM_DISK_GUARD'), bool(gfile and int(gfile[1]) > 0))   # disk_guard: on with the guest file (run --phone), DM_DISK_GUARD=1 or 0 forces it
    smp = w / 'logs' / f'disk-{stamp}.tsv' if {'1': True, '0': False}.get(e.get('DM_DISK_SAMPLER'), a.phone) else None   # disk_sampler: on with --phone, unless DM_DISK_SAMPLER says
    gd = (e.get('DM_GUEST_FILE_DIR') or host_temp()) if guard or smp else None   # the guest file's folder (hle_svc.c guest_file_init), whose disk the guard watches
    if gd and not os.path.isabs(gd): gd = str(r / gd)   # a relative DM_GUEST_FILE_DIR is the host's, whose working directory is the run folder
    free0 = first = None
    if guard:   # the room to start with, or the run stops here
        where = f'{"DM_GUEST_FILE_DIR" if e.get("DM_GUEST_FILE_DIR") else "the temporary folder"} ({gd})'
        try: free0 = shutil.disk_usage(gd).free
        except OSError as x: first = f'[switch2mac] [RED] [disk] cannot read the free space on the disk of {where} ({x}): no disk guard in this run\n'
        else:
            if free0 < DISK_START:
                sys.exit(f'stopped: {gb(free0)} GB free on the disk of {where} (as statvfs counts it, without the space macOS could purge), under {DISK_START // GB} GB. A run takes room '
                         f"there as it goes: with DM_GUEST_FILE=1 (the iPhone settings, run --phone) the kernel writes the guest's memory out to a file in that folder, up to all the game "
                         f'has touched, and macOS swaps to that disk when memory runs short; under {DISK_STOP // GB} GB the run stops the game. Make room, or run with DM_DISK_GUARD=0 to '
                         f'start anyway')
            first = f'[switch2mac] [disk] {gb(free0)} GB free on the disk of {where} at the start: the game is stopped under {DISK_STOP // GB} GB (DM_DISK_GUARD=0 turns this off)\n'
    print(f'log: {log}', flush=True)
    if smp: print(f'disk statistics: {smp}', flush=True)
    lock, done, stopped, jobs = threading.Lock(), threading.Event(), threading.Event(), []
    def out(line):   # a whole line to the terminal and the log, from the loop below or the disk guard's thread; once the log is closed, appended to it
        with lock:
            sys.stdout.write(line)
            if not f.closed: f.write(line)
            else:
                with log.open('a') as fa: fa.write(line)
    with log.open('w') as f:
        if first: out(first)
        p = subprocess.Popen(cmd, env=e, cwd=r, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors='replace'); t0 = time.monotonic()
        if free0 is not None: jobs.append(threading.Thread(target=disk_guard, args=(p, gd, free0, t0, done, stopped, out), daemon=True))
        if smp: jobs.append(threading.Thread(target=disk_sampler, args=(smp, gd, t0, done), daemon=True))
        for j in jobs: j.start()
        for line in p.stdout: out(line)
        with lock: f.close()   # under out()'s lock, which checks f.closed and writes as one step: a guard line from here on is appended to the closed log
    rc = p.wait(); done.set(); t1 = time.monotonic()
    for j in jobs: j.join(25)   # the guard's last line before the ones below, and the sampler's last row, as the game exits (each tool gets 10 s)
    fx = disk_free(gd) if free0 is not None else None
    if free0 is not None: out(f'[switch2mac] [disk] {gb(fx)} GB free as the game exits; the free space again {DISK_AFTER} s later (Ctrl-C skips it)\n')
    ms = r / e.get('DM_MEMSTAT', '')   # the host's working directory is the run folder
    if ms.is_file(): subprocess.run([py(), ROOT / 'tools/memstat.py', ms])   # the run's memory, summed up
    if free0 is not None:   # what the disk got back once the guest file was gone and the system had given back its swap
        try:
            time.sleep(max(0, t1 + DISK_AFTER - time.monotonic()))
            out(f'[switch2mac] [disk] {gb(disk_free(gd))} GB free {DISK_AFTER} s after the game exited ({gb(fx)} as it exited, {gb(free0)} at the start)\n')
        except KeyboardInterrupt: print()
    sys.exit(rc or int(stopped.is_set()))   # 1 when the guard stopped the game, whose graceful exit returns 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--work', default=str(ROOT / 'work'), help='build folder (default ./work)')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('setup'); bp = sub.add_parser('build'); bp.add_argument('dump')
    rp = sub.add_parser('refresh'); rp.add_argument('--force', action='store_true', help='rebuild even with no new shaders')
    sub.add_parser('install')
    gp = sub.add_parser('run'); gp.add_argument('vars', nargs='*', help='host switches, VAR=value'); gp.add_argument('--print', action='store_true', help='show the command only')
    gp.add_argument('--phone', action='store_true', help="the iPhone app's switches (games/<game>/env_ios.txt), memory statistics and disk statistics (work/logs/disk-*.tsv): the phone's settings tried on the Mac")
    gp.add_argument('--memstat', action='store_true', help='memory statistics (work/logs/memstat-*.tsv) and address regions (work/logs/regions-*.tsv) beside the log')
    ip = sub.add_parser('ios'); ip.add_argument('--xcode', action='store_true', help='an Xcode project to sign with your team and run on the phone, instead of the IPA')
    ip.add_argument('--romfs', action='store_true', help="the game files packed into the app (<name>-full.ipa), instead of copied into its Documents folder")
    a = ap.parse_args()
    if a.cmd != 'setup' and Path(sys.prefix).resolve() != VENV.resolve():   # run inside .venv
        os.execv(py(), [str(py()), __file__] + sys.argv[1:])
    {'setup': setup, 'build': build, 'refresh': refresh, 'install': install, 'run': run_game, 'ios': ios}[a.cmd](a)


if __name__ == '__main__':
    main()
