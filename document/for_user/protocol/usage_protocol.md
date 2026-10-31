# 命令与参数参考

## 0. 安装与首次准备

安装包由 GitHub Releases 提供，采用 Debian 软件包格式：

```bash
sudo apt install ./winpe-terminal_<版本>_amd64.deb
```

软件包只安装程序本身，不包含 Windows PE。首次使用前必须准备运行资源：

```bash
sudo winpe_terminal prepare
```

`prepare` 会检查系统工具、显示微软官方许可来源并要求你明确接受，然后以普通用户身份下载官方组件并构建镜像，
最后由 root 将校验通过的镜像发布到 `/var/lib/winpe-terminal/assets/winpe.iso`。
下载中断后再次运行同一命令即可续传；镜像已就绪时会直接报告状态。

## 1.启动与存储对象参数

### 1.1 不指定存储对象

```text
sudo winpe_terminal start
```

启动最简内存 WinPE，并将当前终端接入该实例。不指定对象时，不自动挑选宿主磁盘或分区进行透传。

### 1.2 显式指定存储对象

```text
sudo winpe_terminal start -<存储对象的绝对设备路径>
```

参数规则借鉴 ntfsfix 通过设备路径明确选择目标的方式，但本项目要求在设备路径前额外加一个 `-`。例如:

```text
sudo winpe_terminal start -/dev/nvme0n1p3
```

这里 `-/dev/nvme0n1p3` 是一个完整参数。仅移除开头的一个 `-` 后，目标路径为 `/dev/nvme0n1p3`。

## 2. 设置、重载与帮助

### 2.1 设置

```text
winpe_terminal setting
```

执行时立即重新读取配置文件（`/etc/winpe-terminal/config.json`），然后进入设置界面。
该命令不要求 root：普通用户可以查看和编辑草稿；保存时若需要提升权限，会通过图形授权完成，不会把配置文件改成所有用户可写。
如果当前终端不支持所需交互，则报告当前配置文件所在地址。

重载后的配置只用于后续新启动的虚拟机。

### 2.2 重载配置

```text
winpe_terminal reload
```

立即重新读取配置文件，供后续新启动的虚拟机使用，不改变已运行实例。

### 2.3 帮助

```text
winpe_terminal -help
```

显示用法、参数和必要的权限说明。

## 3. 状态查询

```text
sudo winpe_terminal -status
```

报告当前所有仍在运行的 WinPE 实例的状态与资源占用。每个实例输出：

| 项目 | 含义 |
| --- | --- |
| 序号、UUID、状态 | 实例编号、唯一标识、当前状态 |
| 启动时间、时长 | 实例启动时刻（UTC）与已运行时长 |
| supervisor PID、QEMU PID | 监督进程与虚拟机进程；已退出时标注 |
| QEMU RSS、CPU 累计 | 虚拟机进程常驻内存与累计 CPU 时间 |
| 内存／vCPU | 该实例启动时采用的 guest 资源配置 |
| 设备路径 | 接入的物理分区；未接入时显示“未接入” |

RSS 只是单个进程的常驻内存，不包含共享缓存等开销，不能当作宿主总占用。

## 4. 关闭实例

以下五个子命令等价，默认向所有仍在运行的 winpe_terminal 实例请求正常关机：

```text
sudo winpe_terminal terminate
sudo winpe_terminal stop
sudo winpe_terminal shutdown
sudo winpe_terminal end
sudo winpe_terminal exit
```

在子命令后加入 `-f`，表示强制关机。例如：

```text
sudo winpe_terminal stop -f
```

可用 `-x=UUID` 只关闭指定实例，例如：

```text
sudo winpe_terminal stop -x=62fa0f07-f30a-467a-b6df-150fae11558e
sudo winpe_terminal stop -f -x=62fa0f07-f30a-467a-b6df-150fae11558e
```

UUID 取 `-status` 输出的值，必须是带连字符的完整标准格式。`-f` 与 `-x=UUID` 可同时使用，顺序不限。

强制关机存在文件系统损坏和未写入数据丢失风险，需要谨慎使用。

## 5. 卸载

```text
sudo winpe_terminal -unistall
```

`-unistall` 是当前确认的命令拼写，不擅自改写为其他拼写。

卸载会按照下列顺序执行：

1. 请求用户再次确认卸载，确认前不进行实际卸载。
2. 检查全部仍在运行的实例，并请求它们正常关机。
3. 如果正常关机超时，再询问用户：
   - 确认强制关机并继续卸载；或
   - 取消尚未正式开始的卸载过程。
4. 仅在再次确认后，强制关闭仍未退出的目标实例。
5. 清理本程序实例遗留的存储设备占用和挂载。
6. 移除全部 WinPE 实例相关内容。
7. 最后彻底卸载 winpe_terminal。
