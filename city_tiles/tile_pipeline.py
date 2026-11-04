"""Tile generation pipeline used by generate_tiles.ipynb.

The notebook sets its options as plain variables (SCALE, TILE_SIZE, ...) and
passes them here with ``Options.from_namespace(globals())``. Each step of the
notebook then calls one function from this module:

    select_area(opts)                    -> shapes drawn on the map
    preview_layout(shapes, opts)         -> Layout (bounds, grid, buildings)
    generate_city_tiles(layout, opts)    -> STL tiles in processed_tiles/
    add_magnet_holes(opts)               -> runs blender_magnet_holes.py

Install the packages first (tile_setup.setup) before importing this module.
"""

import math
import subprocess
import sys
from dataclasses import dataclass, fields
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import trimesh
from IPython.display import display
from shapely.geometry import box

from dtcc_core.builder import (
    build_terrain_raster,
    clean_footprints,
    compute_building_heights,
    extract_roof_points,
)
from dtcc_core.builder.meshing import SurfaceMeshClipper, conform_boundary
from dtcc_core.io.data.wrapper import download_footprints, download_pointcloud
from dtcc_core.io.meshes import load_mesh
from dtcc_core.model import City
from dtcc_core.model.geometry.bounds import Bounds

HERE = Path(__file__).resolve().parent
BLENDER_SCRIPT = HERE / "blender_magnet_holes.py"


# --- Options -----------------------------------------------------------------

@dataclass
class Options:
    """All settings of the pipeline. Names match the notebook's variables."""

    # Section 1 of the notebook
    SCALE: float = 1250
    TILE_SIZE: float = 0.2
    MAGNET_RADIUS: float = 5
    MAGNET_HEIGHT: float = 2
    MAGNET_PERIMETER_OFFSET: float = 8
    BASE_TILE_THICKNESS: float = 20
    ANCHOR_Z_LEVEL: float = 0
    BUILDING_MARGIN: float = 50
    MANUAL_BOUNDS: bool = False
    ORIGIN_X: float = 319_470.0
    ORIGIN_Y: float = 6_397_410.0
    SIZE_X: float = 750
    SIZE_Y: float = 1250
    ALIGN: bool = False
    ALIGNMENT_NODE_X: float = 319_470.0
    ALIGNMENT_NODE_Y: float = 6_397_410.0

    # Other inputs, change only if necessary (can also be set in the notebook)
    RASTER_CELL_SIZE: float = 2.0
    RASTER_RADIUS: float = 3.0
    OUTLIER_STDDEV: float = 3.0
    MAX_MESH_SIZE: float = 5.0
    MIN_MESH_ANGLE: float = 25.0
    SMOOTHING: int = 3
    MERGE_MAX_DIST: float = 0.5   # Footprints closer than this are merged [m]
    MERGE_MIN_AREA: float = 10.0  # Buildings smaller than this are dropped [m2]
    MIN_HEIGHT: float = 3.0       # Minimum building height [m]

    TILES_DIR: Path = Path("raw_tiles")
    OUT_DIR: Path = Path("processed_tiles")
    OLD_MESHES_DIR: Path = Path("old_meshes")

    @classmethod
    def from_namespace(cls, ns):
        """Build Options from a dict such as the notebook's globals()."""
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in ns.items() if k in names})

    @property
    def grid_size(self):
        """Real-world size of one tile [m]."""
        return self.TILE_SIZE * self.SCALE


@dataclass
class Layout:
    """Result of the layout step, used by the mesh and tile steps."""

    bounds: Bounds
    n_tiles_x: int
    n_tiles_y: int
    buildings: list

    @property
    def min_x(self):
        return self.bounds.xmin

    @property
    def min_y(self):
        return self.bounds.ymin


# --- 1) Interactive map ------------------------------------------------------

def select_area(opts, center=(57.687845516523026, 11.976528661251745), zoom=13):
    """Show a map to draw the model bounds and exclusion shapes.

    Returns a list that fills with GeoJSON features as shapes are drawn.
    """
    import pyproj
    from ipyleaflet import DrawControl, Map, basemaps
    from ipywidgets import Output
    from shapely.geometry import shape
    from shapely.ops import transform as shp_transform

    shapes = []
    output = Output()

    map_widget = Map(basemap=basemaps.OpenStreetMap.Mapnik, center=center, zoom=zoom)
    map_widget.layout.width = "1000px"
    map_widget.layout.height = "600px"

    draw_control = DrawControl()
    draw_control.rectangle = {
        "shapeOptions": {"color": "#0000FF", "fillOpacity": 0.3},
        "showArea": True,
        "metric": True,
    }
    draw_control.polygon = {
        "shapeOptions": {"color": "#FF0000", "fillOpacity": 0.3},
        "showArea": True,
        "metric": True,
    }
    draw_control.polyline = {}
    draw_control.circle = {}
    draw_control.circlemarker = {}

    project = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3006", always_xy=True).transform

    def handle_draw(target, action, geo_json):
        if action == "created" and geo_json["geometry"]["type"] == "Polygon":
            coords = geo_json["geometry"]["coordinates"][0]
            is_rectangle = len(coords) == 5

            geom_sweref = shp_transform(project, shape(geo_json["geometry"]))
            minx, miny, maxx, maxy = geom_sweref.bounds

            shapes.append({
                "type": "Feature",
                "geometry": geo_json["geometry"],
                "properties": {"shape": "rectangle" if is_rectangle else "polygon"},
            })

            label = "Rectangle" if is_rectangle else "Polygon"
            with output:
                print(f"─── Shape {len(shapes)} ({label}) ───────────────────")
                print(f"  E–W width : {maxx - minx:>8.0f} m")
                print(f"  N–S height: {maxy - miny:>8.0f} m")
                print(f"  Area      : {geom_sweref.area/1e6:>8.4f} km²")

    draw_control.on_draw(handle_draw)
    map_widget.add_control(draw_control)

    if opts.MANUAL_BOUNDS:
        print("Draw shapes around buildings to be excluded")
    else:
        print("1. Draw shape defining model bounds")
        print("2. Draw shapes around buildings to be excluded")
    print("-" * 50)
    display(map_widget, output)
    return shapes


# --- 2) Convert geojson ------------------------------------------------------

def convert_geojson(shapes, opts):
    """Split drawn shapes into (boundary, exclusions) in EPSG:3006."""
    boundary = []
    exclusions = []
    if len(shapes) > 0:
        gdf = gpd.GeoDataFrame.from_features(shapes, crs="EPSG:4326").to_crs(3006)
        boundary = gdf.geometry.iloc[0]
        exclusions = gdf.geometry if opts.MANUAL_BOUNDS else gdf.geometry.iloc[1:]
    return boundary, exclusions


# --- 3) Compute bounds -------------------------------------------------------

def compute_bounds(boundary, opts):
    """Model bounds snapped to whole tiles (and to the alignment grid)."""
    print("Computing area bounds…")
    grid_size = opts.grid_size

    if opts.MANUAL_BOUNDS:
        origin_x, origin_y, size_x, size_y = opts.ORIGIN_X, opts.ORIGIN_Y, opts.SIZE_X, opts.SIZE_Y
    else:
        if isinstance(boundary, list) or boundary.is_empty:
            raise ValueError("No model bounds: draw a rectangle on the map in section 3 first.")
        minx, miny, maxx, maxy = boundary.bounds
        size_x, size_y = maxx - minx, maxy - miny
        origin_x, origin_y = round(minx), round(miny)

    if opts.ALIGN:
        min_x = opts.ALIGNMENT_NODE_X + math.floor((origin_x - opts.ALIGNMENT_NODE_X) / grid_size) * grid_size
        min_y = opts.ALIGNMENT_NODE_Y + math.floor((origin_y - opts.ALIGNMENT_NODE_Y) / grid_size) * grid_size
    else:
        min_x, min_y = origin_x, origin_y

    max_x = min_x + math.ceil(((origin_x + size_x - min_x) / grid_size)) * grid_size
    max_y = min_y + math.ceil(((origin_y + size_y - min_y) / grid_size)) * grid_size

    bounds = Bounds(min_x, min_y, max_x, max_y)
    n_tiles_x = round((max_x - min_x) / grid_size)
    n_tiles_y = round((max_y - min_y) / grid_size)

    print(f"Model Size: {n_tiles_x * opts.TILE_SIZE:.2f} x {n_tiles_y * opts.TILE_SIZE:.2f} m ({n_tiles_x}x{n_tiles_y} tiles)")
    print(f"Min X: {min_x}  Min Y: {min_y}  Max X: {max_x}  Max Y: {max_y}")
    return bounds, n_tiles_x, n_tiles_y


# --- 4) Load buildings -------------------------------------------------------

def mesh_bounds(bounds, opts):
    """Model bounds expanded by BUILDING_MARGIN: the area that is meshed.

    With ALIGN the mesh edge must stay on the model edge so it can be
    conformed to the old meshes, so no margin is added.
    """
    m = 0 if opts.ALIGN else opts.BUILDING_MARGIN
    return Bounds(bounds.xmin - m, bounds.ymin - m, bounds.xmax + m, bounds.ymax + m)


def get_footprint(b):
    """2D footprint polygon of a building, or None if it has none.

    b.footprint() also finds footprints stored on building parts, which
    b.geometry[GeometryType.LOD0] does not.
    """
    surf = b.footprint()
    if surf is None:
        return None
    poly = surf.to_polygon()
    return None if poly.is_empty else poly


def load_buildings(bounds, exclusions, opts, preview_path="layout_preview.png"):
    """Download footprints, drop excluded buildings and save a layout preview."""
    min_x, min_y, max_x, max_y = bounds.xmin, bounds.ymin, bounds.xmax, bounds.ymax
    grid_size = opts.grid_size
    boundary = box(min_x, min_y, max_x, max_y)
    fig, ax = plt.subplots(figsize=(8, 8), dpi=120)
    ax.axis("off")
    gpd.GeoSeries([boundary]).plot(ax=ax, color="none", edgecolor="black", linewidth=1.0)

    print("Loading building footprints…")
    # Download over the expanded area, then keep every building that touches
    # the model, including those crossing its edge.
    buildings = download_footprints(bounds=mesh_bounds(bounds, opts), provider="dtcc")
    buildings = [b for b in buildings if get_footprint(b) is not None]
    # Merge footprints of the same building before exclusion, so an exclusion
    # shape removes whole buildings rather than some of their pieces.
    buildings = clean_footprints(
        buildings, min_building_detail=opts.MERGE_MAX_DIST, min_area=opts.MERGE_MIN_AREA
    )
    buildings = [b for b in buildings if get_footprint(b).intersects(boundary)]

    if len(exclusions) > 0:
        exclusion_union = exclusions.union_all()

        if not exclusion_union.intersects(boundary):
            print("Exclusion area is outside of model bounds")

        gpd.GeoSeries(exclusion_union).plot(ax=ax, color="none", edgecolor="red", linewidth=0.5)

        b_kept, kept_fp, removed_fp = [], [], []
        for b in buildings:
            fp = get_footprint(b)
            if exclusion_union.covers(fp):
                removed_fp.append(fp)
            else:
                b_kept.append(b)
                kept_fp.append(fp)

        if removed_fp:
            gpd.GeoSeries(removed_fp).plot(ax=ax, color="red")
        print(f"{len(removed_fp)} buildings excluded from mesh")
    else:
        b_kept = buildings
        kept_fp = [get_footprint(b) for b in b_kept]

    gpd.GeoSeries(kept_fp).plot(ax=ax, color="lightgray", zorder=0)

    for x in np.arange(min_x, max_x + grid_size, grid_size):
        ax.plot([x, x], [min_y, max_y], color="black", linestyle="--", linewidth=0.5, zorder=10)
    for y in np.arange(min_y, max_y + grid_size, grid_size):
        ax.plot([min_x, max_x], [y, y], color="black", linestyle="--", linewidth=0.5, zorder=10)

    ax.set_xlim(min_x, max_x)
    ax.set_ylim(min_y, max_y)
    fig.savefig(preview_path, dpi=300, bbox_inches="tight")

    return b_kept


def preview_layout(shapes, opts):
    """Notebook section 4: bounds, building footprints and layout preview."""
    boundary, exclusions = convert_geojson(shapes, opts)
    bounds, n_tiles_x, n_tiles_y = compute_bounds(boundary, opts)
    buildings = load_buildings(bounds, exclusions, opts)
    return Layout(bounds, n_tiles_x, n_tiles_y, buildings)


# --- 5) Load pointcloud ------------------------------------------------------

def load_pointcloud(bounds, opts):
    print("Downloading pointcloud…")
    return download_pointcloud(bounds=mesh_bounds(bounds, opts))


# --- 6) Build terrain + building heights -------------------------------------

def make_terrain_and_buildings(pointcloud, buildings_all, opts):
    print("Building terrain raster…")
    raster = build_terrain_raster(
        pointcloud,
        cell_size=opts.RASTER_CELL_SIZE,
        radius=opts.RASTER_RADIUS,
        ground_only=True,
    )

    print(f"Raster bounds: {raster.bounds}")

    print("Extracting roof points + computing heights…")

    # Filter buildings to those within the raster bounds
    rb = raster.bounds
    raster_box = box(rb.xmin, rb.ymin, rb.xmax, rb.ymax)
    buildings_in_raster = [
        b for b in buildings_all
        if raster_box.covers(get_footprint(b).centroid)
    ]
    print(f"Buildings within raster: {len(buildings_in_raster)}/{len(buildings_all)}")

    buildings = extract_roof_points(
        buildings_in_raster,
        pointcloud,
        statistical_outlier_remover=True,
        roof_outlier_neighbors=5,
        roof_outlier_margin=1.5,
    )
    # Heights are stored as estimated_height; buildings without usable roof
    # points get MIN_HEIGHT.
    buildings = compute_building_heights(
        buildings, raster, min_building_height=opts.MIN_HEIGHT, overwrite=True
    )

    return raster, buildings


# --- 7) Build city + mesh ----------------------------------------------------

def make_mesh(raster, buildings, bounds, opts):
    print("Creating city and meshing…")
    city = City()
    city.add_terrain(raster)
    city.add_buildings(buildings, remove_outside_terrain=False)

    # LOD1: footprints extruded to building height. Footprints closer than
    # MERGE_MAX_DIST are merged and buildings below MERGE_MIN_AREA dropped.
    mesh = city.build_surface_mesh(
        lod="lod1",
        min_building_detail=opts.MERGE_MAX_DIST,
        min_building_area=opts.MERGE_MIN_AREA,
        max_mesh_size=opts.MAX_MESH_SIZE,
        min_mesh_angle=opts.MIN_MESH_ANGLE,
        smoothing=opts.SMOOTHING,
        pipeline_mode="strict",
    )

    if opts.ALIGN:
        print("Conforming boundary to existing meshes…")
        old_meshes = [load_mesh(stl) for stl in Path(opts.OLD_MESHES_DIR).glob("*.stl")]
        mesh = conform_boundary(
            new_mesh=mesh,
            old_meshes=old_meshes,
            smoothing_iterations=5,
            tolerance=10.0,
        )

    # Save the mesh cut to the model bounds, so it can be used as an old mesh
    # for ALIGN when extending the model later.
    SurfaceMeshClipper(mesh).clip_to_bounds(bounds).save("raw_city_mesh.stl")
    return mesh


# --- 8) Tile, extrude, scale -------------------------------------------------

def generate_tiles(mesh, layout, opts):
    real_tile_size = opts.grid_size
    tiles_dir, out_dir = Path(opts.TILES_DIR), Path(opts.OUT_DIR)
    tiles_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    step = opts.BASE_TILE_THICKNESS / 1000 * opts.SCALE
    anchor = opts.ANCHOR_Z_LEVEL
    min_t = (opts.MAGNET_HEIGHT + opts.MAGNET_RADIUS + 1) / 1000 * opts.SCALE

    # Cut tiles on the model grid. The mesh extends beyond the model, so
    # buildings crossing the model edge are cut there like at any tile edge.
    clipper = SurfaceMeshClipper(mesh)
    min_x, min_y = layout.min_x, layout.min_y

    print("Cutting, extruding and scaling tiles...")

    for ix in range(layout.n_tiles_x):
        for iy in range(layout.n_tiles_y):
            row, col = ix + 1, iy + 1
            x0 = min_x + ix * real_tile_size
            y0 = min_y + iy * real_tile_size
            tile = clipper.clip_to_bounds(
                Bounds(x0, y0, x0 + real_tile_size, y0 + real_tile_size)
            )
            if len(tile.faces) == 0:
                print(f"Tile {row}_{col} is empty, skipped")
                continue
            tile.save(str(tiles_dir / f"raw_tile_{row}_{col}.stl"))

            min_z = float(tile.vertices[:, 2].min())
            base_level = anchor + math.floor((min_z - min_t - anchor) / step) * step

            out_path = str(out_dir / f"tile_{row}_{col}.stl")
            tile.create_printable_solid(base_z=base_level).save(out_path)

            tri = trimesh.load(out_path)
            tri.apply_scale(1.0 / opts.SCALE)
            tri.apply_translation([-min_x / opts.SCALE, -min_y / opts.SCALE, 0])
            tri.export(out_path)


def generate_city_tiles(layout, opts):
    """Notebook section 5: point cloud, terrain, city mesh and printable tiles."""
    pointcloud = load_pointcloud(layout.bounds, opts)
    raster, buildings = make_terrain_and_buildings(pointcloud, layout.buildings, opts)
    mesh = make_mesh(raster, buildings, layout.bounds, opts)
    generate_tiles(mesh, layout, opts)
    return mesh


# --- 9) Magnet holes (Blender) -----------------------------------------------

def add_magnet_holes(opts, blender="blender"):
    """Notebook section 6: run blender_magnet_holes.py on processed_tiles/."""
    tiles_dir = str(Path(opts.OUT_DIR).resolve())
    cmd = [
        blender, "-b", "--python-exit-code", "1", "-P", str(BLENDER_SCRIPT), "--",
        "--in_dir", tiles_dir,
        "--out_dir", tiles_dir,
        "--tile_size", str(opts.TILE_SIZE),
        "--magnet_radius_mm", str(opts.MAGNET_RADIUS),
        "--magnet_height_mm", str(opts.MAGNET_HEIGHT),
        "--magnet_perimeter_offset_mm", str(opts.MAGNET_PERIMETER_OFFSET),
    ]
    # Stream Blender's output into the notebook as it runs.
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as proc:
        for line in proc.stdout:
            sys.stdout.write(line)
    if proc.returncode != 0:
        raise RuntimeError(f"Blender exited with code {proc.returncode}")
