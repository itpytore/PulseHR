# notifications.py
# Вся логика отправки уведомлений сотрудникам:
#   - определение канала по каскадной схеме (Push → Telegram → SMS → Email)
#   - запись в журнал NotificationLog
#   - заглушки для SMS/Telegram/Email (в prod заменяются реальными провайдерами)
# Файл в корне проекта, импортируется из app.py.

from datetime import datetime, timezone
from models import db, User, Survey, PushSubscription, NotificationLog

DAILY_LIMIT = 5          # максимум уведомлений в сутки на сотрудника
SMS_PER_SURVEY_LIMIT = 1 # не более 1 SMS на один опрос на одного сотрудника


def _count_today(user_id: str) -> int:
    """Сколько уведомлений уже отправлено пользователю сегодня."""
    today = datetime.now(timezone.utc).date()
    return NotificationLog.query.filter(
        NotificationLog.user_id == user_id,
        NotificationLog.status.in_(["sent", "delivered"]),
        NotificationLog.created_at >= datetime(today.year, today.month, today.day,
                                               tzinfo=timezone.utc),
    ).count()


def _already_notified(user_id: str, survey_id: str, channel: str) -> bool:
    """Проверяет дедупликацию: уже отправляли это уведомление по этому каналу."""
    return NotificationLog.query.filter_by(
        user_id=user_id, survey_id=survey_id, channel=channel
    ).filter(NotificationLog.status != "failed").first() is not None


def _log(user_id, survey_id, channel, title, body, status="sent", error=None, cost=None):
    log = NotificationLog(
        user_id=user_id, survey_id=survey_id,
        channel=channel, status=status,
        title=title, body=body,
        sent_at=datetime.now(timezone.utc) if status == "sent" else None,
        error_message=error, sms_cost=cost,
    )
    db.session.add(log)
    db.session.commit()
    return log


# ── Channel senders (stubs — replace with real providers) ────────────────────

def _send_push(user: User, survey: Survey, title: str, body: str) -> bool:
    """
    Отправляет Web Push через подписки пользователя.
    В MVP: логирует в консоль. В prod: использует pywebpush.
    """
    subs = PushSubscription.query.filter_by(user_id=user.id, is_active=True).all()
    if not subs:
        return False

    for sub in subs:
        # prod: pywebpush.send_notification(sub.endpoint, sub.p256dh, sub.auth_key, payload)
        print(f"[PUSH → {user.name}] {title}: {body}")

    return True


def _send_sms(user: User, survey: Survey, title: str, body: str) -> bool:
    """
    Отправляет SMS через провайдера.
    В MVP: логирует в консоль. В prod: HTTP-запрос к sms.ru / МТС / SMSC.
    """
    if not user.phone:
        return False
    # prod: requests.get("https://sms.ru/sms/send", params={...})
    print(f"[SMS → {user.phone}] {body[:160]}")
    return True


def _send_telegram(user: User, survey: Survey, title: str, body: str) -> bool:
    """
    Отправляет сообщение в Telegram-бот.
    В MVP: логирует. В prod: requests.post Telegram Bot API.
    """
    if not user.telegram_chat_id:
        return False
    print(f"[TELEGRAM → {user.telegram_chat_id}] {title}: {body}")
    return True


def _send_email(user: User, survey: Survey, title: str, body: str) -> bool:
    """
    Отправляет email через SMTP.
    В MVP: логирует. В prod: smtplib или сервис вроде SendGrid.
    """
    if not user.email:
        return False
    print(f"[EMAIL → {user.email}] {title}: {body}")
    return True


# ── Cascade logic ─────────────────────────────────────────────────────────────

def send_cascade(user: User, survey: Survey):
    """
    Отправляет уведомления ТОЛЬКО через Web Push.
    Telegram, SMS и Email полностью отключены.
    """
    if not user.consent_notifications and not survey.is_critical:
        return

    if _count_today(user.id) >= DAILY_LIMIT and not survey.is_critical:
        return

    ends_str = survey.ends_at.strftime("%d.%m") if survey.ends_at else "N/A"
    title = f"Новый опрос: {survey.title}"
    body = (f"До окончания — {ends_str}. "
            f"Время прохождения — ~{survey.estimated_minutes} мин.")

    if _already_notified(user.id, survey.id, "push"):
        return

    subs = PushSubscription.query.filter_by(
        user_id=user.id,
        is_active=True
    ).count()

    if subs <= 0:
        _log(
            user.id,
            survey.id,
            "push",
            title,
            body,
            status="failed",
            error="Нет активной Web Push подписки"
        )
        return

    ok = _send_push(user, survey, title, body)
    _log(user.id, survey.id, "push", title, body,
         "sent" if ok else "failed")

def notify_survey_published(survey: Survey):
    """
    Вызывается когда HR публикует опрос (статус → active).
    Определяет целевую аудиторию и запускает каскад для каждого.
    """
    roles = survey.target_roles()
    depts = survey.target_depts()

    query = User.query.filter_by(is_active=True)
    users = query.all()

    target = []
    for u in users:
        if not roles and not depts:
            target.append(u)
        elif u.role in roles or (u.department and u.department in depts):
            target.append(u)

    for user in target:
        send_cascade(user, survey)

    return len(target)
