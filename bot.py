import logging
import os
import random
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from mastodon import Mastodon
from sqlalchemy import text

from app_factory import create_bot_app
from models import db, BotUser, UserQuota, ProcessedNotification, ResponseLog
from common import (
    current_week_start, get_settings, seed_defaults, ensure_schema, pick_option, pick_loot,
)
from game_data import eul_reul

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("occm-bot")
KST = ZoneInfo("Asia/Seoul")
NOTIFICATION_PAGE = 40  # 마스토돈이 한 번에 주는 알림 수


def clean_text(html):
    # 멘션 본문은 HTML로 오기 때문에 태그를 걷어내고 글자만 남긴다
    return BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)


def roll_int(low, high):
    return random.randint(low, high)


def reserve_quota(user, settings):
    week = current_week_start()
    # 봇을 한 개만 띄운다는 전제로 짠 행 잠금이다. 봇을 두 개 이상 켜면 안 된다.
    quota = UserQuota.query.filter_by(user_id=user.id, week_start=week).with_for_update().first()
    if quota is None:
        quota = UserQuota(user_id=user.id, week_start=week, used_count=0)
        db.session.add(quota)
        db.session.flush()

    if quota.used_count >= settings.weekly_free_limit:
        return False, quota.used_count

    quota.used_count += 1
    db.session.commit()
    return True, quota.used_count


def refund_quota(user_id):
    # 답글 전송이 실패했을 때 올려 둔 사용 횟수를 되돌린다
    week = current_week_start()
    quota = UserQuota.query.filter_by(user_id=user_id, week_start=week).with_for_update().first()
    if quota:
        quota.used_count = max(0, quota.used_count - 1)
    db.session.commit()


def make_result(acct, settings):
    zone_grade = pick_option("zone_grade")
    defeated = roll_int(settings.defeated_min, settings.defeated_max)
    injury = pick_option("injury")
    loot = pick_loot()

    text_out = settings.template.format(
        acct=acct,
        region=pick_option("region"),
        zone_grade=zone_grade,
        zone_type=pick_option("zone_type"),
        defeated=defeated,
        body_part=pick_option("body_part"),
        injury=injury,
        injury_obj=eul_reul(injury),
        loot=loot.name,
    )
    # 중복 송출로 인한 마스토돈 스팸 필터링 방지용 타임스탬프 (초 단위까지 포함하여 중복 방지)
    if getattr(settings, "append_timestamp", True):
        ts = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
        text_out += f"\n\n{ts}"
    # 기록 테이블의 start_value 칸에는 구역 등급(숫자일 때만)을 넣는다. reward_value는 안 쓴다.
    grade_value = int(zone_grade) if str(zone_grade).isdigit() else None
    return text_out, loot.name, grade_value, defeated, None


def in_quiet_hours(settings):
    # 한국 시간 기준으로 지금이 쉬는 시간대인지 확인한다. 23시~6시처럼 자정을 넘기는 설정도 처리한다.
    hour = datetime.now(KST).hour
    start, end = settings.quiet_start_hour, settings.quiet_end_hour
    if start < end:
        return start <= hour < end
    if start > end:
        return hour >= start or hour < end
    return hour == start


def process_notification(api, notification, bot_id):
    notification_id = str(notification["id"])
    if ProcessedNotification.query.filter_by(notification_id=notification_id).first():
        return

    result = "ignored"
    detail = None
    user = None
    ticket_keyword_used = False  # 기본 횟수를 넘겨 입장권이라고 적고 진행한 경우

    try:
        if notification.get("type") != "mention":
            result = "not_mention"
            return

        status = notification.get("status") or {}
        account = notification.get("account") or {}
        acct = (account.get("acct") or account.get("username") or "").lstrip("@").lower()

        if not status or not account or str(account.get("id")) == str(bot_id):
            result = "self_or_invalid"
            return

        relationship = api.account_relationships([account["id"]])[0]
        if not relationship.get("following") or not relationship.get("followed_by"):
            result = "not_mutual_follow"
            return

        settings = get_settings()
        if settings.is_paused:
            result = "bot_paused"
            return

        if settings.enable_quiet_hours and in_quiet_hours(settings):
            msg = (settings.quiet_hours_text or "@{acct}\n현재 시간은 게이트 안정화(점검) 시간으로 탐사가 제한됩니다.\n점검 시간 종료 후 다시 시도해 주세요.").format(acct=acct)
            api.status_post(
                msg,
                in_reply_to_id=status["id"],
                visibility=settings.visibility,
                language="ko",
            )
            result = "quiet_hours_notice"
            return

        user = BotUser.query.filter(db.func.lower(BotUser.acct) == acct).first()
        # 맞팔 상태면 처음 멘션한 때 자동으로 사용자 목록에 넣는다
        if user is None:
            user = BotUser(acct=acct, display_name=account.get("display_name") or acct, is_active=True)
            db.session.add(user)
            db.session.flush()

        if not user.is_active:
            result = "user_suspended"
            return

        content = clean_text(status.get("content")).casefold()
        global_keywords = [k.strip() for k in (settings.default_keywords or "").split(",") if k.strip()]
        user_keywords = user.keywords or []
        all_keywords = list(set(global_keywords + user_keywords))
        if not any(str(k).casefold() in content for k in all_keywords):
            result = "keyword_not_found"
            return

        week = current_week_start()
        quota = UserQuota.query.filter_by(user_id=user.id, week_start=week).first()
        used_count = quota.used_count if quota else 0

        has_ticket_keyword = "입장권" in content  # '차원변이구역 입장권'도 이 단어를 포함한다

        if used_count < settings.weekly_free_limit:
            # 이번 주 기본 횟수가 남아 있으면 그대로 진행
            allowed, used_count = reserve_quota(user, settings)

        elif has_ticket_keyword:
            # 기본 횟수를 다 썼어도 본문에 입장권이라고 적었으면 통과시킨다.
            # 입장권을 실제로 갖고 있는지는 따지지 않는다(참여자 자율).
            if quota:
                quota.used_count += 1
                db.session.commit()
            allowed = True
            ticket_keyword_used = True

        else:
            # 기본 횟수를 다 썼고 입장권 언급도 없으면, 안내는 주마다 한 번만 보내고 이후엔 조용히 넘어간다
            if quota is None:
                quota = UserQuota(user_id=user.id, week_start=week, used_count=used_count, limit_notice_sent=False)
                db.session.add(quota)
                db.session.flush()

            if not quota.limit_notice_sent:
                msg = (settings.limit_reached_text or "@{acct}\n이번 주의 게이트 할당량을 모두 채웠습니다.\n추가 탐사를 진행하려면 '차원변이구역 입장권'을 소지하고 키워드를 포함하여 시도해 주세요.").format(acct=acct)
                try:
                    api.status_post(
                        msg,
                        in_reply_to_id=status["id"],
                        visibility=settings.visibility,
                        language="ko",
                    )
                except Exception:
                    log.exception("안내 답글 송출 실패")
                quota.limit_notice_sent = True
                db.session.commit()
                result = "weekly_limit_notice_sent"
            else:
                result = "weekly_limit_already_notified"
            return

        if not allowed:
            result = "quota_error"
            return

        try:
            # 결과 문장을 만드는 단계도 try 안에 둔다. 템플릿이 잘못돼 실패해도 횟수가 차감된 채로 남지 않게 하려는 것
            response_text, loot_name, grade_value, defeated, reward = make_result(acct, settings)
            posted = api.status_post(
                response_text,
                in_reply_to_id=status["id"],
                visibility=settings.visibility,
                language="ko",
            )
        except Exception:
            refund_quota(user.id)
            ticket_keyword_used = False
            raise

        db.session.add(ResponseLog(
            user_id=user.id,
            acct_snapshot=acct,
            notification_id=notification_id,
            status_id=str(posted["id"]),
            response_text=response_text,
            loot_name=loot_name,
            start_value=grade_value,
            defeated_value=defeated,
            reward_value=reward,
            ticket_used=ticket_keyword_used,
            outcome="success",
        ))
        result = "success"

    except Exception as exc:
        log.exception("알림 처리 오류 %s", notification_id)
        result = "error"
        detail = str(exc)[:2000]
    finally:
        # 무시한 알림이나 오류가 난 알림도 처리 완료로 남겨서 같은 알림을 계속 다시 처리하지 않게 한다.
        # 답글을 올린 직후 프로세스가 죽는 경우에 중복 답글이 나가는 것도 이 기록으로 막는다.
        # 단, 일시 정지(bot_paused)로 넘어간 건은 완료 처리하지 않아 정지 해제 후 다시 처리할 수 있게 한다.
        if result != "bot_paused" and not ProcessedNotification.query.filter_by(notification_id=notification_id).first():
            db.session.add(ProcessedNotification(notification_id=notification_id, result=result))
        # 팔로우, 즐겨찾기, 봇 본인 글, 일시정지 등 무관한 알림은 관리자 내역에 남기지 않는다
        if result not in ("success", "not_mention", "self_or_invalid", "ignored", "bot_paused"):
            db.session.add(ResponseLog(
                user_id=user.id if user else None,
                acct_snapshot=(account.get("acct") if 'account' in locals() and account else "unknown"),
                notification_id=notification_id,
                outcome=result,
                detail=detail,
                ticket_used=False,
            ))
        db.session.commit()


def save_cursor(value):
    db.session.execute(
        text("INSERT INTO bot_state(key,value) VALUES ('last_notification_id', :v) "
             "ON CONFLICT (key) DO UPDATE SET value=:v"),
        {"v": value},
    )
    db.session.commit()


def main(flask_app=None):
    token = os.getenv("MASTODON_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("MASTODON_ACCESS_TOKEN 환경변수가 필요합니다.")
    api = Mastodon(
        access_token=token,
        api_base_url=os.getenv("MASTODON_BASE_URL", "https://occm.cc"),
    )
    app = flask_app or create_bot_app()
    with app.app_context():
        db.create_all()
        ensure_schema()
        db.session.commit()
        seed_defaults()
        me = api.account_verify_credentials()
        bot_id = me["id"]
        log.info("봇 로그인 확인: @%s", me.get("acct"))

        cursor_row = db.session.execute(
            text("SELECT value FROM bot_state WHERE key='last_notification_id'")
        ).first()
        cursor = cursor_row[0] if cursor_row else None
        if cursor is None:
            # 처음 켠 날은 이미 쌓여 있던 알림에 답하지 않고 가장 최근 알림부터 시작한다
            latest = api.notifications(limit=1)
            if latest:
                cursor = str(latest[0]["id"])
                save_cursor(cursor)
            log.info("최초 실행: 기존 알림을 건너뜁니다.")

        while True:
            try:
                db.session.remove()
                settings = get_settings()
                # 봇이 일시 정지 상태일 때는 알림 커서를 이동하지 않고 대기한다.
                # 이렇게 해야 정지 해제 후, 일시 정지 중에 쌓인 멘션들을 누락 없이 순서대로 답글 처리할 수 있다.
                if settings.is_paused:
                    time.sleep(max(3, settings.poll_interval_seconds))
                    continue

                # 알림이 한 번에 40개를 넘게 쌓여도 빠지지 않도록, 가져온 개수가 가득 찬 동안은 계속 이어서 가져온다
                while True:
                    page = list(api.notifications(min_id=cursor, limit=NOTIFICATION_PAGE))
                    for notification in sorted(page, key=lambda n: int(n["id"])):
                        # 중간에 일시 정지가 켜졌다면 즉시 중단하여 커서가 미처리 알림을 건너뛰지 않도록 한다
                        cur_settings = get_settings()
                        if cur_settings.is_paused:
                            break
                        process_notification(api, notification, bot_id)
                        cursor = str(notification["id"])
                        save_cursor(cursor)
                    if len(page) < NOTIFICATION_PAGE:
                        break
                time.sleep(max(3, settings.poll_interval_seconds))
            except Exception:
                db.session.rollback()
                log.exception("폴링 오류")
                time.sleep(10)


if __name__ == "__main__":
    main()
