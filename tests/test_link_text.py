# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 samwiseg0
"""The item link of a Discord post (docs/design.md, "Link"). Made-up titles."""
import pytest

from test_report import IMPORT, MOVIES, decision, h

URL = f"{MOVIES}/movie/a-b"
# A live post of 2026-11-25: Discord formats markdown inside the text of a link and prints an escape there with its
# backslash. Each title with its field text. A title that Discord shows right as link text keeps the link. Every
# other title shows as plain text with its escapes, and a link with the app's name follows it.
TITLES = [("A*B*C*D S02E07", f"A\\*B\\*C\\*D S02E07 ([Radarr]({URL}))"),                    # the B shows in italics
          ("A\\*B\\*C\\*D S02E07", f"A\\\\\\*B\\\\\\*C\\\\\\*D S02E07 ([Radarr]({URL}))"),    # the backslashes show
          ("Show_Name_Test S01E01", f"Show\\_Name\\_Test S01E01 ([Radarr]({URL}))"),
          ("A~~B~~`C`||D|| S01E01", f"A\\~\\~B\\~\\~\\`C\\`\\|\\|D\\|\\| S01E01 ([Radarr]({URL}))"),   # strike, code, spoiler
          ("Tilde ~~Film~~", f"Tilde \\~\\~Film\\~\\~ ([Radarr]({URL}))"),                          # each character alone
          ("Back`tick` Film", f"Back\\`tick\\` Film ([Radarr]({URL}))"),
          ("Pipe ||Film||", f"Pipe \\|\\|Film\\|\\| ([Radarr]({URL}))"),
          ("Film (2000) [Extended] S01E01", f"[Film (2000) [Extended] S01E01]({URL})"),
          ("AC\\DC S01E01", f"[AC\\DC S01E01]({URL})"),
          ("**Bold** S01E01", f"\\*\\*Bold\\*\\* S01E01 ([Radarr]({URL}))"),
          ("The _Thing_ S01E01", f"The \\_Thing\\_ S01E01 ([Radarr]({URL}))"),
          ("Film http://x.invalid", f"Film http://x.invalid ([Radarr]({URL}))"),               # Discord shows no link then
          ("Foo [ Bar (2001)", f"Foo [ Bar (2001) ([Radarr]({URL}))"),                          # Discord ends a link at it
          ("Foo ] Bar [ (2001)", f"Foo ] Bar [ (2001) ([Radarr]({URL}))"),
          ("Film \\[Cut]", f"Film \\\\[Cut] ([Radarr]({URL}))"),                                # the backslash may escape the bracket
          ("Film AC\\", f"Film AC\\\\ ([Radarr]({URL}))")]                                      # the backslash may escape the end


@pytest.mark.parametrize("label, value", TITLES)
def test_a_title_keeps_its_link_only_where_discord_shows_it_as_written(settings, label, value):
    """Owner 2026-11-25, after the live post. The file name under the title keeps its escapes."""
    settings(radarr={"link": MOVIES})
    e = h.render(decision(label=label, path="/m/A_B*C.mkv", ids={"slug": "a-b"}), "embed")[0]
    assert e["fields"] == [IMPORT, {"name": "​", "value": f"{value}\nA\\_B\\*C.mkv", "inline": False}], e["fields"]


def test_a_title_with_no_url_names_the_field(settings):
    """No link base gives no link. The title names the field with its escapes, as before."""
    e = h.render(decision(label="A*B*C*D S02E07", path="/m/A.mkv", ids={"slug": "a-b"}), "embed")[0]
    assert e["fields"] == [IMPORT, {"name": "A\\*B\\*C\\*D S02E07", "value": "A.mkv", "inline": False}], e["fields"]


def test_the_audit_post_follows_the_same_rule(settings):
    """The nightly audit links each file's title the same way. Its problem text keeps its escapes. A plain title of
    more than two sentences stays on its line."""
    e = h.audit_embed("radarr", "Audit", [("A*B*C*D S02E07", "a_b", URL), ("Dr. Robo S01E01", "c", URL), ("Mr. X_Y", "d", URL)], "found")
    assert e["description"] == (f"A\\*B\\*C\\*D S02E07 ([Radarr]({URL})): a\\_b\n[Dr. Robo S01E01]({URL}): c\n"
                                f"Mr. X\\_Y ([Radarr]({URL})): d"), e["description"]


def test_the_link_names_the_instance_as_its_post_does(settings):
    """An instance of APP_INSTANCES names its own app in the link, as the footer of its post does."""
    h.ARR["radarr-4k"] = h.ARR["radarr"].__class__("radarr-4k")
    try:
        assert h.escaped(h.link("A*B*C*D", URL, "radarr-4k")) == f"A\\*B\\*C\\*D ([Radarr-4k]({URL}))"
    finally:
        del h.ARR["radarr-4k"]
