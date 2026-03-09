"""
HeadHunter Crawler — Flask 應用入口
"""
import os
import logging

# 載入 .env 檔（API Keys 等敏感設定）
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
from logging.handlers import RotatingFileHandler

import yaml
from flask import Flask
from flask_cors import CORS


def load_config():
    """載入設定檔，並將相對路徑轉為絕對路徑"""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(base_dir, 'config', 'default.yaml')
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    # 將相對路徑轉為基於專案根目錄的絕對路徑
    def resolve(path):
        if path and not os.path.isabs(path):
            return os.path.join(base_dir, path)
        return path

    # 解析各個檔案路徑
    if 'google_sheets' in config:
        cf = config['google_sheets'].get('credentials_file')
        if cf:
            config['google_sheets']['credentials_file'] = resolve(cf)
    if 'dedup' in config:
        cf = config['dedup'].get('cache_file')
        if cf:
            config['dedup']['cache_file'] = resolve(cf)
    if 'scheduler' in config:
        tf = config['scheduler'].get('tasks_file')
        if tf:
            config['scheduler']['tasks_file'] = resolve(tf)

    # ── 環境變數覆蓋 API Keys ──
    api_keys = config.setdefault('api_keys', {})
    if os.environ.get('BRAVE_API_KEY'):
        api_keys['brave_api_key'] = os.environ['BRAVE_API_KEY']
    if os.environ.get('PERPLEXITY_API_KEY'):
        api_keys['perplexity_api_key'] = os.environ['PERPLEXITY_API_KEY']
        # 同步到 enrichment.perplexity.api_key
        config.setdefault('enrichment', {}).setdefault('perplexity', {})['api_key'] = os.environ['PERPLEXITY_API_KEY']
    if os.environ.get('GITHUB_TOKENS'):
        api_keys['github_tokens'] = [t.strip() for t in os.environ['GITHUB_TOKENS'].split(',') if t.strip()]

    return config


def setup_logging(config):
    """設定 logging"""
    log_cfg = config.get('logging', {})
    log_file = os.path.join(os.path.dirname(__file__), log_cfg.get('file', 'logs/crawler.log'))
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    handler = RotatingFileHandler(
        log_file,
        maxBytes=log_cfg.get('max_bytes', 10485760),
        backupCount=log_cfg.get('backup_count', 5),
        encoding='utf-8',
    )
    handler.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    ))

    level = getattr(logging, log_cfg.get('level', 'INFO').upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)

    # 同時輸出到 console
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter('[%(levelname)s] %(name)s: %(message)s'))
    console.setLevel(level)
    root.addHandler(console)


def create_app(test_config=None):
    """建立 Flask 應用"""
    config = load_config()
    if test_config:
        config.update(test_config)
    setup_logging(config)

    app = Flask(
        __name__,
        template_folder='web/templates',
        static_folder='web/static',
    )
    app.config['CRAWLER_CONFIG'] = config
    app.secret_key = os.urandom(24)

    CORS(app)

    # 確保資料目錄存在
    for d in ['data', 'logs']:
        os.makedirs(os.path.join(os.path.dirname(__file__), d), exist_ok=True)

    # 初始化 Task Manager
    from scheduler.task_manager import TaskManager
    task_manager = TaskManager(config)
    app.config['TASK_MANAGER'] = task_manager

    # 初始化本地儲存（取代 Google Sheets）
    try:
        from storage.local_store import LocalStore
        data_dir = os.path.join(os.path.dirname(__file__), 'data')
        store = LocalStore(data_dir=data_dir)
        app.config['SHEETS_STORE'] = store  # 保持同名 key，路由層不需改動
    except Exception as e:
        logging.getLogger(__name__).warning(f"本地儲存初始化失敗: {e}")
        app.config['SHEETS_STORE'] = None

    # 初始化 Step1ne Client (可選)
    step1ne_cfg = config.get('step1ne', {})
    if step1ne_cfg.get('api_base_url'):
        from integration.step1ne_client import Step1neClient
        app.config['STEP1NE_CLIENT'] = Step1neClient(step1ne_cfg['api_base_url'])
    else:
        app.config['STEP1NE_CLIENT'] = None

    # 註冊 Blueprint
    from api.routes import api_bp
    from web.views import web_bp
    app.register_blueprint(api_bp, url_prefix='/api')
    app.register_blueprint(web_bp)

    # 啟動排程器
    task_manager.start()

    return app


if __name__ == '__main__':
    app = create_app()
    config = app.config['CRAWLER_CONFIG']
    server = config.get('server', {})
    app.run(
        host=server.get('host', '0.0.0.0'),
        port=server.get('port', 5000),
        debug=server.get('debug', False),
    )
