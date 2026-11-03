# Porting a Switch 2 game to the Mac

This guide covers how to bring up a new Switch 2 game with switch2mac, or a new version of one that already works. It sums up everything learned while porting Mario Kart World, whose full story is in [mario-kart-world.md](mario-kart-world.md). You can follow it yourself in Terminal, or hand it to an AI coding assistant together with [AGENTS.md](../AGENTS.md).

Bring-up is research. A new game will boot partway, draw wrong, or crash, and each problem has to be traced. Nothing below promises that a given game will run.

## How a build works

| Step | Tool | Output in `work/` |
|---|---|---|
| Game files | `nxnative/dm/tools/romfs_image.py` | `extract/ExeFS`, `extract/RomFS.img` (one image of the RomFS) |
| Code | `nxnative/dm/tools/patch_nso.py` | `build/patched/`: the game's modules, rewritten so system calls and SDK functions land in the host |
| Shaders | `nx2_split.py` (find + lower Ampere → Maxwell with `nx2_lower`), `own_compilers.py` (Ryujinx translator → MSL → Metal), `nx2_res.py`, `nx2_reflect.py` | `cache/metal_shader_cache/b-<chain>/`, `profile/<title ID>/shader_*.tsv` |
| Host | `make -C nxnative/dm/host GAME=<title ID>` | `nxnative/dm/host/dmhost`: the native runtime, built for this game's profile |
| App | `nxnative/dm/mac/pack_app.py` | `<Game>.app` (APFS clones of the big files) |

The game's ARM64 code runs directly on the CPU. NVN2, the Switch 2 graphics API, is implemented on Metal 4 (`nxnative/dm/gfx/nvn/nvn_mtl.m`), and every shader is translated ahead of time. The *chain version* (`nxnative/dm/gfx/nvn/dmsh_chain.h`) is a hash of the translator tools. It names the library folder. Any change to the translator tools needs `dmsh_chain.py --header` and a host rebuild, or the host refuses the libraries.

## 1. The dump

You need a dumped, already-extracted folder: `exefs/` (`main`, `main.npdm`, `sdk`, `subsdk*`, `rtld`) and `romfs/`, or the same under `base/`. switch2mac decrypts nothing and uses no keys. Dump tools leave `.listing` files, and Finder adds `.DS_Store`; `build` skips both.

The title ID is in `main.npdm` (ACI0 + 0x10). The build ID of each module is the first 0x10 bytes of its NSO build ID: `nxnative/dm/tools/nso.py <module> <out.img>` unpacks the module and prints it (`moduleid`). A version's identity is the build ID of its `main`.

Updates: an update dump's RomFS is the full patched tree. Build from the update's own `exefs/` + `romfs/`.

## 2. A game folder and a profile

1. **`games/<game>/game.json`**: `title_id`, `name`, `version` and `marker`. The marker is a RomFS file that only this game has, used by the app to check its image.
2. **`nxnative/dm/games/<title ID>/game.tsv`**: the profile. Start from a copy of Mario Kart World's (`0400C3F00006E000/`) and change:
   - `title_id`, `program_id`, `name`.
   - `build v0 <version> main=<id> sdk=<id> subsdk0=<id> ...`: one line per version, with the IDs the build prints when it stops.
   - `modules`: every module that `main` imports from, in load order (Mario Kart World 1.0.0: `main subsdk0 sdk`; 1.8.0 adds `subsdk1`).
   - `romfs_marker`: as in game.json.
   - Memory: `mem_reserve`, `mem_alias`, `app_pool` and `sys_resource` (the system resource size from NPDM META +0x14). `nxnative/dm/games/profile_guess.py <ExeFS dir> <RomFS dir>` guesses these and other keys from the game's files, with a reason for each.
   - `tex_flush_upload on` and `tex_dirty_upload on`: keep these. Nintendo's own engines fill textures from the CPU, sometimes before the texture is created (see "Lessons" below).
   - Empty shader tables (`shader_map.tsv`, `shader_facts.tsv`, `shader_pairs.tsv`, `shader_map.tsv.conflicts.tsv`, each with only its header). The first build fills them in `work/profile/`; copy the results back into the profile when they are good.
3. `games/<game>/launcher.py` and `env.txt`: copy Mario Kart World's and change the title ID and its game-specific rows (its Performance mode toggle names one of Mario Kart World's own shaders).
4. Run `./switch2mac.py build <dump>`.

nxnative's [`dm/games/README.md`](../nxnative/dm/games/README.md) describes every profile file.

## 3. First runs

Build, then run the game straight from `work/` with a log:

```bash
./switch2mac.py run
```

`run` takes host switches as `VAR=value` arguments, and writes everything to the terminal and to `work/logs/run-<time>.log`. To match the app's defaults, add the switches from the game's `launcher.py` (Mario Kart World: `DM_NVN_ENC_THREAD=1`).

Read the log in this order:

- **Crashes** print `SIGSEGV` / `abort` with `pc=main+0x...` and a backtrace in module offsets. `tools/nso_syms.py work/extract/ExeFS/main dis <offset>` disassembles around the offset; `plt <regex>` and `calls <regex>` find imported functions and their callers.
- **`[RED]` lines**: things the host does not handle. Typical ones are unimplemented NVN2 functions, shaders not in the map, and draws skipped.
- **`not in the shader map`**: a shader the game built at run time. See step 4.
- **Logs to expect while the game is busy:** `[longframe]` lines give CPU and GPU time for slow frames. Shaders and pipelines missing for the first time cause stutter, but only once.

Milestones for a new game: boot → first NVN2 call → first frame → title screen → menus → gameplay → movies → standalone app.

## 4. Run-time shaders (the black screen)

Switch 2 games build some shaders while they run, so they are not in the BNSH containers the split reads. Mario Kart World has 317 of them, including the final copy to the screen; without them the first launch is black. Their parts *are* in the dump, in BAHS containers (`Lib/agl`, `Lib/gsys`, `Lib/Terrain`, `Shader/*.sarc`) and loose in `main`.

1. Play the game with `./switch2mac.py run` (or the installed app). The host saves every unknown shader to `work/run/shdump` (or the app's support folder) as `<key>.ctl` + `<key>.code`.
2. `./switch2mac.py refresh` builds them in, so your own build is complete.
3. To make it complete for everyone, locate them in the game files:

```bash
.venv/bin/python tools/find_runtime_shaders.py work/input/romfs work/shader/main.img games/<game>/runtime_shaders.tsv work/captures
```

The table holds only file names and offsets, never game data. Every later `build` extracts them from that person's dump and checks each one by its key, `sha256(control[:0xd8] + code)[:16]`. The search can take an hour.

## 4b. The shared pipeline list (no first-play stutter)

Metal builds a pipeline for every shader pair and render state the game draws with; the first time is a stall. The app records each one in `pso_user.tsv` (next to its save folder), and the profile's `pso_manifest.tsv` is the shared list every build ships: the app builds its rows before the first frame (progress screen; `DM_PSO_PREPARE=0` builds them in the background instead) and keeps them in its pipeline archive. Rows are `S` (shader library + stage), `C` (compute pipeline) and `R` (render pipeline key: shader keys + render state as hex, pointers cleared): identifiers only. For a new game, play it widely, then seed the list with `tools/pipelines.py new` (an empty list: every row is new) and `merge`. Rows of shaders a build does not have are skipped quietly. (The approach follows MacNativeGames' shared shader list.)

## 5. Finding graphics bugs

All of these are host switches (`./switch2mac.py run VAR=value ...`). While the game runs, you can also write a command line into `work/run/ctl.txt`; the host reads it at the next frame.

| Switch / command | What it gives |
|---|---|
| `shot <file.png>` (ctl) / `DM_NVN_SHOT=<frames>` | screenshot of the game's picture; `DM_NVN_SHOT_OUT=WxH` takes it after the host's present / upscale pass |
| `burst <n> <folder>` (ctl) | screenshots of the next n frames + a pass dump of each: for flicker |
| `passdump 1` (ctl) / `DM_NVN_PASSDUMP=<frames>` + `DM_NVN_PASSDUMP_ALL=1` | every render / compute pass of a frame: targets, shaders (`vs` / `fs` keys), bound textures (serials), draws |
| `texraw <serial> <file>` / `texdump <file with %d>` (ctl), `DM_NVN_TEXSNAP=<pass idx>` + `DM_NVN_SNAPRAW=1` | texture contents, raw floats included. PNG dumps clamp HDR values to white, so check raw values for NaN / inf |
| `tools/pass_trace.py <frame lines> <serial>` | walks a pass dump back from one texture: who wrote it, with which shader, reading what |
| `DM_NVN_CBDUMP=<dir>` | the uniform and read-only storage buffers of every draw in a dumped frame |
| `DM_NVN_HILITE=<fs key>` / `v:<vs key>` / `@file` | draws those shaders in solid magenta: is this draw the thing on screen? |
| `DM_NVN_SKIP_FS=<key>` / `DM_NVN_EVERY_FS=<key>:<n>` | skip a pixel shader's draws / run them every nth frame: bisect, measure a pass's cost |
| `DM_PSO_ARCHIVE=off` | **needed whenever you swap a Metal library by hand**: otherwise the pipeline archive hands back pipelines built from the old one |
| `DM_PROBE=main:<off>[@reg+off]` + `DM_PROBE_LIMIT`, `probewin <frames>` (ctl) | log registers (and memory) each time the game reaches an address |
| `DM_PROBE_WATCH=<addr>+<n>`, `readwatch <addr> <bytes>` (ctl) | who writes / reads guest memory (`pc` + backtrace) |
| `memdump <addr> <bytes> <file>` (ctl) | raw guest memory |
| `DM_GPUW_LOG=1` | each distinct GPU writer (storage-buffer writes with shader key, copies, fills), once |
| `DM_GPU_READWATCH=1` | the CPU code that reads GPU results |
| `DM_NVN_GPUPROF=every:600` + `nxnative/dm/tools/gpuprof.py` | GPU time per pass |
| `DM_METAL_HUD=1`, `DM_OVERLAY=detailed`, `tools/perf.sh [log]` | fps, GPU / frame time, memory |
| `DM_FT_DEBUG=WxH`, `DM_NVN_TEXINFO=WxH`, `DM_TEX_BT=WxH` | life of textures of one size: registration, uploads, who creates them |

Method that worked every time: screenshot → pass dump of that frame → the pass that writes the wrong pixels → its inputs (raw) → the first pass whose output is wrong → its shader (MSL in the library folder) or the host's binding of it → the game code that feeds it (probe / watch).

## 6. Lessons from Mario Kart World

Each of these lessons cost days to find, and each will probably apply to the next Switch 2 game too. All the fixes are in this repository's `nxnative/`.

- **Movies**: 1.0.0 decodes VP9 through the low-level `nn::codec` API, not `movie::VideoDecoder`. `hle_nx2_codec.c` covers both with libvpx, on a worker thread per decoder.
- **NVN2 compute driver constants**: c0 has its own layout on Switch 2 (dispatch size at +4 / +8). The NVN1 layout gave 1/0 → NaN → a white world.
- **Translator**: the block after a `CAL` lost its fall-through edge in Ryujinx's decoder, which broke every shader with NVIDIA's slow-path sqrt / rsqrt / division subroutine.
- **Opcodes the lowering lacked**: SM86 `0x59f` (SURED), `0x2bf` (UPOPC), `0x923` / `0x927` (stage memory of task / mesh shaders), and later `0x634` (HSETP2 with a constant buffer), `0xb07` (FRND from a constant buffer), 128-bit and register-addressed `STL` / `LDL` (`0x387` / `0x983`) and BRA with bit 32 set (a uniform-branch hint, read as part of the offset before). Check `work/shader/rejects.tsv` after every split: each reason ends with the instruction the lowering stopped at (its opcode, its byte offset in the program's code and its four words).
- **Mesh and task shaders** (control section byte 0x56: 6 task, 7 mesh) now run as Metal object / mesh shaders, including per-primitive outputs and `DrawMeshTasks`. Layout notes are in the code (`MeshShaderInfo.cs`, `nx2_split.py`).
- **Compute shared memory size**: control section +0x64. The split used to declare 4 KB for every shader, so a 48 KB shader silently lost data (vanishing traffic cars).
- **Textures the CPU fills before creating them**: `tex_flush_upload` / `tex_dirty_upload`, plus "filled before init" detection. Without these, terrain height tiles were all zero.
- **Linear / sRGB twins**: one memory read as RGBA8 and as RGBA8_sRGB must be one Metal texture with a format view (the frozen intro movie).
- **A pass of only mesh draws** must record its writes, or Apple's tile memory drops it (stored "don't care").
- **Switch 2 clock**: 31.25 MHz ticks, not 19.2 MHz. With the wrong rate, every time span the game measures is off by 1.6×, so it paces itself at 30 fps.
- **Focus at boot**: games read their focus state before they are ready. The host reports "in focus" until the 300th frame.
- **The game's own frame-rate controller** switches to 30 fps pacing whenever a frame takes longer than 16.7 ms. Overriding it speeds up game logic, so speed it up for real instead.
- **GPU timestamps feed game decisions** (shadow quality). Changing the timestamp mode broke shadows.
- **Not worth repeating**: asynchronous submission (bloom regressions), forcing render resolution (the game sizes its targets from data), and a newer Eglantine lowering (incompatible binding layout).

Upstream nxnative has since merged Switch 2 support of its own into `main`: a native Ampere translator frontend, NVN2 device values and push constants. Moving to it is a possible future direction; this repository is built on the earlier patch-series path.

## 7. Performance

Measure first: `tools/perf.sh`, the overlay (key 1), `DM_NVN_GPUPROF`, and macOS `sample <pid> 5` or Instruments' Metal System Trace for CPU / GPU time. What paid off for Mario Kart World, from 20 to 58 fps:

- The Switch 2 clock fix.
- Sparse-texture page walks cached.
- Uniform reads hoisted in every shader.
- Encoding on its own thread (`DM_NVN_ENC_THREAD=1`).
- Attachment-load skipping.
- One reflection chain skipped (Performance mode).

The rule held throughout: a speed-up that changes the picture is reverted, and each has an off switch.

## 8. Ship it

- **`games/<game>/README.md`**: versions (tested / untested), known issues, notes.
- **`games/<game>/runtime_shaders.tsv`**: so the first launch is complete.
- **The profile's shader tables**: from `work/profile/<title ID>/`, if you changed them.
- **The README's game table**: add the game.
- **Never commit** game files, shader dumps (`.ctl` / `.code`), Metal libraries, saves, logs with your paths, screenshots of private data, or built apps.
