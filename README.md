# SaveSync — 游戏存档云同步

两台电脑之间同步游戏存档。在 Mac（含 CrossOver 瓶子）和 Windows 上扫描游戏存档，
打包上传到坚果云 / S3 兼容存储，换机器时一键下载覆盖。

## 特点

- **零依赖**：只用 Python 标准库，不需要 pip 装任何东西
- **双平台**：macOS（含 CrossOver bottle 里的 Windows 游戏）与 Windows
- **全盘扫描**：自动发现新装的游戏，扫到即自动加入同步清单
- **冲突保护**：云端比本地新时拒绝上传，本地有未上传修改时拒绝下载
- **自动备份**：覆盖前自动备份本地存档，本地留 10 份、云端留 5 份历史

## 快速开始

### macOS

从 [Releases](https://github.com/RedDevil233666/savesync/releases) 下载
`SaveSync-macOS.zip`，解压后把 `存档同步.app` 拖进「应用程序」即可。

首次运行若提示「来自身份不明的开发者」：系统设置 → 隐私与安全性 → 仍要打开。

### Windows

1. 安装 [Python 3.8+](https://www.python.org/downloads/)（安装时勾选 Add python.exe to PATH）
2. 把 `build_windows.bat`、`savesync.py`、`savesync_gui.py` 放在同一文件夹，双击 bat
3. 构建完成后得到 `dist\SaveSync.exe`，可复制到任意位置使用

## 界面

四个按钮：

| 按钮 | 作用 |
| --- | --- |
| 扫描游戏 | 扫描本机所有游戏存档，新游戏自动加入同步清单 |
| 上传存档 | 本机存档打包上传到云端（玩完游戏点这个） |
| 下载覆盖 | 用云端存档覆盖本机（换机器开玩前点这个） |
| 查看存档 | 查看同步清单及本地/云端状态 |

左下角「配置云端」填坚果云账号，每台机器配一次。

## 配置云端（坚果云 WebDAV）

1. 登录坚果云网页端 → 账户信息 → 安全选项 → **添加应用密码**
2. 在「配置云端」里填：

```
服务器地址：https://dav.jianguoyun.com/dav/
账号：你的坚果云邮箱
密码：刚才生成的应用密码（不是登录密码）
```

也支持 S3 兼容对象存储，见下方命令行用法。

## 扫描覆盖哪些位置

扫描会把每个用户环境（本机 + 每个 CrossOver bottle）都当成一个 Windows 用户目录
逐一扫过：

- `Documents/My Games/<游戏>`
- `Documents/<厂商>/<游戏>`
- `Saved Games/<游戏>`
- `AppData/LocalLow/<厂商>/<游戏>`
- `AppData/Local/<游戏>`（含 Unreal Engine 的 `Saved/SaveGames`）
- `AppData/Roaming/<游戏>`
- Steam 库：解析 `libraryfolders.vdf` 得到所有库，扫 `common/<游戏>/` 下
  名字含 save / 存档 / uds 的子目录
- 游戏安装目录：`~/games`（Windows 下还会扫各盘符的 `Games` 目录）
  下每个游戏文件夹里的 save / saves / savedata / savefiles / uds 子目录

判定是否为存档：目录内存在 `.sav` `.sl2` `.lsv` `.ess` 等 12 种扩展名的文件，
或文件名以 save / autosave / quicksave / slot / manualsave 开头。

**自定义扫描目录**：游戏装在别处时，在 `~/.savesync/config.json` 里加一行：

```json
{
  "scan_dirs": ["D:/Games", "/Volumes/SSD/游戏"]
}
```

## 命令行用法

图形界面覆盖日常使用，命令行可用于脚本化或排查问题。

```bash
# 扫描（--add-known 会把发现的游戏自动加入配置）
python savesync.py scan --add-known

# 上传 / 下载
python savesync.py push              # 全部
python savesync.py push elden-ring   # 指定游戏
python savesync.py pull --force      # 覆盖本机（自动备份）

# 查看状态
python savesync.py status

# 配置云端
python savesync.py setup-webdav --url https://dav.jianguoyun.com/dav \
    --user you@example.com --password 应用密码

python savesync.py setup-s3 --endpoint <endpoint> --bucket <bucket> \
    --ak <AccessKey> --sk <SecretKey> --region auto

# 手动增删游戏
python savesync.py add my-game --name "我的游戏" --path "~/Documents/My Games/MyGame"
python savesync.py remove my-game
```

## 文件位置

```
~/.savesync/config.json    游戏清单 + 云端配置（含密码，注意保密）
~/.savesync/state.json     上次同步时间等状态
~/.savesync/backups/       覆盖前的本地自动备份（保留 10 份）
```

## 已知限制

- 存档文件夹名字不叫 save / saves / saved / savedata / savefiles / uds / 存档 的
  游戏扫不到，需用 `add` 命令手动添加，或把目录加进 `scan_dirs`
- macOS 原生（非 CrossOver）游戏不会加入同步清单
- 坚果云免费版有月度流量限制，存档体积小一般够用

## 许可

MIT
