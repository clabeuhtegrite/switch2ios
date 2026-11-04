# switch2ios: Mario Kart World 1.0.0 on the iPhone, on nxnative main

This branch is [nxnative](https://github.com/seedds/nxnative) main as it is, with a few commits on top and this folder. Each commit stands alone, so it can be offered upstream by itself. The folder holds what upstream does not need: the iPhone's settings for Mario Kart World, the scripts that build and try them, and the tools that read the results.

The fork's earlier tree (`ios-port`: switch2mac, with nxnative under `nxnative/`) stays as it was. This branch replaces it once an IPA built from it has held on the phone.

Target: iPhone 17 Pro / 18 Pro (12 GB) in LiveContainer, which lets an app use 6,143 MiB. 1080p at least, so always handheld mode.

## The commits on top of nxnative main

| Commit | Switches | What it does | Measured on the fork |
| --- | --- | --- | --- |
| host: the guest files' free space | `DM_GUEST_FILE_MIN_FREE` | When `f_bavail` is under the minimum, the volume's capacity for important use counts instead if larger: it includes what the system would purge, as an iPhone's Settings do. | iPhone: `f_bavail` gave 2.7 GB while Settings showed 46.97 GB, and with `f_bavail` alone the game stopped 1 s in (exit code 87) at each start. Mac: 173.3 GB against an `f_bavail` of 57.7. |
| host: `DM_MEM_MAP_LIMIT` | `DM_MEM_MAP_LIMIT` | Turns the profile's `mem_map_limit` on at run time. MapPhysicalMemory is then limited to the application pool, as on the console. | The alias region stayed at 1.25 GB instead of growing to 2.25 GB over four races. After 8 minutes on the main menu: 5.54 GB instead of 6.17. |
| Metal: `DM_NVN_VC_WAYS` | `DM_NVN_VC_WAYS` | A texture that keeps replacing its cached views gets more places for them, up to 64. | On the menus the cache had reused no view, and malloc grew about 300 MB on the phone. With it, malloc stays flat. |
| memstat | `DM_MEMSTAT_WALK_EVERY` | New columns: the guest files, the process's and the system's paging, and marks (`mark <text>` on `DM_CTL`). The region walk can be thinned. The iOS app starts memstat from its env files. | The walk took about 270 ms a sample with 20 guest files. |
| sanitizers: the folder relative to the test folder | | `make asan` / `tsan-threads` in a repository path with a space, such as `~/DEPOTS GIT`. | |

The guest's memory in files (`DM_GUEST_FILE`) is on main since 865cc35f, which took it from this fork (nxnative PR #1315) with changes: on iOS it turns on by itself when the game's memory pool is larger than the room to the memory limit, in 1 GB files.

`docs/env_switches.tsv`, `docs/reference/memory.md` and `docs/reference/nvn-objects.md` describe the switches. The game's note (`dm/games/0400C3F00006E000/README.md`, "Found on this game") points to them.

## The iPhone's settings: `env_ios.txt`

These are packed into the app as `dm/env.txt`. On the phone, `Documents/env.txt` overrides any of them without a new IPA.

- `DM_OPMODE=0`: handheld mode, 1080p.
- `DM_GUEST_FILE=1`: on the phone main turns the files on by itself for this game; the line is for `mac_run.py --phone`, as the Mac keeps them off by default.
- `DM_MEM_MAP_LIMIT=1`.
- `DM_NVN_VC_WAYS=64`.
- `DM_MEMSTAT=memstat.tsv` (in Documents) and `DM_MEMSTAT_WALK_EVERY=10`.
- Pipeline warm-up: left on, as the fork's IPA has had it since test 6.

nxnative's seed list for Mario Kart World is still empty. The fork's recorded list keys its shaders another way: none of its 5,373 keys is in this shader map. `tools/pso_translate.py` gives that list this chain's keys, after a list recorded on the Mac with `DM_PSO_RECORD`, and its output replaces `dm/games/0400C3F00006E000/pso_manifest.tsv` before the IPA is built. With the list empty, the first session builds its pipelines in play, and later starts warm up what the phone recorded (`pso_user.tsv`).

## Building the IPA

```
switch2ios/build_ipa.sh <dump folder> <build folder> [scripts/build_ipa_from_nsp.py options]
```

- **Dump layout.** The dump folder holds `base/exefs/` and `base/romfs/` (`scripts/pack_dump.py` gives the layout). A dump with `exefs/` and `romfs/` at its top is laid out through links in `<build folder>/dump`. The dump itself is only read.
- **Disk.** The build folder must be outside the dump folder. It gets the RomFS image (about 22 GB), the shader caches and the IPA (about 24 GB with the game files in it). That is about 50 GB free.
- **Bundle ID.** It is the script's default, `org.nxnative.0400c3f00006e000`, the same as the fork's earlier IPAs. LiveContainer replaces the app and keeps its Documents.
- **Other options** pass through, for example `-l fr`, `-o <file>.ipa`, or `--gpu g18p` (a pipeline archive for A19 Pro, made on the Mac).
- **`--platform mac`** builds what the Mac host needs instead, without `env_ios.txt`. It prints the command that runs the game.

## Trying the phone's settings on the Mac first

```
switch2ios/build_ipa.sh <dump folder> <build folder> --platform mac 2>&1 | tee build-mac.log
switch2ios/mac_run.py --cmd-file build-mac.log --phone --minutes 10
```

`mac_run.py` runs that command with `env_ios.txt` and the iPhone's alias region (`--phone`: `DM_ALIAS_SIZE` set to the profile's `mem_alias`, 4 GB, as the Mac host otherwise gives the Switch's 64 GB), keeps the log and memstat in `switch2ios/work/logs/`, and keeps the Mac awake. It stops the host when one of these guards trips:

- wired memory over 10.5 GiB (over 11 GiB, it kills the host at once);
- swap over 3.5 GiB;
- under 15 GiB free on the guest files' volume;
- after `--minutes`.

It sets `DM_MEM_CAP_GB=10` and will not start a second host. A fork run with the original settings froze the 16 GB Mac. Each launch of the game on Ad's Mac needs Ad's own yes.

## Tools

- `tools/memstat.py [memstat.tsv] [--session FILE]`: summary of a memstat file, from the phone (`Documents/memstat.tsv`) or from `mac_run.py`.
- `tools/phone_session.py`: a timecoded session on the iPhone, guided from the Mac. Its file splits the phone's memstat into parts.
- `tools/nocopy_cross.m`: checks that Metal no-copy buffers and `vm_remap` work over a pool that crosses from one guest file into the next. No game needed.
- `tools/pso_translate.py`: the fork's recorded pipeline list in this chain's shader keys. Both chains hash the same bytes of each shader in the game files, each in its own way, so the script walks the RomFS (and main, and the fork's run-time shader dumps) and hashes each shader both ways. It stops if the walk gives back under 90 % of the fork map's keys. The output holds only identifiers and render state, like the list it comes from.

## What the fork had that this branch leaves out

| Left out | Why |
| --- | --- |
| `DM_NVN_DESWZ_GPU=2`, `DM_NVN_SPARSE_RT`, `SPARSE_FREE`, `SPARSE_SHARE` | nxnative main does the same by default. Large pools are wired only as far as lookups reach (`DM_NVN_POOL_CHUNKS`). Large sparse textures are placement sparse textures, with only their mapped tiles backed and shared by physical page (`DM_NVN_SPARSE_TILES`). |
| The fix for the vanishing trees (fork 38d0693, e5fb884) | It is nxnative's own c8521ee and e07cba1c, both in main. |
| Punching holes in the guest files (`DM_GUEST_FILE_PUNCH`) | It was off by default in the fork and never on in the phone's settings. |
| `DM_VA_PROBE`, `DM_ALIAS_SIZE`, `DM_HEAP_REGION`, `DM_APP_POOL` | The phone's settings do not use them. |
| Cutting the sdk's node area for build 90ebba18 | It only saved address space, and nxnative runs without it. |
| Metal's memory by kind and the texture list in memstat | They would need porting onto nxnative's pools. Those columns read `-`. |
| The paging counters in `hle_nvn.c`, `prog_shared` | Tied to switch2mac's code. |
| The recorded pipeline list as it is (12,306 lines) | Its shader keys are not this map's. `tools/pso_translate.py` translates it. |

## Updating from nxnative

```
git remote add nxnative https://github.com/seedds/nxnative.git
git fetch nxnative main
git rebase nxnative/main
```

The commits touch a few places in `hle_svc.c`, `crash.c`, `memstat.m`, `hle_hid.c`, `nvn_mtl.m` (the view cache) and `main.m`. That is where conflicts would come from.
