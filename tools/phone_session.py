#!/usr/bin/env python3
"""A timecoded iPhone session: on the Mac, tells you what to do in the game and when (aloud and on screen), and writes down when each part began and the moments you mark,
so that the phone's memstat.tsv and log.txt can be read part by part afterwards (tools/memstat.py memstat.tsv --session work/logs/phone-session-<time>.tsv).

  tools/phone_session.py [--plan FILE] [--speed N] [--no-voice] [--out FILE]

  --plan      the steps, one a line: start (m:ss from the launch), a short name, what to do, separated by tabs; # starts a comment (default: PLAN below)
  --speed     the plan N times faster (a dry run: --speed 60 --no-voice takes a few seconds)
  --no-voice  no spoken steps (macOS say, with a French voice when the Mac has one)
  --out       the session file (default work/logs/phone-session-<time>.tsv)

Start it with the iPhone on LiveContainer's list and press Enter the moment you tap the game: that is second 0, as the phone's memstat.tsv counts it (within a second or two).
Then follow the steps; 20 s before each one it says what comes next. While it runs, Enter alone marks a moment (a stall, a freeze, the app closing) and a word then Enter marks
it with that word; q then Enter ends the session early. The session file has one line per step and per mark: the seconds since the launch, the Mac's clock, the kind, the text.
Nothing leaves this Mac."""
import argparse, shutil, subprocess, sys, threading, time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The default plan (Mario Kart World, a test of the iPhone app after a memory fix for the menus): start, name, what to do
PLAN = [
    ('0:00', 'lancement', "Le jeu se lance. Ne touche à rien jusqu'à l'écran titre."),
    ('1:00', 'menu 1', "Va jusqu'à l'écran de choix du personnage du Grand Prix (titre, puis A jusqu'au choix du personnage), et ne touche plus à rien pendant 3 minutes."),
    ('4:00', 'course 1', "Choisis ton personnage, lance le Grand Prix et joue la première course jusqu'au bout."),
    ('8:00', 'menu 2', "Course finie : reste sur l'écran de fin de course ou le menu qui suit, sans toucher à rien, pendant 3 minutes."),
    ('11:00', 'balade', "Quitte vers le menu principal et pars en balade libre : roule partout pendant 3 minutes."),
    ('14:00', 'fin', "C'est fini : ferme le jeu, puis dépose log.txt et memstat.tsv à la racine d'iCloud Drive."),
]


def secs(s):   # m:ss (or seconds) → seconds
    m, _, r = s.strip().partition(':')
    return int(m) * 60 + int(r) if r else int(m)


def mmss(t): return f'{int(t) // 60}:{int(t) % 60:02d}'


def load_plan(path):
    steps = []
    for n, line in enumerate(Path(path).read_text().splitlines(), 1):
        if not line.strip() or line.lstrip().startswith('#'): continue
        f = line.split('\t')
        if len(f) < 3: sys.exit(f'{path}:{n}: start, name and what to do, separated by tabs')
        steps.append((f[0], f[1].strip(), f[2].strip()))
    return steps


def french_voice():   # the first fr_FR voice macOS has, else None (say's default voice)
    try: out = subprocess.run(['say', '-v', '?'], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError): return None
    for line in out.splitlines():
        if 'fr_FR' in line: return line.split('  ')[0].strip()
    return None


class Session:
    def __init__(self, a, steps):
        self.speed, self.steps, self.lock, self.done = a.speed, steps, threading.Lock(), threading.Event()
        self.voice = None if a.no_voice or not shutil.which('say') else (french_voice() or '')
        self.out = Path(a.out) if a.out else ROOT / 'work/logs' / f'phone-session-{datetime.now():%Y%m%d-%H%M%S}.tsv'
        self.out.parent.mkdir(parents=True, exist_ok=True)
        self.f = self.out.open('w', encoding='utf-8')
        self.f.write('# tools/phone_session.py: seconds since the launch (as the phone\'s memstat.tsv counts them, within a second or two), the Mac\'s clock, the kind, the text\n'
                     'seconds\tclock\tkind\ttext\n')
        self.t0 = None

    def now(self): return (time.monotonic() - self.t0) * self.speed

    def say(self, text):
        print(text, flush=True)
        if self.voice is not None:
            try: subprocess.Popen(['say'] + (['-v', self.voice] if self.voice else []) + [text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError: self.voice = None

    def note(self, kind, text, t=None):
        t = self.now() if t is None else t
        with self.lock:
            self.f.write(f'{t:.1f}\t{datetime.now():%Y-%m-%d %H:%M:%S}\t{kind}\t{text}\n'); self.f.flush()
        return t

    def wait_until(self, t):   # False when the session was ended meanwhile
        while not self.done.is_set():
            left = (t - self.now()) / self.speed
            if left <= 0: return True
            self.done.wait(min(left, 0.5))
        return False

    def run_steps(self):
        for n, (start, name, what) in enumerate(self.steps):
            at = secs(start)
            if n and not self.wait_until(at - 20): return   # 20 s ahead: what comes next
            if n and at - 20 > secs(self.steps[n - 1][0]): self.say(f'Dans 20 secondes : {name}.')
            if not self.wait_until(at): return
            self.note('step', name, at)
            nxt = self.steps[n + 1] if n + 1 < len(self.steps) else None
            with self.lock: print(f'\n=== {mmss(at)}  {name.upper()} ({n + 1}/{len(self.steps)}){"  jusqu'à " + mmss(secs(nxt[0])) if nxt else ""} ===', flush=True)
            self.say(what)
        self.say('Session terminée. Appuie sur Entrée pour fermer.')
        self.done.set()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--plan'); ap.add_argument('--speed', type=float, default=1.0); ap.add_argument('--no-voice', action='store_true'); ap.add_argument('--out')
    a = ap.parse_args()
    if a.speed <= 0: sys.exit('--speed: above 0')
    steps = load_plan(a.plan) if a.plan else PLAN
    if not steps or secs(steps[0][0]) != 0: sys.exit('the plan starts at 0:00')
    s = Session(a, steps)
    print('Session iPhone chronométrée. Les étapes :')
    for start, name, what in steps: print(f'  {start:>5}  {name:<10} {what}')
    print(f'\nLe fichier de la session : {s.out}')
    print("\nPrends l'iPhone sur la liste de LiveContainer, et appuie sur Entrée au moment où tu touches le jeu.")
    try: input()
    except EOFError: return
    s.t0 = time.monotonic(); s.note('start', 'lancement du jeu (Entrée)', 0.0)
    print('Pendant la partie : Entrée seule marque un moment (saccade, gel, fermeture), un mot puis Entrée le marque avec ce mot, q puis Entrée arrête la session.')
    th = threading.Thread(target=s.run_steps, daemon=True); th.start()
    while not s.done.is_set():
        try: line = input()
        except EOFError: break
        if s.done.is_set(): break
        line = line.strip()
        if line.lower() == 'q': s.note('stop', 'arrêtée (q)'); s.done.set(); break
        t = s.note('mark', line or 'moment')
        with s.lock: print(f'  marqué à {mmss(t)}{" : " + line if line else ""}', flush=True)
    s.done.set(); th.join(timeout=2)
    s.f.close()
    print(f'\nSession écrite dans {s.out}')
    print('Pour la lire avec la mémoire : tools/memstat.py <memstat.tsv du téléphone> --session ' + str(s.out))


if __name__ == '__main__':
    main()
