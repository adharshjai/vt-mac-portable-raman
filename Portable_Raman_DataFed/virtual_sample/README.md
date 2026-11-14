# Virtual sample (simulation only)

A virtual 25 mm x 25 mm plate for the two simulators. With it, a simulated scan gives spectra
that depend on where the simulated stage is, on the simulated laser (on or off, power level) and
on the focus height, instead of the same synthetic spectrum at every point. The rules are in
`data_layer/PROTOCOL.md`, section 4.

**None of this is used with real instruments.** Only the simulated device of the capture backend
(`RAMAN_BACKEND_SIMULATE=1`) and the simulated transport of the motor program
(`RAMAN_MOTOR_SIMULATE=1`) read these files. The code that talks to the real spectrometer and the
real motor controllers never opens them. Every frame produced from this folder is simulated data,
even though the reference spectra come from real measurements.

| file | what it is |
|---|---|
| `sample.json` | the plate: materials, regions, edge blend widths, focus height and width, power scale, noise, start position |
| `reference_spectra.json` | one dark spectrum and one dark-subtracted mean signal per material, 2048 values each |
| `build_reference_spectra.py` | builds `reference_spectra.json` from measured spectra |
| `sample_model.py` | the rules of section 4 in Python; the backend tests compare the capture simulator with it |
| `check_classifier.py` | shows what the shipped classifier makes of the plate |

## Where the reference spectra come from

From measurements in the hand-off folder of the original project
(`laser_test/build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug`). For each material: the mean of the
complete, unsaturated raw spectra minus the dark measured with them. `dark` is the mean of the
four dark measurements. File names, checksums and counts are also recorded in
`reference_spectra.json` under `sources`.

| material | spectra | dark | used |
|---|---|---|---|
| `r6g` | `scan_sessions/2d_scan_20260922_164646_611` (124 points) | `dark_measurements/dark_20260922_165331_762.txt` | 119; 5 saturated left out |
| `methylene_blue` | `Portable_Raman_database/Database_mythylene_blue.txt` | `Portable_Raman_database/dark_mythylene_blue.txt` | 115; 14 incomplete rows left out |
| `adenine` | `Portable_Raman_database/Database_adenine.txt` | `Portable_Raman_database/dark_adenine.txt` | 110 |
| `di_water` | `Portable_Raman_database/Database_di_water.txt` | `Portable_Raman_database/dark_di_water.txt` | 102; 40 incomplete rows left out |

R6G does not come from `Portable_Raman_database/Database_r6g.txt`: the shipped classifier
(`models/pca_svm_pure_20260922`) got its R6G class from the scan of 2026-09-22 listed above, and it
does not recognise the older spectra of that file (R6G score 0.44 for their mean).

## Regenerating them

Needs Python with numpy (this edition's `.venv` has it). Paths are relative to `--root`:

```
.venv/bin/python virtual_sample/build_reference_spectra.py \
    --root <original project>/laser_test/build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug \
    --material r6g            scan_sessions/2d_scan_20260922_164646_611 dark_measurements/dark_20260922_165331_762.txt \
    --material methylene_blue Portable_Raman_database/Database_mythylene_blue.txt Portable_Raman_database/dark_mythylene_blue.txt \
    --material adenine        Portable_Raman_database/Database_adenine.txt Portable_Raman_database/dark_adenine.txt \
    --material di_water       Portable_Raman_database/Database_di_water.txt Portable_Raman_database/dark_di_water.txt \
    --output virtual_sample/reference_spectra.json
```

The same input gives the same file, byte for byte. A spectra source is a log with `Image Data`
rows or a scan-session folder with `point_NNNN.json` files; its role is given by its position on
the command line, not by its name.

## The numbers in `sample.json`

They were chosen so that the shipped classifier behaves sensibly on the plate, and checked with

```
.venv/bin/python virtual_sample/check_classifier.py \
    --predictor-dir <original project>/laser_test/intersect_bridge
```

which uses the original prediction code (`svm_predictor.py`) and ends with exit code 1 when an
expectation fails. With the shipped values:

- 1D scans (13 points, at HIGH, MEDIUM and LOW power): band centres and blank plate get the right
  class with a score of at least 0.8; the points at 18 mm and 24 mm sit on band edges and stay below 0.8.
- 2D scan grid (11 x 11, HIGH power): 5 points on the droplet rims stay below 0.8, the other 116
  are at or above it.
- Laser off, or far out of focus (z = 0 mm and z = 20 mm, with the focus at 4 mm): rejected as
  "Low raw signal".

`noise_counts_rms` is 2 counts, much less than the real instrument shows. Above about 10 counts a
laser-off frame is no longer rejected as "Low raw signal", and already at 6 counts the edge points
change sides of the 0.8 threshold from run to run at LOW power.

## The simulated stage keeps its position

Like the real stage, the simulated one stays where the last simulated scan left it
(`scan_sessions/_sim_stage.json`). After a 1D X scan X is at 24 mm, so a 1D Y scan that follows
runs along x = 24 mm, which is blank plate. Deleting that file puts the simulated stage back at
`start_position_mm`.
