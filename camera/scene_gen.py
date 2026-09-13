#!/usr/bin/env python3
"""Turn a small camera.json into a full Chunky scene description.

camera.json fields (all optional except position):
  x, y, z            camera position (world coords)
  yaw, pitch         Minecraft-convention degrees (yaw 0=+Z, pitch +90=straight down)
  target             {"x":..,"y":..,"z":..} - overrides yaw/pitch, camera looks at this point
  fov                degrees, default 70
  width, height      default 1280x720
  samples            target SPP, default 64
  projection         PINHOLE (default) | PARALLEL | FISHEYE | PANORAMIC
  chunk_radius       chunks loaded around the camera/target, default 6
  dimension          default minecraft:overworld
  sky_light          0..1 ambient sky multiplier, default 1.0
"""
import json, math, os, struct, sys

SDF_VERSION = 9


def mc_angles_to_chunky(yaw_deg, pitch_deg):
    """Chunky stores radians with its own zero points.

    The mapping is Chunky's own, from Camera.moveToPlayer:
        chunky_pitch = radians(mc_pitch - 90)
        chunky_yaw   = radians(90 - mc_yaw)
    so Chunky pitch 0 looks straight down and MC pitch 0 is the horizon.
    """
    return math.radians(90.0 - yaw_deg), math.radians(pitch_deg - 90.0)


def look_at(pos, target):
    """Minecraft-convention yaw/pitch that points from pos to target."""
    dx = target["x"] - pos[0]
    dy = target["y"] - pos[1]
    dz = target["z"] - pos[2]
    horiz = math.hypot(dx, dz)
    yaw = math.degrees(math.atan2(-dx, dz))
    pitch = math.degrees(math.atan2(-dy, horiz))
    return yaw, pitch


def existing_chunks(world_dir, dimension):
    """Chunks that are actually present, read straight from the region headers.

    Loading a chunk that was never generated makes Chunky log a warning per chunk,
    so we intersect the requested box with what the .mca files really contain.
    """
    ns, name = dimension.split(":", 1)
    region_dir = os.path.join(world_dir, "dimensions", ns, name, "region")
    if not os.path.isdir(region_dir):  # pre-26.x layout
        region_dir = os.path.join(world_dir, "region")
    present = set()
    if not os.path.isdir(region_dir):
        return present
    for fn in os.listdir(region_dir):
        parts = fn.split(".")
        if len(parts) != 4 or parts[0] != "r" or parts[3] != "mca":
            continue
        rx, rz = int(parts[1]), int(parts[2])
        with open(os.path.join(region_dir, fn), "rb") as fh:
            hdr = fh.read(4096)
        if len(hdr) < 4096:
            continue
        for i in range(1024):
            if struct.unpack_from(">I", hdr, i * 4)[0] >> 8:
                present.add((rx * 32 + (i % 32), rz * 32 + (i // 32)))
    return present


def build(cam, world_dir, name="snap", scan_dir=None):
    """world_dir is the path recorded in the scene; scan_dir is where we read
    the region files from now (they differ when Chunky runs in a container)."""
    pos = (float(cam["x"]), float(cam["y"]), float(cam["z"]))
    if cam.get("target"):
        yaw, pitch = look_at(pos, cam["target"])
    else:
        yaw, pitch = float(cam.get("yaw", 0.0)), float(cam.get("pitch", 0.0))
    c_yaw, c_pitch = mc_angles_to_chunky(yaw, pitch)

    dimension = cam.get("dimension", "minecraft:overworld")
    radius = int(cam.get("chunk_radius", 6))

    # Centre the loaded box on whatever we are looking at, else on the camera.
    focus = cam.get("target") or {"x": pos[0], "z": pos[2]}
    cx, cz = int(focus["x"]) >> 4, int(focus["z"]) >> 4
    present = existing_chunks(scan_dir or world_dir, dimension)
    wanted = [
        [x, z]
        for x in range(cx - radius, cx + radius + 1)
        for z in range(cz - radius, cz + radius + 1)
        if (x, z) in present
    ]

    return {
        "sdfVersion": SDF_VERSION,
        "name": name,
        "width": int(cam.get("width", 1280)),
        "height": int(cam.get("height", 720)),
        "exposure": float(cam.get("exposure", 1.0)),
        "postprocess": "GAMMA",
        "outputMode": "PNG",
        "renderTime": 0,
        "spp": 0,
        "sppTarget": int(cam.get("samples", 64)),
        "rayDepth": int(cam.get("ray_depth", 5)),
        "pathTrace": True,
        "dumpFrequency": 0,
        "saveSnapshots": False,
        "emittersEnabled": True,
        "emitterIntensity": 13.0,
        "sunSamplingStrategy": "FAST",
        "transparentSky": False,
        "biomeColorsEnabled": True,
        "waterWorldEnabled": False,
        "hideUnknownBlocks": False,
        "yClipMin": int(cam.get("y_clip_min", -64)),
        "yClipMax": int(cam.get("y_clip_max", 320)),
        "yMin": int(cam.get("y_clip_min", -64)),
        "yMax": int(cam.get("y_clip_max", 320)),
        "world": {"path": world_dir, "dimension": dimension},
        "camera": {
            "name": "camera 1",
            "lockCamera": False,
            "position": {"x": pos[0], "y": pos[1], "z": pos[2]},
            "orientation": {"roll": 0.0, "pitch": c_pitch, "yaw": c_yaw},
            "projectionMode": cam.get("projection", "PINHOLE"),
            "fov": float(cam.get("fov", 70.0)),
            "dof": "Infinity",
            "focalOffset": 2.0,
            "shift": {"x": 0.0, "y": 0.0},
            "apertureShape": "CIRCLE",
        },
        "sun": {
            "altitude": float(cam.get("sun_altitude", 1.1)),
            "azimuth": float(cam.get("sun_azimuth", 2.3)),
            "intensity": float(cam.get("sun_intensity", 1.25)),
            "color": {"red": 1.0, "green": 1.0, "blue": 1.0},
            "drawTexture": True,
            "luminosity": 100.0,
        },
        "sky": {
            "skyYaw": 0.0,
            "skyMirrored": True,
            "skyLight": float(cam.get("sky_light", 1.0)),
            "mode": "SIMULATED",
            "horizonOffset": 0.1,
            "cloudsEnabled": False,
            "fogDensity": 0.0,
        },
        "fog": {"uniformDensity": 0.0, "skyFogDensity": 1.0},
        "chunkList": wanted,
        "entities": [],
        "actors": [],
        "materials": {},
        "octreeImplementation": "PACKED",
        "emitterSamplingStrategy": "NONE",
        "renderer": "PathTracingRenderer",
        "previewRenderer": "PreviewRenderer",
        "additionalData": {},
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("camera")
    ap.add_argument("out")
    ap.add_argument("--world-path", required=True, help="path Chunky will see")
    ap.add_argument("--scan-dir", required=True, help="path we read region files from")
    ap.add_argument("--name", default="snap")
    a = ap.parse_args()

    cam = json.load(open(a.camera))
    scene = build(cam, a.world_path, a.name, a.scan_dir)
    out_path = a.out
    if not scene["chunkList"]:
        sys.exit(f"error: no generated chunks near the camera in {a.scan_dir}")
    with open(out_path, "w") as fh:
        json.dump(scene, fh, indent=2)
    print(f"{len(scene['chunkList'])} chunks")
