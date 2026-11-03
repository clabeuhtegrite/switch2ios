# Changelog

## Unreleased: iPhone port (switch2ios)

- `./switch2mac.py ios`: the iPhone app from the same build, through nxnative's iOS front end: Metal libraries compiled for iOS as well, an unsigned IPA for LiveContainer with the game files left out (`romfs.img` goes into the app's Documents), or `--xcode` for a project to sign. Not run on a phone yet (docs/ios.md).
- `./switch2mac.py run --phone`: the iPhone app's switches and launcher defaults on the Mac, with memory statistics and address regions beside the log; `run --memstat` for the statistics alone.
- `tools/memstat.py`: a memory statistics file summed up (peak, median and a timeline of the footprint, Metal and the guest / GPU / malloc split, time above a phone's budget); `run --phone` and `run --memstat` print it when the game exits.
- Host: `DM_HEAP_REGION` and `DM_APP_POOL` size the guest heap region and the application pool at launch (beside `DM_ALIAS_SIZE`; sizes take K / M / G), the guest layout is logged at start, and `DM_VA_PROBE=1` logs the largest block of address space the process can reserve (also when the guest block does not fit).
- iOS: `DM_MEMSTAT` and `DM_THRCPU` from the app's env files now start (they were read before the files were).
- Memory statistics split what Metal holds: the memory pools' no-copy buffers (those the GPU uses, and all), packaged textures (uploaded copies of pool bytes), other textures, heaps, other buffers, the pools' pages in memory and the pipelines built; `tools/memstat.py` shows them. `DM_MEMSTAT_DUMP` and the control file's `texmem <path>` write every resident Metal allocation and memory pool, one per line.
- Mario Kart World: 4 more shaders the game builds at run time, met in handheld mode, are in `runtime_shaders.tsv`, so a build takes them from the dump (the phone's settings drew the 3D scene black without them).
- Mario Kart World's sdk (SDK 19.3.3 for the Switch 2) gets nxnative's AddressRegionManager node area cut (1 GB → 62 MB at the start of the alias region), which no SDK-wide rule matched: its constructor opens with a pointer-authentication prologue. The game's address regions then start at alias +0.06 GB instead of +1 GB, the room a phone's smaller alias region needs. It applies to the Mac build too (a rebuild picks it up); nothing else changes there.
- Host memory: a texture's storage check at its first use skips pages never written (mincore) instead of reading them, which made the kernel back them with pages of zeros charged to the process (`DM_NVN_MEMANY_MINCORE=0` reads every page). Pure bookkeeping otherwise: the check's answer is the same. It applies to the Mac too.
- Memory diagnostics: `[mem]` log lines for memory pools of 64 MB or more (what of each is in memory at creation, and before and after it becomes resident, with the call site that first asked for its address) and for textures of 64 MB or more (how each was made, its pool's flags, what of its storage is in memory); `texmem` adds each pool's pages in memory, the pools made resident by call site, and the storage checks' bytes read and skipped. `DM_NVN_DESWZ_GPU=0` deswizzles textures on the CPU, so pools holding only textures stay out of the residency set (on iOS a resident pool is wired whole).
- Shaders: nx2_lower lowers SM86 constant loads (LDC) of 8 and 16 bits and with the .IL / .IS / .ISL modes, which Ryujinx's Maxwell frontend reads; it rejected them before. Loads that were already lowered come out the same.
- Shaders: nx2_lower lowers HSETP2 with a constant-buffer operand (SM86 `0x634`), FRND from a constant buffer (`0xb07`), local stores and loads of 64 and 128 bits and at a register address (`STL` / `LDL`, with any cache hint), and a branch with bit 32 set (taken for BRA.U, a uniform-branch hint; it read as a misaligned offset). In Mario Kart World these stopped two DriveRefreshUber vertex shaders (`program has no vertex shader` in the log; the LDC fix above was not enough for them), a DriveRefreshUber pixel shader and three effect shaders, the last 6 of its 6,863. A `build` or `refresh` takes them once the tool is rebuilt (delete `nxnative/dm/gfx/tools/nx2_lower/nx2_lower`). Instructions that were already lowered come out the same.
- Shaders: a program nx2_lower rejects is listed in `work/shader/rejects.tsv` with the instruction the capture or the lowering stopped at (opcode, byte offset, its four words), and the split's summary counts the reasons by opcode, so a missing instruction is named without a debugger.
- Memory: `DM_NVN_SPARSE_RT=1` makes the large sparse textures the game draws into placement sparse textures, with memory only for the 64 KB tiles that have a page mapped behind them, as on the console; what was drawn into a physical page follows it when the game maps that page elsewhere. Such textures over the same pages share their Metal tiles, so from the first one on every barrier makes aliased writes visible (as with image heaps). Mario Kart World has 10 of them, 3.1 GB of Metal memory when made whole. Not measured yet.
- `DM_NVN_SPARSE_FREE=1`: a tile of a large sparse texture read from its pages (Mario Kart World's terrain arrays) that the game unmaps is unmapped too, and its Metal tile is reused by the texture's next tiles, so its heaps grow only to the most tiles mapped at once; such a texture's heaps also go with it. `texmem` gives each one's tiles bound and the physical pages behind them. Before, both stayed for good (the heaps went from 2.2 GB at the title to 3.1 GB after 11 minutes of free roam). Not measured yet.
- `DM_NVN_DESWZ_GPU=2` deswizzles textures on the GPU from a copy of their storage made for the upload and released with it, so their pools stay out of the residency set as with `=0`, without the CPU's cost (Mario Kart World with `=0`: 18 fps in a race). The copy skips pages never written, and gives zeros for pages that cannot be read: a texture's storage can reach such pages past the data its levels use (Mario Kart World: one of 1.4 MB, from 28 KB before its end), and the first run stopped there with SIGBUS. Not measured yet.
- Apart from that cut and the storage check, the Mac app is unchanged unless these switches are set.

## 2026-11-03 (2)

- Builds now work on every macOS 26 version: the app is built for the macOS of the Mac that builds it, instead of nxnative's fixed 26.6 minimum. Thanks to lessy2020 on Reddit for finding and testing this on macOS 26.5.
- `setup` checks the macOS version (macOS 26 or later is needed for Metal 4).

## 2026-11-03

- README: the requirements now say **macOS 26.6 or later**. Apps built with switch2mac do not open on older versions (reported on macOS 26.5.1).

## 2026-11-02: first release

- Native Mac builds of Switch 2 games, built on nxnative. First game: Mario Kart World 1.0.0 (~58 fps in races on an M1 Max).
- Complete first launch: run-time shaders taken from your dump.
- Shared pipeline list: a one-time preparing screen instead of stutter in recorded places (idea from MacNativeGames).
- Porting guide (docs/porting.md) and notes for AI coding assistants (AGENTS.md).
