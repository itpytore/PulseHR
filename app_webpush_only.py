# app.py
# Точка входа приложения PulseHR.
# Содержит ВСЕ маршруты Flask. Запуск: python app.py
# При старте автоматически создаёт pulsehr.db и тестового HR-пользователя.

import json
import os
from datetime import datetime, timezone
from collections import defaultdict

from flask import (Flask, render_template, request, redirect,
                   url_for, session, jsonify, abort, flash)

from models import db, User, OTPCode, Survey, Question, SurveyResponse, Answer, NotificationLog, PushSubscription
from auth   import create_otp, verify_otp, create_session, get_user_from_session, logout_user_session, login_required, hr_required
from notifications import notify_survey_published

# ── App setup ─────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.config["SECRET_KEY"]         = os.getenv("SECRET_KEY", "pulsehr-dev-secret-2024")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///pulsehr.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)


@app.context_processor
def inject_user():
    """Делает current_user доступным во всех шаблонах автоматически."""
    return {"current_user": get_user_from_session()}


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET"])
def login():
    if get_user_from_session():
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/auth/send-otp", methods=["POST"])
def send_otp():
    """Создаёт OTP; при первом входе регистрирует пользователя."""
    phone = request.form.get("phone", "").strip()
    name  = request.form.get("name",  "").strip()

    if not phone:
        return jsonify({"error": "Введите номер телефона"}), 400

    user = User.query.filter_by(phone=phone).first()
    if not user:
        if not name:
            return jsonify({"need_name": True, "error": "Введите имя для регистрации"}), 200
        user = User(phone=phone, name=name)
        db.session.add(user)
        db.session.commit()

    code = create_otp(user)
    # В production здесь SMS. В MVP возвращаем в ответе для dev-режима.
    print(f"[OTP] {phone} → {code}")
    return jsonify({"ok": True, "dev_code": code})


@app.route("/auth/verify-otp", methods=["POST"])
def verify_otp_route():
    phone = request.form.get("phone", "").strip()
    code  = request.form.get("code",  "").strip()

    user = User.query.filter_by(phone=phone).first()
    if not user or not verify_otp(user, code):
        return jsonify({"error": "Неверный или истёкший код"}), 400

    token = create_session(user)
    session["token"] = token
    return jsonify({"ok": True, "redirect": url_for("index")})


@app.route("/logout")
def logout():
    logout_user_session()
    return redirect(url_for("login"))


# ── Employee pages ────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    """Главная страница сотрудника — список активных опросов."""
    user = get_user_from_session()
    all_surveys = Survey.query.filter_by(status="active").all()

    # Фильтрация по аудитории
    accessible = []
    for s in all_surveys:
        roles = s.target_roles()
        depts = s.target_depts()
        if not roles and not depts:
            accessible.append(s)
        elif user.role in roles or (user.department and user.department in depts):
            accessible.append(s)

    # Какие уже пройдены
    completed_ids = {
        r.survey_id
        for r in SurveyResponse.query.filter_by(user_id=user.id).all()
    }

    active    = [s for s in accessible if s.id not in completed_ids]
    completed = [s for s in accessible if s.id in completed_ids]

    return render_template("index.html",
                           active=active,
                           completed=completed,
                           total=len(accessible))


@app.route("/surveys")
@login_required
def surveys_list():
    """Страница «Все опросы» для сотрудника — редирект на главную."""
    return redirect(url_for("index"))


@app.route("/survey/<survey_id>")
@login_required
def take_survey(survey_id):
    """Страница прохождения конкретного опроса."""
    user   = get_user_from_session()
    survey = Survey.query.get_or_404(survey_id)

    if survey.status != "active":
        flash("Этот опрос сейчас недоступен.", "warning")
        return redirect(url_for("index"))

    # Уже проходил?
    done = SurveyResponse.query.filter_by(
        survey_id=survey_id, user_id=user.id
    ).first()
    if done:
        flash("Вы уже прошли этот опрос.", "info")
        return redirect(url_for("index"))

    questions = sorted(survey.questions, key=lambda q: q.order)
    return render_template("survey.html", survey=survey, questions=questions)


@app.route("/survey/<survey_id>/submit", methods=["POST"])
@login_required
def submit_survey(survey_id):
    """Принимает ответы формы и сохраняет в БД."""
    user   = get_user_from_session()
    survey = Survey.query.get_or_404(survey_id)

    if survey.status != "active":
        return jsonify({"error": "Опрос не активен"}), 400

    # Дубль-проверка
    if SurveyResponse.query.filter_by(survey_id=survey_id, user_id=user.id).first():
        return jsonify({"error": "Уже пройден"}), 409

    data = request.get_json(force=True)
    answers_data = data.get("answers", {})      # {question_id: value}
    time_spent   = data.get("time_spent", None)

    response = SurveyResponse(
        survey_id=survey_id,
        user_id=None if survey.is_anonymous else user.id,
        session_id=user.id,   # для анонимных — храним только для дедупликации
        time_spent_seconds=time_spent,
    )
    db.session.add(response)
    db.session.flush()

    for qid, val in answers_data.items():
        answer = Answer(
            response_id=response.id,
            question_id=qid,
            value_json=json.dumps(val, ensure_ascii=False),
        )
        db.session.add(answer)

    db.session.commit()
    return jsonify({"ok": True})


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    """Профиль сотрудника: личные данные + настройки Web Push уведомлений."""
    user = get_user_from_session()

    if request.method == "POST":
        user.name       = request.form.get("name",       user.name)
        user.email      = request.form.get("email",      user.email)
        user.department = request.form.get("department", user.department)
        user.position   = request.form.get("position",   user.position)
        user.city       = request.form.get("city",       user.city)

        user.consent_notifications = "consent_notifications" in request.form
        user.notify_push     = "notify_push"     in request.form
        user.notify_sms      = "notify_sms"      in request.form
        user.notify_telegram = "notify_telegram" in request.form
        user.notify_email    = "notify_email"    in request.form

        db.session.commit()
        flash("Профиль сохранён", "success")
        return redirect(url_for("profile"))

    # Активные Push-подписки устройств
    push_subs = PushSubscription.query.filter_by(user_id=user.id, is_active=True).all()
    return render_template("profile.html", user=user, push_subs=push_subs)


# ── Push subscription API ─────────────────────────────────────────────────────

@app.route("/api/push/subscribe", methods=["POST"])
@login_required
def push_subscribe():
    """Браузер регистрирует Web Push подписку после разрешения сотрудника."""
    user = get_user_from_session()
    data = request.get_json(force=True)

    endpoint = data.get("endpoint")
    p256dh   = data.get("keys", {}).get("p256dh")
    auth_key = data.get("keys", {}).get("auth")

    if not all([endpoint, p256dh, auth_key]):
        return jsonify({"error": "Неполные данные подписки"}), 400

    # Обновляем если уже существует
    sub = PushSubscription.query.filter_by(endpoint=endpoint).first()
    if sub:
        sub.p256dh   = p256dh
        sub.auth_key = auth_key
        sub.is_active = True
    else:
        sub = PushSubscription(
            user_id=user.id, endpoint=endpoint,
            p256dh=p256dh, auth_key=auth_key,
            device_name=request.headers.get("User-Agent", "")[:200],
        )
        db.session.add(sub)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/push/unsubscribe", methods=["POST"])
@login_required
def push_unsubscribe():
    user     = get_user_from_session()
    data     = request.get_json(force=True)
    endpoint = data.get("endpoint")
    sub      = PushSubscription.query.filter_by(endpoint=endpoint, user_id=user.id).first()
    if sub:
        sub.is_active = False
        db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/push/vapid-key")
def vapid_key():
    """Публичный VAPID-ключ для регистрации подписки в браузере."""
    key = app.config.get("VAPID_PUBLIC_KEY",
                         "BNbxFSFcm9jE2gPAcNQ8Ox_0F3TcKZ2p7R1hUpXCqMT0V_W4J3OZxNPrKtlqY8P7sGdXyVDgfPi6jQZRe9kI3Ms")
    return jsonify({"public_key": key})


@app.route("/api/notif/click/<log_id>", methods=["POST"])
@login_required
def notif_click(log_id):
    """Отмечает клик по уведомлению (CTR-аналитика)."""
    user = get_user_from_session()
    log  = NotificationLog.query.filter_by(id=log_id, user_id=user.id).first()
    if log and not log.clicked_at:
        log.clicked_at = datetime.now(timezone.utc)
        log.status     = "clicked"
        db.session.commit()
    return jsonify({"ok": True})


# ── HR pages ──────────────────────────────────────────────────────────────────

@app.route("/hr/surveys")
@hr_required
def hr_surveys():
    """HR: список всех опросов с управлением статусами."""
    surveys = Survey.query.order_by(Survey.created_at.desc()).all()
    return render_template("hr_surveys.html", surveys=surveys)


@app.route("/hr/surveys/create", methods=["GET", "POST"])
@hr_required
def hr_create_survey():
    """HR: конструктор опроса — создание + добавление вопросов."""
    user = get_user_from_session()

    if request.method == "POST":
        ends_at_raw = request.form.get("ends_at")
        ends_at     = datetime.fromisoformat(ends_at_raw) if ends_at_raw else None

        roles_raw   = request.form.get("target_roles", "")
        depts_raw   = request.form.get("target_depts", "")
        roles       = [r.strip() for r in roles_raw.split(",") if r.strip()]
        depts       = [d.strip() for d in depts_raw.split(",") if d.strip()]

        survey = Survey(
            title               = request.form["title"],
            description         = request.form.get("description", ""),
            is_anonymous        = "is_anonymous"  in request.form,
            is_critical         = "is_critical"   in request.form,
            estimated_minutes   = int(request.form.get("estimated_minutes", 5)),
            ends_at             = ends_at,
            target_roles_json   = json.dumps(roles, ensure_ascii=False),
            target_depts_json   = json.dumps(depts, ensure_ascii=False),
            created_by          = user.id,
        )
        db.session.add(survey)
        db.session.commit()
        flash("Опрос создан как черновик", "success")
        return redirect(url_for("hr_edit_survey", survey_id=survey.id))

    return render_template("hr_create.html")


@app.route("/hr/surveys/<survey_id>/edit", methods=["GET", "POST"])
@hr_required
def hr_edit_survey(survey_id):
    """HR: редактирование опроса и добавление вопросов."""
    survey    = Survey.query.get_or_404(survey_id)
    questions = sorted(survey.questions, key=lambda q: q.order)

    if request.method == "POST":
        action = request.form.get("action")

        if action == "add_question":
            qtype   = request.form.get("q_type", "single")
            options = None

            if qtype in ("single", "multiple"):
                raw    = request.form.get("options_text", "")
                options = [o.strip() for o in raw.split("\n") if o.strip()]

            elif qtype == "scale":
                options = list(range(0, 11))   # 0..10 для eNPS

            elif qtype == "matrix":
                rows_raw = request.form.get("matrix_rows", "")
                cols_raw = request.form.get("matrix_cols", "")
                options  = {
                    "rows": [r.strip() for r in rows_raw.split("\n") if r.strip()],
                    "cols": [c.strip() for c in cols_raw.split("\n") if c.strip()],
                }

            cond_qid = request.form.get("cond_question_id", "").strip()
            cond_val = request.form.get("cond_value",       "").strip()
            cond_op  = request.form.get("cond_operator",    "eq")
            condition = None
            if cond_qid and cond_val:
                condition = {"question_id": cond_qid, "operator": cond_op, "value": cond_val}

            q = Question(
                survey_id      = survey_id,
                text           = request.form["q_text"],
                type           = qtype,
                order          = len(questions),
                is_required    = "q_required" in request.form,
                options_json   = json.dumps(options, ensure_ascii=False) if options is not None else None,
                condition_json = json.dumps(condition, ensure_ascii=False) if condition else None,
            )
            db.session.add(q)
            db.session.commit()
            flash("Вопрос добавлен", "success")

        elif action == "delete_question":
            qid = request.form.get("question_id")
            q   = Question.query.filter_by(id=qid, survey_id=survey_id).first()
            if q:
                db.session.delete(q)
                db.session.commit()
                flash("Вопрос удалён", "info")

        return redirect(url_for("hr_edit_survey", survey_id=survey_id))

    return render_template("hr_create.html", survey=survey, questions=questions)


@app.route("/hr/surveys/<survey_id>/update-info", methods=["POST"])
@hr_required
def hr_update_survey_info(survey_id):
    """HR: редактировать мета-данные опроса (название, описание и т.д.)."""
    survey = Survey.query.get_or_404(survey_id)
    survey.title             = request.form.get("title", survey.title).strip() or survey.title
    survey.description       = request.form.get("description", survey.description)
    survey.estimated_minutes = int(request.form.get("estimated_minutes", survey.estimated_minutes) or 5)
    survey.is_critical       = "is_critical" in request.form
    ends_at_raw = request.form.get("ends_at", "")
    if ends_at_raw:
        survey.ends_at = datetime.fromisoformat(ends_at_raw)
    roles_raw = request.form.get("target_roles", "")
    depts_raw = request.form.get("target_depts", "")
    survey.target_roles_json = json.dumps([r.strip() for r in roles_raw.split(",") if r.strip()])
    survey.target_depts_json = json.dumps([d.strip() for d in depts_raw.split(",") if d.strip()])
    db.session.commit()
    flash("Информация об опросе обновлена", "success")
    return redirect(url_for("hr_edit_survey", survey_id=survey_id))


@app.route("/hr/surveys/<survey_id>/delete", methods=["POST"])
@hr_required
def hr_delete_survey(survey_id):
    """HR: полное удаление опроса вместе со всеми ответами."""
    survey = Survey.query.get_or_404(survey_id)
    title  = survey.title
    # Каскадно удалятся: questions → answers, responses, notif_logs
    db.session.delete(survey)
    db.session.commit()
    flash(f"Опрос «{title}» удалён безвозвратно.", "info")
    return redirect(url_for("hr_surveys"))


@app.route("/hr/surveys/<survey_id>/status", methods=["POST"])
@hr_required
def hr_change_status(survey_id):
    """HR: изменение статуса опроса (черновик→активный→завершён→архив)."""
    survey     = Survey.query.get_or_404(survey_id)
    new_status = request.form.get("status")

    valid_transitions = {
        "draft":     ["active"],
        "active":    ["completed"],
        "completed": ["archived"],
        "archived":  [],
    }
    if new_status not in valid_transitions.get(survey.status, []):
        flash(f"Недопустимый переход статуса: {survey.status} → {new_status}", "error")
        return redirect(url_for("hr_surveys"))

    survey.status = new_status
    db.session.commit()

    if new_status == "active":
        count = notify_survey_published(survey)
        flash(f"Опрос опубликован. Уведомления отправлены {count} сотрудникам.", "success")
    else:
        flash(f"Статус изменён на «{new_status}»", "info")

    return redirect(url_for("hr_surveys"))


@app.route("/hr/analytics")
@hr_required
def hr_analytics():
    """HR: дашборд аналитики по опросам."""
    surveys = Survey.query.filter(
        Survey.status.in_(["active", "completed"])
    ).order_by(Survey.created_at.desc()).all()

    # Общая сводка
    total_responses = SurveyResponse.query.count()
    total_users     = User.query.filter_by(is_active=True).count()
    active_surveys  = Survey.query.filter_by(status="active").count()

    # Эффективность каналов уведомлений (глобально)
    channel_stats = defaultdict(lambda: {"sent": 0, "clicked": 0})
    for log in NotificationLog.query.all():
        ch = log.channel
        channel_stats[ch]["sent"] += 1
        if log.clicked_at:
            channel_stats[ch]["clicked"] += 1

    return render_template("hr_analytics.html",
                           surveys=surveys,
                           total_responses=total_responses,
                           total_users=total_users,
                           active_surveys=active_surveys,
                           channel_stats=dict(channel_stats))


@app.route("/hr/analytics/<survey_id>")
@hr_required
def hr_analytics_detail(survey_id):
    """HR: детальная аналитика по конкретному опросу (данные для AJAX).
    Необязательные GET-параметры:
      filter_dept     — фильтр по подразделению (только для именных опросов)
      filter_position — фильтр по должности
    """
    from collections import Counter
    survey    = Survey.query.get_or_404(survey_id)
    questions = sorted(survey.questions, key=lambda q: q.order)

    filter_dept     = request.args.get("filter_dept",     "").strip()
    filter_position = request.args.get("filter_position", "").strip()

    # Собираем user_id → user для фильтрации по подразделению/должности
    user_map = {u.id: u for u in User.query.filter_by(is_active=True).all()}

    # Все ответы; фильтруем если нужно
    all_responses = survey.responses
    if filter_dept or filter_position:
        filtered = []
        for r in all_responses:
            if r.user_id is None:
                continue  # анонимные не фильтруем по подразделению
            u = user_map.get(r.user_id)
            if not u:
                continue
            if filter_dept and (u.department or "") != filter_dept:
                continue
            if filter_position and (u.position or "") != filter_position:
                continue
            filtered.append(r)
        responses = filtered
    else:
        responses = all_responses

    # Целевая аудитория
    roles = survey.target_roles()
    depts = survey.target_depts()
    if roles or depts:
        target_size = sum(1 for u in user_map.values()
                          if u.role in roles or (u.department and u.department in depts))
    else:
        target_size = len(user_map)

    completion_rate = round(len(responses) / target_size * 100, 1) if target_size else 0

    # Динамика по дням
    daily = defaultdict(int)
    for r in responses:
        day = r.completed_at.date().isoformat()
        daily[day] += 1
    timeline = [{"date": k, "count": v} for k, v in sorted(daily.items())]

    # Распределение ответчиков по подразделениям и должностям (для pie-чартов)
    dept_counts     = Counter()
    position_counts = Counter()
    for r in responses:
        u = user_map.get(r.user_id) if r.user_id else None
        dept_counts[u.department or "Не указано"] += 1 if u else 0
        position_counts[u.position or "Не указано"] += 1 if u else 0

    # Распределение ответов по вопросам
    q_stats = []
    for q in questions:
        vals = []
        for resp in responses:
            for ans in resp.answers:
                if ans.question_id == q.id:
                    v = ans.value()
                    if v is not None:
                        vals.append(v)

        stat = {
            "id": q.id, "text": q.text, "type": q.type,
            "answer_count": len(vals),
        }

        if q.type in ("single", "multiple"):
            flat = []
            for v in vals:
                if isinstance(v, list): flat.extend(v)
                elif v: flat.append(str(v))
            stat["distribution"] = dict(Counter(flat))

        elif q.type == "scale":
            nums = []
            for v in vals:
                try: nums.append(float(v))
                except: pass
            if nums:
                stat["average"] = round(sum(nums) / len(nums), 2)
                stat["distribution"] = dict(Counter(str(int(n)) for n in nums))
                promoters  = sum(1 for n in nums if n >= 9)
                detractors = sum(1 for n in nums if n <= 6)
                stat["enps"] = round((promoters / len(nums) - detractors / len(nums)) * 100, 1)

        elif q.type == "text":
            stat["text_answers"] = [v for v in vals if v]

        q_stats.append(stat)

    # Каналы уведомлений
    ch_stats = defaultdict(lambda: {"sent": 0, "clicked": 0})
    for log in NotificationLog.query.filter_by(survey_id=survey_id).all():
        ch_stats[log.channel]["sent"] += 1
        if log.clicked_at:
            ch_stats[log.channel]["clicked"] += 1

    # Списки подразделений и должностей для фильтра (из фактических ответчиков)
    all_depts     = sorted({(user_map.get(r.user_id).department or "") for r in all_responses if r.user_id and user_map.get(r.user_id)} - {""})
    all_positions = sorted({(user_map.get(r.user_id).position  or "") for r in all_responses if r.user_id and user_map.get(r.user_id)} - {""})

    return jsonify({
        "survey":               {"title": survey.title, "is_anonymous": survey.is_anonymous},
        "total_responses":      len(responses),
        "total_all":            len(all_responses),
        "target_size":          target_size,
        "completion_rate":      completion_rate,
        "timeline":             timeline,
        "questions":            q_stats,
        "notification_channels": dict(ch_stats),
        "dept_distribution":    dict(dept_counts),
        "position_distribution": dict(position_counts),
        "available_depts":      all_depts,
        "available_positions":  all_positions,
        "active_filter_dept":   filter_dept,
        "active_filter_pos":    filter_position,
    })


@app.route("/hr/users", methods=["GET", "POST"])
@hr_required
def hr_users():
    """HR: список сотрудников, смена ролей."""
    if request.method == "POST":
        user_id = request.form.get("user_id")
        role    = request.form.get("role")
        if role in ("employee", "hr", "admin"):
            u = User.query.get(user_id)
            if u:
                u.role = role
                db.session.commit()
                flash(f"Роль пользователя {u.name} изменена на «{role}»", "success")
        return redirect(url_for("hr_users"))

    search = request.args.get("q", "").strip()
    query  = User.query.filter_by(is_active=True)
    if search:
        query = query.filter(
            db.or_(User.name.ilike(f"%{search}%"),
                   User.phone.ilike(f"%{search}%"),
                   User.department.ilike(f"%{search}%"))
        )
    users = query.order_by(User.name).all()
    return render_template("hr_users.html", users=users, search=search)


# ── Bootstrap ─────────────────────────────────────────────────────────────────

def create_demo_data():
    """Создаёт тестовых пользователей если БД пустая."""
    if User.query.first():
        return

    hr_user = User(
        phone      = "+70000000001",
        name       = "Мария Иванова",
        role       = "hr",
        department = "HR",
        position   = "HR-директор",
        consent_notifications = True,
        notify_push = True,
    )
    emp1 = User(
        phone      = "+70000000002",
        name       = "Иван Петров",
        role       = "employee",
        department = "Разработка",
        position   = "Разработчик",
        consent_notifications = True,
    )
    emp2 = User(
        phone      = "+70000000003",
        name       = "Анна Смирнова",
        role       = "employee",
        department = "Продажи",
        position   = "Менеджер",
        consent_notifications = True,
    )
    emp3 = User(
        phone      = "+70000000004",
        name       = "Михаил Александров",
        role       = "employee",
        department = "Разработка",
        position   = "Дизайнер",
        consent_notifications = True,
    )
    db.session.add_all([hr_user, emp1, emp2, emp3])
    db.session.commit()
    print("✓ Демо-пользователи созданы:")
    print("  HR:   +70000000001")
    print("  Emp1: +70000000002")
    print("  Emp2: +70000000003")


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        create_demo_data()
    app.run(debug=True, port=5000)