from flask import Flask

from users import bp

app = Flask(__name__)
app.register_blueprint(bp)


@app.route("/health")
def health():
    return {"ok": True}


if __name__ == "__main__":
    app.run(port=5000)
