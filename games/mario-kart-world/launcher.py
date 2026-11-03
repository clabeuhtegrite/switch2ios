#!/usr/bin/env python3
# Mario Kart World's launcher settings (the app's dm/launcher.json): nxnative's generic list (dm/games/settings.py) plus this build's own rows.
# The dump has no control.nacp, so the language list is fixed to en-US. Usage: launcher.py <nxnative folder> <rewritten modules dir> <out.json> [mac | ios]   (switch2mac.py runs it:
# mac for the Mac app, the default; ios for the iPhone app, whose list nxnative's build_ipa.sh takes as DM_LAUNCHER_JSON)
import json, os, sys
repo, patched, out = sys.argv[1:4]
platform = sys.argv[4] if len(sys.argv) > 4 else 'mac'
if platform not in ('mac', 'ios'): sys.exit(f'platform must be mac or ios: {platform}')
sys.path[:0] = [os.path.join(repo, 'dm/games'), os.path.join(repo, 'dm/tools')]
os.environ.update(SETTINGS_PLATFORM=platform, GFXAPI='nvn', DM_LANG='en-US')
import nacp, settings
nacp.languages = lambda control: ['en-US']
d = settings.build('0400C3F00006E000', patched, None)
rows = {s['env']: s for s in d['settings']}
overlay = {'env': 'DM_OVERLAY', 'kind': 'choice', 'section': 'Game', 'label': 'Performance overlay',
           'note': 'Key 1 in game cycles it: compact (FPS + resolution) → detailed (graph, GPU, frame time, CPU, memory) → off.',
           'default': 'compact', 'level': 'basic', 'options': [['compact', 'Compact'], ['detailed', 'Detailed'], ['off', 'Off']]}
enc = rows['DM_NVN_ENC_THREAD']
enc.update(default='1', note='Encodes GPU work on its own thread. Off: about 10 fps slower.')
perf = {'env': 'DM_NVN_SKIP_FS', 'kind': 'toggle', 'section': 'Performance', 'label': 'Performance mode',
        'note': 'Skips one environment-reflection update (about +7 fps, near 60). Off: every reflection updated each frame.',
        'default': '83ea368cd702b9de', 'level': 'basic', 'on': '83ea368cd702b9de', 'off': 'none'}
if platform == 'ios':   # a phone: handheld, the Switch 2's own handheld rendering (smaller targets than docked: less memory and GPU time)
    rows['DM_OPMODE'].update(default='0', note='Handheld renders as the Switch 2 does in handheld mode: less memory and GPU time than Docked.')
order = []
for s in d['settings']:
    if s['env'] == 'DM_METAL_HUD' and platform == 'mac': continue   # the Mac app has its own overlay; on the phone Apple's HUD is the overlay
    if s['env'] == 'DM_FULLSCREEN': order += [s, overlay]; continue   # a Mac-only row
    if s['env'] == 'DM_PRESENT_SNAP': order.append(perf)
    order.append(s)
d['settings'] = [s for s in order if s['section'] != 'Controls'] + [s for s in order if s['section'] == 'Controls']   # tabs: Display, Performance, Controls
open(out, 'w', encoding='utf-8').write(json.dumps(d, indent=1, ensure_ascii=False) + '\n')
print(out, len(order), 'settings', f'({platform})')
