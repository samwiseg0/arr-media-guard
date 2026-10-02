# Re-grabs, restores and kept originals

A re-grab deletes a broken file through the app, marks the grab failed and lets the app search again. A restore puts
back the old file of a bad upgrade. The hook keeps the files it replaces, so you can undo each change.
[design.md](design.md) has the rules behind each step.

## Re-grabs

`REGRAB` lists the kinds of fault that re-grab, joined by commas. The default is `audio,video`.

| Kind | The fault |
| --- | --- |
| `audio` | Broken audio. |
| `video` | Corrupt video. |
| `content` | Wrong content, from the TMDB and duration checks. |
| `damage` | A damaged source that a conversion shows. |

- A kind that is not listed gets the second check, and then only alerts "would re-grab".
- `none` turns every re-grab off.
- An unknown kind is left out, and a blank value re-grabs nothing. `--selftest` fails on both.
- The "Wrong episode" alert never re-grabs.

`REGRAB_CAP` limits the re-grabs per instance in 24 hours, 30 by default. Then the hook alerts only. Every kind shares the
one count. `0` turns every re-grab off.

## Restore after a bad upgrade

With `RESTORE=true`, the default, a re-grab of a broken upgrade puts back the old file from the recycle bin. The old
file comes back only if it checks clean.

1. Turn on the recycle bin in each app, on the media's file system.
2. In Docker, mount the bin at the app's path, or add a pair for it to the app's path map. See
   [docker.md](docker.md#path-maps).
3. Run `--selftest`, or press Test in the app's connection. Both warn when the bin is off or elsewhere. In Docker,
   Test also warns in the log when the bin is not mounted.

With `KEEP_REPLACED=true`, the hook keeps its own copies, and the app's recycle bins are optional.

## Keep replaced files

With `KEEP_REPLACED=true`, at each grab the hook hard-links the library files the grab may replace into
`.<NAME>-recycle`. It also links their subtitles, `.nfo` and images. A restore uses this copy when the app's recycle
bin has none it can use.

1. Set `KEEP_REPLACED='true'` in the env file.
2. Turn on **On Grab** in the app's connection, next to On File Import and On File Upgrade.
3. Run `--selftest`. It warns, and so does Test, when the connection sends no Grab, or when the hook cannot hard-link
   a file on a mount.

- The hook keeps nothing on a file system that refuses a hard link. Examples are an SMB/CIFS share, some FUSE mounts
  and Docker Desktop file sharing.
- Each copy stays, and holds its disk space, for `KEEP_ORIGINALS_DAYS`. Then the nightly audit, or the worker's prune
  once a day, removes it.
- With `KEEP_ORIGINALS_DAYS` at 0, it keeps nothing.
- A value other than `true` or `false` keeps nothing, and `--selftest` fails on it.

## Kept originals

A header repair, a subtitle remux, a sidecar move and a conversion each keep the file they replace. The hook keeps it
for `KEEP_ORIGINALS_DAYS` days, 7 by default, in `.<NAME>-originals`.

| Folder | What it holds |
| --- | --- |
| `.<NAME>-originals` | The kept originals, at `<folder>/.<NAME>-originals/<UTC time>/<path from the folder>`. |
| `.<NAME>-recycle` | The files `KEEP_REPLACED` keeps. |
| `.<NAME>-convert` | Beside a video. It holds a remux's temp file, and the original and the extras during a conversion. |

`NAME` is `arr-media-guard` by default, so the first folder is `.arr-media-guard-originals`.

### Where the folders go

The hook puts `.<NAME>-originals` and `.<NAME>-recycle` at the top of each mount. The app user must be able to write
there. In Docker that user is `PUID:PGID`.

- When the user cannot write the top of the mount, the hook takes the highest folder on the file's path that the user
  can write, such as a show's folder. This happens at the root of a Docker volume, for example.
- A folder of that name that exists on the path wins, so the place stays the same from run to run.
- When no folder on the path is writable, every header repair, subtitle remux and conversion of the file is skipped,
  and a grab keeps nothing. Then create the folders at the top of the mount and give them to the app user.

### In the apps and Plex

- Radarr leaves a hidden folder out of its root folder list.
- Sonarr lists a recycle bin such as `.recycle` as an unmapped folder in Library Import. Do not import it. Its disk
  scan never reads it.
- A kept folder at a root folder's top level, such as `/media/TV/.<NAME>-originals`, shows in Library Import the same
  way. Plex skips it too.

### Links and copies

The hook keeps an original as a hard link. Where the file system refuses a link, as some Windows and network mounts
do, it keeps a copy instead.

- The copy is checked against the original before the change.
- The output says "copied" instead of "hard-linked".
- A copy needs the file's size plus 1 GB free in that folder. With less, nothing changes, and the output says why.

### How long they stay

The nightly audit and the worker's daily prune remove the kept originals and the grab links older than
`KEEP_ORIGINALS_DAYS`. They do this in every folder the hook kept a file in, also one below a root folder.

- `0` keeps nothing.
- At `0` a prune removes every grab link, and leaves the originals already kept.
- Move a kept original out of its folder to keep it longer.

### Undo a change

1. Move the kept original back over the file with `mv`.
2. Rescan the item in the app.

```
mv "/media/TV/.arr-media-guard-originals/<UTC time>/Show A/Season 1/Show A - S01E02.mkv" \
   "/media/TV/Show A/Season 1/Show A - S01E02.mkv"
```
