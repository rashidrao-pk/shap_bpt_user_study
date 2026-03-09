from flask import Flask, render_template, request, redirect, url_for, session, jsonify
import sqlite3
from contextlib import closing
import os
import random
import uuid
from datetime import datetime
import os

import pandas as pd
import numpy as np
from scipy.stats import friedmanchisquare

# scikit-posthocs is optional; Nemenyi matrix will be omitted if not installed
try:
    import scikit_posthocs as sp
except ImportError:
    sp = None

DB_PATH = "ratings.db"
EXCEL_PATH = "/static/files/HumanStudyResults.xlsx"  # <--- UniTo lab ratings (place file here)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")  # needed for session


# ---------------------- DB helpers ---------------------- #

def init_db():
    """Create the SQLite DB/tables if they don't exist."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        # Rankings table (per subject × trial × method × rank)
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

        # User profiles (expertise scores)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject_id TEXT UNIQUE NOT NULL,
                expertise_cv INTEGER,
                expertise_xai INTEGER,
                expertise_methods INTEGER,
                updated_at TEXT NOT NULL
            )
            """
        )

        # Trials configuration (admin-configured examples)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trials_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trial_id      TEXT NOT NULL,
                display_name  TEXT NOT NULL,
                raw_image     TEXT NOT NULL,
                aa_image      TEXT NOT NULL,
                bpt_image     TEXT NOT NULL,
                lime_image    TEXT NOT NULL,
                gradcam_image TEXT NOT NULL,
                is_active     INTEGER NOT NULL DEFAULT 1,
                max_per_subject INTEGER,
                created_at    TEXT NOT NULL
            )
            """
        )

        conn.commit()


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


init_db()


# ---------------------- Helpers ---------------------- #

def get_subject_id():
    """Assign a random subject ID per browser session."""
    if "subject_id" not in session:
        session["subject_id"] = f"S-{uuid.uuid4().hex[:8]}"
    return session["subject_id"]


# ---------------------- PUBLIC: main SPA page ---------------------- #

@app.route("/")
def index():
    # SPA: the main user study sits in templates/index.html
    get_subject_id()  # ensure ID exists
    return render_template("index.html")


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
        { "method_id": "GradCAM", "rank": 2 },
        { "method_id": "LIME-1000", "rank": 3 },
        { "method_id": "AA-1000", "rank": 4 }
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


@app.post("/api/profile")
def api_post_profile():
    """
    Save or update a subject's expertise profile.

    Payload:
    {
      "subject_id": "...",
      "expertise_cv": 1-5,
      "expertise_xai": 1-5,
      "expertise_methods": 1-5
    }
    """
    data = request.get_json(silent=True) or {}
    subject_id = data.get("subject_id")
    cv = data.get("expertise_cv")
    xai = data.get("expertise_xai")
    methods = data.get("expertise_methods")

    if not subject_id:
        return jsonify({"error": "subject_id required"}), 400

    now = datetime.utcnow().isoformat()

    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO user_profiles (subject_id, expertise_cv, expertise_xai, expertise_methods, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(subject_id) DO UPDATE SET
              expertise_cv = excluded.expertise_cv,
              expertise_xai = excluded.expertise_xai,
              expertise_methods = excluded.expertise_methods,
              updated_at = excluded.updated_at
            """,
            (subject_id, cv, xai, methods, now),
        )
        conn.commit()

    print(f"[api_profile] saved profile for subject={subject_id}")
    return jsonify({"status": "ok"})


# ---------------------- DASHBOARD SUMMARY (DB-based) ---------------------- #

def _safe_hist_1to5(values):
    hist = {str(k): 0 for k in range(1, 6)}
    for v in values:
        if v is None:
            continue
        try:
            vi = int(v)
        except (TypeError, ValueError):
            continue
        if 1 <= vi <= 5:
            hist[str(vi)] += 1
    return hist


def compute_summary_for_dashboard():
    """
    Summary based on the live web-study data stored in SQLite.
    Returns a dict suitable for /api/summary_for_dashboard.
    """
    with get_db_connection() as conn:
        # Method-level stats
        rows = conn.execute(
            """
            SELECT
                method_id,
                COUNT(*) AS total_votes,
                AVG(rank) AS avg_rank,
                SUM(CASE WHEN rank = 1 THEN 1 ELSE 0 END) AS first_places
            FROM rankings
            GROUP BY method_id
            """
        ).fetchall()

        methods = []
        for r in rows:
            method_id = r["method_id"]
            rank_rows = conn.execute(
                """
                SELECT rank, COUNT(*) AS c
                FROM rankings
                WHERE method_id = ?
                GROUP BY rank
                """,
                (method_id,),
            ).fetchall()
            rank_counts = {str(rr["rank"]): rr["c"] for rr in rank_rows}
            methods.append(
                {
                    "method_id": method_id,
                    "total_votes": int(r["total_votes"]),
                    "first_places": int(r["first_places"] or 0),
                    "avg_rank": float(r["avg_rank"]) if r["avg_rank"] is not None else None,
                    "rank_counts": rank_counts,
                }
            )

        # Subject count
        srow = conn.execute(
            "SELECT COUNT(DISTINCT subject_id) AS n FROM rankings"
        ).fetchone()
        subject_count = int(srow["n"]) if srow and srow["n"] is not None else 0

        # Trials coverage
        trows = conn.execute(
            """
            SELECT
                trial_name,
                COUNT(DISTINCT subject_id) AS n_subjects,
                COUNT(*) AS n_rankings
            FROM rankings
            GROUP BY trial_name
            ORDER BY trial_name
            """
        ).fetchall()

        trials = []
        for tr in trows:
            trials.append(
                {
                    "trial_name": tr["trial_name"],
                    "n_subjects": int(tr["n_subjects"] or 0),
                    "n_rankings": int(tr["n_rankings"] or 0),
                }
            )

        # Expertise histograms
        urows = conn.execute(
            "SELECT expertise_cv, expertise_xai, expertise_methods FROM user_profiles"
        ).fetchall()

        ex_cv_vals = [row["expertise_cv"] for row in urows]
        ex_xai_vals = [row["expertise_xai"] for row in urows]
        ex_methods_vals = [row["expertise_methods"] for row in urows]

        expertise = {
            "n_profiles": len(urows),
            "cv_hist": _safe_hist_1to5(ex_cv_vals),
            "xai_hist": _safe_hist_1to5(ex_xai_vals),
            "methods_hist": _safe_hist_1to5(ex_methods_vals),
        }

        # Friedman / Nemenyi (DB-based)
        # First build a subject × method table of average ranks.
        pivot_rows = conn.execute(
            """
            SELECT subject_id, method_id, AVG(rank) AS avg_rank
            FROM rankings
            GROUP BY subject_id, method_id
            """
        ).fetchall()

    friedman = None
    nemenyi = None

    if pivot_rows:
        df = pd.DataFrame(
            [
                {
                    "Subject": r["subject_id"],
                    "Method": r["method_id"],
                    "Rank": r["avg_rank"],
                }
                for r in pivot_rows
            ]
        )
        pivot = df.pivot_table(index="Subject", columns="Method", values="Rank")

        # optional method order
        desired_order = ["BPT-1000", "GradCAM", "LIME-1000", "AA-1000"]
        cols = [c for c in desired_order if c in pivot.columns]
        if not cols:
            cols = list(pivot.columns)
        pivot = pivot[cols]

        if pivot.shape[0] >= 2 and pivot.shape[1] >= 2:
            # Friedman
            arrays = [pivot[col].values for col in pivot.columns]
            stat, pval = friedmanchisquare(*arrays)
            friedman = {"statistic": float(stat), "pvalue": float(pval)}

            # Nemenyi (if scikit-posthocs available)
            if sp is not None:
                nem = sp.posthoc_nemenyi_friedman(pivot.values)
                nem.index = pivot.columns
                nem.columns = pivot.columns
                nemenyi = {
                    "labels": list(pivot.columns),
                    "matrix": nem.values.tolist(),
                }

    return {
        "source": "live_db",
        "methods": methods,
        "subject_count": subject_count,
        "trials": trials,
        "expertise": expertise,
        "friedman": friedman,
        "nemenyi": nemenyi,
    }


@app.get("/api/summary_for_dashboard")
def api_summary_for_dashboard():
    summary = compute_summary_for_dashboard()
    return jsonify(summary)


# ---------------------- UniTo Lab XLSX SUMMARY ---------------------- #

def compute_unito_lab_summary(xlsx_path=EXCEL_PATH):
    """
    Load HumanStudyResults.xlsx (UniTo lab ratings) and compute:
    - method-level rank distributions
    - subject count
    - per-question coverage
    - Friedman + Nemenyi

    Returns a dict with the same structure as compute_summary_for_dashboard(),
    but without expertise (since XLSX has no expertise info).
    """
    if not os.path.exists(xlsx_path):
        return {"error": f"Excel file not found at: {xlsx_path}"}

    df = pd.read_excel(xlsx_path)

    # Mapping from question to hidden method for each letter A-D
    mapping_question_to_hiddenmethod = {
        "Butterfly": {"A": "AA", "B": "LIME", "C": "BPT", "D": "GradCAM"},
        "Ladybug": {"A": "LIME", "B": "BPT", "C": "GradCAM", "D": "AA"},
        "StreetSign": {"A": "GradCAM", "B": "AA", "C": "BPT", "D": "LIME"},
        "Bench": {"A": "BPT", "B": "AA", "C": "LIME", "D": "GradCAM"},
    }

    # Map short names to method IDs used in the web-study
    method_id_map = {
        "AA": "AA-1000",
        "BPT": "BPT-1000",
        "LIME": "LIME-1000",
        "GradCAM": "GradCAM",
    }

    # 2. Expand each ranked string into (Subject, Question, Method, Rank) rows
    records = []
    for _, row in df.iterrows():
        # The first column is assumed to be Subject ID
        subject = row.iloc[0]
        for q in mapping_question_to_hiddenmethod:
            if q not in row or pd.isna(row[q]):
                continue
            ranked_letters = str(row[q]).strip()
            # best→1, worst→4
            for pos, letter in enumerate(ranked_letters, start=1):
                if letter not in mapping_question_to_hiddenmethod[q]:
                    continue
                short_method = mapping_question_to_hiddenmethod[q][letter]
                method_id = method_id_map.get(short_method, short_method)
                records.append(
                    {
                        "Subject": subject,
                        "Question": q,
                        "Method": method_id,
                        "Rank": pos,
                    }
                )

    if not records:
        return {"error": "No valid records found in Excel file."}

    long = pd.DataFrame(records)

    # Method stats
    methods_summary = []
    method_order = ["BPT-1000", "GradCAM", "LIME-1000", "AA-1000"]
    for m in method_order:
        subset = long[long["Method"] == m]
        if subset.empty:
            continue
        total_votes = len(subset)
        first_places = (subset["Rank"] == 1).sum()
        avg_rank = subset["Rank"].mean()
        counts = subset["Rank"].value_counts().to_dict()
        rank_counts = {str(k): int(v) for k, v in counts.items()}
        methods_summary.append(
            {
                "method_id": m,
                "total_votes": int(total_votes),
                "first_places": int(first_places),
                "avg_rank": float(avg_rank),
                "rank_counts": rank_counts,
            }
        )

    subject_count = long["Subject"].nunique()

    # Per-question coverage
    trials = []
    for q in sorted(long["Question"].unique()):
        dfq = long[long["Question"] == q]
        trials.append(
            {
                "trial_name": q,
                "n_subjects": int(dfq["Subject"].nunique()),
                "n_rankings": int(len(dfq)),
            }
        )

    # Friedman test
    pivot = (
        long.pivot_table(index="Subject", columns="Method", values="Rank")
        .loc[:, [m for m in method_order if m in long["Method"].unique()]]
    )

    friedman = None
    nemenyi = None

    if pivot.shape[0] >= 2 and pivot.shape[1] >= 2:
        arrays = [pivot[col].values for col in pivot.columns]
        stat, pval = friedmanchisquare(*arrays)
        friedman = {"statistic": float(stat), "pvalue": float(pval)}

        if sp is not None:
            nem = sp.posthoc_nemenyi_friedman(pivot.values)
            nem.index = pivot.columns
            nem.columns = pivot.columns
            nemenyi = {
                "labels": list(pivot.columns),
                "matrix": nem.values.tolist(),
            }

    return {
        "source": "unito_lab_xlsx",
        "methods": methods_summary,
        "subject_count": int(subject_count),
        "trials": trials,
        "expertise": None,  # XLSX has no expertise info
        "friedman": friedman,
        "nemenyi": nemenyi,
    }


@app.get("/api/unito_lab_summary")
def api_unito_lab_summary():
    """
    Load ratings from the UniTo Lab Excel file and return a summary
    in the same JSON format as /api/summary_for_dashboard.
    """
    summary = compute_unito_lab_summary(EXCEL_PATH)
    if "error" in summary:
        return jsonify(summary), 400
    return jsonify(summary)


# ---------------------- ADMIN: dashboard & configure ---------------------- #

@app.route("/admin/dashboard")
def admin_dashboard():
    return render_template("admin_dashboard.html")


@app.route("/admin/configure", methods=["GET", "POST"])
def admin_configure():
    """
    Admin page – select *_Input.png and auto-derive the four explanations.
    """
    images_dir = os.path.join(app.root_path, "static", "images")
    if not os.path.isdir(images_dir):
        os.makedirs(images_dir, exist_ok=True)

    # List input candidates: *_Input.png
    input_candidates = []
    for fname in os.listdir(images_dir):
        if fname.endswith("_Input.png"):
            input_candidates.append(fname)
    input_candidates.sort()

    message = None
    error = None

    if request.method == "POST" and "input_image" in request.form:
        input_image = request.form.get("input_image", "").strip()
        trial_id = request.form.get("trial_id", "").strip()
        display_name = request.form.get("display_name", "").strip()
        max_per_subject_raw = request.form.get("max_per_subject", "").strip()
        max_per_subject = int(max_per_subject_raw) if max_per_subject_raw else None

        if not input_image:
            error = "Please choose an input image."
        else:
            base_id = input_image.replace("_Input.png", "")
            if not trial_id:
                trial_id = base_id
            if not display_name:
                display_name = base_id

            raw_image = f"/static/images/{input_image}"
            aa_image = f"/static/images/{base_id}_Partition-1000.png"
            bpt_image = f"/static/images/{base_id}_BPT-1000.png"
            lime_image = f"/static/images/{base_id}_LIME-1000.png"
            gradcam_image = f"/static/images/{base_id}_GradCAM.png"

            now = datetime.utcnow().isoformat()
            with get_db_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO trials_config (
                        trial_id, display_name, raw_image, aa_image, bpt_image,
                        lime_image, gradcam_image, is_active, max_per_subject, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        trial_id,
                        display_name,
                        raw_image,
                        aa_image,
                        bpt_image,
                        lime_image,
                        gradcam_image,
                        max_per_subject,
                        now,
                    ),
                )
                conn.commit()
            message = f"Trial '{display_name}' added."

    # Load all configured trials
    with get_db_connection() as conn:
        trials_rows = conn.execute(
            """
            SELECT *
            FROM trials_config
            ORDER BY created_at ASC
            """
        ).fetchall()

    trials = [dict(r) for r in trials_rows]

    return render_template(
        "admin_configure.html",
        input_candidates=input_candidates,
        trials=trials,
        message=message,
        error=error,
    )


@app.route("/admin/configure/upload", methods=["POST"])
def admin_configure_upload():
    """
    Optional: upload new image files into static/images.
    """
    images_dir = os.path.join(app.root_path, "static", "images")
    os.makedirs(images_dir, exist_ok=True)

    files = request.files.getlist("files")
    saved = 0
    for f in files:
        if not f.filename:
            continue
        filename = f.filename
        # naive save, you can add secure_filename if you like
        f.save(os.path.join(images_dir, filename))
        saved += 1

    print(f"[admin_configure_upload] saved {saved} files")
    return redirect(url_for("admin_configure"))


if __name__ == "__main__":
    # # For local dev
    # app.run(host="0.0.0.0", port=404, debug=True)
    ## FOR GIT
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
