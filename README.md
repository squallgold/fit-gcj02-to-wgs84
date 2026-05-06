# fit-gcj02-to-wgs84

Restore WGS84 coordinates in FIT files edited by Garmin Connect China.

## The problem

Garmin's China-region Connect (and likely Explore) silently converts route coordinates from **WGS84** to **GCJ-02** (the "Mars coordinate system") whenever a course is **edited and saved** — even just renaming a waypoint triggers the conversion. Direct sync from a GPX import without editing preserves WGS84.

If your watch displays maps in WGS84 (e.g. OSM via the OSM Maps app on Garmin Enduro / Fenix / Forerunner), edited courses appear **offset by ~270 meters** from their true location. This makes Connect China's otherwise nice GUI editing unsafe for any user who relies on accurate map display.

## What this tool does

Takes a FIT file edited by Connect China (containing GCJ-02 coordinates) and produces a new FIT file with all coordinate fields restored to WGS84. **Everything else is preserved byte-for-byte** — timestamps, altitude, course-point names (including non-ASCII like Chinese), distances, lap stats, and even Garmin's private fields are untouched.

## Usage

```bash
# Convert in place (writes 夏羌拉.wgs84.fit alongside the input)
python3 fit-gcj02-to-wgs84.py 夏羌拉.fit

# Custom output path
python3 fit-gcj02-to-wgs84.py 夏羌拉.fit -o /tmp/output.fit

# See every coordinate transformation
python3 fit-gcj02-to-wgs84.py 夏羌拉.fit -v
```

Then copy the resulting `*.wgs84.fit` to your watch's `\Garmin\NewFiles\` folder via USB.

## Requirements

Python 3.8+. The runtime conversion uses **stdlib only** — no FIT library needed; the tool parses the FIT binary structure directly. The `fit_tool` package is only required for running the test suite.

## Limitations

### ⚠️ Cannot auto-detect input coordinate system

This tool will **happily convert any FIT file you give it** — including a perfectly valid WGS84 file — because the FIT format does not record which coordinate system its values use, and Connect China leaves no metadata to distinguish edited from unedited files.

**Only use this tool on FIT files known to come from Connect China after editing.** If you sync a GPX directly to your watch via Connect (no edit), the resulting FIT is already WGS84 and must NOT be passed through this tool — doing so would shift it ~270m the wrong direction.

To prevent accidental double-conversion, the tool refuses to process any input file whose name ends in `.wgs84.fit` (override with `--force` if you really know what you're doing).

### Geographic scope

GCJ-02 is only defined inside mainland China (roughly: longitude 72.0–137.8°, latitude 0.8–55.8°). Coordinates outside this region are left untouched, matching the GCJ-02 spec.

## How it works (technical)

The FIT binary format stores coordinates as 32-bit signed integers in "semicircles" (1 semicircle = 180/2³¹ degrees) within data records, whose layout is described by preceding definition records. This tool:

1. Walks the FIT byte stream tracking definition records by `local_message_type`
2. Locates data records whose definition has `global_message_num` of `record` (20), `course_point` (32), or `lap` (19)
3. Within those, finds `position_lat`/`position_long` fields by `field_definition_num`
4. Reads the semicircles → degrees, applies an iterative GCJ-02→WGS84 inverse, writes degrees → semicircles back into the same byte positions
5. Recomputes the file CRC over the modified body

The GCJ-02 forward transform has no closed-form inverse, so the tool uses an iterative fixed-point method that converges to <1e-9 degree (well below the FIT semicircle precision floor of ~1e-7 degree) in 5 iterations.

Because the conversion is byte-level rather than going through a high-level FIT library, **all unknown / private Garmin fields are preserved exactly**. Higher-level libraries silently drop fields they don't recognize, which produced corrupt output in earlier prototypes of this tool.

## Tests

```bash
~/gpxfit-env/bin/python -m pytest tests/
```

The test suite uses real Connect-edited and pristine FIT files as fixtures (paths configurable via `pytest.ini`).

## Roadmap

This is **M1** of a multi-stage plan. Future milestones (separate releases):

- **M2** — Native macOS GUI app: load a FIT, render trackpoints + course points on an OpenTopoMap layer for visual inspection (Connect-edited files visibly offset; pristine files aligned). Lets you eyeball before converting.
- **M3** — In-app CoursePoint editing (rename, batch-delete, retype). Replaces the editing role currently filled by the unsafe Connect GUI.
- **M4** — One-click "send to watch" via macOS USB volume detection.

## License

MIT — see [LICENSE](LICENSE).
