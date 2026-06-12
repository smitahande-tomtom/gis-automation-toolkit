# GIS Automation Toolkit

A Python-based desktop GUI tool for converting geospatial data between common GIS formats. Built with `tkinter`, `GeoPandas`, and `Shapely`, it handles both spatial and non-spatial data and includes special support for TomTom DDCT GeoJSON structures.

---

## Features

- 🗂️ **Multi-format support** — Convert between CSV, Shapefile (SHP), GeoJSON, GeoPackage (GPKG), File Geodatabase (GDB), and GeoParquet
- 🔁 **Automatic reprojection** — All spatial layers are reprojected to **EPSG:4326 (WGS 84)**
- 🩺 **Geometry validation & repair** — Invalid, null, and empty geometries are detected and fixed or dropped
- 📋 **TomTom DDCT support** — Automatically extracts `CommonNameSet` and `Association` tables from TomTom DDCT GeoJSON files
- 🗃️ **Non-spatial Parquet** — Plain (non-geometry) Parquet tables are converted to CSV, DBF, GPKG, or GDB
- 📂 **Batch conversion** — Process an entire folder of input files in one run
- 📊 **Conversion summary** — Per-layer results with feature counts, drop stats, and status (success / failed / skipped)
- 💾 **Export report** — Summary table can be exported as a CSV log

---

## Supported Input Formats

| Format | Extension |
|---|---|
| CSV (with geometry/WKT/lat+lon) | `.csv` |
| ESRI Shapefile | `.shp` |
| GeoJSON / JSON | `.geojson`, `.json` |
| GeoPackage | `.gpkg` |
| File Geodatabase | `.gdb` |
| GeoParquet / Plain Parquet | `.parquet` |

---

## Supported Output Formats

| Format | Notes |
|---|---|
| CSV | WKT geometry column; lon/lat added for point layers |
| Shapefile (SHP) | Column names truncated to 10 chars; geometry split by type |
| GeoJSON | Single-layer per file |
| GeoPackage (GPKG) | Multi-layer; includes CommonNameSet & Association tables |
| File Geodatabase (GDB) | Multi-layer; includes CommonNameSet & Association tables |

---

## Installation

### Prerequisites

- Python 3.9+
- pip

### Install dependencies

```bash
pip install geopandas pandas shapely
```

### Optional (recommended) dependencies

```bash
pip install pyogrio   # Faster I/O for GPKG/GDB layers (non-spatial table support)
pip install dbf       # Native DBF table writing for Shapefile output
```

---

## Usage

### Run the GUI

```bash
python convert_gis_formats_gui_final_09062026.py
```

### GUI Walkthrough

1. **Input Type** — Select `Single` (one file or `.gdb` folder) or `Multiple` (a folder of files)
2. **Input** — Browse for your input file or folder
3. **Output Folder** — Choose where output files will be saved
4. **Source CRS** *(optional)* — Specify a fallback CRS if the input has no CRS (e.g., `EPSG:3857`)
5. **Output Formats** — Check one or more target formats
6. **Start Conversion** — Runs conversion in a background thread
7. **Summary Table** — View per-layer results; double-click a row to open its output folder
8. **Export Summary CSV** — Save the filtered summary table to a CSV file

---

## Output Structure

Each input file gets its own subfolder inside the output directory:

```
output_folder/
└── my_input_file/
    ├── my_input_file__LayerName_to_csv.csv
    ├── my_input_file__LayerName_to_geojson.geojson
    ├── my_input_file__CommonNameSet_table.csv
    ├── my_input_file__Association_table.csv
    ├── my_input_file_to_gpkg.gpkg
    ├── my_input_file_to_gdb.gdb
    └── conversion_report.csv
```

---

## CSV Input Requirements

CSV files must contain one of the following geometry representations:

| Column(s) | Type |
|---|---|
| `geometry` or `wkt` | WKT string |
| `lon` + `lat` | Longitude / Latitude |
| `longitude` + `latitude` | Longitude / Latitude |
| `x` + `y` | Coordinate pair |

---

## TomTom DDCT GeoJSON Support

When processing TomTom DDCT-style GeoJSON files, the tool automatically:

- **Groups features by `ddctType`** into separate spatial layers
- **Extracts `CommonNameSet`** — flattened name records with language, script, and transliteration info
- **Extracts `Association`** — feature-to-feature association records (non-spatial)
- **Separates association features** (`apiType == "association"`) from spatial features

### CommonNameSet columns

`uuid`, `ddctType`, `CenterOfSettlementDisplayClass`, `CenterOfSettlementAdministrativeClass`, `NameType`, `LanguageCode`, `ISOLanguageCode`, `PrimaryName`, `NameText`, `ISOScriptCode`, `NotationAlphabet`, `ServiceGroup`, `PositionalAccuracy`, `ExternalIdentifier`

### Association columns

`Ass_id`, `ddctType`, `apiType`, `source_id`, `target_id`

---

## Geometry Handling

- **Mixed geometry types** are split into separate layers per geometry family (`polygon`, `line`, `point`)
- **GeometryCollection** types are resolved to their dominant sub-geometry type
- **3D geometries** are forced to 2D before writing
- **Invalid geometries** are repaired with `buffer(0)` or `make_valid()`; unfixable features are dropped

---

## Executable

A precompiled Windows executable is available in the repository:

```
convert_gis_formats.exe
```

Run it directly — no Python installation required.

---

## Dependencies

| Library | Purpose |
|---|---|
| `geopandas` | Spatial data reading, writing, and reprojection |
| `pandas` | Tabular data handling |
| `shapely` | Geometry operations and validation |
| `tkinter` | GUI framework (included with Python) |
| `pyogrio` *(optional)* | Fast I/O for GPKG/GDB non-spatial tables |
| `dbf` *(optional)* | Native DBF table writing |

---

## License

This project is intended for internal TomTom GIS automation workflows.
