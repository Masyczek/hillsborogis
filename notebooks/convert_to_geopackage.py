# --------------------------------------------------
# ArcGIS Feature JSON → GeoPackage Conversion
# --------------------------------------------------
#
# Purpose:
# Convert cleaned ArcGIS Feature JSON datasets into
# GeoPackage files that can be efficiently opened
# and analyzed in QGIS. All cleaned attributes are
# carried through unchanged, including original coded
# fields.
#
# For every ArcGIS coded-value domain found in a dataset's
# preserved metadata sidecar (*_metadata.json), this script
# also generates a companion <FIELD>_LABEL column from the
# domain's authoritative code -> name mapping (see
# load_coded_value_domains / resolve_domain_label). The
# preserved metadata sidecar is the only source of label
# text; no dataset- or field-specific mappings are hard-coded
# here.
#
# Point, polyline, and polygon ArcGIS geometry types
# are all supported through a single generic dispatch
# (see esri_geometry_to_shapely), so the same function
# converts every HIL dataset regardless of layer type.
#
# The original JSON files are NOT modified. A small
# number of leftover invalid geometries are repaired
# in-memory during conversion (see _repair_geometry).
#
# --------------------------------------------------

# Import standard-library modules for file handling and JSON processing.
import json
from pathlib import Path

# Import GeoPandas for building the GeoDataFrame and writing GeoPackage data.
import geopandas as gpd

# Import Shapely for building, repairing, and validating geometries.
from shapely import make_valid
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, Polygon
from shapely.validation import explain_validity


# --------------------------------------------------
# Project Paths
# --------------------------------------------------
# Define the directory containing the cleaned datasets
# for this analysis snapshot.

DATA_VERSION = "2026-09-15"

DATA_ROOT = Path(__file__).parent / DATA_VERSION / "datasets"

PROCESSED_ROOT = DATA_ROOT / "processed"

# Preserved ArcGIS raw feature snapshots and their layer-definition
# metadata sidecars ("<id>_metadata.json") live here. Only the
# metadata sidecars are read during conversion; raw feature data is
# never used as a conversion input.

RAW_ROOT = DATA_ROOT / "raw"

# Store QGIS working files alongside the processed
# datasets for this snapshot.

QGIS_ROOT = DATA_ROOT / "qgis"

# Define the datasets we want to convert.
# The same script can therefore be reused for
# multiple Hillsboro GIS datasets.

DATASETS = [
    "HIL-001_cleaned",
    "HIL-002_cleaned",
    "HIL-003_cleaned",
    "HIL-004_cleaned",
    "HIL-005_cleaned",
    "HIL-006_cleaned",
    "HIL-007_cleaned",
    "HIL-008_cleaned",
    "HIL-009_cleaned",
]

# Esri geometryType values this script knows how to convert,
# mapped to the Shapely geometry type(s) a GeoPackage layer
# is expected to contain after conversion.

GEOMETRY_TYPE_FAMILIES = {
    "esriGeometryPoint": {"Point", "MultiPoint"},
    "esriGeometryPolyline": {"LineString", "MultiLineString"},
    "esriGeometryPolygon": {"Polygon", "MultiPolygon"},
}


# --------------------------------------------------
# Create output directory
# --------------------------------------------------
# Create the QGIS working directory if it does not
# already exist.

QGIS_ROOT.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------
# Esri ring geometry helper
# --------------------------------------------------
# ArcGIS Feature JSON encodes polygons as a flat list of
# rings rather than GeoJSON's explicit exterior/hole
# nesting. Ring winding direction distinguishes exterior
# rings (clockwise) from interior/hole rings
# (counter-clockwise), and a single feature can contain
# multiple exterior rings (a MultiPolygon).

def _signed_area(ring):
    # Shoelace formula: positive area = counter-clockwise ring.
    total = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        total += (x1 * y2) - (x2 * y1)
    return total / 2.0


# Rings with essentially zero area are degenerate slivers
# (collinear or duplicate points) left over in the source
# data. They contribute no shape and can be misclassified
# as holes that fall outside the real shell, so they are
# dropped before grouping rings into polygons.
_DEGENERATE_AREA_TOLERANCE = 1e-6


def esri_rings_to_shapely(rings):
    """
    Convert Esri polygon rings into a Shapely Polygon or
    MultiPolygon, respecting ring winding order so that
    holes and multipart exteriors are preserved.
    """

    polygons = []

    for ring in rings:
        area = _signed_area(ring)

        if abs(area) < _DEGENERATE_AREA_TOLERANCE:
            continue

        if area < 0:
            # Clockwise ring: starts a new exterior polygon.
            polygons.append({"exterior": ring, "holes": []})
        else:
            # Counter-clockwise ring: a hole in the most recent exterior.
            if not polygons:
                raise ValueError(
                    "Encountered a hole ring before any exterior ring."
                )
            polygons[-1]["holes"].append(ring)

    shapely_polygons = [
        Polygon(part["exterior"], part["holes"]) for part in polygons
    ]

    if not shapely_polygons:
        return None
    if len(shapely_polygons) == 1:
        return shapely_polygons[0]
    return MultiPolygon(shapely_polygons)


# --------------------------------------------------
# Esri point / polyline geometry helpers
# --------------------------------------------------
# Point and polyline features use much simpler Esri Feature
# JSON structures than polygons, but still differ from
# GeoJSON's shape, so they get their own small builders.

def esri_point_to_shapely(geometry):
    """Convert an Esri point geometry ({"x": ..., "y": ...}) to a Shapely Point."""

    x = geometry.get("x")
    y = geometry.get("y")

    if x is None or y is None:
        return None

    return Point(x, y)


def esri_paths_to_shapely(paths):
    """
    Convert Esri polyline paths into a Shapely LineString or
    MultiLineString. A polyline feature can contain multiple
    disconnected paths.
    """

    lines = [LineString(path) for path in paths if len(path) >= 2]

    if not lines:
        return None
    if len(lines) == 1:
        return lines[0]
    return MultiLineString(lines)


# --------------------------------------------------
# Geometry dispatch
# --------------------------------------------------
# A single entry point used for every dataset regardless of
# its ArcGIS geometry type, so geometry handling stays generic
# instead of branching per dataset.

def esri_geometry_to_shapely(geometry, geometry_type):
    """Convert an Esri Feature JSON geometry object to Shapely, based on geometry_type."""

    if not geometry:
        return None

    if geometry_type == "esriGeometryPolygon":
        return esri_rings_to_shapely(geometry.get("rings") or [])

    if geometry_type == "esriGeometryPolyline":
        return esri_paths_to_shapely(geometry.get("paths") or [])

    if geometry_type == "esriGeometryPoint":
        return esri_point_to_shapely(geometry)

    raise ValueError(f"Unsupported ArcGIS geometryType: {geometry_type!r}")


# --------------------------------------------------
# Geometry repair helper
# --------------------------------------------------
# A handful of source features have self-intersecting or
# otherwise invalid rings that the cleaning notebooks did
# not fully repair. Rather than aborting the conversion,
# attempt an automatic repair via Shapely's make_valid and
# keep only the polygonal parts of the result.

def _repair_geometry(geometry):
    repaired = make_valid(geometry)

    if repaired.geom_type == "GeometryCollection":
        polygons = [
            part for part in repaired.geoms
            if part.geom_type in ("Polygon", "MultiPolygon")
        ]
        if not polygons:
            return repaired
        if len(polygons) == 1:
            return polygons[0]
        return MultiPolygon(polygons)

    return repaired


# --------------------------------------------------
# Coded-value domain enrichment
# --------------------------------------------------
# Reads the preserved ArcGIS metadata sidecar for a dataset and turns
# its coded-value domains into code -> label lookups. This is the only
# place domain interpretation happens; the resulting labels are the
# sole basis for the <FIELD>_LABEL columns added during conversion.

def load_coded_value_domains(dataset_id):
    """
    Load coded-value domains for a dataset from its preserved ArcGIS
    metadata sidecar, keyed by field name.

    Returns (domains, metadata_path), where domains is
    {field_name: [{"code": ..., "name": ...}, ...]}. domains is empty
    if the sidecar is missing or defines no coded-value domains.
    """

    base_id = dataset_id[: -len("_cleaned")] if dataset_id.endswith("_cleaned") else dataset_id
    metadata_path = RAW_ROOT / f"{base_id}_metadata.json"

    if not metadata_path.exists():
        return {}, metadata_path

    with metadata_path.open("r", encoding="utf-8") as file:
        metadata = json.load(file)

    domains = {}

    for field in metadata.get("fields", []) or []:
        domain = field.get("domain")

        if not domain or domain.get("type") != "codedValue":
            continue

        coded_values = domain.get("codedValues") or []

        if coded_values:
            domains[field["name"]] = coded_values

    return domains, metadata_path


def _domain_code_key(value):
    """
    Normalize a code to a numeric key so codes that differ only in
    JSON representation (e.g. int 0 vs. string "0") still match.
    Returns None for values that cannot be safely treated as numeric,
    so genuinely different string codes are never conflated.
    """

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def build_domain_lookup(coded_values):
    """
    Build exact and numeric-normalized code -> name lookups from a
    domain's codedValues list.
    """

    exact_lookup = {}
    normalized_lookup = {}

    for entry in coded_values:
        code = entry.get("code")
        name = entry.get("name")

        exact_lookup[code] = name

        key = _domain_code_key(code)
        if key is not None and key not in normalized_lookup:
            normalized_lookup[key] = name

    return exact_lookup, normalized_lookup


def resolve_domain_label(value, exact_lookup, normalized_lookup):
    """
    Resolve a source attribute value to its domain label.

    Returns (label, resolved): resolved is True for a null source
    value (label is None) or a value found in the domain; resolved is
    False when a non-null value has no matching coded value, in which
    case label is None and the caller must leave the code unexplained
    rather than guess.
    """

    if value is None:
        return None, True

    if value in exact_lookup:
        return exact_lookup[value], True

    key = _domain_code_key(value)
    if key is not None and key in normalized_lookup:
        return normalized_lookup[key], True

    return None, False


# --------------------------------------------------
# Conversion function
# --------------------------------------------------
# Convert one ArcGIS Feature JSON file into a
# GeoPackage layer.

def convert_arcgis_json_to_gpkg(dataset_id):
    """
    Convert a cleaned ArcGIS Feature JSON dataset
    into a GeoPackage suitable for QGIS.
    """

    # Define the source JSON file.
    input_path = PROCESSED_ROOT / f"{dataset_id}.json"

    # Define the output GeoPackage.
    output_path = QGIS_ROOT / f"{dataset_id}.gpkg"

    # Confirm that the input file exists before
    # attempting the conversion.
    if not input_path.exists():
        raise FileNotFoundError(
            f"Input file not found: {input_path}"
        )

    print(f"\nConverting {dataset_id}")
    print(f"Input:  {input_path}")
    print(f"Output: {output_path}")


    # --------------------------------------------------
    # Read ArcGIS Feature JSON
    # --------------------------------------------------
    # ArcGIS Feature JSON stores attributes and geometry
    # separately, so we parse the JSON directly instead of
    # relying on GDAL's ESRIJSON driver, which fails on
    # some of these cleaned datasets.

    with input_path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    geometry_type = data.get("geometryType")

    if geometry_type not in GEOMETRY_TYPE_FAMILIES:
        raise ValueError(
            f"{dataset_id} has unsupported geometryType: "
            f"{geometry_type!r}."
        )

    # The identifier field varies in name across ArcGIS layers,
    # so read it from the dataset rather than assuming "OBJECTID".
    oid_field = data.get("objectIdFieldName", "OBJECTID")

    # --------------------------------------------------
    # Load coded-value domains from the metadata sidecar
    # --------------------------------------------------
    # The processed/cleaned JSON is still the feature-data input; the
    # preserved ArcGIS metadata sidecar is only consulted here, for its
    # authoritative coded-value domains.

    domains, metadata_path = load_coded_value_domains(dataset_id)

    if domains:
        print(f"Metadata sidecar: {metadata_path}")
        print(
            "Coded-value domain fields discovered: "
            f"{', '.join(sorted(domains))}"
        )
    else:
        print(
            "Metadata sidecar: "
            f"{metadata_path if metadata_path.exists() else 'not found'}"
        )
        print("Coded-value domain fields discovered: none")

    processed_field_names = {
        field.get("name") or field.get("field")
        for field in (data.get("fields") or [])
    }

    active_domains = {}
    skipped_domain_fields = []

    for field_name, coded_values in domains.items():
        if field_name not in processed_field_names:
            skipped_domain_fields.append(field_name)
            continue
        active_domains[field_name] = build_domain_lookup(coded_values)

    if skipped_domain_fields:
        print(
            "Coded-domain field(s) in metadata but absent from the "
            f"processed dataset (skipped): {', '.join(sorted(skipped_domain_fields))}"
        )

    # --------------------------------------------------
    # Determine CRS
    # --------------------------------------------------
    # Use the spatial reference recorded in the dataset
    # itself rather than assuming a fixed CRS.

    spatial_reference = data.get("spatialReference") or {}
    wkid = spatial_reference.get("latestWkid") or spatial_reference.get("wkid")

    if wkid is not None:
        crs = f"EPSG:{wkid}"
    elif spatial_reference.get("wkt"):
        # Some layers (e.g. state-plane sources) omit a wkid and only
        # publish a WKT spatial reference string.
        crs = spatial_reference["wkt"]
    else:
        raise ValueError(
            f"{dataset_id} does not contain a valid spatialReference."
        )

    # --------------------------------------------------
    # Extract features
    # --------------------------------------------------
    # Each feature contains an attribute dictionary and an
    # ArcGIS geometry object (rings, paths, or x/y depending
    # on geometryType).

    features = data["features"]

    print(f"Features found: {len(features):,}")

    # --------------------------------------------------
    # Build GeoPandas records
    # --------------------------------------------------
    # Convert each feature's Esri geometry into a Shapely
    # geometry while preserving all of its cleaned attributes,
    # and generate a <FIELD>_LABEL column for every active
    # coded-value domain field.

    records = []
    resolved_counts = {field_name: 0 for field_name in active_domains}
    unresolved_codes = {field_name: set() for field_name in active_domains}

    for feature in features:
        attributes = feature["attributes"].copy()
        attributes["geometry"] = esri_geometry_to_shapely(
            feature.get("geometry"), geometry_type
        )

        for field_name, (exact_lookup, normalized_lookup) in active_domains.items():
            source_value = attributes.get(field_name)
            label, resolved = resolve_domain_label(
                source_value, exact_lookup, normalized_lookup
            )
            attributes[f"{field_name}_LABEL"] = label

            if source_value is not None:
                if resolved:
                    resolved_counts[field_name] += 1
                else:
                    unresolved_codes[field_name].add(source_value)

        records.append(attributes)

    gdf = gpd.GeoDataFrame(records, geometry="geometry", crs=crs)

    if active_domains:
        print("\nGenerated *_LABEL fields:")
        for field_name in sorted(active_domains):
            label_field = f"{field_name}_LABEL"
            resolved = resolved_counts[field_name]
            unresolved = sorted(unresolved_codes[field_name], key=str)

            message = f"  {label_field}: {resolved:,} value(s) resolved"
            if unresolved:
                message += f" | unresolved codes: {unresolved}"
            print(message)
    else:
        print(
            "\nGenerated *_LABEL fields: none "
            "(no coded-value domains apply to this dataset)"
        )

    # --------------------------------------------------
    # Drop features with no geometry
    # --------------------------------------------------
    # A feature whose rings were entirely degenerate
    # slivers has no shape to convert at all.

    missing_mask = gdf.geometry.isna()
    missing_count = int(missing_mask.sum())

    if missing_count > 0:
        print(f"\nDropping {missing_count:,} feature(s) with no geometry:")
        for index, row in gdf.loc[missing_mask].iterrows():
            print(
                f"  Index: {index} | "
                f"{oid_field}: {row.get(oid_field)}"
            )
        gdf = gdf.loc[~missing_mask].reset_index(drop=True)

    # --------------------------------------------------
    # Validate and repair geometry
    # --------------------------------------------------
    # Confirm that the cleaned dataset contains valid
    # geometries before writing the GeoPackage. A few
    # source features still have self-intersecting rings
    # that the cleaning notebooks did not fully repair, so
    # those are automatically repaired here rather than
    # aborting the conversion.

    invalid_mask = ~gdf.geometry.is_valid
    invalid_count = int(invalid_mask.sum())

    print(f"Invalid geometries: {invalid_count:,}")

    if invalid_count > 0:
        print("\nRepairing invalid geometries:")
        for index, row in gdf.loc[invalid_mask].iterrows():
            # Explain why each invalid geometry failed the Shapely validity check.
            reason = explain_validity(row.geometry)

            print(
                f"  Index: {index} | "
                f"{oid_field}: {row.get(oid_field)} | "
                f"Reason: {reason}"
            )

        gdf.loc[invalid_mask, "geometry"] = gdf.loc[invalid_mask, "geometry"].apply(
            _repair_geometry
        )

        still_invalid_mask = ~gdf.geometry.is_valid
        still_invalid_count = int(still_invalid_mask.sum())

        if still_invalid_count > 0:
            raise ValueError(
                f"{dataset_id} still contains "
                f"{still_invalid_count:,} invalid geometries "
                "after automatic repair. The cleaned dataset "
                "should be validated before conversion."
            )

        print(f"Repaired {invalid_count:,} geometries successfully.")

    print("Geometry validation: PASSED")


    # --------------------------------------------------
    # Write GeoPackage
    # --------------------------------------------------
    # Write the converted dataset to a GeoPackage.
    # The layer name matches the dataset ID.

    gdf.to_file(
        output_path,
        layer=dataset_id,
        driver="GPKG",
    )


    # --------------------------------------------------
    # Validate output
    # --------------------------------------------------
    # Re-open the GeoPackage and verify that the number
    # of features survived the conversion.

    output_gdf = gpd.read_file(
        output_path,
        layer=dataset_id,
    )

    input_count = len(gdf)
    output_count = len(output_gdf)

    print(f"Input features:  {input_count:,}")
    print(f"Output features: {output_count:,}")

    if input_count != output_count:
        raise ValueError(
            f"Record count mismatch for {dataset_id}: "
            f"{input_count:,} input vs "
            f"{output_count:,} output."
        )

    print("Record count validation: PASSED")

    # --------------------------------------------------
    # Validate geometry type
    # --------------------------------------------------
    # Confirm the GeoPackage layer's geometry type matches
    # the source ArcGIS geometryType (single- or multi-part).

    expected_geom_types = GEOMETRY_TYPE_FAMILIES[geometry_type]
    actual_geom_types = set(output_gdf.geom_type.dropna().unique())

    if not actual_geom_types <= expected_geom_types:
        raise ValueError(
            f"{dataset_id} produced unexpected geometry type(s) "
            f"{sorted(actual_geom_types - expected_geom_types)} for "
            f"ArcGIS geometryType {geometry_type!r}."
        )

    print(
        "Geometry type validation: PASSED "
        f"({', '.join(sorted(actual_geom_types)) or 'no features'})"
    )

    # --------------------------------------------------
    # Validate CRS
    # --------------------------------------------------

    if output_gdf.crs is None:
        raise ValueError(f"{dataset_id} GeoPackage layer has no CRS.")

    print(f"CRS validation: PASSED ({output_gdf.crs.srs})")

    # --------------------------------------------------
    # Validate attribute retention
    # --------------------------------------------------
    # Every cleaned attribute - original coded fields,
    # generated *_LABEL fields, and any other cleaned/derived
    # fields - must survive the round trip unchanged. This
    # script does not resolve coded-value domains itself, so
    # it only confirms that whatever the cleaning stage
    # produced was preserved.

    input_columns = set(gdf.columns) - {"geometry"}
    output_columns = set(output_gdf.columns) - {"geometry"}
    missing_columns = input_columns - output_columns

    if missing_columns:
        raise ValueError(
            f"{dataset_id} lost attribute(s) during conversion: "
            f"{sorted(missing_columns)}"
        )

    label_fields = sorted(
        column for column in output_columns if column.endswith("_LABEL")
    )

    print(
        f"Attribute validation: PASSED ({len(output_columns)} fields retained)"
    )

    if label_fields:
        print(f"Generated *_LABEL fields retained: {', '.join(label_fields)}")
    else:
        print(
            "Generated *_LABEL fields retained: none present "
            "(no coded-value domains resolved for this dataset)"
        )

    print(f"{dataset_id} conversion complete.")


# --------------------------------------------------
# Run conversions
# --------------------------------------------------
# Process every dataset listed in DATASETS.

if __name__ == "__main__":
    for dataset_id in DATASETS:
        convert_arcgis_json_to_gpkg(dataset_id)

    print("\nAll conversions completed successfully.")