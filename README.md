# kpop-content-member-skipper

Personal tool: pre-scan YouTube videos of K-pop groups and find the time
ranges where only the members you want to skip are on screen, so a Firefox
extension can jump over them. Members you want to see more of are recorded as
highlights. Each group is a **profile**: Hearts2Hearts (skip Carmen and Ye-on,
highlight Ian) and ILLIT (skip Yunah). Nobody picks a group: every video is
checked against all members of all groups, and its skip list says which groups
are in it.

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

`photosDir` holds one subfolder of photos per member (folder name = member id;
other folders, e.g. group photos, are ignored). To point it somewhere else on
your machine without committing the path, put `{"photosDir": "D:/photos/h2h"}`
in the gitignored `profiles/<id>.local.json`, or set `KMS_PHOTOS_<ID>` (e.g.
`KMS_PHOTOS_HEARTS2HEARTS`). `highlight` may be empty (ILLIT has none).

### Adding a group

1. **Photos:** one folder per member, named by member id, e.g.
   `~/Pictures/ILLIT/yunah`, `.../minju`, ... (a couple of thousand photos each
   is plenty; 2000 per member are sampled).
2. **Profile:** copy `profiles/illit.json` to `profiles/<id>.json` and set `id`,
   `group`, `photosDir`, `members` (the folder names), `names`, `skip` and
   `highlight`.
3. **References:** `.venv\Scripts\python build_refs.py --profile <id>` (about
   4 minutes for 5 members). It prints the holdout accuracy and confusion
   matrix of the group, then checks all groups together (most similar members
   across groups, cross-group mistakes) and saves the joint "unknown" threshold
   to `data/gallery.json`. Re-run `build_refs.py --joint` alone after deleting a
   group.
4. **Extension:** `.venv\Scripts\python sync_skiplists.py` (updates the groups the
   popup and settings page list), commit the profile, raise the extension
   version and run **sign.bat**.

From then on every scan also looks for the new group; nothing else changes.
Videos scanned earlier don't know about it until they are scanned again (fast:
detections are cached).

## Scanning a video

Double-click **scan.bat**, paste the YouTube link (youtube.com/watch, youtu.be
or shorts) and press Enter. It scans the video (about 1.5 minutes for a
30-minute video), then commits **only** that video's skip-list files and pushes
them to GitHub. Each stage shows one updating line:

```
1/3 Downloading video...  45%  (12.3 MB/s)
2/3 Scanning faces...  [######----]  62%  1:58 / 3:13  ETA 0:04
3/3 Uploading skip list...
```

and it ends with e.g. `Done: Hearts2Hearts: 10 skips (176 s), 22 Ian parts.` and "Press any
key to close" (one video per run; a video scanned before skips stage 2's work:
"using earlier scan of this video").
Refresh the YouTube tab; the extension checks GitHub first, so no add-on reload
is needed (GitHub can take a minute or two to serve a new file). If the upload
fails, the window says so; the list is still committed locally, so
`git push origin master` later, or reload the add-on to use the bundled copy.

From a terminal (cmd.exe, in this folder):

```
scan.bat https://www.youtube.com/watch?v=oVMQTIUI_2c
scan.bat https://youtu.be/gNUMxMuxH20
scan.bat gNUMxMuxH20
scan.bat "https://www.youtube.com/watch?v=oVMQTIUI_2c&t=30s"   (quotes needed: link has &)
scan.bat <link> [--profile illit] [scan.py options]
```

No group needs to be named: every group is looked for. `--profile` (one id or
several, comma-separated) only limits the scan to those groups.

Any common link form works (youtube.com/watch?v=, m./music. hosts, youtu.be,
shorts/live/embed, extra `&t=`/`&list=` parts) as does the bare 11-character
video ID. Links containing `&` must be quoted on the command line, because
cmd.exe splits the line at `&` before scan.bat runs; the double-click paste
prompt takes them as they are. A failed download prints one short line
(`Couldn't download the video: ...`); add `--debug` for the full traceback.
`--no-push` (or `set KMS_NO_PUSH=1`) commits the skip list without uploading.
scan.bat itself only starts `scan_cli.py`, which holds all the logic.

### From Firefox: "Filter this video"

On this PC the add-on can start the scan itself. Once, double-click
**install_native_host.bat** (per user, no admin): it registers the scanner with
Firefox under `HKCU\Software\Mozilla\NativeMessagingHosts\kpop_content_member_skipper`
and writes the gitignored `native_host.local.json` (the paths on this PC).
`install_native_host.bat /uninstall` removes both. Reload the add-on afterwards.

Then on a YouTube video, click **Filter this video** in the add-on's popup (or
under the title of a video that isn't filtered yet). It runs the same scan,
commit and upload as scan.bat. A small banner on the video shows
"Filtering… 62%, about 0:20 left"; when it ends, the skips apply right away
(without waiting for GitHub) and it shows e.g. "Done: Ian 25% · Skipped 17%".
The popup can be closed meanwhile; the scan keeps running even if Firefox
closes, and clicking again for the same video shows the running scan instead of
starting another. Scans log to `cache/jobs/<videoId>.log`.

On PCs without the scanner (friends' PCs) the button is replaced by "Scanning
is only available on the PC with the scanner installed".

How it works: Firefox starts `native_host.bat` (venv Python, `native_host.py`)
over native messaging; it starts a separate worker process for the scan (using
`scan_cli.py`'s scan and publish functions) that writes its progress to
`cache/jobs/<videoId>.json`, and relays that to the add-on.

## Usage (individual steps)

```
python build_refs.py [--profile illit]           # photos -> data/<profile>/refs.npz (~4 min) + joint check
python build_refs.py --joint                     # only the joint check of all groups -> data/gallery.json
python scan.py <link or video id> [--profile ...] # scan only -> skiplists/<videoId>.json
python sync_skiplists.py                         # refresh the extension's bundled copies
python publish_skiplists.py [<videoId>]          # commit + push only that video's skip list
python eval_test.py --detail [--profile ...]     # compare against tests/labels_*.json
python debug_frames.py <videoId> 1:46 2:18 ...   # annotated frames -> cache/debug/
```

Photos, embeddings, videos and caches live in the gitignored `data/` and
`cache/` folders; only code, profiles and `skiplists/` are committed. Face
detections are cached per video (`cache/<id>.faces.npz`), so re-scanning a
video after adding a group or changing the rules takes seconds.

## Skip list format (`skiplists/<videoId>.json`)

One list per video, for whichever groups are in it:

```json
{"format": 3, "videoId": "oVMQTIUI_2c", "title": "...", "duration": 1672,
 "scannedAt": "...", "scannerVersion": "0.4.0",
 "groups": [{"id": "hearts2hearts", "name": "Hearts2Hearts", "faces": 7453}],
 "skipMembers": [{"member": "carmen", "name": "Carmen", "group": "Hearts2Hearts", "profile": "hearts2hearts"}, ...],
 "skips": [{"start": 4.5, "end": 23.17}],
 "highlights": [{"member": "ian", "name": "Ian", "group": "Hearts2Hearts", "profile": "hearts2hearts",
                 "ranges": [{"start": 23.67, "end": 28.0}]}]}
```

`groups` are the groups found in the video (with the number of confidently
recognized faces), `highlights` only lists highlighted members who have parts
in it. `skiplists/index.json` lists every scanned video with its groups.
The per-group folder `skiplists/hearts2hearts/` holds the format-2 lists from
before 0.5.0; add-on versions before 0.5.0 still read them, and 0.5.0 reads
them only when a video has no list of its own. It can be deleted once
everyone is on 0.5.0.

## How a frame is labelled

Each detected face (short side >= `--min-face` px) is compared with every
member of every group (one joint gallery of all `data/<profile>/refs.npz`) by
mean similarity to her k nearest reference photos.

1. **Which groups are in the video:** a group counts when at least 20 faces,
   and 5% of all confidently recognized faces, match its members (the group
   with the most always counts). This uses the joint "unknown" threshold
   (`data/gallery.json`, the 99th percentile of impostor scores over all groups).
2. Each face is then matched only among the members of those groups, with the
   threshold of just those groups (one group: that group's own threshold from
   build_refs, so adding a group doesn't change how a Hearts2Hearts video is
   labelled). Below the threshold a face is "unknown".

Face tracking fills in faces that are unknown on their own (e.g. turned away
for a moment) from the same face in neighbouring frames.

A frame is SKIP when at least one skip-member (of any group) is recognized and
every other recognized member is a skip-member too. These don't block a skip:
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
- when the video has a highlighted member's parts: a button in the player
  controls (**Ian ⏭** for Hearts2Hearts) and **Alt+Shift+H** jump to the next
  one (rebind under about:addons → ⚙ → Manage Extension Shortcuts). Videos
  without highlights (e.g. ILLIT) have no button.
- under the video title, only the parts that apply: e.g. "**Ian: 29%** ·
  Skipped: 27%" for Hearts2Hearts, "Skipped: 12%" for ILLIT (share of the
  video's length; follows the on/off switches), or "Not filtered yet"
- toolbar popup: who is skipped, e.g. "Skipping Yunah (ILLIT)" (without a
  scanned video: every group's skip-members), skipping on/off, highlights
  on/off, and e.g. "10 skips (… s) · 22 Ian parts" for the current video
- settings page: the groups it knows, skip list base URL and cache time.
  There is no group to pick. Lists are fetched from
  `https://raw.githubusercontent.com/greatgreatasset/kpop-content-member-skipper/master/skiplists/<videoId>.json`
  by default (falling back to the pre-0.5.0 `skiplists/hearts2hearts/<videoId>.json`),
  and the copy bundled in the extension is used when a video has no list
  online. A missing list just means no skips; "not scanned" answers are only
  cached for a minute, so newly published lists show up quickly.

### Installing it permanently (and sharing it)

Normal Firefox only keeps add-ons that Mozilla has signed. Signing is free and
"unlisted": the add-on is not shown in Mozilla's add-on store.

1. Once: sign in at https://addons.mozilla.org/developers/ → **Tools** →
   **Manage API Keys** → generate a key. You get a *JWT issuer* (the key) and
   a *JWT secret*. Keep the secret private, like a password.
2. Double-click **sign.bat**, paste the key, then the secret (typing is
   hidden). It sends the extension to Mozilla for signing (a few minutes), puts
   the signed `.xpi` in `dist\` (not uploaded to git), publishes it as a
   GitHub release, and adds it to `updates.json`. The key and secret are not
   saved anywhere.
3. Drag the `.xpi` onto a Firefox window (or **File → Open File**) and click
   **Add**. Remove the temporary copy in `about:debugging`.

Friends: open https://github.com/greatgreatasset/kpop-content-member-skipper/releases/latest
in Firefox and click the `.xpi`. Installed copies check `updates.json` (the
manifest's `update_url`) about once a day and update themselves. To ship a
change, raise `"version"` in `extension/manifest.json` (Mozilla signs each
version once) and run **sign.bat** again. Skip lists don't need a new version;
they are fetched from GitHub.

### Loading it temporarily (development)

1. Open `about:debugging` → **This Firefox** → **Load Temporary Add-on…**
2. Pick `extension\manifest.json`.
3. Open a scanned video, e.g. https://www.youtube.com/watch?v=oVMQTIUI_2c

Temporary add-ons are removed when Firefox restarts. Alternatively,
`cd extension` and run `npx web-ext run` to launch a separate Firefox profile
with it installed.

### Checks

```
cd extension && npx web-ext lint --self-hosted   # manifest/code lint (self-distributed add-on)
.venv\Scripts\python tests\browser_test.py       # end-to-end in headless Firefox
.venv\Scripts\python tests\browser_test.py --scan-video <id> [--rescan]   # "Filter this video", real scan + upload
```

`browser_test.py` (needs `pip install -r requirements-dev.txt`) installs the
add-on into a fresh temporary Firefox profile, opens the test video, and checks
markers, the skip, toast/Undo, the next-highlight button, the popup, the
settings page, the line under the title and in-page navigation. `--scan-video`
needs install_native_host.bat and checks button → progress banner → skips
applied (it commits and pushes the skip list like scan.bat).
