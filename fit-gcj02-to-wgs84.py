#!/usr/bin/env python3
"""Restore WGS84 coordinates in FIT files edited by Garmin Connect China.

Garmin's China-region Connect silently converts route coordinates from WGS84
to GCJ-02 ("Mars coordinate system") whenever a course is edited and saved.
The watch's OSM map uses WGS84, so edited courses appear offset by ~270m.

This tool reverses that conversion: input a Connect-edited FIT, output a new
FIT with all coordinate fields restored to WGS84. All non-coordinate bytes
(timestamps, altitude, names, distances, types, custom Garmin fields, etc.)
are preserved byte-for-byte.

Implementation: parses the FIT binary structure directly per the Garmin FIT
SDK spec. Walks records tracking definitions, locates position_lat/long
fields by global_message_num + field_definition_num, modifies semicircle
integers in place, recomputes the file CRC. Does not depend on any FIT
library — avoids loss of Garmin custom fields that high-level libraries
silently strip.

LIMITATION: cannot auto-detect input coordinate system. Only run on files
known to come from Connect China after editing. See README for details.
"""
import argparse
import math
import os
import struct
import sys


# ----- Standard WGS84 ↔ GCJ-02 algorithm (public reference impl) -----

def _out_of_china(lat, lon):
    if lon < 72.004 or lon > 137.8347:
        return True
    if lat < 0.8293 or lat > 55.8271:
        return True
    return False


def _transform_lat(x, y):
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lon(x, y):
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lat, lon):
    if _out_of_china(lat, lon):
        return lat, lon
    a = 6378245.0
    ee = 0.00669342162296594323
    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlon = _transform_lon(lon - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - ee * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sqrtmagic) * math.pi)
    dlon = (dlon * 180.0) / (a / sqrtmagic * math.cos(radlat) * math.pi)
    return lat + dlat, lon + dlon


def gcj02_to_wgs84(gcj_lat, gcj_lon, max_iter=5):
    """Iterative inverse of wgs84_to_gcj02. Converges to <1e-9 deg in ~5 iters."""
    if _out_of_china(gcj_lat, gcj_lon):
        return gcj_lat, gcj_lon
    lat, lon = gcj_lat, gcj_lon
    for _ in range(max_iter):
        f_lat, f_lon = wgs84_to_gcj02(lat, lon)
        lat += gcj_lat - f_lat
        lon += gcj_lon - f_lon
    return lat, lon


# ----- FIT CRC (Garmin's 16-bit polynomial, per FIT SDK) -----

_CRC_TABLE = (
    0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
    0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400,
)


def fit_crc(data):
    crc = 0
    for byte in data:
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _CRC_TABLE[byte & 0xF]
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _CRC_TABLE[(byte >> 4) & 0xF]
    return crc & 0xFFFF


# ----- FIT byte-level converter -----

# Map of (global_msg_num) -> dict of {field_def_num: 'lat' or 'lon'}
# Pairs are inferred by ordering: each consecutive (lat, lon) in sorted order forms one pair.
COORD_FIELDS = {
    20: {0: "lat", 1: "lon"},                          # record
    32: {2: "lat", 3: "lon"},                          # course_point
    19: {3: "lat", 4: "lon", 5: "lat", 6: "lon"},      # lap: start, end
}

SEMICIRCLE_TO_DEG = 180.0 / (2 ** 31)
DEG_TO_SEMICIRCLE = (2 ** 31) / 180.0
INVALID_SINT32 = 0x7FFFFFFF  # FIT "field not present" sentinel for sint32


def _convert_pair_in_place(buf, start, lat_off, lon_off, endian, verbose, label):
    """Convert one (lat, lon) pair within a data record, in place."""
    lat_semi = struct.unpack_from(endian + "i", buf, start + lat_off)[0]
    lon_semi = struct.unpack_from(endian + "i", buf, start + lon_off)[0]
    if lat_semi == INVALID_SINT32 or lon_semi == INVALID_SINT32:
        return False  # field absent
    lat_deg = lat_semi * SEMICIRCLE_TO_DEG
    lon_deg = lon_semi * SEMICIRCLE_TO_DEG
    new_lat, new_lon = gcj02_to_wgs84(lat_deg, lon_deg)
    if verbose:
        print(f"  {label}: ({lat_deg:.6f}, {lon_deg:.6f}) -> ({new_lat:.6f}, {new_lon:.6f})")
    new_lat_semi = round(new_lat * DEG_TO_SEMICIRCLE)
    new_lon_semi = round(new_lon * DEG_TO_SEMICIRCLE)
    struct.pack_into(endian + "i", buf, start + lat_off, new_lat_semi)
    struct.pack_into(endian + "i", buf, start + lon_off, new_lon_semi)
    return True


def _convert_data_record(buf, start, fields, arch, coord_map, verbose, label):
    """Walk a data record's fields, converting any position pairs found."""
    endian = "<" if arch == 0 else ">"
    field_offsets = {}  # field_def_num -> (byte_offset, size)
    offset = 0
    for fdn, size, _base_type in fields:
        if fdn in coord_map:
            field_offsets[fdn] = (offset, size)
        offset += size

    # Pair up lat/lon fields by sorted field_def_num order
    converted = 0
    sorted_keys = sorted(coord_map.keys())
    for i in range(0, len(sorted_keys), 2):
        if i + 1 >= len(sorted_keys):
            break
        lat_key, lon_key = sorted_keys[i], sorted_keys[i + 1]
        if coord_map[lat_key] != "lat" or coord_map[lon_key] != "lon":
            continue
        if lat_key not in field_offsets or lon_key not in field_offsets:
            continue
        lat_off, lat_size = field_offsets[lat_key]
        lon_off, lon_size = field_offsets[lon_key]
        if lat_size != 4 or lon_size != 4:
            continue
        if _convert_pair_in_place(buf, start, lat_off, lon_off, endian, verbose, label):
            converted += 1
    return converted


def convert_fit_bytes(data, verbose=False):
    """Byte-level conversion. Returns (new_bytes, counts_dict)."""
    if data[8:12] != b".FIT":
        raise ValueError("Not a FIT file (missing .FIT magic bytes)")

    header_size = data[0]
    if header_size not in (12, 14):
        raise ValueError(f"Unexpected FIT header size: {header_size}")

    data_size = struct.unpack_from("<I", data, 4)[0]
    body_start = header_size
    body_end = body_start + data_size  # bytes [body_end:body_end+2] are file CRC

    if len(data) < body_end + 2:
        raise ValueError("FIT file truncated")

    out = bytearray(data)
    definitions = {}  # local_msg_type -> (global_num, arch, fields_list)
    counts = {"record": 0, "course_point": 0, "lap": 0, "definitions": 0}

    pos = body_start
    while pos < body_end:
        header = out[pos]
        pos += 1
        if header & 0x80:
            # Compressed Timestamp Header — refers to a previously defined local_msg_type
            local_mt = (header >> 5) & 0x3
            if local_mt not in definitions:
                raise ValueError(f"Compressed header references undefined local_msg_type {local_mt}")
            global_num, arch, fields = definitions[local_mt]
            record_size = sum(f[1] for f in fields)
            if global_num in COORD_FIELDS:
                label = {20: "record", 32: "course_point", 19: "lap"}[global_num]
                cnt = _convert_data_record(out, pos, fields, arch, COORD_FIELDS[global_num], verbose, label)
                if global_num == 19:
                    counts["lap"] += cnt
                elif global_num == 20:
                    counts["record"] += cnt
                else:
                    counts["course_point"] += cnt
            pos += record_size
        else:
            local_mt = header & 0x0F
            is_def = bool(header & 0x40)
            has_dev = bool(header & 0x20)
            if is_def:
                # Definition message
                pos += 1  # reserved
                arch = out[pos]; pos += 1
                endian = "<" if arch == 0 else ">"
                global_num = struct.unpack_from(endian + "H", out, pos)[0]
                pos += 2
                num_fields = out[pos]; pos += 1
                fields = []
                for _ in range(num_fields):
                    fields.append((out[pos], out[pos + 1], out[pos + 2]))
                    pos += 3
                if has_dev:
                    num_dev = out[pos]; pos += 1
                    for _ in range(num_dev):
                        # Each developer field def is also 3 bytes; we only need its size
                        # to walk past it in data records.
                        dev_size = out[pos + 1]
                        # Append as a synthetic field; field_def_num=-1 ensures no match.
                        fields.append((-1, dev_size, 0))
                        pos += 3
                definitions[local_mt] = (global_num, arch, fields)
                counts["definitions"] += 1
            else:
                # Data message
                if local_mt not in definitions:
                    raise ValueError(f"Data message references undefined local_msg_type {local_mt}")
                global_num, arch, fields = definitions[local_mt]
                record_size = sum(f[1] for f in fields)
                if global_num in COORD_FIELDS:
                    label = {20: "record", 32: "course_point", 19: "lap"}[global_num]
                    cnt = _convert_data_record(out, pos, fields, arch, COORD_FIELDS[global_num], verbose, label)
                    if global_num == 19:
                        counts["lap"] += cnt
                    elif global_num == 20:
                        counts["record"] += cnt
                    else:
                        counts["course_point"] += cnt
                pos += record_size

    if pos != body_end:
        raise ValueError(f"Body parse ended at {pos}, expected {body_end}")

    # Recompute file CRC over header + body, write into trailing 2 bytes
    new_crc = fit_crc(bytes(out[:body_end]))
    struct.pack_into("<H", out, body_end, new_crc)

    return bytes(out), counts


def convert_fit(input_path, output_path, verbose=False):
    with open(input_path, "rb") as f:
        data = f.read()
    new_data, counts = convert_fit_bytes(data, verbose=verbose)
    with open(output_path, "wb") as f:
        f.write(new_data)
    return counts


# ----- CLI -----

def parse_args():
    p = argparse.ArgumentParser(
        prog="fit-gcj02-to-wgs84",
        description="Restore WGS84 coordinates in FIT files edited by Garmin Connect China.",
    )
    p.add_argument("input_file", help="Path to .fit file edited by Connect (China region)")
    p.add_argument("-o", "--output", help="Output path (default: INPUT.wgs84.fit, same dir)")
    p.add_argument(
        "-f", "--force", action="store_true",
        help="Allow processing files already named *.wgs84.fit",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true",
        help="Print every coordinate transformation",
    )
    return p.parse_args()


def main():
    args = parse_args()

    if not os.path.isfile(args.input_file):
        print(f"✗ Input file not found: {args.input_file}", file=sys.stderr)
        sys.exit(1)

    if args.input_file.lower().endswith(".wgs84.fit") and not args.force:
        print(
            "✗ Input filename ends with .wgs84.fit — refusing to convert "
            "(would corrupt valid WGS84 data).",
            file=sys.stderr,
        )
        print("  If you really mean to convert this file again, pass --force.", file=sys.stderr)
        sys.exit(1)

    if args.output:
        output_path = args.output
    else:
        base, ext = os.path.splitext(args.input_file)
        output_path = base + ".wgs84" + ext

    counts = convert_fit(args.input_file, output_path, verbose=args.verbose)

    total = counts["record"] + counts["course_point"] + counts["lap"]
    print(
        f"✓ Read FIT: {counts['record']} trackpoints, "
        f"{counts['course_point']} course points, {counts['lap']} lap position(s)"
    )
    print(f"✓ Converted {total} coordinate pairs (GCJ-02 → WGS84)")
    print(f"✓ Written: {output_path}")


if __name__ == "__main__":
    main()
