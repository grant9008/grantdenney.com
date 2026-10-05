# Music credits

Background songs for Level 1 Hollywood (src/audio/music.ts). All three are **CC0 1.0 (public domain)**: free for a
commercial game, no attribution required. They are credited here anyway, as a courtesy. Checked 2026-10-02: no
ContentID registration was found for any of them, so players' clips should not get claimed.

| File | Song | Artist | Source | License |
|---|---|---|---|---|
| midnight_cruiser.mp3 | Midnight Cruiser (album New Peach Radio) | Zane Little Music | https://opengameart.org/content/midnight-cruiser | CC0 1.0 |
| raspberry_jam.mp3 | Raspberry Jam | congusbongus | https://opengameart.org/node/118121 | CC0 1.0 |
| funky_hiphop_lofi_jam_lowpass.mp3 | Funky Hip Hop Lofi Jam (low-pass version) | omfgdude (OMF-Games) | https://opengameart.org/node/94882 | CC0 1.0 |

CC0: https://creativecommons.org/publicdomain/zero/1.0/

The game ships 96 kbps MP3 re-encodes (scripts/encode-music.mjs). The original downloads live in music-src/, which is
not in git. To add a song: put the file in music-src/, run the script with the dev server up, add the name to PLAYLISTS
in src/audio/music.ts, and add a row here with its license.
