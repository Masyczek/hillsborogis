# --------------------------------------------------
# ArcGIS Feature JSON → GeoPackage Conversion
# --------------------------------------------------
#
# Purpose:
# Convert cleaned ArcGIS Feature JSON datasets into
# GeoPackage files that can be efficiently opened
# and analyzed in QGIS.
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
from shapely.geometry import Polygon, MultiPolygon
from shapely.validation import explain_validity


# --------------------------------------------------
# Project Paths
# --------------------------------------------------
# Define the directory containing the cleaned datasets
# for this analysis snapshot.

DATA_VERSION = "2026-09-08"

DATA_ROOT = Path(__file__).parent / DATA_VERSION / "datasets"

PROCESSED_ROOT = DATA_ROOT / "processed"

# Store QGIS working files alongside the processed
# datasets for this snapshot.

QGIS_ROOT = DATA_ROOT / "qgis"

# Define the datasets we want to convert.
# The same script can therefore be reused for
# multiple Hillsboro GIS datasets.

DATASETS = [
    "HIL-005_cleaned",
    "HIL-006_cleaned",
]


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

    if data.get("geometryType") != "esriGeometryPolygon":
        raise ValueError(
            f"{dataset_id} has unsupported geometryType: "
            f"{data.get('geometryType')!r}. This script "
            "only supports esriGeometryPolygon."
        )

    # --------------------------------------------------
    # Determine CRS
    # --------------------------------------------------
    # Use the spatial reference recorded in the dataset
    # itself rather than assuming a fixed CRS.

    spatial_reference = data.get("spatialReference") or {}
    wkid = spatial_reference.get("latestWkid") or spatial_reference.get("wkid")

    if wkid is None:
        raise ValueError(
            f"{dataset_id} does not contain a valid spatialReference."
        )

    crs = f"EPSG:{wkid}"

    # --------------------------------------------------
    # Extract features
    # --------------------------------------------------
    # Each feature contains an attribute dictionary and
    # an ArcGIS geometry object made of polygon rings.

    features = data["features"]

    print(f"Features found: {len(features):,}")

    # --------------------------------------------------
    # Build GeoPandas records
    # --------------------------------------------------
    # Convert each feature's Esri rings into a Shapely
    # geometry while preserving its attributes.

    records = []

    for feature in features:
        attributes = feature["attributes"].copy()

        geometry = feature.get("geometry")
        rings = geometry.get("rings") if geometry else None

        attributes["geometry"] = (
            esri_rings_to_shapely(rings) if rings else None
        )

        records.append(attributes)

    gdf = gpd.GeoDataFrame(records, geometry="geometry", crs=crs)

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
                f"OBJECTID: {row.get('OBJECTID')} | "
                f"BLDG_ID: {row.get('BLDG_ID')}"
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
                f"OBJECTID: {row.get('OBJECTID')} | "
                f"BLDG_ID: {row.get('BLDG_ID')} | "
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
    print(f"{dataset_id} conversion complete.")


# --------------------------------------------------
# Run conversions
# --------------------------------------------------
# Process every dataset listed in DATASETS.

if __name__ == "__main__":
    for dataset_id in DATASETS:
        convert_arcgis_json_to_gpkg(dataset_id)

    print("\nAll conversions completed successfully.")