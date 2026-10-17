# GameLife 游戏人生

把你 Steam 库里那些数字，变成一份看得见的「游戏人生」。

它读的是**你本机已经有的数据**：游戏列表、游玩时长、成就和状态，封面与中文名也优先从本地 Steam 缓存里翻。所以国内网络下不用挂加速器，整份都能跑完。

## 功能

最顺手的入口是**本地面板**。双击仓库根目录的 `启动面板.bat`，浏览器会自动开一个网页，功能全在里面点，不用记命令。

- 统计看板：游戏总数、总时长、吃灰率、已玩占比、状态分布
- 智能推荐：告诉我还剩多少时间、什么心情，它挑一款出来
- 游戏族谱：按系列铺开全部作品，分清「已拥有 / 家庭库玩过 / 未拥有」
- 性价比榜：每小时花掉多少钱，统一换算成人民币
- 成就冲刺雷达：离全成就最近的那些排最前面
- 六张可视化：年度报告、游戏墓园、游戏星图、时间轴、时光机、体检报告
- 多账号合流：把自己几个账号合成一个来统计
- 清库挑战：给吃灰的游戏定个月度目标，顺手赚点 XP

## 环境要求

- Windows 10 / 11
- Python 3.11 或更高（开发环境是 3.14）
- 本机装过 Steam 客户端，并且登录过至少一个账号

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r game-lifeequirements.txt
```

依赖只有四个：`vdf`、`requests`、`psutil`、`jinja2`。

不想装 Python 的话，可以直接去 [Releases](https://github.com/lucime470/GameLife/releases) 下载 `game-life.exe`，双击就能用。

## 开始使用

需要敲命令的地方不多，推荐直接双击 `启动面板.bat`：

1. 双击 `启动面板.bat`，等几秒，浏览器会自动打开 `http://127.0.0.1:8765/`。端口被占用时会自动换一个，以窗口里打印的地址为准。
2. 在页面顶部点「添加账号」，勾上你想看的账号。剩下的数据程序会在后台自己补齐，等着就行。
3. 之后切账号、看统计、生成报告，都在这个页面里完成。

## 常用命令

```powershell
# 看所有命令
.\.venv\Scripts\python.exe game-life\main.py --help

# 启动面板
.\.venv\Scripts\python.exe game-life\main.py panel

# 扫一遍并存快照（--dry-run 只预览，什么都不写）
.\.venv\Scripts\python.exe game-life\main.py run --account-id <账号ID> --dry-run

# 终端看统计 / 让它推荐一款
.\.venv\Scripts\python.exe game-life\main.py stats     --account-id <账号ID>
.\.venv\Scripts\python.exe game-life\main.py recommend --account-id <账号ID> --time 1h --mood 轻松

# 多账号合流：把已添加的账号当一个账号统计
.\.venv\Scripts\python.exe game-life\main.py family --account-id all
```

账号 ID 就是 `Steam安装目录\userdata\` 下面那串纯数字的文件夹名。

## 目录结构

```
game-life/
├─ main.py      # 命令入口，也是面板入口
├─ core/        # 数据底座：找 Steam、读库、读时长、归属判定、元数据
├─ engine/      # 分析与状态：分类、统计、推荐、族谱、成就雷达……
├─ render/      # 六张可视化报告的模板与渲染
├─ webapp/      # 本地面板：页面 + 轻量 HTTP API
├─ data/        # 内置的只读资料表（类型、系列、中文名对照）
└─ tests/       # 测试
```

## 几点说明

- 程序**只读** Steam 的文件。唯一会写的是可选的「状态标签写回」，默认关闭，要自己开。
- 第一次用会陆续生成本机缓存（封面、中文名、快照等），都放在 `game-life/data/` 和 `game-life/output/` 里，不会动 Steam 本身。
- 仓库里不带任何个人数据，克隆下来直接用。

## 许可

MIT，随便用、随便改。
