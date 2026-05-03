import os
import json
import random
import threading
from typing import Optional

from flask import Flask, render_template, request, session, redirect, jsonify
from flask_socketio import SocketIO, emit, send
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

# ==================================================
# 기본 설정
# ==================================================
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL",
    "sqlite:///mafia_rpg.db"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
SocketIO(app, async_mode="eventlet", cors_allowed_origins="*")

MIN_PLAYERS = 4
MAX_USERNAME_LEN = 20
MIN_PASSWORD_LEN = 4

# ==================================================
# DB 모델
# ==================================================
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    username = db.Column(db.String(30), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)

    level = db.Column(db.Integer, default=1)
    exp = db.Column(db.Integer, default=0)
    gold = db.Column(db.Integer, default=100)
    gem = db.Column(db.Integer, default=0)

    wins = db.Column(db.Integer, default=0)
    loses = db.Column(db.Integer, default=0)

    mafia_mastery = db.Column(db.Integer, default=0)
    doctor_mastery = db.Column(db.Integer, default=0)
    police_mastery = db.Column(db.Integer, default=0)
    citizen_mastery = db.Column(db.Integer, default=0)

    title = db.Column(db.String(50), default="초보 시민")
    inventory = db.Column(db.Text, default="{}")


# ==================================================
# 게임 상태
# ==================================================
game_lock = threading.RLock()

users = {}          # sid -> username
roles = {}          # sid -> role
alive = set()
votes = {}

phase = "waiting"

mafia_target = None
doctor_target = None
police_target = None

used_items = {}

# ==================================================
# 상점
# ==================================================
SHOP = {
    "방탄조끼": 100,
    "투표강화권": 150,
    "독약": 200
}

# ==================================================
# 유틸
# ==================================================
def get_user(username):
    return User.query.filter_by(username=username).first()

def current_user():
    uid = session.get("user_id")
    if uid is None:
        return None
    return db.session.get(User, uid)

def get_inventory(user):
    try:
        return json.loads(user.inventory or "{}")
    except:
        return {}

def save_inventory(user, data):
    user.inventory = json.dumps(data, ensure_ascii=False)

def need_exp(level):
    return max(1, level * 100)

def update_title(user):
    if user.level >= 30:
        user.title = "전설의 지배자"
    elif user.level >= 20:
        user.title = "암흑 군주"
    elif user.level >= 10:
        user.title = "숙련자"
    else:
        user.title = "초보 시민"

def level_up(user):
    for _ in range(100):
        need = need_exp(user.level)
        if user.exp < need:
            break
        user.exp -= need
        user.level += 1
        user.gold += 50

    update_title(user)

def reward_player(username, role, win=False, alive_end=False):
    user = get_user(username)
    if not user:
        return

    user.exp += 30
    user.gold += 20

    if win:
        user.exp += 70
        user.gold += 50
        user.wins += 1
    else:
        user.loses += 1

    if alive_end:
        user.exp += 20

    if role == "마피아":
        user.mafia_mastery += 5
    elif role == "의사":
        user.doctor_mastery += 5
    elif role == "경찰":
        user.police_mastery += 5
    else:
        user.citizen_mastery += 3

    level_up(user)
    db.session.commit()

def reset_game_state():
    global phase, mafia_target, doctor_target, police_target

    roles.clear()
    alive.clear()
    votes.clear()
    used_items.clear()

    mafia_target = None
    doctor_target = None
    police_target = None

    phase = "waiting"

def find_sid_by_name(name):
    for sid, username in users.items():
        if username == name:
            return sid
    return None

def send_dead_list():
    dead = []

    for sid, username in users.items():
        if sid not in alive:
            role = roles.get(sid, "?")
            dead.append(f"{username} ({role})")

    socketio.emit("dead_list", dead)

# ==================================================
# 라우팅
# ==================================================
@app.route("/")
def home():
    user = current_user()
    if not user:
        return redirect("/login")
    return render_template("home.html", user=user)

@app.route("/game")
def game():
    user = current_user()
    if not user:
        return redirect("/login")
    return render_template("game.html", user=user)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = get_user(username)

        if user and check_password_hash(user.password, password):
            session.clear()
            session["user_id"] = user.id
            return redirect("/")

        return render_template("login.html", error="로그인 실패")

    return render_template("login.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if get_user(username):
            return render_template("register.html", error="이미 존재")

        user = User(
            username=username,
            password=generate_password_hash(password)
        )

        db.session.add(user)
        db.session.commit()

        return redirect("/login")

    return render_template("register.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

@app.route("/profile")
def profile():
    user = current_user()
    if not user:
        return jsonify({"error":"login"}),401

    return jsonify({
        "username": user.username,
        "gold": user.gold,
        "gem": user.gem,
        "level": user.level,
        "exp": user.exp,
        "wins": user.wins,
        "loses": user.loses,
        "title": user.title,
        "inventory": get_inventory(user)
    })

@app.route("/buy/<item>", methods=["POST"])
def buy(item):
    user = current_user()
    if not user:
        return jsonify({"error":"login"}),401

    if item not in SHOP:
        return jsonify({"error":"없는 아이템"}),400

    price = SHOP[item]

    if user.gold < price:
        return jsonify({"error":"골드 부족"}),400

    user.gold -= price

    inv = get_inventory(user)
    inv[item] = inv.get(item, 0) + 1
    save_inventory(user, inv)

    db.session.commit()

    return jsonify({
        "ok": True,
        "gold": user.gold,
        "inventory": inv
    })

# ==================================================
# 소켓 입장
# ==================================================
@socketio.on("join")
def join(username):
    remove_list = []

    for sid, name in users.items():
        if name == username:
            remove_list.append(sid)

    for sid in remove_list:
        users.pop(sid, None)
        roles.pop(sid, None)
        alive.discard(sid)
        votes.pop(sid, None)

    users[request.sid] = username

    emit("join_success")
    socketio.emit("user_list", list(users.values()))

# ==================================================
# 게임 시작
# ==================================================
@socketio.on("start_game")
def start_game():
    global phase

    with game_lock:
        if phase not in ("waiting", "end"):
            emit("message", "이미 게임중")
            return

        if len(users) < MIN_PLAYERS:
            emit("message", "4명 이상 필요")
            return

        roles.clear()
        alive.clear()
        votes.clear()
        used_items.clear()

        sids = list(users.keys())

        role_list = ["마피아", "의사", "경찰"]
        role_list += ["시민"] * (len(sids)-3)
        random.shuffle(role_list)

        for sid, role in zip(sids, role_list):
            roles[sid] = role
            alive.add(sid)
            socketio.emit("role", role, to=sid)

        phase = "night"

    send_dead_list()
    socketio.emit("phase", "night")

# ==================================================
# 채팅
# ==================================================
@socketio.on("message")
def chat(msg):
    if request.sid not in users:
        return

    username = users[request.sid]
    send(f"{username}: {msg}", broadcast=True)

# ==================================================
# 아이템 사용
# ==================================================
@socketio.on("use_item")
def use_item(item_name):
    sid = request.sid

    if sid not in users:
        return

    username = users[sid]
    user = get_user(username)

    inv = get_inventory(user)

    if inv.get(item_name, 0) <= 0:
        emit("message", "아이템 없음")
        return

    inv[item_name] -= 1
    if inv[item_name] <= 0:
        del inv[item_name]

    save_inventory(user, inv)
    db.session.commit()

    used_items[sid] = item_name

    emit("inventory_update", inv)
    emit("message", f"{item_name} 사용 완료")

# ==================================================
# 연결 종료
# ==================================================
@socketio.on("disconnect")
def disconnect():
    sid = request.sid

    if sid in users:
        users.pop(sid, None)
        roles.pop(sid, None)
        alive.discard(sid)
        votes.pop(sid, None)

    if len(users) <= 1:
        reset_game_state()
        socketio.emit("phase", "waiting")

    socketio.emit("user_list", list(users.values()))
    send_dead_list()

# ==================================================
# 실행
# ==================================================
if __name__ == "__main__":
    with app.app_context():
        db.create_all()

    port = int(os.environ.get("PORT", 5000))

    socketio.run(
        app,
        host="0.0.0.0",
        port=port,
        allow_unsafe_werkzeug=True
    )
