# Re-grabs, restores and kept originals

When a file is broken, arr-media-guard (AMG) can delete it and have Sonarr or Radarr search for another copy. When the
broken file was an upgrade, AMG can also put back the old file that the upgrade replaced. Before AMG changes or replaces
a file, it keeps the old one for a while, so you can undo the change. [design.md](design.md) has the rules behind each
step.

## Re-grabs

A *re-grab* deletes a broken file through the app and marks its grab as failed, so the app searches for another copy. It
runs at import. Before it deletes anything, AMG checks the file a second time from scratch, and the second check must
find the same fault. AMG then checks the other files of the same download, such as the other episodes of a season pack.
It deletes each one that is broken too. The files that check clean stay.

`REGRAB` lists the kinds of fault that re-grab, joined by commas. The default is `audio,video`.

| Kind | The fault |
| --- | --- |
| `audio` | The audio that plays is broken. |
| `video` | The video is cut short or damaged. |
| `content` | The file holds another film or episode, by the TMDB and runtime checks, see [features.md](features.md#wrong-content). |
| `damage` | A conversion to MKV shows that the source file is damaged. |

- A kind that `REGRAB` leaves out still gets the second check. The file then stays, and the alert title ends "re-grab is
  off".
- `none` turns every re-grab off.
- AMG leaves out a kind it does not know, and a blank value re-grabs nothing. `--selftest` fails on both.
- The "Maybe the wrong episode" alert never re-grabs.

`REGRAB_CAP` limits the re-grabs of each Sonarr or Radarr instance to 30 in 24 hours by default. Every kind counts
toward the one limit, and one download counts once. Past the limit, AMG keeps the file and alerts. `0` turns every
re-grab off.

A manual import has no grab to mark as failed. When it replaced a file and `RESTORE` is on, AMG deletes the broken file
and puts the old one back. When no old file comes back, AMG asks the app to search, if the item was monitored. A manual
import that replaced nothing stays, and an alert says the app has no record of grabbing it.

A re-grab goes to the decision log only, and so does an old file put back that the app picked up again. When AMG deleted
a file, the other alerts of that file go to the log only too. Every fault that AMG did not re-grab posts an alert, as
when a setting or the limit stopped it. A restore whose old files the app did not pick up posts too. So does a deleted
file whose item is not monitored, because no search started.

## Restore after a bad upgrade

With `RESTORE=true`, the default, a re-grab of a broken upgrade puts back the old file from the app's recycle bin. The
old file comes back only when it checks clean. It comes back by a rename on the same file system, never a copy. Its
subtitles, `.nfo` and image files come back with it. AMG then has the app scan the item, and checks that the app lists
the old file again.

1. Turn on the recycle bin in each app, on the same file system as the media.
2. In Docker, mount the bin at the app's path, or add a pair for it to the app's path map. See
   [docker.md](docker.md#path-maps).
3. Run `--selftest`, or press Test in the app's connection. Both warn when the bin is off, out of AMG's reach, or on
   another file system than the media.

When an app's root folders are on more than one file system, one recycle bin cannot sit beside them all. The warning
then says to set `KEEP_REPLACED=true`, which keeps each copy on the file system of its own root folder. With
`KEEP_REPLACED` on, the app's recycle bins are optional. With `KEEP_ORIGINALS_DAYS` at 0 the warning stays quiet, and a
restore from a root folder away from the bin is up to you.

## Keep replaced files

With `KEEP_REPLACED=true`, AMG keeps its own copy of each file a grab may replace. When the app grabs a release, AMG
hard-links the library files the release may replace into the folder `.<NAME>-recycle`. Their subtitles, `.nfo` and
images go with them. A restore uses this copy when the app's recycle bin has none it can use. AMG never uses a copy of a
file that changed after the grab.

1. Set `KEEP_REPLACED='true'` in the env file.
2. The app's connection to AMG must send **On Grab**, beside On File Import and On File Upgrade. AMG turns it on there
   itself, at the listener's start and in the nightly audit, and logs a line. The Test button and `--selftest` never
   turn it on, because the app saves the connection after its Test.
3. Run `--selftest`. It warns, and so does Test, when the connection sends no Grab, or when AMG cannot hard-link a file
   on a mount.

- AMG keeps nothing on a file system that refuses hard links, such as some network shares, and never copies there.
- A hard link keeps the old file's disk space in use after the app deletes the file. AMG removes each copy after
  `KEEP_ORIGINALS_DAYS`, in the nightly audit or the worker's daily clean-up.
- With `KEEP_ORIGINALS_DAYS` at 0, AMG keeps nothing.
- A value other than `true` or `false` keeps nothing, and `--selftest` fails on it.

## Kept originals

When AMG replaces a file with a changed one, it keeps the old file as a *kept original*. That covers a header repair, a
subtitle change in the file, and a `.srt` file that AMG moves aside or writes again. A conversion to MKV keeps the old
file only when you forced it with `--force-convert`, or when it left a subtitle out. Every other conversion keeps
nothing, because AMG checks every stream of the new file before the old one goes.
[Force a conversion](commands.md#force-a-conversion) says how to force one.

AMG keeps each one for `KEEP_ORIGINALS_DAYS` days, 7 by default.

| Folder | What it holds |
| --- | --- |
| `.<NAME>-originals` | The kept originals, at `<folder>/.<NAME>-originals/<UTC time>/<path from the folder>`. |
| `.<NAME>-recycle` | The copies `KEEP_REPLACED` keeps at a grab. |
| `.<NAME>-convert` | Beside a video. It holds the new file while AMG writes it, and during a conversion also the old file and its extras. |

`NAME` is `arr-media-guard` by default, so the first folder is `.arr-media-guard-originals`.

### Where the folders go

AMG puts `.<NAME>-originals` and `.<NAME>-recycle` at the top of each mount, when the app user can write there. In
Docker that user is `PUID:PGID`.

- When that user cannot write the top of the mount, AMG takes the highest folder on the file's path that it can write.
  That can be a show's folder. This happens at the root of a Docker volume, for example.
- A folder of that name that already exists on the path wins, so the place stays the same from run to run.
- When no folder on the path is writable, AMG skips every header repair and subtitle change of the file. A grab then
  keeps nothing. Create the folders at the top of the mount, and give them to the app user.

### In the apps and Plex

- Radarr leaves a hidden folder out of its root folder list.
- Sonarr lists a recycle bin such as `.recycle` as an unmapped folder in Library Import. Do not import it. Its disk scan
  never reads it.
- A kept folder at the top of a root folder, such as `/media/TV/.<NAME>-originals`, shows in Library Import the same
  way.
- Each kept folder holds a `.plexignore` with the pattern `*`, so Plex skips everything in it. AMG writes it once, and
  never writes over a `.plexignore` that is already there.

### Links and copies

AMG keeps an original as a hard link, which takes no extra space. Where the file system refuses a link, as some Windows
and network mounts do, it keeps a copy instead.

- AMG compares the copy with the original before it changes anything.
- The output says "copied" in place of "hard-linked".
- A copy needs the file's size plus about 1 GB free in that folder. With less, AMG changes nothing, and the output says
  why.

### How long they stay

The nightly audit and the worker's daily clean-up remove the kept originals and the `KEEP_REPLACED` copies older than
`KEEP_ORIGINALS_DAYS`. They do this in every folder AMG kept a file in, also one below a root folder.

- `0` keeps nothing new.
- At `0` a clean-up removes every `KEEP_REPLACED` copy, and leaves the originals already kept.
- Move a kept original out of its folder to keep it longer.

### Undo a change

1. Move the kept original back over the file with `mv`.
2. Rescan the item in the app.

```
mv "/media/TV/.arr-media-guard-originals/<UTC time>/Show A/Season 1/Show A - S01E02.mkv" \
   "/media/TV/Show A/Season 1/Show A - S01E02.mkv"
```
