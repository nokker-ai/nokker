---
name: video-shots
description: Render each storyboard shot with ffmpeg in the sandbox
---
# Rendering shots

All commands run in the sandbox (`terminal`, or `process_start` for anything longer than a minute, then
`process_logs`). Work inside `video/<slug>/`; write shots to `video/<slug>/shots/`.

## 0. Check the tools

```
ffmpeg -hide_banner -version | head -n 1
ffmpeg -hide_banner -encoders | grep -E "nvenc|qsv|vaapi|libx264"
```

Use a hardware encoder if one is listed (`h264_nvenc`, `h264_qsv`, `h264_vaapi`), else `libx264`. Say which one
you use. If ffmpeg is missing, stop and tell the user: do not install packages on your own.

## 1. One format for every shot

Every shot leaves this step with the same size, frame rate, pixel format and aspect, or the edit will fail:

```
-vf "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p"
```

(Swap 1920:1080 for 1080:1920 or 1080:1080.) Shots carry no audio at this stage (`-an`).

## 2. Kinds of shots

- **Still image with slow zoom** (photo, screenshot), 5 seconds:
  ```
  ffmpeg -y -loop 1 -i assets/photo.png -t 5 -vf "scale=3840:-2,zoompan=z='min(zoom+0.0008,1.12)':d=150:s=1920x1080:fps=30,setsar=1,format=yuv420p" -an -c:v libx264 -crf 18 shots/s03.mp4
  ```
- **Clip cut** (from a recording): `-ss <start> -t <duration>` before `-i` for speed, then the format filter.
- **Title card** on a brand colour, text centred:
  ```
  ffmpeg -y -f lavfi -i "color=c=0x0F5C5C:s=1920x1080:d=4:r=30" -vf "drawtext=text='Product name':fontcolor=white:fontsize=96:x=(w-text_w)/2:y=(h-text_h)/2,format=yuv420p" -an -c:v libx264 -crf 18 shots/s01.mp4
  ```
  Use a font file that is in the project (`fontfile=assets/fonts/<file>.ttf`); escape `:` and `'` in the text.

## 3. Check every shot

```
ffprobe -v error -select_streams v:0 -show_entries stream=width,height,r_frame_rate,pix_fmt -show_entries format=duration -of csv=p=0 shots/s01.mp4
```

Compare with `shots.csv`. Re-render a shot that differs; never stretch a shot in the edit to hide an error.

## Rules

- Never overwrite the user's assets; read from `assets/`, write to `shots/`.
- Long renders: `process_start` with a clear name, check `process_logs`, `process_stop` when done or stuck.
