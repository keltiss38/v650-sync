🇫🇷 [Version française](THIRD_PARTY.fr.md)

# Third-party components

This project is **independent**: it is not affiliated with, or endorsed by, Polar, Komoot, Strava or Intervals.icu.

## Included in the executable or used by the code

| Component | Use | License |
|---|---|---|
| [kompy](https://github.com/Tsadoq/kompy) (Matteo Villosio) | The Komoot access logic of `v650sync/komoot.py` (internal API addresses, authentication, pagination, GPX export) is taken from it, rewritten without the dependency | MIT, Copyright (c) 2023-2026 Matteo Villosio |
| [zeroconf](https://github.com/python-zeroconf/python-zeroconf) | Device discovery (mDNS) | LGPL-2.1 or later |
| [pywebview](https://github.com/r0x0r/pywebview) | Native window (WebView2) | BSD-3-Clause |
| [pythonnet](https://github.com/pythonnet/pythonnet) | .NET bridge used by pywebview | MIT |
| [PyInstaller](https://pyinstaller.org) | Building the executable | GPL-2.0 or later, with an exception for built programs |

## Downloaded on the user's request (not included in this repository)

To build maps, the program offers to download, with checksum verification, from their official sites: Java (Eclipse Temurin), Osmosis and its *mapsforge-map-writer* plugin. They remain under their own licenses. OpenStreetMap data extracts come from Geofabrik.

## Data

- Contour lines: SRTM elevations (NASA / USGS, public domain; USGS asks that the source be credited), downloaded on request from AWS's public "Terrain Tiles" dataset (`elevation-tiles-prod`, 1° `.hgt.gz` tiles). The contour lines are computed by the application (`v650sync/contours.py`); none of this data is included in this repository.
- OpenStreetMap maps and data: © OpenStreetMap contributors, ODbL license. The background tiles of the previews come from OpenStreetMap's servers: please respect their [usage policy](https://operations.osmfoundation.org/policies/tiles/).
- `v650sync/mapdata/tags_polar.json`: a mapping list of OpenStreetMap tags, established by observing a map from the device.

Komoot's internal API is undocumented: it may change or be restricted without notice. Use only your own account.

## kompy license (MIT)

The Komoot access logic in `v650sync/komoot.py` is derived from [kompy](https://github.com/Tsadoq/kompy). Its license text, reproduced as the MIT license requires:

```text
Copyright (c) 2023-2026 Matteo Villosio

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
