# The policy file

The policy file says which audio and which subtitles play first. arr-media-guard (AMG) reads it from `POLICY_FILE`, by
default `/etc/arr-media-guard.policy.json`. In Docker it is `/config/policy.json`.

- A missing key is an error. AMG never takes a silent default.
- Without the file, AMG edits nothing and alerts once.
- Without the file, `--selftest` fails and prints the command that creates it.

## Example

This is [examples/policy.json](../examples/policy.json), the file the install copies.

```json
{
  "kids": {
    "genres": {"radarr": ["Family"], "sonarr": ["Children", "Family"]},
    "profiles": ["Kids"],
    "studios": ["Studio A"]
  },
  "audio": {
    "english": ["original"],
    "foreign": ["original", "english"],
    "foreign_kids": ["english", "original"]
  },
  "subtitles": {
    "english": ["forced"],
    "foreign": ["full", "sdh", "forced", "dub"]
  },
  "sparse_events": 1.5,
  "forced_flag_events": 4.0,
  "density_min_minutes": 15,
  "min_confidence": 0.7,
  "forced_clear": {"events": 10.0, "english_only_audio": true, "reference_ratio": 0.8}
}
```

## Keys

| Key | What it does |
| --- | --- |
| `kids` | What makes a foreign item a kids title. `genres`: per app, the Radarr or Sonarr genres of a kids title. `profiles`: Radarr quality profile names. `studios`: Radarr studio names. Sonarr reads only `genres`. An item originally in English never counts as a kids title. |
| `audio` | The audio languages to try, in order, for each item class (`english`, `foreign` and `foreign_kids`). `original` is the movie's or series' original language, as Radarr or Sonarr lists it. `english` is English. The first one the file has plays first. See [Which audio plays](#which-audio-plays). |
| `subtitles` | Which English subtitle plays first, by the language of the audio that plays. `english`: under English audio, only an English subtitle in one of these roles may stay on, normally `forced` only. `foreign`: under audio in another language, AMG turns on the first English subtitle in this role order and turns every other subtitle off. With no English subtitle in these roles, only a forced or commentary subtitle loses its default. The roles are these. `full` holds all dialogue. `sdh` holds all dialogue and the sounds, for deaf and hard-of-hearing viewers. `forced` shows only signs and foreign-language parts. `dub` is a transcript of an English dub. |
| `sparse_events` | A subtitle track with fewer lines a minute than this counts as forced. AMG counts the lines from the statistics mkvmerge writes into the file. |
| `forced_flag_events` | A track with a forced flag counts as forced only below this many lines a minute. A denser track counts as full dialogue, unless its title says forced. |
| `density_min_minutes` | AMG counts lines a minute only in a file of at least this many minutes. In a shorter file, `sparse_events` and `forced_flag_events` do not apply. |
| `min_confidence` | When a file has no audio in the item's original language, English audio plays first only when AMG is at least this sure the track is English. A tag alone gives 0.6. A tag that the track title or the heard language confirms gives 1.0. "English" or "Dubbed" in the release name also makes it sure. Below it, AMG changes nothing and logs the file as undecided. |
| `forced_clear` | Under English audio, a forced-flagged English full or SDH subtitle that holds the full dialogue loses its forced flag. `events`: the least lines a minute it needs. `english_only_audio`: at `true`, English must be the only main audio language. `reference_ratio`: beside other full or SDH subtitles, it needs at least this share of their median lines a minute. An English original also needs TMDB to list English as its only spoken language. |

## Which audio plays

AMG puts each movie or series in one of three classes, and the class picks its list under `audio`. The *original
language* is the one Radarr or Sonarr lists for the movie or series.

- `english`: the original language is English.
- `foreign_kids`: the original language is another language, and the item is a kids title. The `kids` key says what
  makes a kids title. Radarr checks the movie's genres, quality profile and studio. Sonarr checks the series' genres.
- `foreign`: every other item. That includes an item whose original language the app does not list, which is never a
  kids title.

Each list holds `original`, `english` or both. `original` is the original language, and `english` is English. AMG tries
the entries in order. The first language the file has a main audio track in plays first. A commentary or an audio
description never counts. Of several tracks in that language, the one that plays now stays, else the one with the most
channels.

- When no entry matches, AMG changes no default flag.
- When the `original` entry finds no track and a main audio track is untagged, that track may be the original one. AMG
  then changes nothing and logs the file as undecided with that reason. This holds for `foreign` and `foreign_kids`.
- When the file has no track in the original language, English plays only when its language is sure. A bare `eng` tag
  is not enough, unless the release name says English or dubbed, see `min_confidence`.

The default plays the original audio first on foreign titles, and English first on foreign kids titles.

```json
"audio": {
  "english": ["original"],
  "foreign": ["original", "english"],
  "foreign_kids": ["english", "original"]
}
```

To play an English dub before the original audio on foreign titles too, set `"foreign": ["english", "original"]`. A
file with no English audio then still plays its original audio. `foreign_kids` does this already by default.

The subtitles follow the audio that plays.

- Under English audio, only an English subtitle in a role that `subtitles.english` lists may play by default. The
  default lists `forced` only, so only a forced English subtitle may play.
- Under other audio, `subtitles.foreign` ranks the roles. The best English subtitle the file has in one of them plays
  by default.

The Original language flag is separate from what plays first. It marks the audio in the original language as original
also when an English dub plays first. Like `min_confidence`, it needs more than a tag, so AMG hears the audio first, see
[features.md](features.md#original-language-flag).

## When a change takes effect

New imports use the changed policy. So do the files you run a backfill on by hand, see
[commands.md](commands.md#dry-runs-and-the-backfill). Files AMG already checked are not checked again on their own.

- In Docker, the listener reads the policy when it starts. Restart the container after you change it.
- `--selftest` checks that your policy loads.
- The tests check the rules against a fixed copy of the example policy, in `tests/test_arr_decide.py`, so your changes
  never fail them.
