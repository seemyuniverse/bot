from datetime import datetime
from zoneinfo import ZoneInfo

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint

from game_data import DEFAULT_TEMPLATE

db = SQLAlchemy()


class BotUser(db.Model):
    __tablename__ = "bot_users"

    id = db.Column(db.Integer, primary_key=True)
    acct = db.Column(db.String(255), unique=True, nullable=False, index=True)
    display_name = db.Column(db.String(255), nullable=True)
    keywords = db.Column(db.JSON, nullable=False, default=list)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    gate_tickets = db.Column(db.Integer, nullable=False, default=0)  # 지금은 쓰지 않는다(예전 DB와 맞추려고 남겨 둠)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(ZoneInfo("Asia/Seoul")))


class LootItem(db.Model):
    __tablename__ = "loot_items"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False, unique=True)
    weight = db.Column(db.Float, nullable=False, default=1.0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    grade = db.Column(db.String(50), nullable=False, default="")
    description = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(ZoneInfo("Asia/Seoul")))


class LootGrade(db.Model):
    """보상 등급(4급~특급)과 등급마다의 출현 확률. 합이 100일 필요는 없고 서로의 비율만 본다."""
    __tablename__ = "loot_grades"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    probability = db.Column(db.Float, nullable=False, default=1.0)
    sort_order = db.Column(db.Integer, nullable=False, default=0)


class GameOption(db.Model):
    """답글에 섞여 들어가는 지역, 구역 등급, 구역 유형, 부상 부위, 부상 종류의 후보 목록."""
    __tablename__ = "game_options"

    id = db.Column(db.Integer, primary_key=True)
    category = db.Column(db.String(50), nullable=False, index=True)
    value = db.Column(db.String(255), nullable=False)
    weight = db.Column(db.Float, nullable=False, default=1.0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    __table_args__ = (UniqueConstraint("category", "value", name="uq_option_category_value"),)


class AdminAccount(db.Model):
    """관리 화면에 로그인할 수 있는 occm.cc 계정 목록."""
    __tablename__ = "admin_accounts"

    id = db.Column(db.Integer, primary_key=True)
    acct = db.Column(db.String(255), unique=True, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(ZoneInfo("Asia/Seoul")))


class BotSetting(db.Model):
    __tablename__ = "bot_settings"

    id = db.Column(db.Integer, primary_key=True)
    is_paused = db.Column(db.Boolean, nullable=False, default=False)
    weekly_free_limit = db.Column(db.Integer, nullable=False, default=3)
    poll_interval_seconds = db.Column(db.Integer, nullable=False, default=15)
    default_keywords = db.Column(db.String(255), nullable=False, default="진입, 조사, 탐사")
    limit_reached_text = db.Column(
        db.Text,
        nullable=False,
        default="@{acct}\n이번 주의 게이트 할당량을 모두 채웠습니다.\n추가 탐사를 진행하려면 '차원변이구역 입장권'을 소지하고 키워드를 포함하여 시도해 주세요.",
    )
    # 봇이 쉬는 시간대 설정 (한국 시간 기준, 시 단위)
    enable_quiet_hours = db.Column(db.Boolean, nullable=False, default=False)
    quiet_start_hour = db.Column(db.Integer, nullable=False, default=3)
    quiet_end_hour = db.Column(db.Integer, nullable=False, default=7)
    quiet_hours_text = db.Column(
        db.Text,
        nullable=False,
        default="@{acct}\n현재 시간은 게이트 안정화(점검) 시간으로 탐사가 제한됩니다.\n점검 시간 종료 후 다시 시도해 주세요.",
    )
    visibility = db.Column(db.String(20), nullable=False, default="private")
    append_timestamp = db.Column(db.Boolean, nullable=False, default=True)
    # 아래 no_loot_text, start_*, reward_*는 지금 쓰지 않는다. 이미 운영 중인 DB의 컬럼과 맞추려고 남겨 둔 것
    no_loot_text = db.Column(db.String(255), nullable=False, default="아무것도 발견하지 못했습니다")
    template = db.Column(
        db.Text,
        nullable=False,
        default=DEFAULT_TEMPLATE,
    )
    start_min = db.Column(db.Integer, nullable=False, default=1)
    start_max = db.Column(db.Integer, nullable=False, default=100)
    defeated_min = db.Column(db.Integer, nullable=False, default=1)
    defeated_max = db.Column(db.Integer, nullable=False, default=20)
    reward_min = db.Column(db.Integer, nullable=False, default=1)
    reward_max = db.Column(db.Integer, nullable=False, default=500)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(ZoneInfo("Asia/Seoul")), onupdate=lambda: datetime.now(ZoneInfo("Asia/Seoul")))


class UserQuota(db.Model):
    __tablename__ = "user_quotas"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("bot_users.id", ondelete="CASCADE"), nullable=False)
    week_start = db.Column(db.String(10), nullable=False)
    used_count = db.Column(db.Integer, nullable=False, default=0)
    limit_notice_sent = db.Column(db.Boolean, nullable=False, default=False)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(ZoneInfo("Asia/Seoul")))
    user = db.relationship("BotUser", backref=db.backref("quotas", lazy=True, cascade="all, delete-orphan"))
    __table_args__ = (UniqueConstraint("user_id", "week_start", name="uq_user_week"),)


class ProcessedNotification(db.Model):
    __tablename__ = "processed_notifications"

    id = db.Column(db.Integer, primary_key=True)
    notification_id = db.Column(db.String(100), unique=True, nullable=False, index=True)
    result = db.Column(db.String(30), nullable=False, default="processed")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(ZoneInfo("Asia/Seoul")))


class ResponseLog(db.Model):
    __tablename__ = "response_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("bot_users.id", ondelete="SET NULL"), nullable=True)
    acct_snapshot = db.Column(db.String(255), nullable=False)
    notification_id = db.Column(db.String(100), nullable=False, index=True)
    status_id = db.Column(db.String(100), nullable=True)
    response_text = db.Column(db.Text, nullable=True)
    loot_name = db.Column(db.String(255), nullable=True)
    start_value = db.Column(db.Integer, nullable=True)
    defeated_value = db.Column(db.Integer, nullable=True)
    reward_value = db.Column(db.Integer, nullable=True)
    ticket_used = db.Column(db.Boolean, nullable=False, default=False)
    outcome = db.Column(db.String(30), nullable=False)
    detail = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(ZoneInfo("Asia/Seoul")))
    user = db.relationship("BotUser", backref="response_logs")


class AdminAuditLog(db.Model):
    __tablename__ = "admin_audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    action = db.Column(db.String(100), nullable=False)
    target = db.Column(db.String(255), nullable=True)
    detail = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(ZoneInfo("Asia/Seoul")))
