from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    jsonify,
)
import sqlite3
from contextlib import closing
import random
import uuid
from datetime import datetime
from collections import defaultdict

DB_PATH = "ratings.db"

app = Flask(__name__)
app.secret_key = "change_this_to_a_random_secret"  # needed for session


# ---------------------- DB helpers ---------------------- #

def init_db():
    """
    Create the SQLite DB/tables if they don't exist.
    If tables already exist, add any missing columns (simple migration).
    """
    with closing(sqlite3.connect(DB_PATH)) as conn:
        # --- rankings table (old code already used this) ---
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rankings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject_id TEXT NOT NULL,
                trial_id   TEXT NOT NULL,
                trial_name TEXT NOT NULL,
                method_id  TEXT NOT NULL,
                rank       INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

        # --- user_profiles table (new) ---
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject_id    TEXT NOT NULL UNIQUE
                -- other columns may be added later via ALTER TABLE
            )
            """
        )

        # now ensure all expected columns exist in user_profiles
        cur = conn.execute("PRAGMA table_info(user_profiles)")
        cols = {row[1] for row in cur.fetchall()}  # row[1] = column name

        # list of required columns and their SQLite definitions
        needed = {
            "cv_level": "INTEGER",
            "xai_level": "INTEGER",
            "methods_level": "INTEGER",
            "keep_anonymous": "INTEGER NOT NULL DEFAULT 1",
            "created_at": "TEXT NOT NULL DEFAULT ''",
            "updated_at": "TEXT NOT NULL DEFAULT ''",
        }

        for col_name, col_def in needed.items():
            if col_name not in cols:
                conn.execute(
                    f"ALTER TABLE user_profiles ADD COLUMN {col_name} {col_def}"
                )

        conn.commit()


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


init_db()


# ---------------------- Study configuration ---------------------- #
# 4 trials, 4 methods each.
# method_id holds the TRUE method name (AA-1000, BPT-1000, etc.) – hidden from users.

TRIALS = [
    {
        "id": "butterfly",
        "name": "Butterfly",
        "raw_image": "/static/images/47683_Input.png",
        "methods": [
            {"method_id": "AA-1000",   "image": "/static/images/47683_Partition-1000.png"},
            {"method_id": "BPT-1000",  "image": "/static/images/47683_BPT-1000.png"},
            {"method_id": "LIME-1000", "image": "/static/images/47683_LIME-1000.png"},
            {"method_id": "GradCAM",   "image": "/static/images/47683_GradCAM.png"},
        ],
    },
    {
        "id": "ladybug",
        "name": "Ladybug",
        "raw_image": "/static/images/8292_Input.png",
        "methods": [
            {"method_id": "AA-1000",   "image": "/static/images/8292_Partition-1000.png"},
            {"method_id": "BPT-1000",  "image": "/static/images/8292_BPT-1000.png"},
            {"method_id": "LIME-1000", "image": "/static/images/8292_LIME-1000.png"},
            {"method_id": "GradCAM",   "image": "/static/images/8292_GradCAM.png"},
        ],
    },
    {
        "id": "streetsign",
        "name": "Street Sign",
        "raw_image": "/static/images/11346_Input.png",
        "methods": [
            {"method_id": "AA-1000",   "image": "/static/images/11346_Partition-1000.png"},
            {"method_id": "BPT-1000",  "image": "/static/images/11346_BPT-1000.png"},
            {"method_id": "LIME-1000", "image": "/static/images/11346_LIME-1000.png"},
            {"method_id": "GradCAM",   "image": "/static/images/11346_GradCAM.png"},
        ],
    },
    {
        "id": "bench",
        "name": "Bench",
        "raw_image": "/static/images/4203_Input.png",
        "methods": [
            {"method_id": "AA-1000",   "image": "/static/images/4203_Partition-1000.png"},
            {"method_id": "BPT-1000",  "image": "/static/images/4203_BPT-1000.png"},
            {"method_id": "LIME-1000", "image": "/static/images/4203_LIME-1000.png"},
            {"method_id": "GradCAM",   "image": "/static/images/4203_GradCAM.png"},
        ],
    },
]


# ---------------------- Helpers ---------------------- #

def get_subject_id():
    """Assign a random subject ID per browser session."""
    if "subject_id" not in session:
        session["subject_id"] = f"S-{uuid.uuid4().hex[:8]}"
    return session["subject_id"]


# ---------------------- Web routes (HTML templates) ---------------------- #

@app.route("/")
def index():
    """
    Main SPA page (JS-powered study in templates/index.html).
    """
    get_subject_id()  # ensure subject is set
    return render_template("index.html")


@app.route("/trial/<int:idx>", methods=["GET", "POST"])
def trial(idx):
    """
    Optional classic form-based version of the study, kept for reference.
    """
    if idx < 0 or idx >= len(TRIALS):
        return redirect(url_for("thanks"))

    trial = TRIALS[idx]

    if request.method == "POST":
        subject_id = get_subject_id()
        trial_id = trial["id"]
        trial_name = trial["name"]

        method_ids = request.form.getlist("method_id")
        ranks = request.form.getlist("rank")

        if len(method_ids) != 4 or len(ranks) != 4:
            return "Invalid submission", 400

        try:
            ranks_int = [int(r) for r in ranks]
        except ValueError:
            return "Invalid rank values", 400

        if sorted(ranks_int) != [1, 2, 3, 4]:
            return "Ranks must be 1,2,3,4 without repetition", 400

        now = datetime.utcnow().isoformat()

        with get_db_connection() as conn:
            for m_id, r in zip(method_ids, ranks_int):
                conn.execute(
                    """
                    INSERT INTO rankings (subject_id, trial_id, trial_name, method_id, rank, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (subject_id, trial_id, trial_name, m_id, r, now),
                )
            conn.commit()

        if idx + 1 < len(TRIALS):
            return redirect(url_for("trial", idx=idx + 1))
        else:
            return redirect(url_for("thanks"))

    methods_shuffled = trial["methods"][:]
    random.shuffle(methods_shuffled)

    labels = ["A", "B", "C", "D"]
    for label, m in zip(labels, methods_shuffled):
        m["label"] = label

    return render_template(
        "trial.html",
        trial=trial,
        idx=idx,
        total_trials=len(TRIALS),
        methods=methods_shuffled,
    )


@app.route("/thanks")
def thanks():
    return render_template("thanks.html")


@app.route("/admin/dashboard")
def admin_dashboard():
    """Admin page with Chart.js visualization."""
    return render_template("admin_dashboard.html")


# ---------------------- JSON API for SPA / JS frontend ---------------------- #

@app.post("/api/rank")
def api_rank():
    """
    JSON endpoint for the JS-based user study.

    Expects payload:
    {
      "subject_id": "...",
      "trial_id": "butterfly",
      "trial_name": "Butterfly",
      "rankings": [
        { "method_id": "BPT-1000", "rank": 1 },
        ...
      ]
    }
    """
    data = request.get_json(silent=True) or {}
    subject_id = data.get("subject_id")
    trial_id = data.get("trial_id")
    trial_name = data.get("trial_name")
    rankings = data.get("rankings", [])

    if not subject_id or not trial_id or not trial_name or not rankings:
        return jsonify({"error": "Missing fields"}), 400

    try:
        ranks_int = [int(r.get("rank")) for r in rankings]
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid rank values"}), 400

    if sorted(ranks_int) != [1, 2, 3, 4]:
        return jsonify({"error": "Ranks must be 1,2,3,4"}), 400

    now = datetime.utcnow().isoformat()

    with get_db_connection() as conn:
        for r in rankings:
            method_id = r.get("method_id")
            rank_val = int(r.get("rank"))
            conn.execute(
                """
                INSERT INTO rankings (subject_id, trial_id, trial_name, method_id, rank, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (subject_id, trial_id, trial_name, method_id, rank_val, now),
            )
        conn.commit()

    print(f"[api_rank] saved trial={trial_id} from subject={subject_id}")
    return jsonify({"status": "ok"})


# ---------------------- Profile (expertise) API ---------------------- #

@app.get("/api/profile")
def api_get_profile():
    """
    GET /api/profile?subject_id=...
    Returns:
      { "exists": False } or
      {
        "exists": True,
        "keep_anonymous": true/false,
        "cv_level": 1-5 or null,
        "xai_level": 1-5 or null,
        "methods_level": 1-5 or null
      }
    """
    subject_id = request.args.get("subject_id")
    if not subject_id:
        return jsonify({"error": "subject_id required"}), 400

    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT subject_id, cv_level, xai_level, methods_level, keep_anonymous
            FROM user_profiles
            WHERE subject_id = ?
            """,
            (subject_id,),
        ).fetchone()

    if not row:
        return jsonify({"exists": False})

    return jsonify(
        {
            "exists": True,
            "keep_anonymous": bool(row["keep_anonymous"]),
            "cv_level": row["cv_level"],
            "xai_level": row["xai_level"],
            "methods_level": row["methods_level"],
        }
    )


@app.post("/api/profile")
def api_post_profile():
    """
    POST /api/profile
    Payload:
      {
        "subject_id": "...",
        "mode": "anonymous" | "describe",
        // if mode=="describe":
        "cv_level": 1-5,
        "xai_level": 1-5,
        "methods_level": 1-5
      }
    """
    data = request.get_json(silent=True) or {}
    subject_id = data.get("subject_id")
    mode = data.get("mode")

    if not subject_id or mode not in {"anonymous", "describe"}:
        return jsonify({"error": "subject_id and valid mode required"}), 400

    keep_anonymous = 1 if mode == "anonymous" else 0
    cv_level = xai_level = methods_level = None

    if mode == "describe":
        try:
            cv_level = int(data.get("cv_level"))
            xai_level = int(data.get("xai_level"))
            methods_level = int(data.get("methods_level"))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid levels"}), 400

        for v in (cv_level, xai_level, methods_level):
            if v < 1 or v > 5:
                return jsonify({"error": "Levels must be between 1 and 5"}), 400

    now = datetime.utcnow().isoformat()

    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO user_profiles
              (subject_id, cv_level, xai_level, methods_level, keep_anonymous, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(subject_id) DO UPDATE SET
              cv_level      = excluded.cv_level,
              xai_level     = excluded.xai_level,
              methods_level = excluded.methods_level,
              keep_anonymous= excluded.keep_anonymous,
              updated_at    = excluded.updated_at
            """,
            (
                subject_id,
                cv_level,
                xai_level,
                methods_level,
                keep_anonymous,
                now,
                now,
            ),
        )
        conn.commit()

    return jsonify({"status": "ok"})


# ---------------------- Summary helpers & endpoints ---------------------- #

def compute_summary():
    """
    Overall stats per method:
    - total_votes
    - first_places
    - avg_rank
    """
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT method_id,
                   COUNT(*) AS total_votes,
                   AVG(rank) AS avg_rank,
                   SUM(CASE WHEN rank = 1 THEN 1 ELSE 0 END) AS first_places
            FROM rankings
            GROUP BY method_id
            """
        ).fetchall()

    methods = []
    for r in rows:
        methods.append(
            {
                "method_id": r["method_id"],
                "total_votes": int(r["total_votes"]),
                "first_places": int(r["first_places"] or 0),
                "avg_rank": float(r["avg_rank"]) if r["avg_rank"] is not None else None,
            }
        )

    winner = None
    if methods:
        methods_sorted = sorted(
            methods, key=lambda m: (-m["first_places"], m["avg_rank"])
        )
        winner = methods_sorted[0]

    return {"methods": methods, "winner": winner}


@app.get("/api/summary")
def api_summary():
    """JSON summary for global winner info (used on home)."""
    summary = compute_summary()
    return jsonify(summary)


def compute_rank_distribution():
    """
    Build counts[method][rank] from the rankings table (all users).

    Returns:
      {
        "methods": [...],
        "ranks": [1,2,3,4],
        "counts": { "BPT-1000": [c1,c2,c3,c4], ... },
        "num_subjects": N
      }
    """
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT method_id, rank, COUNT(*) AS freq
            FROM rankings
            GROUP BY method_id, rank
            """
        ).fetchall()
        subj_row = conn.execute(
            "SELECT COUNT(DISTINCT subject_id) AS n_subj FROM rankings"
        ).fetchone()

    methods_set = set()
    counts = defaultdict(lambda: {1: 0, 2: 0, 3: 0, 4: 0})

    for r in rows:
        m = r["method_id"]
        rk = int(r["rank"])
        f = int(r["freq"])
        methods_set.add(m)
        if rk in counts[m]:
            counts[m][rk] = f

    preferred_order = ["BPT-1000", "GradCAM", "LIME-1000", "AA-1000"]
    methods = [m for m in preferred_order if m in methods_set] + [
        m for m in sorted(methods_set) if m not in preferred_order
    ]

    ranks = [1, 2, 3, 4]
    counts_by_method = {m: [counts[m][rk] for rk in ranks] for m in methods}

    return {
        "methods": methods,
        "ranks": ranks,
        "counts": counts_by_method,
        "num_subjects": int(subj_row["n_subj"] or 0),
    }


@app.get("/api/rank_distribution")
def api_rank_distribution():
    """JSON for the admin dashboard grouped-bar chart (all users)."""
    data = compute_rank_distribution()
    return jsonify(data)


def compute_rank_distribution_by_expertise():
    """
    Split the rank distribution by user expertise, using `methods_level`
    from user_profiles (1–5). Buckets:

      - beginner:     1–2
      - intermediate: 3
      - expert:       4–5

    Anonymous users (no profile or methods_level is NULL) are ignored here.
    """
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                r.subject_id,
                r.method_id,
                r.rank,
                up.methods_level
            FROM rankings AS r
            LEFT JOIN user_profiles AS up
                ON r.subject_id = up.subject_id
            """
        ).fetchall()

    bucket_names = ["beginner", "intermediate", "expert"]
    counts = {b: defaultdict(lambda: {1: 0, 2: 0, 3: 0, 4: 0})
              for b in bucket_names}
    subjects = {b: set() for b in bucket_names}
    methods_set = set()

    def bucket_for_level(level):
        if level is None:
            return None
        level = int(level)
        if level <= 2:
            return "beginner"
        elif level == 3:
            return "intermediate"
        else:  # 4–5
            return "expert"

    for r in rows:
        lvl = r["methods_level"]
        bucket = bucket_for_level(lvl)
        if bucket is None:
            continue

        m = r["method_id"]
        rk = int(r["rank"])
        if rk not in (1, 2, 3, 4):
            continue

        methods_set.add(m)
        counts[bucket][m][rk] += 1
        subjects[bucket].add(r["subject_id"])

    preferred_order = ["BPT-1000", "GradCAM", "LIME-1000", "AA-1000"]
    methods = [m for m in preferred_order if m in methods_set] + [
        m for m in sorted(methods_set) if m not in preferred_order
    ]
    ranks = [1, 2, 3, 4]

    buckets_out = []
    for b in bucket_names:
        if not subjects[b]:
            continue
        bucket_counts = {
            m: [counts[b][m][rk] for rk in ranks] for m in methods
        }
        buckets_out.append(
            {
                "name": b,
                "num_subjects": len(subjects[b]),
                "counts": bucket_counts,
            }
        )

    return {
        "methods": methods,
        "ranks": ranks,
        "buckets": buckets_out,
    }


@app.get("/api/rank_distribution_by_expertise")
def api_rank_distribution_by_expertise():
    """
    JSON for expertise-aware distributions.
    """
    data = compute_rank_distribution_by_expertise()
    return jsonify(data)


# Optional quick admin text summary
@app.route("/admin_summary")
def admin_summary():
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT trial_name, method_id, COUNT(*) AS votes, AVG(rank) AS avg_rank
            FROM rankings
            GROUP BY trial_name, method_id
            ORDER BY trial_name, avg_rank
            """
        ).fetchall()
    return "<pre>" + "\n".join(
        f"{r['trial_name']}: {r['method_id']} -> votes={r['votes']}, avg_rank={r['avg_rank']:.2f}"
        for r in rows
    ) + "</pre>"


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
