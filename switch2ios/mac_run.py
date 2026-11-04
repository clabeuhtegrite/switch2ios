#!/usr/bin/env python3
"""Run Mario Kart World in the Mac host with guards on the Mac's memory, optionally with the iPhone app's switches (--phone).

  switch2ios/mac_run.py (--cmd-file FILE | --cmd 'COMMAND') [--phone] [--minutes N] [-e KEY=VALUE ...] [--label NAME]
                        [--wired-stop GIB] [--wired-kill GIB] [--swap-stop GIB] [--disk-stop GIB] [--dry-run]

COMMAND is the last line `scripts/build_ipa_from_nsp.py <dump folder> --platform mac` prints: cd <run folder> && K=V ... <dmhost> <patched modules>
(an optional `2>&1 | tee <file>` at its end is dropped: this script keeps the log). --cmd-file reads it from a file (its last line that starts with "cd ").

--phone adds switch2ios/env_ios.txt, the switches the IPA packs, as `scripts/build_ipa_from_nsp.py --env-file` would, and the iPhone's alias region: DM_ALIAS_SIZE=<the profile's
mem_alias>, as the Mac host otherwise gives the Switch's 64 GB (docs/env_switches.tsv); -e adds or overrides one (after --phone).
The log goes to switch2ios/work/logs/log-<label>.txt and DM_MEMSTAT, when set, to work/logs/memstat-<label>.tsv (switch2ios/tools/memstat.py reads the newest by
default); DM_CTL is work/logs/ctl-<label>.txt unless set (`echo "mark <text>" >> <it>` marks the memstat file; hle_hid.c lists the other commands).

Guards, read every second; the host is stopped (SIGTERM, then SIGKILL 5 s later) when one trips:
  - wired memory (vm_stat, "Pages wired down") over --wired-stop GiB (default 10.5); over --wired-kill GiB (default 11) it is killed at once. A whole Metal
    residency set is wired, and a fork run with the original settings froze this Mac (16 GB);
  - swap used (sysctl vm.swapusage) over --swap-stop GiB (default 3.5);
  - free space on the guest files' volume under --disk-stop GiB (default 15; the host itself stops at 5 GB, exit 87);
  - --minutes N of play (default: none).
DM_MEM_CAP_GB, the host's own watchdog on its footprint (exit 86), is 10 here unless set (the host's default on the Mac is 24). caffeinate keeps the Mac awake while the host runs.
Ctrl-C (or SIGTERM to this script) stops the host the same way. It does not start while another host runs or wired memory is already over --wired-stop. At the end: the host's
exit status, the guards' peaks, and memstat.py's summary when there is a memstat file. Exit status: 3 when a guard (wired, swap, disk) stopped the host, else the host's (0 after --minutes).
Each launch of the game on Ad's Mac needs Ad's own yes first.
"""
import argparse, os, re, shlex, signal, subprocess, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent            # switch2ios/
LOGS = HERE / 'work/logs'                         # git ignores work/
GIB = 1 << 30
GAME_TSV = HERE.parent / 'dm/games/0400C3F00006E000/game.tsv'   # the game's profile: mem_alias, the iPhone's alias region
EXITS = {86: 'the host\'s memory watchdog (DM_MEM_CAP_GB)', 87: 'the guest files\' volume short of free space (DM_GUEST_FILE_MIN_FREE)'}


def parse_cmd(cmd):
    """(run folder, environment, argv) of the command build_ipa_from_nsp.py --platform mac prints"""
    t = shlex.split(cmd)
    if len(t) < 4 or t[0] != 'cd' or t[2] != '&&': raise SystemExit(f'not the command --platform mac prints (cd <run folder> && K=V ... <host> <modules>): {cmd[:200]}')
    env, i = {}, 3
    while i < len(t) and re.match(r'[A-Za-z_][A-Za-z0-9_]*=', t[i]):
        k, v = t[i].split('=', 1); env[k] = v; i += 1
    argv = t[i:]
    if '|' in argv: argv = argv[:argv.index('|')]
    argv = [a for a in argv if a != '2>&1']
    if not argv: raise SystemExit('the command names no host')
    return t[1], env, argv


def env_file(path):   # KEY=VALUE lines, # starts a comment (as build_ipa_from_nsp.py env_file_pairs and the iOS app read them)
    out = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'): continue
        if '=' not in line: raise SystemExit(f'{path}: not KEY=VALUE: {line}')
        k, v = line.split('=', 1); out[k.strip()] = v.strip()
    return out


def wired_bytes():
    out = subprocess.run(['vm_stat'], capture_output=True, text=True, check=True).stdout
    page = int(re.search(r'page size of (\d+) bytes', out).group(1))
    return int(re.search(r'Pages wired down:\s+(\d+)', out).group(1)) * page


def swap_used_bytes():
    out = subprocess.run(['sysctl', '-n', 'vm.swapusage'], capture_output=True, text=True, check=True).stdout
    m = re.search(r'used = ([\d.]+)([KMG])', out)
    return float(m.group(1)) * {'K': 1 << 10, 'M': 1 << 20, 'G': GIB}[m.group(2)] if m else 0.0


def profile_value(key):
    for line in GAME_TSV.read_text().splitlines():
        f = line.split('\t')
        if len(f) >= 2 and f[0].strip() == key: return f[1].strip()
    return None


def free_bytes(path):
    st = os.statvfs(path); return st.f_bavail * st.f_frsize


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument('--cmd-file', help='a file holding the command (its last line that starts with "cd ")')
    src.add_argument('--cmd', help='the command itself')
    ap.add_argument('--phone', action='store_true', help='add switch2ios/env_ios.txt, the iPhone app\'s switches, and its alias region (DM_ALIAS_SIZE)')
    ap.add_argument('-e', action='append', default=[], metavar='KEY=VALUE', help='add or override a switch (after --phone)')
    ap.add_argument('--label', default=time.strftime('%Y%m%d-%H%M%S'), help='the logs\' name (default: the time)')
    ap.add_argument('--minutes', type=float, default=0, help='stop the host after this many minutes (default: none)')
    ap.add_argument('--wired-stop', type=float, default=10.5); ap.add_argument('--wired-kill', type=float, default=11.0)
    ap.add_argument('--swap-stop', type=float, default=3.5); ap.add_argument('--disk-stop', type=float, default=15.0)
    ap.add_argument('--dry-run', action='store_true', help='print what would run and exit')
    a = ap.parse_args()

    if a.cmd_file:
        lines = [l.strip() for l in Path(a.cmd_file).read_text().splitlines() if l.strip().startswith('cd ')]
        if not lines: raise SystemExit(f'{a.cmd_file}: no line starting with "cd "')
        cmd = lines[-1]
    else:
        cmd = a.cmd
    cwd, env, argv = parse_cmd(cmd)
    if a.phone:
        env.update(env_file(HERE / 'env_ios.txt'))
        alias = profile_value('mem_alias')
        if 'DM_ALIAS_SIZE' not in env and alias: env['DM_ALIAS_SIZE'] = alias   # the iPhone's address space; the Mac host's alias region is the Switch's 64 GB
    for kv in a.e:
        if '=' not in kv: raise SystemExit(f'-e {kv}: not KEY=VALUE')
        k, v = kv.split('=', 1); env[k] = v
    LOGS.mkdir(parents=True, exist_ok=True)
    log = LOGS / f'log-{a.label}.txt'
    if env.get('DM_MEMSTAT') and not os.path.isabs(env['DM_MEMSTAT']): env['DM_MEMSTAT'] = str(LOGS / f'memstat-{a.label}.tsv')
    if 'DM_CTL' not in env:
        env['DM_CTL'] = str(LOGS / f'ctl-{a.label}.txt'); Path(env['DM_CTL']).touch()
    env.setdefault('DM_MEM_CAP_GB', '10')
    gdir = env.get('DM_GUEST_FILE_DIR') or tempfile.gettempdir()   # the host's default: the user's temporary folder (confstr _CS_DARWIN_USER_TEMP_DIR, as $TMPDIR)

    print(f'run folder: {cwd}\nhost: {shlex.join(argv)}\nswitches: ' + ' '.join(f'{k}={shlex.quote(v)}' for k, v in env.items()))
    print(f'log: {log}' + (f'\nmemstat: {env["DM_MEMSTAT"]}' if env.get('DM_MEMSTAT') else '') + f'\ncontrol: {env["DM_CTL"]}')
    w0, s0, f0 = wired_bytes() if not a.dry_run else 0, swap_used_bytes() if not a.dry_run else 0, free_bytes(gdir) if os.path.isdir(gdir) else 0
    print(f'before: wired {w0 / GIB:.2f} GiB, swap used {s0 / GIB:.2f} GiB, free on {gdir}: {f0 / GIB:.1f} GiB')
    if a.dry_run: return 0
    if w0 > a.wired_stop * GIB: raise SystemExit(f'wired memory is already {w0 / GIB:.2f} GiB, over --wired-stop: not starting')
    if f0 < (a.disk_stop + 5) * GIB: raise SystemExit(f'{f0 / GIB:.1f} GiB free on {gdir}: under --disk-stop + 5 GiB, not starting')
    other = subprocess.run(['pgrep', '-x', Path(argv[0]).name], capture_output=True, text=True).stdout.split()
    if other: raise SystemExit(f'{Path(argv[0]).name} is already running (pid {", ".join(other)}): not starting a second one')

    full = dict(os.environ, **env)
    with open(log, 'wb') as lf:
        p = subprocess.Popen(argv, cwd=cwd, env=full, stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
        caff = subprocess.Popen(['caffeinate', '-dims', '-w', str(p.pid)])
        t0 = time.time(); peak = {'wired': w0, 'swap': s0}; low = f0; why = None; alarm = False; stopping = None; killed = False
        state = {'ask': None}

        def ask_stop(sig, frm): state['ask'] = f'signal {sig} to mac_run.py'
        signal.signal(signal.SIGINT, ask_stop); signal.signal(signal.SIGTERM, ask_stop)

        def stop(reason, now=False, guard=False):
            nonlocal why, alarm, stopping, killed
            if (stopping and not now) or killed: return
            killed = now
            if not why: why, alarm = reason, guard
            print(f'[guard] {reason}: {"killing" if now else "stopping"} the host', flush=True)
            try: os.killpg(p.pid, signal.SIGKILL if now else signal.SIGTERM)
            except ProcessLookupError: pass
            stopping = stopping or time.time()

        last = 0.0
        while p.poll() is None:
            time.sleep(1)
            el = time.time() - t0
            try: w, s = wired_bytes(), swap_used_bytes()
            except (subprocess.CalledProcessError, AttributeError) as e: print(f'[guard] cannot read the memory figures: {e}'); w, s = 0, 0
            f = free_bytes(gdir)
            peak['wired'] = max(peak['wired'], w); peak['swap'] = max(peak['swap'], s); low = min(low, f)
            if w > a.wired_kill * GIB: stop(f'wired memory {w / GIB:.2f} GiB, over {a.wired_kill} GiB', now=True, guard=True)
            elif w > a.wired_stop * GIB: stop(f'wired memory {w / GIB:.2f} GiB, over {a.wired_stop} GiB', guard=True)
            elif s > a.swap_stop * GIB: stop(f'swap used {s / GIB:.2f} GiB, over {a.swap_stop} GiB', guard=True)
            elif f < a.disk_stop * GIB: stop(f'{f / GIB:.1f} GiB free on {gdir}, under {a.disk_stop} GiB', guard=True)
            elif a.minutes and el > a.minutes * 60: stop(f'{a.minutes:g} minutes')
            elif state['ask']: stop(state['ask'])
            if stopping and time.time() - stopping > 5 and p.poll() is None: stop('still running 5 s after SIGTERM', now=True)
            if el - last >= 30:
                last = el
                print(f'{el / 60:5.1f} min  wired {w / GIB:5.2f} GiB  swap {s / GIB:4.2f} GiB  free {f / GIB:5.1f} GiB', flush=True)
        caff.terminate()
    rc = p.returncode
    el = time.time() - t0
    print(f'host ended after {el / 60:.1f} min, status {rc}' + (f' ({EXITS[rc]})' if rc in EXITS else '') + (f'; stopped: {why}' if why else ''))
    print(f'peaks: wired {peak["wired"] / GIB:.2f} GiB, swap used {peak["swap"] / GIB:.2f} GiB; least free on {gdir}: {low / GIB:.1f} GiB')
    ms = env.get('DM_MEMSTAT')
    if ms and Path(ms).is_file():
        subprocess.run([sys.executable, str(HERE / 'tools/memstat.py'), ms])
    return 3 if alarm else 0 if rc == 0 or why else rc   # 3: a guard stopped it (wired, swap or disk)


if __name__ == '__main__':
    sys.exit(main())
