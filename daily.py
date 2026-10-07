import os
import time
import psycopg2
import feedparser
import urllib.parse
import requests
from openai import OpenAI
from brevo import Brevo
from brevo.transactional_emails import SendTransacEmailRequestSender, SendTransacEmailRequestToItem
from datetime import datetime
from dotenv import load_dotenv
from gnews_decoder import from_rss


load_dotenv()

# --- Configuration ---
DATABASE_URL = os.environ["DATABASE_URL"]
DEEPSEEK_API_KEY = os.environ["DEEPSEEK_API_KEY"]
BREVO_API_KEY = os.environ["BREVO_API_KEY"]
FROM_EMAIL = os.environ.get("FROM_EMAIL", "digest@yourdomain.com")
FROM_NAME = os.environ.get("FROM_NAME", "News Digest")

# --- Clients ---
deepseek = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
brevo = Brevo(api_key=BREVO_API_KEY)


def get_db():
    return psycopg2.connect(DATABASE_URL)


def fetch_news_for_topic(topic_name):
    """Fetch last 24h articles with resolved publisher URLs."""
    try:
        articles = []
        for item in from_rss(topic_name, limit=10):
            articles.append({
                'title': item['title'],
                'url': item['url'],  # Resolved publisher URL, not news.google.com
                'snippet': item.get('summary', '')[:500]
            })
        print(f"  Found {len(articles)} articles for '{topic_name}'")
        return articles
    except Exception as e:
        print(f"News fetch failed for {topic_name}: {e}")
        return []
def generate_email(user_name, topics_with_articles):
    """Use DeepSeek to write a personalized digest."""
    articles_text = ""
    for topic, articles in topics_with_articles.items():
        articles_text += f"\n\n## {topic}\n"
        for a in articles:
            articles_text += f"- {a['title']}: {a['snippet']}\n"

    response = deepseek.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a concise news digest writer. Write a friendly email summary "
                    "using bullet points. Include links in markdown format. Keep it under 400 words."
                )
            },
            {
                "role": "user",
                "content": f"Write a daily digest for {user_name} about these topics:\n{articles_text}"
            }
        ],
        max_tokens=800,
        temperature=0.7
    )
    return response.choices[0].message.content


def send_email(to_email, subject, html_content):
    """Send via Brevo."""
    try:
        brevo.transactional_emails.send_transac_email(
            html_content=html_content,
            sender=SendTransacEmailRequestSender(email=FROM_EMAIL, name=FROM_NAME),
            subject=subject,
            to=[SendTransacEmailRequestToItem(email=to_email)]
        )
        return True
    except Exception as e:
        print(f"Email failed for {to_email}: {e}")
        return False


def main():
    print(f"Starting job at {datetime.utcnow()}")
    conn = get_db()
    cur = conn.cursor()

    # 1. Get all unique topics that at least one user follows
    cur.execute("""
        SELECT DISTINCT t.id, t.name
        FROM topics t
        JOIN user_topics ut ON t.id = ut.topic_id;
    """)
    all_topics = cur.fetchall()

    # 2. Fetch news for each topic and cache it
    articles_by_topic = {}
    for topic_id, topic_name in all_topics:
        print(f"Fetching: {topic_name}")
        articles = fetch_news_for_topic(topic_name)
        articles_by_topic[topic_id] = articles

        # Save to news_cache for in-app users
        for a in articles:
            cur.execute(
                """
                INSERT INTO news_cache (topic_id, title, url, summary)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING;
                """,
                (topic_id, a['title'], a['url'], a['snippet'])
            )
    conn.commit()

    # 3. Get users who want EMAIL delivery, ordered by priority
    cur.execute("""
        SELECT u.id, u.email, u.name,
               array_agg(t.id ORDER BY ut.priority) as topic_ids,
               array_agg(ut.priority ORDER BY ut.priority) as priorities
        FROM users u
        JOIN user_topics ut ON u.id = ut.user_id
        JOIN topics t ON ut.topic_id = t.id
        WHERE ut.delivery_method = 'email'
        GROUP BY u.id, u.email, u.name;
    """)
    users = cur.fetchall()

    # 4. Loop through users
    for user_id, email, name, topic_ids, priorities in users:
        # Check if already sent today
        cur.execute(
            "SELECT 1 FROM email_log WHERE user_id = %s AND sent_date = CURRENT_DATE;",
            (user_id,)
        )
        if cur.fetchone():
            print(f"Skipping {email} (already sent)")
            continue

        # Build topic->articles map, respecting priority (lower = more articles)
        user_topics = {}
        max_articles_map = {1: 6, 2: 5, 3: 3, 4: 2, 5: 1}

        for tid, prio in zip(topic_ids, priorities):
            topic_name = next(t[1] for t in all_topics if t[0] == tid)
            articles = articles_by_topic.get(tid, [])
            limit = max_articles_map.get(prio, 3)
            user_topics[topic_name] = articles[:limit]

        if not any(user_topics.values()):
            print(f"No articles for {email}, skipping")
            continue

        try:
            body = generate_email(name or "there", user_topics)
            if send_email(email, "Your Daily News Digest", body):
                cur.execute(
                    "INSERT INTO email_log (user_id) VALUES (%s) ON CONFLICT DO NOTHING;",
                    (user_id,)
                )
                conn.commit()
                print(f"✅ Sent to {email}")
            time.sleep(0.5)
        except Exception as e:
            print(f"Failed for {email}: {e}")
            continue

    cur.close()
    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()