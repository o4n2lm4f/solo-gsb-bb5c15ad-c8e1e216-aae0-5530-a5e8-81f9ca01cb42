"""全局配置，均可通过环境变量覆盖。"""
import os

# SQLite 数据库文件路径（容器内默认 /data/coldchain.db，本地默认 data/coldchain.db）
DB_PATH = os.getenv("COLDCHAIN_DB_PATH", "data/coldchain.db")
