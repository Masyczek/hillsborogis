# Hillsboro Growth & Development GIS Analysis

A reproducible geospatial analysis of construction activity, zoning, comprehensive planning, and the existing built environment in **Hillsboro, Oregon**.

**Data snapshot:** September 15, 2026  
**Study area:** City of Hillsboro, Oregon

**[Read the Full GIS Case Study](REPORT.md)**

## Research Question

How is Hillsboro growing, and how does current development relate to zoning, long-term planning, and the existing built environment?

## Project Overview

This project examines spatial relationships between municipal construction project boundaries, zoning districts, comprehensive-plan designations, and building footprints.

Publicly available GIS datasets were acquired from the City of Hillsboro's ArcGIS services, processed using Python, and analyzed in QGIS. Spatial intersections, acreage calculations, and building-level associations were summarized using pivot tables and visualized through maps and charts.

The analysis uses a preserved data snapshot rather than a continuously updated dashboard.

## Key Findings

- **Industrial development:** Within the analyzed Developer Industrial project intersections in Industrial Sanctuary zoning, infrastructure-related projects accounted for approximately 46.1% of overlap acreage, data centers for 35.7%, and other development for 18.1%.
- **Comprehensive planning:** Industrial development represented a substantial share of project-boundary overlap with planned land-use designations, while residential projects were concentrated in residential and mixed-use areas.
- **Building age:** Of 1,028 distinct buildings associated with construction project boundaries, 856 were built in 2010 or later, indicating a strong concentration of newer buildings within the analyzed project areas.

## Methodology

The reproducible workflow includes:

1. Acquisition of public GIS data through ArcGIS REST services.
2. Data cleaning, attribute standardization, and GeoPackage preparation using Python.
3. Reprojection to **NAD83 / Oregon GIC Lambert (EPSG:2992)** for consistent spatial analysis.
4. Spatial intersections and building associations using QGIS.
5. Acreage calculations, pivot-table summaries, and analytical visualizations.

## Analysis

Three primary spatial relationships were investigated:

- **REL-001:** Construction Projects × Zoning
- **REL-002:** Construction Projects × Comprehensive Plan
- **REL-003:** Construction Projects × Buildings

The complete methodology, maps, charts, findings, and limitations are documented in **[REPORT.md](REPORT.md)**.

## Tools

Python · Jupyter Notebook · ArcGIS REST API · GeoPackage · QGIS · Spreadsheet Pivot Tables

## Data and Limitations

All findings reflect municipal GIS records acquired on **September 15, 2026**.

Project-boundary intersection acreage does not necessarily represent physical construction footprints, and spatial overlap does not establish causation. See the full report for additional methodological details and limitations.