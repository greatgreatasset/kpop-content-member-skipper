# kpop-content-member-skipper

Personal tool: pre-scan YouTube videos of a K-pop group and find the time
ranges where only the members you want to skip are on screen, so a Firefox
extension can jump over them. Members you want to see more of are recorded as
highlights. Each group is a **profile**; the first one is Hearts2Hearts
(skip Carmen and Ye-on, highlight Ian).

## Setup (Windows, NVIDIA GPU)

```
setup.bat
```

This creates `.venv`, installs `requirements.txt` and `requirements-dev.txt`,
and checks that the CUDA provider loads. `onnxruntime-gpu[cuda,cudnn]` pulls
the CUDA/cuDNN DLLs from pip, so no system CUDA install is needed. insightface
also drags in the CPU `onnxruntime` package, which shadows the GPU build;
`setup.bat` removes it and reinstalls the GPU build. ffmpeg/ffprobe must be on PATH. The InsightFace
`buffalo_l` model downloads to `~/.insightface` on first run.

## Profiles (`profiles/<id>.json`)

```json
{"id": "hearts2hearts", "group": "Hearts2Hearts",
 "photosDir": "~/Pictures/Hearts2Hearts",
 "members": ["jiwoo", "carmen", "yuha", "stella", "juun", "a-na", "ian", "ye-on"],
 "names": {"ye-on": "Ye-on", "...": "..."},
 "skip": ["carmen", "ye-on"], "highlight": ["ian"],
 "usePhotosOf": ["ye-on"], "photosPerMember": 2000}
```

`photosDir` holds one subfolder of photos per member (folder name = member id).
To point it somewhere else on your machine without committing the path, put
`{"photosDir": "D:/photos/h2h"}` in the gitignored `profiles/<id>.local.json`,
or set `KMS_PHOTOS_<ID>` (e.g. `KMS_PHOTOS_HEARTS2HEARTS`).

**Adding a group:** copy the profile, change the members/skip/highlight lists,
collect photos per member, run `build_refs.py --profile <id>`, then scan videos
with `--profile <id>`. Run `sync_skiplists.py` so the extension lists it.

## Usage

```
python build_refs.py [--profile hearts2hearts]   # photos -> data/<profile>/refs.npz (~7 min)
scan.bat <youtube url> [--profile hearts2hearts] # scan + refresh the extension's bundled lists
python scan.py <youtube url> [--profile ...]     # scan only -> skiplists/<profile>/<videoId>.json
python eval_test.py --detail                     # compare against tests/labels_*.json
```

Photos, embeddings, videos and caches live in the gitignored `data/` and
`cache/` folders; only code, profiles and `skiplists/` are committed. Face
detections are cached per video (`cache/<id>.faces.npz`) and shared by all
profiles, so re-scanning a video for another profile or with new rules takes
seconds.

## Skip list format (`skiplists/<profile>/<videoId>.json`)

```json
{"format": 2, "videoId": "oVMQTIUI_2c", "profile": "hearts2hearts", "group": "Hearts2Hearts",
 "title": "...", "duration": 1672, "scannedAt": "...", "scannerVersion": "0.3.0",
 "skipMembers": ["Carmen", "Ye-on"],
 "skips": [{"start": 4.5, "end": 23.17}],
 "highlights": [{"member": "ian", "name": "Ian", "ranges": [{"start": 23.67, "end": 28.0}]}]}
```

`skiplists/<profile>/index.json` lists the scanned videos of each profile.

## How a frame is labelled

Each detected face (short side >= `--min-face` px) is matched to a member by
mean similarity to its k nearest reference photos; below the threshold it is
"unknown". Face tracking fills in faces that are unknown on their own (e.g.
turned away for a moment) from the same face in neighbouring frames.

A frame is SKIP when at least one skip-member is recognized and no other member
is. These don't block a skip:
- unknown faces (backup dancers, crew)
- faces much smaller than the frame's main face (members in the background)
- members whose face is cut off by the frame edge (only partly in the scene)

Frames with no recognized member inherit from their neighbours across short
gaps. Then a median filter, gap merge (< 1 s) and minimum length (2 s) are
applied, and each skip is trimmed 0.5 s inward at both ends (dropped if under
1.5 s) so it never cuts into someone else's part. Highlights are the ranges
where a highlighted member is on screen, minus the skipped ranges.

## Firefox extension (`extension/`)

What it does on youtube.com/watch pages:

- jumps over the skip ranges (never while an ad is playing) and shows a
  "Skipped 0:04 → 0:23" toast with **Undo**; Undo jumps back and leaves that
  range alone until you open another video
- markers on the progress bar for the highlighted member's parts (pink for the
  first highlighted member), muted striped markers for skipped ranges
- a button in the player controls (**Ian ⏭** for Hearts2Hearts) and
  **Alt+Shift+H** jump to the next highlighted part (rebind under
  about:addons → ⚙ → Manage Extension Shortcuts)
- toolbar popup: skipping on/off, highlights on/off, and e.g.
  "10 skips (… s) · 22 Ian parts" for the current video (or "not scanned yet")
- settings page: profile (group), skip list base URL and cache time. Lists are
  fetched from
  `https://raw.githubusercontent.com/greatgreatasset/kpop-content-member-skipper/master/skiplists/<profile>/<videoId>.json`
  by default, and the copy bundled in the extension is used when a video has
  no list online. A missing list just means no skips.

### Loading it in Firefox

1. Open `about:debugging` → **This Firefox** → **Load Temporary Add-on…**
2. Pick `extension\manifest.json`.
3. Open a scanned video, e.g. https://www.youtube.com/watch?v=oVMQTIUI_2c

Temporary add-ons are removed when Firefox restarts, so repeat this after a
restart (after `scan.bat`, just click **Reload** on the same page).
Alternatively, `cd extension` and run `npx web-ext run` to launch a separate
Firefox profile with it installed.

### Permanent install for friends

Release Firefox only installs signed add-ons. To share it, sign it as a
self-distributed (unlisted) add-on: create an API key at
addons.mozilla.org → Developer Hub → Manage API Keys, then
`npx web-ext sign --channel=unlisted --api-key=... --api-secret=...` in
`extension/`. That produces a signed `.xpi` friends can install by opening it
in Firefox. (Not done yet; nothing has been signed or uploaded.)

### Checks

```
cd extension && npx web-ext lint                 # manifest/code lint
.venv\Scripts\python tests\browser_test.py       # end-to-end in headless Firefox
```

`browser_test.py` (needs `pip install -r requirements-dev.txt`) installs the
add-on into a fresh temporary Firefox profile, opens the test video, and checks
markers, the skip, toast/Undo, the next-highlight button, the popup, the
settings page and in-page navigation.
