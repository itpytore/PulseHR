# models.py
# Описание всех таблиц базы данных через SQLAlchemy ORM.
# Файл находится в корне проекта рядом с app.py.
# При запуске приложения Flask автоматически создаёт pulsehr.db в той же папке.

from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import uuid

db = SQLAlchemy()


def gen_uuid():
    return str(uuid.uuid4())


class User(db.Model):
    """Сотрудник или HR-специалист. Авторизация по номеру телефона."""
    __tablename__ = "users"

    id         = db.Column(db.String, primary_key=True, default=gen_uuid)
    phone      = db.Column(db.String(20), unique=True, nullable=False, index=True)
    name       = db.Column(db.String(200), nullable=False)
    email      = db.Column(db.String(200))
    department = db.Column(db.String(200))
    position   = db.Column(db.String(200))
    city       = db.Column(db.String(100))
    # Роль: employee | hr | admin
    role       = db.Column(db.String(20), default="employee", nullable=False)
    is_active  = db.Column(db.Boolean, default=True)

    # Уведомления — согласие и каналы
    consent_notifications = db.Column(db.Boolean, default=False)
    notify_push           = db.Column(db.Boolean, default=True)
    notify_sms            = db.Column(db.Boolean, default=True)
    notify_telegram       = db.Column(db.Boolean, default=True)
    notify_email          = db.Column(db.Boolean, default=True)
    telegram_chat_id      = db.Column(db.String(100))

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # Связи
    otp_codes     = db.relationship("OTPCode",         back_populates="user", cascade="all, delete-orphan")
    sessions      = db.relationship("UserSession",     back_populates="user", cascade="all, delete-orphan")
    push_subs     = db.relationship("PushSubscription",back_populates="user", cascade="all, delete-orphan")
    responses     = db.relationship("SurveyResponse",  back_populates="user")
    notif_logs    = db.relationship("NotificationLog", back_populates="user")

    # Flask-Login требует эти свойства
    @property
    def is_authenticated(self): return True
    @property
    def is_anonymous(self):     return False
    def get_id(self):           return self.id

    def is_hr(self):
        return self.role in ("hr", "admin")

    def __repr__(self):
        return f"<User {self.name} [{self.role}]>"


class OTPCode(db.Model):
    """Одноразовый код подтверждения для входа по SMS."""
    __tablename__ = "otp_codes"

    id         = db.Column(db.String, primary_key=True, default=gen_uuid)
    user_id    = db.Column(db.String, db.ForeignKey("users.id"), nullable=False)
    code       = db.Column(db.String(10), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used       = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    user = db.relationship("User", back_populates="otp_codes")


class UserSession(db.Model):
    """Активная сессия пользователя (токен в cookie)."""
    __tablename__ = "user_sessions"

    id         = db.Column(db.String, primary_key=True, default=gen_uuid)
    user_id    = db.Column(db.String, db.ForeignKey("users.id"), nullable=False)
    token      = db.Column(db.String(200), unique=True, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    user = db.relationship("User", back_populates="sessions")


class PushSubscription(db.Model):
    """Web Push подписка браузера сотрудника (одно устройство = одна запись)."""
    __tablename__ = "push_subscriptions"

    id          = db.Column(db.String, primary_key=True, default=gen_uuid)
    user_id     = db.Column(db.String, db.ForeignKey("users.id"), nullable=False)
    endpoint    = db.Column(db.Text, nullable=False)
    p256dh      = db.Column(db.Text, nullable=False)
    auth_key    = db.Column(db.Text, nullable=False)
    device_name = db.Column(db.String(200))
    is_active   = db.Column(db.Boolean, default=True)
    created_at  = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    user = db.relationship("User", back_populates="push_subs")


class Survey(db.Model):
    """Опрос, созданный HR-специалистом."""
    __tablename__ = "surveys"

    id                  = db.Column(db.String, primary_key=True, default=gen_uuid)
    title               = db.Column(db.String(500), nullable=False)
    description         = db.Column(db.Text)
    # draft | active | completed | archived
    status              = db.Column(db.String(20), default="draft", nullable=False)
    is_anonymous        = db.Column(db.Boolean, default=False)
    # JSON-список ролей: ["employee","hr"] или [] = все
    target_roles_json   = db.Column(db.Text, default="[]")
    # JSON-список подразделений: ["Бухгалтерия"] или []
    target_depts_json   = db.Column(db.Text, default="[]")
    starts_at           = db.Column(db.DateTime)
    ends_at             = db.Column(db.DateTime)
    estimated_minutes   = db.Column(db.Integer, default=5)
    is_critical         = db.Column(db.Boolean, default=False)
    created_by          = db.Column(db.String, db.ForeignKey("users.id"), nullable=False)
    created_at          = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    questions  = db.relationship("Question",      back_populates="survey",
                                  cascade="all, delete-orphan",
                                  order_by="Question.order")
    responses  = db.relationship("SurveyResponse",back_populates="survey")
    notif_logs = db.relationship("NotificationLog",back_populates="survey")

    def target_roles(self):
        import json
        try: return json.loads(self.target_roles_json or "[]")
        except: return []

    def target_depts(self):
        import json
        try: return json.loads(self.target_depts_json or "[]")
        except: return []

    def response_count(self):
        return len(self.responses)

    def days_left(self):
        if not self.ends_at:
            return None
        delta = self.ends_at.replace(tzinfo=ZoneInfo("UTC")) - datetime.now(timezone.utc)
        return max(0, delta.days)


class Question(db.Model):
    """Вопрос внутри опроса."""
    __tablename__ = "questions"

    id          = db.Column(db.String, primary_key=True, default=gen_uuid)
    survey_id   = db.Column(db.String, db.ForeignKey("surveys.id"), nullable=False)
    text        = db.Column(db.Text, nullable=False)
    # single | multiple | scale | text | matrix
    type        = db.Column(db.String(20), nullable=False)
    order       = db.Column(db.Integer, default=0, nullable=False)
    is_required = db.Column(db.Boolean, default=True)
    # JSON: список строк для single/multiple, список чисел для scale,
    #       {"rows":[], "cols":[]} для matrix, null для text
    options_json = db.Column(db.Text)
    # Условие показа: JSON {"question_id":"...", "operator":"eq", "value":"..."}
    condition_json = db.Column(db.Text)

    survey  = db.relationship("Survey",   back_populates="questions")
    answers = db.relationship("Answer",   back_populates="question")

    def options(self):
        import json
        try: return json.loads(self.options_json) if self.options_json else None
        except: return None

    def condition(self):
        import json
        try: return json.loads(self.condition_json) if self.condition_json else None
        except: return None


class SurveyResponse(db.Model):
    """Факт прохождения опроса сотрудником (или анонимной сессией)."""
    __tablename__ = "survey_responses"

    id                  = db.Column(db.String, primary_key=True, default=gen_uuid)
    survey_id           = db.Column(db.String, db.ForeignKey("surveys.id"), nullable=False)
    # NULL если анонимный опрос
    user_id             = db.Column(db.String, db.ForeignKey("users.id"))
    # Для анонимных: случайный UUID чтобы не дать пройти дважды
    session_id          = db.Column(db.String(200))
    completed_at        = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    time_spent_seconds  = db.Column(db.Integer)

    survey  = db.relationship("Survey",   back_populates="responses")
    user    = db.relationship("User",     back_populates="responses")
    answers = db.relationship("Answer",   back_populates="response", cascade="all, delete-orphan")


class Answer(db.Model):
    """Один ответ на один вопрос в рамках прохождения опроса."""
    __tablename__ = "answers"

    id          = db.Column(db.String, primary_key=True, default=gen_uuid)
    response_id = db.Column(db.String, db.ForeignKey("survey_responses.id"), nullable=False)
    question_id = db.Column(db.String, db.ForeignKey("questions.id"), nullable=False)
    # JSON: строка, число, список строк — зависит от типа вопроса
    value_json  = db.Column(db.Text)

    response = db.relationship("SurveyResponse", back_populates="answers")
    question = db.relationship("Question",       back_populates="answers")

    def value(self):
        import json
        try: return json.loads(self.value_json) if self.value_json is not None else None
        except: return self.value_json


class NotificationLog(db.Model):
    """Журнал каждой попытки отправить уведомление (для аналитики и аудита)."""
    __tablename__ = "notification_logs"

    id            = db.Column(db.String, primary_key=True, default=gen_uuid)
    user_id       = db.Column(db.String, db.ForeignKey("users.id"), nullable=False)
    survey_id     = db.Column(db.String, db.ForeignKey("surveys.id"))
    # push | sms | telegram | email
    channel       = db.Column(db.String(20), nullable=False)
    # pending | sent | delivered | failed | clicked
    status        = db.Column(db.String(20), default="pending")
    title         = db.Column(db.String(500))
    body          = db.Column(db.Text)
    sent_at       = db.Column(db.DateTime)
    clicked_at    = db.Column(db.DateTime)
    error_message = db.Column(db.Text)
    sms_cost      = db.Column(db.Float)
    created_at    = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    user   = db.relationship("User",   back_populates="notif_logs")
    survey = db.relationship("Survey", back_populates="notif_logs")
