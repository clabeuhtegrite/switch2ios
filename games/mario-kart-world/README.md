# Mario Kart World

Title ID `0400C3F00006E000`. Profile: [`nxnative/dm/games/0400C3F00006E000`](../../nxnative/dm/games/0400C3F00006E000).

| Version | Title version | Status |
|---|---|---|
| 1.0.0 | 0 | Tested on an M1 Max (64 GB, macOS 26): title, menus, Grand Prix, free roam, traffic and movies; ~58 fps in races with Performance mode on, 2560x1440 |
| any update | — | **Untested** |

## Files here

| File | What |
|---|---|
| `game.json` | title ID, name, version and RomFS marker: how `switch2mac.py` knows this game |
| `launcher.py` | writes the app's settings list (`Resources/dm/launcher.json`): nxnative's generic settings plus Performance overlay, Performance mode and this game's defaults; with `ios`, the iPhone app's (handheld by default) |
| `runtime_shaders.tsv` | where each of the 317 shaders the game builds at run time sits in the dump (file, offset, size, checked by its key); `build` extracts them so the first launch has no black screen, black minimap, frozen intro movie or missing models |
| `env.txt` | switches baked into the app (`DM_FPS=60`: frame pacing statistics fixed at 60 so macOS's 30 Hz power-saving pacing never starts) |
| `env_ios.txt` | switches baked into the iPhone app (memory statistics, the address-space probe, the phone's memory layout once measured); `run --phone` uses them on the Mac ([docs/ios.md](../../docs/ios.md)) |

## Known issues

- **Lamp lights shine through walls at night** (local-light shadows are ignored). Not investigated yet.
- **Not a locked 60**: about 58 fps in races. The game's own frame-rate controller switches to 30 fps pacing whenever a frame takes longer than 16.7 ms, so heavy moments feel uneven.
- **Performance mode** skips the update of one environment-reflection chain (pixel shader `83ea368cd702b9de`, ~4 ms a frame on the Mac for reasons not found yet). No visible difference was seen in play; turn it off in the settings for full reflections.
- **Shadows / detail in the distance** can flicker when driving fast (may be the game's own detail switching).
- **Memory**: 13-16 GB typical, peaks up to ~18 GB in long free-roam sessions.
- **First start** shows a progress screen while the shared pipeline list (~12,000 entries) is built; a place the list lacks can still stutter once.
- **First start after each install** waits 1-3 minutes for macOS's scan of the app.
- Only the US English language list is offered (the dump has no control data).

## Other versions

The profile's `game.tsv` names version 1.0.0 by the build IDs of its modules (first 16 bytes each):

```
build	v0	1.0.0	main=9e244438c3d8ef1c703a6a85ef99c4f0	subsdk0=d39a7a3d44530bb61b35690e5b0383d5	sdk=90ebba182862b9ecdc21fcb87b999012
```

An update has other build IDs, and from 1.8.0 on also a `subsdk1` module and a newer SDK. To try one:

1. Pack the update's own `exefs/` and `romfs/` (an update dump's RomFS is the full patched tree) as the dump folder.
2. Add a `build` line for it in `game.tsv` (the IDs are printed when `build` stops) and the modules in its `modules` line, then run `build` again.
3. Expect differences: the update may move functions the rewriter patches, and the original 1.8.0 patch series found the movie player changed (1.8.0 decodes through `movie::VideoDecoder`, 1.0.0 through `nn::codec`; both are handled in `nxnative/dm/host`).

Please report what happens (log + screenshots, no game files). Mark such builds as untested.
