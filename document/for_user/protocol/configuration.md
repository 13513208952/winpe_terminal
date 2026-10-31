# 配置文件参考

配置文件为 `/etc/winpe-terminal/config.json`，采用 UTF-8 JSON 格式，由 dpkg 作为 conffile 管理：
升级时保留你修改过的配置，`apt remove` 保留配置，`apt purge` 才删除。所有字段均须提供，不接受未知字段。

## 格式

```json
{
  "schema": 1,
  "qemu": "/usr/bin/qemu-system-x86_64",
  "iso": "assets/winpe.iso",
  "memory_mib": 1024,
  "cpus": 2,
  "boot_timeout_seconds": 180,
  "shutdown_timeout_seconds": 30,
  "allowed_devices": []
}
```

## 字段

| 字段 | 类型与取值 | 含义 |
| --- | --- | --- |
| `schema` | 整数，固定为 `1` | 配置格式版本 |
| `qemu` | 非空绝对路径 | QEMU 可执行文件或本地启动器 |
| `iso` | 非空路径 | 已启用 EMS/SAC 串口控制台的 WinPE ISO；相对路径以 `/var/lib/winpe-terminal` 为基准，默认 `assets/winpe.iso` |
| `memory_mib` | 整数，256–4096 | 分配给 guest 的内存，单位 MiB；不等于宿主总内存开销，也不保证镜像在该值下可启动 |
| `cpus` | 整数，1–8 | 虚拟 CPU 数量 |
| `boot_timeout_seconds` | 整数，5–1800 | 等待 WinPE 串口控制台（EMS/SAC）就绪的时间，单位秒 |
| `shutdown_timeout_seconds` | 整数，1–600 | 正常关机等待时间，单位秒；普通关机超时不自动转为强制关机 |
| `allowed_devices` | 列表，默认 `[]` | 可显式接入的物理分区白名单；空列表禁用物理设备接入 |

数值字段不接受字符串、小数或布尔值。路径不接受控制字符。配置中不支持 shell 命令、插件或额外 QEMU 参数。

## 设备白名单项

每项必须且仅包含以下字段：

| 字段 | 类型与规则 |
| --- | --- |
| `path` | 规范的 `/dev/…` 绝对路径，不可重复；应与设备规范路径一致 |
| `serial` | 父设备的非空序列号，用于核对设备身份 |
| `size_bytes` | 所选分区的精确容量，正整数，单位字节；不是整盘的宣称容量 |

白名单只表示允许检查该目标，不会自动挂载或启动，也不会绕过独占检查。当前仅接入 512 字节逻辑扇区、MBR 范围可表达的分区，不接入整盘。已挂载、用于 swap、被其他块设备持有、只读或身份不符的对象将被拒绝；宿主根文件系统与本程序所在分区始终拒绝。允许与宿主共用物理磁盘的其他分区（例如同一块 SSD 上的 Windows 分区），但必须由用户自行卸载，程序不会代卸。

## 生效规则

- `setting` 进入时及 `reload` 执行时，立即重新读取并校验配置。
- 每次 `start` 也会读取并校验当前配置。
- 变更仅用于后续启动的实例，不修改已运行实例。
- 配置校验失败时，不应继续启动虚拟机。
