# app.py
import sqlite3
import json
import time
from io import StringIO
from datetime import datetime
import streamlit as st
import pandas as pd
import random
import os
import requests
import unicodedata

SECRET_KEY = os.getenv("SECRET_KEY", "")
DB_PATH = "names.db"

POKEMON_MAX_ID = 1010  # up to Gen 9; safe upper bound

@st.cache_data(show_spinner=False)
def fetch_pokemon_data(poke_id: int):
    """Fetch Pokémon data from PokéAPI with simple retry + fallback."""
    url = f"https://pokeapi.co/api/v2/pokemon/{poke_id}"
    try:
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        data = r.json()
        name = data["name"].capitalize()
        # Official artwork (PNG)
        art = data["sprites"]["other"]["official-artwork"]["front_default"]
        # Try an animated GIF from Pokémon Showdown (fallback to None)
        slug = normalize_for_showdown(data["name"])
        gif_url = f"https://play.pokemonshowdown.com/sprites/ani/{slug}.gif"
        # quick HEAD to see if it exists
        ok = requests.head(gif_url, timeout=5).status_code == 200
        gif = gif_url if ok else None
        return {"name": name, "art": art, "gif": gif}
    except Exception:
        return None

def normalize_for_showdown(name: str) -> str:
    """
    Convert API name to Showdown slug (lowercase, de-accent, spaces->-, etc).
    Works for most standard species.
    """
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    s = s.lower().replace(" ", "-").replace("'", "")
    return s

# ---------- DB helpers ----------
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS suggestions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            user TEXT,
            ts TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS assignments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pokemon TEXT NOT NULL,
            chosen_name TEXT NOT NULL,
            ts TEXT NOT NULL
        )
    """)
    return conn

def add_suggestion(conn, name, user=None):
    conn.execute("INSERT INTO suggestions(name, user, ts) VALUES(?,?,?)",
                 (name.strip(), (user or "").strip(), datetime.utcnow().isoformat()))
    conn.commit()

def list_suggestions(conn):
    df = pd.read_sql_query("SELECT id, name, user, ts FROM suggestions ORDER BY id DESC", conn)
    return df

def clear_suggestions(conn):
    conn.execute("DELETE FROM suggestions")
    conn.commit()

def add_assignment(conn, pokemon, chosen_name):
    conn.execute("INSERT INTO assignments(pokemon, chosen_name, ts) VALUES(?,?,?)",
                 (pokemon.strip(), chosen_name.strip(), datetime.utcnow().isoformat()))
    conn.commit()

def list_assignments(conn):
    df = pd.read_sql_query("SELECT id, pokemon, chosen_name, ts FROM assignments ORDER BY id DESC", conn)
    return df

def clear_assignments(conn):
    conn.execute("DELETE FROM assignments")
    conn.commit()

# ---------- App ----------
st.set_page_config(page_title="Pokémon Name Jar", page_icon="🎲", layout="centered")
st.title("🎲 Pokémon Name Jar")

with st.sidebar:
    st.markdown("### Settings")
    st.write("Use this app to collect name ideas and assign them to your Pokémon.")
    admin_key = st.text_input("Admin key (optional)", type="password",
                              help="Enter a secret to unlock admin actions (clear data).")
    st.markdown("---")
    st.caption("Data is stored in a local SQLite database (`names.db`).")

conn = get_conn()


if "landing_pokemon" not in st.session_state:
    st.session_state.landing_pokemon = fetch_pokemon_data(random.randint(1, POKEMON_MAX_ID))

lp = st.session_state.landing_pokemon
if lp:
    col_a, col_b = st.columns([1,4])
    with col_a:
        st.image(lp["gif"] or lp["art"], width=120)
    with col_b:
        st.subheader(f"Today’s spotlight: {lp['name']}")



# Tabs
tab_collect, tab_assign, tab_export = st.tabs(["📝 Collect Names", "🧢 Assign to Pokémon", "📦 Export"])

# ---------- Collect Tab ----------
with tab_collect:
    st.subheader("Submit a name idea")
    with st.form("name_form", clear_on_submit=True):
        col1, col2 = st.columns([3,2])
        with col1:
            name = st.text_input("Name idea*", placeholder="e.g., Sparky, Luna, Blaze")
        with col2:
            user = st.text_input("Your name (optional)", placeholder="e.g., Ash")
        submitted = st.form_submit_button("Submit")
    if submitted:
        # basic validation
        if not name or not name.strip():
            st.error("Please enter a valid name.")
        elif len(name.strip()) > 50:
            st.error("Name is too long (max 50 characters).")
        else:
            add_suggestion(conn, name=name.strip(), user=user)
            st.success(f"Added “{name.strip()}”!")

    st.markdown("#### Current suggestions")
    sug_df = list_suggestions(conn)
    if sug_df.empty:
        st.info("No names yet. Be the first!")
    else:
        st.dataframe(sug_df.rename(columns={"ts":"timestamp"}), use_container_width=True, height=320)

    # Admin controls
    if admin_key and st.button("⚠️ Clear all suggestions (admin)"):
        clear_suggestions(conn)
        st.success("All suggestions cleared.")
        st.experimental_rerun()

# ---------- Assign Tab ----------
with tab_assign:
    st.subheader("Assign a random name to a Pokémon")

    # (Optional) maintain a small roster the user can type/select
    pokemon_name = st.text_input("Pokémon to name*", placeholder="e.g., Charmander")
    pool_df = list_suggestions(conn)

    colA, colB = st.columns([1,1])
    with colA:
        unique_only = st.toggle("Use unique names (no repeats across assignments)", value=True,
                                help="If on, names already assigned won’t be drawn again.")
    with colB:
        allow_duplicates_in_pool = st.toggle("Include duplicate suggestions in draw", value=False,
                                help="If off, identical suggestions are counted once in the draw.")

    if unique_only:
        assigned = set(list_assignments(conn)["chosen_name"].str.lower().tolist())
    else:
        assigned = set()

    # Build draw pool
    if allow_duplicates_in_pool:
        draw_pool = [n for n in pool_df["name"].tolist() if n.lower() not in assigned]
    else:
        draw_pool = sorted({n.strip(): None for n in pool_df["name"].tolist() if n.strip() and n.lower() not in assigned}.keys())

    st.caption(f"Names available for draw: **{len(draw_pool)}**")

    pick = st.button("🎯 Draw a random name")
    if pick:
        if not pokemon_name.strip():
            st.error("Please enter the Pokémon to name.")
        elif not draw_pool:
            st.warning("No available names to draw from. Collect more first.")
        else:
            chosen = random.choice(draw_pool)
            st.success(f"Chosen name for **{pokemon_name.strip()}**: **{chosen}**")
            add_assignment(conn, pokemon=pokemon_name.strip(), chosen_name=chosen)

    st.markdown("#### Assignments")
    asg_df = list_assignments(conn)
    if asg_df.empty:
        st.info("No assignments yet.")
    else:
        st.dataframe(asg_df.rename(columns={"ts":"timestamp"}), use_container_width=True, height=320)

    # Admin controls
    if admin_key and st.button("⚠️ Clear all assignments (admin)"):
        clear_assignments(conn)
        st.success("All assignments cleared.")
        st.experimental_rerun()

# ---------- Export Tab ----------
with tab_export:
    st.subheader("Export data")
    sug_df = list_suggestions(conn).rename(columns={"ts":"timestamp"})
    asg_df = list_assignments(conn).rename(columns={"ts":"timestamp"})

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("**Suggestions**")
        if sug_df.empty:
            st.caption("No suggestions to export.")
        else:
            csv_buf = StringIO()
            sug_df.to_csv(csv_buf, index=False)
            st.download_button(
                "Download suggestions (CSV)",
                csv_buf.getvalue(),
                file_name=f"suggestions_{int(time.time())}.csv",
                mime="text/csv",
            )
            st.download_button(
                "Download suggestions (JSON)",
                json.dumps(sug_df.to_dict(orient="records"), indent=2),
                file_name=f"suggestions_{int(time.time())}.json",
                mime="application/json",
            )

    with col2:
        st.markdown("**Assignments**")
        if asg_df.empty:
            st.caption("No assignments to export.")
        else:
            csv_buf2 = StringIO()
            asg_df.to_csv(csv_buf2, index=False)
            st.download_button(
                "Download assignments (CSV)",
                csv_buf2.getvalue(),
                file_name=f"assignments_{int(time.time())}.csv",
                mime="text/csv",
            )
            st.download_button(
                "Download assignments (JSON)",
                json.dumps(asg_df.to_dict(orient="records"), indent=2),
                file_name=f"assignments_{int(time.time())}.json",
                mime="application/json",
            )

st.markdown("---")
st.caption("Tip: Share this app’s URL so friends can submit name ideas. You can moderate by clearing suggestions via the admin key.")
