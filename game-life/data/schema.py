import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path


def _resolve_db_path() -> Path:
    """解析 SQLite 数据库位置，兼容源码运行与 PyInstaller onefile 打包。

    - 源码运行：沿用 ``data/cache.db``（行为与以往完全一致）；
    - 打包运行（onefile）：``__file__`` 落在**临时解包目录** ``sys._MEIPASS``，
      该目录进程退出即删除，缓存放进去会「看不见也留不住」——元数据抓取、
      快照、回填全部落空。此时改为在 exe 所在目录附近寻找真实的
      ``game-life/data``；
    - 找不到既有库时退回 ``exe 同级 data/cache.db``（首次运行自动建库）。
    """
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent / "cache.db"

    exe_dir = Path(sys.executable).resolve().parent
    candidates = (
        exe_dir / "game-life" / "data",      # exe 放在仓库 game-life/ 内
        exe_dir.parent / "game-life" / "data",  # 常见布局：仓库根 dist/game-life.exe
        exe_dir / "data",                    # 与 data/ 同级分发
    )
    for folder in candidates:
        if (folder / "cache.db").is_file():
            return folder / "cache.db"
    for folder in candidates:
        if (folder / "schema.py").is_file():
            return folder / "cache.db"
    return exe_dir / "data" / "cache.db"


DB_PATH = _resolve_db_path()


def data_dir() -> Path:
    """缓存/产物所在目录（与 cache.db 同目录，兼容打包运行）。"""
    return _resolve_db_path().parent


# 集中建表：任何模块不要自行 CREATE TABLE
_TABLES = [
    # 1. 游戏元数据缓存表（与 store_api.py 使用的结构一致）
    """CREATE TABLE IF NOT EXISTS games (
        appid INTEGER PRIMARY KEY,
        name TEXT,
        genres TEXT,
        categories TEXT,
        developers TEXT,
        price INTEGER,
        release_date TEXT,
        last_updated TEXT
    )""",
    # 2. 游玩数据表（每次 run 更新）
    """CREATE TABLE IF NOT EXISTS play_stats (
        appid INTEGER PRIMARY KEY,
        playtime INTEGER,
        last_played INTEGER,
        status TEXT
    )""",
    # 3. 历史快照表
    """CREATE TABLE IF NOT EXISTS snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        record_date TEXT,
        appid INTEGER,
        playtime INTEGER,
        last_played INTEGER,
        status TEXT
    )""",
    # 4. 挑战进度表（xp_awarded = 该月已发放 XP，作为幂等记账用）
    """CREATE TABLE IF NOT EXISTS challenges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        month TEXT,
        title TEXT,
        appids TEXT,
        completed INTEGER DEFAULT 0,
        completed_date TEXT,
        xp_awarded INTEGER DEFAULT 0
    )""",
    # 5. 玩家经验表
    """CREATE TABLE IF NOT EXISTS player_xp (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        total_xp INTEGER DEFAULT 0,
        level INTEGER DEFAULT 1
    )""",
    # 6. 成就缓存表（achievements.py 用，7 天内不重复抓取）
    """CREATE TABLE IF NOT EXISTS achievements (
        appid INTEGER PRIMARY KEY,
        source TEXT,
        achieved_count INTEGER,
        total_count INTEGER,
        completion_rate REAL,
        last_updated TEXT
    )""",
    # 7. HLTB 时长缓存表（hltb.py 用，7 天内不重复抓取；查不到也记负缓存）
    """CREATE TABLE IF NOT EXISTS hltb (
        appid INTEGER PRIMARY KEY,
        name TEXT,
        main REAL,
        extra REAL,
        completionist REAL,
        last_updated TEXT
    )""",
]

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_snapshots_date ON snapshots(record_date)",
    "CREATE INDEX IF NOT EXISTS idx_snapshots_appid ON snapshots(appid)",
]


# 幂等列迁移：旧库已存在同名表时 CREATE TABLE IF NOT EXISTS 不会补列，
# 这里先用 PRAGMA table_info 判断，缺列才 ALTER TABLE ADD COLUMN。
# 表名 -> [(列名, 列定义)]
_COLUMN_MIGRATIONS = {
    "challenges": [
        ("xp_awarded", "INTEGER DEFAULT 0"),
    ],
    # 元数据扩展列（mainland 多源抓取：本地 appinfo / Steam 官方镜像 / 官方商店 API）
    # 幂等：旧库缺列时自动 ALTER TABLE 补齐，不动既有列。
    "games": [
        ("name_cn", "TEXT"),
        ("genres_cn", "TEXT"),
        ("header_image", "TEXT"),
        ("publisher", "TEXT"),
        ("source", "TEXT"),
        # T7：负缓存时间戳（「什么都没抓到」的行用它算**短 TTL**；老行没有它 -> 立即失效重试）
        ("miss_at", "TEXT"),
        # T9：价格来源（xiaoheihe / steampowered / steamchina …），如实记录不写假
        ("price_source", "TEXT"),
    ],
}


def _migrate_columns(db: sqlite3.Connection) -> None:
    """为已存在的旧表补齐新增列（幂等）。"""
    for table, columns in _COLUMN_MIGRATIONS.items():
        existing = {
            str(row[1]) for row in db.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if not existing:
            continue  # 表不存在（理论上不会发生，建表已在前面执行）
        for column, ddl in columns:
            if column in existing:
                continue
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db():
    """统一建表入口，幂等，可重复调用。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB_PATH))
    try:
        for sql in _TABLES + _INDEXES:
            db.execute(sql)
        _migrate_columns(db)
        db.commit()
    finally:
        db.close()


def get_connection():
    """打开连接并保证表已建好。"""
    init_db()
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    return db


def _is_fresh(last_updated: str) -> bool:
    try:
        d = datetime.strptime(last_updated, "%Y-%m-%d").date()
        return (date.today() - d).days < 7
    except (ValueError, TypeError):
        return False
