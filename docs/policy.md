# The policy file

The policy file says which audio and which subtitles play first. The hook reads it from `POLICY_FILE`, by default
`/etc/arr-media-guard.policy.json`. In Docker it is `/config/policy.json`.

- A missing key is an error. The hook never takes a silent default.
- Without the file, the hook edits nothing and alerts once.
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
| `kids` | What makes a foreign title a kids title: an app genre, a Radarr quality profile name, or a studio. |
| `audio` | Per item class, the audio to try in order. `original` is the app's original language. |
| `subtitles` | Which English subtitle may play, by the audio that plays. `english` lists the roles allowed under English audio, normally `forced` only. `foreign` ranks the roles for other audio, and the file's best match turns on. The roles are `full`, `sdh`, `forced` and `dub`. |
| `sparse_events` | An English subtitle under this many events a minute counts as forced. |
| `forced_flag_events` | A forced flag counts only under this many events a minute. |
| `density_min_minutes` | A shorter file gives no reliable events a minute. |
| `min_confidence` | The file decides the language only at this confidence. A tag alone is 0.6. |
| `forced_clear` | A forced English subtitle that holds the full dialogue loses its forced flag under English audio. |

`--selftest` checks that your policy loads. The tests check the rules against a fixed copy of the example policy, in
`tests/test_arr_decide.py`, so your changes never fail them.

In Docker, the listener reads the policy when it starts. Restart the container after you change it.
