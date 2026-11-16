from flask import Blueprint, abort, jsonify, request

bp = Blueprint("users", __name__, url_prefix="/users")
USERS = {1: {"name": "ann"}}


@bp.route("/<int:user_id>")
def get_user(user_id):
    if user_id not in USERS:
        abort(404)
    return jsonify(USERS[user_id])


@bp.route("/", methods=["POST"])
def create_user():
    data = request.get_json()
    new_id = max(USERS) + 1
    USERS[new_id] = {"name": data["name"]}
    return jsonify(id=new_id), 201


@bp.route("/")
def list_users():
    limit = request.args.get("limit", 10, type=int)
    return jsonify(list(USERS.values())[:limit])
