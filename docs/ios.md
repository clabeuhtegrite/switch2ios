# Mario Kart World on iPhone (in progress)

The iPhone app is the same build as the Mac app: the game's code rewritten once on the Mac, its shaders translated ahead of time, nxnative's iOS front end (`nxnative/dm/ios`) around it. The rewritten modules are compiled into the app's signed executable, so it needs no JIT. Targets: iPhone 17 Pro (A19 Pro), iPhone 18 Pro and iPhone Duo (A20 Pro), all with 12 GB of memory, installed with [LiveContainer](https://github.com/LiveContainer/LiveContainer).

**Status:** nothing has run on a phone yet. The first step is measuring the game on the Mac with a phone's budget (below).

As for the Mac app: everything is built on your Mac from your own dump, and nothing made from it leaves your devices. The IPA holds the game's rewritten code: never share it.

## What differs from the Mac

| | Mac (M1 Max, as tested) | iPhone (12 GB) |
|---|---|---|
| Memory the app may use | 13-16 GB in play, peaks of ~18 GB | about 6 GB, then iOS ends the app |
| Address space in one block | 20 GB (the profile's guest block) | about 6 GB without the extended virtual addressing entitlement |
| GPU | M1 Max | A19 Pro / A20 Pro (BC textures from the A17 Pro on) |

The game is made for the 9.5 GB a Switch 2 gives it, and the host tells it the same (`app_pool` in the profile). Fitting it into a phone means a smaller guest block and pool the game still runs with, and less memory of the host's own (Metal textures, caches).

## Building

1. Once: Xcode with the iOS platform and the Metal Toolchain (Xcode → Settings → Components), and `brew install xcodegen`. `./switch2mac.py setup` lists what is missing.
2. `./switch2mac.py build <dump folder>`, as for the Mac.
3. `./switch2mac.py ios`: the shaders again with Metal libraries for iOS as well (the first time about as long as the Mac's), then nxnative's `build_ipa.sh` packs `work/MarioKartWorld.ipa`, unsigned. `./switch2mac.py ios --xcode` makes an Xcode project instead (`work/xcode`), to sign with your own team and run on the phone with Xcode's log and debugger.

The game files stay out of the IPA (`DM_COPY_ROMFS=NO`): the app carries only the identity of `work/extract/RomFS.img` and uses that image from its Documents folder.

## Installing with LiveContainer

1. Install `work/MarioKartWorld.ipa` in LiveContainer.
2. Copy `work/extract/RomFS.img` into the app's Documents folder: in the Files app, On My iPhone › LiveContainer › Data › Application › (the app's data folder) › Documents. Any letter case of `romfs.img` works; a copy cut off partway is ignored.
3. Start the app from LiveContainer. Its log is `Documents/log.txt`, its memory statistics `Documents/memstat.tsv`.

LiveContainer runs the app inside its own process, so the app has LiveContainer's entitlements, not its own. The log's first `[mem] address space` line (`DM_VA_PROBE`) says how large a block it can reserve: about 6 GB means no extended virtual addressing.

## Switches

`games/mario-kart-world/env_ios.txt` holds the phone's switches; `switch2mac.py ios` packs it into the app, and a `Documents/env.txt` on the phone overrides any of them without a rebuild (one `KEY=VALUE` per line). The launcher screen has the rest (handheld by default on the phone, Performance mode on).

| Switch | What |
|---|---|
| `DM_ALIAS_SIZE` | the alias region, where the game maps memory on demand (profile: 10 GB) |
| `DM_HEAP_REGION` | the heap region (profile: 9.5 GB) |
| `DM_APP_POOL` | the application pool the game is told, from which it sizes its heap (profile: 9.55 GB) |
| `DM_VA_PROBE=1` | log the largest block of address space the process can reserve |
| `DM_MEMSTAT=<file>` | memory every second: footprint, what iOS has left, Metal, and the guest / code / malloc / GPU split |
| `DM_REGIONS=<file>` | the game's address regions every 2 s, with what is mapped in each |
| `DM_OPMODE=0` | handheld mode |
| `DM_NVN_DESWZ_GPU=0` | textures deswizzled on the CPU: a pool that holds only textures never becomes resident, which on iOS wires the whole pool |
| `DM_NVN_MEMANY_MINCORE=0` | storage checks read every page again, the never-written ones included (to measure what skipping them saves) |

Sizes are bytes, or whole K / M / G (`DM_APP_POOL=4G`). The guest block is 256 MB + alias region + 256 MB of stacks + heap region; its size and parts are logged at start (`[mem] guest block`).

## Fitting into a phone's memory

On the Mac first, where a run that fails costs a minute:

1. `./switch2mac.py run --phone`: the phone's switches and launcher defaults (handheld, pipelines through the MTLDevice as on iOS), with `work/logs/memstat-*.tsv` and `regions-*.tsv` beside the log. Play the same route each time (title, a Grand Prix race, some free roam). The `[mem]` lines give the heap the game asks for and how deep it uses the alias region.
   The `[mem] memory pool` and `[mem] large texture` lines say what of the large pools and textures is in memory and what put it there; the control file's `texmem <path>` lists every Metal allocation and pool at that moment.
   `tools/memstat.py` sums the statistics up (peak, median, a timeline, the time above the budget; `--from` / `--to` for one part of the route); `run --phone` prints it when the game exits, and it reads the phone's `Documents/memstat.tsv` as well.
2. Smaller layouts: `./switch2mac.py run --phone DM_APP_POOL=… DM_HEAP_REGION=… DM_ALIAS_SIZE=…`. Whether the game runs with less than its 9.5 GB, and with how much, is the open question.
3. The target for a phone without extended virtual addressing: a guest block under about 5.5 GB, and a footprint (`memstat`) under about 5.5 GB through a whole race.

What the runs find goes into `env_ios.txt`, then the IPA.
