from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import aiosqlite
import os

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_NAME = "database.db"

# Admin IDs
ADMIN_IDS = [123456789] # আপনার টেলিগ্রাম ইউজার আইডি দিয়ে এটি পরিবর্তন করুন

class VideoSchema(BaseModel):
    image: str
    title: str
    ads_required: int

class TaskSchema(BaseModel):
    image: str
    title: str
    redirect_link: str

class SmartLinkSchema(BaseModel):
    url: str

@app.on_event("startup")
async def startup():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS videos (
                            id TEXT PRIMARY KEY, image TEXT, title TEXT, ads_required INTEGER)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS tasks (
                            id TEXT PRIMARY KEY, image TEXT, title TEXT, redirect_link TEXT)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS smart_link (
                            id INTEGER PRIMARY KEY, url TEXT)''')
        # Default smart link if empty
        cursor = await db.execute("SELECT COUNT(*) FROM smart_link")
        count = await cursor.fetchone()
        if count[0] == 0:
            await db.execute("INSERT INTO smart_link (url) VALUES ('https://example.com/default-offer')")
        await db.commit()

@app.get("/api/config")
def get_config():
    return {"admin_ids": ADMIN_IDS}

# Videos APIs
@app.get("/api/videos")
async def get_videos():
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM videos")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

@app.post("/api/videos")
async def add_video(video: VideoSchema):
    import time
    vid_id = f"vid_{int(time.time())}"
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO videos (id, image, title, ads_required) VALUES (?, ?, ?, ?)",
                         (vid_id, video.image, video.title, video.ads_required))
        await db.commit()
    return {"status": "success", "id": vid_id}

@app.delete("/api/videos/{video_id}")
async def delete_video(video_id: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM videos WHERE id=?", (video_id,))
        await db.commit()
    return {"status": "deleted"}

# Tasks APIs
@app.get("/api/tasks")
async def get_tasks():
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM tasks")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

@app.post("/api/tasks")
async def add_task(task: TaskSchema):
    import time
    task_id = f"task_{int(time.time())}"
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO tasks (id, image, title, redirect_link) VALUES (?, ?, ?, ?)",
                         (task_id, task.image, task.title, task.redirect_link))
        await db.commit()
    return {"status": "success", "id": task_id}

@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        await db.commit()
    return {"status": "deleted"}

# Smart Link API
@app.get("/api/smartlink")
async def get_smartlink():
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT url FROM smart_link LIMIT 1")
        row = await cursor.fetchone()
        return {"url": row["url"] if row else ""}

@app.post("/api/smartlink")
async def update_smartlink(data: SmartLinkSchema):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM smart_link")
        await db.execute("INSERT INTO smart_link (url) VALUES (?)", (data.url,))
        await db.commit()
    return {"status": "success"}

# SPA Routing
@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    return FileResponse("public/index.html")
