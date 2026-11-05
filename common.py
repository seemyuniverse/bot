import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import inspect, text

from models import db, BotSetting, LootItem, LootGrade, GameOption

KST = ZoneInfo("Asia/Seoul")


def current_week_start():
    now = datetime.now(KST)
    monday = now.date() - timedelta(days=now.weekday())
    return monday.isoformat()


def get_settings():
    db.session.expire_all()
    settings = db.session.get(BotSetting, 1)
    if settings is None:
        settings = BotSetting(id=1)
        db.session.add(settings)
        db.session.commit()
    return settings


# 랜덤 요소 종류: 키 -> (화면에 보일 이름, 답글에서 쓰이는 자리 표시)
OPTION_CATEGORIES = {
    "region": ("지역", "{region}"),
    "zone_grade": ("구역 등급", "{zone_grade} (숫자만 입력, 예: 3)"),
    "zone_type": ("구역 유형", "{zone_type}"),
    "body_part": ("부상 부위", "{body_part}"),
    "injury": ("부상 종류", "{injury}, {injury_obj}"),
}


def ensure_schema():
    """create_all은 이미 있는 테이블에 컬럼을 추가해 주지 않아서, 나중에 늘린 컬럼은 여기서 직접 붙인다."""
    insp = inspect(db.engine)
    cols = {c["name"] for c in insp.get_columns("loot_items")}
    with db.engine.begin() as conn:
        if "grade" not in cols:
            conn.execute(text("ALTER TABLE loot_items ADD COLUMN grade VARCHAR(50) NOT NULL DEFAULT ''"))
        if "description" not in cols:
            conn.execute(text("ALTER TABLE loot_items ADD COLUMN description TEXT"))

        setting_cols = {c["name"] for c in insp.get_columns("bot_settings")}
        if "default_keywords" not in setting_cols:
            conn.execute(text("ALTER TABLE bot_settings ADD COLUMN default_keywords VARCHAR(255) NOT NULL DEFAULT '진입, 조사, 탐사'"))
        if "limit_reached_text" not in setting_cols:
            conn.execute(text("ALTER TABLE bot_settings ADD COLUMN limit_reached_text TEXT NOT NULL DEFAULT '@{acct}\n이번 주의 게이트 할당량을 모두 채웠습니다.\n추가 탐사를 진행하려면 [차원변이구역 입장권]을 소지하고 키워드를 포함하여 시도해 주세요.'"))
        if "enable_quiet_hours" not in setting_cols:
            conn.execute(text("ALTER TABLE bot_settings ADD COLUMN enable_quiet_hours BOOLEAN NOT NULL DEFAULT FALSE"))
        if "quiet_start_hour" not in setting_cols:
            conn.execute(text("ALTER TABLE bot_settings ADD COLUMN quiet_start_hour INTEGER NOT NULL DEFAULT 3"))
        if "quiet_end_hour" not in setting_cols:
            conn.execute(text("ALTER TABLE bot_settings ADD COLUMN quiet_end_hour INTEGER NOT NULL DEFAULT 7"))
        if "quiet_hours_text" not in setting_cols:
            conn.execute(text("ALTER TABLE bot_settings ADD COLUMN quiet_hours_text TEXT NOT NULL DEFAULT '@{acct}\n현재 시간은 게이트 안정화(점검) 시간으로 탐사가 제한됩니다.\n점검 시간 종료 후 다시 시도해 주세요.'"))
        if "append_timestamp" not in setting_cols:
            conn.execute(text("ALTER TABLE bot_settings ADD COLUMN append_timestamp BOOLEAN NOT NULL DEFAULT TRUE"))

        quota_cols = {c["name"] for c in insp.get_columns("user_quotas")}
        if "limit_notice_sent" not in quota_cols:
            conn.execute(text("ALTER TABLE user_quotas ADD COLUMN limit_notice_sent BOOLEAN NOT NULL DEFAULT FALSE"))

        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS bot_state (key VARCHAR(100) PRIMARY KEY, value TEXT)"
        ))


def seed_defaults():
    """game_data.py에 적어 둔 초기값을 DB에 처음 한 번만 넣는다.
    그 뒤로는 관리자 화면에서 고친 값이 우선이라, 봇을 재시작해도 덮어쓰지 않는다."""
    from game_data import (
        DEFAULT_TEMPLATE, LEGACY_TEMPLATES, LOOT, GRADE_PROBABILITY, DEFAULT_OPTIONS,
    )

    settings = get_settings()
    if settings.template in LEGACY_TEMPLATES:
        settings.template = DEFAULT_TEMPLATE

    # 이미 한 번 넣었다면 여기서 끝낸다. 관리자가 지운 항목이 재시작 때 다시 생기면 곤란하다.
    if db.session.execute(text("SELECT 1 FROM bot_state WHERE key='seed_done'")).first():
        db.session.commit()
        return

    # 보상 등급
    existing_grades = {g.name for g in LootGrade.query.all()}
    for order, (name, prob) in enumerate(GRADE_PROBABILITY.items()):
        if name not in existing_grades:
            db.session.add(LootGrade(name=name, probability=prob, sort_order=order))

    # 보상 아이템. 등급 안에서의 확률은 일단 전부 1로 둔다
    items = {i.name: i for i in LootItem.query.all()}
    order = 0
    for grade, entries in LOOT.items():
        for name, desc in entries:
            order += 1
            item = items.get(name)
            if item is None:
                db.session.add(LootItem(name=name, weight=1.0, grade=grade, description=desc,
                                        is_active=True, sort_order=order))
            else:
                if not item.grade:
                    item.grade = grade
                if item.description is None:
                    item.description = desc
    # 등급 없이 만들어진 예전 아이템은 첫 번째(가장 흔한) 등급에 넣어 둔다
    first_grade = next(iter(GRADE_PROBABILITY))
    for item in items.values():
        if not item.grade:
            item.grade = first_grade

    # 랜덤 요소는 해당 종류에 항목이 하나도 없을 때만 채운다
    for category, entries in DEFAULT_OPTIONS.items():
        if GameOption.query.filter_by(category=category).count() == 0:
            for order, (value, weight) in enumerate(entries):
                db.session.add(GameOption(category=category, value=str(value), weight=weight, sort_order=order))
    db.session.execute(text("INSERT INTO bot_state(key, value) VALUES ('seed_done', '1')"))
    db.session.commit()


def pick_option(category):
    rows = GameOption.query.filter_by(category=category, is_active=True).filter(GameOption.weight > 0).all()
    if not rows:
        label = OPTION_CATEGORIES.get(category, (category,))[0]
        raise RuntimeError(f"'{label}' 목록에 활성화된 항목이 없습니다. 관리자 화면에서 추가하세요.")
    return random.choices(rows, weights=[r.weight for r in rows], k=1)[0].value


def pick_loot():
    """먼저 등급을 확률대로 뽑고, 뽑힌 등급 안에서 아이템 확률대로 하나를 고른다."""
    items = LootItem.query.filter_by(is_active=True).filter(LootItem.weight > 0).all()
    by_grade = {}
    for item in items:
        by_grade.setdefault(item.grade, []).append(item)
    grades = [g for g in LootGrade.query.filter(LootGrade.probability > 0).all() if g.name in by_grade]
    if not grades:
        raise RuntimeError("추첨 가능한 보상이 없습니다. 보상 등급 확률과 아이템을 확인하세요.")
    grade = random.choices(grades, weights=[g.probability for g in grades], k=1)[0]
    pool = by_grade[grade.name]
    return random.choices(pool, weights=[i.weight for i in pool], k=1)[0]


CATEGORY_ALIASES = {
    "지역": "region",
    "구역등급": "zone_grade",
    "구역 등급": "zone_grade",
    "등급": "zone_grade",
    "구역유형": "zone_type",
    "구역 유형": "zone_type",
    "유형": "zone_type",
    "부상부위": "body_part",
    "부상 부위": "body_part",
    "부위": "body_part",
    "부상": "injury",
    "부상종류": "injury",
    "부상 종류": "injury",
    "region": "region",
    "zone_grade": "zone_grade",
    "zone_type": "zone_type",
    "body_part": "body_part",
    "injury": "injury",
}


def decode_uploaded_file(file_storage):
    """업로드된 파일 바이트를 다양한 한글 인코딩(UTF-8-SIG, UTF-8, CP949 등)으로 안전하게 텍스트로 변환한다."""
    raw = file_storage.read()
    for enc in ("utf-8-sig", "utf-8", "cp949", "euc-kr"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_bulk_items_text(text_content, default_grade="일반"):
    """CSV/TSV 또는 텍스트 파일 내용을 읽어 아이템 리스트 [{'name', 'grade', 'weight', 'description'}]를 추출한다."""
    import csv
    import io
    rows = []
    lines = [line.strip() for line in text_content.splitlines() if line.strip()]
    if not lines:
        return rows

    current_grade = default_grade
    for line in lines:
        if line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            g = line[1:-1].strip()
            if g:
                current_grade = g
            continue

        delimiter = "\t" if "\t" in line else ","
        reader = csv.reader(io.StringIO(line), delimiter=delimiter)
        parts = next(reader, [])
        parts = [p.strip() for p in parts if p is not None]
        if not parts or not parts[0]:
            continue

        col1 = parts[0]
        if col1 in ("이름", "아이템", "아이템명", "아이템 이름", "name", "item"):
            continue

        grade = current_grade
        weight = 1.0
        desc = ""

        if len(parts) == 1:
            if " - " in col1:
                col1, desc = [x.strip() for x in col1.split(" - ", 1)]
        elif len(parts) == 2:
            try:
                weight = float(parts[1])
            except ValueError:
                grade = parts[1] or current_grade
        elif len(parts) == 3:
            grade = parts[1] or current_grade
            try:
                weight = float(parts[2])
            except ValueError:
                desc = parts[2]
        else:
            grade = parts[1] or current_grade
            try:
                weight = float(parts[2])
            except ValueError:
                weight = 1.0
            desc = parts[3]

        rows.append({
            "name": col1,
            "grade": grade,
            "weight": max(0.0, weight),
            "description": desc,
        })
    return rows


def parse_bulk_options_text(text_content, default_category="region"):
    """CSV/TSV 또는 텍스트 파일 내용을 읽어 랜덤 요소 리스트 [{'category', 'value', 'weight'}]를 추출한다."""
    import csv
    import io
    rows = []
    lines = [line.strip() for line in text_content.splitlines() if line.strip()]
    if not lines:
        return rows

    curr_cat = default_category
    for line in lines:
        if line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            tag = line[1:-1].strip()
            if tag in CATEGORY_ALIASES:
                curr_cat = CATEGORY_ALIASES[tag]
            continue

        delimiter = "\t" if "\t" in line else ","
        reader = csv.reader(io.StringIO(line), delimiter=delimiter)
        parts = next(reader, [])
        parts = [p.strip() for p in parts if p is not None]
        if not parts or not parts[0]:
            continue

        col1 = parts[0]
        if col1 in ("분류", "카테고리", "category", "종류", "값", "내용", "value"):
            continue

        cat = curr_cat
        val = col1
        weight = 1.0

        if len(parts) >= 2:
            clean_col1 = col1.replace(" ", "")
            if clean_col1 in CATEGORY_ALIASES:
                cat = CATEGORY_ALIASES[clean_col1]
                val = parts[1]
                if len(parts) >= 3:
                    try:
                        weight = float(parts[2])
                    except ValueError:
                        weight = 1.0
            else:
                try:
                    weight = float(parts[1])
                except ValueError:
                    weight = 1.0

        if val:
            rows.append({
                "category": cat,
                "value": val,
                "weight": max(0.0, weight),
            })
    return rows


def parse_bulk_users_text(text_content):
    """CSV/TSV 또는 텍스트 파일 내용을 읽어 사용자 리스트 [{'acct', 'display_name', 'keywords'}]를 추출한다."""
    import csv
    import io
    rows = []
    lines = [line.strip() for line in text_content.splitlines() if line.strip()]
    if not lines:
        return rows

    for line in lines:
        if line.startswith("#"):
            continue
        delimiter = "\t" if "\t" in line else ","
        reader = csv.reader(io.StringIO(line), delimiter=delimiter)
        parts = next(reader, [])
        parts = [p.strip() for p in parts if p is not None]
        if not parts or not parts[0]:
            continue

        col1 = parts[0].lstrip("@").lower()
        if col1 in ("계정", "아이디", "username", "acct", "user"):
            continue

        display_name = ""
        keywords = "진입, 조사, 탐사"
        if len(parts) >= 2:
            display_name = parts[1]
        if len(parts) >= 3:
            keywords = ", ".join([k.strip() for k in parts[2:] if k.strip()])

        rows.append({
            "acct": col1,
            "display_name": display_name,
            "keywords": keywords,
        })
    return rows

