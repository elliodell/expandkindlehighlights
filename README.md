# Expand Kindle Highlights

Recover Kindle highlight text using the **saved selection boundaries on your Mac**, rather than guessing where a truncated highlight ended.

## Current capabilities

- Reads modern `ksdk_annotation_v1.db` stores; falls back to legacy `AnnotationStorage` when no modern store exists.
- Makes a consistent in-memory SQLite snapshot, including committed WAL changes, without changing Kindle files.
- Inventories locally synced highlights by book identifier (ASIN).
- Resolves saved numeric ranges against downloaded, readable KFX books using your installed Calibre KFX Input plugin.
- Saves selected text, individual source segments, original coordinates, gaps and review flags.
- Excludes pending local edits rather than guessing deletion/action semantics.
- Leaves Kindle, Calibre and Readwise untouched. No automatic uploads or emails.

**Local counts are not your account total.** Kindle may only have annotations for some books synced locally. If a book is missing, open/download it in the Mac Kindle app, allow it to sync, and rerun the inventory. This is a step to try, not a guarantee of complete account-wide retrieval.

## Setup on macOS

Use Python 3.10+ and the current Mac Kindle app. Sign in to your own Amazon account and download the books you want to recover. Install Calibre and its [KFX Input plugin](https://www.mobileread.com/forums/showthread.php?t=291290).

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Inventory requires only Python's standard library:

```sh
python3 mac_kindle.py inventory --output inventory.json
```

Extract locally readable KFX books:

```sh
python mac_kindle.py recover --output recovered.json
```

To process one book, pass `--asin BOOK_IDENTIFIER` (from the inventory). Alternative installations can use `--kindle-root /path/to/Library` and `--kfx-plugin '/path/to/KFX Input.zip'`. Existing output files are never overwritten.

## Results and limitations

`recovered_range` means text covers all requested numeric positions. It **does not assert that every separator and endpoint has been independently verified**: end coordinates are interpreted as inclusive, readable text inserts spaces between source chunks, and every recovery is flagged for review. `segments` preserves the unmodified selected strings and their position ranges so a display separator is not confused with a source character.

`partial_range` means some coordinates were not mapped; `unmapped_range` means none were mapped. Other statuses distinguish missing books, multiple downloaded versions, unsupported long/structured positions and decoding errors. Never use numeric Kindle positions directly as offsets into a converted EPUB: formats have different coordinate systems.

This version does not decrypt DRM, handle encrypted store purchases through OCR, or automate account-wide syncing. It supports local KFX `.azw8` files stored under the app's ASIN directory. It will not silently select between multiple downloaded versions. It has not been tested against every Kindle app version.

Reports contain your private annotations and copyrighted excerpts. Keep them private; do not commit them. `private/` and report-pattern filenames are ignored, but you must still check any files before publishing.

## Calibre / export fallback

`expand_highlights.py` is an initial separate fallback for Readwise CSV or English Kindle `My Clippings.txt` plus a Calibre library. It finds surviving prefixes and proposes paragraph continuations for review; it cannot establish the original selection's endpoint. Prefer the Mac range-based approach where available. This fallback is experimental and is not the validated recovery path.

## Plan

1. Inventory local annotation coverage before claiming completeness.
2. Resolve local KFX ranges and retain provenance and uncertainty.
3. Compare recovered selections against Kindle's visible highlights to validate endpoints.
4. Add book-title metadata and a review interface.
5. Add explicit handling for other ebook formats, missing sync coverage and encrypted-book recovery if needed.
6. Only add approved Readwise replacement after review and duplicate handling are implemented.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Synthetic fixtures cover inclusive ranges, Unicode positions, gaps, overlaps, missing downloads, consistent database reads and pending-edit exclusion. No personal annotations are included in the repository.

## Related work

- [l3a0's Kindle highlight recovery](https://github.com/l3a0/claude-plugins): Mac annotations plus Cloud Reader/OCR recovery.
- [KFX-Highlights](https://github.com/aakar/KFX-Highlights): Kindle-device annotation extraction.
- [KFX Input](https://www.mobileread.com/forums/showthread.php?t=291290): John Howell's Calibre plugin providing KFX content/position decoding. This project uses the user's installed plugin and does not vendor it.
