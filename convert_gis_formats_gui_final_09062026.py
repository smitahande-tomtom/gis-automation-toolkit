import json
import os
import shutil
import subprocess
import sys
import threading
import traceback
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import pandas as pd
import geopandas as gpd
from shapely import wkt
from shapely.geometry import Point, MultiPolygon, MultiLineString, MultiPoint
from shapely.ops import transform

try:
    import dbf
except Exception:
    dbf = None

try:
    import pyogrio
except Exception:
    pyogrio = None

TARGET_CRS = "EPSG:4326"
SUPPORTED_INPUTS = {".csv", ".shp", ".geojson", ".json", ".gpkg", ".gdb", ".parquet"}
OUTPUT_FORMATS = ["csv", "shp", "geojson", "gpkg", "gdb"]

COMMON_NAME_COLUMNS = [
    "uuid",
    "ddctType",
    "CenterOfSettlementDisplayClass",
    "CenterOfSettlementAdministrativeClass",
    "NameType",
    "LanguageCode",
    "ISOLanguageCode",
    "PrimaryName",
    "NameText",
    "ISOScriptCode",
    "NotationAlphabet",
    "ServiceGroup",
    "PositionalAccuracy",
    "ExternalIdentifier",
]

ASSOCIATION_COLUMNS = [
    "Ass_id",
    "ddctType",
    "apiType",
    "source_id",
    "target_id",
]


def safe_name(name, max_len=80):
    cleaned = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in str(name))
    cleaned = cleaned.strip("_") or "item"
    return cleaned[:max_len]


def validate_csv_columns(path):
    try:
        df = pd.read_csv(path, nrows=5)
        cols = {c.lower(): c for c in df.columns}
        valid = (
            "geometry" in cols or
            "wkt" in cols or
            ("lon" in cols and "lat" in cols) or
            ("longitude" in cols and "latitude" in cols) or
            ("x" in cols and "y" in cols)
        )
        if valid:
            return True, "CSV columns look valid."
        return False, (
            "CSV must contain one of these: geometry, wkt, lon+lat, "
            "longitude+latitude, or x+y"
        )
    except Exception as e:
        return False, f"Could not validate CSV: {e}"


def validate_parquet_columns(path):
    try:
        try:
            gpd.read_parquet(path)
            return True, "GeoParquet file looks valid."
        except Exception:
            df = pd.read_parquet(path)
            cols = {c.lower(): c for c in df.columns}
            valid = (
                "geometry" in cols or
                "wkt" in cols or
                ("lon" in cols and "lat" in cols) or
                ("longitude" in cols and "latitude" in cols) or
                ("x" in cols and "y" in cols)
            )
            if valid:
                return True, "Plain Parquet file has usable geometry-related columns."
            return True, "Plain non-spatial Parquet table detected. Table conversions are supported."
    except Exception as e:
        return False, f"Could not validate Parquet: {e}"


def validate_input_path_for_mode(path, mode):
    try:
        p = Path(path)

        if mode == "single":
            if not p.exists():
                return False, "Selected input path does not exist."
            if p.is_dir() and p.suffix.lower() == ".gdb":
                return True, "Single GDB selected."
            if not p.is_file() and not (p.is_dir() and p.suffix.lower() == ".gdb"):
                return False, "Please select a valid file or .gdb folder."
            if p.is_file() and p.suffix.lower() not in SUPPORTED_INPUTS:
                return False, f"Unsupported input file type: {p.suffix}"
            return True, "Single input selected."

        if mode == "multiple":
            if not p.exists() or not p.is_dir():
                return False, "Please select a valid input folder."
            return True, "Input folder selected."

        return False, "Unknown input mode."

    except Exception as e:
        return False, str(e)


def read_csv_as_gdf(path, source_crs=None):
    df = pd.read_csv(path)
    cols = {c.lower(): c for c in df.columns}

    if "geometry" in cols:
        geom = df[cols["geometry"]].apply(lambda v: wkt.loads(v) if pd.notna(v) else None)
    elif "wkt" in cols:
        geom = df[cols["wkt"]].apply(lambda v: wkt.loads(v) if pd.notna(v) else None)
    elif "lon" in cols and "lat" in cols:
        geom = [Point(xy) if pd.notna(xy[0]) and pd.notna(xy[1]) else None for xy in zip(df[cols["lon"]], df[cols["lat"]])]
    elif "longitude" in cols and "latitude" in cols:
        geom = [Point(xy) if pd.notna(xy[0]) and pd.notna(xy[1]) else None for xy in zip(df[cols["longitude"]], df[cols["latitude"]])]
    elif "x" in cols and "y" in cols:
        geom = [Point(xy) if pd.notna(xy[0]) and pd.notna(xy[1]) else None for xy in zip(df[cols["x"]], df[cols["y"]])]
    else:
        raise ValueError(
            f"CSV {path} must contain geometry/WKT or lon+lat or longitude+latitude or x+y columns"
        )

    return gpd.GeoDataFrame(df.copy(), geometry=geom, crs=source_crs or TARGET_CRS)


def read_parquet_input(path, source_crs=None):
    path = Path(path)

    try:
        gdf = gpd.read_parquet(path)
        return {
            "layers": [(safe_name(path.stem), gdf)],
            "table_df": None,
            "is_spatial": True,
        }
    except Exception:
        df = pd.read_parquet(path)

    cols = {c.lower(): c for c in df.columns}

    if "geometry" in cols:
        geom_series = df[cols["geometry"]]
        sample = geom_series.dropna().head(5)
        if not sample.empty:
            first_val = sample.iloc[0]
            if isinstance(first_val, str):
                geom = geom_series.apply(lambda v: wkt.loads(v) if pd.notna(v) else None)
                gdf = gpd.GeoDataFrame(df.copy(), geometry=geom, crs=source_crs or TARGET_CRS)
                return {
                    "layers": [(safe_name(path.stem), gdf)],
                    "table_df": None,
                    "is_spatial": True,
                }

    if "wkt" in cols:
        geom = df[cols["wkt"]].apply(lambda v: wkt.loads(v) if pd.notna(v) else None)
        gdf = gpd.GeoDataFrame(df.copy(), geometry=geom, crs=source_crs or TARGET_CRS)
        return {
            "layers": [(safe_name(path.stem), gdf)],
            "table_df": None,
            "is_spatial": True,
        }

    if "lon" in cols and "lat" in cols:
        geom = [Point(xy) if pd.notna(xy[0]) and pd.notna(xy[1]) else None for xy in zip(df[cols["lon"]], df[cols["lat"]])]
        gdf = gpd.GeoDataFrame(df.copy(), geometry=geom, crs=source_crs or TARGET_CRS)
        return {
            "layers": [(safe_name(path.stem), gdf)],
            "table_df": None,
            "is_spatial": True,
        }

    if "longitude" in cols and "latitude" in cols:
        geom = [Point(xy) if pd.notna(xy[0]) and pd.notna(xy[1]) else None for xy in zip(df[cols["longitude"]], df[cols["latitude"]])]
        gdf = gpd.GeoDataFrame(df.copy(), geometry=geom, crs=source_crs or TARGET_CRS)
        return {
            "layers": [(safe_name(path.stem), gdf)],
            "table_df": None,
            "is_spatial": True,
        }

    if "x" in cols and "y" in cols:
        geom = [Point(xy) if pd.notna(xy[0]) and pd.notna(xy[1]) else None for xy in zip(df[cols["x"]], df[cols["y"]])]
        gdf = gpd.GeoDataFrame(df.copy(), geometry=geom, crs=source_crs or TARGET_CRS)
        return {
            "layers": [(safe_name(path.stem), gdf)],
            "table_df": None,
            "is_spatial": True,
        }

    return {
        "layers": [],
        "table_df": df.copy(),
        "is_spatial": False,
    }


def ensure_crs_and_reproject(gdf, source_crs=None):
    if gdf.crs is None:
        gdf = gdf.set_crs(source_crs or TARGET_CRS)
    return gdf.to_crs(TARGET_CRS)


def force_2d(geom):
    if geom is None or geom.is_empty:
        return geom
    try:
        return transform(lambda x, y, z=None: (x, y), geom)
    except Exception:
        return geom


def geometrycollection_to_supported(geom):
    if geom is None or geom.is_empty:
        return geom

    gt = geom.geom_type
    if gt != "GeometryCollection":
        return geom

    geoms = [g for g in getattr(geom, "geoms", []) if g is not None and not g.is_empty]
    if not geoms:
        return None

    polys = [g for g in geoms if g.geom_type in ("Polygon", "MultiPolygon")]
    lines = [g for g in geoms if g.geom_type in ("LineString", "MultiLineString")]
    points = [g for g in geoms if g.geom_type in ("Point", "MultiPoint")]

    if polys:
        return polys[0]
    if lines:
        return lines[0]
    if points:
        return points[0]
    return geoms[0]


def promote_to_multi(geom):
    if geom is None or geom.is_empty:
        return geom
    if geom.geom_type == "Polygon":
        return MultiPolygon([geom])
    if geom.geom_type == "LineString":
        return MultiLineString([geom])
    if geom.geom_type == "Point":
        return MultiPoint([geom])
    return geom


def normalize_geometry_for_writing(geom):
    geom = force_2d(geom)
    geom = geometrycollection_to_supported(geom)
    geom = promote_to_multi(geom)
    return geom


def fix_invalid_geometries(gdf):
    gdf = gdf.copy()
    gdf["geometry"] = gdf["geometry"].apply(normalize_geometry_for_writing)

    total_count = len(gdf)
    null_before = int(gdf.geometry.isna().sum())

    non_null = gdf.geometry.notna()
    invalid_before = int((~gdf.loc[non_null, "geometry"].is_valid).sum()) if total_count else 0

    if invalid_before > 0:
        try:
            gdf.loc[non_null, "geometry"] = gdf.loc[non_null, "geometry"].buffer(0)
        except Exception:
            try:
                gdf.loc[non_null, "geometry"] = gdf.loc[non_null, "geometry"].make_valid()
            except Exception:
                pass

    gdf["geometry"] = gdf["geometry"].apply(normalize_geometry_for_writing)

    non_null_after_fix = gdf.geometry.notna()
    invalid_after_fix = int((~gdf.loc[non_null_after_fix, "geometry"].is_valid).sum()) if total_count else 0

    before_drop = len(gdf)
    gdf = gdf[gdf.geometry.notna()].copy()
    gdf = gdf[gdf.geometry.is_valid].copy()
    gdf = gdf[~gdf.geometry.is_empty].copy()
    dropped_count = before_drop - len(gdf)

    stats = {
        "feature_count_before": total_count,
        "null_geometry_before": null_before,
        "invalid_geometry_before": invalid_before,
        "invalid_geometry_after_fix": invalid_after_fix,
        "dropped_after_fix": dropped_count,
        "feature_count_after": len(gdf),
    }
    return gdf, stats


def _rename_geometry_attribute_conflict(gdf):
    non_geom_cols = [c for c in gdf.columns if c != gdf.geometry.name]
    if "geometry" in non_geom_cols:
        gdf = gdf.rename(columns={"geometry": "geometry_attr"})
    return gdf


def find_primary_standard_name(props):
    common_name_set = props.get("CommonNameSet")
    if not isinstance(common_name_set, list):
        return None

    for cns in common_name_set:
        if not isinstance(cns, dict):
            continue

        name_types = cns.get("NameType", [])
        if isinstance(name_types, str):
            name_types = [name_types]

        if "StandardName" not in name_types:
            continue

        name_sets = cns.get("NameSet", [])
        if not isinstance(name_sets, list):
            continue

        for ns in name_sets:
            if not isinstance(ns, dict):
                continue

            translit_sets = ns.get("NameTransliterationSet", [])
            if not isinstance(translit_sets, list):
                continue

            for ts in translit_sets:
                if not isinstance(ts, dict):
                    continue

                primary_name = ts.get("PrimaryName", ns.get("PrimaryName"))
                if primary_name is not True:
                    continue

                combined_names = []
                if isinstance(ts.get("Name"), list):
                    combined_names.extend(ts.get("Name"))
                if isinstance(ts.get("TransliteratedName"), list):
                    combined_names.extend(ts.get("TransliteratedName"))

                for name_obj in combined_names:
                    if not isinstance(name_obj, dict):
                        continue
                    name_text = name_obj.get("NameText")
                    if name_text:
                        return name_text
    return None


def extract_standard_name(props):
    return find_primary_standard_name(props)


def flatten_props_for_output(props):
    cleaned = {}
    for k, v in props.items():
        out_key = safe_name(k)
        if isinstance(v, (list, dict)):
            cleaned[out_key] = json.dumps(v, ensure_ascii=False)
        else:
            cleaned[out_key] = v

    common_name_set = props.get("CommonNameSet")
    cleaned["CommonNameSet_json"] = (
        json.dumps(common_name_set, ensure_ascii=False)
        if common_name_set is not None
        else None
    )
    cleaned["StandardName"] = extract_standard_name(props)

    if "OfficialCode" in props:
        cleaned["OfficialCode"] = props.get("OfficialCode")
    if "StandardLanguage" in props:
        cleaned["StandardLanguage"] = props.get("StandardLanguage")

    return cleaned


def build_common_name_base_row(props):
    return {
        "uuid": props.get("uuid"),
        "ddctType": props.get("ddctType"),
        "CenterOfSettlementDisplayClass": props.get("CenterOfSettlementDisplayClass"),
        "CenterOfSettlementAdministrativeClass": props.get("CenterOfSettlementAdministrativeClass"),
        "NameType": None,
        "LanguageCode": None,
        "ISOLanguageCode": None,
        "PrimaryName": None,
        "NameText": None,
        "ISOScriptCode": None,
        "NotationAlphabet": None,
        "ServiceGroup": props.get("ServiceGroup"),
        "PositionalAccuracy": props.get("PositionalAccuracy"),
        "ExternalIdentifier": props.get("ExternalIdentifier"),
    }


def build_association_row_from_feature(props):
    return {
        "Ass_id": props.get("uuid"),
        "ddctType": props.get("ddctType"),
        "apiType": props.get("apiType"),
        "source_id": props.get("featureId"),
        "target_id": props.get("associatedFeatureId"),
    }


def expand_common_name_rows(props):
    common_name_set = props.get("CommonNameSet")
    if not isinstance(common_name_set, list):
        return []

    rows = []

    for cns in common_name_set:
        if not isinstance(cns, dict):
            continue

        name_types = cns.get("NameType", [])
        if isinstance(name_types, str):
            name_types = [name_types]
        if not isinstance(name_types, list):
            name_types = []

        name_type_text = "|".join(str(x) for x in name_types) if name_types else None

        name_sets = cns.get("NameSet", [])
        if not isinstance(name_sets, list):
            name_sets = []

        if not name_sets:
            row = build_common_name_base_row(props)
            row["NameType"] = name_type_text
            rows.append({col: row.get(col) for col in COMMON_NAME_COLUMNS})
            continue

        for ns in name_sets:
            if not isinstance(ns, dict):
                continue

            ns_language = ns.get("LanguageCode")
            ns_iso_language = ns.get("ISOLanguageCode")
            ns_primary = ns.get("PrimaryName")

            translit_sets = ns.get("NameTransliterationSet", [])
            if not isinstance(translit_sets, list):
                translit_sets = []

            if not translit_sets:
                row = build_common_name_base_row(props)
                row["NameType"] = name_type_text
                row["LanguageCode"] = ns_language
                row["ISOLanguageCode"] = ns_iso_language
                row["PrimaryName"] = ns_primary
                rows.append({col: row.get(col) for col in COMMON_NAME_COLUMNS})
                continue

            for ts in translit_sets:
                if not isinstance(ts, dict):
                    continue

                language_code = ts.get("LanguageCode", ns_language)
                iso_language_code = ts.get("ISOLanguageCode", ns_iso_language)
                primary_name = ts.get("PrimaryName", ns_primary)

                combined_names = []

                if isinstance(ts.get("Name"), list):
                    combined_names.extend(ts.get("Name"))

                if isinstance(ts.get("TransliteratedName"), list):
                    combined_names.extend(ts.get("TransliteratedName"))

                if not combined_names:
                    continue

                for name_obj in combined_names:
                    if not isinstance(name_obj, dict):
                        continue

                    name_text = name_obj.get("NameText")
                    if not name_text:
                        continue

                    row = build_common_name_base_row(props)
                    row["NameType"] = name_type_text
                    row["LanguageCode"] = language_code
                    row["ISOLanguageCode"] = iso_language_code
                    row["PrimaryName"] = primary_name
                    row["NameText"] = name_text
                    row["ISOScriptCode"] = (
                        name_obj.get("ISOScriptCode")
                        or ts.get("ISOScriptCode")
                        or ns.get("ISOScriptCode")
                    )
                    row["NotationAlphabet"] = (
                        name_obj.get("NotationAlphabet")
                        or ts.get("NotationAlphabet")
                        or ns.get("NotationAlphabet")
                    )

                    rows.append({col: row.get(col) for col in COMMON_NAME_COLUMNS})

    return rows


def map_ddct_type_to_layer(ddct_type):
    if ddct_type == "TTOM-Core::Built_UpAreaComponent":
        return "Built_UpAreaComponent"
    if ddct_type == "TTOM-Core::Built_UpArea":
        return "Built_UpArea"
    return safe_name(str(ddct_type).split("::")[-1] if ddct_type else "unknown")


def cleanup_spatial_layer_columns(gdf):
    gdf = gdf.copy()
    cols_to_remove = []
    for c in gdf.columns:
        if c == gdf.geometry.name:
            continue
        lc = c.lower()
        if lc in {"commonnameset_json", "commonnameset"}:
            cols_to_remove.append(c)
    if cols_to_remove:
        gdf = gdf.drop(columns=cols_to_remove, errors="ignore")
    return gdf


def preprocess_special_geojson_layers(path, source_crs=None):
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    if raw.get("type") != "FeatureCollection":
        raise ValueError("GeoJSON root type must be FeatureCollection")

    features = raw.get("features", [])
    grouped_rows = {}
    common_name_rows = []
    association_rows = []

    for feature in features:
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            continue

        props = feature.get("properties", {}) or {}

        if str(props.get("apiType", "")).strip().lower() == "association":
            association_rows.append(
                {col: build_association_row_from_feature(props).get(col) for col in ASSOCIATION_COLUMNS}
            )
            continue

        geometry = feature.get("geometry")
        if geometry is None:
            continue

        ddct_type = props.get("ddctType")
        layer_name = map_ddct_type_to_layer(ddct_type)

        flat_props = flatten_props_for_output(props)

        row = flat_props.copy()
        row["geometry"] = geometry
        grouped_rows.setdefault(layer_name, []).append(row)

        common_name_rows.extend(expand_common_name_rows(props))

    layers = []
    for layer_name, rows in grouped_rows.items():
        if not rows:
            continue

        features_for_gdf = []
        for r in rows:
            features_for_gdf.append({
                "type": "Feature",
                "geometry": r["geometry"],
                "properties": {k: v for k, v in r.items() if k != "geometry"}
            })

        gdf = gpd.GeoDataFrame.from_features(features_for_gdf, crs=source_crs or TARGET_CRS)
        gdf = _rename_geometry_attribute_conflict(gdf)
        gdf = cleanup_spatial_layer_columns(gdf)
        gdf = ensure_crs_and_reproject(gdf, source_crs)
        layers.append((safe_name(layer_name), gdf))

    common_name_df = pd.DataFrame(common_name_rows, columns=COMMON_NAME_COLUMNS)
    association_df = pd.DataFrame(association_rows, columns=ASSOCIATION_COLUMNS)

    if not layers and common_name_df.empty and association_df.empty:
        raise ValueError(f"No usable layers found in {path}")

    return layers, common_name_df, association_df


def extract_common_name_table_from_generic_layers(layers):
    rows = []
    for _, gdf in layers:
        if gdf is None or gdf.empty:
            continue

        col_map = {c.lower(): c for c in gdf.columns}
        common_col = col_map.get("commonnameset_json") or col_map.get("commonnameset")
        if not common_col:
            continue

        for _, rec in gdf.iterrows():
            common_val = rec.get(common_col)
            if pd.isna(common_val):
                continue

            try:
                parsed = json.loads(common_val) if isinstance(common_val, str) else common_val
            except Exception:
                parsed = None

            props = {
                "uuid": rec.get(col_map["uuid"]) if "uuid" in col_map else None,
                "ddctType": rec.get(col_map["ddcttype"]) if "ddcttype" in col_map else None,
                "CenterOfSettlementDisplayClass": rec.get(col_map["centerofsettlementdisplayclass"]) if "centerofsettlementdisplayclass" in col_map else None,
                "CenterOfSettlementAdministrativeClass": rec.get(col_map["centerofsettlementadministrativeclass"]) if "centerofsettlementadministrativeclass" in col_map else None,
                "ServiceGroup": rec.get(col_map["servicegroup"]) if "servicegroup" in col_map else None,
                "PositionalAccuracy": rec.get(col_map["positionalaccuracy"]) if "positionalaccuracy" in col_map else None,
                "ExternalIdentifier": rec.get(col_map["externalidentifier"]) if "externalidentifier" in col_map else None,
                "CommonNameSet": parsed if isinstance(parsed, list) else None,
            }

            expanded = expand_common_name_rows(props)
            if expanded:
                rows.extend(expanded)

    return pd.DataFrame(rows, columns=COMMON_NAME_COLUMNS)


def extract_association_table_from_generic_layers(layers):
    rows = []
    for _, gdf in layers:
        if gdf is None or gdf.empty:
            continue

        col_map = {c.lower(): c for c in gdf.columns}
        api_type_col = col_map.get("apitype")
        feature_id_col = col_map.get("featureid")
        associated_feature_id_col = col_map.get("associatedfeatureid")

        if api_type_col and feature_id_col and associated_feature_id_col:
            for _, rec in gdf.iterrows():
                if str(rec.get(api_type_col, "")).strip().lower() == "association":
                    row = {
                        "Ass_id": rec.get(col_map["uuid"]) if "uuid" in col_map else None,
                        "ddctType": rec.get(col_map["ddcttype"]) if "ddcttype" in col_map else None,
                        "apiType": rec.get(api_type_col),
                        "source_id": rec.get(feature_id_col),
                        "target_id": rec.get(associated_feature_id_col),
                    }
                    rows.append({col: row.get(col) for col in ASSOCIATION_COLUMNS})

    return pd.DataFrame(rows, columns=ASSOCIATION_COLUMNS)


def read_input(path, source_crs=None):
    path = Path(path)
    ext = path.suffix.lower()

    layers = []
    common_name_df = pd.DataFrame(columns=COMMON_NAME_COLUMNS)
    association_df = pd.DataFrame(columns=ASSOCIATION_COLUMNS)
    table_df = None
    is_spatial = True

    if ext == ".csv":
        gdf = read_csv_as_gdf(path, source_crs=source_crs)
        gdf = _rename_geometry_attribute_conflict(gdf)
        gdf = cleanup_spatial_layer_columns(gdf)
        layers.append((safe_name(path.stem), ensure_crs_and_reproject(gdf, source_crs)))

    elif ext == ".shp":
        gdf = gpd.read_file(path)
        gdf = _rename_geometry_attribute_conflict(gdf)
        gdf = cleanup_spatial_layer_columns(gdf)
        layers.append((safe_name(path.stem), ensure_crs_and_reproject(gdf, source_crs)))
        common_name_df = extract_common_name_table_from_generic_layers(layers)
        association_df = extract_association_table_from_generic_layers(layers)

    elif ext == ".parquet":
        parsed = read_parquet_input(path, source_crs=source_crs)
        layers = parsed["layers"]
        table_df = parsed["table_df"]
        is_spatial = parsed["is_spatial"]

        if layers:
            fixed_layers = []
            for layer_name, gdf in layers:
                gdf = _rename_geometry_attribute_conflict(gdf)
                gdf = cleanup_spatial_layer_columns(gdf)
                gdf = ensure_crs_and_reproject(gdf, source_crs)
                fixed_layers.append((layer_name, gdf))
            layers = fixed_layers
            common_name_df = extract_common_name_table_from_generic_layers(layers)
            association_df = extract_association_table_from_generic_layers(layers)

    elif ext in {".geojson", ".json"}:
        try:
            layers, common_name_df, association_df = preprocess_special_geojson_layers(path, source_crs=source_crs)
        except Exception:
            gdf = gpd.read_file(path)
            gdf = _rename_geometry_attribute_conflict(gdf)
            gdf = cleanup_spatial_layer_columns(gdf)
            layers.append((safe_name(path.stem), ensure_crs_and_reproject(gdf, source_crs)))
            common_name_df = extract_common_name_table_from_generic_layers(layers)
            association_df = extract_association_table_from_generic_layers(layers)

    elif ext in {".gpkg", ".gdb"}:
        info = gpd.list_layers(path)
        if info.empty:
            raise ValueError(f"No layers found in {path}")

        table_candidates = []

        for _, row in info.iterrows():
            layer_name = row["name"]
            geom_type = str(row.get("geometry_type", "") or "").strip()

            try:
                gdf = gpd.read_file(path, layer=layer_name)
            except Exception:
                continue

            if gdf is None or gdf.empty:
                continue

            if not geom_type or geom_type.lower() in {"none", "unknown"}:
                table_candidates.append((layer_name, pd.DataFrame(gdf)))
                continue

            gdf = _rename_geometry_attribute_conflict(gdf)
            gdf = cleanup_spatial_layer_columns(gdf)
            layers.append((safe_name(layer_name), ensure_crs_and_reproject(gdf, source_crs)))

        for layer_name, df in table_candidates:
            lname = safe_name(layer_name).lower()
            temp_df = pd.DataFrame(df)

            if lname == "commonnameset":
                matching_cols = [c for c in COMMON_NAME_COLUMNS if c in temp_df.columns]
                if len(matching_cols) == len(COMMON_NAME_COLUMNS):
                    common_name_df = temp_df[COMMON_NAME_COLUMNS]

            if lname == "association":
                matching_cols = [c for c in ASSOCIATION_COLUMNS if c in temp_df.columns]
                if len(matching_cols) == len(ASSOCIATION_COLUMNS):
                    association_df = temp_df[ASSOCIATION_COLUMNS]

        if common_name_df.empty:
            common_name_df = extract_common_name_table_from_generic_layers(layers)
        if association_df.empty:
            association_df = extract_association_table_from_generic_layers(layers)

    else:
        raise ValueError(f"Unsupported input format: {ext}")

    if not layers and table_df is None and common_name_df.empty and association_df.empty:
        raise ValueError(f"No usable content found in {path}")

    return layers, common_name_df, association_df, table_df, is_spatial


def sanitize_for_shapefile(gdf):
    gdf = gdf.copy()
    rename_map = {}
    used = set()

    for c in gdf.columns:
        if c == gdf.geometry.name:
            continue
        short = safe_name(c, max_len=10)
        base = short
        i = 1
        while short in used:
            suffix = f"_{i}"
            short = f"{base[:10 - len(suffix)]}{suffix}"
            i += 1
        used.add(short)
        rename_map[c] = short

    return gdf.rename(columns=rename_map)


def sanitize_table_columns(df, max_len=31):
    df = df.copy()
    rename_map = {}
    used = set()

    for c in df.columns:
        short = safe_name(c, max_len=max_len)
        base = short
        i = 1
        while short in used:
            suffix = f"_{i}"
            short = f"{base[:max_len - len(suffix)]}{suffix}"
            i += 1
        used.add(short)
        rename_map[c] = short

    return df.rename(columns=rename_map)


def sanitize_table_columns_for_dbf(df):
    df = df.copy()
    rename_map = {}
    used = set()

    for c in df.columns:
        short = safe_name(c, max_len=10)
        base = short
        i = 1
        while short in used:
            suffix = f"_{i}"
            short = f"{base[:10 - len(suffix)]}{suffix}"
            i += 1
        used.add(short)
        rename_map[c] = short

    out = df.rename(columns=rename_map)
    for c in out.columns:
        out[c] = out[c].apply(
            lambda v: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v
        )
    return out


def coerce_table_values_for_dbf(df):
    out = df.copy()
    for c in out.columns:
        def convert(v):
            if pd.isna(v):
                return None
            if isinstance(v, (list, dict)):
                return json.dumps(v, ensure_ascii=False)
            if isinstance(v, pd.Timestamp):
                return str(v)
            return v
        out[c] = out[c].apply(convert)
    return out


def infer_dbf_field_spec(series, name):
    non_null = series.dropna()
    if non_null.empty:
        return f"{name} C(254)"

    if non_null.map(lambda v: isinstance(v, bool)).all():
        return f"{name} L"

    if non_null.map(lambda v: isinstance(v, int) and not isinstance(v, bool)).all():
        return f"{name} N(18,0)"

    if non_null.map(lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)).all():
        return f"{name} N(18,6)"

    max_len = int(non_null.astype(str).map(len).max())
    max_len = max(1, min(max_len, 254))
    return f"{name} C({max_len})"


def write_dbf_table(df, out_dbf_path):
    if df is None or df.empty:
        return

    out_dbf_path = Path(out_dbf_path)
    out_dbf_path.parent.mkdir(parents=True, exist_ok=True)

    table_df = sanitize_table_columns_for_dbf(df)
    table_df = coerce_table_values_for_dbf(table_df)

    if dbf is not None:
        field_specs = [infer_dbf_field_spec(table_df[col], col) for col in table_df.columns]
        table = dbf.Table(str(out_dbf_path), "; ".join(field_specs))
        table.open(mode=dbf.READ_WRITE)
        try:
            for _, row in table_df.iterrows():
                values = []
                for col in table_df.columns:
                    v = row[col]
                    if isinstance(v, float) and pd.isna(v):
                        v = None
                    if isinstance(v, str) and len(v) > 254:
                        v = v[:254]
                    values.append(v)
                table.append(tuple(values))
        finally:
            table.close()
        return

    temp_dir = out_dbf_path.parent / f"_{out_dbf_path.stem}_tmp_shp"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)

    temp_shp = temp_dir / f"{out_dbf_path.stem}.shp"
    table_gdf = gpd.GeoDataFrame(table_df.copy(), geometry=[None] * len(table_df), crs=None)
    table_gdf.to_file(temp_shp, driver="ESRI Shapefile")

    produced_dbf = temp_dir / f"{out_dbf_path.stem}.dbf"
    if not produced_dbf.exists():
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise RuntimeError("Failed to create DBF table.")

    shutil.copy2(produced_dbf, out_dbf_path)
    shutil.rmtree(temp_dir, ignore_errors=True)


def write_csv(gdf, out_path):
    df = gdf.copy()
    wkt_geometry = df.geometry.apply(
        lambda geom: geom.wkt if geom is not None and not geom.is_empty else None
    )

    geom_col = gdf.geometry.name
    output_df = pd.DataFrame(df.drop(columns=[geom_col], errors="ignore"))

    if "geometry" in output_df.columns:
        output_df = output_df.rename(columns={"geometry": "geometry_attr"})

    output_df["geometry"] = wkt_geometry

    non_null_geom = df.geometry.notna()
    if len(df) > 0 and non_null_geom.any():
        geom_types = df.loc[non_null_geom, "geometry"].geom_type.unique().tolist()
        if len(geom_types) == 1 and geom_types[0] in {"Point", "MultiPoint"}:
            try:
                centroids = df.geometry.centroid
                output_df["lon"] = centroids.x
                output_df["lat"] = centroids.y
            except Exception:
                pass

    cols = [c for c in output_df.columns if c != "geometry"] + ["geometry"]
    output_df = output_df[cols]
    output_df.to_csv(out_path, index=False)


def split_gdf_by_geometry_type(gdf, base_layer_name):
    gdf = gdf.copy()
    gdf["geometry"] = gdf["geometry"].apply(normalize_geometry_for_writing)
    gdf = gdf[gdf.geometry.notna()].copy()
    gdf = gdf[~gdf.geometry.is_empty].copy()

    if gdf.empty:
        return []

    def geometry_family(geom):
        gt = geom.geom_type
        if gt in ("Polygon", "MultiPolygon"):
            return "polygon"
        if gt in ("LineString", "MultiLineString"):
            return "line"
        if gt in ("Point", "MultiPoint"):
            return "point"
        return gt.lower()

    gdf["_geom_family"] = gdf.geometry.apply(geometry_family)
    families = sorted(gdf["_geom_family"].dropna().unique().tolist())

    parts = []
    if "polygon" in families:
        subset = gdf[gdf["_geom_family"] == "polygon"].copy().drop(columns=["_geom_family"])
        if not subset.empty:
            parts.append((safe_name(base_layer_name), subset))

    for family in families:
        if family == "polygon":
            continue
        subset = gdf[gdf["_geom_family"] == family].copy().drop(columns=["_geom_family"])
        if subset.empty:
            continue
        layer_name = safe_name(f"{base_layer_name}_{family}")
        parts.append((layer_name, subset))

    return parts


def prepare_spatial_layer_for_output(gdf, fmt):
    gdf = cleanup_spatial_layer_columns(gdf)
    if fmt == "shp":
        return sanitize_for_shapefile(gdf)
    return gdf


def write_single_layer_output(gdf, out_path, fmt):
    out_path = Path(out_path)
    gdf = prepare_spatial_layer_for_output(gdf, fmt)

    if fmt == "csv":
        write_csv(gdf, out_path)
    elif fmt == "shp":
        gdf.to_file(out_path, driver="ESRI Shapefile")
    elif fmt == "geojson":
        gdf.to_file(out_path, driver="GeoJSON")
    else:
        raise ValueError(f"Unsupported single-layer format: {fmt}")


def write_standalone_table_to_dataset(df, out_path, table_name, fmt, mode):
    if df is None or df.empty:
        return

    table_df = sanitize_table_columns(df, max_len=31)

    if pyogrio is not None:
        pyogrio.write_dataframe(
            table_df,
            out_path,
            layer=safe_name(table_name),
            driver="GPKG" if fmt == "gpkg" else "OpenFileGDB",
            append=(mode == "a"),
            geometry_type=None,
            promote_to_multi=False,
        )
        return

    table_gdf = gpd.GeoDataFrame(table_df.copy(), geometry=[None] * len(table_df), crs=None)
    driver = "GPKG" if fmt == "gpkg" else "OpenFileGDB"
    table_gdf.to_file(out_path, layer=safe_name(table_name), driver=driver, mode=mode)


def write_multi_layer_output(layers, out_path, fmt, common_name_df=None, association_df=None):
    out_path = Path(out_path)

    if out_path.exists():
        if out_path.is_file():
            out_path.unlink()
        elif out_path.is_dir():
            shutil.rmtree(out_path)

    normalized_layers = []
    for layer_name, gdf in layers:
        parts = split_gdf_by_geometry_type(gdf, layer_name)
        for part_layer_name, part_gdf in parts:
            normalized_layers.append((part_layer_name, prepare_spatial_layer_for_output(part_gdf, fmt)))

    if not normalized_layers and (common_name_df is None or common_name_df.empty) and (association_df is None or association_df.empty):
        raise ValueError("No supported geometries or tables available to write")

    if fmt == "gpkg":
        layer_index = 0
        for layer_name, gdf in normalized_layers:
            gdf.to_file(out_path, layer=layer_name, driver="GPKG", mode="w" if layer_index == 0 else "a")
            layer_index += 1

        if common_name_df is not None and not common_name_df.empty:
            write_standalone_table_to_dataset(common_name_df, out_path, "CommonNameSet", fmt, "w" if layer_index == 0 else "a")
            layer_index += 1

        if association_df is not None and not association_df.empty:
            write_standalone_table_to_dataset(association_df, out_path, "Association", fmt, "w" if layer_index == 0 else "a")

    elif fmt == "gdb":
        layer_index = 0
        for layer_name, gdf in normalized_layers:
            gdf.to_file(out_path, layer=layer_name, driver="OpenFileGDB", mode="w" if layer_index == 0 else "a")
            layer_index += 1

        if common_name_df is not None and not common_name_df.empty:
            write_standalone_table_to_dataset(common_name_df, out_path, "CommonNameSet", fmt, "w" if layer_index == 0 else "a")
            layer_index += 1

        if association_df is not None and not association_df.empty:
            write_standalone_table_to_dataset(association_df, out_path, "Association", fmt, "w" if layer_index == 0 else "a")
    else:
        raise ValueError(f"Unsupported multi-layer format: {fmt}")


def create_source_output_folder(output_root, input_file):
    folder = Path(output_root) / safe_name(Path(input_file).stem)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def build_single_layer_output_path(source_folder, source_stem, layer_name, fmt):
    if fmt == "csv":
        return source_folder / f"{source_stem}__{layer_name}_to_csv.csv"
    if fmt == "shp":
        return source_folder / f"{safe_name(layer_name)}.shp"
    if fmt == "geojson":
        return source_folder / f"{source_stem}__{layer_name}_to_geojson.geojson"
    raise ValueError(f"Unsupported single-layer format: {fmt}")


def build_multi_layer_output_path(source_folder, source_stem, fmt):
    if fmt == "gpkg":
        return source_folder / f"{source_stem}_to_gpkg.gpkg"
    if fmt == "gdb":
        return source_folder / f"{source_stem}_to_gdb.gdb"
    raise ValueError(f"Unsupported multi-layer format: {fmt}")


def build_common_name_output_path(source_folder, source_stem, fmt):
    if fmt == "csv":
        return source_folder / f"{source_stem}__CommonNameSet_table.csv"
    if fmt == "shp":
        return source_folder / "CommonNameSet.dbf"
    raise ValueError(f"Unsupported CommonNameSet table format: {fmt}")


def build_association_output_path(source_folder, source_stem, fmt):
    if fmt == "csv":
        return source_folder / f"{source_stem}__Association_table.csv"
    if fmt == "shp":
        return source_folder / "Association.dbf"
    raise ValueError(f"Unsupported Association table format: {fmt}")


def build_plain_table_csv_output_path(source_folder, source_stem):
    return source_folder / f"{source_stem}_table_to_csv.csv"


def build_plain_table_dbf_output_path(source_folder, source_stem):
    return source_folder / f"{source_stem}.dbf"


def classify_status(status):
    s = (status or "").lower()
    if "success" in s:
        return "success"
    if "skip" in s:
        return "skipped"
    return "failed"


def open_folder(path_str):
    if not path_str:
        return
    path = Path(path_str)
    target = path if path.is_dir() else path.parent
    if not target.exists():
        return

    if sys.platform.startswith("win"):
        os.startfile(str(target))
    elif sys.platform == "darwin":
        subprocess.run(["open", str(target)], check=False)
    else:
        subprocess.run(["xdg-open", str(target)], check=False)


def convert_plain_table(input_file, source_folder, source_stem, table_df, selected_output_formats, log_callback=None, summary_callback=None):
    log_rows = []

    for fmt in selected_output_formats:
        if fmt == "csv":
            out_path = build_plain_table_csv_output_path(source_folder, source_stem)
            try:
                if log_callback:
                    log_callback(f"Writing plain table to CSV: {out_path}")
                table_df.to_csv(out_path, index=False)
                row = {
                    "source_file": str(input_file),
                    "layer_name": safe_name(source_stem),
                    "target_format": "csv",
                    "status": "success",
                    "output_path": str(out_path),
                    "feature_count_before": len(table_df),
                    "feature_count_after": len(table_df),
                    "dropped_after_fix": 0,
                    "message": "Stored as standalone table",
                }
            except Exception as e:
                row = {
                    "source_file": str(input_file),
                    "layer_name": safe_name(source_stem),
                    "target_format": "csv",
                    "status": "write_failed",
                    "output_path": str(out_path),
                    "feature_count_before": len(table_df),
                    "feature_count_after": len(table_df),
                    "dropped_after_fix": 0,
                    "message": str(e),
                }
            log_rows.append(row)
            if summary_callback:
                summary_callback(row)

        elif fmt == "shp":
            out_path = build_plain_table_dbf_output_path(source_folder, source_stem)
            try:
                if log_callback:
                    log_callback(f"Writing plain table to DBF: {out_path}")
                write_dbf_table(table_df, out_path)
                row = {
                    "source_file": str(input_file),
                    "layer_name": safe_name(source_stem),
                    "target_format": "shp",
                    "status": "success",
                    "output_path": str(out_path),
                    "feature_count_before": len(table_df),
                    "feature_count_after": len(table_df),
                    "dropped_after_fix": 0,
                    "message": "Stored as standalone DBF table",
                }
            except Exception as e:
                row = {
                    "source_file": str(input_file),
                    "layer_name": safe_name(source_stem),
                    "target_format": "shp",
                    "status": "write_failed",
                    "output_path": str(out_path),
                    "feature_count_before": len(table_df),
                    "feature_count_after": len(table_df),
                    "dropped_after_fix": 0,
                    "message": str(e),
                }
            log_rows.append(row)
            if summary_callback:
                summary_callback(row)

        elif fmt == "geojson":
            row = {
                "source_file": str(input_file),
                "layer_name": safe_name(source_stem),
                "target_format": "geojson",
                "status": "skipped_non_spatial",
                "output_path": "",
                "feature_count_before": len(table_df),
                "feature_count_after": len(table_df),
                "dropped_after_fix": 0,
                "message": "Plain non-spatial parquet/table cannot be written to GeoJSON.",
            }
            log_rows.append(row)
            if summary_callback:
                summary_callback(row)

        elif fmt in {"gpkg", "gdb"}:
            out_path = build_multi_layer_output_path(source_folder, source_stem, fmt)
            try:
                if out_path.exists():
                    if out_path.is_file():
                        out_path.unlink()
                    elif out_path.is_dir():
                        shutil.rmtree(out_path)

                if log_callback:
                    log_callback(f"Writing plain table to {fmt}: {out_path}")
                write_standalone_table_to_dataset(table_df, out_path, safe_name(source_stem), fmt, "w")
                row = {
                    "source_file": str(input_file),
                    "layer_name": safe_name(source_stem),
                    "target_format": fmt,
                    "status": "success",
                    "output_path": str(out_path),
                    "feature_count_before": len(table_df),
                    "feature_count_after": len(table_df),
                    "dropped_after_fix": 0,
                    "message": "Stored as standalone non-spatial table",
                }
            except Exception as e:
                row = {
                    "source_file": str(input_file),
                    "layer_name": safe_name(source_stem),
                    "target_format": fmt,
                    "status": "write_failed",
                    "output_path": str(out_path),
                    "feature_count_before": len(table_df),
                    "feature_count_after": len(table_df),
                    "dropped_after_fix": 0,
                    "message": str(e),
                }
            log_rows.append(row)
            if summary_callback:
                summary_callback(row)

    return log_rows


def convert_one_file(input_file, output_dir, selected_output_formats, source_crs=None, log_callback=None, summary_callback=None):
    input_file = Path(input_file)
    source_stem = safe_name(input_file.stem)
    source_folder = create_source_output_folder(output_dir, input_file)
    log_rows = []

    try:
        if log_callback:
            log_callback(f"Reading: {input_file}")
        layers, common_name_df, association_df, table_df, is_spatial = read_input(input_file, source_crs=source_crs)
    except Exception as e:
        row = {
            "source_file": str(input_file),
            "layer_name": "",
            "target_format": "",
            "status": "read_failed",
            "output_path": "",
            "feature_count_before": "",
            "feature_count_after": "",
            "dropped_after_fix": "",
            "message": str(e),
        }
        log_rows.append(row)
        if summary_callback:
            summary_callback(row)
        pd.DataFrame(log_rows).to_csv(source_folder / "conversion_report.csv", index=False)
        return

    if not is_spatial and table_df is not None:
        log_rows.extend(
            convert_plain_table(
                input_file=input_file,
                source_folder=source_folder,
                source_stem=source_stem,
                table_df=table_df,
                selected_output_formats=selected_output_formats,
                log_callback=log_callback,
                summary_callback=summary_callback,
            )
        )
        pd.DataFrame(log_rows).to_csv(source_folder / "conversion_report.csv", index=False)
        return

    cleaned_layers = []

    for layer_name, gdf in layers:
        try:
            cleaned_gdf, stats = fix_invalid_geometries(gdf)
            if cleaned_gdf.empty:
                row = {
                    "source_file": str(input_file),
                    "layer_name": layer_name,
                    "target_format": "",
                    "status": "skipped_empty_after_fix",
                    "output_path": "",
                    "feature_count_before": stats["feature_count_before"],
                    "feature_count_after": stats["feature_count_after"],
                    "dropped_after_fix": stats["dropped_after_fix"],
                    "message": "All features removed after cleanup",
                }
                log_rows.append(row)
                if summary_callback:
                    summary_callback(row)
                continue
            cleaned_layers.append((layer_name, cleaned_gdf, stats))
        except Exception as e:
            row = {
                "source_file": str(input_file),
                "layer_name": layer_name,
                "target_format": "",
                "status": "layer_cleanup_failed",
                "output_path": "",
                "feature_count_before": "",
                "feature_count_after": "",
                "dropped_after_fix": "",
                "message": str(e),
            }
            log_rows.append(row)
            if summary_callback:
                summary_callback(row)

    for fmt in selected_output_formats:
        if fmt == "csv":
            for layer_name, gdf, stats in cleaned_layers:
                out_path = build_single_layer_output_path(source_folder, source_stem, layer_name, fmt)
                try:
                    if log_callback:
                        log_callback(f"Writing {fmt} layer {layer_name}: {out_path}")
                    write_single_layer_output(gdf, out_path, fmt)
                    row = {
                        "source_file": str(input_file),
                        "layer_name": layer_name,
                        "target_format": fmt,
                        "status": "success",
                        "output_path": str(out_path),
                        "feature_count_before": stats["feature_count_before"],
                        "feature_count_after": stats["feature_count_after"],
                        "dropped_after_fix": stats["dropped_after_fix"],
                        "message": "",
                    }
                except Exception as e:
                    row = {
                        "source_file": str(input_file),
                        "layer_name": layer_name,
                        "target_format": fmt,
                        "status": "write_failed",
                        "output_path": str(out_path),
                        "feature_count_before": stats["feature_count_before"],
                        "feature_count_after": stats["feature_count_after"],
                        "dropped_after_fix": stats["dropped_after_fix"],
                        "message": str(e),
                    }
                log_rows.append(row)
                if summary_callback:
                    summary_callback(row)

            if common_name_df is not None and not common_name_df.empty:
                table_csv_path = build_common_name_output_path(source_folder, source_stem, "csv")
                try:
                    common_name_df.to_csv(table_csv_path, index=False)
                    row = {
                        "source_file": str(input_file),
                        "layer_name": "CommonNameSet",
                        "target_format": "csv",
                        "status": "success",
                        "output_path": str(table_csv_path),
                        "feature_count_before": len(common_name_df),
                        "feature_count_after": len(common_name_df),
                        "dropped_after_fix": 0,
                        "message": "Stored as standalone table",
                    }
                except Exception as e:
                    row = {
                        "source_file": str(input_file),
                        "layer_name": "CommonNameSet",
                        "target_format": "csv",
                        "status": "write_failed",
                        "output_path": str(table_csv_path),
                        "feature_count_before": len(common_name_df),
                        "feature_count_after": len(common_name_df),
                        "dropped_after_fix": 0,
                        "message": str(e),
                    }
                log_rows.append(row)
                if summary_callback:
                    summary_callback(row)

            if association_df is not None and not association_df.empty:
                table_csv_path = build_association_output_path(source_folder, source_stem, "csv")
                try:
                    association_df.to_csv(table_csv_path, index=False)
                    row = {
                        "source_file": str(input_file),
                        "layer_name": "Association",
                        "target_format": "csv",
                        "status": "success",
                        "output_path": str(table_csv_path),
                        "feature_count_before": len(association_df),
                        "feature_count_after": len(association_df),
                        "dropped_after_fix": 0,
                        "message": "Stored as standalone table",
                    }
                except Exception as e:
                    row = {
                        "source_file": str(input_file),
                        "layer_name": "Association",
                        "target_format": "csv",
                        "status": "write_failed",
                        "output_path": str(table_csv_path),
                        "feature_count_before": len(association_df),
                        "feature_count_after": len(association_df),
                        "dropped_after_fix": 0,
                        "message": str(e),
                    }
                log_rows.append(row)
                if summary_callback:
                    summary_callback(row)

        elif fmt == "geojson":
            for layer_name, gdf, stats in cleaned_layers:
                out_path = build_single_layer_output_path(source_folder, source_stem, layer_name, fmt)
                try:
                    if log_callback:
                        log_callback(f"Writing {fmt} layer {layer_name}: {out_path}")
                    write_single_layer_output(gdf, out_path, fmt)
                    row = {
                        "source_file": str(input_file),
                        "layer_name": layer_name,
                        "target_format": fmt,
                        "status": "success",
                        "output_path": str(out_path),
                        "feature_count_before": stats["feature_count_before"],
                        "feature_count_after": stats["feature_count_after"],
                        "dropped_after_fix": stats["dropped_after_fix"],
                        "message": "",
                    }
                except Exception as e:
                    row = {
                        "source_file": str(input_file),
                        "layer_name": layer_name,
                        "target_format": fmt,
                        "status": "write_failed",
                        "output_path": str(out_path),
                        "feature_count_before": stats["feature_count_before"],
                        "feature_count_after": stats["feature_count_after"],
                        "dropped_after_fix": stats["dropped_after_fix"],
                        "message": str(e),
                    }
                log_rows.append(row)
                if summary_callback:
                    summary_callback(row)

        elif fmt == "shp":
            for layer_name, gdf, stats in cleaned_layers:
                parts = split_gdf_by_geometry_type(gdf, layer_name)
                if not parts:
                    row = {
                        "source_file": str(input_file),
                        "layer_name": layer_name,
                        "target_format": "shp",
                        "status": "skipped_empty_after_fix",
                        "output_path": "",
                        "feature_count_before": stats["feature_count_before"],
                        "feature_count_after": 0,
                        "dropped_after_fix": stats["dropped_after_fix"],
                        "message": "No supported geometry parts available for shapefile output",
                    }
                    log_rows.append(row)
                    if summary_callback:
                        summary_callback(row)
                    continue

                for part_layer_name, part_gdf in parts:
                    out_path = build_single_layer_output_path(source_folder, source_stem, part_layer_name, "shp")
                    try:
                        if log_callback:
                            log_callback(f"Writing shp layer {part_layer_name}: {out_path}")
                        write_single_layer_output(part_gdf, out_path, "shp")
                        row = {
                            "source_file": str(input_file),
                            "layer_name": part_layer_name,
                            "target_format": "shp",
                            "status": "success",
                            "output_path": str(out_path),
                            "feature_count_before": len(part_gdf),
                            "feature_count_after": len(part_gdf),
                            "dropped_after_fix": 0,
                            "message": "",
                        }
                    except Exception as e:
                        row = {
                            "source_file": str(input_file),
                            "layer_name": part_layer_name,
                            "target_format": "shp",
                            "status": "write_failed",
                            "output_path": str(out_path),
                            "feature_count_before": len(part_gdf),
                            "feature_count_after": len(part_gdf),
                            "dropped_after_fix": 0,
                            "message": str(e),
                        }
                    log_rows.append(row)
                    if summary_callback:
                        summary_callback(row)

            if common_name_df is not None and not common_name_df.empty:
                table_dbf_path = build_common_name_output_path(source_folder, source_stem, "shp")
                try:
                    if log_callback:
                        log_callback(f"Writing CommonNameSet standalone DBF table: {table_dbf_path}")
                    write_dbf_table(common_name_df, table_dbf_path)
                    row = {
                        "source_file": str(input_file),
                        "layer_name": "CommonNameSet",
                        "target_format": "shp",
                        "status": "success",
                        "output_path": str(table_dbf_path),
                        "feature_count_before": len(common_name_df),
                        "feature_count_after": len(common_name_df),
                        "dropped_after_fix": 0,
                        "message": "Stored as standalone DBF table",
                    }
                except Exception as e:
                    row = {
                        "source_file": str(input_file),
                        "layer_name": "CommonNameSet",
                        "target_format": "shp",
                        "status": "write_failed",
                        "output_path": str(table_dbf_path),
                        "feature_count_before": len(common_name_df),
                        "feature_count_after": len(common_name_df),
                        "dropped_after_fix": 0,
                        "message": str(e),
                    }
                log_rows.append(row)
                if summary_callback:
                    summary_callback(row)

            if association_df is not None and not association_df.empty:
                table_dbf_path = build_association_output_path(source_folder, source_stem, "shp")
                try:
                    if log_callback:
                        log_callback(f"Writing Association standalone DBF table: {table_dbf_path}")
                    write_dbf_table(association_df, table_dbf_path)
                    row = {
                        "source_file": str(input_file),
                        "layer_name": "Association",
                        "target_format": "shp",
                        "status": "success",
                        "output_path": str(table_dbf_path),
                        "feature_count_before": len(association_df),
                        "feature_count_after": len(association_df),
                        "dropped_after_fix": 0,
                        "message": "Stored as standalone DBF table",
                    }
                except Exception as e:
                    row = {
                        "source_file": str(input_file),
                        "layer_name": "Association",
                        "target_format": "shp",
                        "status": "write_failed",
                        "output_path": str(table_dbf_path),
                        "feature_count_before": len(association_df),
                        "feature_count_after": len(association_df),
                        "dropped_after_fix": 0,
                        "message": str(e),
                    }
                log_rows.append(row)
                if summary_callback:
                    summary_callback(row)

        elif fmt in {"gpkg", "gdb"}:
            out_path = build_multi_layer_output_path(source_folder, source_stem, fmt)
            try:
                valid_layers = [(layer_name, gdf) for layer_name, gdf, _ in cleaned_layers]
                if log_callback:
                    log_callback(f"Writing {fmt} with {len(valid_layers)} spatial layer(s)")
                if common_name_df is not None and not common_name_df.empty and log_callback:
                    log_callback(f"Writing CommonNameSet standalone table with {len(common_name_df)} record(s)")
                if association_df is not None and not association_df.empty and log_callback:
                    log_callback(f"Writing Association standalone table with {len(association_df)} record(s)")

                write_multi_layer_output(valid_layers, out_path, fmt, common_name_df=common_name_df, association_df=association_df)

                for layer_name, _, stats in cleaned_layers:
                    row = {
                        "source_file": str(input_file),
                        "layer_name": layer_name,
                        "target_format": fmt,
                        "status": "success",
                        "output_path": str(out_path),
                        "feature_count_before": stats["feature_count_before"],
                        "feature_count_after": stats["feature_count_after"],
                        "dropped_after_fix": stats["dropped_after_fix"],
                        "message": "",
                    }
                    log_rows.append(row)
                    if summary_callback:
                        summary_callback(row)

                if common_name_df is not None and not common_name_df.empty:
                    row = {
                        "source_file": str(input_file),
                        "layer_name": "CommonNameSet",
                        "target_format": fmt,
                        "status": "success",
                        "output_path": str(out_path),
                        "feature_count_before": len(common_name_df),
                        "feature_count_after": len(common_name_df),
                        "dropped_after_fix": 0,
                        "message": "Stored as standalone non-spatial table",
                    }
                    log_rows.append(row)
                    if summary_callback:
                        summary_callback(row)

                if association_df is not None and not association_df.empty:
                    row = {
                        "source_file": str(input_file),
                        "layer_name": "Association",
                        "target_format": fmt,
                        "status": "success",
                        "output_path": str(out_path),
                        "feature_count_before": len(association_df),
                        "feature_count_after": len(association_df),
                        "dropped_after_fix": 0,
                        "message": "Stored as standalone non-spatial table",
                    }
                    log_rows.append(row)
                    if summary_callback:
                        summary_callback(row)

            except Exception as e:
                for layer_name, _, stats in cleaned_layers:
                    row = {
                        "source_file": str(input_file),
                        "layer_name": layer_name,
                        "target_format": fmt,
                        "status": "write_failed",
                        "output_path": str(out_path),
                        "feature_count_before": stats["feature_count_before"],
                        "feature_count_after": stats["feature_count_after"],
                        "dropped_after_fix": stats["dropped_after_fix"],
                        "message": str(e),
                    }
                    log_rows.append(row)
                    if summary_callback:
                        summary_callback(row)

                if common_name_df is not None and not common_name_df.empty:
                    row = {
                        "source_file": str(input_file),
                        "layer_name": "CommonNameSet",
                        "target_format": fmt,
                        "status": "write_failed",
                        "output_path": str(out_path),
                        "feature_count_before": len(common_name_df),
                        "feature_count_after": len(common_name_df),
                        "dropped_after_fix": 0,
                        "message": str(e),
                    }
                    log_rows.append(row)
                    if summary_callback:
                        summary_callback(row)

                if association_df is not None and not association_df.empty:
                    row = {
                        "source_file": str(input_file),
                        "layer_name": "Association",
                        "target_format": fmt,
                        "status": "write_failed",
                        "output_path": str(out_path),
                        "feature_count_before": len(association_df),
                        "feature_count_after": len(association_df),
                        "dropped_after_fix": 0,
                        "message": str(e),
                    }
                    log_rows.append(row)
                    if summary_callback:
                        summary_callback(row)

    pd.DataFrame(log_rows).to_csv(source_folder / "conversion_report.csv", index=False)


def get_input_files(input_path):
    input_path = Path(input_path)
    if input_path.is_file():
        return [input_path]
    if input_path.is_dir() and input_path.suffix.lower() == ".gdb":
        return [input_path]
    if input_path.is_dir():
        return [
            p for p in input_path.iterdir()
            if (p.is_file() and p.suffix.lower() in SUPPORTED_INPUTS)
            or (p.is_dir() and p.suffix.lower() == ".gdb")
        ]
    raise FileNotFoundError(f"Input path not found: {input_path}")


class GISConverterGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("GIS Format Converter")
        self.root.geometry("1250x900")

        self.input_path_var = tk.StringVar()
        self.output_path_var = tk.StringVar()
        self.source_crs_var = tk.StringVar(value="")
        self.input_mode_var = tk.StringVar(value="single")
        self.progress_var = tk.DoubleVar(value=0)
        self.filter_var = tk.StringVar(value="all")
        self.format_vars = {fmt: tk.BooleanVar(value=False) for fmt in OUTPUT_FORMATS}
        self.summary_rows = []

        self.build_ui()

    def build_ui(self):
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)

        ttk.Label(main, text="GIS Format Converter", font=("Arial", 16, "bold")).pack(anchor="w", pady=(0, 10))

        mode_frame = ttk.LabelFrame(main, text="Input Type", padding=10)
        mode_frame.pack(fill="x", pady=5)
        ttk.Radiobutton(mode_frame, text="Single", variable=self.input_mode_var, value="single").pack(side="left", padx=5)
        ttk.Radiobutton(mode_frame, text="Multiple", variable=self.input_mode_var, value="multiple").pack(side="left", padx=5)

        input_frame = ttk.LabelFrame(main, text="Input", padding=10)
        input_frame.pack(fill="x", pady=5)
        ttk.Entry(input_frame, textvariable=self.input_path_var, width=100).pack(side="left", fill="x", expand=True, padx=(0, 5))
        ttk.Button(input_frame, text="Browse", command=self.browse_input).pack(side="left")

        output_frame = ttk.LabelFrame(main, text="Output Folder", padding=10)
        output_frame.pack(fill="x", pady=5)
        ttk.Entry(output_frame, textvariable=self.output_path_var, width=100).pack(side="left", fill="x", expand=True, padx=(0, 5))
        ttk.Button(output_frame, text="Browse", command=self.browse_output).pack(side="left")

        crs_frame = ttk.LabelFrame(main, text="Source CRS if missing", padding=10)
        crs_frame.pack(fill="x", pady=5)
        ttk.Entry(crs_frame, textvariable=self.source_crs_var, width=30).pack(side="left", padx=(0, 8))
        ttk.Label(crs_frame, text="Example: EPSG:3857").pack(side="left")

        format_frame = ttk.LabelFrame(main, text="Select Output Formats", padding=10)
        format_frame.pack(fill="x", pady=5)

        cb_frame = ttk.Frame(format_frame)
        cb_frame.pack(fill="x")
        for i, fmt in enumerate(OUTPUT_FORMATS):
            ttk.Checkbutton(cb_frame, text=fmt, variable=self.format_vars[fmt]).grid(row=0, column=i, padx=10, pady=5, sticky="w")

        btn_frame = ttk.Frame(format_frame)
        btn_frame.pack(fill="x", pady=(5, 0))
        ttk.Button(btn_frame, text="Select All", command=self.select_all_formats).pack(side="left")
        ttk.Button(btn_frame, text="Unselect All", command=self.unselect_all_formats).pack(side="left", padx=5)

        progress_frame = ttk.LabelFrame(main, text="Progress", padding=10)
        progress_frame.pack(fill="x", pady=5)
        ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100).pack(fill="x", expand=True)
        self.progress_label = ttk.Label(progress_frame, text="0%")
        self.progress_label.pack(anchor="e", pady=(5, 0))

        action_frame = ttk.Frame(main)
        action_frame.pack(fill="x", pady=10)
        ttk.Button(action_frame, text="Start Conversion", command=self.start_conversion).pack(side="left")
        ttk.Button(action_frame, text="Clear Log", command=self.clear_log).pack(side="left", padx=5)
        ttk.Button(action_frame, text="Clear Summary", command=self.clear_summary).pack(side="left", padx=5)
        ttk.Button(action_frame, text="Export Summary CSV", command=self.export_summary_csv).pack(side="left", padx=5)

        log_frame = ttk.LabelFrame(main, text="Log", padding=10)
        log_frame.pack(fill="both", expand=False, pady=5)

        self.log_text = tk.Text(log_frame, wrap="word", height=10)
        self.log_text.pack(side="left", fill="both", expand=True)

        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        log_scroll.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=log_scroll.set)

        summary_controls = ttk.LabelFrame(main, text="Summary Controls", padding=10)
        summary_controls.pack(fill="x", pady=5)
        ttk.Label(summary_controls, text="Filter:").pack(side="left")
        filter_combo = ttk.Combobox(
            summary_controls,
            textvariable=self.filter_var,
            values=["all", "success", "failed", "skipped"],
            state="readonly",
            width=15,
        )
        filter_combo.pack(side="left", padx=8)
        filter_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh_summary_table())
        ttk.Label(summary_controls, text="Double-click a row to open its output folder.").pack(side="left", padx=20)

        summary_frame = ttk.LabelFrame(main, text="Summary Table", padding=10)
        summary_frame.pack(fill="both", expand=True, pady=5)

        columns = (
            "source_file", "layer_name", "target_format", "status",
            "output_path", "feature_count_before", "feature_count_after",
            "dropped_after_fix", "message"
        )
        self.summary_tree = ttk.Treeview(summary_frame, columns=columns, show="headings", height=16)

        headings = {
            "source_file": "Source File",
            "layer_name": "Layer",
            "target_format": "Target Format",
            "status": "Status",
            "output_path": "Output Path",
            "feature_count_before": "Count Before",
            "feature_count_after": "Count After",
            "dropped_after_fix": "Dropped",
            "message": "Message",
        }

        for col in columns:
            self.summary_tree.heading(col, text=headings[col])
            self.summary_tree.column(col, width=120, anchor="w")

        self.summary_tree.column("source_file", width=220)
        self.summary_tree.column("output_path", width=320)
        self.summary_tree.column("message", width=280)

        self.summary_tree.pack(side="left", fill="both", expand=True)
        self.summary_tree.bind("<Double-1>", self.on_summary_double_click)

        tree_scroll_y = ttk.Scrollbar(summary_frame, orient="vertical", command=self.summary_tree.yview)
        tree_scroll_y.pack(side="right", fill="y")
        self.summary_tree.configure(yscrollcommand=tree_scroll_y.set)

    def browse_input(self):
        path = ""
        mode = self.input_mode_var.get()

        if mode == "single":
            path = filedialog.askopenfilename(
                title="Select input file",
                filetypes=[
                    ("Supported GIS files", "*.csv *.shp *.geojson *.json *.gpkg *.parquet"),
                    ("All files", "*.*")
                ]
            )

            if path and Path(path).suffix.lower() == ".csv":
                valid, msg = validate_csv_columns(path)
                if not valid:
                    messagebox.showwarning("CSV Validation", msg)
            elif path and Path(path).suffix.lower() == ".parquet":
                valid, msg = validate_parquet_columns(path)
                if not valid:
                    messagebox.showwarning("Parquet Validation", msg)

            if not path:
                gdb_path = filedialog.askdirectory(title="Or select a .gdb folder")
                if gdb_path and Path(gdb_path).suffix.lower() == ".gdb":
                    path = gdb_path

        elif mode == "multiple":
            path = filedialog.askdirectory(title="Select input folder")

        if path:
            self.input_path_var.set(path)

    def browse_output(self):
        path = filedialog.askdirectory(title="Select output folder")
        if path:
            self.output_path_var.set(path)

    def select_all_formats(self):
        for var in self.format_vars.values():
            var.set(True)

    def unselect_all_formats(self):
        for var in self.format_vars.values():
            var.set(False)

    def log(self, message):
        self.root.after(0, lambda: (self.log_text.insert("end", message + "\n"), self.log_text.see("end")))

    def clear_log(self):
        self.log_text.delete("1.0", "end")

    def clear_summary(self):
        self.summary_rows.clear()
        self.refresh_summary_table()

    def add_summary_row(self, row):
        self.summary_rows.append(row)
        self.root.after(0, self.refresh_summary_table)

    def refresh_summary_table(self):
        for item in self.summary_tree.get_children():
            self.summary_tree.delete(item)

        current_filter = self.filter_var.get().strip().lower()

        for row in self.summary_rows:
            if current_filter != "all":
                if classify_status(row.get("status")) != current_filter:
                    continue

            values = (
                row.get("source_file", ""),
                row.get("layer_name", ""),
                row.get("target_format", ""),
                row.get("status", ""),
                row.get("output_path", ""),
                row.get("feature_count_before", ""),
                row.get("feature_count_after", ""),
                row.get("dropped_after_fix", ""),
                row.get("message", ""),
            )
            self.summary_tree.insert("", "end", values=values)

    def export_summary_csv(self):
        if not self.summary_rows:
            messagebox.showinfo("Export Summary", "No summary rows to export.")
            return

        path = filedialog.asksaveasfilename(
            title="Save summary CSV",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]
        )
        if not path:
            return

        rows = []
        current_filter = self.filter_var.get().strip().lower()
        for row in self.summary_rows:
            if current_filter != "all" and classify_status(row.get("status")) != current_filter:
                continue
            rows.append(row)

        pd.DataFrame(rows).to_csv(path, index=False)
        messagebox.showinfo("Export Summary", f"Summary exported to:\n{path}")

    def on_summary_double_click(self, event):
        item_id = self.summary_tree.focus()
        if not item_id:
            return
        values = self.summary_tree.item(item_id, "values")
        if not values:
            return
        output_path = values[4]
        if output_path:
            open_folder(output_path)

    def update_progress(self, value):
        def _update():
            self.progress_var.set(value)
            self.progress_label.config(text=f"{int(value)}%")
        self.root.after(0, _update)

    def validate_inputs(self):
        input_path = self.input_path_var.get().strip()
        output_path = self.output_path_var.get().strip()
        selected_formats = [fmt for fmt, var in self.format_vars.items() if var.get()]
        input_mode = self.input_mode_var.get().strip()

        if not input_path:
            messagebox.showerror("Error", "Please select an input file or folder.")
            return None
        if not output_path:
            messagebox.showerror("Error", "Please select an output folder.")
            return None
        if not selected_formats:
            messagebox.showerror("Error", "Please select at least one output format.")
            return None

        valid, msg = validate_input_path_for_mode(input_path, input_mode)
        if not valid:
            messagebox.showerror("Error", msg)
            return None

        input_path_obj = Path(input_path)
        if input_path_obj.is_file() and input_path_obj.suffix.lower() == ".csv":
            valid, msg = validate_csv_columns(input_path_obj)
            if not valid:
                proceed = messagebox.askyesno("CSV Validation Warning", f"{msg}\n\nContinue anyway?")
                if not proceed:
                    return None

        if input_path_obj.is_file() and input_path_obj.suffix.lower() == ".parquet":
            valid, msg = validate_parquet_columns(input_path_obj)
            if not valid:
                proceed = messagebox.askyesno("Parquet Validation Warning", f"{msg}\n\nContinue anyway?")
                if not proceed:
                    return None

        return input_path, output_path, self.source_crs_var.get().strip() or None, selected_formats

    def start_conversion(self):
        validated = self.validate_inputs()
        if not validated:
            return

        self.clear_summary()
        self.update_progress(0)

        input_path, output_path, source_crs, selected_formats = validated
        worker = threading.Thread(
            target=self.run_conversion,
            args=(input_path, output_path, source_crs, selected_formats),
            daemon=True
        )
        worker.start()

    def run_conversion(self, input_path, output_path, source_crs, selected_formats):
        try:
            self.log("Starting conversion...")
            os.makedirs(output_path, exist_ok=True)
            files = get_input_files(input_path)

            if not files:
                self.log("No supported input files found.")
                self.update_progress(0)
                return

            total = len(files)
            for idx, f in enumerate(files, start=1):
                self.log("-" * 100)
                self.log(f"Processing {idx} of {total}: {f.name}")
                convert_one_file(
                    f,
                    output_path,
                    selected_formats,
                    source_crs=source_crs,
                    log_callback=self.log,
                    summary_callback=self.add_summary_row,
                )
                self.update_progress((idx / total) * 100)

            self.log("-" * 100)
            self.log("Conversion completed.")
            self.update_progress(100)
            self.root.after(0, lambda: messagebox.showinfo("Done", "Conversion completed successfully."))

        except Exception as e:
            err = traceback.format_exc()
            self.log("Conversion failed.")
            self.log(str(e))
            self.log(err)
            self.root.after(0, lambda: messagebox.showerror("Error", f"Conversion failed:\n{e}"))


def main():
    root = tk.Tk()
    GISConverterGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()