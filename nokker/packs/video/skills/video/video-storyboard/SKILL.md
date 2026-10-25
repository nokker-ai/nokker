---
name: video-storyboard
description: Turn a brief into a script and a shot-by-shot storyboard
---
# Storyboard

Everything downstream (shots, edit, mix) reads the files written here. Get them right before rendering anything.

## Brief

Ask or extract: goal (one sentence), audience, channel and format (16:9 1920x1080, 9:16 1080x1920, 1:1
1080x1080), target length, language, voice-over or captions only, music mood, assets the user has (logos,
screenshots, clips, photos) and where they are in the project folder.

## Script

Write the voice-over or on-screen text first, then time it: about 2.5 words per second for a calm voice. If the
script is longer than the target length, cut words, not speed.

## Storyboard files

Create a project folder `video/<slug>/` with:

- `brief.md`: the brief as agreed.
- `script.md`: the final text, one paragraph per shot.
- `shots.csv` with one line per shot:

```
id,start,duration,visual,source,on_screen_text,voice,transition
s01,0.0,4.0,title card on brand background,generated,Product name,"Meet ...",fade
s02,4.0,6.0,screen recording of the dashboard,assets/dashboard.mp4,,"Every morning ...",cut
```

Rules for shots: 2 to 8 seconds each; one message per shot; on-screen text of at most 7 words, readable for at
least 2 seconds; keep titles inside the safe area (10 percent margin, more for 9:16 where the interface covers the
bottom fifth).

## Review

Show the user the table of shots with total length. Change nothing downstream until they approve it.
