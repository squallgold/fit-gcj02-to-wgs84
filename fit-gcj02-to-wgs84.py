#!/usr/bin/env python3
"""把 Garmin Connect 中国区编辑过的 FIT 文件中的坐标从 GCJ-02 还原为 WGS84。

Garmin 中国区的 Connect 在编辑并保存路线时（哪怕只是改个名字），会静默地
把坐标从 WGS84 转成 GCJ-02（"火星坐标系"）。手表的 OSM 地图用的是 WGS84，
所以编辑后同步过去的路线在地图上整体偏移约 270 米。

本工具反向：输入一个被 Connect 编辑过的 FIT，输出一个新 FIT，所有坐标字段
还原为 WGS84。其他所有字节（时间戳、海拔、路点名字、距离、类型，包括
Garmin 写的私有字段）逐字节保留。

实现思路：直接按 Garmin FIT SDK 规范解析二进制结构。遍历记录、跟踪定义，
按 global_message_num + field_definition_num 定位坐标字段，原地修改
semicircle 整数，最后重算文件 CRC。不依赖任何 FIT 库——这样能避免高层库
在不认识 Garmin 私有字段时静默丢弃它们。

限制：无法自动判断输入坐标系。只对确认是从 Connect 中国区编辑后导出的
FIT 文件使用。详见 README.md。
"""
import argparse
import math
import os
import struct
import sys


# ----- 标准 WGS84 ↔ GCJ-02 算法（公开参考实现） -----

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
    """wgs84_to_gcj02 的迭代反函数。5 次迭代后误差 < 1e-9 度。"""
    if _out_of_china(gcj_lat, gcj_lon):
        return gcj_lat, gcj_lon
    lat, lon = gcj_lat, gcj_lon
    for _ in range(max_iter):
        f_lat, f_lon = wgs84_to_gcj02(lat, lon)
        lat += gcj_lat - f_lat
        lon += gcj_lon - f_lon
    return lat, lon


# ----- FIT CRC（Garmin 16-bit 多项式，参见 FIT SDK） -----

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


# ----- FIT 字节级转换器 -----

# 映射：(global_msg_num) -> {field_def_num: 'lat' 或 'lon'}
# 经度纬度成对的逻辑：按 field_def_num 升序排列，每相邻两个 (lat, lon) 算一对。
COORD_FIELDS = {
    20: {0: "lat", 1: "lon"},                          # record（轨迹点）
    32: {2: "lat", 3: "lon"},                          # course_point（路点）
    19: {3: "lat", 4: "lon", 5: "lat", 6: "lon"},      # lap（圈：start, end）
}

SEMICIRCLE_TO_DEG = 180.0 / (2 ** 31)
DEG_TO_SEMICIRCLE = (2 ** 31) / 180.0
INVALID_SINT32 = 0x7FFFFFFF  # FIT 协议中 sint32 的"字段缺失"哨兵值


def _convert_pair_in_place(buf, start, lat_off, lon_off, endian, verbose, label):
    """在数据记录的指定字节偏移处，把一对 (lat, lon) 原地转换。"""
    lat_semi = struct.unpack_from(endian + "i", buf, start + lat_off)[0]
    lon_semi = struct.unpack_from(endian + "i", buf, start + lon_off)[0]
    if lat_semi == INVALID_SINT32 or lon_semi == INVALID_SINT32:
        return False  # 字段缺失
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
    """遍历一条数据记录的字段，对其中所有坐标对执行转换。"""
    endian = "<" if arch == 0 else ">"
    field_offsets = {}  # field_def_num -> (字节偏移, 大小)
    offset = 0
    for fdn, size, _base_type in fields:
        if fdn in coord_map:
            field_offsets[fdn] = (offset, size)
        offset += size

    # 按 field_def_num 升序两两配对（lat 在前、lon 在后）
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
    """字节级转换。返回 (新的字节串, 计数字典)。"""
    if data[8:12] != b".FIT":
        raise ValueError("不是 FIT 文件（缺少 .FIT 标识）")

    header_size = data[0]
    if header_size not in (12, 14):
        raise ValueError(f"FIT 文件头大小异常：{header_size}")

    data_size = struct.unpack_from("<I", data, 4)[0]
    body_start = header_size
    body_end = body_start + data_size  # [body_end : body_end+2] 是文件 CRC

    if len(data) < body_end + 2:
        raise ValueError("FIT 文件被截断")

    out = bytearray(data)
    definitions = {}  # local_msg_type -> (global_num, arch, fields_list)
    counts = {"record": 0, "course_point": 0, "lap": 0, "definitions": 0}

    pos = body_start
    while pos < body_end:
        header = out[pos]
        pos += 1
        if header & 0x80:
            # 压缩时间戳头：复用先前定义过的 local_msg_type
            local_mt = (header >> 5) & 0x3
            if local_mt not in definitions:
                raise ValueError(f"压缩头引用了未定义的 local_msg_type {local_mt}")
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
                # 定义记录
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
                        # 开发者字段定义也是 3 字节，我们只关心其大小，
                        # 用来正确跳过数据记录里这部分字节。
                        dev_size = out[pos + 1]
                        # field_def_num 取 -1 确保跟坐标 map 不会撞上
                        fields.append((-1, dev_size, 0))
                        pos += 3
                definitions[local_mt] = (global_num, arch, fields)
                counts["definitions"] += 1
            else:
                # 数据记录
                if local_mt not in definitions:
                    raise ValueError(f"数据记录引用了未定义的 local_msg_type {local_mt}")
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
        raise ValueError(f"文件体解析在 {pos} 结束，但应当在 {body_end}")

    # 在 [header + body] 上重算 CRC，写回末尾两字节
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


# ----- 命令行接口 -----

def parse_args():
    p = argparse.ArgumentParser(
        prog="fit-gcj02-to-wgs84",
        description="把 Garmin Connect 中国区编辑过的 FIT 文件中的坐标从 GCJ-02 还原为 WGS84。",
    )
    p.add_argument("input_file", help="被 Connect 中国区编辑过的 .fit 文件路径")
    p.add_argument("-o", "--output", help="输出路径（默认：INPUT.wgs84.fit，同目录）")
    p.add_argument(
        "-f", "--force", action="store_true",
        help="允许处理已含 .wgs84.fit 后缀的文件（绕过幂等保护）",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true",
        help="打印每一对坐标的转换详情",
    )
    return p.parse_args()


def main():
    args = parse_args()

    if not os.path.isfile(args.input_file):
        print(f"✗ 找不到输入文件：{args.input_file}", file=sys.stderr)
        sys.exit(1)

    if args.input_file.lower().endswith(".wgs84.fit") and not args.force:
        print(
            "✗ 输入文件名以 .wgs84.fit 结尾——拒绝转换"
            "（避免破坏已经是 WGS84 的数据）。",
            file=sys.stderr,
        )
        print("  如果确实要再转一次，请加 --force 参数。", file=sys.stderr)
        sys.exit(1)

    if args.output:
        output_path = args.output
    else:
        base, ext = os.path.splitext(args.input_file)
        output_path = base + ".wgs84" + ext

    counts = convert_fit(args.input_file, output_path, verbose=args.verbose)

    total = counts["record"] + counts["course_point"] + counts["lap"]
    print(
        f"✓ 读取 FIT：{counts['record']} 个轨迹点，"
        f"{counts['course_point']} 个路点，{counts['lap']} 处 Lap 坐标"
    )
    print(f"✓ 已转换 {total} 对坐标（GCJ-02 → WGS84）")
    print(f"✓ 已写入：{output_path}")


if __name__ == "__main__":
    main()
