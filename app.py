import hmac
import os
import secrets
from datetime import datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from mastodon import Mastodon
from sqlalchemy import func

from models import (
    db, BotUser, LootItem, UserQuota,
    ResponseLog, AdminAuditLog, LootGrade, GameOption, AdminAccount,
)
from common import (
    current_week_start, get_settings, OPTION_CATEGORIES,
    decode_uploaded_file, parse_bulk_items_text, parse_bulk_options_text, parse_bulk_users_text,
)

load_dotenv()

OAUTH_SCOPES = ["read:accounts"]

# 기록 화면에 보여줄 처리 결과 이름. 코드 안에서 쓰는 값은 그대로 두고 화면 표기만 바꾼다.
OUTCOME_LABELS = {
    "success": "답글 완료",
    "ignored": "처리하지 않고 넘김",
    "keyword_not_found": "반응 키워드가 없어서 넘김",
    "not_mention": "멘션이 아니라서 넘김",
    "self_or_invalid": "봇 본인 글이거나 잘못된 알림",
    "not_mutual_follow": "맞팔로우가 아니라서 넘김",
    "bot_paused": "봇 일시 정지 중이라 넘김",
    "user_suspended": "이용 정지된 사용자라 넘김",
    "user_not_allowed_or_suspended": "이용 정지된 사용자라 넘김",
    "quiet_hours_notice": "쉬는 시간 안내 발송",
    "weekly_limit_notice_sent": "주간 한도 초과 안내 발송",
    "weekly_limit_already_notified": "주간 한도 초과 (안내는 이미 보내서 응답 없음)",
    "weekly_limit_reached": "주간 한도 초과",
    "weekly_limit_need_ticket": "주간 한도 초과 안내 발송",
    "quota_error": "횟수 처리 오류",
    "error": "오류 발생 (상세 내용 확인)",
}

# 관리자 변경 이력에 보여줄 작업 이름
AUDIT_LABELS = {
    "admin_login": "관리자 로그인",
    "admin_login_denied": "관리자 로그인 거부",
    "admin_add": "관리자 추가",
    "admin_remove": "관리자 권한 회수",
    "user_save": "사용자 저장",
    "user_delete": "사용자 삭제",
    "ticket_add": "입장권 지급 (예전 기능)",
    "ticket_remove": "입장권 회수 (예전 기능)",
    "item_save": "아이템 저장",
    "item_disable": "아이템 비활성화",
    "item_delete": "아이템 삭제",
    "grade_save": "보상 등급 저장",
    "grade_delete": "보상 등급 삭제",
    "option_save": "랜덤 요소 저장",
    "option_delete": "랜덤 요소 삭제",
    "settings_update": "운영 설정 변경",
    "bot_pause": "봇 일시 정지",
    "bot_resume": "봇 다시 시작",
    "bulk_items_save": "아이템 일괄 등록",
    "bulk_options_save": "랜덤 요소 일괄 등록",
    "bulk_users_save": "사용자 일괄 등록",
    "item_toggle": "아이템 상태 변경",
    "option_toggle": "랜덤 요소 상태 변경",
    "user_toggle": "사용자 상태 변경",
}


def env_admin_accts():
    """ADMIN_ACCTS에 적어 둔 계정들. 처음 로그인할 사람을 정해 두는 용도라 화면에서는 지울 수 없다."""
    raw = os.environ.get("ADMIN_ACCTS", "")
    return {a.strip().lstrip("@").lower() for a in raw.split(",") if a.strip()}


def is_admin_acct(acct):
    acct = (acct or "").strip().lstrip("@").lower()
    if not acct:
        return False
    if acct in env_admin_accts():
        return True
    return AdminAccount.query.filter(func.lower(AdminAccount.acct) == acct).first() is not None


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "")
if not app.config["SECRET_KEY"]:
    raise RuntimeError("FLASK_SECRET_KEY 환경변수가 필요합니다.")

database_url = os.environ.get("DATABASE_URL", "")
# requirements에 들어 있는 건 psycopg3라서, 주소 앞부분을 그 드라이버 이름으로 맞춰 준다
# (Railway나 Neon 같은 곳의 주소는 보통 postgres:// 나 postgresql:// 로 시작한다)
for _prefix in ("postgres://", "postgresql://"):
    if database_url.startswith(_prefix):
        database_url = "postgresql+psycopg://" + database_url[len(_prefix):]
        break
if not database_url:
    raise RuntimeError("DATABASE_URL 환경변수가 필요합니다.")

app.config.update(
    SQLALCHEMY_DATABASE_URI=database_url,
    SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True, "pool_recycle": 280},  # 오래 놀던 DB 연결이 끊겨 있어도 다시 이어 쓴다
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax" if os.environ.get("COOKIE_SECURE", "true").lower() == "true" else None,
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "true").lower() == "true",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)
db.init_app(app)


@app.template_filter("outcome_label")
def outcome_label(value):
    return OUTCOME_LABELS.get(value, value)


@app.template_filter("audit_label")
def audit_label(value):
    return AUDIT_LABELS.get(value, value)


def audit(action, target="", detail=""):
    db.session.add(AdminAuditLog(action=action, target=target, detail=detail[:4000]))
    db.session.commit()


def csrf_token():
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


@app.context_processor
def inject_globals():
    return {"csrf_token": csrf_token, "kst_now": datetime.now(ZoneInfo("Asia/Seoul"))}


def require_csrf():
    submitted = request.form.get("_csrf", "")
    expected = session.get("_csrf", "")
    if not expected or not hmac.compare_digest(submitted, expected):
        abort(400, "잘못된 요청입니다. 페이지를 새로고침한 뒤 다시 시도하세요.")


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("login", next=request.path))
        acct = session.get("admin_acct", "")
        # 마스토돈으로 로그인한 경우엔 요청할 때마다 관리자 목록을 다시 본다. 권한을 회수하면 바로 막힌다.
        if not acct.startswith("(") and not is_admin_acct(acct):
            session.clear()
            flash("관리자 권한이 없습니다.", "error")
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapped


def oauth_redirect_uri():
    return os.environ.get("OAUTH_REDIRECT_URI") or url_for("oauth_callback", _external=True)


def oauth_configured():
    return bool(os.environ.get("MASTODON_CLIENT_ID") and os.environ.get("MASTODON_CLIENT_SECRET"))


def mastodon_client():
    return Mastodon(
        client_id=os.environ.get("MASTODON_CLIENT_ID"),
        client_secret=os.environ.get("MASTODON_CLIENT_SECRET"),
        api_base_url=os.environ.get("MASTODON_BASE_URL", "https://occm.cc"),
    )


def finish_login(acct):
    session.clear()
    session["admin_logged_in"] = True
    session["admin_acct"] = acct
    session.permanent = True
    audit("admin_login", acct)


@app.route("/login", methods=["GET", "POST"])
def login():
    password_enabled = bool(os.environ.get("ADMIN_USERNAME") and os.environ.get("ADMIN_PASSWORD"))
    if request.method == "POST":
        require_csrf()
        if not password_enabled:
            abort(400, "비밀번호 로그인이 비활성화되어 있습니다. 마스토돈으로 로그인하세요.")
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        expected_user = os.environ.get("ADMIN_USERNAME", "")
        expected_password = os.environ.get("ADMIN_PASSWORD", "")
        user_ok = hmac.compare_digest(username.encode(), expected_user.encode())
        pass_ok = hmac.compare_digest(password.encode(), expected_password.encode())
        if user_ok and pass_ok:
            finish_login("(비밀번호 로그인)")
            flash("로그인했습니다.", "success")
            return redirect(url_for("dashboard"))
        flash("아이디 또는 비밀번호가 올바르지 않습니다.", "error")
    return render_template(
        "login.html", oauth_enabled=oauth_configured(), password_enabled=password_enabled,
        server=os.environ.get("MASTODON_BASE_URL", "https://occm.cc"),
    )


@app.route("/oauth/start")
def oauth_start():
    if not oauth_configured():
        abort(400, "MASTODON_CLIENT_ID / MASTODON_CLIENT_SECRET 환경변수가 설정되지 않았습니다.")
    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state
    url = mastodon_client().auth_request_url(
        redirect_uris=oauth_redirect_uri(), scopes=OAUTH_SCOPES, state=state,
    )
    return redirect(url)


@app.route("/oauth/callback")
def oauth_callback():
    expected = session.pop("oauth_state", "")
    state = request.args.get("state", "")
    code = request.args.get("code", "")
    if request.args.get("error") or not code:
        flash("마스토돈 로그인이 취소되었거나 실패했습니다.", "error")
        return redirect(url_for("login"))
    # 내 PC에서 http로 테스트할 때(COOKIE_SECURE=false)는 돌아오는 길에 세션 쿠키가 빠져서
    # state 확인이 항상 실패한다. 그래서 HTTPS 운영일 때만 확인한다.
    is_secure = os.environ.get("COOKIE_SECURE", "true").lower() == "true"
    if is_secure and (not expected or not hmac.compare_digest(state, expected)):
        abort(400, "로그인 요청이 만료되었습니다. 다시 시도하세요.")
    try:
        client = mastodon_client()
        client.log_in(code=code, redirect_uri=oauth_redirect_uri(), scopes=OAUTH_SCOPES)
        acct = (client.account_verify_credentials().get("acct") or "").lower()
    except Exception:
        app.logger.exception("마스토돈 로그인 실패")
        flash("마스토돈 인증 중 오류가 발생했습니다.", "error")
        return redirect(url_for("login"))
    # 다른 서버 계정은 acct에 @도메인이 붙어서 온다. occm.cc 계정만 받는다.
    if "@" in acct or not is_admin_acct(acct):
        audit("admin_login_denied", acct)
        flash(f"@{acct} 계정은 관리자 권한이 없습니다.", "error")
        return redirect(url_for("login"))
    finish_login(acct)
    flash(f"@{acct} 님, 로그인했습니다.", "success")
    return redirect(url_for("dashboard"))


@app.post("/logout")
@login_required
def logout():
    require_csrf()
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    settings = get_settings()
    users_count = BotUser.query.count()
    items_count = LootItem.query.filter_by(is_active=True).count()
    week = current_week_start()
    responses = db.session.query(func.count(ResponseLog.id)).filter(
        ResponseLog.created_at >= datetime.fromisoformat(week).replace(tzinfo=ZoneInfo("Asia/Seoul"))
    ).scalar() or 0
    recent = ResponseLog.query.order_by(ResponseLog.id.desc()).limit(8).all()
    return render_template(
        "dashboard.html", settings=settings, users_count=users_count,
        items_count=items_count, responses=responses,
        recent=recent,
    )


@app.route("/users", methods=["GET", "POST"])
@login_required
def users():
    bulk_preview = None
    if request.method == "POST":
        require_csrf()
        action = request.form.get("action")
        acct = request.form.get("acct", "").strip().lstrip("@").lower()
        if action == "save":
            if not acct:
                flash("계정 아이디를 입력하세요.", "error")
            else:
                user = BotUser.query.filter(func.lower(BotUser.acct) == acct).first()
                if user is None:
                    user = BotUser(acct=acct)
                    db.session.add(user)
                user.display_name = request.form.get("display_name", "").strip() or None
                user.keywords = [x.strip() for x in request.form.get("keywords", "").split(",") if x.strip()]
                user.is_active = request.form.get("is_active") == "on"
                db.session.commit()
                audit("user_save", acct, f"키워드: {', '.join(user.keywords) or '없음'} / {'이용 허용' if user.is_active else '이용 정지'}")
                flash("사용자 설정을 저장했습니다.", "success")
            return redirect(url_for("users"))
        elif action == "delete":
            user = BotUser.query.filter(func.lower(BotUser.acct) == acct).first_or_404()
            db.session.delete(user)
            db.session.commit()
            audit("user_delete", acct)
            flash("사용자를 삭제했습니다.", "success")
            return redirect(url_for("users"))
        elif action == "toggle_active":
            user = BotUser.query.filter(func.lower(BotUser.acct) == acct).first_or_404()
            user.is_active = not user.is_active
            db.session.commit()
            audit("user_toggle", acct, f"{'이용 허용' if user.is_active else '이용 정지'}")
            flash(f"'{acct}' 사용자를 {'이용 허용' if user.is_active else '이용 정지'} 처리했습니다.", "success")
            return redirect(url_for("users"))
        elif action == "preview_bulk":
            f = request.files.get("file")
            if not f or not f.filename:
                flash("업로드할 파일을 선택하세요.", "error")
                return redirect(url_for("users"))
            content = decode_uploaded_file(f)
            parsed = parse_bulk_users_text(content)
            if not parsed:
                flash("파일에서 등록 가능한 사용자 정보를 찾지 못했습니다.", "error")
                return redirect(url_for("users"))
            bulk_preview = parsed
        elif action == "apply_bulk":
            selected = request.form.getlist("selected")
            if not selected:
                flash("선택된 항목이 없습니다.", "error")
                return redirect(url_for("users"))
            count = 0
            for idx in selected:
                u_acct = request.form.get(f"acct_{idx}", "").strip().lstrip("@").lower()
                if not u_acct:
                    continue
                d_name = request.form.get(f"display_name_{idx}", "").strip() or None
                raw_kw = request.form.get(f"keywords_{idx}", "")
                kw_list = [k.strip() for k in raw_kw.split(",") if k.strip()]
                user = BotUser.query.filter(func.lower(BotUser.acct) == u_acct).first()
                if user is None:
                    user = BotUser(acct=u_acct)
                    db.session.add(user)
                user.display_name = d_name
                user.keywords = kw_list
                user.is_active = True
                count += 1
            db.session.commit()
            audit("bulk_users_save", f"{count}명 일괄 등록/수정")
            flash(f"총 {count}명의 사용자를 성공적으로 등록/수정했습니다.", "success")
            return redirect(url_for("users"))

    all_users = BotUser.query.order_by(BotUser.acct.asc()).all()
    week = current_week_start()
    quota_map = {q.user_id: q.used_count for q in UserQuota.query.filter_by(week_start=week).all()}
    return render_template("users.html", users=all_users, quota_map=quota_map,
                           weekly_limit=get_settings().weekly_free_limit, bulk_preview=bulk_preview)


@app.route("/items", methods=["GET", "POST"])
@login_required
def items():
    bulk_preview = None
    if request.method == "POST":
        require_csrf()
        action = request.form.get("action")
        if action == "save":
            name = request.form.get("name", "").strip()
            grade = request.form.get("grade", "").strip()
            try:
                weight = float(request.form.get("weight", "0"))
            except ValueError:
                weight = -1
            if not name or not 0 <= weight <= 1000000:
                flash("아이템 이름과 확률(0 이상의 숫자)을 확인하세요.", "error")
            elif not LootGrade.query.filter_by(name=grade).first():
                flash("등급을 선택하세요.", "error")
            else:
                item_id = request.form.get("item_id")
                item = db.session.get(LootItem, int(item_id)) if item_id else None
                if item is None:
                    if LootItem.query.filter_by(name=name).first():
                        flash("같은 이름의 아이템이 이미 있습니다.", "error")
                        return redirect(url_for("items"))
                    item = LootItem(name=name)
                    db.session.add(item)
                item.name = name
                item.weight = weight
                item.grade = grade
                item.description = request.form.get("description", "").strip() or None
                item.is_active = request.form.get("is_active") == "on"
                db.session.commit()
                audit("item_save", name, f"등급 {grade} / 확률 {weight} / {'사용' if item.is_active else '중지'}")
                flash("아이템을 저장했습니다.", "success")
            return redirect(url_for("items"))
        elif action == "delete":
            item = LootItem.query.get_or_404(int(request.form.get("item_id")))
            item.is_active = False
            db.session.commit()
            audit("item_disable", item.name)
            flash("아이템을 비활성화했습니다. 기존 기록은 보존됩니다.", "success")
            return redirect(url_for("items"))
        elif action == "toggle_active":
            item = LootItem.query.get_or_404(int(request.form.get("item_id")))
            item.is_active = not item.is_active
            db.session.commit()
            audit("item_toggle", item.name, f"{'활성화' if item.is_active else '비활성화'}")
            flash(f"아이템 '{item.name}'을(를) {'활성화' if item.is_active else '비활성화'}했습니다.", "success")
            return redirect(url_for("items"))
        elif action == "purge":
            item = LootItem.query.get_or_404(int(request.form.get("item_id")))
            name = item.name
            db.session.delete(item)  # 응답 기록에는 아이템 이름이 글자로 저장돼 있어서 지워도 기록은 그대로다
            db.session.commit()
            audit("item_delete", name)
            flash("아이템을 삭제했습니다.", "success")
            return redirect(url_for("items"))
        elif action == "save_grade":
            name = request.form.get("name", "").strip()
            try:
                probability = float(request.form.get("probability", "0"))
            except ValueError:
                probability = -1
            if not name or not 0 <= probability <= 1000000:
                flash("등급 이름과 0 이상의 확률을 확인하세요.", "error")
            else:
                grade_id = request.form.get("grade_id")
                grade = db.session.get(LootGrade, int(grade_id)) if grade_id else None
                if grade is None:
                    if LootGrade.query.filter_by(name=name).first():
                        flash("같은 이름의 등급이 이미 있습니다.", "error")
                        return redirect(url_for("items"))
                    grade = LootGrade(name=name, sort_order=LootGrade.query.count())
                    db.session.add(grade)
                elif grade.name != name:
                    LootItem.query.filter_by(grade=grade.name).update({"grade": name})
                grade.name = name
                grade.probability = probability
                db.session.commit()
                audit("grade_save", name, f"확률 {probability}")
                flash("등급을 저장했습니다.", "success")
            return redirect(url_for("items"))
        elif action == "delete_grade":
            grade = LootGrade.query.get_or_404(int(request.form.get("grade_id")))
            if LootItem.query.filter_by(grade=grade.name).count():
                flash("이 등급을 쓰는 아이템이 있어 삭제할 수 없습니다. 확률을 0으로 두거나 아이템을 먼저 옮기세요.", "error")
            else:
                db.session.delete(grade)
                db.session.commit()
                audit("grade_delete", grade.name)
                flash("등급을 삭제했습니다.", "success")
            return redirect(url_for("items"))
        elif action == "preview_bulk":
            f = request.files.get("file")
            if not f or not f.filename:
                flash("업로드할 파일을 선택하세요.", "error")
                return redirect(url_for("items"))
            content = decode_uploaded_file(f)
            first_grade = LootGrade.query.order_by(LootGrade.sort_order).first()
            default_grade = first_grade.name if first_grade else "일반"
            parsed = parse_bulk_items_text(content, default_grade=default_grade)
            if not parsed:
                flash("파일에서 등록 가능한 아이템 정보를 찾지 못했습니다.", "error")
                return redirect(url_for("items"))
            bulk_preview = parsed
        elif action == "apply_bulk":
            selected = request.form.getlist("selected")
            if not selected:
                flash("선택된 항목이 없습니다.", "error")
                return redirect(url_for("items"))
            count = 0
            for idx in selected:
                name = request.form.get(f"name_{idx}", "").strip()
                if not name:
                    continue
                grade_name = request.form.get(f"grade_{idx}", "").strip() or "일반"
                try:
                    weight = float(request.form.get(f"weight_{idx}", "1.0"))
                except ValueError:
                    weight = 1.0
                desc = request.form.get(f"description_{idx}", "").strip() or None
                if not LootGrade.query.filter_by(name=grade_name).first():
                    new_g = LootGrade(name=grade_name, probability=1.0, sort_order=LootGrade.query.count())
                    db.session.add(new_g)
                    db.session.flush()
                it = LootItem.query.filter_by(name=name).first()
                if it is None:
                    it = LootItem(name=name, sort_order=LootItem.query.count())
                    db.session.add(it)
                it.grade = grade_name
                it.weight = max(0.0, weight)
                it.description = desc
                it.is_active = True
                count += 1
            db.session.commit()
            audit("bulk_items_save", f"{count}개 아이템 일괄 등록/수정")
            flash(f"총 {count}개의 아이템을 성공적으로 등록/수정했습니다.", "success")
            return redirect(url_for("items"))

    grades = LootGrade.query.order_by(LootGrade.sort_order, LootGrade.id).all()
    all_items = LootItem.query.order_by(LootItem.sort_order, LootItem.id).all()
    # 화면에 보여줄 실제 확률은 (등급 확률 / 뽑을 수 있는 등급 확률의 합) * (아이템 확률 / 그 등급 안 확률의 합)
    weight_sum = {}
    for it in all_items:
        if it.is_active and it.weight > 0:
            weight_sum[it.grade] = weight_sum.get(it.grade, 0) + it.weight
    live = [g for g in grades if g.probability > 0 and g.name in weight_sum]
    grade_total = sum(g.probability for g in live)
    grade_prob = {g.name: g.probability / grade_total * 100 for g in live} if grade_total else {}
    chances = {}
    for it in all_items:
        if it.is_active and it.weight > 0 and it.grade in grade_prob:
            chances[it.id] = grade_prob[it.grade] * it.weight / weight_sum[it.grade]
    return render_template("items.html", items=all_items, grades=grades, chances=chances,
                           grade_prob=grade_prob, grade_total=grade_total, bulk_preview=bulk_preview)


@app.route("/options", methods=["GET", "POST"])
@login_required
def options():
    bulk_preview = None
    if request.method == "POST":
        require_csrf()
        action = request.form.get("action")
        if action == "preview_bulk":
            f = request.files.get("file")
            if not f or not f.filename:
                flash("업로드할 파일을 선택하세요.", "error")
                return redirect(url_for("options"))
            content = decode_uploaded_file(f)
            default_cat = request.form.get("default_category", "region")
            parsed = parse_bulk_options_text(content, default_category=default_cat)
            if not parsed:
                flash("파일에서 등록 가능한 요소를 찾지 못했습니다.", "error")
                return redirect(url_for("options"))
            bulk_preview = parsed
        elif action == "apply_bulk":
            selected = request.form.getlist("selected")
            if not selected:
                flash("선택된 항목이 없습니다.", "error")
                return redirect(url_for("options"))
            count = 0
            for idx in selected:
                cat = request.form.get(f"category_{idx}", "").strip()
                val = request.form.get(f"value_{idx}", "").strip()
                if not cat or not val or cat not in OPTION_CATEGORIES:
                    continue
                try:
                    weight = float(request.form.get(f"weight_{idx}", "1.0"))
                except ValueError:
                    weight = 1.0
                opt = GameOption.query.filter_by(category=cat, value=val).first()
                if opt is None:
                    opt = GameOption(category=cat, sort_order=GameOption.query.filter_by(category=cat).count())
                    db.session.add(opt)
                opt.value = val
                opt.weight = max(0.0, weight)
                opt.is_active = True
                count += 1
            db.session.commit()
            audit("bulk_options_save", f"{count}개 항목 일괄 등록/수정")
            flash(f"총 {count}개의 랜덤 요소를 성공적으로 등록/수정했습니다.", "success")
            return redirect(url_for("options"))
        else:
            category = request.form.get("category", "")
            if category not in OPTION_CATEGORIES:
                abort(400, "알 수 없는 항목입니다.")
            label = OPTION_CATEGORIES[category][0]
            if action == "save":
                value = request.form.get("value", "").strip()
                try:
                    weight = float(request.form.get("weight", "0"))
                except ValueError:
                    weight = -1
                if not value or not 0 <= weight <= 1000000:
                    flash("값과 확률(0 이상의 숫자)을 확인하세요.", "error")
                else:
                    option_id = request.form.get("option_id")
                    opt = db.session.get(GameOption, int(option_id)) if option_id else None
                    duplicate = GameOption.query.filter_by(category=category, value=value).first()
                    if duplicate and (opt is None or duplicate.id != opt.id):
                        flash("같은 값이 이미 있습니다.", "error")
                        return redirect(url_for("options") + f"#{category}")
                    if opt is None:
                        opt = GameOption(category=category, sort_order=GameOption.query.filter_by(category=category).count())
                        db.session.add(opt)
                    opt.value = value
                    opt.weight = weight
                    opt.is_active = request.form.get("is_active") == "on"
                    db.session.commit()
                    audit("option_save", f"{label}:{value}", f"확률 {weight} / {'사용' if opt.is_active else '중지'}")
                    flash(f"{label} 항목을 저장했습니다.", "success")
            elif action == "delete":
                opt = GameOption.query.get_or_404(int(request.form.get("option_id")))
                audit("option_delete", f"{label}:{opt.value}")
                db.session.delete(opt)
                db.session.commit()
                flash(f"{label} 항목을 삭제했습니다.", "success")
            elif action == "toggle_active":
                opt = GameOption.query.get_or_404(int(request.form.get("option_id")))
                opt.is_active = not opt.is_active
                db.session.commit()
                audit("option_toggle", f"{label}:{opt.value}", f"{'사용' if opt.is_active else '중지'}")
                flash(f"'{opt.value}' 항목을 {'사용' if opt.is_active else '중지'} 상태로 변경했습니다.", "success")
            return redirect(url_for("options") + f"#{category}")

    groups = []
    for key, (label, hint) in OPTION_CATEGORIES.items():
        rows = GameOption.query.filter_by(category=key).order_by(GameOption.sort_order, GameOption.id).all()
        total = sum(r.weight for r in rows if r.is_active and r.weight > 0)
        groups.append({"key": key, "label": label, "hint": hint, "rows": rows, "total": total})
    return render_template("options.html", groups=groups, bulk_preview=bulk_preview,
                           category_labels={k: v[0] for k, v in OPTION_CATEGORIES.items()})


@app.route("/admins", methods=["GET", "POST"])
@login_required
def admins():
    if request.method == "POST":
        require_csrf()
        action = request.form.get("action")
        acct = request.form.get("acct", "").strip().lstrip("@").lower()
        if action == "add":
            if not acct or "@" in acct:
                flash("occm.cc 계정 아이디만 입력하세요. (예: admin)", "error")
            elif AdminAccount.query.filter(func.lower(AdminAccount.acct) == acct).first() or acct in env_admin_accts():
                flash("이미 등록된 관리자입니다.", "error")
            else:
                db.session.add(AdminAccount(acct=acct))
                db.session.commit()
                audit("admin_add", acct)
                flash(f"@{acct} 을(를) 관리자로 추가했습니다.", "success")
        elif action == "remove":
            if acct == session.get("admin_acct"):
                flash("자기 자신은 삭제할 수 없습니다.", "error")
            else:
                row = AdminAccount.query.filter(func.lower(AdminAccount.acct) == acct).first_or_404()
                db.session.delete(row)
                db.session.commit()
                audit("admin_remove", acct)
                flash(f"@{acct} 의 관리자 권한을 회수했습니다.", "success")
        return redirect(url_for("admins"))
    return render_template(
        "admins.html", rows=AdminAccount.query.order_by(AdminAccount.acct).all(),
        env_accts=sorted(env_admin_accts()), me=session.get("admin_acct"),
    )


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    current = get_settings()
    if request.method == "POST":
        require_csrf()
        try:
            current.weekly_free_limit = int(request.form.get("weekly_free_limit", "3"))
            current.poll_interval_seconds = int(request.form.get("poll_interval_seconds", "15"))
            current.defeated_min = int(request.form.get("defeated_min"))
            current.defeated_max = int(request.form.get("defeated_max"))
            current.enable_quiet_hours = request.form.get("enable_quiet_hours") == "on"
            current.quiet_start_hour = int(request.form.get("quiet_start_hour", "3")) % 24
            current.quiet_end_hour = int(request.form.get("quiet_end_hour", "7")) % 24
            current.quiet_hours_text = request.form.get("quiet_hours_text", "").strip()
        except (ValueError, TypeError):
            flash("숫자 설정을 올바르게 입력하세요.", "error")
            return redirect(url_for("settings"))

        ranges = [
            (current.defeated_min, current.defeated_max),
        ]
        if any(lo < 0 or lo > hi for lo, hi in ranges):
            flash("숫자 범위를 확인하세요. 최소값은 0 이상이며 최대값보다 클 수 없습니다.", "error")
            return redirect(url_for("settings"))
        if not 1 <= current.weekly_free_limit <= 100 or not 3 <= current.poll_interval_seconds <= 300:
            flash("주간 횟수는 1~100, 확인 간격은 3~300초로 설정하세요.", "error")
            return redirect(url_for("settings"))

        new_template = request.form.get("template", "").strip()
        try:
            new_template.format(
                acct="a", region="r", zone_grade=1, zone_type="t", defeated=1,
                body_part="b", injury="i", injury_obj="i", loot="l",
            )
        except (KeyError, IndexError, ValueError) as exc:
            db.session.rollback()
            flash(f"답글 템플릿에 쓸 수 없는 항목이 있습니다: {exc}. 아래 안내에 있는 항목만 {{중괄호}}로 감싸서 쓰세요.", "error")
            return redirect(url_for("settings"))
        current.template = new_template
        current.default_keywords = request.form.get("default_keywords", "").strip() or "진입, 조사, 탐사"
        current.limit_reached_text = request.form.get("limit_reached_text", "").strip()
        current.append_timestamp = request.form.get("append_timestamp") == "on"
        current.visibility = request.form.get("visibility", "private")
        if current.visibility not in ("private", "unlisted", "public"):
            current.visibility = "private"
        db.session.commit()
        audit("settings_update", "global", "설정 변경")
        flash("운영 설정을 저장했습니다.", "success")
        return redirect(url_for("settings"))
    return render_template("settings.html", settings=current)


@app.post("/pause")
@login_required
def pause():
    require_csrf()
    settings = get_settings()
    settings.is_paused = request.form.get("paused") == "true"
    db.session.commit()
    audit("bot_pause" if settings.is_paused else "bot_resume", "global")
    flash("봇을 일시 정지했습니다." if settings.is_paused else "봇을 다시 실행하도록 설정했습니다.", "success")
    return redirect(url_for("dashboard"))


@app.route("/logs")
@login_required
def logs():
    page = max(1, request.args.get("page", 1, type=int))
    pagination = ResponseLog.query.order_by(ResponseLog.id.desc()).paginate(page=page, per_page=30, error_out=False)
    audits = AdminAuditLog.query.order_by(AdminAuditLog.id.desc()).limit(20).all()
    return render_template("logs.html", pagination=pagination, audits=audits)


@app.errorhandler(400)
def bad_request(error):
    return render_template("error.html", message=getattr(error, "description", "잘못된 요청입니다.")), 400


with app.app_context():
    db.create_all()
    from common import ensure_schema, seed_defaults
    ensure_schema()
    get_settings()
    seed_defaults()


def _bot_worker_supervisor(flask_app):
    import time
    import logging
    from bot import main as bot_main
    worker_log = logging.getLogger("bot_worker_supervisor")
    while True:
        try:
            bot_main(flask_app=flask_app)
        except Exception as exc:
            worker_log.exception("봇 백그라운드 워커 비정상 종료, 5초 후 재시작합니다: %s", exc)
            time.sleep(5)


def _start_background_bot():
    """클라우드 환경(Render 등)에서 웹 서버가 뜰 때 봇 프로세스를 백그라운드 스레드로 함께 가동한다."""
    if os.environ.get("RUN_BOT_THREAD", "true").lower() == "false":
        return
    if not os.environ.get("MASTODON_ACCESS_TOKEN"):
        return
    import threading
    t = threading.Thread(target=_bot_worker_supervisor, args=(app,), daemon=True, name="MastodonBotWorker")
    t.start()


_start_background_bot()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)

