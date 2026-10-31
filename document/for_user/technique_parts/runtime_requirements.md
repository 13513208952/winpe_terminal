# 运行环境与安全使用

当前版本为发布前预览。WinPE 启动、终端透传、真实分区上的 chkdsk 及正常关机已在实测中通过；长输出、多实例、卸载流程等仍在收尾，尚不宜用于唯一副本上的修复操作。

## 运行环境

- Linux x86-64、Python 3.10 或更高版本。
- QEMU x86-64，支持 KVM、QMP、串口及 NBD；宿主必须提供可用的 `/dev/kvm`，不自动退回软件模拟。
- 合法获取、允许用于部署或恢复的 x64 WinPE ISO，且已在 BCD 中启用 EMS/SAC 串口控制台（本项目的构建工具会完成这一步）。普通 Windows 安装 ISO 不等同于所需镜像。
- 终端通过 Windows 自带的 EMS/SAC 走串口与虚拟机交互，不需要另外的程序或 DLL。宿主的终端能力决定显示效果；SAC 不支持窗口尺寸变化，长行会自动折行。
- 安装目录所在文件系统须支持 `user.winpe_terminal.created_at` 扩展属性，用于实例记录的创建时间元数据。

WinPE 许可限制其用途，不能把它用作通用生产操作系统。请自行核对所用版本的许可。参见 [微软 WinPE 介绍](https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/winpe-intro)。

## 启动与终端

安装目录内的入口文件为 `winpe_terminal`。未将命令安装到系统命令搜索路径时，可进入安装目录后执行：

```text
./winpe_terminal -help
./winpe_terminal setting
sudo ./winpe_terminal start
```

每个启动终端对应一个实例。WinPE 就绪后，键盘输入和终端输出由该实例处理；WinPE 退出后返回原 Linux shell。关闭窗口会请求关闭对应实例；窗口消失后若正常关机未完成，程序会回收该实例，可能造成未写入数据丢失。

手动执行普通关机命令时，超时不会擅自强制关闭。请先查看状态，再决定是否显式使用 `-f`。

## 存储对象

- 接入前备份数据，并确保目标分区未被宿主或其他程序使用。
- 程序不自动卸载宿主文件系统，也不自动运行 chkdsk。
- 分区以隔离的虚拟磁盘视图交给 WinPE，不把宿主父磁盘交给虚拟机；分区表之外的空间不映射到物理盘。
- 允许接入与宿主共用物理磁盘的分区（双系统常见情形），但宿主根文件系统与本程序所在分区始终拒绝，且目标必须未被挂载。
- 分区在 Windows 中的盘符不一定与原系统相同。运行任何修复命令前，应在 WinPE 中核对卷标、容量和目标盘符。
- 不要在透传期间重新挂载目标分区，也不要在修复过程中拔出设备。

## 系统依赖

正式发行的软件包通过 APT 依赖系统提供的 QEMU 与构建工具，不再自带一份私有 QEMU：

```text
python3  qemu-system-x86  curl  7zip（或 p7zip-full）  xorriso
wimtools  msitools  libhivex-bin
```

`sudo apt install ./winpe-terminal_<版本>_amd64.deb` 会由 APT 自动解析并安装这些依赖。

## 目录布局

| 路径 | 内容 | dpkg 管理 |
| --- | --- | --- |
| `/usr/bin/winpe_terminal` | 命令入口 | 是 |
| `/usr/lib/winpe-terminal/` | 程序模块、构建脚本、`startnet.cmd` | 是 |
| `/etc/winpe-terminal/config.json` | 配置（conffile） | 是 |
| `/var/lib/winpe-terminal/` | 镜像、构建回执、实例记录、安装标识 | 否 |
| `/var/cache/winpe-terminal/` | 下载缓存、构建暂存 | 否 |
| `/var/log/winpe-terminal/` | 每个实例的日志 | 否 |
| `/usr/share/doc/winpe-terminal/` | 用户文档与许可声明 | 是 |

`apt remove` 删除 dpkg 管理的文件，保留 `/var/lib` 与 `/var/cache` 中的运行资源；
`apt purge` 还会清除 WPT 自己创建的状态、缓存与日志。

## 资源与文件

默认分配 1024 MiB（1 GiB）guest 内存。QEMU、主程序和宿主文件缓存另有开销，因此宿主总内存压力可能高于 1 GiB，不能把 guest 分配值当作总占用测量结果。

- `/etc/winpe-terminal/config.json`：配置文件。
- `/var/lib/winpe-terminal/assets/winpe.iso`：由 `prepare` 构建并发布的镜像，`winpe-build.json` 记录版本与哈希。
- `/var/lib/winpe-terminal/instances/序号_UUID.txt`：各实例的 JSON 文本记录，含启动用户、时间、设备、运行状态与 `mount_directory`（该分区在宿主的挂载路径：当前已挂载则为实际挂载点，未挂载则为按卷标推导的 `/run/media/<用户>/<卷标>`，无法推导时为 null）。
- `/var/log/winpe-terminal/UUID.log`：对应实例的 QEMU／固件输出与宿主侧状态记录（启动快照、控制台就绪、通道切换、退出原因）；启动失败时可根据终端提示查看。

卸载见 `protocol/usage_protocol.md` 第 5 节：`-unistall`、`apt remove` 与 `apt purge` 的行为各不相同，但都不会删除物理分区内容或其他软件安装的组件。
