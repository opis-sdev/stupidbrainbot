import os
import json
import datetime
import requests
from bs4 import BeautifulSoup
from github import Github
from openai import OpenAI
from flask import Flask, request

# ---------- Environment variables (set in Vercel) ----------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO = os.environ["GITHUB_REPO"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])

# ---------- Clients ----------
ai_client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)
gh = Github(GITHUB_TOKEN)
repo = gh.get_repo(GITHUB_REPO)

# ---------- Vault helper functions (same as before) ----------
def read_file(path):
    try:
        file = repo.get_contents(path)
        return file.decoded_content.decode("utf-8")
    except:
        return None

def ensure_parent_dirs(path):
    parts = path.split('/')
    dirs = parts[:-1]
    current = ""
    for d in dirs:
        current = f"{current}/{d}" if current else d
        if not read_file(current):
            try:
                repo.create_file(f"{current}/.gitkeep", f"Auto-create dir: {current}", "")
            except:
                pass

def write_file(path, content, operation="create", section=None):
    if operation == "create":
        ensure_parent_dirs(path)
        repo.create_file(path, f"Bot create: {path}", content)
    elif operation == "append_to_section":
        existing = read_file(path)
        if existing is None:
            new_content = f"# {path.split('/')[-1]}\n\n{section}\n{content}"
            ensure_parent_dirs(path)
            repo.create_file(path, f"Bot create: {path}", new_content)
        else:
            if section and section in existing:
                updated = existing.replace(section, f"{section}\n{content}", 1)
            else:
                updated = existing + f"\n\n{section}\n{content}"
            file = repo.get_contents(path)
            repo.update_file(path, f"Bot update: {path}", updated, sha=file.sha)

# ---------- Intent classification prompt (same as before) ----------
SYSTEM_PROMPT = """
You are an AI assistant managing an Obsidian vault via GitHub. Today is {today}.
Analyze the user's message and respond ONLY with a JSON object containing:
{{
    "intent": "save_note" | "add_task" | "journal" | "search_vault" | "ask_vault" | "summarize_url" | "help",
    "query": "search query if intent is search_vault or ask_vault",
    "url": "URL if intent is summarize_url",
    "content": "the note or task text",
    "tags": ["tag1", "tag2"],
    "file_path": "suggested file path (if save_note, optional)",
    "section": "section header to append to (optional, default '## Tasks' for add_task, '## Journal' for journal)"
}}

For save_note, if no file_path is given, suggest a path like Inbox/YYYY-MM-DD - Short Title.md.
For add_task, the content should be a checkbox item (start with "- [ ] ").
For journal, content is the text to append under ## Journal in today's daily note.
For summarize_url, include the URL.
For search_vault or ask_vault, include the query.
""".strip()

# ---------- Process a single user message (same logic, returns reply string) ----------
def process_message(text):
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    prompt = SYSTEM_PROMPT.format(today=today)
    try:
        response = ai_client.chat.completions.create(
            model="meta-llama/llama-3.3-70b-instruct:free",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": text},
            ],
            temperature=0.1,
        )
        data = json.loads(response.choices[0].message.content)
    except Exception as e:
        return f"LLM error: {e}"

    intent = data.get("intent", "save_note")

    if intent == "save_note":
        content = data.get("content", text)
        tags = data.get("tags", [])
        file_path = data.get("file_path") or f"Inbox/{today} - {content[:40].replace(' ', '_')}.md"
        yaml_tags = json.dumps(tags)
        note = f"---\ntags: {yaml_tags}\ndate: {today}\n---\n\n{content}"
        try:
            write_file(file_path, note, operation="create")
            return f"✅ Saved note to `{file_path}`"
        except Exception as e:
            return f"❌ Failed to save note: {e}"

    elif intent == "add_task":
        task_text = data.get("content", "")
        if not task_text.startswith("- [ ]"):
            task_text = f"- [ ] {task_text}"
        path = f"Journals/{today}.md"
        section = data.get("section", "## Tasks")
        try:
            write_file(path, task_text + "\n", operation="append_to_section", section=section)
            return "✅ Task added to today's list."
        except Exception as e:
            return f"❌ Failed to add task: {e}"

    elif intent == "journal":
        journal_text = data.get("content", text)
        timestamp = datetime.datetime.now().strftime("%H:%M")
        entry = f"**{timestamp}** – {journal_text}\n"
        path = f"Journals/{today}.md"
        section = "## Journal"
        try:
            write_file(path, entry, operation="append_to_section", section=section)
            return "✅ Journal entry saved."
        except Exception as e:
            return f"❌ Failed to save journal: {e}"

    elif intent == "search_vault":
        query = data.get("query", "")
        if not query:
            return "Please provide a search term."
        try:
            query_str = f"{query} repo:{GITHUB_REPO}"
            result = gh.search_code(query_str)
            results = []
            for item in result:
                if item.path.endswith(".md"):
                    snippet = item.decoded_content[:200] if item.decoded_content else ""
                    results.append(f"• `{item.path}`\n  {snippet}...")
            if results:
                return "Found in:\n" + "\n".join(results[:5])
            else:
                return "No results found."
        except Exception as e:
            return f"Search error: {e}"

    elif intent == "ask_vault":
        query = data.get("query", "")
        if not query:
            return "Please provide a question."
        try:
            query_str = f"{query} repo:{GITHUB_REPO}"
            result = gh.search_code(query_str)
            top_files = []
            for item in result:
                if item.path.endswith(".md"):
                    top_files.append(item.path)
                if len(top_files) >= 3:
                    break
            if not top_files:
                return "I couldn't find any notes about that."
            contents = []
            for path in top_files:
                content = read_file(path)
                if content:
                    contents.append(f"### {path}\n{content}")
            context_text = "\n\n".join(contents)
            answer_prompt = f"Based on the following notes, answer the question: {query}\n\n{context_text}"
            answer = ai_client.chat.completions.create(
                model="meta-llama/llama-3.3-70b-instruct:free",
                messages=[{"role": "user", "content": answer_prompt}],
                temperature=0.3,
            ).choices[0].message.content
            return answer
        except Exception as e:
            return f"Ask error: {e}"

    elif intent == "summarize_url":
        url = data.get("url", "")
        if not url:
            return "Please provide a URL."
        try:
            resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            soup = BeautifulSoup(resp.text, "html.parser")
            for script in soup(["script", "style"]):
                script.decompose()
            text = soup.get_text(separator="\n", strip=True)
            text = text[:5000]
            if not text:
                return "Couldn't extract text from that URL."
            summary_prompt = f"Summarise the following text in 3-5 bullet points:\n\n{text}"
            summary = ai_client.chat.completions.create(
                model="meta-llama/llama-3.3-70b-instruct:free",
                messages=[{"role": "user", "content": summary_prompt}],
                temperature=0.3,
            ).choices[0].message.content
            title = url.rstrip('/').split('/')[-1] or "Summary"
            path = f"Summaries/{today}-{title}.md"
            note = f"---\nsource: {url}\ndate: {today}\n---\n\n# Summary: {title}\n\n{summary}"
            write_file(path, note, operation="create")
            return f"📝 Summary saved to `{path}`\n\n{summary}"
        except Exception as e:
            return f"Summarise error: {e}"

    elif intent == "help":
        return """
🤖 **Second Brain Bot Commands**

- Save a note: "Save this idea: ..."
- Add task: "Add buy groceries to today's tasks"
- Journal: "Journal: had a meeting with John"
- Search: "Search for notes about machine learning"
- Ask: "What did I write about productivity?"
- Summarise URL: "Summarise this article: https://..."
- Help: "help"
        """
    else:
        return "I didn't understand that. Try 'help'."

# ---------- Flask webhook ----------
app = Flask(__name__)

@app.route("/", methods=["POST"])
def webhook():
    update = request.get_json(silent=True)
    if not update:
        return "OK"
    message = update.get("message")
    if not message or "text" not in message:
        return "OK"
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    text = message["text"]

    if user_id != ALLOWED_USER_ID:
        reply = "⛔ Unauthorized"
    else:
        reply = process_message(text)

    send_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(send_url, json={"chat_id": chat_id, "text": reply})
    return "OK"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)