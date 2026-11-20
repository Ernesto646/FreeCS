#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Build an editable GoldSrc map and its prototype textures from a floor plan."""

import argparse
import json
import os
from pathlib import Path
import re
import struct
import subprocess
from tempfile import TemporaryDirectory


PLAYER_HALF_WIDTH = 16
PLAYER_HEIGHT = 72
SHELL_THICKNESS = 16
MAP_LIMIT = 4096
SPAWN_CLASSES = {"ct": "info_player_start", "t": "info_player_deathmatch"}
PALETTE = {
    "FC_FLOOR": ((119, 115, 105), (103, 99, 90)),
    "FC_WALL": ((158, 171, 175), (138, 156, 160)),
    "FC_CEILING": ((190, 189, 173), (172, 170, 152)),
    "FC_PROP": ((159, 109, 62), (136, 91, 50)),
    "AAATRIGGER": ((224, 151, 64), (174, 104, 49)),
}


def number(value, label):
    if type(value) not in (int, float) or not -1_000_000 <= value <= 1_000_000:
        raise ValueError(f"{label} must be a finite number between -1000000 and 1000000")
    return value


def array(value, label, length=None):
    if not isinstance(value, list) or (length is not None and len(value) != length):
        expected = "an array" if length is None else f"an array of {length} values"
        raise ValueError(f"{label} must be {expected}")
    return value


def coordinates(value, label, length):
    return tuple(number(item, label) for item in array(value, label, length))


def box(value, label, size):
    coords = coordinates(value, label, 6)
    if any(not 0 <= a < b <= edge for a, b, edge in zip(coords[:3], coords[3:], size)):
        raise ValueError(f"{label} must have positive volume and fit inside the room")
    return coords


def read_materials(plan):
    result = {name: ("checker", colors) for name, colors in PALETTE.items()}
    custom = plan.get("materials", {})
    if not isinstance(custom, dict):
        raise ValueError("materials must be an object")
    for name, spec in custom.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,14}", name) or not isinstance(spec, dict):
            raise ValueError(f"Invalid material definition: {name}")
        pattern = spec.get("pattern", "checker")
        if pattern not in ("checker", "tile", "grain"):
            raise ValueError(f"materials.{name}.pattern must be checker, tile or grain")
        colors = [tuple(array(color, f"materials.{name} RGB", 3))
                  for color in array(spec.get("colors"), f"materials.{name}.colors", 2)]
        if any(type(channel) is not int or not 0 <= channel <= 255 for color in colors for channel in color):
            raise ValueError(f"materials.{name} requires RGB integers from 0 to 255")
        result[name] = (pattern, colors)
    return result


def read_solids(plan, group, size, materials, default):
    result = []
    for index, entry in enumerate(array(plan.get(group, []), group)):
        label = f"{group}[{index}]"
        bounds = entry
        material = default
        if isinstance(entry, dict):
            bounds = entry.get("box")
            material = entry.get("material", default)
        if not isinstance(material, str) or material not in materials:
            raise ValueError(f"{label} references an unknown material: {material}")
        result.append((box(bounds, label, size), material))
    return result


def overlaps(first, second):
    return all(a < d and c < b for a, b, c, d in zip(first[:3], first[3:], second[:3], second[3:]))


def fmt(value):
    return f"{value:.6f}".rstrip("0").rstrip(".")


def point(coords):
    return " ".join(fmt(value) for value in coords)


def entity(classname, properties=(), brushes=()):
    lines = ["{", f'"classname" "{classname}"']
    lines.extend(f'"{key}" "{value}"' for key, value in properties)
    lines.extend(brushes)
    return "\n".join([*lines, "}"])


def brush(bounds, texture, scale):
    x0, y0, z0, x1, y1, z1 = (round(v * scale, 6) for v in bounds)
    if x0 >= x1 or y0 >= y1 or z0 >= z1:
        raise ValueError(f"Brush collapses at the selected scale: {bounds}")
    faces = (
        (((x0, y0, z0), (x0, y1, z0), (x0, y1, z1)), "0 1 0", "0 0 -1"),
        (((x1, y0, z0), (x1, y0, z1), (x1, y1, z1)), "0 1 0", "0 0 -1"),
        (((x0, y0, z0), (x0, y0, z1), (x1, y0, z1)), "1 0 0", "0 0 -1"),
        (((x0, y1, z0), (x1, y1, z0), (x1, y1, z1)), "1 0 0", "0 0 -1"),
        (((x0, y0, z0), (x1, y0, z0), (x1, y1, z0)), "1 0 0", "0 -1 0"),
        (((x0, y0, z1), (x0, y1, z1), (x1, y1, z1)), "1 0 0", "0 -1 0"),
    )
    lines = []
    for vertices, u, v in faces:
        plane = " ".join(f"( {point(vertex)} )" for vertex in vertices)
        lines.append(f"{plane} {texture} [ {u} 0 ] [ {v} 0 ] 0 {fmt(scale)} {fmt(scale)}")
    return "\n".join(["{", *lines, "}"])


def shell(size):
    x, y, z = size
    t = SHELL_THICKNESS
    return (
        ((-t, -t, -t, x + t, y + t, 0), "FC_FLOOR"),
        ((-t, -t, z, x + t, y + t, z + t), "FC_CEILING"),
        ((-t, -t, 0, 0, y + t, z), "FC_WALL"),
        ((x, -t, 0, x + t, y + t, z), "FC_WALL"),
        ((0, -t, 0, x, 0, z), "FC_WALL"),
        ((0, y, 0, x, y + t, z), "FC_WALL"),
    )


def check_clearance(x, y, height, size, solids, scale, label):
    radius = PLAYER_HALF_WIDTH / scale
    hull = (x - radius, y - radius, 0, x + radius, y + radius, height / scale)
    if any(a < 0 or b > edge for a, b, edge in zip(hull[:3], hull[3:], size)):
        raise ValueError(f"{label} needs room for a character inside the map")
    if any(overlaps(hull, solid) for solid in solids):
        raise ValueError(f"{label} overlaps a wall or prop")


def make_map(plan, scale_override):
    if not isinstance(plan, dict):
        raise ValueError("The plan must be a JSON object")
    name = plan.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"(?:cs|de)_[a-z0-9_]{1,28}", name):
        raise ValueError("name must start with cs_ or de_ and use only lowercase letters, digits and underscores")

    scale = number(plan.get("scale", 1) if scale_override is None else scale_override, "scale")
    if scale < 1:
        raise ValueError("scale must be at least 1")
    size = coordinates(plan.get("size"), "size", 3)
    if any(edge <= 0 or (edge + SHELL_THICKNESS) * scale >= MAP_LIMIT for edge in size):
        raise ValueError(f"Scaled room and shell must fit within {MAP_LIMIT} map units on each axis")

    materials = read_materials(plan)
    walls = read_solids(plan, "walls", size, materials, "FC_WALL")
    props = read_solids(plan, "props", size, materials, "FC_PROP")
    solids = [bounds for bounds, _ in walls + props]
    world = [*shell(size), *walls, *props]
    entities = [entity("worldspawn", (("mapversion", "220"), ("wad", f"{name}.wad")),
                       (brush(bounds, texture, scale) for bounds, texture in world))]

    spawns = plan.get("spawns")
    if not isinstance(spawns, dict):
        raise ValueError("spawns must define points for ct and t")
    spawn_hulls = []
    for team, classname in SPAWN_CLASSES.items():
        positions = array(spawns.get(team), f"spawns.{team}")
        if not positions:
            raise ValueError(f"spawns.{team} must contain at least one point")
        for index, item in enumerate(positions):
            label = f"spawns.{team}[{index}]"
            x, y, angle = coordinates(item, label, 3)
            check_clearance(x, y, PLAYER_HEIGHT + 1, size, solids, scale, label)
            px, py = x * scale, y * scale
            r = PLAYER_HALF_WIDTH
            hull = (px - r, py - r, 0, px + r, py + r, PLAYER_HEIGHT)
            if any(overlaps(hull, other) for other in spawn_hulls):
                raise ValueError(f"{label} overlaps another spawn point")
            spawn_hulls.append(hull)
            entities.append(entity(classname, (("origin", point((px, py, PLAYER_HEIGHT / 2 + 1))),
                                               ("angle", fmt(angle % 360)))))

    default_light = [[edge / 2 for edge in size]]
    lights = array(plan.get("lights", default_light), "lights")
    for item in lights:
        values = array(item, "lights entry")
        if len(values) not in (3, 4):
            raise ValueError("A light requires [x, y, z] and an optional intensity")
        light = coordinates(values[:3], "lights position", 3)
        intensity = number(values[3], "light intensity") if len(values) == 4 else 240
        if not 0 < intensity <= 2000:
            raise ValueError("Light intensity must be greater than 0 and at most 2000")
        if any(not 0 < value < edge for value, edge in zip(light, size)) or any(
            all(a <= v <= b for a, v, b in zip(solid[:3], light, solid[3:])) for solid in solids
        ):
            raise ValueError("Each light must be in open space inside the room")
        brightness = round(intensity * scale)
        entities.append(entity("light", (("origin", point(v * scale for v in light)),
                                         ("_light", f"255 246 225 {brightness}"))))

    objective = plan.get("objective")
    if not isinstance(objective, dict) or not objective.get("zones"):
        raise ValueError("objective must define at least one zone")
    classname = "func_bomb_target" if name.startswith("de_") else "func_hostage_rescue"
    for item in array(objective["zones"], "objective.zones"):
        zone = box(item, "objective zone", size)
        if any(overlaps(zone, solid) for solid in solids):
            raise ValueError("An objective zone overlaps a wall or prop")
        entities.append(entity(classname, brushes=(brush(zone, "AAATRIGGER", scale),)))

    hostages = array(objective.get("hostages", []), "objective.hostages")
    if name.startswith("cs_") != bool(hostages):
        raise ValueError("cs_ maps require hostages and de_ maps use objective zones only")
    for index, item in enumerate(hostages):
        label = f"objective.hostages[{index}]"
        x, y = coordinates(item, label, 2)
        check_clearance(x, y, PLAYER_HEIGHT + 1, size, solids, scale, label)
        entities.append(entity("hostage_entity", (("origin", point((x * scale, y * scale, 1))),
                                                  ("model", "models/hostage.mdl"))))
    return name, "\n".join(entities) + "\n", materials


def texture_pixels(size, pattern):
    if pattern == "tile":
        return (int(x == 0 or y == 0) for y in range(size) for x in range(size))
    if pattern == "grain":
        return (int((y * 7 // size) % 3 == 0) for y in range(size) for _ in range(size))
    tile = size // 4
    return ((x // tile + y // tile) % 2 for y in range(size) for x in range(size))


def write_wad(destination, materials):
    """Write indexed prototype textures and their four WAD3 mip levels."""
    directory = bytearray()
    with destination.open("wb") as wad:
        wad.write(bytes(12))
        for name, (pattern, colors) in materials.items():
            offsets = []
            pixels = bytearray()
            for size in (32, 16, 8, 4):
                offsets.append(40 + len(pixels))
                pixels.extend(texture_pixels(size, pattern))
            palette = bytes(colors[0] + colors[1]) + bytes(254 * 3)
            named = name.encode("ascii")
            miptex = struct.pack("<16s6I", named, 32, 32, *offsets)
            lump = miptex + pixels + struct.pack("<H", 256) + palette + bytes(2)
            directory.extend(struct.pack("<iiiBBBB16s", wad.tell(), len(lump), len(lump), 0x43, 0, 0, 0, named))
            wad.write(lump)
        directory_offset = wad.tell()
        wad.write(directory)
        wad.seek(0)
        wad.write(struct.pack("<4sii", b"WAD3", len(materials), directory_offset))


def compilers(directory):
    if not directory.is_dir():
        raise ValueError(f"Compiler directory does not exist: {directory}")
    files = {path.name.lower(): path for path in directory.iterdir() if path.is_file()}
    for stage in ("csg", "bsp", "vis", "rad"):
        names = (f"sdhl{stage}", f"hl{stage}")
        if os.name == "nt":
            names = tuple(f"{name}{suffix}" for name in names for suffix in ("_x64.exe", ".exe"))
        tool = next((files[name] for name in names if name in files), None)
        if tool is None:
            raise ValueError(f"Missing {stage.upper()} compiler in {directory}")
        yield tool


def compile_map(directory, stage, name):
    tools = list(compilers(directory))
    for index, tool in enumerate(tools):
        options = ["-wadinclude", name] if index == 0 else []
        try:
            subprocess.run([str(tool), *options, name], cwd=stage, check=True)
        except (OSError, subprocess.CalledProcessError) as error:
            raise ValueError(f"{tool.name} failed: {error}") from error
        if (stage / f"{name}.err").exists():
            raise ValueError(f"{tool.name} reported a build error")
        if (stage / f"{name}.lin").exists() or (stage / f"{name}.pts").exists():
            raise ValueError("The compiled map leaks outside the room")
    with (stage / f"{name}.bsp").open("rb") as compiled:
        if compiled.read(4) != struct.pack("<I", 30):
            raise ValueError("The compiler did not produce a GoldSrc BSP (version 30)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path, help="JSON floor plan")
    parser.add_argument("--output", type=Path, required=True, help="Directory for the generated files")
    parser.add_argument("--scale", type=float, help="Override the room and furniture scale in the plan")
    parser.add_argument("--compiler-dir", type=Path, help="Directory with the GoldSrc CSG/BSP/VIS/RAD binaries")
    args = parser.parse_args()

    try:
        with args.plan.open(encoding="utf-8-sig") as source:
            name, map_text, materials = make_map(json.load(source), args.scale)
        args.output.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="mapgen-", dir=args.output) as temp:
            stage = Path(temp)
            (stage / f"{name}.map").write_text(map_text, encoding="ascii", newline="\n")
            write_wad(stage / f"{name}.wad", materials)
            suffixes = ["map", "wad"]
            if args.compiler_dir:
                compile_map(args.compiler_dir.resolve(), stage, name)
                suffixes.append("bsp")
            for suffix in suffixes:
                output = args.output / f"{name}.{suffix}"
                (stage / output.name).replace(output)
                print(output.resolve())
    except (ValueError, OSError) as error:
        parser.exit(1, f"mapgen: {error}\n")


if __name__ == "__main__":
    main()
