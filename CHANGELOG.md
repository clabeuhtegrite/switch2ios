# Changelog

## Unreleased: iPhone port (switch2ios)

- `./switch2mac.py ios`: the iPhone app from the same build, through nxnative's iOS front end: Metal libraries compiled for iOS as well, an unsigned IPA for LiveContainer with the game files left out (`romfs.img` goes into the app's Documents), or `--xcode` for a project to sign. Not run on a phone yet (docs/ios.md).
- `./switch2mac.py run --phone`: the iPhone app's switches and launcher defaults on the Mac, with memory statistics and address regions beside the log; `run --memstat` for the statistics alone.
- `tools/memstat.py`: a memory statistics file summed up (peak, median and a timeline of the footprint, Metal and the guest / GPU / malloc split, time above a phone's budget); `run --phone` and `run --memstat` print it when the game exits.
- Host: `DM_HEAP_REGION` and `DM_APP_POOL` size the guest heap region and the application pool at launch (beside `DM_ALIAS_SIZE`; sizes take K / M / G), the guest layout is logged at start, and `DM_VA_PROBE=1` logs the largest block of address space the process can reserve (also when the guest block does not fit).
- iOS: `DM_MEMSTAT` and `DM_THRCPU` from the app's env files now start (they were read before the files were).
- Mario Kart World's sdk (SDK 19.3.3 for the Switch 2) gets nxnative's AddressRegionManager node area cut (1 GB → 62 MB at the start of the alias region), which no SDK-wide rule matched: its constructor opens with a pointer-authentication prologue. The game's address regions then start at alias +0.06 GB instead of +1 GB, the room a phone's smaller alias region needs. It applies to the Mac build too (a rebuild picks it up); nothing else changes there.
- Apart from that cut, the Mac app is unchanged unless these switches are set.

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
