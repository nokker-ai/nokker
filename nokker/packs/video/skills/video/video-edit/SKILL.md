---
name: video-edit
description: Assemble shots with transitions and captions in ffmpeg
---
# Edit

Input: the shots of `video/<slug>/shots/`, all with the same format (see `video-shots`). Output:
`video/<slug>/edit/picture.mp4` (no audio) and `video/<slug>/edit/captions.srt`.

## Straight cuts

Write `edit/list.txt` in shot order, then use the concat demuxer (no re-encode needed when formats match):

```
file '../shots/s01.mp4'
file '../shots/s02.mp4'
```

```
ffmpeg -y -f concat -safe 0 -i edit/list.txt -c copy edit/picture.mp4
```

## Transitions

For a fade between two shots, use `xfade`; the offset is the length of the first shot minus the transition:

```
ffmpeg -y -i shots/s01.mp4 -i shots/s02.mp4 -filter_complex "[0:v][1:v]xfade=transition=fade:duration=0.5:offset=3.5,format=yuv420p" -an -c:v libx264 -crf 18 edit/s01-s02.mp4
```

Chain more shots by feeding the result into the next `xfade`, adding each new offset. Keep transitions short
(0.3 to 0.7 s) and use them only where `shots.csv` says so.

## Captions

1. Build `edit/captions.srt` from `script.md` and the shot timings: one or two lines per caption, 42 characters
   per line at most, at least 1 second on screen.
2. Burn them only if the channel needs it (most social feeds autoplay muted):
   ```
   ffmpeg -y -i edit/picture.mp4 -vf "subtitles=edit/captions.srt:force_style='FontSize=28,MarginV=60'" -c:v libx264 -crf 18 edit/picture-captioned.mp4
   ```
   Otherwise keep the `.srt` to upload as a separate caption track.

## Check

Total duration within 0.2 s of the storyboard; look at three frames (start, middle, end) by extracting them:
`ffmpeg -y -ss 00:00:05 -i edit/picture.mp4 -frames:v 1 edit/frame-05.png`. Show them to the user before mixing.
