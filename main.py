from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn, os, json, subprocess, tempfile, time
from supabase import create_client

app = FastAPI(title="Trimly.io Backend")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
sb = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

class JobRequest(BaseModel):
    job_id: str
    youtube_url: str
    num_clips: int = 5
    clip_length: int = 30
    aspect_ratio: str = "9:16"
    clips_data: list = []

def update_job(job_id, status, progress, error=None):
    if not sb: return
    data = {"status": status, "progress": progress}
    if error: data["error_message"] = error
    try:
        sb.table("video_jobs").update(data).eq("id", job_id).execute()
        print(f"[{job_id}] {status} {progress}%")
    except Exception as e:
        print(f"DB update error: {e}")

def time_to_sec(t):
    parts = str(t).split(":")
    if len(parts) == 2: return int(parts[0])*60 + float(parts[1])
    if len(parts) == 3: return int(parts[0])*3600 + int(parts[1])*60 + float(parts[2])
    return float(parts[0])

def process_job(job_id, youtube_url, num_clips, clip_length, aspect_ratio, clips_data):
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            # Step 1: Download
            update_job(job_id, "downloading", 10)
            out = os.path.join(tmpdir, "source.mp4")
            cmd = ["yt-dlp", "-f", "best[height<=720][ext=mp4]/best[height<=720]/best",
                   "--output", out, "--no-playlist", youtube_url]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                raise Exception(f"Download failed: {r.stderr[-300:]}")

            # Find downloaded file
            src = None
            for f in os.listdir(tmpdir):
                if f.endswith(".mp4"):
                    src = os.path.join(tmpdir, f)
                    break
            if not src:
                raise Exception("Downloaded file not found")

            # Get duration
            dur_cmd = ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                       "-of", "default=noprint_wrappers=1:nokey=1", src]
            dur_r = subprocess.run(dur_cmd, capture_output=True, text=True)
            video_duration = float(dur_r.stdout.strip() or "0")
            update_job(job_id, "clipping", 40)

            # Step 2: Cut and reframe each clip
            user_id = sb.table("video_jobs").select("user_id").eq("id", job_id).single().execute().data["user_id"]

            for i, clip in enumerate(clips_data[:num_clips]):
                clip_num = i + 1
                progress = 40 + int((i / max(num_clips,1)) * 50)
                update_job(job_id, "reframing", progress)

                try:
                    start = time_to_sec(clip.get("start_time", "0:00"))
                    end = time_to_sec(clip.get("end_time", f"0:{clip_length}"))
                except:
                    start = i * clip_length
                    end = start + clip_length

                # Clamp
                if video_duration > 0:
                    start = min(start, max(0, video_duration - 10))
                    end = min(end, video_duration)
                if end - start < 3:
                    end = start + clip_length

                duration = end - start
                clip_out = os.path.join(tmpdir, f"clip_{clip_num:02d}.mp4")

                # Build video filter for aspect ratio
                if aspect_ratio == "9:16":
                    vf = "scale=1920:1080:force_original_aspect_ratio=increase,crop=607:1080:(iw-607)/2:0,scale=1080:1920"
                elif aspect_ratio == "1:1":
                    vf = "scale=1080:1080:force_original_aspect_ratio=increase,crop=1080:1080"
                else:
                    vf = "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080"

                ffmpeg_cmd = [
                    "ffmpeg", "-y",
                    "-ss", str(start),
                    "-i", src,
                    "-t", str(duration),
                    "-vf", vf,
                    "-c:v", "libx264", "-preset", "fast", "-crf", "26",
                    "-c:a", "aac", "-b:a", "128k",
                    "-movflags", "+faststart",
                    clip_out
                ]
                ff = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, timeout=180)
                if ff.returncode != 0:
                    print(f"FFmpeg error clip {clip_num}: {ff.stderr[-200:]}")
                    continue

                # Upload to Supabase Storage
                storage_path = f"{job_id}/clip_{clip_num:02d}.mp4"
                download_url = None
                try:
                    with open(clip_out, "rb") as f:
                        sb.storage.from_("trimly-clips").upload(
                            storage_path, f.read(),
                            file_options={"content-type": "video/mp4", "upsert": "true"}
                        )
                    download_url = sb.storage.from_("trimly-clips").get_public_url(storage_path)
                except Exception as e:
                    print(f"Upload error: {e}")

                file_size = round(os.path.getsize(clip_out) / (1024*1024), 2) if os.path.exists(clip_out) else 0

                # Save clip record
                sb.table("output_clips").insert({
                    "job_id": job_id,
                    "user_id": user_id,
                    "clip_number": clip_num,
                    "title": clip.get("title", f"Clip {clip_num}"),
                    "start_time": clip.get("start_time", "0:00"),
                    "end_time": clip.get("end_time", "0:30"),
                    "viral_score": clip.get("viralScore", 75),
                    "clip_type": clip.get("clip_type", "highlight"),
                    "storage_path": storage_path,
                    "download_url": download_url,
                    "duration_seconds": int(duration),
                    "file_size_mb": file_size,
                    "status": "done" if download_url else "error"
                }).execute()

            update_job(job_id, "done", 100)
            print(f"[{job_id}] Done!")

        except Exception as e:
            print(f"[{job_id}] Error: {e}")
            update_job(job_id, "error", 0, str(e))

@app.get("/")
def root():
    return {"status": "Trimly.io backend running ✅", "ffmpeg": "checking..."}

@app.get("/health")
def health():
    # Check ffmpeg
    r = subprocess.run(["ffmpeg", "-version"], capture_output=True)
    ffmpeg_ok = r.returncode == 0
    # Check yt-dlp
    r2 = subprocess.run(["yt-dlp", "--version"], capture_output=True)
    ytdlp_ok = r2.returncode == 0
    return {"status": "ok", "ffmpeg": ffmpeg_ok, "yt_dlp": ytdlp_ok, "supabase": sb is not None}

@app.post("/process")
async def process(req: JobRequest, bg: BackgroundTasks):
    update_job(req.job_id, "downloading", 5)
    bg.add_task(process_job, req.job_id, req.youtube_url,
                req.num_clips, req.clip_length, req.aspect_ratio, req.clips_data)
    return {"message": "Processing started", "job_id": req.job_id}

@app.get("/status/{job_id}")
def status(job_id: str):
    if not sb: raise HTTPException(500, "DB not configured")
    r = sb.table("video_jobs").select("id,status,progress,error_message,video_title").eq("id", job_id).single().execute()
    if not r.data: raise HTTPException(404, "Job not found")
    return r.data

@app.get("/clips/{job_id}")
def clips(job_id: str):
    if not sb: raise HTTPException(500, "DB not configured")
    r = sb.table("output_clips").select("*").eq("job_id", job_id).order("clip_number").execute()
    return {"clips": r.data or []}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
