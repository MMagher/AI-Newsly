import streamlit as st
from sqlalchemy import text
import time

# --- Database connection ---
conn = st.connection("neon", type="sql")

# --- Retry helpers for Neon's suspend/wake behavior ---
def run_query_with_retry(query, params=None, retries=3, delay=2):
    """Run a SELECT query, retrying on transient SSL failures."""
    for attempt in range(retries):
        try:
            return conn.query(query, params=params, ttl=0)
        except Exception as e:
            msg = str(e).lower()
            if ("ssl connection has been closed" in msg
                    or "consuming input failed" in msg
                    or "connection was closed" in msg):
                if attempt < retries - 1:
                    time.sleep(delay)
                    continue
            raise
    return None

def run_write_with_retry(sql, params=None, retries=3, delay=2):
    """Run a write (INSERT/UPDATE/DELETE) with retry on transient failures."""
    for attempt in range(retries):
        try:
            with conn.session as session:
                session.execute(text(sql), params or {})
                session.commit()
            return True
        except Exception as e:
            msg = str(e).lower()
            if ("ssl connection has been closed" in msg
                    or "consuming input failed" in msg
                    or "connection was closed" in msg):
                if attempt < retries - 1:
                    time.sleep(delay)
                    continue
            raise
    return False

# --- Authentication Gate ---
if not st.user.is_logged_in:
    st.header("📰 Your Daily News Digest")
    st.subheader("Please log in to continue.")
    st.button("Log in with Google", on_click=st.login)
    st.stop()

# --- User is logged in ---
user_email = st.user.email
user_name = st.user.name

st.title(f"Welcome, {user_name}! 👋")

# --- Register user if new ---
try:
    run_write_with_retry(
        "INSERT INTO users (email, name) VALUES (:email, :name) ON CONFLICT (email) DO NOTHING;",
        {"email": user_email, "name": user_name}
    )
except Exception as e:
    st.error(f"Insert error: {e}")

# --- Get user ID ---
user_df = run_query_with_retry(
    "SELECT id FROM users WHERE email = :email;",
    {"email": user_email}
)

if user_df is None or user_df.empty:
    st.error("User was not found in the database after insert. Check your Neon connection and permissions.")
    st.stop()

user_id = int(user_df.iloc[0]['id'])

# --- Load available topics (global + user's custom) ---
topics_df = run_query_with_retry(
    """
    SELECT id, name, category, created_by
    FROM topics
    WHERE is_active = TRUE
      AND (created_by IS NULL OR created_by = :uid)
    ORDER BY category, name;
    """,
    {"uid": user_id}
)

if topics_df is None:
    st.error("Could not load topics. Try refreshing the page.")
    st.stop()

# --- Get user's current subscriptions ---
current_df = run_query_with_retry(
    """
    SELECT t.id as topic_id, t.name, ut.delivery_method, ut.priority
    FROM user_topics ut
    JOIN topics t ON ut.topic_id = t.id
    WHERE ut.user_id = :uid;
    """,
    {"uid": user_id}
)

current_topic_ids = current_df['topic_id'].tolist() if (current_df is not None and not current_df.empty) else []

# --- Topic selection UI ---
st.subheader("Choose Your Topics")

categories = topics_df['category'].dropna().unique().tolist()
selected_topics = []

for category in categories:
    cat_topics = topics_df[topics_df['category'] == category]
    options = {row['name']: row['id'] for _, row in cat_topics.iterrows()}

    default = [name for name, tid in options.items() if tid in current_topic_ids]

    chosen = st.multiselect(
        f"{category}",
        options=list(options.keys()),
        default=default,
        key=f"cat_{category}"
    )

    for name in chosen:
        selected_topics.append({
            'topic_id': options[name],
            'name': name,
            'category': category
        })

# --- Custom topic creation ---
st.subheader("Create a Custom Topic")
with st.form("custom_topic"):
    new_topic_name = st.text_input("Topic name (e.g., 'Quantum Computing')")
    submitted = st.form_submit_button("Add Topic")

    if submitted and new_topic_name:
        try:
            run_write_with_retry(
                """
                INSERT INTO topics (name, category, created_by)
                VALUES (:name, 'Custom', :uid)
                ON CONFLICT (name) DO NOTHING;
                """,
                {"name": new_topic_name, "uid": user_id}
            )
        except Exception as e:
            st.error(f"Could not create topic: {e}")

        topic_row = run_query_with_retry(
            "SELECT id FROM topics WHERE name = :name;",
            {"name": new_topic_name}
        )

        if topic_row is not None and not topic_row.empty:
            tid = int(topic_row.iloc[0]['id'])
            if tid not in [t['topic_id'] for t in selected_topics]:
                selected_topics.append({
                    'topic_id': tid,
                    'name': new_topic_name,
                    'category': 'Custom'
                })
                st.success(f"Added '{new_topic_name}' — don't forget to save!")
        st.rerun()

# --- Priority setting for selected topics ---
st.subheader("Set Priorities")
st.caption("1 = most important, 5 = least important")

topic_priorities = {}
for topic in selected_topics:
    existing = current_df[current_df['topic_id'] == topic['topic_id']] if current_df is not None else None
    current_priority = int(existing.iloc[0]['priority']) if (existing is not None and not existing.empty) else 3

    topic_priorities[topic['topic_id']] = st.slider(
        f"{topic['name']}",
        min_value=1,
        max_value=5,
        value=current_priority,
        key=f"prio_{topic['topic_id']}"
    )

# --- Delivery preference ---
st.subheader("Delivery Preference")
delivery = st.radio(
    "How would you like your digest?",
    options=["email", "in_app"],
    index=0
)

# --- Save button ---
if st.button("💾 Save Preferences", type="primary"):
    try:
        # Delete old subscriptions first
        run_write_with_retry(
            "DELETE FROM user_topics WHERE user_id = :uid;",
            {"uid": user_id}
        )
        # Insert new subscriptions
        for topic in selected_topics:
            run_write_with_retry(
                """
                INSERT INTO user_topics (user_id, topic_id, delivery_method, priority)
                VALUES (:uid, :tid, :method, :prio);
                """,
                {
                    "uid": user_id,
                    "tid": topic['topic_id'],
                    "method": delivery,
                    "prio": topic_priorities[topic['topic_id']]
                }
            )
        st.success("✅ Preferences saved!")
        st.rerun()
    except Exception as e:
        st.error(f"Save failed: {e}")

# --- In-App News Display ---
st.subheader("📰 Today's News")

news_df = run_query_with_retry(
    """
    SELECT t.name as topic, n.title, n.url, n.summary
    FROM user_topics ut
    JOIN topics t ON ut.topic_id = t.id
    LEFT JOIN news_cache n ON t.id = n.topic_id AND n.cache_date = CURRENT_DATE
    WHERE ut.user_id = :uid AND n.id IS NOT NULL
    ORDER BY ut.priority ASC, t.name;
    """,
    {"uid": user_id}
)

if news_df is None or news_df.empty:
    st.info("No news available yet. Check back tomorrow!")
else:
    for topic in news_df['topic'].unique():
        st.markdown(f"### {topic}")
        topic_news = news_df[news_df['topic'] == topic]
        for _, article in topic_news.iterrows():
            st.markdown(f"- [{article['title']}]({article['url']})")
            if article['summary']:
                st.caption(article['summary'][:200])

# --- Logout ---
st.sidebar.button("Log out", on_click=st.logout)