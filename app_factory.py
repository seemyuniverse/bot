import os
from dotenv import load_dotenv
from flask import Flask
from models import db

load_dotenv()


def create_bot_app():
    app = Flask("occm_bot")
    database_url = os.environ.get("DATABASE_URL", "")
    # requirements에 들어 있는 건 psycopg3라서, 주소 앞부분을 그 드라이버 이름으로 맞춰 준다
    for prefix in ("postgres://", "postgresql://"):
        if database_url.startswith(prefix):
            database_url = "postgresql+psycopg://" + database_url[len(prefix):]
            break
    if not database_url:
        raise RuntimeError("DATABASE_URL 환경변수가 필요합니다.")
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True, "pool_recycle": 280}
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    return app
