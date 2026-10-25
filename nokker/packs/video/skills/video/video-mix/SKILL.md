---
name: video-mix
description: Mix voice and music, set loudness, encode the final file
---
# Audio mix and final encode

Inputs: `edit/picture.mp4`, a voice track (`audio/voice.wav`, recorded or synthesised by the user), a music track
(`audio/music.mp3`). Output: `video/<slug>/out/<slug>-<format>.mp4`.

## Music licence first

Use only music the user owns or that is licensed for this use (and note the licence in `brief.md`). If the
licence is unknown, stop and ask: a takedown later costs more than a quiet video.

## Ducking and loudness

Lower the music under the voice, then normalise the whole mix:

```
ffmpeg -y -i audio/voice.wav -i audio/music.mp3 -filter_complex "[1:a]volume=0.35[m];[m][0:a]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[duck];[0:a][duck]amix=inputs=2:duration=first:dropout_transition=2,loudnorm=I=-14:TP=-1.5:LRA=11[a]" -map "[a]" -ar 48000 -c:a pcm_s16le audio/mix.wav
```

Targets: -14 LUFS integrated and -1.5 dBTP for social and web; -16 LUFS for podcasts; -23 LUFS for broadcast.
Measure after mixing: `ffmpeg -i audio/mix.wav -af loudnorm=print_format=summary -f null -`.

## Final encode

```
ffmpeg -y -i edit/picture.mp4 -i audio/mix.wav -map 0:v -map 1:a -c:v libx264 -preset slow -crf 20 -pix_fmt yuv420p -c:a aac -b:a 192k -movflags +faststart -shortest out/<slug>-16x9.mp4
```

With a hardware encoder, replace `-c:v libx264 -preset slow -crf 20` by its own quality setting (for example
`-c:v h264_nvenc -preset p5 -cq 21`). Run long encodes with `process_start` and follow `process_logs`.

## Final checks

1. `ffprobe` on the output: duration, size, frame rate, audio present at 48 kHz.
2. Loudness summary within 1 LU of the target.
3. Extract a frame every ten seconds and look at them; listen to the first and last five seconds.
4. Tell the user the file path, its size and what was checked. Publishing is not part of this pack; with the
   social pack, scheduling the post goes through its approval card.
