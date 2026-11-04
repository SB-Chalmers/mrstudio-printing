"""Add magnet holes to printable city tiles. Runs inside Blender 4.1.

Called by tile_pipeline.add_magnet_holes() from section 6 of the notebook:

    blender -b -P blender_magnet_holes.py -- --in_dir processed_tiles --out_dir processed_tiles \
        --tile_size 0.2 --magnet_radius_mm 5 --magnet_height_mm 2 --magnet_perimeter_offset_mm 8

Every STL in --in_dir gets four corner magnet pockets with a countersink cone
above each one, and is written to --out_dir under the same name.
"""
import addon_utils
import bpy, sys, os, time, glob
from mathutils import Vector


# enable STL add-on (provides import/export operators)
addon_utils.enable("io_mesh_stl", default_set=True, persistent=True)


def get_arg(name, default=None, cast=str):
    argv = sys.argv
    if "--" not in argv:
        return default
    argv = argv[argv.index("--") + 1:]
    if name not in argv:
        return default
    i = argv.index(name)
    if i + 1 >= len(argv):
        return default
    return cast(argv[i + 1])


# === Parameters ===

in_dir = get_arg("--in_dir", "./tiles_output", str)
out_dir = get_arg("--out_dir", "./tiles_processed", str)

tile_size = get_arg("--tile_size", 0.20, float)
magnet_radius_mm = get_arg("--magnet_radius_mm", 5.0, float)
magnet_height_mm = get_arg("--magnet_height_mm", 2.0, float)
magnet_perimeter_offset = get_arg("--magnet_perimeter_offset_mm", 8.0, float)

# Convert mm to m
magnet_radius = magnet_radius_mm / 1000.0
magnet_height = magnet_height_mm / 1000.0

os.makedirs(out_dir, exist_ok=True)

# Discover all tile STL files in input directory
tile_files = sorted(glob.glob(os.path.join(in_dir, "*.stl")))
total_tiles = len(tile_files)

start_time = time.time()
print(f"Processing {total_tiles} tile(s) from {in_dir}", flush=True)


def add_corner_magnet_holes(tile_obj, tile_index):
    """Create 4 magnet hole cutters at the corners of the tile."""
    # Get tile mesh bounds to determine position
    b = [v.co for v in tile_obj.data.vertices]
    if not b:
        return None

    min_v = Vector((min(v[0] for v in b), min(v[1] for v in b), min(v[2] for v in b)))
    max_v = Vector((max(v[0] for v in b), max(v[1] for v in b), max(v[2] for v in b)))

    tile_center_x = (min_v.x + max_v.x) / 2.0
    tile_center_y = (min_v.y + max_v.y) / 2.0
    tile_bottom_z = min_v.z

    # Magnet hole cylinder parameters
    offset_m = magnet_perimeter_offset / 1000.0
    safety = 0.001
    effective_offset = max(offset_m, magnet_radius + safety)
    magnet_cylinder_extra = 0.0005
    magnet_cylinder_depth = magnet_height + magnet_cylinder_extra * 2
    magnet_z = tile_bottom_z + (magnet_height / 2)

    half = tile_size / 2.0
    dx = half - effective_offset
    dy = half - effective_offset

    corner_positions = [
        (tile_center_x - dx, tile_center_y - dy),
        (tile_center_x + dx, tile_center_y - dy),
        (tile_center_x - dx, tile_center_y + dy),
        (tile_center_x + dx, tile_center_y + dy),
    ]

    cutters = []

    for i, (cx, cy) in enumerate(corner_positions):
        # Magnet hole cylinder
        bpy.ops.mesh.primitive_cylinder_add(
            radius=magnet_radius,
            depth=magnet_cylinder_depth,
            location=(cx, cy, magnet_z)
        )
        cyl = bpy.context.active_object
        cyl.name = f"MagnetHole_tile_{tile_index}_{i}"
        cutters.append(cyl)

        # Countersink cone
        cone_base_radius = magnet_radius
        cone_height = cone_base_radius
        cone_base_z = tile_bottom_z + magnet_height
        cone_center_z = cone_base_z + cone_height / 2.0

        bpy.ops.mesh.primitive_cone_add(
            radius1=cone_base_radius,
            radius2=0.0,
            depth=cone_height,
            location=(cx, cy, cone_center_z),
            rotation=(0.0, 0.0, 0.0)
        )
        cone = bpy.context.active_object
        cone.name = f"MagnetCountersink_tile_{tile_index}_{i}"
        cutters.append(cone)

    # Union all cutters
    union_obj = cutters[0]
    for i, cutter in enumerate(cutters[1:], start=1):
        bpy.ops.object.select_all(action='DESELECT')
        union_obj.select_set(True)
        bpy.context.view_layer.objects.active = union_obj

        union_mod = union_obj.modifiers.new(name=f"Union_{i}", type='BOOLEAN')
        union_mod.object = cutter
        union_mod.operation = 'UNION'
        union_mod.solver = 'EXACT'

        bpy.ops.object.modifier_apply(modifier=union_mod.name)
        bpy.data.objects.remove(cutter, do_unlink=True)

    union_obj.name = f"MagnetCutters_tile_{tile_index}"
    return union_obj


# Main processing loop
for tile_index, tile_path in enumerate(tile_files, start=1):
    tile_start = time.time()
    tile_name = os.path.basename(tile_path)

    print(f"[Tile {tile_index}/{total_tiles}] Importing {tile_name}...", flush=True)

    # Clear scene
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)

    # Import tile
    bpy.ops.import_mesh.stl(filepath=tile_path)
    tile_obj = bpy.context.selected_objects[0]
    tile_obj.name = f"Tile_{tile_index}"

    # Clean up mesh
    bpy.context.view_layer.objects.active = tile_obj
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.remove_doubles(threshold=0.0001)
    try:
        bpy.ops.mesh.fill_holes()
    except Exception:
        pass
    bpy.ops.mesh.normals_make_consistent(inside=False)
    bpy.ops.object.mode_set(mode='OBJECT')

    print(f"[Tile {tile_index}/{total_tiles}] Adding magnet holes...", flush=True)

    # Create magnet hole cutters
    magnet_cutter_obj = add_corner_magnet_holes(tile_obj, tile_index)

    if magnet_cutter_obj is not None:
        # Apply magnet holes as boolean difference
        mod_magnet = tile_obj.modifiers.new(name="MagnetCutters", type='BOOLEAN')
        mod_magnet.object = magnet_cutter_obj
        mod_magnet.operation = 'DIFFERENCE'
        mod_magnet.solver = 'EXACT'
        mod_magnet.use_self = True
        mod_magnet.use_hole_tolerant = True

        bpy.ops.object.select_all(action='DESELECT')
        bpy.context.view_layer.objects.active = tile_obj

        try:
            bpy.ops.object.modifier_apply(modifier="MagnetCutters")
        except RuntimeError:
            print(f"[Tile {tile_index}/{total_tiles}] WARNING: Failed to apply magnet cutters.", flush=True)

        # Delete cutter
        bpy.data.objects.remove(magnet_cutter_obj, do_unlink=True)

    # Export processed tile
    bpy.ops.object.select_all(action='DESELECT')
    tile_obj.select_set(True)
    export_path = os.path.join(out_dir, tile_name)
    bpy.ops.export_mesh.stl(filepath=export_path, use_selection=True)

    print(f"[Tile {tile_index}/{total_tiles}] Exported to {export_path} ({time.time() - tile_start:.0f} s)", flush=True)

    bpy.data.objects.remove(tile_obj, do_unlink=True)

elapsed_s = time.time() - start_time
print(f"Done. {total_tiles} tile(s) processed in {elapsed_s:.0f} s.", flush=True)
