# Scripts

Standalone helpers for reclaiming space. Neither is part of the container, and
neither needs PlexCleaner installed — they are plain Python 3 with no
dependencies beyond `ffmpeg`/`ffprobe` (for `reencode.py`).

Both **default to a dry run.** `--apply` is required before anything is written.

Run them on whichever machine does the work — for re-encoding that means the box
with the fast CPU or a QuickSync/NVENC GPU, not the NAS.

---

## Which one do I want?

|  | `reencode.py` | `regrab.py` |
|---|---|---|
| Costs | CPU/GPU time | Bandwidth |
| Savings | Predictable — you pick the quality | Depends on a smaller release existing |
| Quality | Slight generation loss | Fresh encode from source |
| Risk | Can lose Dolby Vision; irreversible | Low — the \*arr manages it |
| Best when | You have hardware encode, or lots of remuxes | You have bandwidth and good indexers |

If you have an iGPU with QuickSync (`ls /dev/dri` shows `renderD128`), or lots of
cores, re-encoding wins on a large library. Without hardware encode, a 4K film
can take 10+ hours on a NAS CPU — re-grab instead.

**A useful split:** `regrab.py` for titles where a good smaller release exists,
`reencode.py` for the remuxes nothing smaller exists for.

---

## `reencode.py` — transcode oversized files to HEVC

```bash
# What would it touch?
./reencode.py --source /mnt/video --min-gb 20

# Do it, for one night, keeping originals until you trust it
./reencode.py --source /mnt/video --min-gb 20 --apply \
    --scratch /var/tmp/reencode --until 06:00 --keep-originals
```

Nightly:

```cron
0 22 * * *  /opt/plexcleaner/scripts/reencode.py --source /mnt/video \
              --min-gb 20 --apply --scratch /var/tmp/reencode --until 06:00 \
              >> /var/log/reencode.log 2>&1
```

What makes it safe to leave running:

- Encodes to scratch, never in place, so a crash cannot truncate a film.
- Verifies before replacing — the output must probe cleanly, match the original's
  duration within a few seconds, keep every stream, and actually be smaller.
  Any failure and the original is untouched.
- Skips Dolby Vision (the layer does not survive), anything already HEVC/AV1,
  and files modified in the last hour (likely still downloading).
- Remembers what it did, so a nightly run resumes instead of restarting.
- Stops cleanly on Ctrl-C or `--until`, finishing the file in flight.

**Put `--scratch` on a disk with room.** The encode lands there first, so a full
share is not a problem — but a full scratch is.

Pick your quality on one film before committing: `--quality` is QSV
`global_quality` or x265 CRF, lower is better, 22–26 is the useful range.

---

## `regrab.py` — make Radarr/Sonarr fetch something smaller

```bash
# What is big?
./regrab.py --radarr http://10.12.128.4:7878 --api-key KEY --min-gb 20

# Which profiles exist?
./regrab.py --radarr http://10.12.128.4:7878 --api-key KEY --list-profiles

# Move the biggest one to a capped profile and search. Start with --max 1.
./regrab.py --radarr http://10.12.128.4:7878 --api-key KEY --min-gb 20 \
    --quality Remux --profile "HD-1080p" --search --max 1 --apply
```

**The part that trips people up:** Radarr does not delete a file for being large.
It replaces a file when it grabs something its profile ranks *higher*. Pointing a
90 GB 2160p remux at a profile topping out at 1080p is what makes the current
file unwanted. Changing the profile without `--search` does nothing; searching
without changing the profile finds nothing better.

**The size cap lives in the profile, not here.** Settings → Quality → Max MB/min.
Roughly 40 MB/min gives a 5 GB two-hour film. Set that first or you may re-grab
something just as large.

---

## Finding the candidates

PlexCleaner's Media page is the shortlist. Set **Min GB** and **Codec: Not HEVC**
and the footer tells you the prize before you start. `reencode.py` can read that
list directly with `--plexcleaner http://host:8585` (it still needs `--source` to
find the files on disk, since Plex does not expose paths).
