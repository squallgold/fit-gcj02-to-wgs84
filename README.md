# fit-gcj02-to-wgs84

把 Garmin Connect 中国区编辑过的 FIT 文件中的坐标从 GCJ-02 还原为 WGS84。

## 问题背景

Garmin 中国区的 Connect（Explore 应该也一样）在你**编辑并保存**任何路线时，会静默地把坐标从 **WGS84** 转成 **GCJ-02**（俗称"火星坐标系"）——哪怕只是改一个路点名字也会触发。如果只是把 GPX 直接同步给手表、不做编辑，坐标会保持 WGS84。

如果你的手表用 WGS84 地图（比如 Garmin Enduro / Fenix / Forerunner 上的 OSM Maps 应用），编辑过的路线在地图上会**整体偏移约 270 米**。本来想用 Connect 的图形界面编辑路线（删点、改名、改类型等），结果被坐标系问题堵死。

## 这个工具做什么

把一个被 Connect 中国区编辑过的 FIT 文件（含 GCJ-02 坐标）转换成新的 FIT 文件，所有坐标字段还原为 WGS84。**其他所有内容逐字节保留**——时间戳、海拔、路点名字（包括中文）、距离、Lap 统计，连 Garmin 的私有字段都不动。

## 用法

```bash
# 转换并输出到同目录（生成 夏羌拉.wgs84.fit）
python3 fit-gcj02-to-wgs84.py 夏羌拉.fit

# 自定义输出路径
python3 fit-gcj02-to-wgs84.py 夏羌拉.fit -o /tmp/output.fit

# 打印每一对坐标的转换前后值
python3 fit-gcj02-to-wgs84.py 夏羌拉.fit -v
```

转换好的 `*.wgs84.fit` 拷到手表 `/GARMIN/Courses/` 目录里，开机后从课程列表加载即可。

## macOS 用户：跟手表传输文件

macOS 不会把 Garmin 手表识别为 Finder 可见的存储卷（Garmin 走 MTP 协议，不是 USB Mass Storage）。需要装一个 GUI 工具来读写手表里的文件：

- **Android File Transfer**（Google 官方）——https://www.android.com/filetransfer/
- **OpenMTP**（开源，新版 macOS 上往往更稳）——https://openmtp.ganeshrvel.com/

打开任意一个，连接手表后导航到 `/GARMIN/Courses/`，把转换后的 `.wgs84.fit` 拖进去。

## 环境要求

Python 3.8+。运行时**只用标准库**——不依赖任何 FIT 解析库；工具直接按 FIT 二进制规范解析文件。仅在跑测试时需要 `fit_tool` 包。

## 已知限制

### ⚠️ 无法自动判断输入坐标系

工具会**对你给的任何 FIT 文件都执行转换**——包括本来就是 WGS84 的文件——因为 FIT 格式没有记录坐标系信息，Connect 中国区也不会留下任何"已转换"的标记来区分。

**只对从 Connect 中国区编辑后导出的 FIT 用本工具**。如果是 GPX 直接同步给手表（不编辑）后导出的 FIT，那已经是 WGS84，**不要**再喂给本工具——会反向偏移 270m。

为了防止误用，工具会拒绝处理文件名以 `.wgs84.fit` 结尾的输入（除非加 `--force` 参数）。

### 适用地理范围

GCJ-02 仅在中国大陆有定义（大致：经度 72.0–137.8°，纬度 0.8–55.8°）。境外坐标会原样保留，跟 GCJ-02 规范一致。

## 工作原理（技术细节）

FIT 二进制格式把坐标存成 32 位有符号整数（"semicircles"，1 semicircle = 180/2³¹ 度），位于"数据记录"中，每条数据记录的字段布局由前面的"定义记录"指定。本工具：

1. 按 `local_message_type` 跟踪每条定义记录
2. 在数据记录里查找 `global_message_num` 为 `record`（20）、`course_point`（32）或 `lap`（19）的
3. 在这些记录里按 `field_definition_num` 定位 `position_lat` / `position_long` 字段
4. 把 semicircle 整数转回度，应用迭代式 GCJ-02→WGS84 反向变换，再转回 semicircle 写回原字节位置
5. 在文件末尾重新计算 CRC

GCJ-02 正向变换没有解析逆，工具用迭代不动点法求逆，5 次迭代后误差 < 1e-9 度（远小于 FIT semicircle 精度 ~1e-7 度）。

之所以走字节级修改而不是用高层 FIT 库，是因为**这种做法能完美保留所有未知字段**——包括 Garmin 写入的私有字段。早期用高层库（`fit_tool`）做的原型在重建文件时会丢字段，还会把 UTF-8 中文路点名搞坏。字节级方案不会有这些问题。

## 测试

```bash
~/gpxfit-env/bin/python -m pytest tests/
```

测试套件用真实的 Connect 编辑过的 FIT 和原始 GPX 作为 fixture（路径写在 `tests/test_roundtrip.py` 顶部）。

## 路线图

这是分阶段交付计划的 **M1**。后续阶段（独立发布）：

- **M2** — macOS 原生 GUI 应用：加载 FIT，在 OpenTopoMap 等高线地形图上叠加显示轨迹和路点，便于肉眼判断坐标系（编辑过的明显偏移、未编辑的对齐）
- **M3** — GUI 内编辑路点：改名、批量删除、改类型。替代 Connect 中国区编辑步骤
- **M4** — 一键发送到手表

## 许可证

MIT — 见 [LICENSE](LICENSE)。
