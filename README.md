# GameLife 游戏人生

读取你**本机 Steam** 的游戏库，把游玩时长、成就、状态整理成统计看板与可视化报告。

> 数据优先本机、网络只作补充：封面、中文名、时长都优先读本机 Steam 已缓存的文件，
> 大陆网络**不用加速器**即可使用全部功能。

## 功能一览

- **本地面板（网页）**：一个页面里用上全部功能，点按钮即可，不用记命令
- **统计看板**：总游戏数 / 总时长 / 吃灰率 / 已玩占比 / 状态分布
- **智能推荐**：按「时间 + 心情 + 类型」推荐下一款玩什么
- **游戏族谱**：按系列（如刺客信条、黑暗之魂）列出全部作品，标出已拥有 / 未拥有
- **性价比榜**：每小时花费排行（统一人民币）
- **成就冲刺雷达**：找出「距全成就最近」的游戏（已 100% 的自动隐藏）
- **六个可视化**：年度报告 / 游戏墓园 / 游戏星图 / 时间轴 / 时光机 / 体检报告
- **多账号合流**：把你添加的多个账号当作一个账号一起统计
- **清库挑战 + XP**：给吃灰游戏设计月度挑战

## 环境要求

- Windows 10 / 11
- Python 3.11+（开发环境为 3.14）
- 本机已安装并登录过 **Steam 客户端**（用于读取游戏库与时长）

## 安装

```powershell
# 1) 建议先建虚拟环境
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r game-life\requirements.txt

# 2) 可选：安装打包依赖（只有想自己打 exe 时才需要）
.\.venv\Scripts\python.exe -m pip install pyinstaller
```

依赖只有 4 个：`vdf`、`requests`、`psutil`、`jinja2`。

## 快速开始（推荐：不用敲命令）

双击仓库根目录的 **`启动面板.bat`**，几秒后浏览器会自动打开本地面板。

- 默认地址 `http://127.0.0.1:8765/`；端口被占用会**自动顺延**，以窗口里打印的地址为准
- 首次打开请在面板顶部**「添加账号」**里选择你的 Steam 账号（程序会自动帮你补齐数据）
- 想直接指定账号：`启动面板.bat 你的账号ID`

## 常用命令

```powershell
# 查看全部命令
.\.venv\Scripts\python.exe game-life\main.py --help

# 启动本地面板
.\.venv\Scripts\python.exe game-life\main.py panel

# 扫描 + 状态判定 + 存快照（dry-run 只预览，不写回 Steam）
.\.venv\Scripts\python.exe game-life\main.py run --account-id <你的账号ID> --dry-run

# 统计看板 / 智能推荐 / 人格测试
.\.venv\Scripts\python.exe game-life\main.py stats    --account-id <你的账号ID>
.\.venv\Scripts\python.exe game-life\main.py recommend --account-id <你的账号ID> --time 1h --mood 轻松

# 多账号合流：把已添加的账号当作同一个账号统计
.\.venv\Scripts\python.exe game-life\main.py family --account-id all
```

账号 ID 就是 `Steam安装目录\userdata\` 下的那串数字目录名。

## 目录结构

```
game-life/
├── main.py         # CLI 入口（也是面板入口），全部命令在这里路由
├── core/           # 数据底座：定位 Steam、读库、读时长、归属判定、元数据
├── engine/         # 分析与状态引擎：分类、统计、推荐、族谱、成就雷达……
├── render/         # 六页可视化（Jinja2 模板）
├── webapp/         # 本地网页面板（HTML/CSS/JS + 轻量 HTTP API）
├── data/           # 内置只读资料表（类型/系列/中文名等）
└── tests/          # 测试
```

## 说明

- 程序**只读**你的 Steam 文件；唯一的「写回」是可选的状态分类标签，且默认关闭、需手动开启。
- 首次使用会陆续生成本机缓存（封面、中文名、快照等），都在 `game-life/data/` 与 `game-life/output/` 下，不会污染 Steam。

## 开源许可

本项目以 MIT 许可发布，欢迎自由使用与修改。
