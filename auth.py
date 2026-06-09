# auth.py
# Вспомогательные функции авторизации:
#   - генерация и проверка OTP-кодов
#   - создание/проверка сессионных токенов
#   - декораторы @login_required и @hr_required для защиты маршрутов
# Файл в корне проекта, импортируется из app.py.

import random
import string
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import session, redirect, url_for, abort
from models import db, User, OTPCode, UserSession

OTP_TTL_MINUTES = 10
SESSION_DAYS    = 7


# ── OTP ──────────────────────────────────────────────────────────────────────

def generate_otp(length: int = 6) -> str:
    """Генерирует числовой OTP-код заданной длины."""
    return "".join(random.choices(string.digits, k=length))


def create_otp(user: User) -> str:
    """Создаёт новый OTP-код в БД и возвращает его значение."""
    code       = generate_otp()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_TTL_MINUTES)
    otp        = OTPCode(user_id=user.id, code=code, expires_at=expires_at)
    db.session.add(otp)
    db.session.commit()
    return code


def verify_otp(user: User, code: str) -> bool:
    """Проверяет OTP; при успехе помечает как использованный."""
    now = datetime.now(timezone.utc)
    otp = OTPCode.query.filter_by(
        user_id=user.id, code=code, used=False
    ).filter(OTPCode.expires_at > now).first()

    if not otp:
        return False
    otp.used = True
    db.session.commit()
    return True


# ── Sessions ──────────────────────────────────────────────────────────────────

def create_session(user: User) -> str:
    """Создаёт запись сессии в БД, возвращает токен для cookie."""
    token      = secrets.token_urlsafe(40)
    expires_at = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    us         = UserSession(user_id=user.id, token=token, expires_at=expires_at)
    db.session.add(us)
    db.session.commit()
    return token


def get_user_from_session() -> User | None:
    """Читает токен из Flask-сессии и возвращает User или None."""
    token = session.get("token")
    if not token:
        return None
    now = datetime.now(timezone.utc)
    us  = UserSession.query.filter_by(token=token).first()
    if not us or us.expires_at.replace(tzinfo=timezone.utc) < now:
        return None
    return User.query.filter_by(id=us.user_id, is_active=True).first()


def logout_user_session():
    """Удаляет сессионный токен из БД и из cookie."""
    token = session.pop("token", None)
    if token:
        UserSession.query.filter_by(token=token).delete()
        db.session.commit()


# ── Decorators ────────────────────────────────────────────────────────────────

def login_required(f):
    """Декоратор: перенаправляет на /login если пользователь не авторизован."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = get_user_from_session()
        if not user:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def hr_required(f):
    """Декоратор: возвращает 403 если пользователь не HR/admin."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = get_user_from_session()
        if not user:
            return redirect(url_for("login"))
        if not user.is_hr():
            abort(403)
        return f(*args, **kwargs)
    return wrapper
